"""Bounded native browser readiness consumed by unattended acceptance checks; no model/tool registration."""
from __future__ import annotations

import json
import secrets
import time


def _synthetic_code(nonce):
    return f'''# Verify the local browser using a temporary synthetic tab
import base64, json
_tid = cdp("Target.createTarget", url="about:blank")["targetId"]
try:
    switch_tab(_tid)
    js('document.body.innerHTML = "<p id=hermes-readiness>{nonce}</p>"')
    assert js('document.getElementById("hermes-readiness").textContent') == "{nonce}"
    _shot = cdp("Page.captureScreenshot", format="png", clip={{"x":0,"y":0,"width":64,"height":64,"scale":1}})
    _raw = base64.b64decode(_shot["data"], validate=True)
    assert _raw.startswith(b"\\x89PNG\\r\\n\\x1a\\n") and 24 <= len(_raw) <= 1048576
    assert int.from_bytes(_raw[16:20], "big") == 64 and int.from_bytes(_raw[20:24], "big") == 64
    print("HERMES_BROWSER_READY:{nonce}")
finally:
    assert cdp("Target.closeTarget", targetId=_tid).get("success") is True
'''


def _run_probe(task_id, nonce, timeout):
    from tools.browser_use_cli import browser_exec
    result = browser_exec(_synthetic_code(nonce), task_id=task_id, timeout_s=max(5, int(timeout)))
    value = json.loads(result) if isinstance(result, str) else result
    if not value.get('success') or f'HERMES_BROWSER_READY:{nonce}' not in value.get('output', ''):
        raise RuntimeError('native synthetic CDP/DOM/screenshot operation failed')


def _supported_route():
    from agent.secret_scope import get_secret
    from tools.browser_session_cdp import get_cdp_override_raw
    from tools.browser_session_cloud import get_cloud_provider, use_real_profile
    from tools.browser_lightpanda import using_lightpanda_engine
    return not (get_cdp_override_raw() or get_cloud_provider() is not None or use_real_profile()
                or using_lightpanda_engine() or get_secret('BU_CDP_WS') or get_secret('BU_CDP_URL'))


def check_browser_readiness(*, task_id: str, timeout: float = 45.0) -> dict:
    """Use the configured production sandbox/profile; return healthy, deferred or failed.

    The caller binds its production profile and terminal task context normally. This function does not
    drain work, replace a sandbox, contact a portal, or close a browser present before the reservation.
    The actual-profile flock stays held from admission through final close; takeover/attachment refuses
    while held. Whole-browser cleanup is allowed only for the probe's sole persisted holder.
    """
    from tools.bot_desktop import lease, placement, runtime, sandbox_host
    from tools.browser_sandbox_lifecycle import BrowserBusy, profile_guard, live_holders
    from tools import browser_session_sandbox as sandbox
    from tools.browser_session import get_session_info
    from tools.browser_session_lifecycle import cleanup_single_session
    from tools.browser_use_cli import _backend_cache_key
    started = time.monotonic()
    result = {'status': 'failed', 'reason': 'browser_unavailable', 'checks': {'cdp': False, 'dom': False,
              'screenshot': False}, 'cleanup': False, 'placement': 'sandbox', 'container_id': None,
              'persistent_profile': True}
    if not task_id or not 10 <= timeout <= 120:
        result['reason'] = 'invalid_probe_budget_or_task'
        return result
    if placement.resolve().where != placement.TERMINAL:
        result.update(status='deferred', reason='unsupported_browser_placement')
        return result
    if not _supported_route():
        result.update(status='deferred', reason='unsupported_browser_route')
        return result
    env = runtime._sandbox_env(create=True)
    if env is None:
        return result
    result['container_id'] = getattr(env, '_container_id', None)
    profile = sandbox_host.browser_profile_dir(env)
    native_task = 'readiness-' + secrets.token_hex(8)
    key = _backend_cache_key(native_task)
    nonce = secrets.token_hex(16)
    info = None
    try:
        with profile_guard(env, profile) as guard:
            guard.deadline = started + timeout - 10
            if lease.human_holds() or live_holders(guard):
                raise BrowserBusy('browser has existing human/session ownership')
            existing = guard.command('status')['endpoint'] is not None
            try:
                info = get_session_info(key)
                if not (info.get('features') or {}).get('sandbox') or info.get('sandbox_env') is not env:
                    raise RuntimeError('native browser resolved a different placement or sandbox')
                _run_probe(native_task, nonce, timeout - (time.monotonic() - started) - 10)
                result['checks'] = dict.fromkeys(result['checks'], True)
            finally:
                if info is not None:
                    info['sandbox_preserve'] = existing
                    cleanup_single_session(key)
                    after = guard.command('status')
                    if live_holders(guard):
                        raise RuntimeError('probe holder cleanup incomplete')
                    if not existing and (after['endpoint'] or after['processes'] or after['links']):
                        raise RuntimeError('check-owned browser did not exit cleanly')
                    result['cleanup'] = True
            result.update(status='healthy', reason='ready')
    except BrowserBusy:
        result.update(status='deferred', reason='browser_busy')
    except (OSError, RuntimeError, ValueError, TimeoutError):
        result.update(status='failed', reason='browser_probe_or_cleanup_failed')
    return result
