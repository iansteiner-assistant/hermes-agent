"""Host-side access to the sandbox's profile lifecycle critical section.

Lock order: container/backing admission, actual-profile flock, home lease lock, short session locks.
A context-local guard is the reservation capability for nested calls. Never pass it to another thread.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import time
import uuid

_ACTIVE = contextvars.ContextVar('sandbox_browser_guard', default=None)
_ADMISSION = contextvars.ContextVar('sandbox_browser_admission', default=None)
_PROFILE_ABSENT_RC = 73


class BrowserBusy(RuntimeError):
    """Another operation owns the shared profile; retry after its bounded operation."""


def _read_line(proc, timeout):
    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    if not ready:
        raise TimeoutError('sandbox browser guard did not answer')
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError('sandbox browser guard disconnected')
    answer = json.loads(line)
    if answer.get('busy'):
        raise BrowserBusy('browser profile is reserved by another operation')
    if not answer.get('ok'):
        raise RuntimeError(answer.get('error', 'sandbox browser guard failed'))
    return answer.get('result', {})


def install_worker(env):
    from tools.bot_desktop import sandbox_host
    from tools.environments import streams
    payload = Path(__file__).with_name('browser_sandbox_worker.py').read_bytes()
    digest = hashlib.sha256(payload).hexdigest()[:20]
    path = f"{streams.sandbox_temp_dir(env)}/hermes-browser-lifecycle-{digest}.py"
    script = ('import os,sys; p=sys.argv[1]; data=sys.stdin.buffer.read(); '
              'f=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); '
              'os.write(f,data); os.close(f)')
    # A content-addressed helper is immutable. Creation races can only install identical bytes.
    probe = streams.run_in(env, ['test', '-f', path], user=sandbox_host._user_for(env), timeout=10)
    if probe.returncode:
        result = streams.run_in(env, ['python3', '-c', script, path], stdin=payload,
                                user=sandbox_host._user_for(env), timeout=10)
        if result.returncode:
            check = streams.run_in(env, ['sha256sum', path], user=sandbox_host._user_for(env), timeout=10)
            if check.returncode or not check.stdout.decode().startswith(hashlib.sha256(payload).hexdigest()):
                raise RuntimeError('sandbox lifecycle helper could not be installed')
    return path


class Guard:
    def __init__(self, env, profile, proc):
        self.env, self.profile, self.proc = env, profile, proc

    def command(self, op, *, timeout=15, **values):
        self.proc.stdin.write((json.dumps({'op': op, **values}) + '\n').encode())
        self.proc.stdin.flush()
        return _read_line(self.proc, timeout + 2)


def current(env, profile):
    value = _ACTIVE.get()
    return value if value is not None and value.env is env and value.profile == profile else None


def _host_records():
    from tools.browser_launcher import _shared_profiles_root
    from tools.browser_launcher import _private_dir
    parent = _private_dir(_shared_profiles_root())
    root = _private_dir(parent / 'sandbox-lifecycle') if parent is not None else None
    if root is None:
        raise RuntimeError('browser lifecycle host state is not private')
    return root


def _container_record(container):
    return _host_records() / ('container-' + hashlib.sha256(container.encode()).hexdigest() + '.json')


def _local_docker_endpoint(docker):
    configured = os.environ.get('DOCKER_HOST')
    if configured:
        endpoint = configured
    else:
        proc = subprocess.run([docker, 'context', 'inspect'], capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=10, check=False)
        if proc.returncode:
            raise RuntimeError('Docker context ownership inspection failed')
        endpoint = json.loads(proc.stdout)[0].get('Endpoints', {}).get('docker', {}).get('Host', '')
    if not endpoint.startswith('unix://'):
        raise RuntimeError('remote Docker lifecycle identity unsupported')
    return endpoint


@contextlib.contextmanager
def _host_admission_lock(key):
    from tools.browser_launcher_holders import records_lock
    directory = _host_records() / ('admission-' + hashlib.sha256(key.encode()).hexdigest())
    directory.mkdir(mode=0o700, exist_ok=True)
    with records_lock(directory, blocking=False) as held:
        if not held:
            raise BrowserBusy('browser creation or container teardown already admitted')
        yield


def _profile_backing_key(env, profile):
    _local_docker_endpoint(env._docker_exe)
    proc = subprocess.run([env._docker_exe, 'inspect', '--format', '{{json .Mounts}}', env._container_id],
                          capture_output=True, timeout=10, check=False)
    if proc.returncode:
        raise RuntimeError('cannot identify browser profile backing; teardown refused')
    mounts = json.loads(proc.stdout)
    shared = [mount for mount in mounts if mount.get('Source', '').startswith('/')
              and (profile == mount['Destination'] or profile.startswith(mount['Destination'].rstrip('/') + '/'))]
    if shared:
        mount = max(shared, key=lambda value: len(value['Destination']))
        return 'backing:' + os.path.realpath(mount['Source'] + profile[len(mount['Destination']):])
    return 'container:' + env._container_id + ':' + profile


@contextlib.contextmanager
def _profile_admission_guard(env, profile):
    """Fence the backing path BEFORE profile creation, including affirmative-absence teardown."""
    if not getattr(env, '_docker_exe', None):
        yield
        return
    identity = (env._docker_exe, env._container_id, profile)
    if _ADMISSION.get() == identity:
        yield
        return
    with _host_admission_lock('container:' + env._container_id):
        with _host_admission_lock(_profile_backing_key(env, profile)):
            token = _ADMISSION.set(identity)
            try:
                yield
            finally:
                _ADMISSION.reset(token)


@contextlib.contextmanager
def _host_profile_guard(env, profile):
    """A local-Docker host flock survives docker stop, unlike the sandbox exec's guard.

    The key is the actual shared filesystem inode, observed in the running sandbox. Retain its mapping
    for a later docker start, which cannot inspect /proc/files before the container is running.
    """
    if not getattr(env, '_docker_exe', None):
        yield
        return
    from tools.bot_desktop import sandbox_host
    from tools.environments import streams
    _local_docker_endpoint(env._docker_exe)
    command = ('import os,sys; p=sys.argv[1]; os.makedirs(p,mode=0o700,exist_ok=True); '
               's=os.stat(p); print(str(s.st_dev)+":"+str(s.st_ino))')
    observed = streams.run_in(env, ['python3', '-c', command, profile],
                              user=sandbox_host._user_for(env), timeout=10)
    identity = observed.stdout.decode('ascii').strip()
    if observed.returncode or len(identity.split(':')) != 2 or not all(x.isdigit() for x in identity.split(':')):
        raise RuntimeError('cannot identify actual browser profile inode')
    root = _host_records()
    record = _container_record(env._container_id)
    from tools.browser_launcher_holders import records_lock
    with records_lock(root, blocking=False) as held:
        if not held:
            raise BrowserBusy('container profile registration busy')
        known = json.loads(record.read_text(encoding='utf-8-sig')) if record.exists() else []
        if identity not in known:
            from utils import atomic_json_write
            atomic_json_write(record, sorted([*known, identity]), mode=0o600)
    directory = root / hashlib.sha256(identity.encode()).hexdigest()
    directory.mkdir(mode=0o700, exist_ok=True)
    with records_lock(directory, blocking=False) as held:
        if not held:
            raise BrowserBusy('actual browser profile reserved across container replacement')
        yield


@contextlib.contextmanager
def registered_container_guard(container):
    """Before restarting a stopped known sandbox, honor the same replacement-spanning profile locks."""
    from tools.browser_launcher_holders import records_lock
    record = _container_record(container)
    identities = json.loads(record.read_text(encoding='utf-8-sig')) if record.exists() else []
    with contextlib.ExitStack() as stack:
        stack.enter_context(_host_admission_lock('container:' + container))
        for identity in sorted(identities):
            directory = _host_records() / hashlib.sha256(identity.encode()).hexdigest()
            directory.mkdir(mode=0o700, exist_ok=True)
            if not stack.enter_context(records_lock(directory, blocking=False)):
                raise BrowserBusy('browser profile replacement in progress; container start deferred')
        yield


@contextlib.contextmanager
def profile_guard(env, profile):
    existing = current(env, profile)
    if existing is not None:
        yield existing
        return
    with _profile_admission_guard(env, profile), _host_profile_guard(env, profile):
        with _sandbox_profile_guard(env, profile) as guard:
            yield guard


@contextlib.contextmanager
def _sandbox_profile_guard(env, profile):
    from tools.bot_desktop import sandbox_host
    from tools.environments import streams
    helper = install_worker(env)
    proc = streams.open_stream(env, ['python3', helper, 'guard', profile], user=sandbox_host._user_for(env))
    token = None
    try:
        _read_line(proc, 10)
        guard = Guard(env, profile, proc)
        _resume_retired_profile(guard)
        token = _ACTIVE.set(guard)
        yield _ACTIVE.get()
    finally:
        if token is not None:
            _ACTIVE.reset(token)
        proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()  # the exec transport, never Chromium or a sandbox process group
            proc.wait(timeout=5)
        proc.stdout.close()


def holder_identity():
    from tools.browser_launcher_holders import ns_identity
    from tools.browser_launcher import _start_time
    return {'pid': os.getpid(), 'start': _start_time(os.getpid()), 'namespace': ns_identity()}


def live_holders(guard):
    from tools.browser_launcher_holders import ns_identity
    from tools.browser_launcher import _pid_alive, _start_time
    holders = guard.command('status')['holders']
    alive = {}
    for key, owner in holders.items():
        if owner.get('kind') == 'human':
            from tools.bot_desktop import lease
            if lease.human_holds(owner.get('home')):
                alive[key] = owner
            continue
        if owner.get('namespace') != ns_identity() or owner.get('start') is None:
            alive[key] = owner  # unknown namespaces are never declared dead
        elif _pid_alive(owner['pid']) and _start_time(owner['pid']) == owner['start']:
            alive[key] = owner
    if holders != alive:
        guard.command('holders', holders=alive)
    return alive


def add_holder(guard):
    holders = live_holders(guard)
    key = uuid.uuid4().hex
    holders[key] = holder_identity()
    guard.command('holders', holders=holders)
    return key


def remove_holder(guard, key):
    holders = live_holders(guard)
    owner = holders.get(key)
    if owner is not None and owner != holder_identity():
        raise RuntimeError('browser session holder changed; refusing release')
    holders.pop(key, None)
    guard.command('holders', holders=holders)
    return holders


@contextlib.contextmanager
def takeover_guard(profile_key=None):
    """Human acquisition joins the same critical section BEFORE taking the home-scoped lease lock."""
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tools.bot_desktop import runtime, sandbox_host, placement
    token = set_hermes_home_override(profile_key) if profile_key else None
    try:
        if placement.resolve().where != placement.TERMINAL:
            yield
            return
        env = runtime._owned_sandbox_env()
        if env is None:
            # A missing sandbox has no browser to take over. Do not create one on this path.
            yield
            return
        with profile_guard(env, sandbox_host.browser_profile_dir(env)) as guard:
            yield guard
    finally:
        if token is not None:
            reset_hermes_home_override(token)


def _host_profile_in_use(source):
    import psutil
    for process in psutil.process_iter(['pid']):
        try:
            argv = process.cmdline()
        except psutil.NoSuchProcess:
            continue
        except (psutil.AccessDenied, OSError) as exc:
            raise RuntimeError('host process inspection incomplete; no repair') from exc
        for index, token in enumerate(argv):
            if token.startswith('--user-data-dir='):
                directory = token.split('=', 1)[1]
            elif token == '--user-data-dir' and index + 1 < len(argv):
                directory = argv[index + 1]
            else:
                continue
            if os.path.realpath(directory) == source:
                return True
    return False


def orphan_proof(env, profile, record):
    """Only an absent recorded container on the local rootless engine with a local bind-backed profile.

    No remote engine, volume/network filesystem, other container sharing that source or failed query
    is sufficient proof. The caller still holds the actual profile lock through quarantine and retry.
    """
    from tools.environments.docker import DockerEnvironment
    container = record.get('container', '')
    if (not isinstance(env, DockerEnvironment) or len(container) != 64
            or any(ch not in '0123456789abcdef' for ch in container)):
        raise RuntimeError('orphan repair requires a complete recorded rootless Docker owner')
    docker = env._docker_exe
    def query(args):
        proc = subprocess.run([docker, *args], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15, check=False)
        if proc.returncode:
            raise RuntimeError('Docker ownership inspection failed; no repair')
        return proc.stdout
    _local_docker_endpoint(docker)
    options = json.loads(query(['info', '--format', '{{json .SecurityOptions}}']))
    if not any('rootless' in item for item in options):
        raise RuntimeError('engine is not verified rootless; no repair')
    ids = query(['ps', '-aq', '--no-trunc']).split()
    if env._container_id not in ids or record['container'] in ids:
        raise RuntimeError('recorded container still exists or current owner unknown; no repair')
    containers = json.loads(query(['inspect', *ids]))
    current_container = next(c for c in containers if c['Id'] == env._container_id)
    mounts = [m for m in current_container.get('Mounts', []) if m['Type'] == 'bind'
              and (profile == m['Destination'] or profile.startswith(m['Destination'].rstrip('/') + '/'))]
    if not mounts:
        raise RuntimeError('profile backing filesystem unknown; no repair')
    mount = max(mounts, key=lambda m: len(m['Destination']))
    source = os.path.realpath(mount['Source'] + profile[len(mount['Destination']):])
    fs = subprocess.run(['findmnt', '-n', '-o', 'FSTYPE', '-T', source], capture_output=True,
                        text=True, encoding='utf-8', errors='replace', timeout=10, check=False)
    if fs.returncode or fs.stdout.strip() not in {'ext4', 'xfs', 'btrfs', 'zfs'}:
        raise RuntimeError('profile is not verified local storage; no repair')
    for container in containers:
        if container['Id'] == env._container_id:
            continue
        for candidate in container.get('Mounts', []):
            backing = os.path.realpath(candidate.get('Source', ''))
            if backing and (source == backing or source.startswith(backing.rstrip('/') + '/')):
                raise RuntimeError('another container can use the profile; no repair')
    if _host_profile_in_use(source):
        raise RuntimeError('host Chromium uses profile; no repair')
    return {'container': record['container'], 'absent': True, 'current': env._container_id,
            'backing': source, 'checked_at': time.time()}



def _resume_retired_profile(guard):
    state = guard.command('status')
    retired = state.get('retired')
    if not retired:
        return
    current = getattr(guard.env, '_container_id', None)
    if current == retired.get('container') and state.get('namespace') != retired.get('namespace'):
        guard.command('unretire', retired=retired)
        return
    docker = getattr(guard.env, '_docker_exe', None)
    if not docker:
        raise BrowserBusy('profile teardown is pending; ownership unknown')
    proc = subprocess.run([docker, 'ps', '-aq', '--no-trunc'], capture_output=True, timeout=10, check=False)
    if proc.returncode or retired.get('container') in proc.stdout.decode().split():
        raise BrowserBusy('retired profile owner still exists; teardown completion unproved')
    guard.command('unretire', retired=retired)


class ContainerTarget:
    """Snapshot the exec identity without copying an environment with a cleanup destructor."""
    def __init__(self, env, container_id=None):
        self._container_id = container_id or env._container_id
        self._docker_exe = env._docker_exe
        self._temp_dir = env.get_temp_dir()
        for name in ("_bd_desktop_user", "_bd_desktop_home"):
            if hasattr(env, name):
                setattr(self, name, getattr(env, name))

    def get_temp_dir(self):
        return self._temp_dir

    def exec_prefix(self, *, user=None, interactive=True):
        return [self._docker_exe, 'exec', *(['-i'] if interactive else []),
                *(['-u', user] if user else []), self._container_id]


def close_owned_browser(guard, generation, timeout=10):
    """Ask Chromium to close over its native CDP endpoint, then prove exit and singleton release.

    A clean SIGTERM exit can leave Chromium singleton links behind. CDP is the normal shutdown path;
    signal only when there is no endpoint or the WebSocket handshake proved no close was sent.
    """
    from tools.bot_desktop import sandbox_host
    from tools.environments import streams
    from tools.browser_cdp_tool import _run_async, _cdp_call
    from websockets.exceptions import InvalidHandshake, WebSocketException
    owned = guard.command('owned_endpoint', generation=generation)
    endpoint = owned['endpoint']
    signal_parent = endpoint is None
    if endpoint is not None:
        local = streams.forward_port(guard.env, endpoint[0], user=sandbox_host._user_for(guard.env))
        url = f'ws://127.0.0.1:{local}{endpoint[1]}'
        try:
            _run_async(_cdp_call(url, 'Browser.close', {}, None, min(timeout, 5)))
        except InvalidHandshake:
            signal_parent = True  # no WebSocket opened, so no CDP close command was sent
        except (WebSocketException, OSError, RuntimeError, TimeoutError):
            # Disconnect after send is a normal close outcome; actual exit/link release decides.
            signal_parent = False
    return guard.command('stop', generation=generation, timeout=timeout, signal=signal_parent)


@contextlib.contextmanager
def container_stop_guard(env, container_id=None):
    """Retain the attachment/takeover fence until the destructive container operation finishes."""
    from tools.bot_desktop import sandbox_host, lease
    from tools.environments import streams
    target = ContainerTarget(env, container_id)
    profile = sandbox_host.browser_profile_dir(target)
    with _profile_admission_guard(target, profile):
        challenge = uuid.uuid4().hex
        script = ('if [ -d "$1" ]; then printf "present:%s" "$2"; exit 0; fi; '
                  'if [ ! -e "$1" ] && [ ! -L "$1" ]; then printf "absent:%s" "$2"; '
                  f'exit {_PROFILE_ABSENT_RC}; fi; exit 74')
        present = streams.run_in(target, ['bash', '-c', script, 'hermes-browser-profile', profile, challenge],
                                 user=sandbox_host._user_for(target), timeout=10)
        if present.returncode == _PROFILE_ABSENT_RC and present.stdout == f'absent:{challenge}'.encode():
            yield
            return
        if present.returncode or present.stdout != f'present:{challenge}'.encode():
            raise RuntimeError('cannot affirm browser profile presence or absence; container stop refused')
        with profile_guard(target, profile) as guard:
            state = guard.command('status')
            if live_holders(guard) or lease.human_holds():
                raise BrowserBusy('browser has active session/human ownership; container teardown deferred')
            if state['owner'] or state['processes'] or state['links']:
                close_owned_browser(guard, state['owner'].get('generation'))
            guard.command('retire', container=target._container_id)
            yield


@contextlib.contextmanager
def desktop_stop_guard(env):
    """Desktop stop keeps its container alive, so no container-retirement marker is needed."""
    from tools.bot_desktop import sandbox_host, lease
    with profile_guard(env, sandbox_host.browser_profile_dir(env)) as guard:
        state = guard.command('status')
        if live_holders(guard) or lease.human_holds():
            raise BrowserBusy('browser is in use; desktop stop deferred')
        if state['owner'] or state['processes'] or state['links']:
            close_owned_browser(guard, state['owner'].get('generation'))
        yield
