"""Tests for cld.messenger.agent_loop: the headless agent supervisor state machine.

Drives AgentSupervisor bare-metal (no Docker) against a stub `claude` binary
and a temp mailbox root, per the design doc's suggested manual-test approach.
"""

import json
import subprocess
from pathlib import Path

import pytest

from cld.messenger import mailbox
from cld.messenger.agent_loop import AgentSupervisor, TaskMode, _extract_cost, compose_kickoff

_STUB_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "stub-messenger-agent" / "claude"


class TestExtractCost:
    def test_prefers_total_cost_usd(self):
        assert _extract_cost({"total_cost_usd": 1.5, "cost_usd": 9.9}) == 1.5

    def test_falls_back_to_cost_usd(self):
        assert _extract_cost({"cost_usd": 0.5}) == 0.5

    def test_missing_returns_zero(self):
        assert _extract_cost({}) == 0.0

    def test_non_numeric_falls_through(self):
        assert _extract_cost({"total_cost_usd": "n/a", "cost_usd": 0.25}) == 0.25


@pytest.fixture
def persona_path(tmp_path):
    p = tmp_path / "repo-agent.md"
    p.write_text("Repo: ${REPO_BASENAME} at ${REPO_ABS_PATH}, turns=${MAX_TURNS}, container=${CONTAINER_NAME}\n")
    return p


@pytest.fixture
def supervisor(tmp_path, persona_path, monkeypatch):
    mailbox_root = tmp_path / "mailboxes"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    argv_log = tmp_path / "argv.log"
    prompt_log = tmp_path / "prompt.log"
    monkeypatch.setenv("STUB_ARGV_LOG", str(argv_log))
    monkeypatch.setenv("STUB_PROMPT_LOG", str(prompt_log))
    monkeypatch.setenv("STUB_MAILBOX_ROOT", str(mailbox_root))
    monkeypatch.setenv("STUB_SESSION_NAME", "a.t1.repoA")
    sup = AgentSupervisor(
        session_name="a.t1.repoA",
        repo_root=repo_root,
        mailbox_root=mailbox_root,
        persona_path=persona_path,
        claude_bin=str(_STUB_CLAUDE),
        max_turns=7,
    )
    sup._argv_log = argv_log
    sup._prompt_log = prompt_log
    return sup


def _read_argv_log(path: Path) -> list[list[str]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _read_prompt_log(path: Path) -> list[str]:
    """Prompts the stub received on stdin -- the real claude takes them there, not in argv."""
    return [json.loads(line) for line in path.read_text().splitlines()]


class TestKickoff:
    def test_sets_session_id_and_cost(self, supervisor, monkeypatch):
        monkeypatch.setenv("STUB_COST", "0.25")
        supervisor.kickoff()
        assert supervisor.session_id == "kickoff-session-id"
        assert supervisor.cost_usd_total == pytest.approx(0.25)

    def test_writes_idle_state(self, supervisor):
        supervisor.kickoff()
        state = json.loads(supervisor.state_path.read_text())
        assert state["phase"] == "idle"
        assert state["session_id"] == "kickoff-session-id"
        assert state["msg_count"] == 0
        assert state["current"] is None

    def test_no_resume_flag_on_first_call(self, supervisor):
        supervisor.kickoff()
        argv = _read_argv_log(supervisor._argv_log)
        assert "--resume" not in argv[0]

    def test_persona_substituted_into_prompt(self, supervisor):
        # The prompt goes over stdin (a persona's leading `---` frontmatter used to
        # be parsed as an unknown option when it was an argv token), so assert
        # against what the stub read from stdin.
        supervisor.kickoff()
        prompt = _read_prompt_log(supervisor._prompt_log)[0]
        assert "repo" in prompt  # REPO_BASENAME
        assert "turns=7" in prompt
        assert "container=a.t1.repoA" in prompt

    def test_frontmatter_stripped_from_persona(self, supervisor, persona_path):
        persona_path.write_text("---\ndescription: meta\n---\n\n# Role\n\nbody\n")
        supervisor.kickoff()
        assert _read_prompt_log(supervisor._prompt_log)[0].startswith("# Role")

    def test_max_turns_flag_passed(self, supervisor):
        supervisor.kickoff()
        argv = _read_argv_log(supervisor._argv_log)[0]
        assert argv[argv.index("--max-turns") + 1] == "7"


class TestSpentEdgeStopsTheFallback:
    """The reply guarantee is bounded by the edge budget (D29) -- with no code here.

    Both of the supervisor's own writes go through `mailbox.write_message`, so a spent
    edge refuses them like anything else. Without that, the guarantee is a loop engine:
    two agents would trade supervisor-authored fallbacks forever, one Claude turn each.
    """

    _SPAWN = {
        "parent": "cld_master_repoA_abcd1234",
        "task": "t",
        "persona": "implementer",
        "deliverable_branch": "b",
        "anchor": "abc",
        "peers": {},
    }

    _PEER = "a.repoA.peer"

    def _exchange(self, supervisor, limit):
        """Run the real flow up to an inbound peer message: we open the edge, the peer replies.

        Both hops go through `gated_send`, which is what seeds the edge's limit -- the
        same reason a supervisor write never has to supply one.
        """
        root = supervisor.mailbox_root
        mailbox.ensure_meta(root, supervisor.session_name, **{**self._SPAWN, "peers": {self._PEER: limit}})
        mailbox.ensure_meta(root, self._PEER, **self._SPAWN)
        mailbox.gated_send(root, supervisor.session_name, self._PEER, "over to you", "b", default_limit=99, ask_limit=3)
        # expects_reply is what puts a reply on the hook at all, so the peer's inbound
        # message has to ask for one for there to be a fallback to refuse.
        inbound = mailbox.gated_send(
            root, self._PEER, supervisor.session_name, "back to you", "b",
            default_limit=99, ask_limit=3, expects_reply=True,
        )
        return inbound["id"]

    def test_fallback_refused_once_the_edge_is_spent(self, supervisor, monkeypatch):
        monkeypatch.setenv("STUB_SEND_REPLY", "0")          # claude replies to nobody
        msg_id = self._exchange(supervisor, limit=2)        # two hops spend it
        root = supervisor.mailbox_root
        assert mailbox.edge_spent(root, supervisor.session_name, self._PEER)
        supervisor.kickoff()
        supervisor.process_one(msg_id)
        # Nothing new: the peer still holds only our opening message, and our outbox
        # still holds only the line for it. This is the loop that used to be infinite.
        assert [m["subject"] for m in mailbox.list_inbox(root, self._PEER)] == ["over to you"]
        assert mailbox.outbox_snapshot(root, supervisor.session_name) == 1
        assert json.loads(supervisor.state_path.read_text())["phase"] == "idle"

    def test_fallback_still_lands_while_the_edge_is_open(self, supervisor, monkeypatch):
        monkeypatch.setenv("STUB_SEND_REPLY", "0")
        msg_id = self._exchange(supervisor, limit=5)
        root = supervisor.mailbox_root
        supervisor.kickoff()
        supervisor.process_one(msg_id)
        assert [m["subject"] for m in mailbox.list_inbox(root, self._PEER)] == [
            "over to you", "Re: back to you",
        ]
        # A fallback is a real delivery, so it consumed a hop like any other message.
        assert mailbox.read_edge(root, supervisor.session_name, self._PEER)["count"] == 3


class TestProcessOne:
    def test_reply_sent_no_fallback(self, supervisor, monkeypatch):
        supervisor.kickoff()
        msg = mailbox.write_message(supervisor.mailbox_root, "sender", supervisor.session_name, "hi", "please help")

        monkeypatch.setenv("STUB_SEND_REPLY", "1")
        monkeypatch.setenv("STUB_REPLY_TO", "sender")
        supervisor.process_one(msg["id"])

        sender_inbox = mailbox.list_inbox(supervisor.mailbox_root, "sender")
        assert len(sender_inbox) == 1
        assert sender_inbox[0]["subject"] == "Re: test"

        # Original message archived, not left unread
        assert mailbox.list_inbox(supervisor.mailbox_root, supervisor.session_name) == []
        assert supervisor.msg_count == 1

    def test_uses_resume_flag(self, supervisor):
        supervisor.kickoff()
        msg = mailbox.write_message(supervisor.mailbox_root, "sender", supervisor.session_name, "hi", "body")
        supervisor.process_one(msg["id"])
        argv = _read_argv_log(supervisor._argv_log)[1]
        assert argv[argv.index("--resume") + 1] == "kickoff-session-id"

    def test_no_reply_synthesizes_fallback(self, supervisor, monkeypatch):
        supervisor.kickoff()
        msg = mailbox.write_message(
            supervisor.mailbox_root, "sender", supervisor.session_name, "hi", "body",
            expects_reply=True,
        )

        monkeypatch.setenv("STUB_RESULT_TEXT", "I looked into it but did nothing")
        supervisor.process_one(msg["id"])

        sender_inbox = mailbox.list_inbox(supervisor.mailbox_root, "sender")
        assert len(sender_inbox) == 1
        full = mailbox.read_message(supervisor.mailbox_root, "sender", sender_inbox[0]["id"])
        assert "no reply produced" in full["body"]
        assert "I looked into it but did nothing" in full["body"]
        assert full["answers"] == msg["id"]

    def test_no_fallback_when_the_message_asked_for_nothing(self, supervisor, monkeypatch):
        """The politeness loop's engine: a fallback for *every* arrival makes an
        acknowledgment oblige another acknowledgment, and it overrides Claude
        correctly deciding it had nothing to add."""
        supervisor.kickoff()
        msg = mailbox.write_message(
            supervisor.mailbox_root, "sender", supervisor.session_name, "fyi", "body",
        )

        monkeypatch.setenv("STUB_SEND_REPLY", "0")
        supervisor.process_one(msg["id"])

        assert mailbox.list_inbox(supervisor.mailbox_root, "sender") == []
        assert supervisor.msg_count == 1

    def test_prompt_states_the_obligation(self, supervisor):
        supervisor.kickoff()
        asked = mailbox.write_message(
            supervisor.mailbox_root, "sender", supervisor.session_name, "q", "?", expects_reply=True,
        )
        supervisor.process_one(asked["id"])
        told = mailbox.write_message(
            supervisor.mailbox_root, "sender", supervisor.session_name, "fyi", "x",
        )
        supervisor.process_one(told["id"])
        prompts = _read_prompt_log(supervisor._prompt_log)
        assert f'answers="{asked["id"]}"' in prompts[1]
        assert "expects no reply" in prompts[2]

    def test_send_to_peer_still_synthesizes_reply_to_sender(self, supervisor, monkeypatch):
        # The reply guarantee is recipient-scoped: answering a peer mid-turn must
        # not discharge the reply owed to whoever sent the message being processed.
        supervisor.kickoff()
        msg = mailbox.write_message(
            supervisor.mailbox_root, "sender", supervisor.session_name, "hi", "body",
            expects_reply=True,
        )

        monkeypatch.setenv("STUB_SEND_REPLY", "1")
        monkeypatch.setenv("STUB_REPLY_TO", "peer")
        supervisor.process_one(msg["id"])

        assert len(mailbox.list_inbox(supervisor.mailbox_root, "peer")) == 1
        sender_inbox = mailbox.list_inbox(supervisor.mailbox_root, "sender")
        assert len(sender_inbox) == 1
        full = mailbox.read_message(supervisor.mailbox_root, "sender", sender_inbox[0]["id"])
        assert "no reply produced" in full["body"]

    def test_claude_failure_synthesizes_failure_reply(self, supervisor, monkeypatch):
        supervisor.kickoff()
        msg = mailbox.write_message(supervisor.mailbox_root, "sender", supervisor.session_name, "hi", "body")

        monkeypatch.setenv("STUB_FAIL", "1")
        supervisor.process_one(msg["id"])

        sender_inbox = mailbox.list_inbox(supervisor.mailbox_root, "sender")
        assert len(sender_inbox) == 1
        full = mailbox.read_message(supervisor.mailbox_root, "sender", sender_inbox[0]["id"])
        assert full["body"].startswith("failed:")
        # Message still archived and counted even on failure
        assert mailbox.list_inbox(supervisor.mailbox_root, supervisor.session_name) == []
        assert supervisor.msg_count == 1

    def test_accumulates_cost_across_messages(self, supervisor, monkeypatch):
        monkeypatch.setenv("STUB_COST", "0.10")
        supervisor.kickoff()
        msg1 = mailbox.write_message(supervisor.mailbox_root, "sender", supervisor.session_name, "a", "b")
        monkeypatch.setenv("STUB_COST", "0.20")
        supervisor.process_one(msg1["id"])
        assert supervisor.cost_usd_total == pytest.approx(0.30)

    def test_writes_processing_state_with_current(self, supervisor, monkeypatch, tmp_path):
        supervisor.kickoff()
        msg = mailbox.write_message(supervisor.mailbox_root, "sender", supervisor.session_name, "urgent", "body")

        # Patch _run_claude to snapshot state.json mid-flight before returning.
        captured = {}
        original = supervisor._run_claude

        def spy(prompt, *, resume):
            captured["state"] = json.loads(supervisor.state_path.read_text())
            return original(prompt, resume=resume)

        supervisor._run_claude = spy
        supervisor.process_one(msg["id"])

        assert captured["state"]["phase"] == "processing"
        assert captured["state"]["current"]["from"] == "sender"
        assert captured["state"]["current"]["subject"] == "urgent"


class TestRequestStop:
    def test_sets_stop_flag(self, supervisor):
        assert supervisor._stop is False
        supervisor.request_stop()
        assert supervisor._stop is True


class TestRun:
    def test_stops_cleanly_and_writes_stopped_state(self, supervisor, monkeypatch):
        calls = {"n": 0}

        def fake_sleep(_seconds):
            calls["n"] += 1
            supervisor._stop = True

        monkeypatch.setattr("cld.messenger.agent_loop.time.sleep", fake_sleep)
        supervisor.run()

        assert calls["n"] == 1
        state = json.loads(supervisor.state_path.read_text())
        assert state["phase"] == "stopped"

    def test_processes_pending_message_before_polling(self, supervisor, monkeypatch):
        mailbox.write_message(supervisor.mailbox_root, "sender", supervisor.session_name, "hi", "body")

        def fake_sleep(_seconds):
            supervisor._stop = True

        monkeypatch.setattr("cld.messenger.agent_loop.time.sleep", fake_sleep)
        supervisor.run()

        assert supervisor.msg_count == 1


class TestForgetSessionBookmark:
    """The origin's working copy is read-only: a plain jj there fails (design D2)."""

    def test_passes_ignore_working_copy_in_origin(self, supervisor, tmp_path, monkeypatch):
        origin = tmp_path / "origin"
        monkeypatch.setenv("WORKSPACE_ORIGIN", str(origin))
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs["cwd"]))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr("cld.messenger.agent_loop.subprocess.run", fake_run)
        supervisor._forget_session_bookmark()
        assert calls == [
            (["jj", "--ignore-working-copy", "bookmark", "forget", "a.t1.repoA"], str(origin)),
        ]

    def test_a_failed_forget_warns(self, supervisor, monkeypatch, caplog):
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="no such bookmark")
        monkeypatch.setattr("cld.messenger.agent_loop.subprocess.run", lambda *a, **k: failed)
        supervisor._forget_session_bookmark()
        assert "jj bookmark forget a.t1.repoA failed (rc=1): no such bookmark" in caplog.text


# --- task mode ---------------------------------------------------------------

_CLD_PROMPTS = Path(__file__).resolve().parent.parent / "prompts" / "personas"


@pytest.fixture
def task_files(tmp_path):
    """A preamble + role persona pair on disk, both templated."""
    preamble = tmp_path / "task-agent.md"
    preamble.write_text(
        "---\ndescription: meta\n---\n\n# Preamble\n\n"
        "container=${CONTAINER_NAME} slug=${TASK_SLUG} branch=${DELIVERABLE_BRANCH}\n"
        "master=${PARENT_MASTER} persona=${PERSONA} anchor=${AGENT_ANCHOR_HASH}\n"
        "turns=${MAX_TURNS} repo=${REPO_BASENAME} path=${REPO_ABS_PATH}\n\npeers:\n${PEERS}\n"
    )
    persona = tmp_path / "implementer.md"
    persona.write_text("---\ndescription: role\n---\n\n# Implementer\n\nbranch is ${DELIVERABLE_BRANCH}\n")
    return preamble, persona


def _task_mode(task_files, **overrides):
    preamble, persona = task_files
    kwargs = {
        "slug": "add-oauth",
        "parent_master": "cld_master_repoA_ab12",
        "deliverable_branch": "add-oauth-login",
        "peers": {"a.repoA.contract": 15},
        "persona_name": "implementer",
        "preamble_path": preamble,
        "task_text": "Wire up OAuth login.",
        "anchor": "abc123def456",
    }
    return TaskMode(**{**kwargs, **overrides})


class TestTurnCapIsNotFatal:
    """Hitting --max-turns is claude exiting 1 with a full JSON envelope. Raising there
    killed the supervisor mid-kickoff, so any task needing more turns died with its
    container."""

    _CAPPED = {
        "is_error": True, "subtype": "error_max_turns", "session_id": "sess-1",
        "total_cost_usd": 1.5, "result": "got partway",
        "errors": ["Reached maximum number of turns (7)"],
    }

    def _capped_run(self, supervisor, monkeypatch):
        import subprocess as sp
        completed = sp.CompletedProcess([], 1, stdout=json.dumps(self._CAPPED), stderr="")
        monkeypatch.setattr("cld.messenger.agent_loop.subprocess.run", lambda *a, **k: completed)

    def test_run_claude_returns_the_envelope(self, supervisor, monkeypatch):
        self._capped_run(supervisor, monkeypatch)
        data = supervisor._run_claude("do it", resume=False)
        assert data["session_id"] == "sess-1"

    def test_kickoff_keeps_the_session_and_goes_idle(self, supervisor, monkeypatch):
        self._capped_run(supervisor, monkeypatch)
        supervisor.kickoff()
        assert supervisor.session_id == "sess-1"
        state = json.loads((supervisor.mailbox_root / supervisor.session_name / "state.json").read_text())
        assert state["phase"] == "idle"

    def test_a_real_failure_still_raises(self, supervisor, monkeypatch):
        import subprocess as sp
        completed = sp.CompletedProcess([], 1, stdout='{"is_error": true, "subtype": "error_other"}', stderr="boom")
        monkeypatch.setattr("cld.messenger.agent_loop.subprocess.run", lambda *a, **k: completed)
        with pytest.raises(RuntimeError, match="claude exited 1"):
            supervisor._run_claude("do it", resume=False)

    def test_fallback_reply_says_the_cap_was_hit(self, supervisor, monkeypatch):
        mailbox.write_message(supervisor.mailbox_root, "cld_master_x", supervisor.session_name,
                              "question", "well?", expects_reply=True)
        msg_id = mailbox.list_inbox(supervisor.mailbox_root, supervisor.session_name)[0]["id"]
        supervisor.session_id = "sess-1"
        self._capped_run(supervisor, monkeypatch)
        supervisor.process_one(msg_id)
        sent = (supervisor.mailbox_root / supervisor.session_name / "outbox.log").read_text()
        assert "turn cap" in sent


class TestComposeKickoff:
    def _compose(self, task, tmp_path):
        return compose_kickoff(
            task, session_name="a.repoA.add-oauth",
            repo_root=tmp_path / "repo", max_turns=7, root_ask_limit=3,
        )

    def test_layer_order(self, task_files, tmp_path):
        prompt = self._compose(_task_mode(task_files), tmp_path)
        assert prompt.index("# Preamble") < prompt.index("# Your task")

    def test_substitutes_in_both_layers(self, task_files, tmp_path):
        prompt = self._compose(_task_mode(task_files), tmp_path)
        assert "container=a.repoA.add-oauth" in prompt
        assert "slug=add-oauth" in prompt
        assert "branch=add-oauth-login" in prompt
        assert "master=cld_master_repoA_ab12" in prompt
        assert "persona=implementer" in prompt
        assert "anchor=abc123def456" in prompt
        assert "turns=7" in prompt
        assert "repo=repo" in prompt
        assert "${" not in prompt

    def test_task_appended_verbatim(self, task_files, tmp_path):
        # A task description is user content, not a template: $VAR must survive.
        task = _task_mode(task_files, task_text="Set $DELIVERABLE_BRANCH and ${CONTAINER_NAME} literally")
        prompt = self._compose(task, tmp_path)
        assert "Set $DELIVERABLE_BRANCH and ${CONTAINER_NAME} literally" in prompt

    def test_frontmatter_stripped_from_the_preamble(self, task_files, tmp_path):
        prompt = self._compose(_task_mode(task_files), tmp_path)
        assert prompt.startswith("# Preamble")
        assert "description: meta" not in prompt

    def test_peers_rendered(self, task_files, tmp_path):
        prompt = self._compose(_task_mode(task_files), tmp_path)
        assert "- `a.repoA.contract` (hop budget: 15)" in prompt

    def test_two_peers_both_listed(self, task_files, tmp_path):
        task = _task_mode(task_files, peers={"a.r.a": 3, "a.r.b": 9})
        prompt = self._compose(task, tmp_path)
        assert "- `a.r.a` (hop budget: 3)" in prompt
        assert "- `a.r.b` (hop budget: 9)" in prompt

    def test_no_peers_says_master_only(self, task_files, tmp_path):
        prompt = self._compose(_task_mode(task_files, peers={}), tmp_path)
        assert "the master is your only correspondent" in prompt

    def test_host_launched_master_placeholder(self, task_files, tmp_path):
        prompt = self._compose(_task_mode(task_files, parent_master=""), tmp_path)
        assert "master=(none -- launched directly on the host)" in prompt


class TestShippedPreamble:
    """The real prompts/personas/task-agent.md, as baked into the image."""

    def test_exists_where_the_image_bakes_it(self):
        assert (_CLD_PROMPTS / "task-agent.md").is_file()

    def test_no_placeholder_survives_composition(self, task_files, tmp_path):
        _, persona = task_files
        task = _task_mode(task_files, preamble_path=_CLD_PROMPTS / "task-agent.md")
        prompt = compose_kickoff(
            task, session_name="a.repoA.add-oauth",
            repo_root=tmp_path / "repo", max_turns=7, root_ask_limit=3,
        )
        # A mistyped ${VAR} in the shipped preamble would otherwise reach the agent
        # verbatim and only be noticed by reading a live transcript.
        assert "${" not in prompt


class TestTaskModeFromEnv:
    @pytest.fixture(autouse=True)
    def _env(self, task_files, monkeypatch, tmp_path):
        preamble, persona = task_files
        monkeypatch.setattr("cld.messenger.agent_loop._CLD_ROOT", tmp_path / "cld_root")
        baked = tmp_path / "cld_root" / "prompts" / "personas"
        baked.mkdir(parents=True)
        (baked / "task-agent.md").write_text(preamble.read_text())
        monkeypatch.setattr("cld.messenger.agent_loop._BRIEF_FILE", tmp_path / "brief.md")
        (tmp_path / "brief.md").write_text("Wire up OAuth login.\n")
        monkeypatch.setenv("AGENT_TASK_SLUG", "add-oauth")
        monkeypatch.setenv("AGENT_DELIVERABLE_BRANCH", "add-oauth-login")
        monkeypatch.setenv("AGENT_PERSONA", "implementer")
        monkeypatch.setenv("AGENT_PARENT_MASTER", "cld_master_repoA_ab12")
        monkeypatch.setenv("AGENT_PEERS", "a.repoA.contract:15")
        monkeypatch.setenv("AGENT_ANCHOR_HASH", "abc123")

    def test_reads_all_fields(self, task_files):
        task = TaskMode.from_env()
        assert task.slug == "add-oauth"
        assert task.parent_master == "cld_master_repoA_ab12"
        assert task.deliverable_branch == "add-oauth-login"
        assert task.peers == {"a.repoA.contract": 15}
        assert task.persona_name == "implementer"
        assert task.anchor == "abc123"
        assert task.task_text == "Wire up OAuth login."

    def test_task_is_the_composed_brief(self, tmp_path):
        (tmp_path / "brief.md").write_text("# Implementer\n\nWire up OAuth login.\n")
        assert TaskMode.from_env().task_text == "# Implementer\n\nWire up OAuth login."

    def test_missing_brief_refuses(self, tmp_path):
        (tmp_path / "brief.md").unlink()
        with pytest.raises(RuntimeError, match="no task given"):
            TaskMode.from_env()

    def test_persona_name_is_display_only_and_may_be_empty(self, monkeypatch):
        monkeypatch.delenv("AGENT_PERSONA")
        assert TaskMode.from_env().persona_name == ""

    def test_empty_peers(self, monkeypatch):
        monkeypatch.setenv("AGENT_PEERS", "")
        assert TaskMode.from_env().peers == {}

    def test_missing_slug_raises(self, monkeypatch):
        monkeypatch.delenv("AGENT_TASK_SLUG")
        with pytest.raises(RuntimeError, match="AGENT_TASK_SLUG"):
            TaskMode.from_env()

    def test_missing_branch_raises(self, monkeypatch):
        monkeypatch.delenv("AGENT_DELIVERABLE_BRANCH")
        with pytest.raises(RuntimeError, match="wrap-up has no target"):
            TaskMode.from_env()

    def test_missing_preamble_raises(self, monkeypatch, tmp_path):
        (tmp_path / "cld_root" / "prompts" / "personas" / "task-agent.md").unlink()
        with pytest.raises(RuntimeError, match="preamble not found"):
            TaskMode.from_env()

    def test_repo_name_prefers_host_repo_over_workspace_dir(self, monkeypatch, tmp_path):
        # /workspace/current's basename would tell the agent its repo is "current".
        monkeypatch.setenv("CLD_HOST_PROJECT_DIR", "/home/u/projects/lide-api")
        task = TaskMode.from_env()
        prompt = compose_kickoff(
            task, session_name="s", repo_root=Path("/workspace/current"), max_turns=7, root_ask_limit=3,
        )
        assert "repo=lide-api" in prompt

    def test_repo_name_falls_back_to_workspace_dir(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CLD_HOST_PROJECT_DIR", raising=False)
        prompt = compose_kickoff(
            TaskMode.from_env(), session_name="s", repo_root=tmp_path / "repo", max_turns=7,
            root_ask_limit=3,
        )
        assert "repo=repo" in prompt


class TestSupervisorTaskMode:
    @pytest.fixture
    def task_supervisor(self, tmp_path, task_files, monkeypatch):
        mailbox_root = tmp_path / "mailboxes"
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        prompt_log = tmp_path / "prompt.log"
        monkeypatch.setenv("STUB_PROMPT_LOG", str(prompt_log))
        monkeypatch.setenv("STUB_MAILBOX_ROOT", str(mailbox_root))
        monkeypatch.setenv("STUB_SESSION_NAME", "a.repoA.add-oauth")
        task = _task_mode(task_files)
        sup = AgentSupervisor(
            session_name="a.repoA.add-oauth",
            repo_root=repo_root,
            mailbox_root=mailbox_root,
            persona_path=None,
            claude_bin=str(_STUB_CLAUDE),
            max_turns=7,
            task=task,
        )
        sup._prompt_log = prompt_log
        return sup

    def test_meta_written_at_construction(self, task_supervisor):
        meta = mailbox.read_meta(task_supervisor.mailbox_root, task_supervisor.session_name)
        assert meta["parent"] == "cld_master_repoA_ab12"
        assert meta["task"] == "Wire up OAuth login."
        assert meta["persona"] == "implementer"
        assert meta["deliverable_branch"] == "add-oauth-login"
        assert meta["anchor"] == "abc123def456"
        assert meta["peers"] == {"a.repoA.contract": 15}
        assert "created_at" in meta

    def test_meta_is_write_once_across_restart(self, task_supervisor, task_files, tmp_path):
        first = mailbox.read_meta(task_supervisor.mailbox_root, task_supervisor.session_name)
        AgentSupervisor(
            session_name=task_supervisor.session_name,
            repo_root=task_supervisor.repo_root,
            mailbox_root=task_supervisor.mailbox_root,
            persona_path=task_supervisor.persona_path,
            claude_bin=str(_STUB_CLAUDE),
            task=_task_mode(task_files, task_text="a different task"),
        )
        assert mailbox.read_meta(task_supervisor.mailbox_root, task_supervisor.session_name) == first

    def test_kickoff_uses_composed_prompt(self, task_supervisor):
        task_supervisor.kickoff()
        prompt = _read_prompt_log(task_supervisor._prompt_log)[0]
        assert prompt.index("# Preamble") < prompt.index("# Your task")
        assert "Wire up OAuth login." in prompt

    def test_phases_unchanged(self, task_supervisor):
        task_supervisor.kickoff()
        state = json.loads(task_supervisor.state_path.read_text())
        assert state["phase"] == "idle"
        assert state["session_id"] == "kickoff-session-id"

    def test_repo_agent_mode_writes_no_meta(self, supervisor):
        assert mailbox.read_meta(supervisor.mailbox_root, supervisor.session_name) is None
