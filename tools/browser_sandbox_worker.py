"""Private stdlib helper copied into a sandbox: profile flock, Chromium supervision and clean stop.

Not an agent tool or a service. The guard owns the actual shared profile lock until its caller closes
stdin; a crashed/disconnected caller releases it. Supervisor status belongs to the actual Chromium child.
"""
from __future__ import annotations

import fcntl  # windows-footgun: ok — copied helper executes only inside a Linux sandbox
import json
import http.client
import os
from pathlib import Path
import selectors
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid

LINKS = ("SingletonLock", "SingletonSocket", "SingletonCookie")
TAIL_BYTES = 16384


def start_time(pid):
    try:
        data = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8-sig")
        fields = data[data.rfind(")") + 2:].split()
        return None if fields[0] == "Z" else fields[19]
    except (OSError, ValueError, IndexError):
        return None


def namespace():
    return f"{os.stat('/proc/self/ns/pid').st_ino}:{Path('/proc/sys/kernel/random/boot_id').read_text(encoding='utf-8-sig').strip()}"


def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid():  # windows-footgun: ok — Linux sandbox helper
        raise RuntimeError("browser lifecycle state is not a private owned directory")
    path.chmod(0o700)


def read_json(path):
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("invalid browser lifecycle record")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path, value):
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(value, out)
    os.replace(tmp, path)


def links(profile):
    result = {}
    for name in LINKS:
        path = profile / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISLNK(info.st_mode):
            raise RuntimeError("singleton entry is not a symlink")
        result[name] = {"target": os.readlink(path), "inode": info.st_ino, "device": info.st_dev,
                        "uid": info.st_uid, "gid": info.st_gid, "mtime_ns": info.st_mtime_ns,
                        "ctime_ns": info.st_ctime_ns, "mode": info.st_mode}
    return result


def endpoint(profile):
    try:
        parts = (profile / "DevToolsActivePort").read_text(encoding="utf-8-sig").splitlines()
        port, target = int(parts[0]), parts[1]
        if not 0 < port < 65536 or not target.startswith("/devtools/browser/"):
            return None
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=0.5)
        try:
            connection.request('GET', '/json/version')
            response = connection.getresponse()
            payload = json.loads(response.read(16384))
            if response.status == 200 and payload.get('webSocketDebuggerUrl', '').endswith(target):
                return [port, target]
        finally:
            connection.close()
        return None
    except (OSError, ValueError, IndexError, http.client.HTTPException):
        return None


def profile_processes(profile):
    token = f"--user-data-dir={profile}".encode()
    found = []
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            argv = (path / 'cmdline').read_bytes().split(b'\0')
        except FileNotFoundError:
            continue
        except PermissionError as exc:
            # Other users are irrelevant only when /proc explicitly reports their uid.
            if path.stat().st_uid != os.getuid():  # windows-footgun: ok — Linux sandbox helper
                continue
            raise RuntimeError("browser process inspection incomplete") from exc
        naming = token in argv or any(a == b'--user-data-dir' and i + 1 < len(argv)
                                      and argv[i + 1] == str(profile).encode() for i, a in enumerate(argv))
        if naming and not any(a.startswith(b'--type=') for a in argv):
            found.append(int(path.name))
    return found


def ours(record, profile):
    pid = record.get("pid")
    if record.get("namespace") != namespace() or not record.get("start"):
        return False
    if start_time(pid) != record['start']:
        return False
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b'\0')
    except OSError:
        return False
    return f"--user-data-dir={profile}".encode() in argv


def supervise(profile, request):
    state = profile / '.hermes-lifecycle'
    status_path = state / (request['generation'] + '.json')
    child_env = {**os.environ, **request.get('child_env', {})}
    child = subprocess.Popen(request['argv'], env=child_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, start_new_session=True, close_fds=True)  # detached-spawn: ok — recorded shared browser outlives a gateway, reaped by its supervisor
    record = {"generation": request['generation'], "pid": child.pid, "start": start_time(child.pid),
              "namespace": namespace(), "hostname": socket.gethostname(), "container": request.get('container'),
              "profile": str(profile), "profile_stat": [profile.stat().st_dev, profile.stat().st_ino],
              "exit_code": None, "stderr": "", "singletons": {}}
    write_json(status_path, record)
    tail = b''
    selector = selectors.DefaultSelector()
    selector.register(child.stderr, selectors.EVENT_READ)
    while child.poll() is None or selector.select(0):
        for key, _ in selector.select(0.2):
            chunk = os.read(key.fd, 4096)
            if chunk:
                tail = (tail + chunk)[-TAIL_BYTES:]
            else:
                selector.unregister(key.fileobj)
        if not record['singletons'] and endpoint(profile):
            record['singletons'] = links(profile)
            write_json(status_path, record)
    record.update(exit_code=child.wait(timeout=1), stderr=tail.decode('utf-8', 'replace'))
    write_json(status_path, record)
    selector.close()
    child.stderr.close()


def launch(profile, request):
    state = profile / '.hermes-lifecycle'
    if endpoint(profile) or profile_processes(profile):
        raise RuntimeError('browser already running; refusing blind launch')
    records = sorted((p for p in state.glob('*.json') if len(p.stem) == 32
                      and all(c in '0123456789abcdef' for c in p.stem)), key=lambda p: p.stat().st_mtime_ns)
    for previous in records[:-3]:
        previous.unlink()
    generation = uuid.uuid4().hex
    request = {**request, 'generation': generation}
    write_json(state / 'owner.json', {'generation': generation})
    (profile / 'DevToolsActivePort').unlink(missing_ok=True)
    # Detached supervisor retains the real child status after the launch exec has returned.
    subprocess.Popen([sys.executable, __file__, 'supervise', str(profile), json.dumps(request)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)  # detached-spawn: ok — supervisor retains Chromium exit status after launch exec returns
    return {'generation': generation}


def status(profile):
    state = profile / '.hermes-lifecycle'
    owner = read_json(state / 'owner.json')
    generation = owner.get('generation', '')
    if generation and (len(generation) != 32 or any(c not in '0123456789abcdef' for c in generation)):
        raise RuntimeError('invalid browser generation')
    record = read_json(state / (generation + '.json')) if generation else {}
    return {'owner': record, 'endpoint': endpoint(profile), 'links': links(profile),
            'processes': profile_processes(profile), 'holders': read_json(state / 'holders.json'),
            'namespace': namespace(), 'retired': read_json(state / 'retired.json')}


def stop(profile, request):
    record = status(profile)['owner']
    if not record:
        if profile_processes(profile) or links(profile):
            raise RuntimeError('unknown browser owner; refusing stop')
        return {'stopped': True}
    if record.get('generation') != request.get('generation'):
        raise RuntimeError('browser ownership changed; refusing stop')
    pid = record['pid']
    if start_time(pid) is not None:
        if not ours(record, profile):
            raise RuntimeError('browser identity cannot be proved; refusing signal')
        # pidfd binds the signal to this process even if it exits/recycles after verification.
        fd = os.pidfd_open(pid)
        try:
            if not ours(record, profile):
                raise RuntimeError('browser identity changed before signal')
            signal.pidfd_send_signal(fd, signal.SIGTERM)
        finally:
            os.close(fd)
    deadline = time.monotonic() + float(request.get('timeout', 10))
    while time.monotonic() < deadline:
        if start_time(pid) is None and not profile_processes(profile) and not links(profile):
            return {'stopped': True}
        time.sleep(0.05)
    raise RuntimeError('browser did not exit cleanly and release singleton links; teardown refused')


def quarantine(profile, request):
    current = status(profile)
    record = request.get('orphan', {})
    saved = read_json(profile / '.hermes-lifecycle' / (record.get('generation', '') + '.json')) if record else {}
    proof = request.get('proof', {})
    expected = request.get('links')
    if (not record or saved != record or current['owner'].get('exit_code') != 21
            or record.get('container') != proof.get('container') or not proof.get('absent')
            or not record.get('singletons') or current['links'] != expected
            or current['links'] != record['singletons'] or current['endpoint'] or current['processes']
            or current['holders']):
        raise RuntimeError('orphan ownership proof changed or incomplete; no repair')
    if set(expected) != set(LINKS):
        raise RuntimeError('incomplete singleton tuple; no repair')
    target = profile / '.hermes-lifecycle' / ('quarantine-' + uuid.uuid4().hex)
    private_dir(target)
    write_json(target / 'receipt.json', {'owner': record, 'singletons': expected, 'proof': proof})
    for name in LINKS:
        if links(profile).get(name) != expected[name]:
            raise RuntimeError('singleton changed during quarantine; repair interrupted')
        os.rename(profile / name, target / name)
    return {'quarantine': str(target)}


def dispatch(profile, request):
    op = request['op']
    if op == 'status':
        return status(profile)
    if op == 'launch':
        return launch(profile, request)
    if op == 'stop':
        return stop(profile, request)
    if op == 'quarantine':
        return quarantine(profile, request)
    if op == 'retire':
        write_json(profile / '.hermes-lifecycle' / 'retired.json',
                   {'container': request['container'], 'namespace': namespace()})
        return {}
    if op == 'unretire':
        state = status(profile)
        if state['retired'] != request['retired'] or state['processes'] or state['holders']:
            raise RuntimeError('retired profile ownership changed; refusing admission')
        (profile / '.hermes-lifecycle' / 'retired.json').unlink()
        return {}
    if op == 'holders':
        write_json(profile / '.hermes-lifecycle' / 'holders.json', request['holders'])
        return {}
    raise ValueError('unknown browser lifecycle operation')


def guard(profile):
    private_dir(profile / '.hermes-lifecycle')
    lock = profile / '.hermes-lifecycle' / 'profile.lock'
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'ok': False, 'busy': True}), flush=True)
            return
        print(json.dumps({'ok': True}), flush=True)
        for line in sys.stdin:
            try:
                result = dispatch(profile, json.loads(line))
                print(json.dumps({'ok': True, 'result': result}), flush=True)
            except (OSError, RuntimeError, ValueError, KeyError) as exc:
                print(json.dumps({'ok': False, 'error': str(exc)}), flush=True)
    finally:
        os.close(fd)


def dock(profile, request):
    private_dir(profile / '.hermes-lifecycle')
    fd = os.open(profile / '.hermes-lifecycle' / 'profile.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if read_json(profile / '.hermes-lifecycle' / 'retired.json'):
            raise RuntimeError('profile is retiring; dock launch deferred')
        if endpoint(profile):
            # Chromium's normal singleton forwarding opens a human-requested window in the existing browser.
            subprocess.run(request['argv'], timeout=10, check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            return
        launch(profile, request)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            current = status(profile)
            if current['owner'].get('exit_code') is not None:
                raise RuntimeError('dock Chromium exited during startup; see private lifecycle record')
            if current['endpoint']:
                return
            time.sleep(0.1)
        raise RuntimeError('dock Chromium did not become ready')
    finally:
        os.close(fd)


if __name__ == '__main__':
    mode, directory = sys.argv[1:3]
    profile_path = Path(directory)
    if mode == 'supervise':
        supervise(profile_path, json.loads(sys.argv[3]))
    elif mode == 'guard':
        guard(profile_path)
    elif mode == 'dock':
        dock(profile_path, json.loads(sys.argv[3]))
    else:
        raise ValueError('unknown worker mode')
