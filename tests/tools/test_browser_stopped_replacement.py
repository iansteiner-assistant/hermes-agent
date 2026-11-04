"""Replacement exercises the real lifecycle guard against a bounded Unix Engine protocol fixture."""
import copy
import json
import io
import tarfile
import socketserver
import subprocess
import threading
import tempfile
from http.server import BaseHTTPRequestHandler
from types import SimpleNamespace

import pytest


@pytest.fixture
def engine(tmp_path, monkeypatch):
    from tools import browser_sandbox_lifecycle as lifecycle
    from tools.environments import docker
    from tools.bot_desktop import sandbox_host
    cid = 'a' * 64
    value = SimpleNamespace(status=404, api=True, inspect_failure=False, changed=False, calls=[], requests=[],
                            passwd=b'root:x:0:0:root:/root:/bin/bash\n', archive_override=None)
    value.state = {'Id':cid, 'State':{'Status':'exited', 'Pid':0, 'Running':False,
                   'Paused':False, 'Restarting':False, 'Dead':False}}
    socket_root = tempfile.TemporaryDirectory(prefix='browser-engine-')
    path = socket_root.name + '/s'
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            value.requests.append(self.path)
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode='w') as archive:
                member = tarfile.TarInfo('passwd')
                member.size = len(value.passwd)
                archive.addfile(member, io.BytesIO(value.passwd))
            self.send_response(200)
            self.send_header('Api-Version', '1.45')
            self.end_headers()
            self.wfile.write(data.getvalue() if value.archive_override is None else value.archive_override)
        def do_HEAD(self):
            value.requests.append(self.path)
            self.send_response(value.status)
            if value.api:
                self.send_header('Api-Version', '1.45')
            self.end_headers()
        def log_message(self, *args):
            pass
    server = socketserver.UnixStreamServer(path, Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv('DOCKER_HOST', 'unix://' + path)
    records = tmp_path/'locks'
    records.mkdir()
    monkeypatch.setattr(lifecycle, '_host_records', lambda: records)
    monkeypatch.setattr(sandbox_host, 'browser_profile_dir', lambda env:'/fixture/profile')
    monkeypatch.setattr(sandbox_host, '_user_for', lambda env:None)
    def command(argv, **kwargs):
        value.calls.append(argv)
        if argv[1] == 'inspect':
            if value.inspect_failure:
                return subprocess.CompletedProcess(argv,1,b'',b'inspect failed')
            if argv[3] == '{{json .Mounts}}':
                return subprocess.CompletedProcess(argv,0,b'[]',b'')
            snapshot = copy.deepcopy(value.state)
            if value.changed and len(value.requests):
                snapshot['State']['Status'] = 'running'
                snapshot['State']['Running'], snapshot['State']['Pid'] = True, 123
            return subprocess.CompletedProcess(argv,0,json.dumps(snapshot).encode(),b'')
        if argv[1] == 'exec':
            return subprocess.CompletedProcess(argv,1,b'',b'container is not running or exec failed')
        if argv[1:3] == ['rm','-f']:
            return subprocess.CompletedProcess(argv,0,b'',b'')
        raise AssertionError('unexpected Docker call: '+repr(argv))
    monkeypatch.setattr(docker.subprocess, 'run', command)
    env = object.__new__(docker.DockerEnvironment)
    env._container_id, env._docker_exe = None, 'fixture-docker'
    env._image, env._image_pinned = 'new-image', True
    monkeypatch.setattr(env, 'get_temp_dir', lambda:'/tmp')
    monkeypatch.setattr(env, '_find_reusable_container', lambda *a:(cid, 'exited'))
    monkeypatch.setattr(env, '_image_available_locally', lambda:True)
    monkeypatch.setattr(env, '_container_image', lambda cid:'new-image')
    monkeypatch.setattr(env, '_container_network_mode', lambda cid:'bridge')
    value.env, value.cid = env, cid
    yield value
    env._container_id = None
    server.shutdown()
    server.server_close()
    thread.join(2)
    socket_root.cleanup()


@pytest.mark.parametrize('replacement', ['image', 'network'])
def test_stopped_nonbrowser_replacement_uses_real_guard(engine, monkeypatch, replacement):
    if replacement == 'image':
        monkeypatch.setattr(engine.env, '_container_image', lambda cid:'old-image')
    assert engine.env._attach_existing_container('task','profile','', replacement == 'image') is False
    assert [call for call in engine.calls if call[1:3] == ['rm','-f']] == [['fixture-docker','rm','-f',engine.cid]]
    assert engine.requests == ['/containers/'+engine.cid+'/archive?path=%2Fetc%2Fpasswd',
                               '/containers/'+engine.cid+'/archive?path=%2Froot%2F.hermes%2Fbot-desktop%2Fbrowser-profile']
    assert not any(call[1] in ('start','exec') for call in engine.calls)


@pytest.mark.parametrize('failure', ['inspect', 'present', 'transport404', 'changed', 'ambiguous', 'identity', 'passwd', 'archive', 'home', 'oversize', 'cached'])
def test_stopped_replacement_unknown_or_profile_present_refuses(engine, failure):
    mutations = {
        'inspect': lambda: setattr(engine, 'inspect_failure', True),
        'present': lambda: setattr(engine, 'status', 200),
        'transport404': lambda: setattr(engine, 'api', False),
        'changed': lambda: setattr(engine, 'changed', True),
        'ambiguous': lambda: engine.state['State'].update(Status='restarting'),
        'identity': lambda: engine.state.update(Id='b' * 64),
        'passwd': lambda: setattr(engine, 'passwd', b'pn:incomplete\n'),
        'archive': lambda: setattr(engine, 'archive_override', b'invalid'),
        'oversize': lambda: setattr(engine, 'archive_override', b'0' * 131073),
        'cached': lambda: setattr(engine.env, '_bd_desktop_home', '/unproved'),
        'home': lambda: engine.state.update(Env=['HOME=/root', 'HOME=/ambiguous']),
    }
    mutations[failure]()
    with pytest.raises(RuntimeError):
        engine.env._attach_existing_container('task','profile','', False)
    assert not any(call[1] in ('rm','start','exec') for call in engine.calls)


def test_stopped_native_account_home_wins_over_inherited_root(engine):
    engine.passwd += b'pn:x:1000:1000:desktop:/custom-desktop:/bin/bash\n'
    engine.state['Env'] = ['HOME=/root']
    assert engine.env._attach_existing_container('task','profile','', False) is False
    assert engine.requests[-1].endswith('path=%2Fcustom-desktop%2F.hermes%2Fbot-desktop%2Fbrowser-profile')


def test_fresh_running_state_with_failed_exec_refuses_replacement(engine):
    engine.state['State'].update(Status='running', Running=True, Pid=123)
    with pytest.raises(RuntimeError, match='cannot affirm'):
        engine.env._attach_existing_container('task','profile','', False)
    assert not any(call[1] in ('rm','start') for call in engine.calls)
    assert not engine.requests
