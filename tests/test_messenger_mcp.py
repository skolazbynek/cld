"""Tests for cld.mcp.messenger MCP tools (identity + mailbox wiring)."""

import json
from unittest.mock import patch

import pytest

import cld.mcp.messenger as messenger_mod
from cld.mcp.messenger import (
    archive,
    fleet_digest,
    list_agents,
    list_inbox,
    read_mailbox,
    read_message,
    send,
)
from cld.messenger.mailbox import ensure_meta, ensure_mailbox, write_message


@pytest.fixture(autouse=True)
def _mailbox_env(tmp_path, monkeypatch):
    monkeypatch.delenv("SESSION_NAME", raising=False)
    monkeypatch.setattr(messenger_mod, "_mailbox_root", lambda: tmp_path)
    yield tmp_path


class TestSend:
    def test_requires_session_name(self):
        result = send(to="a.t1.repoB", subject="hi", body="b")
        assert "error" in result

    def test_sends_to_full_container_name(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SESSION_NAME", "cld_master_repoA_abcd1234")
        with patch("cld.messenger.mailbox.list_containers", return_value=[
            {"name": "a.t1.repoB", "kind": "agent", "repo": "/x/repoB", "status": "running"},
        ]):
            result = send(to="a.t1.repoB", subject="hi", body="review this")
        assert "id" in result
        inbox_files = list((tmp_path / "a.t1.repoB" / "inbox").glob("*.json"))
        assert len(inbox_files) == 1

    def test_unresolvable_recipient_returns_error(self, monkeypatch):
        monkeypatch.setenv("SESSION_NAME", "cld_master_repoA_abcd1234")
        with patch("cld.messenger.mailbox.list_containers", return_value=[]):
            result = send(to="nonexistent", subject="hi", body="b")
        assert "error" in result


class TestListInboxAndRead:
    def test_round_trip(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SESSION_NAME", "a.t1.repoB")
        with patch("cld.messenger.mailbox.list_containers", return_value=[
            {"name": "a.t1.repoB", "kind": "agent", "repo": "/x/repoB", "status": "running"},
        ]):
            sent = send(to="a.t1.repoB", subject="hi", body="body text")

        entries = list_inbox()
        assert len(entries) == 1
        assert entries[0]["id"] == sent["id"]

        full = read_message(id=sent["id"])
        assert full["subject"] == "hi"
        assert full["body"] == "body text"

    def test_list_inbox_without_session_name_returns_error_entry(self):
        result = list_inbox()
        assert result[0]["error"]

    def test_read_missing_message_returns_error(self, monkeypatch):
        monkeypatch.setenv("SESSION_NAME", "a.t1.repoB")
        result = read_message(id="nonexistent")
        assert "error" in result


class TestArchive:
    def test_archives_own_message(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SESSION_NAME", "a.t1.repoB")
        with patch("cld.messenger.mailbox.list_containers", return_value=[
            {"name": "a.t1.repoB", "kind": "agent", "repo": "/x/repoB", "status": "running"},
        ]):
            sent = send(to="a.t1.repoB", subject="hi", body="b")
        result = archive(id=sent["id"])
        assert result == {"ok": True}
        assert list_inbox() == []

    def test_missing_message_returns_error(self, monkeypatch):
        monkeypatch.setenv("SESSION_NAME", "a.t1.repoB")
        result = archive(id="nonexistent")
        assert "error" in result


class TestListAgents:
    def test_delegates_to_mailbox_list_containers(self):
        with patch("cld.messenger.mailbox.list_containers", return_value=[{"name": "x"}]) as m:
            result = list_agents(kind="agent")
        m.assert_called_once_with("agent")
        assert result == [{"name": "x"}]

    def test_empty_kind_passes_none(self):
        with patch("cld.messenger.mailbox.list_containers", return_value=[]) as m:
            list_agents()
        m.assert_called_once_with(None)


_SPAWN = {
    "parent": "cld_master_repoA_abcd1234",
    "task": "wire up oauth login\nwith all the trimmings",
    "persona": "implementer",
    "deliverable_branch": "add-oauth",
    "anchor": "abc123",
    "peers": {},
}


class TestSendGate:
    """send() is one of two instructed paths into the transport; both are gated."""

    def test_master_channel_is_not_counted(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SESSION_NAME", "agent-a")
        ensure_meta(tmp_path, "agent-a", **_SPAWN)
        ensure_mailbox(tmp_path, "cld_master_repoA_abcd1234")
        result = send(to="cld_master_repoA_abcd1234", subject="hi", body="b")
        assert "hops" not in result
        assert not (tmp_path / "_edges").exists()

    def test_peer_edge_reports_position(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SESSION_NAME", "agent-a")
        monkeypatch.setenv("CLD_PEER_ABSOLUTE_LIMIT", "2")
        ensure_meta(tmp_path, "agent-a", **_SPAWN)
        ensure_meta(tmp_path, "agent-b", **_SPAWN)
        first = send(to="agent-b", subject="hi", body="b")
        assert (first["hops"], first["limit"]) == (1, 2)
        assert send(to="agent-b", subject="hi", body="b")["hops"] == 2
        refused = send(to="agent-b", subject="hi", body="b")
        assert "error" in refused and "2/2" in refused["error"]
        assert len(list((tmp_path / "agent-b" / "inbox").glob("*.json"))) == 2

    def test_declared_limit_beats_the_config_default(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SESSION_NAME", "agent-a")
        monkeypatch.setenv("CLD_PEER_ABSOLUTE_LIMIT", "50")
        ensure_meta(tmp_path, "agent-a", **{**_SPAWN, "peers": {"agent-b": 1}})
        ensure_meta(tmp_path, "agent-b", **_SPAWN)
        assert send(to="agent-b", subject="hi", body="b")["limit"] == 1

    def test_obligation_flags_reach_the_transport(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SESSION_NAME", "agent-a")
        ensure_meta(tmp_path, "agent-a", **_SPAWN)
        ensure_mailbox(tmp_path, "cld_master_repoA_abcd1234")
        sent = send(to="cld_master_repoA_abcd1234", subject="q", body="?",
                    expects_reply=True, answers="m0")
        delivered = json.loads(
            (tmp_path / "cld_master_repoA_abcd1234" / "inbox" / f"{sent['id']}.json").read_text()
        )
        assert (delivered["expects_reply"], delivered["answers"]) == (True, "m0")

    def test_ask_limit_refuses_a_further_question(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SESSION_NAME", "agent-a")
        monkeypatch.setenv("CLD_PEER_ABSOLUTE_LIMIT", "50")
        monkeypatch.setenv("CLD_ROOT_ASK_LIMIT", "1")
        ensure_meta(tmp_path, "agent-a", **_SPAWN)
        ensure_meta(tmp_path, "agent-b", **_SPAWN)
        assert send(to="agent-b", subject="q1", body="?", expects_reply=True)["open_asks"] == 1
        assert "ask limit 1" in send(to="agent-b", subject="q2", body="?", expects_reply=True)["error"]
        # Only asking is refused -- the edge is still open for an answer or an update.
        assert "error" not in send(to="agent-b", subject="fyi", body="x")


class TestFleetDigest:
    def test_requires_session_name(self):
        assert "error" in fleet_digest()[0]

    def test_own_fleet_only(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SESSION_NAME", "cld_master_repoA_abcd1234")
        ensure_meta(tmp_path, "mine", **_SPAWN)
        ensure_meta(tmp_path, "theirs", **{**_SPAWN, "parent": "cld_master_other"})
        rows = fleet_digest()
        assert [r["name"] for r in rows] == ["mine"]
        assert rows[0]["task"] == "wire up oauth login"      # truncated to one line

    def test_empty_fleet(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SESSION_NAME", "cld_master_nobody")
        assert fleet_digest() == []


class TestReadMailbox:
    def _fleet_member(self, tmp_path, name="mine"):
        ensure_meta(tmp_path, name, **_SPAWN)
        return name

    def test_requires_session_name(self):
        assert "error" in read_mailbox(name="mine")[0]

    def test_includes_received_and_sent(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SESSION_NAME", "cld_master_repoA_abcd1234")
        name = self._fleet_member(tmp_path)
        write_message(tmp_path, "cld_master_repoA_abcd1234", name, "do this", "task")
        write_message(tmp_path, name, "cld_master_repoA_abcd1234", "Re: do this", "done")
        entries = read_mailbox(name=name)
        assert [(e["direction"], e["subject"]) for e in entries] == [
            ("in", "do this"), ("out", "Re: do this"),
        ]

    def test_since_is_exclusive(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SESSION_NAME", "cld_master_repoA_abcd1234")
        name = self._fleet_member(tmp_path)
        write_message(tmp_path, "cld_master_repoA_abcd1234", name, "first", "b")
        write_message(tmp_path, "cld_master_repoA_abcd1234", name, "second", "b")
        first_ts = read_mailbox(name=name)[0]["ts"]
        assert [e["subject"] for e in read_mailbox(name=name, since=first_ts)] == ["second"]

    def test_foreign_mailbox_refused_naming_the_parent(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SESSION_NAME", "cld_master_repoA_abcd1234")
        ensure_meta(tmp_path, "theirs", **{**_SPAWN, "parent": "cld_master_other"})
        assert "cld_master_other" in read_mailbox(name="theirs")[0]["error"]

    def test_non_task_agent_refused(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SESSION_NAME", "cld_master_repoA_abcd1234")
        ensure_mailbox(tmp_path, "a.t1.repoA")
        assert "not a task-agent" in read_mailbox(name="a.t1.repoA")[0]["error"]

    def test_reaped_member_still_readable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SESSION_NAME", "cld_master_repoA_abcd1234")
        name = self._fleet_member(tmp_path)
        write_message(tmp_path, "cld_master_repoA_abcd1234", name, "do this", "task")
        from cld.messenger.mailbox import archive_mailbox
        archive_mailbox(tmp_path, name)
        assert [e["subject"] for e in read_mailbox(name=name)] == ["do this"]

    def test_own_mailbox_refused(self, monkeypatch, tmp_path):
        """The read tools are master surfaces; an agent has list_inbox for itself."""
        monkeypatch.setenv("SESSION_NAME", "mine")
        self._fleet_member(tmp_path)
        assert "not in your fleet" in read_mailbox(name="mine")[0]["error"]
