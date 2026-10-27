"""Tests for the container-side CLI (cld/cli_container.py).

The surface is defined by what a container can actually reach: the host broker for
anything needing a docker daemon, the bind-mounted mailbox for anything needing a
conversation. See docs/design-cli-split.md.
"""

import json
import os
import subprocess
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from cld.cli_container import app
from cld.config import Config


runner = CliRunner()


def _write_mailbox(root: Path, name: str, *, meta: dict | None = None) -> Path:
    d = root / name
    (d / "inbox").mkdir(parents=True, exist_ok=True)
    (d / "archive").mkdir(parents=True, exist_ok=True)
    if meta is not None:
        (d / "meta.json").write_text(json.dumps(meta))
    return d


def _meta(**over) -> dict:
    base = {
        "parent": "", "task": "wire up oauth login", "persona": "implementer",
        "deliverable_branch": "add-oauth", "anchor": "a" * 40, "peers": {},
        "created_at": "2026-08-12T10:00:00.000000Z",
    }
    base.update(over)
    return base


class TestHostOnlyStubs:
    """Verbs that need a docker daemon say so instead of failing obscurely."""

    @pytest.mark.parametrize("argv", [
        ["build"],
        [],
        # Ticket lifecycle verbs (v2) drive the docker daemon, so they are
        # host-only too -- each stub takes any args without a usage error.
        ["start", "lide-2600", "lide-api"],
        ["claude", "lide-2600", "--", "--model", "opus"],
        ["shell", "lide-2600"],
        ["stop", "lide-2600"],
        ["restart", "lide-2600"],
        ["shutdown", "--all"],
        ["status"],
        ["logs", "lide-2600"],
    ])
    def test_refused_with_a_host_only_message(self, argv):
        result = runner.invoke(app, argv)
        assert result.exit_code == 2
        assert "host-only" in result.output

    def test_hidden_from_help(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        for verb in ("build", "start", "claude",
                     "shell", "stop", "restart", "shutdown", "status", "logs"):
            assert f"│ {verb}" not in result.output
        for verb in ("task-agent", "msg", "run", "chain"):
            assert f"│ {verb}" in result.output


class TestTaskAgentDispatch:
    """Lifecycle goes through the broker, which stamps --parent and refuses --force
    (docs/design-task-agents.md §9)."""

    def _broker(self, stack, tmp_path, monkeypatch, available=True):
        monkeypatch.setenv("CLD_MAILBOX_ROOT", str(tmp_path / "mailboxes"))
        stack.enter_context(patch("cld.cli_container.broker_available", return_value=available))
        return stack.enter_context(patch("cld.cli_container.broker_task_agent_op", return_value=0))

    def test_start_forwards_rebuilt_argv(self, tmp_path, monkeypatch):
        with ExitStack() as stack:
            op = self._broker(stack, tmp_path, monkeypatch)
            result = runner.invoke(app, [
                "task-agent", "start", "@personas/implementer", "@some-task",
                "-n", "add-oauth", "-p", "do it",
                "--branch", "oauth", "-m", "opus", "-r", "@-", "--peer", "cld_agent_x_y:3",
            ])
        assert result.exit_code == 0, result.output
        verb, argv = op.call_args.args
        assert verb == "start"
        assert argv == [
            "@personas/implementer", "@some-task", "-n", "add-oauth", "-p", "do it",
            "--branch", "oauth", "-m", "opus", "-r", "@-", "--peer", "cld_agent_x_y:3",
        ]
        assert "--parent" not in argv          # the broker sets it, host-side
        assert "--repo" not in argv            # none given: the broker's single-repo default

    def test_start_repo_leads_the_argv(self, tmp_path, monkeypatch):
        """The broker consumes only a LEADING --repo; anything later is start's own argv."""
        with ExitStack() as stack:
            op = self._broker(stack, tmp_path, monkeypatch)
            result = runner.invoke(app, ["task-agent", "start", "@some-task", "-n", "s", "--repo", "other"])
        assert result.exit_code == 0, result.output
        assert op.call_args.args == ("start", ["--repo", "other", "@some-task", "-n", "s"])

    def test_start_folds_local_files_into_the_prompt_in_order(self, tmp_path, monkeypatch):
        first = tmp_path / "a.md"; first.write_text("# First\n")
        second = tmp_path / "b.md"; second.write_text("# Second\n")
        with ExitStack() as stack:
            op = self._broker(stack, tmp_path, monkeypatch)
            result = runner.invoke(app, [
                "task-agent", "start", "@personas/implementer", str(first), str(second),
                "-n", "s", "-p", "and this",
            ])
        assert result.exit_code == 0, result.output
        argv = op.call_args.args[1]
        assert str(first) not in argv         # a container path means nothing host-side
        assert "@personas/implementer" in argv
        body = argv[argv.index("-p") + 1]
        assert body == "# First\n\n# Second\n\nand this"

    def test_start_on_a_directory_exits_cleanly(self, tmp_path, monkeypatch):
        """A directory positional used to traceback out of read_text()."""
        with ExitStack() as stack:
            op = self._broker(stack, tmp_path, monkeypatch)
            result = runner.invoke(app, ["task-agent", "start", str(tmp_path), "-n", "s"])
        assert result.exit_code == 1
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert not op.called

    def test_start_forwards_an_at_ref_verbatim(self, tmp_path, monkeypatch):
        """The host must resolve it against the *target* repo, not master's own."""
        with ExitStack() as stack:
            op = self._broker(stack, tmp_path, monkeypatch)
            result = runner.invoke(app, ["task-agent", "start", "@some-task", "-n", "s"])
        assert result.exit_code == 0, result.output
        assert "@some-task" in op.call_args.args[1]

    def test_status_and_logs_dispatch(self, tmp_path, monkeypatch):
        with ExitStack() as stack:
            op = self._broker(stack, tmp_path, monkeypatch)
            assert runner.invoke(app, ["task-agent", "status"]).exit_code == 0
            assert op.call_args.args == ("status", [])
            assert runner.invoke(app, ["task-agent", "logs", "slug", "-n", "20"]).exit_code == 0
            assert op.call_args.args == ("logs", ["slug", "-n", "20"])

    def test_shutdown_dispatches_name_and_all(self, tmp_path, monkeypatch):
        with ExitStack() as stack:
            op = self._broker(stack, tmp_path, monkeypatch)
            assert runner.invoke(app, ["task-agent", "shutdown", "slug"]).exit_code == 0
            assert op.call_args.args == ("shutdown", ["slug"])
            assert runner.invoke(app, ["task-agent", "shutdown", "--all"]).exit_code == 0
            assert op.call_args.args == ("shutdown", ["--all"])

    def test_shutdown_force_refused_locally_with_a_reason(self, tmp_path, monkeypatch):
        with ExitStack() as stack:
            op = self._broker(stack, tmp_path, monkeypatch)
            result = runner.invoke(app, ["task-agent", "shutdown", "slug", "--force"])
        assert result.exit_code == 1
        assert "host-only" in result.output
        assert "wrap-up has not finished" in result.output
        assert not op.called

    def test_missing_broker_names_what_still_works(self, tmp_path, monkeypatch):
        with ExitStack() as stack:
            op = self._broker(stack, tmp_path, monkeypatch, available=False)
            result = runner.invoke(app, ["task-agent", "shutdown", "slug"])
        assert result.exit_code == 1
        assert "broker_key" in result.output
        assert "transcript" in result.output      # reading the fleet needs no broker
        assert not op.called

    def test_transcript_works_without_the_broker_or_docker(self, tmp_path, monkeypatch):
        root = tmp_path / "mailboxes"
        name = "cld_agent_myrepo_add-oauth"
        d = _write_mailbox(root, name, meta=_meta())
        (d / "archive" / "m1.json").write_text(json.dumps({
            "id": "m1", "from": "cld_master_myrepo_ab12", "to": name,
            "subject": "kick off", "body": "start here", "ts": "2026-08-12T10:00:00Z",
        }))
        monkeypatch.setenv("CLD_MAILBOX_ROOT", str(root))
        with patch("cld.task_agent.docker_task_agent_list", return_value=[]):
            result = runner.invoke(app, ["task-agent", "transcript", "add-oauth"])
        assert result.exit_code == 0, result.output
        assert "kick off" in result.output
        assert "start here" in result.output


_TICKET = {"v": 1, "ticket": "t1", "repos": [
    {"name": "api", "path": "/h/api", "anchor_base": "a" * 40},
    {"name": "web", "path": "/h/web", "anchor_base": "b" * 40},
]}
_CHANGE_ID = "k" * 32


def _jj_result(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


class _Clock:
    """A fake `time` module: sleeping advances the clock instead of waiting."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture(params=["run", "chain"])
def kind(request) -> str:
    return request.param


class TestRunAndChainDispatch:
    """`cld run|chain` go through the broker with a change id, never a revset or a path."""

    @pytest.fixture(autouse=True)
    def ticket(self, monkeypatch):
        monkeypatch.setenv("CLD_TICKET_MANIFEST", json.dumps({**_TICKET, "repos": _TICKET["repos"][:1]}))

    @pytest.fixture
    def stack(self):
        with ExitStack() as stack:
            stack.enter_context(patch("cld.cli_container.broker_available", return_value=True))
            yield stack

    def _ops(self, stack) -> dict[str, MagicMock]:
        return {
            k: stack.enter_context(patch(f"cld.cli_container.broker_{k}_op", return_value=_jj_result()))
            for k in ("run", "chain")
        }

    def _jj(self, stack, *, resolved: str = _CHANGE_ID + "\n", working_copy: str = "") -> MagicMock:
        def fake(argv, **_):
            revset = argv[argv.index("-r") + 1]
            return _jj_result(working_copy if "working_copies()" in revset else resolved)
        return stack.enter_context(patch("cld.cli_container.subprocess.run", side_effect=fake))

    def _first_ref(self, kind: str) -> list[str]:
        return ["@personas/implementer"] if kind == "run" else ["@review-chain"]

    def test_start_sends_the_change_id_not_the_revset(self, stack, kind):
        ops = self._ops(stack)
        jj = self._jj(stack)
        result = runner.invoke(app, [
            kind, "start", *self._first_ref(kind), "-r", "@-", "-n", "job", "-m", "opus", "-p", "do it",
        ])
        assert result.exit_code == 0, result.output
        assert ops[kind].call_args.args == (
            "start", *self._first_ref(kind), "-n", "job", "-p", "do it", "-m", "opus", "-r", _CHANGE_ID,
        )
        assert ops[kind].call_args.kwargs["repo"] == ""
        assert not ops["chain" if kind == "run" else "run"].called
        assert jj.call_args_list[0].args[0][:4] == ["jj", "-R", "/workspace/t1/api", "log"]
        assert jj.call_args_list[0].args[0][jj.call_args_list[0].args[0].index("-r") + 1] == "@-"

    def test_start_folds_local_files_into_the_prompt(self, stack, tmp_path, kind):
        local = tmp_path / "brief.md"
        local.write_text("# Brief\n")
        ops = self._ops(stack)
        self._jj(stack)
        result = runner.invoke(app, [
            kind, "start", *self._first_ref(kind), str(local), "-r", "@-", "-p", "and this",
        ])
        assert result.exit_code == 0, result.output
        argv = ops[kind].call_args.args
        assert str(local) not in argv
        assert argv[argv.index("-p") + 1] == "# Brief\n\nand this"

    def test_start_passes_repo_to_the_helper_and_resolves_in_its_workspace(self, stack, monkeypatch, kind):
        monkeypatch.setenv("CLD_TICKET_MANIFEST", json.dumps(_TICKET))
        ops = self._ops(stack)
        jj = self._jj(stack)
        result = runner.invoke(app, [kind, "start", *self._first_ref(kind), "-r", "@-", "--repo", "web"])
        assert result.exit_code == 0, result.output
        assert ops[kind].call_args.kwargs["repo"] == "web"
        assert "--repo" not in ops[kind].call_args.args
        assert jj.call_args_list[0].args[0][2] == "/workspace/t1/web"

    def test_start_requires_a_revision(self, stack, kind):
        ops = self._ops(stack)
        result = runner.invoke(app, [kind, "start", *self._first_ref(kind)])
        assert result.exit_code == 2
        assert not ops[kind].called

    def test_working_copy_base_is_refused_with_a_hint(self, stack, kind):
        ops = self._ops(stack)
        self._jj(stack, working_copy=_CHANGE_ID + "\n")
        result = runner.invoke(app, [kind, "start", *self._first_ref(kind), "-r", "@"])
        assert result.exit_code == 1
        assert "jj new" in result.output
        assert not ops[kind].called

    def test_working_copy_probe_uses_the_resolved_change_id(self, stack, kind):
        self._ops(stack)
        jj = self._jj(stack)
        runner.invoke(app, [kind, "start", *self._first_ref(kind), "-r", "a | b"])
        probe = jj.call_args_list[1].args[0]
        assert probe[probe.index("-r") + 1] == f"{_CHANGE_ID} & working_copies()"

    @pytest.mark.parametrize("resolved", ["", f"{'k' * 32}\n{'m' * 32}\n"], ids=["none", "several"])
    def test_revision_must_resolve_to_exactly_one_change(self, stack, kind, resolved):
        ops = self._ops(stack)
        self._jj(stack, resolved=resolved)
        result = runner.invoke(app, [kind, "start", *self._first_ref(kind), "-r", "x"])
        assert result.exit_code == 1
        assert "exactly one change" in result.output
        assert not ops[kind].called

    def test_a_failing_jj_is_an_error(self, stack, kind):
        ops = self._ops(stack)
        stack.enter_context(patch(
            "cld.cli_container.subprocess.run", return_value=_jj_result(returncode=1, stderr="no such revision"),
        ))
        result = runner.invoke(app, [kind, "start", *self._first_ref(kind), "-r", "nope"])
        assert result.exit_code == 1
        assert "no such revision" in result.output
        assert not ops[kind].called

    def test_chain_needs_an_at_ref_first(self, stack, tmp_path):
        local = tmp_path / "chain.yaml"
        local.write_text("x")
        ops = self._ops(stack)
        self._jj(stack)
        result = runner.invoke(app, ["chain", "start", str(local), "-r", "@-"])
        assert result.exit_code == 1
        assert "@<chain-name>" in result.output
        assert not ops["chain"].called

    @pytest.mark.parametrize(("manifest", "argv", "message"), [
        (_TICKET, [], "mounts 2 repos"),
        (_TICKET, ["--repo", "other"], "not mounted"),
        (None, [], "only in a ticket container"),
    ], ids=["ambiguous", "unmounted", "no-ticket"])
    def test_repo_must_be_one_of_the_mounted_ones(self, stack, monkeypatch, kind, manifest, argv, message):
        if manifest is None:
            monkeypatch.delenv("CLD_TICKET_MANIFEST")
        else:
            monkeypatch.setenv("CLD_TICKET_MANIFEST", json.dumps(manifest))
        ops = self._ops(stack)
        jj = self._jj(stack)
        result = runner.invoke(app, [kind, "start", *self._first_ref(kind), "-r", "@-", *argv])
        assert result.exit_code == 1
        assert message in result.output
        assert not ops[kind].called and not jj.called

    def test_status_and_cancel_dispatch(self, stack, kind):
        ops = self._ops(stack)
        assert runner.invoke(app, [kind, "status"]).exit_code == 0
        assert ops[kind].call_args.args == ("status",)
        assert runner.invoke(app, [kind, "status", "job"]).exit_code == 0
        assert ops[kind].call_args.args == ("status", "job")
        assert runner.invoke(app, [kind, "cancel", "job"]).exit_code == 0
        assert ops[kind].call_args.args == ("cancel", "job")
        assert ops[kind].call_args.kwargs["capture"] is False

    def test_broker_exit_code_is_propagated(self, stack, kind):
        ops = self._ops(stack)
        ops[kind].return_value = _jj_result(returncode=3)
        assert runner.invoke(app, [kind, "cancel", "job"]).exit_code == 3

    def test_missing_broker_is_reported(self, kind):
        with patch("cld.cli_container.broker_available", return_value=False), \
             patch(f"cld.cli_container.broker_{kind}_op") as op:
            result = runner.invoke(app, [kind, "status"])
        assert result.exit_code == 1
        assert "broker_key" in result.output
        assert not op.called


class TestRunAndChainWait:
    """`wait` polls `status <name>` every 15 s, bounded under the agent's command limit."""

    @pytest.fixture
    def clock(self):
        clock = _Clock()
        with patch("cld.cli_container.time", clock), patch("cld.cli_container.broker_available", return_value=True):
            yield clock

    @staticmethod
    def _status(state: str) -> subprocess.CompletedProcess:
        return _jj_result(f"name: job\nkind: run\nstate: {state}\nbookmark: job\n")

    def _wait(self, kind: str, states: list[subprocess.CompletedProcess], *argv: str):
        with patch(f"cld.cli_container.broker_{kind}_op", side_effect=states) as op:
            return runner.invoke(app, [kind, "wait", "job", *argv]), op

    def test_finished_at_once_prints_the_detail_and_exits_zero(self, clock, kind):
        result, op = self._wait(kind, [self._status("success")])
        assert result.exit_code == 0, result.output
        assert "state: success" in result.output
        assert op.call_args.args == ("status", "job")
        assert op.call_args.kwargs["capture"] is True
        assert clock.sleeps == []

    def test_polls_every_fifteen_seconds_until_not_running(self, clock, kind):
        result, op = self._wait(kind, [self._status("running")] * 3 + [self._status("failed")])
        assert result.exit_code == 0, result.output
        assert clock.sleeps == [15, 15, 15]
        assert op.call_count == 4
        assert "state: failed" in result.output

    def test_timeout_exits_124_after_a_final_poll(self, clock, kind):
        result, op = self._wait(kind, [self._status("running")] * 10, "--timeout", "40")
        assert result.exit_code == 124
        assert clock.sleeps == [15, 15, 10]
        assert op.call_count == 4
        assert "state: running" in result.output

    def test_default_timeout_is_540_seconds(self, clock, kind):
        result, _ = self._wait(kind, [self._status("running")] * 100)
        assert result.exit_code == 124
        assert sum(clock.sleeps) == 540

    def test_timeout_above_the_maximum_is_rejected(self, clock, kind):
        result, op = self._wait(kind, [], "--timeout", "541")
        assert result.exit_code == 2
        assert not op.called

    def test_broker_errors_propagate(self, clock, kind):
        result, _ = self._wait(kind, [_jj_result(returncode=1, stderr="denied: not your job")])
        assert result.exit_code == 1
        assert "denied: not your job" in result.output

    def test_a_status_without_a_state_line_is_an_error(self, clock, kind):
        result, _ = self._wait(kind, [_jj_result("something else\n")])
        assert result.exit_code == 1


class TestBrokerExitCodes:
    """typer.Exit subclasses RuntimeError, so _handle_errors has to let it through."""

    def _invoke(self, rc):
        with ExitStack() as stack:
            stack.enter_context(patch("cld.cli_container.broker_available", return_value=True))
            stack.enter_context(patch("cld.cli_container.broker_task_agent_op", return_value=rc))
            return runner.invoke(app, ["task-agent", "status"])

    def test_zero_stays_zero(self):
        assert self._invoke(0).exit_code == 0

    def test_failure_code_is_propagated(self):
        assert self._invoke(3).exit_code == 3


class TestRepos:
    @pytest.fixture(autouse=True)
    def no_broker(self):
        with patch("cld.cli_container.broker_available", return_value=False):
            yield

    def test_lists_the_containers_own_repo(self, monkeypatch):
        # The host-set CLD_HOST_PROJECT_DIR is the channel, not TOML re-read
        # in-container (see build_container_args in cld/docker.py).
        monkeypatch.delenv("CLD_TICKET_MANIFEST", raising=False)
        cfg = Config(host_project_dir="/host/side/cld")
        with patch("cld.cli_container.Config.from_env", return_value=cfg):
            result = runner.invoke(app, ["repos"])
        assert result.exit_code == 0, result.output
        assert "/host/side/cld\town" in result.output

    def _ticket_manifest_json(self):
        return json.dumps({
            "v": 1,
            "ticket": "lide-2600",
            "repos": [
                {"name": "lide-api", "path": "/home/u/lide-api",
                 "anchor_base": "a" * 40, "anchor_mode": "isolated",
                 "rev_source": "registry"},
                {"name": "diskuze-api", "path": "/home/u/diskuze-api",
                 "anchor_base": "b" * 40, "anchor_mode": "shared",
                 "rev_source": "arg"},
            ],
        })

    def test_ticket_container_prints_the_manifest(self, monkeypatch):
        """In a ticket container (v2) `repos` renders CLD_TICKET_MANIFEST:
        name, origin path, workspace path, anchor, mode (design section 6.6)."""
        monkeypatch.setenv("CLD_TICKET_MANIFEST", self._ticket_manifest_json())
        with patch("cld.cli_container.Config.from_env", return_value=Config()):
            result = runner.invoke(app, ["repos"])
        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        assert (
            f"lide-api\t/workspace/origin/lide-api\t/workspace/lide-2600/lide-api"
            f"\t{'a' * 12}\tisolated"
        ) in lines
        assert (
            f"diskuze-api\t/workspace/origin/diskuze-api\t/workspace/lide-2600/diskuze-api"
            f"\t{'b' * 12}\tshared"
        ) in lines
        assert "\town" not in result.output

    def test_v1_output_when_no_manifest_env(self, monkeypatch):
        monkeypatch.delenv("CLD_TICKET_MANIFEST", raising=False)
        with patch("cld.cli_container.Config.from_env",
                   return_value=Config(host_project_dir="/host/side/cld")):
            result = runner.invoke(app, ["repos"])
        assert result.exit_code == 0, result.output
        assert "/host/side/cld\town" in result.output

    def test_registry_block_lists_only_unmounted_repos(self, monkeypatch):
        monkeypatch.setenv("CLD_TICKET_MANIFEST", self._ticket_manifest_json())
        registry = [("lide-api", "/home/u/lide-api", True), ("fastapi-deps", "/home/u/deps", False)]
        with ExitStack() as stack:
            stack.enter_context(patch("cld.cli_container.Config.from_env", return_value=Config()))
            stack.enter_context(patch("cld.cli_container.broker_available", return_value=True))
            stack.enter_context(patch("cld.cli_container.list_registered_repos", return_value=registry))
            result = runner.invoke(app, ["repos"])
        assert result.exit_code == 0, result.output
        block = result.output.split("Also registered on the host (not mounted here):\n", 1)[1]
        assert block.splitlines() == ["fastapi-deps\t/home/u/deps"]

    def test_no_registry_block_without_the_broker(self, monkeypatch):
        monkeypatch.setenv("CLD_TICKET_MANIFEST", self._ticket_manifest_json())
        with ExitStack() as stack:
            stack.enter_context(patch("cld.cli_container.Config.from_env", return_value=Config()))
            listing = stack.enter_context(patch("cld.cli_container.list_registered_repos"))
            result = runner.invoke(app, ["repos"])
        assert result.exit_code == 0, result.output
        assert "Also registered" not in result.output
        assert not listing.called


class TestMsg:
    """The mailbox verbs replace `python3 -m cld.messenger.*`; each calls the same
    module function the MCP server uses."""

    def test_send_reads_the_body_file(self, tmp_path):
        body = tmp_path / "body.md"
        body.write_text("the body\n")
        with patch("cld.cli_msg.send_cmd.deliver") as deliver:
            result = runner.invoke(app, [
                "msg", "send", "--to", "cld_agent_x_y", "--subject", "hi",
                "--body-file", str(body), "--expects-reply", "--answers", "m3",
            ])
        assert result.exit_code == 0, result.output
        assert deliver.call_args.args == ("cld_agent_x_y", "hi", "the body\n")
        assert deliver.call_args.kwargs == {"expects_reply": True, "answers": "m3"}

    def test_send_accepts_inline_body(self):
        with patch("cld.cli_msg.send_cmd.deliver") as deliver:
            result = runner.invoke(app, [
                "msg", "send", "--to", "cld_agent_x_y", "--subject", "hi",
                "--body", "quick note",
            ])
        assert result.exit_code == 0, result.output
        assert deliver.call_args.args == ("cld_agent_x_y", "hi", "quick note")

    def test_send_rejects_body_and_body_file_together(self, tmp_path):
        body = tmp_path / "body.md"
        body.write_text("the body\n")
        result = runner.invoke(app, [
            "msg", "send", "--to", "cld_agent_x_y", "--subject", "hi",
            "--body", "quick note", "--body-file", str(body),
        ])
        assert result.exit_code == 1
        assert "exactly one of --body or --body-file" in result.output

    def test_send_rejects_neither_body_nor_body_file(self):
        result = runner.invoke(app, [
            "msg", "send", "--to", "cld_agent_x_y", "--subject", "hi",
        ])
        assert result.exit_code == 1
        assert "exactly one of --body or --body-file" in result.output

    def test_inbox_all_flag(self):
        with patch("cld.cli_msg.inbox_cmd.show") as show:
            assert runner.invoke(app, ["msg", "inbox", "--all"]).exit_code == 0
        assert show.call_args.args == (True,)

    def test_read_takes_an_id(self):
        with patch("cld.cli_msg.read_cmd.show") as show:
            assert runner.invoke(app, ["msg", "read", "m1"]).exit_code == 0
        assert show.call_args.args == ("m1",)

    def test_archive_takes_an_id(self):
        with patch("cld.cli_msg.archive_cmd.move") as move:
            assert runner.invoke(app, ["msg", "archive", "m1"]).exit_code == 0
        assert move.call_args.args == ("m1",)

    def test_ticket_flag_routes_into_the_identity_chain(self, monkeypatch):
        """--ticket travels to resolve_self via the CLD_TICKET env branch
        (design-ticket-containers.md section 6.5) -- the explicit-ticket rung
        of the host-side identity chain."""
        monkeypatch.setenv("CLD_TICKET", "previous")  # also restores env after
        with patch("cld.cli_msg.send_cmd.deliver") as deliver:
            result = runner.invoke(app, [
                "msg", "send", "--to", "cld_ticket_other", "--subject", "hi",
                "--body", "b", "--ticket", "LIDE-2600",
            ])
        assert result.exit_code == 0, result.output
        assert os.environ["CLD_TICKET"] == "LIDE-2600"
        deliver.assert_called_once()

    def test_without_ticket_flag_the_identity_chain_is_untouched(self, monkeypatch):
        monkeypatch.setenv("CLD_TICKET", "previous")
        with patch("cld.cli_msg.inbox_cmd.show"):
            assert runner.invoke(app, ["msg", "inbox"]).exit_code == 0
        assert os.environ["CLD_TICKET"] == "previous"

    @pytest.mark.parametrize("argv,target", [
        (["msg", "inbox", "--ticket", "t9"], "cld.cli_msg.inbox_cmd.show"),
        (["msg", "read", "m1", "--ticket", "t9"], "cld.cli_msg.read_cmd.show"),
        (["msg", "archive", "m1", "--ticket", "t9"], "cld.cli_msg.archive_cmd.move"),
    ])
    def test_ticket_flag_on_every_identity_verb(self, monkeypatch, argv, target):
        monkeypatch.setenv("CLD_TICKET", "previous")
        with patch(target) as cmd:
            assert runner.invoke(app, argv).exit_code == 0
        assert os.environ["CLD_TICKET"] == "t9"
        cmd.assert_called_once()

    def test_agents_kind_filter_defaults_to_none(self):
        with patch("cld.cli_msg.agents_cmd.show") as show:
            assert runner.invoke(app, ["msg", "agents"]).exit_code == 0
            assert show.call_args.args == (None,)
            assert runner.invoke(app, ["msg", "agents", "--kind", "task-agent"]).exit_code == 0
            assert show.call_args.args == ("task-agent",)


class TestPrompts:
    def test_lists_refs_with_descriptions(self, tmp_path):
        items = [("personas/architect", "Designs a solution"), ("todo-agent", "")]
        with patch("cld.cli_container.list_prompt_items", return_value=items):
            result = runner.invoke(app, ["prompts"])
        assert result.exit_code == 0, result.output
        assert "personas/architect" in result.output
        assert "Designs a solution" in result.output
