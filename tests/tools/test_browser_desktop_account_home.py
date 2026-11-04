"""A Docker exec user can inherit root's HOME; persistent browser identity follows its passwd home."""
import subprocess


def test_desktop_profile_uses_execution_account_home(monkeypatch):
    from tools.bot_desktop import sandbox_host
    from tools.environments import streams
    env = type('Sandbox', (), {})()
    monkeypatch.setattr(sandbox_host, '_user_for', lambda env: 'pn')
    def run(env, argv, **kwargs):
        value = b'pn:x:1000:1000::/home/pn:/bin/bash\n' if argv[:2] == ['getent','passwd'] else b'/root'
        return subprocess.CompletedProcess(argv, 0, stdout=value, stderr=b'')
    monkeypatch.setattr(streams, 'run_in', run)
    assert sandbox_host.browser_profile_dir(env) == '/home/pn/.hermes/bot-desktop/browser-profile'
