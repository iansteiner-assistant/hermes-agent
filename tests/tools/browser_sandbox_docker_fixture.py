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
    from tools.browser_sandbox_lifecycle import registered_container_guard
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
    try:
        security = json.loads(docker_run(['info','--format','{{json .SecurityOptions}}']))
        assert any('rootless' in value for value in security), 'fixture requires rootless engine'
        docker_run(['image','inspect',image])
        first = create()
        profile = sandbox_host.browser_profile_dir(first)
        with profile_guard(first, profile) as guard:
            launch(first, guard)
            owner = guard.command('status')['owner']
            deadline = time.monotonic() + 5
            while not owner.get('singletons') and time.monotonic() < deadline:
                owner = guard.command('status')['owner']
            assert set(owner['singletons']) == {'SingletonLock','SingletonSocket','SingletonCookie'}
            guard.command('stop', generation=owner['generation'], timeout=10)
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
            guard.command('stop', generation=owner['generation'], timeout=10)
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
