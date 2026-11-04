"""The bot's Chromium INSIDE the terminal backend's sandbox (Bot Desktop placed there).

Same browser a human takes over in the pane: the sandbox image's Playwright Chromium on the profile the
dock's Browser icon uses, on the sandbox screen's DISPLAY. Its DevTools port is the sandbox's loopback,
unreachable from this host (Docker bridge / ssh remote), so it is forwarded over the sandbox's exec
stream; Chromium's DevTools accepts any loopback ``Host`` header, port included.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

_LAUNCH_TIMEOUT_S = 60.0
# Sandbox Chromium this process launched, per shared key: ``(sandbox env, pid, start time, user-data-dir)``.
# Every session in that container attaches to it, whichever profile it serves, so it is stopped only when the
# LAST of them tears down (release_session_backend's in-use check on the shared ``launcher_key``); the env is
# kept so a teardown outside the owner's scope hits the right sandbox. The start time (``/proc/<pid>/stat``
# field 22, read inside the sandbox at launch) and the ``--user-data-dir`` token are the identity a stop
# re-checks there before signalling: a crashed Chromium's pid may name another job by then.
_launched: dict[str, tuple[Any, int, Optional[str], str]] = {}
_launched_lock = threading.Lock()
# Per shared key: held across "is a browser running?" + launch + claim, and across the stop, so two sessions
# first using one container never both see no browser and both launch.
_key_locks: dict[str, threading.Lock] = {}
# Endpoints a liveness probe confirmed: ``cdp_url -> (id(env), sandbox port, forward failures then, when)``. A
# browser call re-probes after a connection through its forward failed, the sandbox was replaced, or the
# confirmation is older than _CONFIRMED_TTL_S (a relaunch on the same port that nothing connected to yet
# leaves no failure behind), so a healthy call spends no exec in the sandbox.
_confirmed: dict[str, tuple[int, int, int, float]] = {}
_CONFIRMED_TTL_S = 30.0
# Exit code of the liveness script when the sandbox answers "not running": no DevTools port file, or nothing
# listens on its port. Any other failure (docker/ssh transport error, timeout) says nothing about Chromium.
_GONE_RC = 3
GONE = "gone"


def _sandbox():
    from tools.bot_desktop import runtime as _bd_runtime
    env = _bd_runtime._sandbox_env(create=True)
    if env is None:
        raise RuntimeError("the terminal backend's sandbox is not running, so there is nowhere to run the browser")
    published = _bd_runtime.published_env()
    if not published.get("DISPLAY"):
        # The sandbox died under a live screen: fail here rather than run headless in a dead sandbox or,
        # worse, fall back to the host.
        raise RuntimeError("the screen inside the terminal backend's sandbox is gone; start it again")
    return env, published


def _live_devtools(env, user, udd: str):
    """One exec: ``(port, path)`` of the Chromium on ``udd`` while its DevTools port answers; ``GONE`` when the
    sandbox says it is not running (a crashed Chromium leaves a stale ``DevToolsActivePort`` behind); None
    when the exec itself failed or timed out, which proves nothing either way."""
    from tools.environments import streams
    active = shlex.quote(f"{udd}/DevToolsActivePort")
    script = (f'[ -s {active} ] || exit {_GONE_RC}; p=$(head -n1 {active}); '
              f'( exec 3<>"/dev/tcp/127.0.0.1/$p" ) 2>/dev/null || exit {_GONE_RC}; cat {active}')
    try:
        proc = streams.run_in(env, ["bash", "-c", script], user=user, timeout=10)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        logger.debug("sandbox browser liveness probe failed: %s", exc)
        return None
    if proc.returncode == _GONE_RC:
        return GONE
    lines = proc.stdout.decode("utf-8", "replace").splitlines() if proc.returncode == 0 else []
    if len(lines) >= 2 and lines[0].strip().isdigit() and lines[1].startswith("/devtools/browser/"):
        return int(lines[0].strip()), lines[1].strip()
    logger.debug("sandbox browser liveness probe inconclusive (exit %s)", proc.returncode)
    return None


def _shared_key(env, udd: str) -> str:
    """The launcher key every session on this container's Chromium shares. Keyed by the container, never by
    profile: profiles on one ``terminal.docker_shared_container_key`` share one browser, and each
    ``container_persistent: false`` session has its own. The env object is the container's identity in this
    process (its port forwards keep it alive, so the id is never reused while a session names it)."""
    return f"sandbox:{id(env):x}:{udd}"


def _start_chromium(env, user, exe: str, udd: str, child_env: dict) -> tuple[int, Optional[str]]:
    """Launch detached in the sandbox; returns its sandbox pid and start time (None when unreadable)."""
    from tools.bot_desktop.browser import SHARED_PROFILE_ARGS
    from tools.browser_launcher import CHROMIUM_SANDBOX_BYPASS_ARGS, build_chromium_argv
    # Container: no user namespaces for Chromium's own sandbox, so the bypass flags always apply (this
    # host's facts say nothing about the sandbox); the shared-profile flags match the dock's icon.
    argv = build_chromium_argv(exe, udd, packaged=False, headed=True, sandbox_bypass=False,
                               extra_args=(*SHARED_PROFILE_ARGS, *CHROMIUM_SANDBOX_BYPASS_ARGS))
    argv = list(dict.fromkeys(argv))
    from tools.browser_sandbox_lifecycle import current
    guard = current(env, udd)
    if guard is None:
        raise RuntimeError("Chromium launch requires the shared profile guard")
    before = guard.command('status')
    guard.command('launch', argv=argv, child_env=child_env, container=getattr(env, '_container_id', None))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = guard.command('status')
        if state['owner']:
            guard.launch_before = before
            return state['owner']['pid'], state['owner']['start']
        time.sleep(0.05)
    raise RuntimeError("Chromium supervisor did not publish its child identity")


def _wait_for_port(env, user, udd: str) -> tuple[int, str]:
    from agent.redact import redact_sensitive_text
    from tools.browser_sandbox_lifecycle import current
    guard = current(env, udd)
    deadline = min(time.monotonic() + _LAUNCH_TIMEOUT_S, getattr(guard, 'deadline', float('inf')))
    while time.monotonic() < deadline:
        state = guard.command('status')
        owner = state['owner']
        if owner.get('exit_code') is not None:
            tail = redact_sensitive_text(owner.get('stderr', '')[-2000:], force=True)
            raise RuntimeError(f"Chromium exited with code {owner['exit_code']} during startup.\n{tail}")
        if state['endpoint']:
            return tuple(state['endpoint'])
        time.sleep(0.1)
    raise RuntimeError("Chromium in the sandbox did not expose a DevTools endpoint in time")


def _key_lock(key: str) -> threading.Lock:
    with _launched_lock:
        return _key_locks.setdefault(key, threading.Lock())


def _spawn_owned(env, user, udd: str, key: str, published: dict) -> int:
    """Launch the sandbox Chromium and own it under ``key`` from the moment it exists, before its port is
    up, so stop_sandbox_browser can always stop it; returns its sandbox pid."""
    from tools.bot_desktop import sandbox_host
    from tools.environments import streams
    exe = sandbox_host.chromium_executable(env)
    if not exe:
        raise RuntimeError("the sandbox image has no Chromium to run the browser")
    child_env = {**published, "TMPDIR": streams.sandbox_temp_dir(env)}
    pid, start = _start_chromium(env, user, exe, udd, child_env)
    with _launched_lock:
        _launched[key] = (env, pid, start, udd)
    return pid


def create_sandbox_session(cache_key: str) -> dict[str, Any]:
    """Session on the sandbox Chromium (attach to the running one, else launch it)."""
    from tools.bot_desktop import sandbox_host
    from tools.browser_session import _session_record
    from tools.environments import streams
    env, published = _sandbox()
    user = sandbox_host._user_for(env)
    udd = sandbox_host.browser_profile_dir(env)
    key = _shared_key(env, udd)
    from tools.browser_sandbox_lifecycle import profile_guard, add_holder
    with profile_guard(env, udd) as guard, _key_lock(key):
        state = guard.command('status')
        found = tuple(state['endpoint']) if state['endpoint'] else GONE
        if found is None:
            # Launching blind would wipe a live browser's DevToolsActivePort and lose to its singleton.
            raise RuntimeError("the terminal backend's sandbox did not answer whether its browser is running; "
                               "try again")
        spawned: Optional[int] = None
        try:
            if found is GONE:
                spawned = _spawn_owned(env, user, udd, key, published)
                found = _wait_with_repair(env, user, udd, key, published)
            local_port = streams.forward_port(env, found[0], user=user)
            holder = add_holder(guard)
        except BaseException:
            if spawned is not None:  # a timeout, a failed forward or an interrupt must not leave it running unowned
                try:
                    state = guard.command('status')
                    if state['owner'].get('exit_code') is not None and not state['processes']:
                        with _launched_lock:
                            _launched.pop(key, None)
                    else:
                        _stop_launched(key)
                except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                    logger.warning("Failed-launch browser cleanup incomplete: %s", exc)
            raise
    info = _session_record("sb", f"ws://127.0.0.1:{local_port}{found[1]}", {"local": True, "sandbox": True})
    _confirm(info["cdp_url"], env, found[0])
    info.update(shared_profile=True, launcher_key=key, sandbox_holder=holder, sandbox_env=env,
                sandbox_profile=udd)
    logger.info("Created sandbox browser session %s for %s", info["session_name"], cache_key)
    return info


def _confirm(cdp_url: str, env, port: int) -> None:
    from tools.environments import streams
    _confirmed[cdp_url] = (id(env), port, streams.forward_failures(env, port), time.monotonic())


def sandbox_browser_alive(session_info: dict[str, Any]) -> bool:
    """The sandbox Chromium still serves this session's endpoint through the current port forward: False once
    the sandbox answers that it died, was relaunched (new DevTools path) or the sandbox was replaced (new
    forward). Re-probed only after a connection through the forward failed or the last confirmation expired;
    a probe that cannot reach the sandbox keeps the session."""
    from tools.bot_desktop import runtime as _bd_runtime, sandbox_host
    from tools.environments import streams
    env = _bd_runtime._sandbox_env(create=False)
    if env is None:
        return False
    cdp_url = session_info.get("cdp_url") or ""
    confirmed = _confirmed.get(cdp_url)
    if (confirmed and confirmed[0] == id(env) and streams.forward_failures(env, confirmed[1]) == confirmed[2]
            and time.monotonic() - confirmed[3] < _CONFIRMED_TTL_S):
        return True
    try:
        user = sandbox_host._user_for(env)
        udd = sandbox_host.browser_profile_dir(env)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        logger.debug("sandbox browser liveness probe failed: %s", exc)
        return True
    found = _live_devtools(env, user, udd)
    if found is None:
        return True
    if found is GONE or cdp_url != f"ws://127.0.0.1:{streams.forward_port(env, found[0], user=user)}{found[1]}":
        _confirmed.pop(cdp_url, None)
        return False
    _confirm(cdp_url, env, found[0])
    return True


def stop_sandbox_browser(session_info: dict[str, Any]) -> None:
    """Release this persisted holder; last locally launched owner closes under the profile guard."""
    from tools.browser_sandbox_lifecycle import profile_guard, remove_holder
    _confirmed.pop(session_info.get("cdp_url") or "", None)
    key = session_info.get("launcher_key") or ""
    env = session_info.get('sandbox_env')
    profile = session_info.get('sandbox_profile')
    if env is None or not profile:
        raise RuntimeError('sandbox session has no ownership identity')
    with profile_guard(env, profile) as guard, _key_lock(key):
        if remove_holder(guard, session_info.get('sandbox_holder')) or session_info.get('sandbox_preserve'):
            return
        _stop_launched(key)


def _stop_launched(key: str) -> None:
    from tools.browser_sandbox_lifecycle import profile_guard, close_owned_browser
    from tools.bot_desktop import lease
    with _launched_lock:
        launched = _launched.get(key)
    if launched is None:
        return
    env, pid, start, udd = launched
    with profile_guard(env, udd) as guard:
        if lease.human_holds():
            raise RuntimeError('human owns the browser; leaving it running')
        state = guard.command('status')
        record = state['owner']
        if record.get('pid') != pid or record.get('start') != start:
            raise RuntimeError('browser ownership changed; refusing stop')
        close_owned_browser(guard, record.get('generation'))
        with _launched_lock:
            _launched.pop(key, None)


def _wait_with_repair(env, user, udd, key, published):
    from tools.browser_sandbox_lifecycle import current, orphan_proof, live_holders
    guard = current(env, udd)
    try:
        return _wait_for_port(env, user, udd)
    except RuntimeError:
        failed = guard.command('status')['owner']
        before = getattr(guard, 'launch_before', {})
        old = before.get('owner', {})
        if (failed.get('exit_code') != 21 or not old.get('singletons')
                or 'profile' not in failed.get('stderr', '').lower() or live_holders(guard)):
            raise
        proof = orphan_proof(env, udd, old)
        guard.command('quarantine', proof=proof, links=before['links'], orphan=old)
        _spawn_owned(env, user, udd, key, published)
        return _wait_for_port(env, user, udd)  # exactly one retry
