"""Tests for cld.messenger.mailbox: pure filesystem transport."""

import json
from unittest.mock import patch

import pytest

from cld.messenger.mailbox import (
    archive_mailbox,
    archive_message,
    ask_spent,
    bump_edge,
    edge_obligations,
    edge_path,
    edge_spent,
    ensure_mailbox,
    ensure_meta,
    fleet_digest,
    gated_send,
    list_containers,
    list_fleet,
    list_inbox,
    mailbox_dir,
    mailbox_reaped,
    oldest_inbox_id,
    outbox_snapshot,
    read_edge,
    read_message,
    read_meta,
    read_meta_resolved,
    read_state,
    replied_since,
    resolve_mailbox_dir,
    resolve_recipient,
    state_path,
    task_summary,
    transcript,
    write_message,
)

_SPAWN = {
    "parent": "cld_master_repoA_abcd1234",
    "task": "add oauth login",
    "persona": "implementer",
    "deliverable_branch": "add-oauth-login",
    "anchor": "abc123",
    "peers": {"a.repoA.contract": 15},
}


def _outbox_lines(root, name) -> list[dict]:
    path = mailbox_dir(root, name) / "outbox.log"
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()] if path.is_file() else []


class TestEnsureMailbox:
    def test_creates_subdirs(self, tmp_path):
        ensure_mailbox(tmp_path, "a.t1.repoA")
        base = mailbox_dir(tmp_path, "a.t1.repoA")
        assert (base / "tmp").is_dir()
        assert (base / "inbox").is_dir()
        assert (base / "archive").is_dir()


class TestWriteMessage:
    def test_delivers_to_inbox(self, tmp_path):
        msg = write_message(tmp_path, "sender", "recipient", "hi", "body text")
        inbox_file = mailbox_dir(tmp_path, "recipient") / "inbox" / f"{msg['id']}.json"
        assert inbox_file.is_file()
        assert msg["from"] == "sender"
        assert msg["to"] == "recipient"
        assert msg["subject"] == "hi"
        assert msg["body"] == "body text"
        assert "ts" in msg

    def test_no_leftover_tmp_file(self, tmp_path):
        msg = write_message(tmp_path, "sender", "recipient", "hi", "body")
        tmp_file = mailbox_dir(tmp_path, "recipient") / "tmp" / f"{msg['id']}.json"
        assert not tmp_file.exists()

    def test_appends_outbox_log(self, tmp_path):
        write_message(tmp_path, "sender", "recipient", "hi", "body")
        outbox = mailbox_dir(tmp_path, "sender") / "outbox.log"
        assert outbox.is_file()
        assert len(outbox.read_text().strip().splitlines()) == 1

    def test_outbox_line_carries_subject_and_body(self, tmp_path):
        msg = write_message(tmp_path, "sender", "recipient", "hi", "body text")
        line = json.loads((mailbox_dir(tmp_path, "sender") / "outbox.log").read_text().strip())
        assert line == {
            "id": msg["id"], "to": "recipient", "subject": "hi",
            "body": "body text", "answers": "", "expects_reply": False, "ts": msg["ts"],
        }

    def test_creates_recipient_mailbox_if_missing(self, tmp_path):
        write_message(tmp_path, "sender", "new-recipient", "hi", "body")
        assert (mailbox_dir(tmp_path, "new-recipient") / "inbox").is_dir()


class TestListInbox:
    def test_lists_unread_sorted_by_ts(self, tmp_path):
        m1 = write_message(tmp_path, "a", "recipient", "first", "b1")
        m2 = write_message(tmp_path, "a", "recipient", "second", "b2")
        entries = list_inbox(tmp_path, "recipient")
        assert [e["id"] for e in entries] == [m1["id"], m2["id"]]
        assert entries[0]["subject"] == "first"

    def test_unread_only_excludes_archived(self, tmp_path):
        msg = write_message(tmp_path, "a", "recipient", "hi", "body")
        archive_message(tmp_path, "recipient", msg["id"])
        assert list_inbox(tmp_path, "recipient") == []

    def test_archive_listing(self, tmp_path):
        msg = write_message(tmp_path, "a", "recipient", "hi", "body")
        archive_message(tmp_path, "recipient", msg["id"])
        entries = list_inbox(tmp_path, "recipient", unread_only=False)
        assert len(entries) == 1
        assert entries[0]["id"] == msg["id"]

    def test_empty_mailbox_returns_empty_list(self, tmp_path):
        assert list_inbox(tmp_path, "nobody") == []


class TestReadMessage:
    def test_reads_from_inbox(self, tmp_path):
        msg = write_message(tmp_path, "a", "recipient", "hi", "body")
        result = read_message(tmp_path, "recipient", msg["id"])
        assert result == msg

    def test_reads_from_archive(self, tmp_path):
        msg = write_message(tmp_path, "a", "recipient", "hi", "body")
        archive_message(tmp_path, "recipient", msg["id"])
        result = read_message(tmp_path, "recipient", msg["id"])
        assert result == msg

    def test_missing_id_returns_none(self, tmp_path):
        ensure_mailbox(tmp_path, "recipient")
        assert read_message(tmp_path, "recipient", "nonexistent") is None


class TestArchiveMessage:
    def test_moves_file(self, tmp_path):
        msg = write_message(tmp_path, "a", "recipient", "hi", "body")
        assert archive_message(tmp_path, "recipient", msg["id"]) is True
        base = mailbox_dir(tmp_path, "recipient")
        assert not (base / "inbox" / f"{msg['id']}.json").exists()
        assert (base / "archive" / f"{msg['id']}.json").exists()

    def test_noop_if_already_archived(self, tmp_path):
        msg = write_message(tmp_path, "a", "recipient", "hi", "body")
        archive_message(tmp_path, "recipient", msg["id"])
        assert archive_message(tmp_path, "recipient", msg["id"]) is True

    def test_missing_id_returns_false(self, tmp_path):
        ensure_mailbox(tmp_path, "recipient")
        assert archive_message(tmp_path, "recipient", "nonexistent") is False


class TestOldestInboxId:
    def test_returns_none_when_empty(self, tmp_path):
        ensure_mailbox(tmp_path, "recipient")
        assert oldest_inbox_id(tmp_path, "recipient") is None

    def test_returns_oldest_by_mtime(self, tmp_path):
        m1 = write_message(tmp_path, "a", "recipient", "first", "b1")
        m2 = write_message(tmp_path, "a", "recipient", "second", "b2")
        inbox = mailbox_dir(tmp_path, "recipient") / "inbox"
        import os
        import time
        now = time.time()
        os.utime(inbox / f"{m1['id']}.json", (now + 100, now + 100))
        os.utime(inbox / f"{m2['id']}.json", (now, now))
        assert oldest_inbox_id(tmp_path, "recipient") == m2["id"]


class TestOutboxSnapshot:
    def test_zero_when_missing(self, tmp_path):
        assert outbox_snapshot(tmp_path, "sender") == 0

    def test_counts_lines(self, tmp_path):
        write_message(tmp_path, "sender", "r1", "hi", "b")
        write_message(tmp_path, "sender", "r2", "hi", "b")
        assert outbox_snapshot(tmp_path, "sender") == 2


class TestRepliedSince:
    def test_reply_to_sender_detected(self, tmp_path):
        snap = outbox_snapshot(tmp_path, "agent")
        write_message(tmp_path, "agent", "master", "Re: task", "done")
        assert replied_since(tmp_path, "agent", snap, "master") is True

    def test_send_to_peer_does_not_count(self, tmp_path):
        snap = outbox_snapshot(tmp_path, "agent")
        write_message(tmp_path, "agent", "peer", "fyi", "b")
        assert replied_since(tmp_path, "agent", snap, "master") is False

    def test_no_sends_at_all(self, tmp_path):
        assert replied_since(tmp_path, "agent", 0, "master") is False

    def test_pre_snapshot_reply_does_not_count(self, tmp_path):
        write_message(tmp_path, "agent", "master", "earlier", "b")
        snap = outbox_snapshot(tmp_path, "agent")
        assert replied_since(tmp_path, "agent", snap, "master") is False

    def test_malformed_line_skipped(self, tmp_path):
        write_message(tmp_path, "agent", "master", "hi", "b")  # creates the mailbox
        outbox = mailbox_dir(tmp_path, "agent") / "outbox.log"
        snap = outbox_snapshot(tmp_path, "agent")
        with outbox.open("a") as f:
            f.write("{not json\n")
        write_message(tmp_path, "agent", "master", "Re:", "b")
        assert replied_since(tmp_path, "agent", snap, "master") is True


class TestMeta:
    def test_round_trip_including_peer_budgets(self, tmp_path):
        written = ensure_meta(tmp_path, "agent1", **_SPAWN)
        assert read_meta(tmp_path, "agent1") == written
        assert written["peers"] == {"a.repoA.contract": 15}
        assert "created_at" in written

    def test_second_call_keeps_original(self, tmp_path):
        first = ensure_meta(tmp_path, "agent1", **_SPAWN)
        again = ensure_meta(tmp_path, "agent1", **{**_SPAWN, "task": "something else"})
        assert again == first
        assert read_meta(tmp_path, "agent1")["task"] == "add oauth login"

    def test_missing_returns_none(self, tmp_path):
        ensure_mailbox(tmp_path, "a.t1.repoA")
        assert read_meta(tmp_path, "a.t1.repoA") is None


class TestListFleet:
    def test_filters_by_parent(self, tmp_path):
        ensure_meta(tmp_path, "agent1", **_SPAWN)
        ensure_meta(tmp_path, "agent2", **{**_SPAWN, "parent": "cld_master_other"})
        assert [m["name"] for m in list_fleet(tmp_path, _SPAWN["parent"])] == ["agent1"]
        assert len(list_fleet(tmp_path)) == 2

    def test_excludes_mailboxes_without_meta(self, tmp_path):
        ensure_meta(tmp_path, "agent1", **_SPAWN)
        ensure_mailbox(tmp_path, "a.t1.repoA")          # legacy repo-agent mailbox
        ensure_mailbox(tmp_path, "cld_master_repoA_abcd1234")  # master
        assert [m["name"] for m in list_fleet(tmp_path)] == ["agent1"]

    def test_skips_reserved_entries(self, tmp_path):
        ensure_meta(tmp_path, "agent1", **_SPAWN)
        archive_mailbox(tmp_path, "agent1")
        bump_edge(tmp_path, "a", "b", 5)
        ensure_meta(tmp_path, "agent2", **_SPAWN)
        assert [m["name"] for m in list_fleet(tmp_path)] == ["agent2"]

    def test_empty_root(self, tmp_path):
        assert list_fleet(tmp_path / "nope") == []


class TestReadState:
    def test_missing_returns_none(self, tmp_path):
        ensure_mailbox(tmp_path, "agent1")
        assert read_state(tmp_path, "agent1") is None

    def test_reads_written_state(self, tmp_path):
        ensure_mailbox(tmp_path, "agent1")
        state_path(tmp_path, "agent1").write_text(json.dumps({"phase": "processing"}))
        assert read_state(tmp_path, "agent1")["phase"] == "processing"

    def test_unreadable_returns_none(self, tmp_path):
        ensure_mailbox(tmp_path, "agent1")
        state_path(tmp_path, "agent1").write_text("{not json")
        assert read_state(tmp_path, "agent1") is None


class TestTaskSummary:
    def test_first_line_only(self):
        assert task_summary("do the thing\nand then more\n") == "do the thing"

    def test_truncates(self):
        assert task_summary("x" * 200, width=10) == "x" * 9 + "…"

    def test_exact_width_kept(self):
        assert task_summary("x" * 10, width=10) == "x" * 10

    def test_empty(self):
        assert task_summary("") == ""
        assert task_summary("   \n  ") == ""


class TestReadMetaResolved:
    def test_live(self, tmp_path):
        ensure_meta(tmp_path, "agent1", **_SPAWN)
        assert read_meta_resolved(tmp_path, "agent1")["task"] == _SPAWN["task"]

    def test_archived(self, tmp_path):
        ensure_meta(tmp_path, "agent1", **_SPAWN)
        archive_mailbox(tmp_path, "agent1")
        assert read_meta_resolved(tmp_path, "agent1")["task"] == _SPAWN["task"]

    def test_unknown(self, tmp_path):
        assert read_meta_resolved(tmp_path, "agent1") is None

    def test_ensure_meta_still_ignores_an_archived_meta(self, tmp_path):
        """A re-used slug must write its own facts, not inherit the reaped agent's."""
        ensure_meta(tmp_path, "agent1", **_SPAWN)
        archive_mailbox(tmp_path, "agent1")
        fresh = ensure_meta(tmp_path, "agent1", **{**_SPAWN, "task": "a different task"})
        assert fresh["task"] == "a different task"
        assert read_meta(tmp_path, "agent1")["task"] == "a different task"


class TestFleetDigest:
    def test_own_parent_only(self, tmp_path):
        ensure_meta(tmp_path, "mine", **_SPAWN)
        ensure_meta(tmp_path, "theirs", **{**_SPAWN, "parent": "cld_master_other"})
        rows = fleet_digest(tmp_path, _SPAWN["parent"])
        assert [r["name"] for r in rows] == ["mine"]

    def test_field_set_matches_the_design(self, tmp_path):
        ensure_meta(tmp_path, "mine", **_SPAWN)
        assert set(fleet_digest(tmp_path, _SPAWN["parent"])[0]) == {
            "name", "task", "phase", "msg_count", "cost_usd_total", "unread", "last_activity",
            "open_asks", "open_with", "oldest_open",
        }

    def test_task_is_truncated(self, tmp_path):
        ensure_meta(tmp_path, "mine", **{**_SPAWN, "task": "first line\n" + "y" * 500})
        assert fleet_digest(tmp_path, _SPAWN["parent"])[0]["task"] == "first line"

    def test_state_fields_and_unread(self, tmp_path):
        ensure_meta(tmp_path, "mine", **_SPAWN)
        state_path(tmp_path, "mine").write_text(json.dumps(
            {"phase": "processing", "msg_count": 4, "cost_usd_total": 1.25}
        ))
        write_message(tmp_path, "cld_master_repoA_abcd1234", "mine", "hi", "b")
        row = fleet_digest(tmp_path, _SPAWN["parent"])[0]
        assert (row["phase"], row["msg_count"], row["cost_usd_total"], row["unread"]) == (
            "processing", 4, 1.25, 1,
        )

    def test_missing_state_gets_defaults(self, tmp_path):
        ensure_meta(tmp_path, "mine", **_SPAWN)
        row = fleet_digest(tmp_path, _SPAWN["parent"])[0]
        assert (row["phase"], row["msg_count"], row["cost_usd_total"]) == ("unknown", 0, 0.0)

    def test_last_activity_advances_when_a_message_lands(self, tmp_path):
        ensure_meta(tmp_path, "mine", **_SPAWN)
        before = fleet_digest(tmp_path, _SPAWN["parent"])[0]["last_activity"]
        write_message(tmp_path, "cld_master_repoA_abcd1234", "mine", "hi", "b")
        assert fleet_digest(tmp_path, _SPAWN["parent"])[0]["last_activity"] >= before

    def test_archived_members_absent(self, tmp_path):
        ensure_meta(tmp_path, "mine", **_SPAWN)
        archive_mailbox(tmp_path, "mine")
        assert fleet_digest(tmp_path, _SPAWN["parent"]) == []

    def test_empty_root(self, tmp_path):
        assert fleet_digest(tmp_path / "nope", "whoever") == []


class TestArchiveMailbox:
    def test_moves_whole_dir(self, tmp_path):
        write_message(tmp_path, "master", "agent1", "hi", "body")
        dest = archive_mailbox(tmp_path, "agent1")
        assert dest == tmp_path / "_archive" / "agent1"
        assert not mailbox_dir(tmp_path, "agent1").exists()
        assert (dest / "inbox").is_dir()

    def test_idempotent(self, tmp_path):
        ensure_mailbox(tmp_path, "agent1")
        first = archive_mailbox(tmp_path, "agent1")
        assert archive_mailbox(tmp_path, "agent1") == first

    def test_unknown_name_returns_none(self, tmp_path):
        assert archive_mailbox(tmp_path, "agent1") is None

    def test_collision_suffixes_newcomer(self, tmp_path):
        ensure_mailbox(tmp_path, "agent1")
        archive_mailbox(tmp_path, "agent1")
        ensure_mailbox(tmp_path, "agent1")      # slug re-used: a fresh container at boot
        assert archive_mailbox(tmp_path, "agent1") == tmp_path / "_archive" / "agent1-2"


class TestMailboxReaped:
    def test_live_mailbox(self, tmp_path):
        ensure_mailbox(tmp_path, "agent1")
        assert mailbox_reaped(tmp_path, "agent1") is False

    def test_archived_only(self, tmp_path):
        ensure_mailbox(tmp_path, "agent1")
        archive_mailbox(tmp_path, "agent1")
        assert mailbox_reaped(tmp_path, "agent1") is True

    def test_respawned_name_is_not_reaped(self, tmp_path):
        ensure_mailbox(tmp_path, "agent1")
        archive_mailbox(tmp_path, "agent1")
        ensure_mailbox(tmp_path, "agent1")          # same slug, fresh container
        assert mailbox_reaped(tmp_path, "agent1") is False

    def test_unknown_name(self, tmp_path):
        assert mailbox_reaped(tmp_path, "agent1") is False


class TestResolveMailboxDir:
    def test_prefers_live(self, tmp_path):
        ensure_mailbox(tmp_path, "agent1")
        assert resolve_mailbox_dir(tmp_path, "agent1") == mailbox_dir(tmp_path, "agent1")

    def test_falls_back_to_archive(self, tmp_path):
        ensure_mailbox(tmp_path, "agent1")
        archive_mailbox(tmp_path, "agent1")
        assert resolve_mailbox_dir(tmp_path, "agent1") == tmp_path / "_archive" / "agent1"

    def test_unknown_returns_none(self, tmp_path):
        assert resolve_mailbox_dir(tmp_path, "agent1") is None


class TestTranscript:
    def test_interleaves_sent_and_received_by_ts(self, tmp_path):
        write_message(tmp_path, "master", "agent1", "do this", "task body")
        write_message(tmp_path, "agent1", "master", "Re: do this", "done")
        write_message(tmp_path, "peer", "agent1", "question", "?")
        entries = transcript(tmp_path, "agent1")
        assert [(e["direction"], e["subject"]) for e in entries] == [
            ("in", "do this"), ("out", "Re: do this"), ("in", "question"),
        ]
        assert entries[1]["from"] == "agent1"
        assert entries[1]["body"] == "done"

    def test_includes_archived_received(self, tmp_path):
        msg = write_message(tmp_path, "master", "agent1", "do this", "body")
        archive_message(tmp_path, "agent1", msg["id"])
        assert [e["subject"] for e in transcript(tmp_path, "agent1")] == ["do this"]

    def test_reads_archived_mailbox(self, tmp_path):
        write_message(tmp_path, "master", "agent1", "do this", "body")
        write_message(tmp_path, "agent1", "master", "Re: do this", "done")
        archive_mailbox(tmp_path, "agent1")
        assert len(transcript(tmp_path, "agent1")) == 2

    def test_legacy_outbox_line_without_subject_body(self, tmp_path):
        ensure_mailbox(tmp_path, "agent1")
        with (mailbox_dir(tmp_path, "agent1") / "outbox.log").open("a") as f:
            f.write(json.dumps({"id": "x", "to": "master", "ts": "2026-01-01T00:00:00.000000Z"}) + "\n")
        entry = transcript(tmp_path, "agent1")[0]
        assert (entry["subject"], entry["body"], entry["to"]) == ("", "", "master")

    def test_unknown_name_is_empty(self, tmp_path):
        assert transcript(tmp_path, "agent1") == []

    def test_hops_surface_in_both_directions(self, tmp_path):
        """Received messages carry the stamp in the envelope; sent ones need forwarding."""
        ensure_meta(tmp_path, "agent-a", **{**_SPAWN, "peers": {"agent-b": 5}})
        ensure_meta(tmp_path, "agent-b", **_SPAWN)
        write_message(tmp_path, "agent-a", "agent-b", "out", "x")
        write_message(tmp_path, "agent-b", "agent-a", "in", "x")
        entries = transcript(tmp_path, "agent-a")
        assert [(e["direction"], e.get("hops")) for e in entries] == [("out", 1), ("in", 2)]


class TestEdges:
    def test_path_symmetric(self, tmp_path):
        assert edge_path(tmp_path, "b", "a") == edge_path(tmp_path, "a", "b")

    def test_unused_edge_reads_zero(self, tmp_path):
        assert read_edge(tmp_path, "a", "b")["count"] == 0

    def test_partial_file_is_normalized(self, tmp_path):
        """Resetting an edge in this POC means editing the file by hand (§10)."""
        path = edge_path(tmp_path, "a", "b")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"count": 0}))
        assert read_edge(tmp_path, "a", "b") == {
            "count": 0, "limit": None, "open": [], "asks": 0,
            "root_since": None, "updated": None,
        }

    def test_bump_counts_both_directions_on_one_edge(self, tmp_path):
        assert bump_edge(tmp_path, "a", "b", 3) == 1
        assert bump_edge(tmp_path, "b", "a", 3) == 2
        edge = read_edge(tmp_path, "a", "b")
        assert (edge["count"], edge["limit"]) == (2, 3)

    def test_stored_limit_beats_callers(self, tmp_path):
        bump_edge(tmp_path, "a", "b", 2)                      # declaring side sets 2
        assert bump_edge(tmp_path, "b", "a", 99) == 2         # replier inherits it
        assert read_edge(tmp_path, "a", "b")["limit"] == 2

    def test_bump_without_a_limit_leaves_it_unseeded(self, tmp_path):
        """The old fused primitive raised TypeError here (count > None)."""
        assert bump_edge(tmp_path, "a", "b") == 1
        assert read_edge(tmp_path, "a", "b")["limit"] is None

    def test_unseeded_limit_is_seeded_by_a_later_caller(self, tmp_path):
        bump_edge(tmp_path, "a", "b")
        bump_edge(tmp_path, "a", "b", 5)
        edge = read_edge(tmp_path, "a", "b")
        assert (edge["count"], edge["limit"]) == (2, 5)

    def test_bump_never_refuses(self, tmp_path):
        """Accounting only -- "may it pass" is edge_spent's question, asked before."""
        bump_edge(tmp_path, "a", "b", 1)
        assert bump_edge(tmp_path, "a", "b", 1) == 2
        assert read_edge(tmp_path, "a", "b")["count"] == 2


class TestWriteMessagePeerEdge:
    """The one rule: a spent edge is silent (D29). write_message is the chokepoint."""

    def _fleet(self, tmp_path, a="agent-a", b="agent-b", peers=None):
        ensure_meta(tmp_path, a, **{**_SPAWN, "peers": peers or {}})
        ensure_meta(tmp_path, b, **_SPAWN)
        return a, b

    def test_exempt_edge_is_untouched(self, tmp_path):
        """Either endpoint lacking meta.json -> today's behavior, no counter at all."""
        ensure_meta(tmp_path, "agent-a", **_SPAWN)
        msg = write_message(tmp_path, "agent-a", "cld_master_x", "hi", "b", peer_limit=1)
        assert msg is not None and "hops" not in msg
        assert not (tmp_path / "_edges").exists()

    def test_delivers_up_to_the_limit_then_refuses(self, tmp_path):
        a, b = self._fleet(tmp_path, peers={"agent-b": 2})
        assert write_message(tmp_path, a, b, "one", "x") is not None
        assert write_message(tmp_path, a, b, "two", "x") is not None
        # The limit-th message lands; the next one is the first refused.
        assert write_message(tmp_path, a, b, "three", "x") is None
        assert len(list_inbox(tmp_path, b)) == 2
        assert read_edge(tmp_path, a, b)["count"] == 2

    def test_refusal_writes_nothing(self, tmp_path):
        a, b = self._fleet(tmp_path, peers={"agent-b": 1})
        write_message(tmp_path, a, b, "one", "x")
        before = len(_outbox_lines(tmp_path, a))
        assert write_message(tmp_path, a, b, "two", "x") is None
        assert len(list_inbox(tmp_path, b)) == 1
        assert len(_outbox_lines(tmp_path, a)) == before
        assert read_edge(tmp_path, a, b)["count"] == 1

    def test_hops_stamped_on_envelope_and_outbox_line(self, tmp_path):
        a, b = self._fleet(tmp_path, peers={"agent-b": 5})
        msg = write_message(tmp_path, a, b, "one", "x")
        assert msg["hops"] == 1
        assert read_message(tmp_path, b, msg["id"])["hops"] == 1
        assert _outbox_lines(tmp_path, a)[0]["hops"] == 1

    def test_both_directions_share_one_counter(self, tmp_path):
        a, b = self._fleet(tmp_path, peers={"agent-b": 3})
        assert write_message(tmp_path, a, b, "one", "x")["hops"] == 1
        assert write_message(tmp_path, b, a, "two", "x")["hops"] == 2
        assert write_message(tmp_path, a, b, "three", "x")["hops"] == 3
        assert write_message(tmp_path, b, a, "four", "x") is None

    def test_replier_inherits_the_declared_limit(self, tmp_path):
        """B never declared the edge, so its own default must not widen it."""
        a, b = self._fleet(tmp_path, peers={"agent-b": 1})
        write_message(tmp_path, a, b, "one", "x")
        assert write_message(tmp_path, b, a, "reply", "x", peer_limit=99) is None

    def test_reaped_recipient_is_refused_on_an_open_edge(self, tmp_path):
        """Delivering would resurrect the dir, shadow the archived meta, un-budget the edge."""
        a, b = self._fleet(tmp_path, peers={"agent-b": 5})
        write_message(tmp_path, a, b, "one", "x")
        archive_mailbox(tmp_path, b)
        assert write_message(tmp_path, a, b, "two", "x") is None
        assert not mailbox_dir(tmp_path, b).exists()
        assert read_edge(tmp_path, a, b)["count"] == 1

    def test_reaped_peer_keeps_its_spent_edge(self, tmp_path):
        """Reaping a peer must not un-budget the edge it was on, nor resurrect its mailbox."""
        a, b = self._fleet(tmp_path, peers={"agent-b": 1})
        write_message(tmp_path, a, b, "one", "x")
        archive_mailbox(tmp_path, b)                      # b reaped mid-exchange
        assert write_message(tmp_path, a, b, "two", "x") is None
        assert not mailbox_dir(tmp_path, b).exists()

    def test_the_invariant(self, tmp_path):
        """At most `limit` messages ever cross a peer edge, whoever sends them.

        Alternating agent sends with supervisor-style direct writes -- the whole §C.3
        argument in one assertion, since it is exactly the mix that used to loop.
        """
        a, b = self._fleet(tmp_path, peers={"agent-b": 4})
        delivered = 0
        for i in range(20):
            frm, to = (a, b) if i % 2 == 0 else (b, a)
            if write_message(tmp_path, frm, to, f"m{i}", "x") is not None:
                delivered += 1
        assert delivered == 4
        assert len(list_inbox(tmp_path, a)) + len(list_inbox(tmp_path, b)) == 4


class TestEdgeSpent:
    def test_no_file(self, tmp_path):
        assert edge_spent(tmp_path, "a", "b") is False

    def test_below_the_limit(self, tmp_path):
        bump_edge(tmp_path, "a", "b", 2)
        assert edge_spent(tmp_path, "a", "b") is False

    def test_at_the_limit(self, tmp_path):
        bump_edge(tmp_path, "a", "b", 2)
        bump_edge(tmp_path, "a", "b", 2)
        assert edge_spent(tmp_path, "a", "b") is True

    def test_symmetric(self, tmp_path):
        bump_edge(tmp_path, "a", "b", 1)
        assert edge_spent(tmp_path, "b", "a") is True

    def test_unseeded_limit_is_never_spent(self, tmp_path):
        bump_edge(tmp_path, "a", "b")
        bump_edge(tmp_path, "a", "b")
        assert edge_spent(tmp_path, "a", "b") is False


class TestObligationLedger:
    """Reply obligation is declared, not implied: expects_reply opens it, answers closes it."""

    def _fleet(self, tmp_path, peers=None):
        ensure_meta(tmp_path, "agent-a", **{**_SPAWN, "peers": peers or {"agent-b": 50}})
        ensure_meta(tmp_path, "agent-b", **_SPAWN)
        return "agent-a", "agent-b"

    def test_envelope_and_outbox_carry_the_fields(self, tmp_path):
        msg = write_message(tmp_path, "a", "b", "q", "?", answers="m0", expects_reply=True)
        assert (msg["answers"], msg["expects_reply"]) == ("m0", True)
        assert read_message(tmp_path, "b", msg["id"])["expects_reply"] is True
        assert _outbox_lines(tmp_path, "a")[0]["answers"] == "m0"

    def test_plain_message_opens_nothing(self, tmp_path):
        a, b = self._fleet(tmp_path)
        write_message(tmp_path, a, b, "fyi", "x")
        edge = read_edge(tmp_path, a, b)
        assert (edge["open"], edge["asks"], edge["root_since"]) == ([], 0, None)

    def test_ask_opens_and_answer_closes(self, tmp_path):
        a, b = self._fleet(tmp_path)
        q = write_message(tmp_path, a, b, "q", "?", expects_reply=True)
        opened = read_edge(tmp_path, a, b)
        assert (opened["open"], opened["asks"]) == ([q["id"]], 1)
        assert opened["root_since"]
        write_message(tmp_path, b, a, "re", "answer", answers=q["id"])
        closed = read_edge(tmp_path, a, b)
        assert (closed["open"], closed["asks"], closed["root_since"]) == ([], 0, None)

    def test_clarification_sub_dialogue_settles(self, tmp_path):
        """The worked trace: ask, clarify, clarify-answer, answer -- ends fully discharged.

        The root obligation survives the nested exchange (step 3 discharges only the
        clarification), which is why a reply must be allowed to oblige a reply.
        """
        a, b = self._fleet(tmp_path)
        m1 = write_message(tmp_path, a, b, "schema?", "?", expects_reply=True)
        m2 = write_message(tmp_path, b, a, "which version?", "?", expects_reply=True)
        assert read_edge(tmp_path, a, b)["open"] == [m1["id"], m2["id"]]
        write_message(tmp_path, a, b, "v2", "x", answers=m2["id"])
        assert read_edge(tmp_path, a, b)["open"] == [m1["id"]]
        write_message(tmp_path, b, a, "here it is", "x", answers=m1["id"])
        edge = read_edge(tmp_path, a, b)
        assert (edge["open"], edge["asks"], edge["count"]) == ([], 0, 4)

    def test_asks_do_not_reset_while_the_root_stays_open(self, tmp_path):
        """Discharge-and-reopen keeps the *depth* at one, which is why depth is not the metric."""
        a, b = self._fleet(tmp_path)
        m1 = write_message(tmp_path, a, b, "q1", "?", expects_reply=True)
        m2 = write_message(tmp_path, b, a, "q2", "?", expects_reply=True)
        write_message(tmp_path, a, b, "a2+q3", "?", answers=m2["id"], expects_reply=True)
        edge = read_edge(tmp_path, a, b)
        assert len(edge["open"]) == 2 and edge["asks"] == 3
        assert edge["open"][0] == m1["id"]

    def test_root_since_survives_a_partial_discharge(self, tmp_path):
        a, b = self._fleet(tmp_path)
        write_message(tmp_path, a, b, "q1", "?", expects_reply=True)
        opened = read_edge(tmp_path, a, b)["root_since"]
        m2 = write_message(tmp_path, b, a, "q2", "?", expects_reply=True)
        write_message(tmp_path, a, b, "a2", "x", answers=m2["id"])
        assert read_edge(tmp_path, a, b)["root_since"] == opened

    def test_answers_on_an_unknown_id_is_a_no_op(self, tmp_path):
        a, b = self._fleet(tmp_path)
        q = write_message(tmp_path, a, b, "q", "?", expects_reply=True)
        write_message(tmp_path, b, a, "unrelated", "x", answers="not-an-id")
        assert read_edge(tmp_path, a, b)["open"] == [q["id"]]

    def test_master_edge_keeps_no_ledger(self, tmp_path):
        ensure_meta(tmp_path, "agent-a", **_SPAWN)
        ensure_mailbox(tmp_path, "cld_master_x")
        write_message(tmp_path, "agent-a", "cld_master_x", "q", "?", expects_reply=True)
        assert not (tmp_path / "_edges").exists()


class TestAskSpent:
    def test_nothing_open(self, tmp_path):
        assert ask_spent(tmp_path, "a", "b", 3) is False

    def test_below_the_limit(self, tmp_path):
        bump_edge(tmp_path, "a", "b", msg_id="m1", expects_reply=True)
        assert ask_spent(tmp_path, "a", "b", 3) is False

    def test_at_the_limit(self, tmp_path):
        for i in range(3):
            bump_edge(tmp_path, "a", "b", msg_id=f"m{i}", expects_reply=True)
        assert ask_spent(tmp_path, "a", "b", 3) is True

    def test_symmetric(self, tmp_path):
        bump_edge(tmp_path, "a", "b", msg_id="m1", expects_reply=True)
        assert ask_spent(tmp_path, "b", "a", 1) is True

    def test_discharging_the_last_open_question_clears_it(self, tmp_path):
        for i in range(3):
            bump_edge(tmp_path, "a", "b", msg_id=f"m{i}", expects_reply=True)
        for i in range(3):
            bump_edge(tmp_path, "a", "b", answers=f"m{i}")
        assert ask_spent(tmp_path, "a", "b", 3) is False
        assert read_edge(tmp_path, "a", "b")["asks"] == 0


class TestEdgeObligations:
    def test_quiet_edges_report_nothing(self, tmp_path):
        assert edge_obligations(tmp_path, "a", ["b", "c"]) == {
            "open_asks": 0, "open_with": [], "oldest_open": "",
        }

    def test_sums_across_peers_and_names_them(self, tmp_path):
        bump_edge(tmp_path, "a", "b", msg_id="m1", expects_reply=True)
        bump_edge(tmp_path, "a", "b", msg_id="m2", expects_reply=True)
        bump_edge(tmp_path, "a", "c", msg_id="m3", expects_reply=True)
        result = edge_obligations(tmp_path, "a", ["b", "c"])
        assert (result["open_asks"], result["open_with"]) == (3, ["b", "c"])
        assert result["oldest_open"]

    def test_ignores_edges_to_agents_not_asked_about(self, tmp_path):
        bump_edge(tmp_path, "a", "c", msg_id="m1", expects_reply=True)
        assert edge_obligations(tmp_path, "a", ["b"])["open_asks"] == 0


class TestGatedSend:
    """The gate both instructed send paths call (MCP tool + `python -m cld.messenger.send`)."""

    def _fleet(self, tmp_path, peers=None):
        ensure_meta(tmp_path, "agent-a", **{**_SPAWN, "peers": peers or {}})
        ensure_meta(tmp_path, "agent-b", **_SPAWN)
        return "agent-a", "agent-b"

    def test_exempt_send_to_master(self, tmp_path):
        ensure_meta(tmp_path, "agent-a", **_SPAWN)
        ensure_mailbox(tmp_path, "cld_master_x")
        result = gated_send(tmp_path, "agent-a", "cld_master_x", "hi", "b", default_limit=10, ask_limit=3)
        assert set(result) == {"id", "to"}
        assert not (tmp_path / "_edges").exists()

    def test_budgeted_send_reports_position(self, tmp_path):
        a, b = self._fleet(tmp_path, peers={"agent-b": 3})
        result = gated_send(tmp_path, a, b, "hi", "body", default_limit=10, ask_limit=3)
        assert (result["hops"], result["limit"], result["to"]) == (1, 3, b)

    def test_default_limit_when_peer_unlisted(self, tmp_path):
        a, b = self._fleet(tmp_path)
        assert gated_send(tmp_path, a, b, "hi", "b", default_limit=7, ask_limit=3)["limit"] == 7

    def test_replier_inherits_the_declared_limit(self, tmp_path):
        a, b = self._fleet(tmp_path, peers={"agent-b": 2})
        gated_send(tmp_path, a, b, "hi", "b", default_limit=10, ask_limit=3)
        assert gated_send(tmp_path, b, a, "re", "b", default_limit=99, ask_limit=3)["limit"] == 2

    def test_ask_position_reported_while_something_is_open(self, tmp_path):
        a, b = self._fleet(tmp_path, peers={"agent-b": 9})
        plain = gated_send(tmp_path, a, b, "fyi", "b", default_limit=10, ask_limit=3)
        assert "open_asks" not in plain
        asked = gated_send(tmp_path, a, b, "q", "?", default_limit=10, ask_limit=3, expects_reply=True)
        assert (asked["open_asks"], asked["ask_limit"]) == (1, 3)

    def test_ask_refused_past_the_limit(self, tmp_path):
        a, b = self._fleet(tmp_path, peers={"agent-b": 99})
        for i in range(2):
            gated_send(tmp_path, a, b, f"q{i}", "?", default_limit=10, ask_limit=2, expects_reply=True)
        result = gated_send(tmp_path, a, b, "q2", "?", default_limit=10, ask_limit=2, expects_reply=True)
        assert "ask limit 2" in result["error"]
        assert "state the assumption" in result["error"]
        assert _SPAWN["parent"] in result["error"]
        assert len(list_inbox(tmp_path, b)) == 2

    def test_spent_ask_budget_still_lets_the_exchange_land(self, tmp_path):
        """The ask gate refuses *asking*, never speaking -- that is what keeps a landing free.

        Contrast with the hop budget, which closes the edge outright. Here an answer and a
        plain update both still deliver, so an agent that is told to commit can.
        """
        a, b = self._fleet(tmp_path, peers={"agent-b": 99})
        q = gated_send(tmp_path, a, b, "q0", "?", default_limit=10, ask_limit=1, expects_reply=True)
        assert "error" in gated_send(
            tmp_path, b, a, "q1", "?", default_limit=10, ask_limit=1, expects_reply=True,
        )
        assert "error" not in gated_send(tmp_path, b, a, "fyi", "x", default_limit=10, ask_limit=1)
        answer = gated_send(
            tmp_path, b, a, "committing", "assumed v2", default_limit=10, ask_limit=1,
            answers=q["id"],
        )
        assert "error" not in answer
        assert ask_spent(tmp_path, a, b, 1) is False

    def test_refusal_names_the_master_and_the_position(self, tmp_path):
        a, b = self._fleet(tmp_path, peers={"agent-b": 1})
        gated_send(tmp_path, a, b, "hi", "b", default_limit=10, ask_limit=3)
        result = gated_send(tmp_path, a, b, "again", "b", default_limit=10, ask_limit=3)
        assert "1/1" in result["error"]
        assert _SPAWN["parent"] in result["error"]
        assert "Do not retry" in result["error"]
        assert len(list_inbox(tmp_path, b)) == 1        # nothing delivered, no cap notice

    def test_refusal_falls_back_to_generic_master(self, tmp_path):
        ensure_meta(tmp_path, "agent-a", **{**_SPAWN, "parent": "", "peers": {"agent-b": 1}})
        ensure_meta(tmp_path, "agent-b", **_SPAWN)
        gated_send(tmp_path, "agent-a", "agent-b", "hi", "b", default_limit=10, ask_limit=3)
        result = gated_send(tmp_path, "agent-a", "agent-b", "again", "b", default_limit=10, ask_limit=3)
        assert "your master" in result["error"]

    def test_reaped_recipient_gets_its_own_error(self, tmp_path):
        """Not the hop-budget message -- the edge is open, the recipient is simply gone.

        Reachable when the container is still up but its mailbox has been archived (a
        partial or hand-run reap): resolution succeeds, so the refusal comes from the
        transport and must say why. A fully reaped agent fails earlier, at resolution.
        """
        a, b = self._fleet(tmp_path, peers={"agent-b": 5})
        gated_send(tmp_path, a, b, "hi", "b", default_limit=10, ask_limit=3)
        archive_mailbox(tmp_path, b)
        with patch("cld.messenger.mailbox.list_containers", return_value=[
            {"name": b, "kind": "task-agent", "repo": "/x/repo", "status": "running"},
        ]):
            result = gated_send(tmp_path, a, b, "again", "b", default_limit=10, ask_limit=3)
        assert "torn down" in result["error"]
        assert "hop budget" not in result["error"]

    def test_fully_reaped_recipient_fails_at_resolution(self, tmp_path):
        a, b = self._fleet(tmp_path, peers={"agent-b": 5})
        archive_mailbox(tmp_path, b)
        with patch("cld.messenger.mailbox.list_containers", return_value=[]):
            result = gated_send(tmp_path, a, b, "hi", "b", default_limit=10, ask_limit=3)
        assert "No container found" in result["error"]

    def test_unresolvable_recipient_is_an_error_not_an_exception(self, tmp_path):
        with patch("cld.messenger.mailbox.list_containers", return_value=[]):
            result = gated_send(tmp_path, "agent-a", "nobody", "hi", "b", default_limit=10, ask_limit=3)
        assert "error" in result


class TestListContainers:
    """mailbox.list_containers delegates to the host-docker seam; on the host
    (in_master_container False) that hits the local daemon."""

    def test_parses_docker_output(self, tmp_path):
        ps_result = type("R", (), {
            "returncode": 0,
            "stdout": "a.t1.repoA\tUp 2 hours\nt.lide-2600\tExited (0) 1 hour ago\n",
            "stderr": "",
        })()
        inspect_results = [
            type("R", (), {"returncode": 0, "stdout": "agent|/home/u/repoA\n"})(),
            type("R", (), {"returncode": 0, "stdout": "ticket|/home/u/repoB\n"})(),
        ]
        with patch("cld.broker.in_master_container", return_value=False), \
             patch("cld.broker.subprocess.run", side_effect=[ps_result, *inspect_results]):
            containers = list_containers()
        assert containers == [
            {"name": "a.t1.repoA", "kind": "agent", "repo": "/home/u/repoA", "status": "running"},
            {"name": "t.lide-2600", "kind": "ticket", "repo": "/home/u/repoB", "status": "stopped"},
        ]

    def test_docker_failure_returns_empty(self, tmp_path):
        fail_result = type("R", (), {"returncode": 1, "stdout": "", "stderr": "docker not found"})()
        with patch("cld.broker.in_master_container", return_value=False), \
             patch("cld.broker.subprocess.run", return_value=fail_result):
            assert list_containers() == []


class TestResolveRecipient:
    _CONTAINERS = [
        {"name": "a.t1.repoA", "kind": "agent", "repo": "/home/u/repoA", "status": "running"},
        {"name": "a.repoA.add-oauth", "kind": "task-agent", "repo": "/home/u/repoA", "status": "running"},
        {"name": "a.repoB.fix-tests", "kind": "task-agent", "repo": "/x/repoB", "status": "running"},
    ]

    def test_full_name_used_verbatim(self):
        assert resolve_recipient("a.repoB.fix-tests", self._CONTAINERS) == "a.repoB.fix-tests"

    def test_existing_mailbox_short_circuits_without_enumeration(self, tmp_path):
        # Reply path: `to` names an existing mailbox dir -> return verbatim and
        # never call list_containers (so agents can reply with no host channel).
        (tmp_path / "a.t1.repoA").mkdir()
        with patch("cld.messenger.mailbox.list_containers", side_effect=AssertionError("enumerated")):
            assert resolve_recipient("a.t1.repoA", root=tmp_path) == "a.t1.repoA"

    def test_shortname_prefers_the_standing_agent(self):
        assert resolve_recipient("repoA", self._CONTAINERS) == "a.t1.repoA"

    def test_shortname_resolves_to_only_match(self):
        assert resolve_recipient("repoB", self._CONTAINERS) == "a.repoB.fix-tests"

    def test_unknown_shortname_raises(self):
        with pytest.raises(ValueError, match="No container found"):
            resolve_recipient("nonexistent", self._CONTAINERS)

    def test_ambiguous_shortname_raises(self):
        containers = self._CONTAINERS + [
            {"name": "a.repoA2.zz", "kind": "task-agent", "repo": "/other/repoA", "status": "running"},
        ]
        with pytest.raises(ValueError, match="Ambiguous shortname"):
            resolve_recipient("repoA", containers)

    _TASK_AGENTS = [
        {"name": "a.repoC.add-oauth", "kind": "task-agent", "repo": "/home/u/repoC", "status": "running"},
        {"name": "a.repoC.fix-tests", "kind": "task-agent", "repo": "/home/u/repoC", "status": "running"},
    ]

    def test_shortname_matching_several_task_agents_raises(self):
        """Picking the first would be a silent misdelivery to an arbitrary agent."""
        with pytest.raises(ValueError, match="task-agents share that repo"):
            resolve_recipient("repoC", self._TASK_AGENTS)

    def test_repo_agent_still_wins_over_task_agents(self):
        containers = self._TASK_AGENTS + [
            {"name": "a.t1.repoC", "kind": "agent", "repo": "/home/u/repoC", "status": "running"},
        ]
        assert resolve_recipient("repoC", containers) == "a.t1.repoC"

    def test_single_task_agent_shortname_still_resolves(self):
        assert resolve_recipient("repoC", self._TASK_AGENTS[:1]) == "a.repoC.add-oauth"

    # Ticket containers (v2): the slug is a shortname too, resolved after the
    # exact container name and before repo basenames; a collision between a
    # slug and a basename is an ambiguity error naming both
    # (design-ticket-containers.md section 6.5).
    _TICKETS = [
        {"name": "t.lide-2600", "kind": "ticket", "repo": "", "status": "running"},
    ]

    def test_ticket_slug_resolves_to_the_ticket_container(self):
        assert resolve_recipient("lide-2600", self._CONTAINERS + self._TICKETS) \
            == "t.lide-2600"

    def test_ticket_full_name_still_used_verbatim(self):
        assert resolve_recipient("t.lide-2600", self._TICKETS) \
            == "t.lide-2600"

    def test_slug_equal_to_repo_basename_is_ambiguous_naming_both(self):
        containers = self._CONTAINERS + [
            {"name": "t.repoa", "kind": "ticket", "repo": "", "status": "running"},
        ]
        with pytest.raises(ValueError) as exc:
            resolve_recipient("repoA", containers)
        assert "t.repoa" in str(exc.value)
        assert "a.t1.repoA" in str(exc.value)

    def test_ticket_repo_field_never_matches_as_basename(self):
        """A ticket's record carries no repo-root; its (empty) repo must not
        accidentally match anything, and unknown names still error."""
        with pytest.raises(ValueError, match="No container found"):
            resolve_recipient("nonexistent", self._TICKETS)
