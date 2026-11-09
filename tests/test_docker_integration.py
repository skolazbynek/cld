"""Layer 2: Docker integration tests against the real daemon."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from cld.config import Config
from cld.docker import (
    TaskAgentSpec,
    build_container_args,
    docker_task_agent_list,
    docker_task_agent_status,
    ensure_image,
    require_docker,
)
from tests.conftest import skip_no_docker


pytestmark = [pytest.mark.integration, pytest.mark.docker]


@skip_no_docker
class TestRequireDocker:
    def test_passes_when_available(self):
        require_docker()


@skip_no_docker
class TestEnsureImage:
    def test_existing_image_is_noop(self, tmp_path, monkeypatch):
        # Should not trigger a build for an already-existing image
        # Use a dummy dockerfile path since it shouldn't be touched
        import cld.docker as docker_mod
        expected = docker_mod._content_hash([tmp_path / "nonexistent.Dockerfile"], None)
        monkeypatch.setattr(docker_mod, "_image_label", lambda *_: expected)
        ensure_image(
            "claude-devcontainer:latest",
            tmp_path / "nonexistent.Dockerfile",
            tmp_path,
        )

    def test_missing_image_would_build(self, tmp_path):
        # Verify the function detects missing images.
        # We don't actually build -- just confirm it tries (and fails on bad Dockerfile).
        with pytest.raises(subprocess.CalledProcessError):
            ensure_image(
                "test-nonexistent-image:never",
                tmp_path / "Dockerfile",
                tmp_path,
            )


@skip_no_docker
class TestBuildContainerArgs:
    def test_structure_has_required_flags(self, jj_repo):
        args = build_container_args(jj_repo.repo_root, "test-session", Config())
        assert "--rm" in args
        assert "--cap-drop=ALL" in args
        assert "--security-opt=no-new-privileges" in args

    def test_session_name_in_env(self, jj_repo):
        args = build_container_args(jj_repo.repo_root, "mysession", Config())
        env_pairs = [args[i+1] for i in range(len(args)-1) if args[i] == "-e"]
        assert "SESSION_NAME=mysession" in env_pairs

    def test_workspace_volume_mounted(self, jj_repo):
        args = build_container_args(jj_repo.repo_root, "test-session", Config())
        volume_args = [args[i+1] for i in range(len(args)-1) if args[i] == "-v"]
        assert any("/workspace/origin" in v for v in volume_args)

    def test_no_it_flag(self, jj_repo):
        """Every launcher detaches and attaches with `docker exec -it` if at all."""
        args = build_container_args(jj_repo.repo_root, "test-session", Config())
        assert "-it" not in args

    def test_plain_mode_has_no_mailbox_mount(self, jj_repo):
        args = build_container_args(jj_repo.repo_root, "test-session", Config())
        volume_args = [args[i+1] for i in range(len(args)-1) if args[i] == "-v"]
        assert not any("/var/cld/mailboxes" in v for v in volume_args)

    def test_task_agent_mode_mounts_mailbox_and_labels(self, jj_repo, tmp_path):
        cfg = Config(mailbox_root=str(tmp_path / "mailboxes"))
        args = build_container_args(
            jj_repo.repo_root, "a.x.t", cfg, task_agent=TaskAgentSpec(slug="t"),
        )
        volume_args = [args[i+1] for i in range(len(args)-1) if args[i] == "-v"]
        env_pairs = [args[i+1] for i in range(len(args)-1) if args[i] == "-e"]
        assert any(v.endswith(":/var/cld/mailboxes:rw") for v in volume_args)
        assert "org.cld.kind=task-agent" in args
        assert "AGENT_MODE=1" in env_pairs
        assert (tmp_path / "mailboxes").is_dir()

    def test_no_role_publishes_hub_mode_or_targets(self, jj_repo, tmp_path):
        """Both went with the master role: nothing produces them any more."""
        cfg = Config(mailbox_root=str(tmp_path / "mailboxes"))
        args = build_container_args(
            jj_repo.repo_root, "a.x.t", cfg, task_agent=TaskAgentSpec(slug="t"),
        )
        env_pairs = [args[i+1] for i in range(len(args)-1) if args[i] == "-e"]
        assert "HUB_MODE=1" not in env_pairs
        assert not any(e.startswith("MASTER_TARGETS=") for e in env_pairs)
        assert not any(a.startswith("org.cld.targets=") for a in args)

    def test_nested_mailbox_mount_does_not_mkdir_or_touch_docker(self, jj_repo, caplog):
        """When host_project_dir/host_home make us 'nested' (cld running inside
        another container), the real host path isn't in our filesystem view.
        We must NOT mkdir our own -- wrong -- local path, and must NOT reach
        across the docker socket to create it on the real host either
        (container isolation from the host is a hard requirement, even though
        the socket happens to be shared for launching sibling containers).
        Just mount the translated path and warn -- see CLAUDE.md's Messenger
        deviation note."""
        from cld.docker import CONTAINER_HOME
        mailbox_path = f"{CONTAINER_HOME}/.cld/mailboxes-test-nested-xyz"
        cfg = Config(
            mailbox_root=mailbox_path,
            host_project_dir="/host/project",
            host_home="/host/home",
        )
        assert not Path(mailbox_path).exists()
        with patch("cld.docker.subprocess.run") as run_mock:
            args = build_container_args(
                jj_repo.repo_root, "a.x.t", cfg, task_agent=TaskAgentSpec(slug="t"),
            )
        assert not run_mock.called
        assert not Path(mailbox_path).exists()
        volume_args = [args[i+1] for i in range(len(args)-1) if args[i] == "-v"]
        assert "/host/home/.cld/mailboxes-test-nested-xyz:/var/cld/mailboxes:rw" in volume_args
        assert "outside this process's filesystem view" in caplog.text


@skip_no_docker
class TestDockerTaskAgentHelpers:
    def test_status_absent_for_unknown_name(self):
        assert docker_task_agent_status("a.definitely.not_running_xyz") == "absent"

    def test_list_returns_list(self):
        # No real task-agent containers running in this environment; just verify
        # the docker label query round-trips without error.
        assert isinstance(docker_task_agent_list(), list)


@skip_no_docker
class TestRunContainer:
    def test_detached_trivial_container(self):
        result = subprocess.run(
            ["docker", "run", "--rm", "--detach", "claude-devcontainer:latest",
             "echo", "hello"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        cid = result.stdout.strip()
        assert len(cid) >= 12
        # Wait for it to finish, then it auto-removes
        subprocess.run(
            ["docker", "wait", cid],
            capture_output=True, text=True, timeout=30,
        )

    def test_container_auto_removes(self):
        # Override entrypoint to skip VCS checks
        result = subprocess.run(
            ["docker", "run", "--rm", "--name", "test-auto-rm",
             "--entrypoint", "echo",
             "claude-devcontainer:latest", "hello"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        assert "hello" in result.stdout
        # Container should be gone (--rm)
        check = subprocess.run(
            ["docker", "ps", "-a", "--filter", "name=^test-auto-rm$", "--format", "{{.ID}}"],
            capture_output=True, text=True,
        )
        assert not check.stdout.strip()
