from __future__ import annotations

from types import SimpleNamespace

import tools.environments.docker as docker_module
import tools.terminal_tool as terminal_tool
from tools.environments.docker import (
    DockerEnvironment,
    _desired_bind_mounts_from_run_args,
)


def test_desired_bind_mounts_parse_readonly_credentials() -> None:
    mounts = _desired_bind_mounts_from_run_args(
        [
            "-v",
            "/home/hermes/.hermes/google-shared/google_token.json:/root/.hermes/google_token.json:ro",
            "--volume",
            "/tmp/work:/workspace",
        ]
    )

    assert mounts["/root/.hermes/google_token.json"] == (
        "/home/hermes/.hermes/google-shared/google_token.json",
        False,
    )
    assert mounts["/workspace"] == ("/tmp/work", True)


def test_reusable_container_rejected_when_credential_mount_source_drifted(monkeypatch) -> None:
    env = object.__new__(DockerEnvironment)
    env._all_run_args = [
        "-v",
        "/home/hermes/.hermes/google-shared/google_token.json:/root/.hermes/google_token.json:ro",
    ]

    monkeypatch.setattr(
        "tools.credential_files.get_credential_file_mounts",
        lambda: [{
            "host_path": "/home/hermes/.hermes/google-shared/google_token.json",
            "container_path": "/root/.hermes/google_token.json",
        }],
    )
    monkeypatch.setattr(
        env,
        "_inspect_bind_mounts",
        lambda _container_id: {
            "/root/.hermes/google_token.json": (
                "/home/hermes/.hermes/profiles/ian/google_token.json",
                False,
            )
        },
    )

    assert env._container_mounts_match_run_args("abc123") is False


def test_reusable_container_accepted_when_required_mounts_match(monkeypatch) -> None:
    env = object.__new__(DockerEnvironment)
    env._all_run_args = [
        "-v",
        "/home/hermes/.hermes/google-shared/google_token.json:/root/.hermes/google_token.json:ro",
    ]

    monkeypatch.setattr(
        "tools.credential_files.get_credential_file_mounts",
        lambda: [{
            "host_path": "/home/hermes/.hermes/google-shared/google_token.json",
            "container_path": "/root/.hermes/google_token.json",
        }],
    )
    monkeypatch.setattr(
        env,
        "_inspect_bind_mounts",
        lambda _container_id: {
            "/root/.hermes/google_token.json": (
                "/home/hermes/.hermes/google-shared/google_token.json",
                False,
            )
        },
    )

    assert env._container_mounts_match_run_args("abc123") is True


def test_current_credential_mounts_detects_missing_new_skill_mount(monkeypatch) -> None:
    env = object.__new__(DockerEnvironment)
    env._container_id = "abc123"

    monkeypatch.setattr(
        "tools.credential_files.get_credential_file_mounts",
        lambda: [
            {
                "host_path": "/home/hermes/.hermes/google-shared/google_token.json",
                "container_path": "/root/.hermes/google_token.json",
            }
        ],
    )
    monkeypatch.setattr(env, "_inspect_bind_mounts", lambda _container_id: {})

    assert env.current_credential_mounts_satisfied() is False


def test_current_credential_mounts_accepts_current_skill_mount(monkeypatch) -> None:
    env = object.__new__(DockerEnvironment)
    env._container_id = "abc123"

    monkeypatch.setattr(
        "tools.credential_files.get_credential_file_mounts",
        lambda: [
            {
                "host_path": "/home/hermes/.hermes/google-shared/google_token.json",
                "container_path": "/root/.hermes/google_token.json",
            }
        ],
    )
    monkeypatch.setattr(
        env,
        "_inspect_bind_mounts",
        lambda _container_id: {
            "/root/.hermes/google_token.json": (
                "/home/hermes/.hermes/google-shared/google_token.json",
                False,
            )
        },
    )

    assert env.current_credential_mounts_satisfied() is True


def test_reuse_discards_stale_candidate_before_accepting_current_one(monkeypatch) -> None:
    env = object.__new__(DockerEnvironment)
    env._docker_exe = "docker"
    env._all_run_args = ["-v", "/new/token:/root/.hermes/google_token.json:ro"]
    removed = []

    monkeypatch.setattr(
        "tools.credential_files.get_credential_file_mounts",
        lambda: [{
            "host_path": "/new/token",
            "container_path": "/root/.hermes/google_token.json",
        }],
    )
    monkeypatch.setattr(
        docker_module,
        "_docker_query",
        lambda *args, **kwargs: SimpleNamespace(stdout="stale\trunning\ncurrent\texited\n"),
    )
    monkeypatch.setattr(
        env,
        "_container_mounts_match_run_args",
        lambda container_id: container_id == "current",
    )
    monkeypatch.setattr(env, "_remove_stale_reusable_container", removed.append)

    assert env._find_reusable_container("task", "profile", "enabled") == ("current", "exited")
    assert removed == ["stale"]


def test_cached_terminal_environment_recreated_for_new_credentials(monkeypatch) -> None:
    class OldEnv:
        def __init__(self):
            self.cleanup_calls = []

        def current_credential_mounts_satisfied(self):
            return False

        def cleanup(self, *, force_remove=False):
            self.cleanup_calls.append(force_remove)

    old_env = OldEnv()
    new_env = object()
    plan = terminal_tool._ExecPlan(
        config={}, env_type="docker", effective_task_id="credential-refresh",
        image="image", cwd="/root", host_cwd=None, effective_timeout=30,
    )
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal_tool, "_create_configured_env", lambda *args, **kwargs: new_env)
    terminal_tool._active_environments[plan.effective_task_id] = old_env
    terminal_tool._last_activity[plan.effective_task_id] = 0
    try:
        assert terminal_tool._acquire_env(plan, None) is new_env
        assert old_env.cleanup_calls == [True]
        assert terminal_tool._active_environments[plan.effective_task_id] is new_env
    finally:
        terminal_tool._active_environments.pop(plan.effective_task_id, None)
        terminal_tool._last_activity.pop(plan.effective_task_id, None)
