"""Tests for the broker seam (local daemon on the host vs ssh to the cld broker)."""

import base64
import json
import subprocess
from unittest.mock import patch

import pytest

from cld import broker


def _cp(returncode=0, stdout="", stderr=""):
    return type("R", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()


@pytest.fixture
def configured(monkeypatch):
    """A container with an endpoint and a mounted key -- i.e. the broker is reachable."""
    monkeypatch.setenv("CLD_BROKER_ENDPOINT", "host.docker.internal:2222")
    monkeypatch.setenv("SESSION_NAME", "cld_agent_cld")
    with patch("cld.broker.Path.exists", return_value=True):
        yield


def _argv_of(run_mock) -> list[str]:
    """Decode the base64(NUL-joined) argv out of the ssh command the client built."""
    payload = run_mock.call_args[0][0][-1].split(" ")[2]
    return [a.decode() for a in base64.b64decode(payload).split(b"\0") if a]


class TestListCldContainers:
    def test_host_uses_local_docker(self):
        ps = _cp(stdout="cld_agent_a\tUp 1 min\n")
        inspect = _cp(stdout="agent|/repo/a\n")
        with patch("cld.broker.in_master_container", return_value=False), \
             patch("cld.broker.subprocess.run", side_effect=[ps, inspect]):
            out = broker.list_cld_containers()
        assert out == [{"name": "cld_agent_a", "kind": "agent", "repo": "/repo/a", "status": "running"}]

    def test_hub_flag_uses_broker_and_parses_lines(self, configured):
        lines = _cp(stdout=(
            "cld_agent_a\tagent\t/repo/a\tUp 3 hours\n"
            "cld_ticket_lide-2600\tticket\t/repo/b\tExited (0) 2 hours ago\n"
        ))
        with patch("cld.broker.in_master_container", return_value=True), \
             patch("cld.broker.subprocess.run", return_value=lines) as run:
            out = broker.list_cld_containers("agent")
        assert run.call_args[0][0][0] == "ssh"
        assert _argv_of(run) == ["agent"]                      # the kind filter
        assert run.call_args[0][0][-1].startswith("list-containers cld_agent_cld ")
        assert out == [
            {"name": "cld_agent_a", "kind": "agent", "repo": "/repo/a", "status": "running"},
            {"name": "cld_ticket_lide-2600", "kind": "ticket", "repo": "/repo/b", "status": "stopped"},
        ]

    def test_broker_failure_returns_empty(self, configured):
        with patch("cld.broker.in_master_container", return_value=True), \
             patch("cld.broker.subprocess.run", return_value=_cp(returncode=3, stderr="denied")):
            assert broker.list_cld_containers() == []

    def test_malformed_broker_line_skipped(self, configured):
        lines = _cp(stdout="not-enough-fields\ncld_a\tagent\t/r\tUp 1 min\n")
        with patch("cld.broker.in_master_container", return_value=True), \
             patch("cld.broker.subprocess.run", return_value=lines):
            out = broker.list_cld_containers()
        assert out == [{"name": "cld_a", "kind": "agent", "repo": "/r", "status": "running"}]


class TestBrokerAvailable:
    def test_true_with_endpoint_key_and_known_hosts(self, configured):
        assert broker.broker_available() is True

    def test_false_without_a_key(self, monkeypatch):
        monkeypatch.setenv("CLD_BROKER_ENDPOINT", "host.docker.internal:2222")
        with patch("cld.broker.Path.exists", return_value=False):
            assert broker.broker_available() is False

    def test_false_without_known_hosts(self, monkeypatch):
        """run_action always pins the host key, so a missing known_hosts is unusable."""
        monkeypatch.setenv("CLD_BROKER_ENDPOINT", "host.docker.internal:2222")
        with patch("cld.broker.Path.exists", autospec=True,
                   side_effect=lambda self: str(self) == broker.KEY_MOUNT):
            assert broker.broker_available() is False

    def test_false_without_an_endpoint(self, monkeypatch):
        monkeypatch.delenv("CLD_BROKER_ENDPOINT", raising=False)
        with patch("cld.broker.Path.exists", return_value=True):
            assert broker.broker_available() is False


class TestEndpoint:
    @pytest.mark.parametrize("endpoint,expected", [
        ("host.docker.internal:2222", ("zet", "host.docker.internal", "2222")),
        ("someone@myhost:2244", ("someone", "myhost", "2244")),
        ("myhost", ("zet", "myhost", "2222")),
    ])
    def test_split(self, monkeypatch, endpoint, expected):
        monkeypatch.setenv("CLD_BROKER_ENDPOINT", endpoint)
        assert broker._endpoint() == expected


class TestRunAction:
    def test_refuses_when_not_configured(self, monkeypatch):
        monkeypatch.delenv("CLD_BROKER_ENDPOINT", raising=False)
        with patch("cld.broker.subprocess.run") as run:
            result = broker.run_action("run-tests", "-k", "login")
        assert result.returncode == 127
        assert not run.called

    def test_argv_never_becomes_a_command(self, configured):
        """Args are base64(NUL-joined), so a shell metacharacter stays an argument."""
        with patch("cld.broker.subprocess.run", return_value=_cp()) as run:
            broker.run_action("run-tests", "-k", "login; rm -rf /")
        assert _argv_of(run) == ["-k", "login; rm -rf /"]
        assert "rm -rf" not in " ".join(run.call_args[0][0][:-1])

    def test_pins_the_host_key(self, configured):
        with patch("cld.broker.subprocess.run", return_value=_cp()) as run:
            broker.run_action("run-tests")
        cmd = run.call_args[0][0]
        assert "StrictHostKeyChecking=yes" in cmd
        assert f"UserKnownHostsFile={broker.KNOWN_HOSTS_MOUNT}" in cmd
        assert broker.KEY_MOUNT in cmd

    def test_ssh_never_reads_stdin(self, configured):
        """Inside the graphql MCP server, stdin carries the MCP protocol."""
        with patch("cld.broker.subprocess.run", return_value=_cp()) as run:
            broker.run_action("list-containers")
        assert run.call_args.kwargs["stdin"] is subprocess.DEVNULL


class TestSnapshotBeforeRevisionActions:
    """The broker reads the caller's revision from the jj store without
    snapshotting, so the client snapshots every workspace first -- otherwise an
    edit made a moment ago could be missing from the run."""

    _MANIFEST = json.dumps({"v": 1, "ticket": "lide-1", "repos": [
        {"name": "api", "path": "/h/api", "anchor_base": "a" * 40},
        {"name": "diskuze", "path": "/h/diskuze", "anchor_base": "b" * 40},
    ]})

    def _commands(self, run_mock) -> list[list[str]]:
        return [c[0][0] for c in run_mock.call_args_list]

    @pytest.mark.parametrize("action", ["run-tests", "graphql", "run", "chain"])
    def test_ticket_snapshots_every_mounted_repo_before_ssh(self, configured, monkeypatch, action):
        monkeypatch.setenv("CLD_TICKET_MANIFEST", self._MANIFEST)
        with patch("cld.broker.subprocess.run", return_value=_cp()) as run:
            broker.run_action(action, "status")
        cmds = self._commands(run)
        assert cmds[:2] == [["jj", "-R", "/workspace/lide-1/api", "status"],
                            ["jj", "-R", "/workspace/lide-1/diskuze", "status"]]
        assert cmds[2][0] == "ssh"

    def test_v1_snapshots_its_single_workspace(self, configured, monkeypatch):
        monkeypatch.delenv("CLD_TICKET_MANIFEST", raising=False)
        with patch("cld.broker.subprocess.run", return_value=_cp()) as run:
            broker.run_action("run-tests")
        assert self._commands(run)[0] == ["jj", "-R", "/workspace/current", "status"]

    def test_other_actions_do_not_snapshot(self, configured):
        with patch("cld.broker.subprocess.run", return_value=_cp()) as run:
            broker.run_action("list-containers")
        assert [c[0] for c in self._commands(run)] == ["ssh"]

    @pytest.mark.parametrize("failure", [FileNotFoundError("jj"), subprocess.TimeoutExpired("jj", 30)],
                             ids=["missing-jj", "stuck-jj"])
    def test_failed_snapshot_still_reaches_the_broker(self, configured, failure):
        with patch("cld.broker.subprocess.run", side_effect=[failure, _cp()]) as run:
            assert broker.run_action("run-tests").returncode == 0
        assert self._commands(run)[-1][0] == "ssh"

    def test_unreadable_manifest_skips_the_snapshot(self, configured, monkeypatch):
        monkeypatch.setenv("CLD_TICKET_MANIFEST", '{"v": 99}')
        with patch("cld.broker.subprocess.run", return_value=_cp()) as run:
            assert broker.run_action("graphql", "status").returncode == 0
        assert [c[0] for c in self._commands(run)] == ["ssh"]


class TestGraphqlOp:
    def test_forwards_op_and_args_capturing_by_default(self, configured):
        with patch("cld.broker.subprocess.run", return_value=_cp(returncode=0, stdout="ok")) as run:
            result = broker.graphql_op("status")
        assert result.stdout == "ok"
        assert run.call_args[0][0][-1].startswith("graphql cld_agent_cld ")
        assert _argv_of(run) == ["status"]
        assert run.call_args.kwargs["capture_output"] is True

    def test_query_string_with_spaces_quotes_and_semicolon_is_one_argv_element(self, configured):
        """Mirrors test_argv_never_becomes_a_command: a query string is one opaque argument."""
        weird = 'query { user(id: "1; rm -rf /") { name } }'
        with patch("cld.broker.subprocess.run", return_value=_cp()) as run:
            broker.graphql_op("query", "local", weird, "{}")
        assert _argv_of(run) == ["query", "local", weird, "{}"]
        assert "rm -rf" not in " ".join(run.call_args[0][0][:-1])

    def test_capture_can_be_disabled(self, configured):
        with patch("cld.broker.subprocess.run", return_value=_cp()) as run:
            broker.graphql_op("start", capture=False)
        assert run.call_args.kwargs["capture_output"] is False

    def test_repo_becomes_a_leading_repo_flag(self, configured):
        """A ticket container's repo target travels as `--repo <name>` BEFORE the
        op, where the broker script's parse_repo_arg consumes it (design section 6.1)."""
        with patch("cld.broker.subprocess.run", return_value=_cp()) as run:
            broker.graphql_op("status", repo="lide-api")
        assert _argv_of(run) == ["--repo", "lide-api", "status"]

    def test_no_repo_keeps_v1_argv(self, configured):
        """Single-repo and v1 callers send exactly the argv they always did."""
        with patch("cld.broker.subprocess.run", return_value=_cp()) as run:
            broker.graphql_op("status")
        assert _argv_of(run) == ["status"]


class TestBrokerTaskAgentOp:
    def test_uses_its_own_action_and_forwards_argv(self, configured):
        """Its own broker action, with its own op set and argv rules."""
        argv = ["@implementer", "-n", "add-oauth", "-p", "do it", "--peer", "cld_agent_x_y:3"]
        with patch("cld.broker.subprocess.run", return_value=_cp(returncode=0)) as run:
            rc = broker.broker_task_agent_op("start", argv)
        assert rc == 0
        assert run.call_args[0][0][-1].startswith("task-agent cld_agent_cld ")
        assert _argv_of(run) == ["start", *argv]
        assert run.call_args.kwargs["capture_output"] is False

    def test_propagates_exit_code(self, configured):
        with patch("cld.broker.subprocess.run", return_value=_cp(returncode=2)):
            assert broker.broker_task_agent_op("shutdown", ["--all"]) == 2


class TestBrokerJobOps:
    """`run` and `chain` send an optional leading --repo, then the op, then its args."""

    @pytest.mark.parametrize(("helper", "action"), [
        (broker.broker_run_op, "run"), (broker.broker_chain_op, "chain"),
    ], ids=["run", "chain"])
    def test_uses_its_own_action(self, configured, helper, action):
        with patch("cld.broker.subprocess.run", return_value=_cp()) as run:
            helper("start", "@x", "-r", "abc")
        assert run.call_args[0][0][-1].startswith(f"{action} cld_agent_cld ")
        assert _argv_of(run) == ["start", "@x", "-r", "abc"]

    def test_repo_leads_the_op(self, configured):
        with patch("cld.broker.subprocess.run", return_value=_cp()) as run:
            broker.broker_run_op("start", "@x", repo="api")
        assert _argv_of(run) == ["--repo", "api", "start", "@x"]

    def test_streams_by_default_and_captures_on_request(self, configured):
        with patch("cld.broker.subprocess.run", return_value=_cp()) as run:
            broker.broker_chain_op("status")
            assert run.call_args.kwargs["capture_output"] is False
            broker.broker_chain_op("status", "c", capture=True)
            assert run.call_args.kwargs["capture_output"] is True

    def test_returns_the_completed_process(self, configured):
        with patch("cld.broker.subprocess.run", return_value=_cp(returncode=3, stdout="out")):
            result = broker.broker_run_op("cancel", "run_x")
        assert (result.returncode, result.stdout) == (3, "out")


class TestListRegisteredRepos:
    def test_parses_the_mounted_column(self, configured):
        out = "lide-api\t/h/lide-api\tyes\ndeps\t/h/deps\tno\n"
        with patch("cld.broker.subprocess.run", return_value=_cp(returncode=0, stdout=out)) as run:
            assert broker.list_registered_repos() == [("lide-api", "/h/lide-api", True), ("deps", "/h/deps", False)]
        assert run.call_args[0][0][-1].startswith("list-repos cld_agent_cld ")

    def test_failure_is_empty(self, configured):
        with patch("cld.broker.subprocess.run", return_value=_cp(returncode=3, stderr="boom")):
            assert broker.list_registered_repos() == []
