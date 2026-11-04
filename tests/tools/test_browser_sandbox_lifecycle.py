"""Real detached-child status, profile lock and ownership fences against disposable local processes."""
import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest

pytestmark = pytest.mark.platforms('linux')

_STUB = '''#!{python}
import os,signal,socket,sys,threading,json
from pathlib import Path
p=Path(next(a.split('=',1)[1] for a in sys.argv if a.startswith('--user-data-dir=')))
s=socket.socket();s.bind(('127.0.0.1',0));s.listen(8)
def serve():
    while True:
        c,_=s.accept();c.recv(4096)
        body=json.dumps(dict(webSocketDebuggerUrl='ws://127.0.0.1/devtools/browser/stub')).encode()
        c.sendall(b'HTTP/1.1 200 OK\\r\\nContent-Length: '+str(len(body)).encode()+b'\\r\\n\\r\\n'+body);c.close()
threading.Thread(target=serve,daemon=True).start()
for name,target in [('SingletonLock',socket.gethostname()+'-'+str(os.getpid())),('SingletonSocket','/tmp/stub-socket'),('SingletonCookie','stub-cookie')]:
    (p/name).symlink_to(target)
(p/'DevToolsActivePort').write_text(str(s.getsockname()[1])+'\\n/devtools/browser/stub\\n')
def stop(*args):
    for name in ['SingletonLock','SingletonSocket','SingletonCookie']:(p/name).unlink(missing_ok=True)
    sys.exit(0)
signal.signal(signal.SIGTERM,stop)
threading.Event().wait(120)
'''


class LocalSandbox:
    def exec_prefix(self, *, user=None, interactive=True):
        return ['env']


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    from tools import browser_session_sandbox as browser
    from tools.bot_desktop import runtime, sandbox_host, lease
    from tools.environments import streams
    exe = tmp_path / 'chromium'
    exe.write_text(_STUB.format(python=sys.executable))
    exe.chmod(0o700)
    env = LocalSandbox()
    env._container_id = 'fixture-container'
    monkeypatch.setattr(browser, '_launched', {})
    monkeypatch.setattr(browser, '_confirmed', {})
    monkeypatch.setattr(streams, 'forward_port', lambda env, port, user=None: port)
    monkeypatch.setattr(streams, 'forward_failures', lambda env, port: 0)
    monkeypatch.setattr(streams, 'sandbox_temp_dir', lambda env: str(tmp_path))
    monkeypatch.setattr(runtime, '_sandbox_env', lambda create=False: env)
    monkeypatch.setattr(runtime, '_owned_sandbox_env', lambda: env)
    monkeypatch.setattr(runtime, 'published_env', lambda: {'DISPLAY': ':5'})
    monkeypatch.setattr(sandbox_host, '_user_for', lambda env: None)
    monkeypatch.setattr(sandbox_host, 'browser_profile_dir', lambda env: str(tmp_path / 'profile'))
    monkeypatch.setattr(sandbox_host, 'chromium_executable', lambda env: str(exe))
    monkeypatch.setattr(lease, 'human_holds', lambda *a: False)
    yield browser, env, tmp_path / 'profile', exe
    for _, pid, started, _ in browser._launched.values():
        from tools.browser_sandbox_worker import start_time
        if start_time(pid) == started:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)


def test_detached_early_failure_reports_real_exit_and_bounded_diagnostic(sandbox, monkeypatch):
    browser, env, profile, exe = sandbox
    exe.write_text(f'#!{sys.executable}\nimport sys\nsys.stderr.write("x"*30000+" profile in use")\nsys.exit(21)\n')
    monkeypatch.setattr(browser, '_LAUNCH_TIMEOUT_S', 30)
    begin = time.monotonic()
    with pytest.raises(RuntimeError, match='exited with code 21'):
        browser.create_sandbox_session('failure')
    assert time.monotonic() - begin < 15
    from tools.browser_sandbox_lifecycle import profile_guard
    with profile_guard(env, str(profile)) as guard:
        record = guard.command('status')['owner']
        assert record['exit_code'] == 21
        assert len(record['stderr'].encode()) <= 16384


def test_clean_stop_waits_for_process_exit_and_link_release(sandbox):
    browser, _env, profile, _ = sandbox
    info = browser.create_sandbox_session('owned')
    pid = browser._launched[info['launcher_key']][1]
    browser.stop_sandbox_browser(info)
    from tools.browser_sandbox_worker import start_time
    assert start_time(pid) is None
    assert all(not (profile / name).is_symlink() for name in ('SingletonLock','SingletonSocket','SingletonCookie'))
    assert info['launcher_key'] not in browser._launched


def test_other_process_same_profile_cannot_enter_guard(sandbox):
    _, env, profile, _ = sandbox
    from tools.browser_sandbox_lifecycle import profile_guard, install_worker
    helper = install_worker(env)
    with profile_guard(env, str(profile)):
        contender = subprocess.run([sys.executable, helper, 'guard', str(profile)], input='',
                                   capture_output=True, text=True, encoding='utf-8', timeout=5, check=False)
        assert json.loads(contender.stdout)['busy'] is True
    contender = subprocess.run([sys.executable, helper, 'guard', str(profile)], input='',
                               capture_output=True, text=True, encoding='utf-8', timeout=5, check=False)
    assert json.loads(contender.stdout)['ok'] is True


def test_attachment_and_takeover_refuse_at_final_close_boundary(sandbox, monkeypatch):
    browser, env, profile, _ = sandbox
    from tools.browser_sandbox_lifecycle import BrowserBusy, profile_guard
    from tools.bot_desktop import lease, placement
    monkeypatch.setattr(placement, 'resolve', lambda: placement.Placement(placement.TERMINAL, 'docker'))
    info = browser.create_sandbox_session('probe')
    attempted = threading.Event()
    results = []
    def contenders():
        for operation in (lambda: browser.create_sandbox_session('other'), lambda: lease.acquire('human')):
            try:
                operation()
                results.append('admitted')
            except BrowserBusy:
                results.append('busy')
        attempted.set()
    with profile_guard(env, str(profile)):
        contender = threading.Thread(target=contenders)
        contender.start()
        assert attempted.wait(10)
        assert results == ['busy', 'busy']
        assert lease.get().holder == lease.AGENT
        browser.stop_sandbox_browser(info)
        contender.join(5)
    assert not contender.is_alive()


def test_second_holder_prevents_whole_browser_stop(sandbox):
    browser, _, profile, _ = sandbox
    first = browser.create_sandbox_session('first')
    second = browser.create_sandbox_session('second')
    browser.stop_sandbox_browser(first)
    assert (profile / 'SingletonLock').is_symlink()
    browser.stop_sandbox_browser(second)
    assert not (profile / 'SingletonLock').is_symlink()


def test_unknown_owner_and_changed_singletons_never_quarantine(tmp_path):
    from tools import browser_sandbox_worker as worker
    profile = tmp_path / 'profile'
    worker.private_dir(profile / '.hermes-lifecycle')
    for name in worker.LINKS:
        (profile / name).symlink_to('unknown-owner')
    before = worker.links(profile)
    with pytest.raises(RuntimeError, match='proof'):
        worker.quarantine(profile, {'proof': {'absent': True}, 'links': before})
    assert worker.links(profile) == before


def test_recorded_orphan_quarantines_only_exact_symlink_tuple(tmp_path):
    from tools import browser_sandbox_worker as worker
    profile = tmp_path / 'profile'
    state = profile / '.hermes-lifecycle'
    worker.private_dir(state)
    (profile / 'Cookies').write_bytes(b'preserve-cookie-data')
    for name in worker.LINKS:
        (profile / name).symlink_to('old-owner-42')
    snapshot = worker.links(profile)
    old = {'generation':'a'*32,'container':'removed-container','singletons':snapshot}
    worker.write_json(state / ('a'*32+'.json'), old)
    worker.write_json(state / 'owner.json', {'generation':'b'*32})
    worker.write_json(state / ('b'*32+'.json'), {'exit_code':21})
    result = worker.quarantine(profile, {'orphan':old,'links':snapshot,
                                       'proof':{'container':'removed-container','absent':True}})
    quarantine = Path(result['quarantine'])
    assert (profile / 'Cookies').read_bytes() == b'preserve-cookie-data'
    assert all((quarantine / name).is_symlink() for name in worker.LINKS)
    receipt = json.loads((quarantine/'receipt.json').read_text())
    assert receipt['singletons'] == snapshot
    assert all(not (profile / name).is_symlink() for name in worker.LINKS)


def test_host_profile_lock_survives_exec_guard_and_fences_restart(tmp_path, monkeypatch):
    from tools import browser_sandbox_lifecycle as lifecycle
    from tools.environments import streams
    from tools.bot_desktop import sandbox_host
    root = tmp_path / 'host-records'
    root.mkdir()
    monkeypatch.setattr(lifecycle, '_host_records', lambda: root)
    monkeypatch.setattr(lifecycle, '_local_docker_endpoint', lambda docker: 'unix:///fixture')
    monkeypatch.setattr(sandbox_host, '_user_for', lambda env: None)
    monkeypatch.setattr(streams, 'run_in', lambda *a, **k: subprocess.CompletedProcess([],0,b'42:123\n',b''))
    env = type('DockerTarget', (), {'_docker_exe':'fixture', '_container_id':'fixture-container'})()
    attempted = threading.Event()
    results = []
    def restart():
        try:
            with lifecycle.registered_container_guard(env._container_id):
                results.append('admitted')
        except lifecycle.BrowserBusy:
            results.append('busy')
        attempted.set()
    with lifecycle._host_profile_guard(env, '/shared/profile'):
        contender = threading.Thread(target=restart)
        contender.start()
        assert attempted.wait(5)
        contender.join(5)
        assert results == ['busy']
    with lifecycle.registered_container_guard(env._container_id):
        results.append('admitted')
    assert results == ['busy', 'admitted']


def test_human_on_another_home_sharing_profile_remains_a_live_holder(sandbox, tmp_path, monkeypatch):
    _, env, profile, _ = sandbox
    from tools.bot_desktop import lease, placement
    from tools.browser_sandbox_lifecycle import profile_guard, live_holders
    monkeypatch.setattr(placement, 'resolve', lambda: placement.Placement(placement.TERMINAL, 'docker'))
    monkeypatch.setattr(lease, 'human_holds', lambda profile_key=None: lease.get(profile_key).holder == lease.HUMAN)
    other_home = str(tmp_path / 'other-home')
    acquired_outside_lock = []
    def listener(key, changed):
        with profile_guard(env, str(profile)):
            acquired_outside_lock.append(True)
    unsubscribe = lease.on_change(listener)
    try:
        lease.acquire('viewer', profile_key=other_home)
        assert acquired_outside_lock == [True]
        with profile_guard(env, str(profile)) as guard:
            holders = live_holders(guard)
            assert any(owner.get('home') == other_home for owner in holders.values())
        lease.release('viewer', profile_key=other_home)
        with profile_guard(env, str(profile)) as guard:
            assert not live_holders(guard)
    finally:
        unsubscribe()


def test_guard_process_death_releases_its_profile_reservation(sandbox):
    _, env, profile, _ = sandbox
    from tools.browser_sandbox_lifecycle import install_worker
    helper = install_worker(env)
    holder = subprocess.Popen([sys.executable, helper, 'guard', str(profile)], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert json.loads(holder.stdout.readline())['ok']
        holder.kill()
        holder.wait(timeout=5)
        next_holder = subprocess.run([sys.executable, helper, 'guard', str(profile)], input='',
                                     capture_output=True, text=True, encoding='utf-8', timeout=5, check=False)
        assert json.loads(next_holder.stdout)['ok']
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)
        holder.stdin.close()
        holder.stdout.close()
        holder.stderr.close()


def test_incomplete_host_process_inspection_refuses_orphan_proof(monkeypatch):
    import psutil
    from tools.browser_sandbox_lifecycle import _host_profile_in_use
    class Inaccessible:
        def cmdline(self):
            raise psutil.AccessDenied(123)
    monkeypatch.setattr(psutil, 'process_iter', lambda *a: [Inaccessible()])
    with pytest.raises(RuntimeError, match='inspection incomplete'):
        _host_profile_in_use('/profile')
