"""Tests for cld.run.launch_run: input validation and how the brief travels.

The brief is composed host-side (cld.prompts.compose_brief) and ships inside the
anchor scratch envelope, so launch_run has no prompt mounts left to build.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cld.run import launch_run
from cld.config import Config


class TestLaunchAgentValidation:
    def test_empty_brief_exits(self):
        with patch("cld.run.require_docker"):
            with pytest.raises(SystemExit):
                launch_run(Config(), "")

    def test_whitespace_only_brief_exits(self):
        with patch("cld.run.require_docker"):
            with pytest.raises(SystemExit):
                launch_run(Config(), "  \n\n")


def _args_contain(cmd, *args):
    """Return True if args appear consecutively in cmd."""
    n = len(args)
    for i in range(len(cmd) - n + 1):
        if cmd[i : i + n] == list(args):
            return True
    return False


class TestLaunchAgentExtensions:
    """Verify the brief and extra_env produce the right docker run args."""

    def _capture_docker_cmd(self, brief="do the thing", **kwargs):
        """Patch all externals; return the full docker-run command list."""
        session = "agent_testxx"

        docker_result = MagicMock()
        docker_result.returncode = 0
        docker_result.stdout = "abc123def\n"
        docker_result.stderr = ""

        with (
            patch("cld.run.require_docker"),
            patch("cld.run.find_target_repo", return_value=Path("/fake/repo")),
            patch("cld.run.prepare_store_mounts") as preflight,
            patch("cld.run.ensure_image"),
            patch("cld.run.build_session_name", return_value=session),
            patch("cld.run.build_container_args", return_value=[]),
            patch("cld.run.get_backend") as backend,
            patch("cld.run.resolve_anchor", return_value="cafef00d1234") as resolve,
            patch("cld.run.anchor_env_args", return_value=["-e", "AGENT_REVISION_HINT=cafef00d1234"]) as anchor,
            patch("cld.run.subprocess.run", return_value=docker_result) as mock_run,
        ):
            self.order = MagicMock()
            self.order.attach_mock(preflight, "preflight")
            self.order.attach_mock(resolve, "resolve")
            self.backend = backend
            self.resolve = resolve
            launch_run(Config(), brief, quiet=True, **kwargs)
            self.anchor_call = anchor.call_args
            return list(mock_run.call_args.args[0])

    def test_store_preflight_runs_before_the_anchor_is_resolved(self):
        self._capture_docker_cmd()
        calls = self.order.mock_calls
        assert [name for name, _, _ in calls] == ["preflight", "resolve"]
        assert calls[0].args == (Path("/fake/repo"),)

    def test_anchor_is_resolved_plain_without_the_overlap_check(self):
        with patch("cld.docker.docker_occupant_list") as occupants:
            self._capture_docker_cmd(revision="abc123")
        occupants.assert_not_called()
        self.backend.assert_called_once_with(Path("/fake/repo"))
        self.resolve.assert_called_once_with(self.backend.return_value, "abc123")

    def test_brief_goes_to_the_anchor_envelope(self):
        self._capture_docker_cmd(brief="# Role\n\ndo the thing")
        assert self.anchor_call.kwargs["brief"] == "# Role\n\ndo the thing"

    def test_extra_env_adds_env_args(self):
        cmd = self._capture_docker_cmd(extra_env={"FOO": "bar"})
        assert _args_contain(cmd, "-e", "FOO=bar")

    def test_no_prompt_mounts_left(self):
        """The brief rides in the scratch envelope, so nothing is mounted for it."""
        cmd_str = " ".join(self._capture_docker_cmd())
        assert "/config/persona.md" not in cmd_str
        assert "/config/task.md" not in cmd_str
        assert "AGENT_INLINE_PROMPT" not in cmd_str
        assert "AGENT_SYSTEM_PROMPT_FILE" not in cmd_str
