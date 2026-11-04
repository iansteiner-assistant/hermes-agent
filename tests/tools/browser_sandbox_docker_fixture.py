"""Explicit disposable rootless-Docker acceptance. Run from a staged application checkout, never live profiles.

python tests/tools/browser_sandbox_docker_fixture.py --image EXISTING_IMAGE --root PRIVATE_TASK_DIRECTORY
No network, production mounts, provider credentials, gateway restart or image download. Retain root evidence.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def run_fixture(image, root, docker):
    from tools.browser_sandbox_lifecycle import profile_guard, orphan_proof, container_stop_guard, BrowserBusy
    from tools.browser_sandbox_lifecycle import registered_container_guard, close_owned_browser
    from tools.environments.docker import DockerEnvironment
    from tools.bot_desktop import sandbox_host
    from tools.browser_session_sandbox import _wait_for_port
    from tools import browser_launcher
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    home = root / 'home'
    home.mkdir(mode=0o700)
    # Keep the fixture's host locks isolated; the application helper needs no operator state.
    browser_launcher._shared_profiles_root = lambda: root / 'host-locks'
    task = 'browser-lifecycle-' + uuid.uuid4().hex[:12]
    created, envs, results = [], [], []
    def docker_run(args):
        p = subprocess.run([docker, *args], capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=60, check=False)
        if p.returncode:
            raise RuntimeError(f'fixture docker command failed ({args[0]}): {p.stderr[-500:]}')
        return p.stdout.strip()
    def create():
        container = docker_run(['run','--pull=never','-d','--network=none','--label',f'hermes-browser-test={task}',
                                '--mount',f'type=bind,src={home},dst=/fixturehome',image,'sleep','infinity'])
        created.append(container)
        env = object.__new__(DockerEnvironment)
        env._docker_exe, env._container_id = docker, container
        env._persistent, env._persist_across_processes = True, True
        env._bd_desktop_user, env._bd_desktop_home = None, '/fixturehome'
        envs.append(env)
        return env
    def launch(env, guard):
        profile = sandbox_host.browser_profile_dir(env)
        exe = sandbox_host.chromium_executable(env)
        if not exe:
            raise RuntimeError('installed fixture image has no Chromium')
        guard.command('launch', argv=[exe, f'--user-data-dir={profile}', '--remote-debugging-port=0',
                                     '--headless=new','--no-sandbox','--disable-dev-shm-usage','about:blank'],
                      container=env._container_id)
        return _wait_for_port(env, None, profile)
    def verify_render(env, endpoint):
        from tools.environments import streams
        from tools.browser_cdp_tool import _run_async, _cdp_call
        from tools.browser_readiness import _synthetic_code
        local = streams.forward_port(env, endpoint[0], user=None)
        url = f'ws://127.0.0.1:{local}{endpoint[1]}'
        selected = [None]
        def cdp(method, **params):
            target = None if method.startswith(('Target.', 'Browser.')) else selected[0]
            return _run_async(_cdp_call(url, method, params, target, 5))
        def switch_tab(target):
            selected[0] = target
        def js(expression):
            response = cdp('Runtime.evaluate', expression=expression, returnByValue=True, awaitPromise=True)
            if response.get('exceptionDetails'):
                raise RuntimeError('synthetic marker JavaScript failed')
            return response.get('result', {}).get('value')
        exec(_synthetic_code(uuid.uuid4().hex), {'cdp':cdp, 'switch_tab':switch_tab, 'js':js})
        results.append('real-cdp-rendered-png-marker')
    try:
        security = json.loads(docker_run(['info','--format','{{json .SecurityOptions}}']))
        assert any('rootless' in value for value in security), 'fixture requires rootless engine'
        docker_run(['image','inspect',image])
        # Stopped non-browser containers require a native account path, not exec/fallback absence.
        for has_profile in (False, True):
            stopped = create()
            del stopped._bd_desktop_home
            del stopped._bd_desktop_user
            if has_profile:
                with profile_guard(stopped, sandbox_host.browser_profile_dir(stopped)):
                    pass
            docker_run(['stop','-t','10',stopped._container_id])
            try:
                with container_stop_guard(stopped):
                    assert not has_profile, 'stopped existing profile admitted destructive removal'
                    docker_run(['rm',stopped._container_id])
                    stopped._container_id = None
                    results.append('stopped-nonbrowser-confirmed-absence')
            except RuntimeError:
                if not has_profile:
                    raise
                results.append('stopped-existing-profile-refused')
            if stopped._container_id:
                assert docker_run(['inspect','--format','{{index .Config.Labels "hermes-browser-test"}}',
                                   stopped._container_id]) == task
                docker_run(['rm',stopped._container_id])
                stopped._container_id = None
        first = create()
        profile = sandbox_host.browser_profile_dir(first)
        absence_peer = create()
        with container_stop_guard(first):
            try:
                with profile_guard(absence_peer, profile):
                    raise AssertionError('first launch raced affirmative profile absence')
            except BrowserBusy:
                results.append('affirmative-absence-first-launch-fence')
        docker_run(['rm','-f',absence_peer._container_id])
        absence_peer._container_id = None
        with profile_guard(first, profile) as guard:
            endpoint = launch(first, guard)
            verify_render(first, endpoint)
            owner = guard.command('status')['owner']
            deadline = time.monotonic() + 5
            while not owner.get('singletons') and time.monotonic() < deadline:
                owner = guard.command('status')['owner']
            assert set(owner['singletons']) == {'SingletonLock','SingletonSocket','SingletonCookie'}
            close_owned_browser(guard, owner['generation'])
            state = guard.command('status')
            assert not state['links'] and not state['processes'] and not state['endpoint']
            results.append('clean-exit-and-singleton-release')
            launch(first, guard)
            orphan = guard.command('status')['owner']
            deadline = time.monotonic() + 5
            while not orphan.get('singletons') and time.monotonic() < deadline:
                orphan = guard.command('status')['owner']
        # Crash only the disposable browser, then remove only its exact labelled test container.
        docker_run(['exec',first._container_id,'kill','-KILL',str(orphan['pid'])])
        with profile_guard(first, profile) as guard:
            deadline = time.monotonic() + 5
            orphan = guard.command('status')['owner']
            while orphan.get('exit_code') is None and time.monotonic() < deadline:
                orphan = guard.command('status')['owner']
            assert orphan.get('exit_code') is not None, 'actual child exit status was not finalized'
        docker_run(['rm','-f',first._container_id])
        old_container = first._container_id
        first._container_id = None
        second = create()
        with profile_guard(second, profile) as guard:
            snapshot = guard.command('status')
            assert snapshot['links'] == orphan['singletons']
            try:
                launch(second, guard)
            except RuntimeError as exc:
                assert 'code 21' in str(exc)
            else:
                raise AssertionError('foreign singleton did not reproduce exit21')
            proof = orphan_proof(second, profile, orphan)
            guard.command('quarantine', orphan=orphan, links=snapshot['links'], proof=proof)
            launch(second, guard)
            results.append('recorded-orphan-one-quarantine-and-retry')
            # Cross-container admission uses the same actual bind-backed filesystem inode.
            third = create()
            try:
                with profile_guard(third, profile):
                    raise AssertionError('second container entered reserved profile')
            except BrowserBusy:
                results.append('cross-container-shared-profile-fence')
            docker_run(['rm','-f',third._container_id])
            third._container_id = None
            owner = guard.command('status')['owner']
            close_owned_browser(guard, owner['generation'])
        # The host-held lock survives the container-local helper's exit on stop.
        second_id = second._container_id
        with container_stop_guard(second):
            docker_run(['stop','-t','10',second_id])
            try:
                with registered_container_guard(second_id):
                    raise AssertionError('container restart entered replacement fence')
            except BrowserBusy:
                results.append('replacement-boundary-restart-fence')
            docker_run(['rm',second_id])
        second._container_id = None
        receipt = {'passed':results,'old_container':old_container,'task_label':task}
        (root/'result.json').write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(receipt))
    finally:
        for env in envs:
            env._container_id = None  # prevent environment destructor from owning test cleanup
        for container in created:
            query = subprocess.run([docker,'inspect','--format','{{index .Config.Labels "hermes-browser-test"}}',container],
                                   capture_output=True,text=True,encoding='utf-8',timeout=10,check=False)
            if query.returncode == 0 and query.stdout.strip() == task:
                subprocess.run([docker,'rm','-f',container],capture_output=True,timeout=30,check=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',required=True)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--docker',default='docker')
    arguments = parser.parse_args()
    run_fixture(arguments.image,arguments.root,arguments.docker)
