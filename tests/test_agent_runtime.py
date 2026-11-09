"""Tests for cld.agent_runtime.wait_for_agent."""

from unittest.mock import MagicMock, patch

from cld.agent_runtime import wait_for_agent
from cld.config import Config


def _ps(stdout: str):
    return MagicMock(returncode=0, stdout=stdout, stderr="")


class TestWaitForAgent:
    def test_docker_filter_is_anchored_and_dots_are_escaped(self):
        """`t.us` must not also match `tXus`: docker reads the filter value as a regex."""
        vcs = MagicMock()
        vcs.file_show.return_value = '{"status": "success"}'
        with patch("cld.agent_runtime.subprocess.run", return_value=_ps("")) as run:
            summary = wait_for_agent("chain.demo.build", vcs, Config())
        assert summary == {"status": "success"}
        assert run.call_args.args[0] == [
            "docker", "ps", "--filter", r"name=^chain\.demo\.build$", "--format", "{{.Status}}",
        ]

    def test_polls_until_the_container_is_gone(self):
        vcs = MagicMock()
        vcs.file_show.return_value = '{"status": "success"}'
        cfg = Config(poll_interval=0)
        with patch("cld.agent_runtime.subprocess.run", side_effect=[_ps("Up 1s"), _ps("Up 2s"), _ps("")]) as run, \
             patch("cld.agent_runtime.time.sleep"):
            wait_for_agent("run.ab12cd", vcs, cfg)
        assert run.call_count == 3
