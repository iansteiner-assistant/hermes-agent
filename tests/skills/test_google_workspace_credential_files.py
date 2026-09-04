"""Regression test: google-workspace SKILL.md must declare required_credential_files.

PR #9931 accidentally removed the required_credential_files header, which broke
credential file mounting in Docker/Modal remote backends (#16452). This test
prevents the regression from silently reappearing.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch


SKILL_MD = (
    Path(__file__).resolve().parents[2]
    / "skills/productivity/google-workspace/SKILL.md"
)

_EXPECTED_PATHS = {"google_token.json", "google_client_secret.json"}


def _parse_frontmatter(content: str) -> dict:
    from agent.skill_utils import parse_frontmatter

    fm, _ = parse_frontmatter(content)
    return fm


class TestGoogleWorkspaceCredentialFiles:
    def test_required_credential_files_present_in_skill_md(self):
        content = SKILL_MD.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        entries = fm.get("required_credential_files")
        assert entries, "required_credential_files missing from google-workspace SKILL.md"
        assert isinstance(entries, list), "required_credential_files must be a list"
        paths = {
            (e["path"] if isinstance(e, dict) else e)
            for e in entries
        }
        assert _EXPECTED_PATHS <= paths, (
            f"Missing entries in required_credential_files: {_EXPECTED_PATHS - paths}"
        )

    def test_entries_are_registered_when_files_exist(self, tmp_path):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "google_token.json").write_text("{}")
        (hermes_home / "google_client_secret.json").write_text("{}")

        from tools.credential_files import (
            clear_credential_files,
            get_credential_file_mounts,
            register_credential_files,
        )

        clear_credential_files()
        try:
            content = SKILL_MD.read_text(encoding="utf-8")
            fm = _parse_frontmatter(content)
            entries = fm.get("required_credential_files", [])

            with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
                missing = register_credential_files(entries)

            assert missing == [], f"Unexpected missing files: {missing}"
            mounts = get_credential_file_mounts()
            container_paths = {m["container_path"] for m in mounts}
            assert "/root/.hermes/google_token.json" in container_paths
            assert "/root/.hermes/google_client_secret.json" in container_paths
        finally:
            clear_credential_files()

    def test_shared_google_credentials_home_from_profile_env_is_registered(self, tmp_path):
        profile_home = tmp_path / ".hermes" / "profiles" / "agent-a"
        shared_home = tmp_path / ".hermes" / "google-shared"
        profile_home.mkdir(parents=True)
        shared_home.mkdir(parents=True)
        (profile_home / ".env").write_text(
            f"HERMES_GOOGLE_CREDENTIALS_HOME={shared_home}\n",
            encoding="utf-8",
        )
        (shared_home / "google_token.json").write_text("{}")
        (shared_home / "google_client_secret.json").write_text("{}")

        from tools.credential_files import (
            clear_credential_files,
            get_credential_file_mounts,
            register_credential_files,
        )

        clear_credential_files()
        try:
            content = SKILL_MD.read_text(encoding="utf-8")
            fm = _parse_frontmatter(content)
            entries = fm.get("required_credential_files", [])

            with patch.dict(os.environ, {"HERMES_HOME": str(profile_home)}, clear=False):
                os.environ.pop("HERMES_GOOGLE_CREDENTIALS_HOME", None)
                missing = register_credential_files(entries)

            assert missing == []
            mounts = get_credential_file_mounts()
            by_container = {m["container_path"]: m["host_path"] for m in mounts}
            assert by_container["/root/.hermes/google_token.json"] == str(
                shared_home / "google_token.json"
            )
            assert by_container["/root/.hermes/google_client_secret.json"] == str(
                shared_home / "google_client_secret.json"
            )
        finally:
            clear_credential_files()

    def test_config_credential_files_use_shared_google_home_from_profile_env(self, tmp_path, monkeypatch):
        profile_home = tmp_path / ".hermes" / "profiles" / "agent-a"
        shared_home = tmp_path / ".hermes" / "google-shared"
        profile_home.mkdir(parents=True)
        shared_home.mkdir(parents=True)
        (profile_home / ".env").write_text(
            f"HERMES_GOOGLE_CREDENTIALS_HOME={shared_home}\n",
            encoding="utf-8",
        )
        (shared_home / "google_token.json").write_text("{}")
        (shared_home / "google_client_secret.json").write_text("{}")

        import hermes_cli.config as hermes_config
        import tools.credential_files as credential_files

        monkeypatch.setattr(
            hermes_config,
            "read_raw_config",
            lambda: {
                "terminal": {
                    "credential_files": [
                        "google_token.json",
                        "google_client_secret.json",
                    ]
                }
            },
        )
        monkeypatch.setattr(credential_files, "_config_files", None)

        credential_files.clear_credential_files()
        try:
            with patch.dict(os.environ, {"HERMES_HOME": str(profile_home)}, clear=False):
                os.environ.pop("HERMES_GOOGLE_CREDENTIALS_HOME", None)
                mounts = credential_files.get_credential_file_mounts()

            by_container = {m["container_path"]: m["host_path"] for m in mounts}
            assert by_container["/root/.hermes/google_token.json"] == str(
                shared_home / "google_token.json"
            )
            assert by_container["/root/.hermes/google_client_secret.json"] == str(
                shared_home / "google_client_secret.json"
            )
        finally:
            credential_files.clear_credential_files()
            credential_files._config_files = None

    def test_missing_token_is_reported(self, tmp_path):
        """google_token.json absent (first-time setup) — reported as missing, client secret still mounts."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "google_client_secret.json").write_text("{}")

        from tools.credential_files import (
            clear_credential_files,
            get_credential_file_mounts,
            register_credential_files,
        )

        clear_credential_files()
        try:
            content = SKILL_MD.read_text(encoding="utf-8")
            fm = _parse_frontmatter(content)
            entries = fm.get("required_credential_files", [])

            with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
                missing = register_credential_files(entries)

            assert "google_token.json" in missing
            mounts = get_credential_file_mounts()
            container_paths = {m["container_path"] for m in mounts}
            assert "/root/.hermes/google_client_secret.json" in container_paths
            assert "/root/.hermes/google_token.json" not in container_paths
        finally:
            clear_credential_files()
