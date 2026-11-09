"""Tests for the messenger CLI verbs.

Only `send` for now, and only what matters most about it: it is the path the
`messenger-send` skill tells agents to run, so the hop gate has to cover it exactly
like the MCP tool (docs/design-task-agents.md §10, "both send paths").
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from cld.messenger import identity as identity_mod
from cld.messenger import send as send_cli
from cld.messenger.identity import resolve_self
from cld.messenger.mailbox import ensure_mailbox, ensure_meta, list_inbox, read_message

_SPAWN = {
    "parent": "cld_master_repoA_abcd1234",
    "task": "add oauth login",
    "persona": "implementer",
    "deliverable_branch": "add-oauth",
    "anchor": "abc123",
    "peers": {},
}


def _invoke(monkeypatch, tmp_path, to: str, frm: str = "agent-a") -> None:
    body_file = tmp_path / "body.md"
    body_file.write_text("hello\n")
    monkeypatch.setattr(sys, "argv", [
        "send", "--to", to, "--subject", "hi", "--body-file", str(body_file),
    ])
    monkeypatch.setattr(send_cli, "resolve_self", lambda: (frm, tmp_path))
    send_cli.main()


class TestSendCli:
    def test_peer_send_is_counted_and_reported(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("CLD_PEER_ABSOLUTE_LIMIT", "2")
        ensure_meta(tmp_path, "agent-a", **_SPAWN)
        ensure_meta(tmp_path, "agent-b", **_SPAWN)
        _invoke(monkeypatch, tmp_path, "agent-b")
        assert "(hop 1/2)" in capsys.readouterr().out

    def test_master_send_reports_no_hops(self, monkeypatch, tmp_path, capsys):
        ensure_meta(tmp_path, "agent-a", **_SPAWN)
        ensure_mailbox(tmp_path, "cld_master_repoA_abcd1234")
        _invoke(monkeypatch, tmp_path, "cld_master_repoA_abcd1234")
        assert "hop" not in capsys.readouterr().out

    def test_refused_send_exits_1_and_delivers_nothing(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("CLD_PEER_ABSOLUTE_LIMIT", "1")
        ensure_meta(tmp_path, "agent-a", **_SPAWN)
        ensure_meta(tmp_path, "agent-b", **_SPAWN)
        _invoke(monkeypatch, tmp_path, "agent-b")
        capsys.readouterr()
        with pytest.raises(SystemExit) as exc:
            _invoke(monkeypatch, tmp_path, "agent-b")
        assert exc.value.code == 1
        assert "hop budget spent" in capsys.readouterr().err
        assert len(list_inbox(tmp_path, "agent-b")) == 1

    def test_peer_resolution_needs_no_container_enumeration(self, monkeypatch, tmp_path):
        """An agent container has no host channel, so enumerating would fail (§A.3)."""
        ensure_meta(tmp_path, "agent-a", **_SPAWN)
        ensure_meta(tmp_path, "agent-b", **_SPAWN)
        with patch("cld.messenger.mailbox.list_containers", side_effect=AssertionError("enumerated")):
            _invoke(monkeypatch, tmp_path, "agent-b")
        assert len(list_inbox(tmp_path, "agent-b")) == 1

    def test_inline_body_is_delivered(self, monkeypatch, tmp_path):
        ensure_meta(tmp_path, "agent-a", **_SPAWN)
        ensure_meta(tmp_path, "agent-b", **_SPAWN)
        monkeypatch.setattr(sys, "argv", [
            "send", "--to", "agent-b", "--subject", "hi", "--body", "hello inline",
        ])
        monkeypatch.setattr(send_cli, "resolve_self", lambda: ("agent-a", tmp_path))
        send_cli.main()
        [msg] = list_inbox(tmp_path, "agent-b")
        assert read_message(tmp_path, "agent-b", msg["id"])["body"] == "hello inline"

    def test_body_and_body_file_are_mutually_exclusive(self, monkeypatch, tmp_path):
        body_file = tmp_path / "body.md"
        body_file.write_text("hello\n")
        monkeypatch.setattr(sys, "argv", [
            "send", "--to", "agent-b", "--subject", "hi",
            "--body", "hello inline", "--body-file", str(body_file),
        ])
        monkeypatch.setattr(send_cli, "resolve_self", lambda: ("agent-a", tmp_path))
        with pytest.raises(SystemExit) as exc:
            send_cli.main()
        assert exc.value.code == 2


class TestResolveSelf:
    """Host-side identity chain (design-ticket-containers.md section 6.5):
    SESSION_NAME (in-container) > explicit ticket (arg or CLD_TICKET) > the
    single running ticket container > the cwd repo's host identity > error."""

    @pytest.fixture(autouse=True)
    def _host(self, monkeypatch, tmp_path):
        monkeypatch.delenv("SESSION_NAME", raising=False)
        monkeypatch.delenv("CLD_TICKET", raising=False)
        monkeypatch.setenv("CLD_MAILBOX_ROOT", str(tmp_path))
        self.mailbox_root = tmp_path

    def _ticket(self, name, status="running"):
        return {"name": name, "kind": "ticket", "repo": "", "status": status}

    def test_in_container_session_name_wins(self, monkeypatch):
        monkeypatch.setenv("SESSION_NAME", "t.lide-1")
        assert resolve_self() == ("t.lide-1", Path("/var/cld/mailboxes"))

    def _known(self, monkeypatch, *tickets):
        monkeypatch.setattr(
            identity_mod, "list_cld_containers",
            lambda kind: [self._ticket(t) if isinstance(t, str) else self._ticket(*t)
                          for t in tickets],
        )

    def test_explicit_ticket_argument(self, monkeypatch):
        self._known(monkeypatch, "t.lide-2600")
        assert resolve_self("LIDE-2600") == ("t.lide-2600", self.mailbox_root)

    def test_ticket_env(self, monkeypatch):
        monkeypatch.setenv("CLD_TICKET", "LIDE-2600")
        self._known(monkeypatch, "t.lide-2600")
        assert resolve_self() == ("t.lide-2600", self.mailbox_root)

    def test_argument_beats_env(self, monkeypatch):
        monkeypatch.setenv("CLD_TICKET", "LIDE-1")
        self._known(monkeypatch, "t.lide-1", "t.lide-2")
        assert resolve_self("LIDE-2")[0] == "t.lide-2"

    def test_explicit_stopped_ticket_is_valid(self, monkeypatch):
        """Existence, not liveness, validates an explicit ticket: a stopped
        container still owns its mailbox."""
        self._known(monkeypatch, ("t.lide-1", "stopped"))
        assert resolve_self("LIDE-1") == ("t.lide-1", self.mailbox_root)

    def test_unknown_explicit_ticket_errors_naming_existing(self, monkeypatch):
        """A typo'd --ticket must not silently attribute sends to a mailbox
        nothing reads."""
        self._known(monkeypatch, "t.lide-1")
        with pytest.raises(RuntimeError) as exc:
            resolve_self("ghost")
        assert "t.ghost" in str(exc.value)
        assert "t.lide-1" in str(exc.value)

    def test_unknown_ticket_env_errors(self, monkeypatch):
        monkeypatch.setenv("CLD_TICKET", "ghost")
        self._known(monkeypatch)
        with pytest.raises(RuntimeError, match="no ticket container 't.ghost'") as exc:
            resolve_self()
        assert "none" in str(exc.value)

    def test_single_running_ticket_is_self(self, monkeypatch):
        monkeypatch.setattr(
            identity_mod, "list_cld_containers",
            lambda kind: [self._ticket("t.lide-9")],
        )
        assert resolve_self() == ("t.lide-9", self.mailbox_root)

    def test_stopped_tickets_do_not_count(self, monkeypatch):
        """One stopped + one running = exactly one RUNNING ticket -- it is self."""
        monkeypatch.setattr(
            identity_mod, "list_cld_containers",
            lambda kind: [self._ticket("t.a", status="stopped"),
                          self._ticket("t.b")],
        )
        assert resolve_self() == ("t.b", self.mailbox_root)

    def test_no_tickets_falls_back_to_cwd_host_identity(self, monkeypatch, tmp_path):
        monkeypatch.setattr(identity_mod, "list_cld_containers", lambda kind: [])
        repo_root = tmp_path / "repo"
        monkeypatch.setattr(
            identity_mod, "get_backend",
            lambda: type("B", (), {"repo_root": repo_root})(),
        )
        assert resolve_self()[0] == identity_mod.host_identity_name(repo_root)

    def test_ambiguous_tickets_outside_a_repo_error_lists_them(self, monkeypatch):
        monkeypatch.setattr(
            identity_mod, "list_cld_containers",
            lambda kind: [self._ticket("t.a"), self._ticket("t.b")],
        )
        def _no_repo():
            raise RuntimeError("not a repo")
        monkeypatch.setattr(identity_mod, "get_backend", _no_repo)
        with pytest.raises(RuntimeError) as exc:
            resolve_self()
        assert "t.a" in str(exc.value)
        assert "t.b" in str(exc.value)
        assert "CLD_TICKET" in str(exc.value)
