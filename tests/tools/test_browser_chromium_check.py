"""Tests for Chromium-presence detection in browser_tool.

Regression guard for the "browser tool advertised but Chromium missing"
class of bug — where ``agent-browser`` CLI is discoverable but no
Chromium build is on disk, causing every browser_* tool call to hang
for the full command timeout before surfacing a useless error.
"""

import os
import shutil

import pytest

from tools import browser_tool as bt
from tools import browser_tool_install as bt_install
from tools import browser_tool_cloud as bt_cloud


@pytest.fixture(autouse=True)
def _reset_chromium_cache():
    bt._cached_chromium_installed = None
    yield
    bt._cached_chromium_installed = None


class TestChromiumSearchRoots:
    def test_respects_playwright_browsers_path_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
        roots = bt_install._chromium_search_roots()
        assert str(tmp_path) == roots[0]


    def test_always_includes_default_ms_playwright_cache(self, monkeypatch):
        monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
        roots = bt_install._chromium_search_roots()
        home = os.path.expanduser("~")
        assert any(r == os.path.join(home, ".cache", "ms-playwright") for r in roots)


class TestChromiumInstalled:
    def test_true_when_plain_chromium_on_path(self, monkeypatch):
        monkeypatch.delenv("AGENT_BROWSER_EXECUTABLE_PATH", raising=False)
        monkeypatch.setattr(
            shutil,
            "which",
            lambda name, path=None: "/usr/bin/chromium" if name == "chromium" else None,
        )

        assert bt_install._chromium_installed() is True


    def test_result_cached(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
        chromium = tmp_path / "chromium-1208" / "chrome-linux64" / "chrome"
        chromium.parent.mkdir(parents=True)
        chromium.write_text("browser")
        chromium.chmod(0o755)
        assert bt_install._chromium_installed() is True
        # Delete after first call — cached True should still return True.
        chromium.unlink()
        assert bt_install._chromium_installed() is True

    def test_partial_playwright_directory_is_not_browser_ready(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "browsers"))
        (tmp_path / "browsers" / "chromium-1208").mkdir(parents=True)
        monkeypatch.setattr(bt_install.shutil, "which", lambda *_args, **_kwargs: None)

        assert bt_install._chromium_installed() is False

    def test_shared_chrome_is_pinned_for_profile_subprocess_home(self, monkeypatch, tmp_path):
        shared_home = tmp_path / "shared"
        profile_home = tmp_path / "profile" / "home"
        chrome = shared_home / ".agent-browser" / "browsers" / "chrome-123" / "chrome"
        chrome.parent.mkdir(parents=True)
        chrome.write_text("browser")
        chrome.chmod(0o755)
        profile_home.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(shared_home))
        monkeypatch.delenv("AGENT_BROWSER_EXECUTABLE_PATH", raising=False)
        monkeypatch.setattr(bt_install.shutil, "which", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            "tools.environments.local.hermes_subprocess_env",
            lambda **_kwargs: {"HOME": str(profile_home)},
        )

        env = bt._build_browser_env()

        assert env["HOME"] == str(profile_home)
        assert env["AGENT_BROWSER_EXECUTABLE_PATH"] == str(chrome)


class TestCheckBrowserRequirementsChromium:

    def test_local_mode_with_chromium_returns_true(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bt, "_is_camofox_mode", lambda: False)
        monkeypatch.setattr(bt_install, "_find_agent_browser", lambda **_kw: "/usr/local/bin/agent-browser")
        monkeypatch.setattr("tools.browser_tool_install._requires_real_termux_browser_install", lambda _: False)
        monkeypatch.setattr(bt_cloud, "_get_cloud_provider", lambda: None)
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
        chromium = tmp_path / "chromium-1208" / "chrome-linux64" / "chrome"
        chromium.parent.mkdir(parents=True)
        chromium.write_text("browser")
        chromium.chmod(0o755)

        assert bt_install.check_browser_requirements() is True


    def test_camofox_mode_does_not_require_chromium(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bt, "_is_camofox_mode", lambda: True)
        # Even with no chromium on disk, camofox drives its own backend.
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
        monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path / "fakehome"))

        assert bt_install.check_browser_requirements() is True


class TestRunBrowserCommandChromiumGuard:
    """Verify _run_browser_command fails fast (no timeout hang) when
    Chromium is missing in local mode.
    """

