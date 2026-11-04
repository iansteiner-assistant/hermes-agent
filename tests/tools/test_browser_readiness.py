"""Readiness owns only its synthetic target and fences takeover until cleanup completes."""
import json
import re
import base64
import io

import pytest

pytest_plugins = ["tests.tools.test_browser_sandbox_lifecycle"]

pytestmark = pytest.mark.platforms('linux')


def _marker_png(blank=False):
    from PIL import Image
    image = Image.new('RGB', (64,64), (17,83,149))
    if not blank:
        image.paste((211,47,97), (32,0,64,64))
    output = io.BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()


@pytest.fixture
def readiness(sandbox, monkeypatch):
    from tools import browser_readiness, browser_session, browser_use_cli
    from tools.bot_desktop import placement
    browser, env, profile, _exe = sandbox
    monkeypatch.setattr(placement, 'resolve', lambda: placement.Placement(placement.TERMINAL, 'docker'))
    monkeypatch.setattr(browser_readiness, '_supported_route', lambda: True)
    monkeypatch.setattr(browser_session, '_active_sessions', {})
    def create(key):
        info = browser.create_sandbox_session(key)
        browser_session._active_sessions[key] = info
        return info
    monkeypatch.setattr(browser_session, 'get_session_info', create)
    calls = []
    payload = {'png': _marker_png()}
    def execute(code, **kwargs):
        nonce = re.search(r'HERMES_BROWSER_READY:([a-f0-9]+)', code).group(1)
        def cdp(method, **params):
            calls.append((method, params))
            if method == 'Target.createTarget':
                assert params == {'url':'about:blank'}
                return {'targetId':'check-target'}
            if method == 'Page.captureScreenshot':
                return {'data':base64.b64encode(payload['png']).decode()}
            if method == 'Target.closeTarget':
                assert params == {'targetId':'check-target'}
                return {'success':True}
            raise AssertionError(method)
        try:
            exec(code, {'cdp':cdp, 'switch_tab':lambda target: None, 'js':lambda expression: nonce})
        except (AssertionError, OSError, ValueError, SyntaxError):
            return json.dumps({'success':False,'output':''})
        return json.dumps({'success':True,'output':f'HERMES_BROWSER_READY:{nonce}'})
    monkeypatch.setattr(browser_use_cli, 'browser_exec', execute)
    return browser_readiness, browser, env, profile, calls, payload


def test_native_readiness_checks_synthetic_target_and_confirms_own_exit(readiness):
    check, browser, env, profile, calls, _payload = readiness
    result = check.check_browser_readiness(task_id='production', timeout=30)
    assert result['status'] == 'healthy'
    assert result['cleanup'] and all(result['checks'].values())
    assert result['container_id'] == env._container_id
    assert [name for name, _ in calls] == ['Target.createTarget','Page.captureScreenshot','Target.closeTarget']
    assert not (profile/'SingletonLock').is_symlink()
    assert not browser._launched


def test_native_readiness_preserves_a_preexisting_browser(readiness):
    check, browser, env, profile, _, _payload = readiness
    from tools.browser_sandbox_lifecycle import profile_guard, remove_holder
    original = browser.create_sandbox_session('original')
    with profile_guard(env, str(profile)) as guard:
        remove_holder(guard, original['sandbox_holder'])
    result = check.check_browser_readiness(task_id='production', timeout=30)
    assert result['status'] == 'healthy' and result['cleanup']
    assert (profile/'SingletonLock').is_symlink()
    assert original['launcher_key'] in browser._launched
    browser._stop_launched(original['launcher_key'])


def test_readiness_defers_when_an_existing_session_owns_the_profile(readiness):
    check, browser, _, _, calls, _payload = readiness
    original = browser.create_sandbox_session('existing')
    result = check.check_browser_readiness(task_id='production', timeout=30)
    assert result['status'] == 'deferred' and result['reason'] == 'browser_busy'
    assert not calls
    browser.stop_sandbox_browser(original)


def test_unsupported_provider_route_defers_before_creating_any_browser(readiness, monkeypatch):
    check, _, _, _, calls, _payload = readiness
    from tools.bot_desktop import runtime
    monkeypatch.setattr(check, '_supported_route', lambda: False)
    monkeypatch.setattr(runtime, '_sandbox_env', lambda **kwargs: pytest.fail('unsupported route created sandbox'))
    result = check.check_browser_readiness(task_id='production', timeout=30)
    assert result['status'] == 'deferred' and result['reason'] == 'unsupported_browser_route'
    assert not calls


@pytest.mark.parametrize('bad_image', ['header-only','blank','wrong-marker','truncated'])
def test_invalid_or_unrendered_screenshot_fails_and_still_cleans_target(readiness, bad_image):
    check, browser, _, profile, calls, payload = readiness
    if bad_image == 'header-only':
        payload['png'] = b'\x89PNG\r\n\x1a\n' + b'\0'*8 + (64).to_bytes(4,'big')*2
    elif bad_image == 'blank':
        payload['png'] = _marker_png(blank=True)
    elif bad_image == 'wrong-marker':
        from PIL import Image
        image = Image.new('RGB',(64,64),'white')
        output = io.BytesIO()
        image.save(output,format='PNG')
        payload['png'] = output.getvalue()
    else:
        payload['png'] = _marker_png()[:-16]
    result = check.check_browser_readiness(task_id='production', timeout=30)
    assert result['status'] == 'failed' and not result['checks']['screenshot']
    assert result['cleanup']
    assert calls[-1] == ('Target.closeTarget', {'targetId':'check-target'})
    assert not (profile/'SingletonLock').is_symlink() and not browser._launched
