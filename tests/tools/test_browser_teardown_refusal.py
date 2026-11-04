"""Refusal preserves exact container/bind ownership; absence must be affirmative and fenced."""
import contextlib
import subprocess
import threading
from types import SimpleNamespace

import pytest


def _environment(monkeypatch):
    from tools.environments.docker import DockerEnvironment
    env = object.__new__(DockerEnvironment)
    env._container_id, env._docker_exe = 'fixture-container', 'fixture-docker'
    env._persistent, env._persist_across_processes = False, False
    env._sandbox_mounts = []
    env._workspace_dir = env._home_dir = None
    def synchronous(work, log_id):
        work()
        env._cleanup_thread = SimpleNamespace(is_alive=lambda: False, join=lambda **kw: None)
    monkeypatch.setattr(env, '_start_teardown', synchronous)
    return env


@pytest.mark.parametrize('operation', ['cleanup','stop_idle'])
def test_refused_teardown_repeated_cleanup_and_destructor_preserve_bind_dirs(monkeypatch, operation):
    from tools import browser_sandbox_lifecycle as lifecycle
    env = _environment(monkeypatch)
    deleted = []
    monkeypatch.setattr(env, '_remove_bind_dirs', lambda: deleted.append(True))
    @contextlib.contextmanager
    def refused(*args):
        raise lifecycle.BrowserBusy('human owns browser')
        yield
    monkeypatch.setattr(lifecycle, 'container_stop_guard', refused)
    getattr(env, operation)()
    assert env._container_id == 'fixture-container'
    with pytest.raises(RuntimeError, match='teardown failed'):
        env.wait_for_cleanup()
    env.cleanup()
    env.__del__()
    assert env._container_id == 'fixture-container' and not deleted


def test_failed_docker_removal_retains_identity_and_reports_completed_failure(monkeypatch):
    from tools import browser_sandbox_lifecycle as lifecycle
    from tools.environments import docker
    env = _environment(monkeypatch)
    deleted = []
    monkeypatch.setattr(env, '_remove_bind_dirs', lambda: deleted.append(True))
    monkeypatch.setattr(lifecycle, 'container_stop_guard', lambda *a: contextlib.nullcontext())
    monkeypatch.setattr(docker.subprocess, 'run', lambda argv, **kw: subprocess.CompletedProcess(argv, 1 if argv[1]=='rm' else 0))
    env.cleanup()
    assert env._container_id == 'fixture-container' and not deleted
    with pytest.raises(RuntimeError, match='teardown failed'):
        env.wait_for_cleanup()
    env.__del__()
    assert not deleted


@pytest.fixture
def absence_target(tmp_path, monkeypatch):
    from tools import browser_sandbox_lifecycle as lifecycle
    from tools.bot_desktop import sandbox_host
    root = tmp_path/'locks'
    root.mkdir()
    monkeypatch.setattr(lifecycle, '_host_records', lambda: root)
    monkeypatch.setattr(lifecycle, '_profile_backing_key', lambda *a: 'backing:/same/profile', raising=False)
    monkeypatch.setattr(sandbox_host, 'browser_profile_dir', lambda env: '/profile')
    monkeypatch.setattr(sandbox_host, '_user_for', lambda env: None)
    return SimpleNamespace(_docker_exe='fixture-docker',_container_id='fixture-container',get_temp_dir=lambda:'/tmp')


def test_transport_exit_one_does_not_admit_destructive_body(absence_target, monkeypatch):
    from tools import browser_sandbox_lifecycle as lifecycle
    from tools.environments import streams
    monkeypatch.setattr(streams, 'run_in', lambda *a,**k: subprocess.CompletedProcess([],1,b'',b'docker exec failed'))
    admitted = []
    with pytest.raises(RuntimeError, match='cannot affirm'):
        with lifecycle.container_stop_guard(absence_target):
            admitted.append(True)
    assert not admitted


def test_affirmative_absence_blocks_first_launch_until_teardown_finishes(absence_target, monkeypatch):
    from tools import browser_sandbox_lifecycle as lifecycle
    from tools.environments import streams
    def absent(env, argv, **kw):
        return subprocess.CompletedProcess(argv,lifecycle._PROFILE_ABSENT_RC,('absent:'+argv[-1]).encode(),b'')
    monkeypatch.setattr(streams, 'run_in', absent)
    admitted = []
    @contextlib.contextmanager
    def entered(*args):
        admitted.append(True)
        yield SimpleNamespace()
    monkeypatch.setattr(lifecycle, '_host_profile_guard', lambda *a: contextlib.nullcontext())
    monkeypatch.setattr(lifecycle, '_sandbox_profile_guard', entered)
    attempted, busy = threading.Event(), []
    def first_launch():
        try:
            with lifecycle.profile_guard(absence_target, '/profile'):
                pass
        except lifecycle.BrowserBusy:
            busy.append(True)
        attempted.set()
    with lifecycle.container_stop_guard(absence_target):
        contender = threading.Thread(target=first_launch)
        contender.start()
        assert attempted.wait(5)
        contender.join(5)
        assert busy == [True] and not admitted
    with lifecycle.profile_guard(absence_target, '/profile'):
        pass
    assert admitted == [True]


def test_known_browser_prefers_native_cdp_close_then_waits_without_sigterm(monkeypatch):
    from tools import browser_sandbox_lifecycle as lifecycle, browser_cdp_tool
    from tools.environments import streams
    from tools.bot_desktop import sandbox_host
    calls = []
    async def cdp(url, method, params, target, timeout):
        calls.append(method)
        return {}
    monkeypatch.setattr(browser_cdp_tool, '_cdp_call', cdp)
    monkeypatch.setattr(streams, 'forward_port', lambda *a, **k: 9222)
    monkeypatch.setattr(sandbox_host, '_user_for', lambda env: None)
    def command(operation, **values):
        calls.append((operation, values))
        return {'endpoint': [9222, '/devtools/browser/owned']} if operation == 'owned_endpoint' else {'stopped': True}
    guard = SimpleNamespace(env=object(), command=command)
    assert lifecycle.close_owned_browser(guard, 'owned-generation')['stopped']
    assert calls[1] == 'Browser.close'
    assert calls[-1] == ('stop', {'generation':'owned-generation','timeout':10,'signal':False})


def test_wait_reports_failure_completed_while_joining(monkeypatch):
    env = _environment(monkeypatch)
    def completed(**kwargs):
        env._cleanup_error = RuntimeError('refused during join')
    env._cleanup_thread = SimpleNamespace(is_alive=lambda: False, join=completed)
    with pytest.raises(RuntimeError, match='teardown failed'):
        env.wait_for_cleanup()
    env._container_id = None
