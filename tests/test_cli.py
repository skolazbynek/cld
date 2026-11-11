"""Tests for CLI argument validation via typer's CliRunner."""

import json
import os
import signal
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from cld.chain_state import ChainState, StateWriter
from cld.cli import (
    _assert_reap_ready,
    _reap_task_agent,
    _resolve_chain_path,
    app,
)
from cld.task_agent import parse_peer_specs, resolve_task_agent
from cld.config import Config
from cld import names
from tests.test_chain import _seed_state


runner = CliRunner()


class TestRunCommand:
    def test_no_refs_no_prompt_errors(self):
        result = runner.invoke(app, ["run"])
        assert result.exit_code == 1
        assert "at least one prompt ref" in result.output

    def test_missing_task_file_errors(self, tmp_path):
        result = runner.invoke(app, ["run", str(tmp_path / "nope.md")])
        assert result.exit_code == 1
        assert "not found" in result.output


class TestVersion:
    def test_version_flag_prints_and_exits(self):
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "cld " in result.output


class TestRunAtNotation:
    """Tests for @<name> prompt shortcut in cld run."""

    # Use names that don't exist in the real /workspace/current/prompts/ to
    # avoid duplicate-match errors when cld_root == the live repo root.
    _TASK_NAME = "test-xuniq-task-zz9"
    _PERSONA_NAME = "test-xuniq-persona-zz9"

    def _make_prompts(self, tmp_path: Path):
        task = tmp_path / "prompts" / f"{self._TASK_NAME}.md"
        task.parent.mkdir(parents=True)
        task.write_text("# Task\nDo something\n")
        persona = tmp_path / "prompts" / "personas" / f"{self._PERSONA_NAME}.md"
        persona.parent.mkdir(parents=True)
        persona.write_text(f"---\nname: {self._PERSONA_NAME}\n---\n# Test persona\n")
        return task, persona

    def test_at_notation_unknown_name_errors(self, tmp_path):
        with patch("cld.cli.find_repo_root", return_value=tmp_path):
            (tmp_path / "prompts").mkdir(parents=True)
            result = runner.invoke(app, ["run", "@no-such-prompt-xuniq", "-p", "task"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_at_notation_ambiguous_name_errors(self, tmp_path):
        (tmp_path / "prompts" / "tasks").mkdir(parents=True)
        (tmp_path / "prompts" / "tasks" / "test-xdup-zz9.md").write_text("a")
        (tmp_path / "prompts" / "personas").mkdir()
        (tmp_path / "prompts" / "personas" / "test-xdup-zz9.md").write_text("b")
        with patch("cld.cli.find_repo_root", return_value=tmp_path):
            result = runner.invoke(app, ["run", "@test-xdup-zz9", "-p", "task"])
        assert result.exit_code == 1
        assert "Ambiguous" in result.output

    def test_at_notation_composes_the_brief(self, tmp_path):
        self._make_prompts(tmp_path)
        with patch("cld.cli.find_repo_root", return_value=tmp_path), \
             patch("cld.cli.launch_run") as la:
            result = runner.invoke(app, ["run", f"@{self._TASK_NAME}"])
        assert result.exit_code == 0, result.output
        assert la.call_args.args[1] == "# Task\nDo something\n"

    def test_refs_and_inline_compose_in_order(self, tmp_path):
        """Personas and task files are interchangeable blocks; -p lands last."""
        self._make_prompts(tmp_path)
        with patch("cld.cli.find_repo_root", return_value=tmp_path), \
             patch("cld.cli.launch_run") as la:
            result = runner.invoke(app, [
                "run", f"@personas/{self._PERSONA_NAME}", f"@{self._TASK_NAME}",
                "-p", "do the task",
            ])
        assert result.exit_code == 0, result.output
        brief = la.call_args.args[1]
        assert brief == "# Test persona\n\n# Task\nDo something\n\ndo the task\n"


class TestRootGroup:
    def test_no_subcommand_prints_help(self):
        """No subcommand is a usage error now, not a launch: the root group has
        no default command left, so click prints the help and exits 2."""
        result = runner.invoke(app, [])
        assert result.exit_code == 2
        assert "run" in result.output and "task-agent" in result.output

    def test_no_standing_agent_verb(self):
        """The standing per-repo `cld agent` role is gone; only `task-agent` is left.

        Asserted on the registered command names, not on an exit code: a
        re-added `agent` group with no default subcommand would also exit 2.
        """
        verbs = {t.name for t in app.registered_groups} | {
            c.name or c.callback.__name__ for c in app.registered_commands
        }
        assert "task-agent" in verbs
        assert "agent" not in verbs

    # Prompt refs are a `cld run` / `cld task-agent start` surface: click reads a group
    # callback's first positional as a subcommand name, so the root group takes
    # options only and anything positional is a usage error (exit 2), not a
    # dropped arg.
    @pytest.mark.parametrize("argv", [["@personas/x"], ["tsak.md"], ["task-agent", "shutdwn"]])
    def test_positionals_are_a_usage_error(self, argv):
        result = runner.invoke(app, argv)
        assert result.exit_code == 2


class TestTicketVerbs:
    """The 8 root-level ticket lifecycle commands dispatch to cld/ticket.py.

    Each is its own @app.command() with the ticket as the COMMAND's positional;
    the root group callback still takes none (the typer hazard --
    TestBareDevcontainer::test_positionals_are_a_usage_error guards that)."""

    def _invoke(self, argv, **patches):
        with ExitStack() as stack:
            stack.enter_context(patch("cld.cli.require_docker"))
            mocks = {
                name: stack.enter_context(patch(f"cld.cli.{name}"))
                for name in patches.get("names", [])
            }
            result = runner.invoke(app, argv)
        return result, mocks

    def test_start_passes_ticket_repos_and_shared(self):
        result, mocks = self._invoke(
            ["start", "LIDE-2600", "lide-api@trunk()", "diskuze-api",
             "--shared-anchor", "lide-api"],
            names=["start_ticket"],
        )
        assert result.exit_code == 0, result.output
        args = mocks["start_ticket"].call_args.args
        assert args[1] == "LIDE-2600"
        assert args[2] == ["lide-api@trunk()", "diskuze-api"]
        assert args[3] == ["lide-api"]

    def test_start_without_repos_passes_empty_list(self):
        result, mocks = self._invoke(["start", "LIDE-2600"], names=["start_ticket"])
        assert result.exit_code == 0, result.output
        assert mocks["start_ticket"].call_args.args[2] == []

    @pytest.mark.parametrize(
        ("status", "specs"),
        [
            ("absent", ["/home/z/diskuze-api@@"]),
            ("stopped", []),
            ("running", []),
        ],
        ids=["absent-anchors-on-working-copy", "stopped-keeps-anchors", "running-keeps-anchors"],
    )
    def test_start_with_no_args_targets_cwd_repo_ad_hoc(self, status, specs):
        """Bare `cld start`: cwd's repo root, ad hoc (not by registry name),
        ticket named after the repo directory, anchored on `@` only at creation."""
        with ExitStack() as stack:
            stack.enter_context(patch("cld.cli.require_docker"))
            stack.enter_context(
                patch("cld.cli.find_target_repo", return_value=Path("/home/z/diskuze-api"))
            )
            stack.enter_context(patch("cld.cli._docker_status", return_value=status))
            start_ticket = stack.enter_context(patch("cld.cli.start_ticket"))
            result = runner.invoke(app, ["start"])
        assert result.exit_code == 0, result.output
        args = start_ticket.call_args.args
        assert args[1] == "diskuze-api"
        assert args[2] == specs
        assert args[3] == []

    def test_claude_passes_everything_after_dashdash_through(self):
        result, mocks = self._invoke(
            ["claude", "LIDE-2600", "--", "--model", "opus", "--continue"],
            names=["exec_claude"],
        )
        assert result.exit_code == 0, result.output
        assert mocks["exec_claude"].call_args.args == (
            "LIDE-2600", ["--model", "opus", "--continue"],
        )

    def test_shell_stop_restart_logs_dispatch(self):
        for argv, name, expect in (
            (["shell", "t1"], "exec_shell", ("t1",)),
            (["stop", "t1"], "stop_ticket", ("t1",)),
            (["logs", "t1", "-n", "5"], "print_ticket_logs", ("t1", 5)),
        ):
            result, mocks = self._invoke(argv, names=[name])
            assert result.exit_code == 0, result.output
            assert mocks[name].call_args.args == expect

    def test_restart_dispatches_with_cfg(self):
        result, mocks = self._invoke(["restart", "t1"], names=["restart_ticket"])
        assert result.exit_code == 0, result.output
        assert mocks["restart_ticket"].call_args.args[1] == "t1"
        assert mocks["restart_ticket"].call_args.args[2] == []

    def test_restart_reanchor_repeats_in_order(self):
        result, mocks = self._invoke(
            ["restart", "t1", "--reanchor", "lide-api@feature-x",
             "--reanchor", "diskuze-api@main"],
            names=["restart_ticket"],
        )
        assert result.exit_code == 0, result.output
        assert mocks["restart_ticket"].call_args.args[2] == [
            "lide-api@feature-x", "diskuze-api@main",
        ]

    def test_shutdown_rejects_ticket_and_all(self):
        result, _ = self._invoke(
            ["shutdown", "t1", "--all"], names=["shutdown_ticket", "shutdown_all_tickets"])
        assert result.exit_code == 1
        assert "not both" in result.output

    @pytest.mark.parametrize(
        ("argv", "name", "expect"),
        [
            (["claude", "--", "--continue"], "exec_claude", ("diskuze-api", ["--continue"])),
            (["shell"], "exec_shell", ("diskuze-api",)),
            (["stop"], "stop_ticket", ("diskuze-api",)),
            (["logs", "-n", "5"], "print_ticket_logs", ("diskuze-api", 5)),
            (["shutdown"], "shutdown_ticket", ("diskuze-api",)),
        ],
        ids=["claude", "shell", "stop", "logs", "shutdown"],
    )
    def test_bare_verb_targets_cwd_repo_ticket(self, argv, name, expect):
        """Bare verbs name the ticket after the cwd repo directory, as bare `cld start` does."""
        with patch("cld.cli.find_target_repo", return_value=Path("/home/z/diskuze-api")):
            result, mocks = self._invoke(argv, names=[name])
        assert result.exit_code == 0, result.output
        assert mocks[name].call_args.args == expect

    def test_bare_restart_targets_cwd_repo_ticket(self):
        with patch("cld.cli.find_target_repo", return_value=Path("/home/z/diskuze-api")):
            result, mocks = self._invoke(["restart"], names=["restart_ticket"])
        assert result.exit_code == 0, result.output
        assert mocks["restart_ticket"].call_args.args[1:] == ("diskuze-api", [])

    def test_shutdown_one_and_all(self):
        result, mocks = self._invoke(
            ["shutdown", "t1"], names=["shutdown_ticket", "shutdown_all_tickets"])
        assert result.exit_code == 0, result.output
        assert mocks["shutdown_ticket"].call_args.args == ("t1",)
        mocks["shutdown_all_tickets"].assert_not_called()

        result, mocks = self._invoke(
            ["shutdown", "--all"], names=["shutdown_ticket", "shutdown_all_tickets"])
        assert result.exit_code == 0, result.output
        mocks["shutdown_all_tickets"].assert_called_once()
        mocks["shutdown_ticket"].assert_not_called()

    def test_status_roster_or_detail(self):
        result, mocks = self._invoke(
            ["status"], names=["print_ticket_roster", "print_ticket_detail"])
        assert result.exit_code == 0, result.output
        mocks["print_ticket_roster"].assert_called_once()
        mocks["print_ticket_detail"].assert_not_called()

        result, mocks = self._invoke(
            ["status", "t1"], names=["print_ticket_roster", "print_ticket_detail"])
        assert result.exit_code == 0, result.output
        assert mocks["print_ticket_detail"].call_args.args == ("t1",)
        mocks["print_ticket_roster"].assert_not_called()


class TestChainBlockedInMaster:
    def test_run_chain_blocked_inside_master(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HUB_MODE", "1")
        from cld.chain import run_chain
        from cld.config import Config
        chain_file = tmp_path / "c.yaml"
        chain_file.write_text("name: dummy\nsteps: []\n")
        with pytest.raises(RuntimeError, match="not yet supported from inside a master"):
            run_chain(Config(), chain_file)


class TestBuildCommand:
    def test_build_help(self):
        result = runner.invoke(app, ["build", "--help"])
        assert result.exit_code == 0
        assert "no-cache" in result.output


# --- Task-scoped agents -------------------------------------------------------

def _write_mailbox(root: Path, name: str, *, meta: dict | None = None, state: dict | None = None) -> Path:
    d = root / name
    (d / "inbox").mkdir(parents=True, exist_ok=True)
    (d / "archive").mkdir(parents=True, exist_ok=True)
    if meta is not None:
        (d / "meta.json").write_text(json.dumps(meta))
    if state is not None:
        (d / "state.json").write_text(json.dumps(state))
    return d


def _meta(**over) -> dict:
    base = {
        "parent": "", "task": "wire up oauth login", "persona": "implementer",
        "deliverable_branch": "add-oauth", "anchor": "a" * 40, "peers": {},
        "created_at": "2026-08-12T10:00:00.000000Z",
    }
    base.update(over)
    return base


def _ta(name: str, **over) -> dict:
    """One docker_task_agent_list record."""
    rec = {
        "name": name, "repo_root": "/host/myrepo", "session": name,
        "kind": "task-agent", "parent": "", "task": name.rsplit("_", 1)[-1],
    }
    rec.update(over)
    return rec


@pytest.fixture
def start_env(tmp_path, monkeypatch):
    """Stub out everything `cld task-agent start` touches; yield the mocks."""
    repo_root = tmp_path / "myrepo"
    (repo_root / "prompts" / "personas").mkdir(parents=True)
    (repo_root / "prompts" / "personas" / "implementer.md").write_text("---\nx: y\n---\n# impl\n")
    mailbox_root = tmp_path / "mailboxes"
    mailbox_root.mkdir()
    monkeypatch.setenv("CLD_MAILBOX_ROOT", str(mailbox_root))

    with ExitStack() as stack:
        def p(target, **kw):
            return stack.enter_context(patch(f"cld.cli.{target}", **kw))

        # One mock behind both bindings: the roster enumerates in cld.task_agent,
        # the reap path in cld.cli, and a test that seeds peers means both.
        ta_list = MagicMock(return_value=[])
        for mod in ("cld.cli", "cld.task_agent"):
            stack.enter_context(patch(f"{mod}.docker_task_agent_list", ta_list))

        yield SimpleNamespace(
            repo_root=repo_root,
            mailbox_root=mailbox_root,
            anchor_hash="c0ffee" * 6 + "abcd",
            require_docker=p("require_docker"),
            find_target_repo=p("find_target_repo", return_value=repo_root),
            preflight=p("prepare_store_mounts"),
            ensure_image=p("ensure_image"),
            capacity=p("assert_task_agent_capacity"),
            resolve_anchor=p("resolve_anchor_checked", return_value="c0ffee" * 6 + "abcd"),
            # The real namer, minus the docker probe: slug validation stays real.
            allocate=p("allocate_task_agent_name", side_effect=names.agent_id),
            deliverable_free=p("assert_deliverable_free"),
            get_backend=p("get_backend", return_value=SimpleNamespace(name="jj")),
            bca=p("build_container_args", return_value=["--name", "placeholder"]),
            anchor_env=p("anchor_env_args", return_value=["-e", "AGENT_REVISION_HINT=deadbeef"]),
            stage_home=p("stage_home_ro", return_value=[]),
            stage_ssh=p("stage_ssh_agent", return_value=[]),
            run=p("subprocess.run"),
            ready=p("_wait_for_container_ready", return_value=True),
            task_agent_list=ta_list,
        )


class TestParsePeerSpecs:
    def test_bare_name_takes_default_limit(self):
        assert parse_peer_specs(["a.t1.api-fix"], 10) == {"a.t1.api-fix": 10}

    def test_explicit_limit(self):
        assert parse_peer_specs(["a:5"], 10) == {"a": 5}

    def test_multiple_peers(self):
        assert parse_peer_specs(["a:5", "b"], 7) == {"a": 5, "b": 7}

    def test_empty(self):
        assert parse_peer_specs([], 10) == {}

    def test_duplicate_name_raises(self):
        with pytest.raises(ValueError, match="named twice"):
            parse_peer_specs(["a:5", "a:6"], 10)

    @pytest.mark.parametrize("spec", ["a:0", "a:-1", "a:x", "a:"])
    def test_non_positive_limit_raises(self, spec):
        with pytest.raises(ValueError, match="positive integer"):
            parse_peer_specs([spec], 10)

    def test_missing_name_raises(self):
        with pytest.raises(ValueError, match="missing peer name"):
            parse_peer_specs([":5"], 10)


class TestTaskAgentStart:
    def _invoke(self, *extra):
        return runner.invoke(app, ["task-agent", "start", "@personas/implementer", *extra])

    def test_requires_refs_or_prompt(self, start_env):
        result = runner.invoke(app, ["task-agent", "start", "-n", "add-oauth"])
        assert result.exit_code == 1
        assert "at least one prompt ref" in result.output
        assert not start_env.run.called

    def test_requires_a_slug(self, start_env):
        result = self._invoke("-p", "do it")
        assert result.exit_code == 1
        assert "task slug is required" in result.output
        assert not start_env.run.called

    def test_slug_from_name_branch_defaults_to_it(self, start_env):
        result = self._invoke("-p", "do it", "-n", "add-oauth")
        assert result.exit_code == 0, result.output
        spec = start_env.bca.call_args.kwargs["task_agent"]
        assert spec.slug == "add-oauth"
        assert spec.deliverable_branch == "a._host.add-oauth.mr"
        assert spec.parent_master == ""
        assert spec.peers == {}

    def test_slug_falls_back_to_branch(self, start_env):
        result = self._invoke("-p", "do it", "--branch", "feature-x")
        assert result.exit_code == 0, result.output
        spec = start_env.bca.call_args.kwargs["task_agent"]
        assert spec.slug == "feature-x"
        assert spec.deliverable_branch == "a._host.feature-x.feature-x"

    def test_dotted_branch_is_no_slug_fallback(self, start_env):
        result = self._invoke("-p", "do it", "--branch", "t.x.mr")
        assert result.exit_code == 1
        assert "task slug is required" in result.output
        assert not start_env.run.called

    def test_branch_overrides_default(self, start_env):
        result = self._invoke("-p", "do it", "-n", "add-oauth", "--branch", "oauth-work")
        assert result.exit_code == 0, result.output
        spec = start_env.bca.call_args.kwargs["task_agent"]
        assert (spec.slug, spec.deliverable_branch) == ("add-oauth", "a._host.add-oauth.oauth-work")

    def test_dotted_branch_is_used_as_is(self, start_env):
        result = self._invoke("-p", "do it", "-n", "add-oauth", "--branch", "t.x.mr.migration")
        assert result.exit_code == 0, result.output
        spec = start_env.bca.call_args.kwargs["task_agent"]
        assert spec.deliverable_branch == "t.x.mr.migration"

    @pytest.mark.parametrize("branch", ["a/b", "a..b", "x.lock"])
    def test_bad_branch_errors_before_spawning(self, start_env, branch):
        result = self._invoke("-p", "do it", "-n", "add-oauth", "--branch", branch)
        assert result.exit_code == 1
        assert "invalid name" in result.output
        assert not start_env.run.called

    @pytest.mark.parametrize(
        ("parent", "ticket"),
        [("t.lide-1", "lide-1"), ("a.lide-1.review", "lide-1"), ("", None)],
        ids=["ticket-parent", "agent-parent", "no-parent"],
    )
    def test_ticket_comes_from_the_parent(self, start_env, parent, ticket):
        result = self._invoke("-p", "do it", "-n", "add-oauth", "--parent", parent)
        assert result.exit_code == 0, result.output
        assert start_env.allocate.call_args.args == (ticket, "add-oauth")

    def test_deliverable_outside_the_tree_refuses(self, start_env):
        start_env.deliverable_free.side_effect = RuntimeError("deliverable bookmark 'x' already exists")
        result = self._invoke("-p", "do it", "-n", "add-oauth")
        assert result.exit_code == 1
        assert "already exists" in result.output
        assert not start_env.ensure_image.called
        assert not start_env.run.called
        bookmark, anchor = start_env.deliverable_free.call_args.args[1:]
        assert (bookmark, anchor) == ("a._host.add-oauth.mr", start_env.anchor_hash)

    def test_peers_and_parent_reach_the_spec(self, start_env):
        result = self._invoke(
            "-p", "do it", "-n", "add-oauth", "--peer", "a._host.api-login:3",
            "--peer", "a._host.web-ui", "--parent", "cld_master_myrepo_ab12ef34",
        )
        assert result.exit_code == 0, result.output
        spec = start_env.bca.call_args.kwargs["task_agent"]
        assert spec.peers == {"a._host.api-login": 3, "a._host.web-ui": 10}
        assert spec.parent_master == "cld_master_myrepo_ab12ef34"
        start_env.capacity.assert_called_once()
        assert start_env.capacity.call_args.args[1] == "cld_master_myrepo_ab12ef34"

    def test_store_preflight_runs_before_the_anchor_is_resolved(self, start_env):
        order = []
        start_env.preflight.side_effect = lambda repo: order.append(("preflight", repo))
        start_env.resolve_anchor.side_effect = lambda *a, **k: order.append("resolve") or start_env.anchor_hash
        result = self._invoke("-p", "do it", "-n", "add-oauth")
        assert result.exit_code == 0, result.output
        assert order == [("preflight", start_env.repo_root), "resolve"]

    def test_git_repo_is_refused_up_front(self, start_env):
        start_env.get_backend.return_value = SimpleNamespace(name="git")
        result = self._invoke("-p", "do it", "-n", "add-oauth")
        assert result.exit_code == 1
        assert "task-agents need a jj repo" in result.output
        assert "is a git repo" in result.output
        assert not start_env.resolve_anchor.called
        assert not start_env.ensure_image.called
        assert not start_env.run.called

    def test_persona_recorded_for_display(self, start_env):
        """The first persona-kind ref names the role; nothing is mounted for it."""
        result = self._invoke("-p", "do it", "-n", "add-oauth")
        assert result.exit_code == 0, result.output
        argv = start_env.run.call_args.args[0]
        assert "AGENT_PERSONA=implementer" in argv
        assert not any("/config/persona.md" in a for a in argv)

    def test_unknown_persona_errors_before_spawning(self, start_env):
        result = runner.invoke(
            app, ["task-agent", "start", "@no-such-persona-zz9", "-p", "x", "-n", "s"]
        )
        assert result.exit_code == 1
        assert "not found" in result.output
        assert not start_env.run.called

    def test_refs_compose_into_the_brief(self, start_env, tmp_path):
        task = tmp_path / "task.md"
        task.write_text("# do the thing\n")
        result = self._invoke(str(task), "-p", "also this", "-n", "add-oauth", "-m", "opus")
        assert result.exit_code == 0, result.output
        argv = start_env.run.call_args.args[0]
        assert not any("/config/task.md" in a for a in argv)
        assert not any("AGENT_INLINE_PROMPT" in a for a in argv)
        assert "AGENT_MODEL=opus" in argv
        assert argv[:3] == ["docker", "run", "-d"]
        assert start_env.stage_ssh.called
        # persona ref, then the local task file, then -p -- in argument order
        brief = start_env.anchor_env.call_args.kwargs["brief"]
        assert brief == "# impl\n\n# do the thing\n\nalso this\n"

    def test_missing_task_file_errors(self, start_env, tmp_path):
        result = self._invoke(str(tmp_path / "nope.md"), "-n", "s")
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_launch_pins_the_resolved_anchor(self, start_env):
        result = self._invoke("-p", "do it", "-n", "add-oauth", "-r", "some-revset")
        assert result.exit_code == 0, result.output
        assert start_env.resolve_anchor.call_args.args[2] == "some-revset"
        # The already-resolved hash, not the raw -r: the launch must pin exactly
        # the commit the live-stack refusal inspected.
        assert start_env.anchor_env.call_args.args[1] == start_env.anchor_hash

    def test_capacity_refusal_skips_image_build_and_run(self, start_env):
        start_env.capacity.side_effect = RuntimeError("task-agent cap reached for X: 4/4")
        result = self._invoke("-p", "do it", "-n", "add-oauth")
        assert result.exit_code == 1
        assert not start_env.ensure_image.called
        assert not start_env.run.called

    def test_anchor_refusal_skips_run(self, start_env):
        start_env.resolve_anchor.side_effect = RuntimeError("refusing to anchor on abc123")
        result = self._invoke("-p", "do it", "-n", "add-oauth")
        assert result.exit_code == 1
        assert not start_env.run.called

    def test_unknown_peer_warns_but_spawns(self, start_env):
        # The warning reaches the caller on stderr; the command's own setup_logging
        # detaches the cld logger from caplog, so assert on the output.
        result = self._invoke("-p", "do it", "-n", "add-oauth", "--peer", "a._host.typo")
        assert result.exit_code == 0, result.output
        assert start_env.run.called
        assert "not known to this host" in result.output
        assert "a._host.typo" in result.output

    def test_known_peer_does_not_warn(self, start_env):
        start_env.task_agent_list.return_value = [_ta("a._host.api-login")]
        result = self._invoke("-p", "do it", "-n", "add-oauth", "--peer", "a._host.api-login")
        assert result.exit_code == 0, result.output
        assert "not known to this host" not in result.output

    def test_self_peer_errors(self, start_env):
        result = self._invoke("-p", "do it", "-n", "add-oauth", "--peer", "a._host.add-oauth")
        assert result.exit_code == 1
        assert "names this agent itself" in result.output
        assert not start_env.run.called

    def test_invalid_slug_errors_before_the_refusals(self, start_env):
        result = self._invoke("-p", "do it", "-n", "Add OAuth")
        assert result.exit_code == 1
        assert "invalid task slug" in result.output
        assert not start_env.capacity.called
        assert not start_env.ensure_image.called
        assert not start_env.run.called

    def test_collision_suffix_reaches_the_hints(self, start_env):
        """A suffixed name means the slug is no longer the handle -- the hints must follow."""
        start_env.allocate.side_effect = lambda t, s: f"a._host.{s}-2"
        result = self._invoke("-p", "do it", "-n", "add-oauth")
        assert result.exit_code == 0, result.output
        assert "a._host.add-oauth-2" in result.output
        assert "cld task-agent status add-oauth-2" in result.output
        assert "cld task-agent transcript add-oauth-2" in result.output

    def test_readiness_timeout_leaves_container_and_exits(self, start_env):
        start_env.ready.return_value = False
        result = self._invoke("-p", "do it", "-n", "add-oauth")
        assert result.exit_code == 1
        assert "did not become ready" in result.output
        assert "cld task-agent logs" in result.output
        argv_calls = [c.args[0] for c in start_env.run.call_args_list]
        assert not any("rm" in c for c in argv_calls)


class TestResolveTaskAgent:
    def _cfg(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLD_MAILBOX_ROOT", str(tmp_path / "mailboxes"))
        return Config.from_env()

    def test_full_identifier(self, tmp_path, monkeypatch):
        cfg = self._cfg(tmp_path, monkeypatch)
        with patch("cld.task_agent.docker_task_agent_list", return_value=[_ta("a.lide-1.add-oauth")]):
            assert resolve_task_agent(cfg, "a.lide-1.add-oauth") == "a.lide-1.add-oauth"

    def test_bare_slug(self, tmp_path, monkeypatch):
        cfg = self._cfg(tmp_path, monkeypatch)
        with patch("cld.task_agent.docker_task_agent_list", return_value=[_ta("a.lide-1.add-oauth")]):
            assert resolve_task_agent(cfg, "add-oauth") == "a.lide-1.add-oauth"

    def test_bare_slug_of_a_host_agent(self, tmp_path, monkeypatch):
        cfg = self._cfg(tmp_path, monkeypatch)
        with patch("cld.task_agent.docker_task_agent_list", return_value=[_ta("a._host.add-oauth")]):
            assert resolve_task_agent(cfg, "add-oauth") == "a._host.add-oauth"

    def test_slug_matches_only_the_last_part(self, tmp_path, monkeypatch):
        cfg = self._cfg(tmp_path, monkeypatch)
        with patch("cld.task_agent.docker_task_agent_list", return_value=[_ta("a.fix.review")]):
            with pytest.raises(RuntimeError, match="neither live nor archived"):
                resolve_task_agent(cfg, "fix")

    def test_mailbox_only_agent_resolves(self, tmp_path, monkeypatch):
        cfg = self._cfg(tmp_path, monkeypatch)
        _write_mailbox(tmp_path / "mailboxes", "a.lide-1.crashed", meta=_meta())
        with patch("cld.task_agent.docker_task_agent_list", return_value=[]):
            assert resolve_task_agent(cfg, "crashed") == "a.lide-1.crashed"

    def test_ambiguous_slug_lists_candidates(self, tmp_path, monkeypatch):
        cfg = self._cfg(tmp_path, monkeypatch)
        agents = [_ta("a.lide-1.fix"), _ta("a.lide-2.fix")]
        with patch("cld.task_agent.docker_task_agent_list", return_value=agents):
            with pytest.raises(RuntimeError, match="ambiguous.*a.lide-1.fix, a.lide-2.fix"):
                resolve_task_agent(cfg, "fix")

    def test_unknown_name_errors(self, tmp_path, monkeypatch):
        cfg = self._cfg(tmp_path, monkeypatch)
        with patch("cld.task_agent.docker_task_agent_list", return_value=[]):
            with pytest.raises(RuntimeError, match="neither live nor archived"):
                resolve_task_agent(cfg, "ghost")

    def test_archived_full_name_resolves(self, tmp_path, monkeypatch):
        cfg = self._cfg(tmp_path, monkeypatch)
        _write_mailbox(tmp_path / "mailboxes" / "_archive", "a.lide-1.reaped", meta=_meta())
        with patch("cld.task_agent.docker_task_agent_list", return_value=[]):
            assert resolve_task_agent(cfg, "a.lide-1.reaped") == "a.lide-1.reaped"


class TestTaskAgentRoster:
    def test_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLD_MAILBOX_ROOT", str(tmp_path / "mailboxes"))
        with patch("cld.task_agent.docker_task_agent_list", return_value=[]):
            result = runner.invoke(app, ["task-agent", "status"])
        assert result.exit_code == 0, result.output
        assert "No task-agents found." in result.output

    def test_rows_and_gone_footer(self, tmp_path, monkeypatch):
        root = tmp_path / "mailboxes"
        monkeypatch.setenv("CLD_MAILBOX_ROOT", str(root))
        _write_mailbox(root, "a.t1.live", meta=_meta(),
                       state={"phase": "idle", "msg_count": 3, "cost_usd_total": 1.5})
        _write_mailbox(root, "a.t1.orphan", meta=_meta())
        with patch("cld.task_agent.docker_task_agent_list", return_value=[_ta("a.t1.live")]), \
             patch("cld.task_agent.docker_task_agent_status", return_value="running"):
            result = runner.invoke(app, ["task-agent", "status"])
        assert result.exit_code == 0, result.output
        assert "a.t1.live" in result.output
        assert "running" in result.output and "idle" in result.output
        assert "1.5000" in result.output
        assert "gone" in result.output
        assert "a.t1.orphan" in result.output
        assert "1 mailbox(es) with no container" in result.output

    def test_stopped_container_shows_its_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLD_MAILBOX_ROOT", str(tmp_path / "mailboxes"))
        with patch("cld.task_agent.docker_task_agent_list", return_value=[_ta("a.t1.x")]), \
             patch("cld.task_agent.docker_task_agent_status", return_value="stopped"):
            result = runner.invoke(app, ["task-agent", "status"])
        assert result.exit_code == 0, result.output
        assert "stopped" in result.output


class TestTaskAgentDetail:
    def test_live_agent_shows_meta_and_state(self, tmp_path, monkeypatch):
        root = tmp_path / "mailboxes"
        monkeypatch.setenv("CLD_MAILBOX_ROOT", str(root))
        name = "a.t1.add-oauth"
        _write_mailbox(
            root, name,
            meta=_meta(parent="cld_master_myrepo_ab12", peers={"a.t1.api-login": 3}),
            state={"phase": "processing", "msg_count": 2, "cost_usd_total": 0.25,
                   "current": {"subject": "wrap up", "from": "cld_master_myrepo_ab12",
                               "started_at": "2026-08-12T11:00:00Z"}},
        )
        with patch("cld.task_agent.docker_task_agent_list", return_value=[_ta(name)]), \
             patch("cld.task_agent.docker_task_agent_status", return_value="running"):
            result = runner.invoke(app, ["task-agent", "status", "add-oauth"])
        assert result.exit_code == 0, result.output
        for expected in ("wire up oauth login", "implementer", "add-oauth", "aaaaaaaaaaaa",
                         "cld_master_myrepo_ab12", "a.t1.api-login (3 hops)",
                         "processing", "0.2500", "wrap up"):
            assert expected in result.output

    def test_archived_agent_points_at_transcript(self, tmp_path, monkeypatch):
        root = tmp_path / "mailboxes"
        monkeypatch.setenv("CLD_MAILBOX_ROOT", str(root))
        name = "a.t1.reaped"
        _write_mailbox(root / "_archive", name, meta=_meta())
        with patch("cld.task_agent.docker_task_agent_list", return_value=[]), \
             patch("cld.task_agent.docker_task_agent_status", return_value="absent"):
            result = runner.invoke(app, ["task-agent", "status", name])
        assert result.exit_code == 0, result.output
        assert "reaped (archived)" in result.output
        assert "cld task-agent transcript" in result.output

    def test_booting_agent_without_meta(self, tmp_path, monkeypatch):
        root = tmp_path / "mailboxes"
        monkeypatch.setenv("CLD_MAILBOX_ROOT", str(root))
        name = "a.t1.booting"
        _write_mailbox(root, name)
        with patch("cld.task_agent.docker_task_agent_list", return_value=[_ta(name)]), \
             patch("cld.task_agent.docker_task_agent_status", return_value="running"):
            result = runner.invoke(app, ["task-agent", "status", name])
        assert result.exit_code == 0, result.output
        assert "none yet" in result.output
        assert "Supervisor state: unavailable" in result.output


class TestTaskAgentLogsAndTranscript:
    def test_logs_tails_the_container(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLD_MAILBOX_ROOT", str(tmp_path / "mailboxes"))
        fake = MagicMock(stdout="supervisor line\n", stderr="")
        with patch("cld.cli.require_docker"), \
             patch("cld.task_agent.docker_task_agent_list", return_value=[_ta("a.t1.x")]), \
             patch("cld.cli.docker_task_agent_status", return_value="running"), \
             patch("cld.cli.subprocess.run", return_value=fake) as run_mock:
            result = runner.invoke(app, ["task-agent", "logs", "x", "-n", "20"])
        assert result.exit_code == 0, result.output
        assert "supervisor line" in result.output
        argv = run_mock.call_args.args[0]
        assert argv[:2] == ["docker", "logs"]
        assert argv[-1] == "a.t1.x"
        assert "20" in argv

    def test_logs_on_gone_container_points_at_transcript(self, tmp_path, monkeypatch):
        root = tmp_path / "mailboxes"
        monkeypatch.setenv("CLD_MAILBOX_ROOT", str(root))
        _write_mailbox(root, "a.t1.x", meta=_meta())
        with patch("cld.cli.require_docker"), \
             patch("cld.task_agent.docker_task_agent_list", return_value=[]), \
             patch("cld.cli.docker_task_agent_status", return_value="absent"):
            result = runner.invoke(app, ["task-agent", "logs", "a.t1.x"])
        assert result.exit_code == 1
        assert "transcript" in result.output

    def test_transcript_renders_both_directions(self, tmp_path, monkeypatch):
        root = tmp_path / "mailboxes"
        monkeypatch.setenv("CLD_MAILBOX_ROOT", str(root))
        name = "a.t1.x"
        d = _write_mailbox(root, name, meta=_meta())
        (d / "archive" / "m1.json").write_text(json.dumps({
            "id": "m1", "from": "cld_master_myrepo_ab12", "to": name,
            "subject": "kick off", "body": "start with the model", "ts": "2026-08-12T10:00:00Z",
        }))
        (d / "outbox.log").write_text(json.dumps({
            "id": "m2", "to": "cld_master_myrepo_ab12", "subject": "Re: kick off",
            "body": "done, branch pushed", "ts": "2026-08-12T10:05:00Z",
        }) + "\n")
        with patch("cld.task_agent.docker_task_agent_list", return_value=[_ta(name)]):
            result = runner.invoke(app, ["task-agent", "transcript", "x"])
        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        assert "<- cld_master_myrepo_ab12  kick off" in lines[0]
        assert "    start with the model" in lines
        assert any("-> cld_master_myrepo_ab12  Re: kick off" in ln for ln in lines)
        assert "    done, branch pushed" in lines

    def test_transcript_of_empty_mailbox(self, tmp_path, monkeypatch):
        root = tmp_path / "mailboxes"
        monkeypatch.setenv("CLD_MAILBOX_ROOT", str(root))
        _write_mailbox(root, "a.t1.x", meta=_meta())
        with patch("cld.task_agent.docker_task_agent_list", return_value=[]):
            result = runner.invoke(app, ["task-agent", "transcript", "a.t1.x"])
        assert result.exit_code == 0, result.output
        assert "No messages" in result.output


class TestReapReadiness:
    """The three §7 checks. _REAP_WAIT_SECONDS is patched to 0 so check 1 refuses at once."""

    def _cfg(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLD_MAILBOX_ROOT", str(tmp_path / "mailboxes"))
        return Config.from_env()

    def test_idle_agent_passes(self, tmp_path, monkeypatch):
        cfg = self._cfg(tmp_path, monkeypatch)
        _write_mailbox(tmp_path / "mailboxes", "a", meta=_meta(), state={"phase": "idle"})
        with patch("cld.task_agent.docker_task_agent_list", return_value=[]):
            _assert_reap_ready(cfg, "a", parent="")

    def test_no_state_file_passes(self, tmp_path, monkeypatch):
        cfg = self._cfg(tmp_path, monkeypatch)
        _write_mailbox(tmp_path / "mailboxes", "a", meta=_meta())
        with patch("cld.task_agent.docker_task_agent_list", return_value=[]):
            _assert_reap_ready(cfg, "a", parent="")

    def test_mid_turn_refuses_naming_the_message(self, tmp_path, monkeypatch):
        cfg = self._cfg(tmp_path, monkeypatch)
        _write_mailbox(
            tmp_path / "mailboxes", "a", meta=_meta(),
            state={"phase": "processing",
                   "current": {"subject": "wrap up", "from": "master-1", "started_at": "t0"}},
        )
        with patch("cld.task_agent.docker_task_agent_list", return_value=[]), \
             patch("cld.cli._REAP_WAIT_SECONDS", 0):
            with pytest.raises(RuntimeError, match="mid-turn on 'wrap up'"):
                _assert_reap_ready(cfg, "a", parent="")

    def test_live_peer_refuses_naming_dependents(self, tmp_path, monkeypatch):
        cfg = self._cfg(tmp_path, monkeypatch)
        root = tmp_path / "mailboxes"
        _write_mailbox(root, "target", meta=_meta(), state={"phase": "idle"})
        _write_mailbox(root, "dependent", meta=_meta(peers={"target": 5}), state={"phase": "idle"})
        with patch("cld.task_agent.docker_task_agent_list", return_value=[_ta("dependent")]), \
             patch("cld.cli.docker_task_agent_list", return_value=[_ta("dependent")]):
            with pytest.raises(RuntimeError, match="live peer of dependent"):
                _assert_reap_ready(cfg, "target", parent="")

    def test_stopped_peer_does_not_block(self, tmp_path, monkeypatch):
        cfg = self._cfg(tmp_path, monkeypatch)
        root = tmp_path / "mailboxes"
        _write_mailbox(root, "target", meta=_meta(), state={"phase": "idle"})
        _write_mailbox(root, "dependent", meta=_meta(peers={"target": 5}))
        with patch("cld.task_agent.docker_task_agent_list", return_value=[]):
            _assert_reap_ready(cfg, "target", parent="")

    def test_own_peers_never_block_itself(self, tmp_path, monkeypatch):
        cfg = self._cfg(tmp_path, monkeypatch)
        root = tmp_path / "mailboxes"
        _write_mailbox(root, "a", meta=_meta(peers={"a": 5}), state={"phase": "idle"})
        with patch("cld.task_agent.docker_task_agent_list", return_value=[_ta("a")]):
            _assert_reap_ready(cfg, "a", parent="")

    def test_foreign_fleet_refused(self, tmp_path, monkeypatch):
        cfg = self._cfg(tmp_path, monkeypatch)
        _write_mailbox(tmp_path / "mailboxes", "a", meta=_meta(parent="master-1"), state={"phase": "idle"})
        with patch("cld.task_agent.docker_task_agent_list", return_value=[]):
            with pytest.raises(RuntimeError, match="parent master is master-1"):
                _assert_reap_ready(cfg, "a", parent="master-2")

    def test_own_fleet_passes(self, tmp_path, monkeypatch):
        cfg = self._cfg(tmp_path, monkeypatch)
        _write_mailbox(tmp_path / "mailboxes", "a", meta=_meta(parent="master-1"), state={"phase": "idle"})
        with patch("cld.task_agent.docker_task_agent_list", return_value=[]):
            _assert_reap_ready(cfg, "a", parent="master-1")

    def test_container_label_beats_a_lying_meta_json(self, tmp_path, monkeypatch):
        """meta.json is written by the container; the label is host-set, so it wins."""
        cfg = self._cfg(tmp_path, monkeypatch)
        _write_mailbox(tmp_path / "mailboxes", "a", meta=_meta(parent="master-2"), state={"phase": "idle"})
        with patch("cld.task_agent.docker_task_agent_list", return_value=[_ta("a", parent="master-1")]):
            _assert_reap_ready(cfg, "a", parent="master-1")

    def test_human_reaps_any_fleet(self, tmp_path, monkeypatch):
        cfg = self._cfg(tmp_path, monkeypatch)
        _write_mailbox(tmp_path / "mailboxes", "a", meta=_meta(parent="master-1"), state={"phase": "idle"})
        with patch("cld.task_agent.docker_task_agent_list", return_value=[]):
            _assert_reap_ready(cfg, "a", parent="")


class TestReapTaskAgent:
    def _cfg(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLD_MAILBOX_ROOT", str(tmp_path / "mailboxes"))
        return Config.from_env()

    def test_stops_forgets_and_archives(self, tmp_path, monkeypatch):
        cfg = self._cfg(tmp_path, monkeypatch)
        root = tmp_path / "mailboxes"
        name = "a.t1.add-oauth"
        _write_mailbox(root, name, meta=_meta(), state={"phase": "idle"})
        with patch("cld.task_agent.docker_task_agent_list", return_value=[_ta(name)]), \
             patch("cld.cli._stop_and_remove_container") as stop, \
             patch("cld.cli._forget_session_state") as forget:
            _reap_task_agent(cfg, name, parent="", force=False)
        stop.assert_called_once_with(name)
        forget.assert_called_once_with("/host/myrepo", name)
        # The session bookmark, never the deliverable branch (D8): same call, and the
        # two are different strings even when the slug and the branch match.
        assert forget.call_args.args[1] != _meta()["deliverable_branch"]
        assert not (root / name).exists()
        assert (root / "_archive" / name / "meta.json").is_file()

    def test_idempotent_on_an_already_reaped_agent(self, tmp_path, monkeypatch):
        cfg = self._cfg(tmp_path, monkeypatch)
        root = tmp_path / "mailboxes"
        name = "a.t1.x"
        _write_mailbox(root / "_archive", name, meta=_meta())
        with patch("cld.task_agent.docker_task_agent_list", return_value=[]), \
             patch("cld.task_agent.find_target_repo", return_value=Path("/host/myrepo")), \
             patch("cld.cli._stop_and_remove_container"), \
             patch("cld.cli._forget_session_state"):
            _reap_task_agent(cfg, name, parent="", force=False)
        assert (root / "_archive" / name / "meta.json").is_file()

    def test_unknown_repo_skips_forget_with_a_warning(self, tmp_path, monkeypatch, caplog):
        cfg = self._cfg(tmp_path, monkeypatch)
        _write_mailbox(tmp_path / "mailboxes", "a.t1.x", meta=_meta())
        with patch("cld.task_agent.docker_task_agent_list", return_value=[]), \
             patch("cld.cli.find_target_repo", side_effect=RuntimeError("not a repo")), \
             patch("cld.cli._stop_and_remove_container"), \
             patch("cld.cli._forget_session_state") as forget, \
             caplog.at_level("WARNING"):
            _reap_task_agent(cfg, "a.t1.x", parent="", force=False)
        assert not forget.called
        assert "jj bookmark forget" in caplog.text

    def test_force_bypasses_a_refusal(self, tmp_path, monkeypatch):
        cfg = self._cfg(tmp_path, monkeypatch)
        root = tmp_path / "mailboxes"
        _write_mailbox(root, "target", meta=_meta(), state={"phase": "processing", "current": {}})
        _write_mailbox(root, "dependent", meta=_meta(peers={"target": 5}), state={"phase": "idle"})
        with patch("cld.task_agent.docker_task_agent_list", return_value=[_ta("dependent"), _ta("target")]), \
             patch("cld.cli._stop_and_remove_container") as stop, \
             patch("cld.cli._forget_session_state"):
            _reap_task_agent(cfg, "target", parent="", force=True)
        stop.assert_called_once_with("target")


class TestTaskAgentShutdownCommand:
    def test_name_and_all_together_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLD_MAILBOX_ROOT", str(tmp_path / "mailboxes"))
        with patch("cld.cli.require_docker"):
            result = runner.invoke(app, ["task-agent", "shutdown", "x", "--all"])
        assert result.exit_code == 1
        assert "not both" in result.output

    def test_neither_name_nor_all_errors(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLD_MAILBOX_ROOT", str(tmp_path / "mailboxes"))
        with patch("cld.cli.require_docker"):
            result = runner.invoke(app, ["task-agent", "shutdown"])
        assert result.exit_code == 1
        assert "or --all" in result.output

    def test_single_reap_reports(self, tmp_path, monkeypatch):
        root = tmp_path / "mailboxes"
        monkeypatch.setenv("CLD_MAILBOX_ROOT", str(root))
        name = "a.t1.add-oauth"
        _write_mailbox(root, name, meta=_meta(), state={"phase": "idle"})
        with patch("cld.cli.require_docker"), \
             patch("cld.task_agent.docker_task_agent_list", return_value=[_ta(name)]), \
             patch("cld.cli._stop_and_remove_container"), \
             patch("cld.cli._forget_session_state"):
            result = runner.invoke(app, ["task-agent", "shutdown", "add-oauth"])
        assert result.exit_code == 0, result.output
        assert f"Reaped task-agent: {name}" in result.output

    def test_mid_turn_refusal_exits_1(self, tmp_path, monkeypatch):
        root = tmp_path / "mailboxes"
        monkeypatch.setenv("CLD_MAILBOX_ROOT", str(root))
        name = "a.t1.x"
        _write_mailbox(root, name, meta=_meta(),
                       state={"phase": "processing", "current": {"subject": "s", "from": "m"}})
        with patch("cld.cli.require_docker"), \
             patch("cld.task_agent.docker_task_agent_list", return_value=[_ta(name)]), \
             patch("cld.cli._REAP_WAIT_SECONDS", 0), \
             patch("cld.cli._stop_and_remove_container") as stop:
            result = runner.invoke(app, ["task-agent", "shutdown", "x"])
        assert result.exit_code == 1
        assert not stop.called

    def test_all_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLD_MAILBOX_ROOT", str(tmp_path / "mailboxes"))
        with patch("cld.cli.require_docker"), \
             patch("cld.task_agent.docker_task_agent_list", return_value=[]):
            result = runner.invoke(app, ["task-agent", "shutdown", "--all"])
        assert result.exit_code == 0, result.output
        assert "No task-agents found." in result.output

    def test_all_reaps_a_peer_edge_in_one_pass(self, tmp_path, monkeypatch):
        """A declares B as a peer, so B is only reapable once A is gone (§7 check 2)."""
        root = tmp_path / "mailboxes"
        monkeypatch.setenv("CLD_MAILBOX_ROOT", str(root))
        _write_mailbox(root, "a.t1.a", meta=_meta(peers={"a.t1.b": 5}),
                       state={"phase": "idle"})
        _write_mailbox(root, "a.t1.b", meta=_meta(), state={"phase": "idle"})
        alive = {"a.t1.a", "a.t1.b"}

        def listing(*, running_only=False):
            return [_ta(n) for n in sorted(alive)]

        def stop(name):
            alive.discard(name)

        with patch("cld.cli.require_docker"), \
             patch("cld.task_agent.docker_task_agent_list", side_effect=listing), \
             patch("cld.cli._stop_and_remove_container", side_effect=stop), \
             patch("cld.cli._forget_session_state"):
            result = runner.invoke(app, ["task-agent", "shutdown", "--all"])
        assert result.exit_code == 0, result.output
        assert alive == set()
        assert (root / "_archive" / "a.t1.a").is_dir()
        assert (root / "_archive" / "a.t1.b").is_dir()

    def test_all_refuses_a_mutual_edge_and_exits_1(self, tmp_path, monkeypatch):
        root = tmp_path / "mailboxes"
        monkeypatch.setenv("CLD_MAILBOX_ROOT", str(root))
        _write_mailbox(root, "a.t1.a", meta=_meta(peers={"a.t1.b": 5}),
                       state={"phase": "idle"})
        _write_mailbox(root, "a.t1.b", meta=_meta(peers={"a.t1.a": 5}),
                       state={"phase": "idle"})
        agents = [_ta("a.t1.a"), _ta("a.t1.b")]
        with patch("cld.cli.require_docker"), \
             patch("cld.task_agent.docker_task_agent_list", return_value=agents), \
             patch("cld.cli.docker_task_agent_list", return_value=agents), \
             patch("cld.cli._stop_and_remove_container") as stop, \
             patch("cld.cli._forget_session_state"):
            result = runner.invoke(app, ["task-agent", "shutdown", "--all"])
        assert result.exit_code == 1
        assert not stop.called
        assert "add --force to override" in result.output

    def test_all_force_reaps_everything(self, tmp_path, monkeypatch):
        root = tmp_path / "mailboxes"
        monkeypatch.setenv("CLD_MAILBOX_ROOT", str(root))
        _write_mailbox(root, "a.t1.a", meta=_meta(peers={"a.t1.b": 5}),
                       state={"phase": "processing", "current": {}})
        _write_mailbox(root, "a.t1.b", meta=_meta(peers={"a.t1.a": 5}),
                       state={"phase": "idle"})
        agents = [_ta("a.t1.a"), _ta("a.t1.b")]
        with patch("cld.cli.require_docker"), \
             patch("cld.task_agent.docker_task_agent_list", return_value=agents), \
             patch("cld.cli._stop_and_remove_container") as stop, \
             patch("cld.cli._forget_session_state"):
            result = runner.invoke(app, ["task-agent", "shutdown", "--all", "--force"])
        assert result.exit_code == 0, result.output
        assert stop.call_count == 2

    def test_all_scoped_to_a_parent(self, tmp_path, monkeypatch):
        root = tmp_path / "mailboxes"
        monkeypatch.setenv("CLD_MAILBOX_ROOT", str(root))
        _write_mailbox(root, "a.t1.mine", meta=_meta(parent="master-1"), state={"phase": "idle"})
        _write_mailbox(root, "a.t1.theirs", meta=_meta(parent="master-2"), state={"phase": "idle"})
        agents = [_ta("a.t1.mine", parent="master-1"),
                  _ta("a.t1.theirs", parent="master-2")]
        with patch("cld.cli.require_docker"), \
             patch("cld.task_agent.docker_task_agent_list", return_value=agents), \
             patch("cld.cli._stop_and_remove_container") as stop, \
             patch("cld.cli._forget_session_state"):
            result = runner.invoke(app, ["task-agent", "shutdown", "--all", "--parent", "master-1"])
        assert result.exit_code == 0, result.output
        stop.assert_called_once_with("a.t1.mine")


class TestHandleErrorsExitCodes:
    """typer.Exit subclasses RuntimeError, so _handle_errors has to let it through."""

    def test_clean_user_error_has_no_command_failed_noise(self):
        result = runner.invoke(app, ["run"])
        assert result.exit_code == 1
        assert "at least one prompt ref" in result.output
        assert "Command failed" not in result.output


class TestTaskAgentRosterScoping:
    def test_parent_scopes_the_roster(self, tmp_path, monkeypatch):
        root = tmp_path / "mailboxes"
        monkeypatch.setenv("CLD_MAILBOX_ROOT", str(root))
        _write_mailbox(root, "a.t1.mine", meta=_meta(parent="master-1"))
        _write_mailbox(root, "a.t1.theirs", meta=_meta(parent="master-2"))
        agents = [_ta("a.t1.mine", parent="master-1"),
                  _ta("a.t1.theirs", parent="master-2")]
        with patch("cld.task_agent.docker_task_agent_list", return_value=agents), \
             patch("cld.task_agent.docker_task_agent_status", return_value="running"):
            result = runner.invoke(app, ["task-agent", "status", "--parent", "master-1"])
        assert result.exit_code == 0, result.output
        assert "a.t1.mine" in result.output
        assert "a.t1.theirs" not in result.output


class TestChainCancel:
    @pytest.fixture
    def chain_dir(self, tmp_path):
        d = tmp_path / ".cld" / "chains" / "chain.demo"
        d.mkdir(parents=True)
        return d

    def _seed(self, chain_dir, status="running", pid=None, sessions=("chain.demo.s1",)):
        _seed_state(chain_dir, "demo", status=status, pid=os.getpid() if pid is None else pid)
        writer = StateWriter(chain_dir / "state.json", ChainState.load(chain_dir / "state.json"))
        writer.update(current_step_sessions=list(sessions))

    def _cancel(self, tmp_path, name="demo"):
        with patch("cld.cli.find_repo_root", return_value=tmp_path):
            return runner.invoke(app, ["chain", "cancel", name])

    def test_signals_the_runner_group_and_stops_step_containers(self, tmp_path, chain_dir):
        self._seed(chain_dir, sessions=("chain.demo.s1", "chain.demo.s2"))

        def runner_records_interrupt(pid, sig):
            StateWriter(chain_dir / "state.json", ChainState.load(chain_dir / "state.json")) \
                .mark_finished("interrupted", reason="SIGINT")

        with patch("cld.cli.os.killpg", side_effect=runner_records_interrupt) as killpg, \
             patch("cld.cli.subprocess.run") as run:
            result = self._cancel(tmp_path)
        assert result.exit_code == 0, result.output
        killpg.assert_called_once_with(os.getpid(), signal.SIGINT)
        assert run.call_args.args[0] == ["docker", "stop", "chain.demo.s1", "chain.demo.s2"]
        # The runner's own record is kept, not overwritten by the fallback.
        assert ChainState.load(chain_dir / "state.json").failure_reason == "SIGINT"

    def test_marks_interrupted_itself_when_the_runner_does_not(self, tmp_path, chain_dir):
        self._seed(chain_dir)
        with patch("cld.cli.os.killpg"), patch("cld.cli.subprocess.run"), \
             patch("cld.cli._CANCEL_WAIT_SECONDS", 0.3):
            result = self._cancel(tmp_path)
        assert result.exit_code == 0, result.output
        state = ChainState.load(chain_dir / "state.json")
        assert state.status == "interrupted"
        assert state.failure_reason == "cancelled"
        assert state.finished_at

    def test_dead_runner_is_not_signalled_but_still_cleaned_up(self, tmp_path, chain_dir):
        self._seed(chain_dir, pid=999999)
        with patch("cld.cli.os.killpg") as killpg, patch("cld.cli.subprocess.run") as run, \
             patch("cld.cli._CANCEL_WAIT_SECONDS", 0.3):
            result = self._cancel(tmp_path)
        assert result.exit_code == 0, result.output
        killpg.assert_not_called()
        assert run.call_args.args[0] == ["docker", "stop", "chain.demo.s1"]
        assert ChainState.load(chain_dir / "state.json").status == "interrupted"

    def test_no_step_sessions_runs_no_docker(self, tmp_path, chain_dir):
        self._seed(chain_dir, sessions=())
        with patch("cld.cli.os.killpg"), patch("cld.cli.subprocess.run") as run, \
             patch("cld.cli._CANCEL_WAIT_SECONDS", 0.3):
            result = self._cancel(tmp_path)
        assert result.exit_code == 0, result.output
        run.assert_not_called()

    @pytest.mark.parametrize("status", ["success", "failed", "interrupted"])
    def test_refuses_a_chain_that_is_not_running(self, tmp_path, chain_dir, status):
        self._seed(chain_dir, status=status)
        with patch("cld.cli.os.killpg") as killpg, patch("cld.cli.subprocess.run") as run:
            result = self._cancel(tmp_path)
        assert result.exit_code == 1
        assert f"not running (status: {status})" in result.output
        killpg.assert_not_called()
        run.assert_not_called()
        assert ChainState.load(chain_dir / "state.json").status == status

    def test_unknown_chain_errors(self, tmp_path):
        result = self._cancel(tmp_path, name="nope")
        assert result.exit_code == 1
        assert "no chain named 'nope'" in result.output


class TestResolveChainPath:
    @pytest.fixture
    def repo(self, tmp_path):
        (tmp_path / "chains").mkdir()
        return tmp_path

    def test_name_resolves_under_the_chains_folder(self, repo):
        chain = repo / "chains" / "demo.yaml"
        chain.write_text("name: demo\n")
        with patch("cld.cli.find_repo_root", return_value=repo):
            assert _resolve_chain_path("@demo") == chain

    def test_dotdot_name_is_refused(self, repo):
        (repo / "outside.yaml").write_text("name: outside\n")
        with patch("cld.cli.find_repo_root", return_value=repo), \
             pytest.raises(ValueError, match="escapes the chains folder"):
            _resolve_chain_path("@../outside")

    def test_symlink_out_of_the_chains_folder_is_refused(self, repo, tmp_path_factory):
        target = tmp_path_factory.mktemp("elsewhere") / "secret.yaml"
        target.write_text("name: secret\n")
        (repo / "chains" / "link.yaml").symlink_to(target)
        with patch("cld.cli.find_repo_root", return_value=repo), \
             pytest.raises(ValueError, match="escapes the chains folder"):
            _resolve_chain_path("@link")

    def test_cli_reports_the_escape(self, repo, caplog):
        with patch("cld.cli.find_repo_root", return_value=repo):
            result = runner.invoke(app, ["chain", "validate", "@../outside"])
        assert result.exit_code == 1
        assert "escapes the chains folder" in caplog.text

    def test_plain_path_is_untouched(self, repo):
        assert _resolve_chain_path("some/dir/c.yaml") == Path("some/dir/c.yaml")
