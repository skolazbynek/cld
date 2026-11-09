"""Identifier builders (docs/design-bookmark-naming.md)."""

import re

import pytest

from cld import names


class TestTicketSlug:
    def test_lowercases(self):
        assert names.ticket_slug("LIDE-2600") == "lide-2600"

    def test_sanitizes(self):
        assert names.ticket_slug("My Ticket_v2!") == "my-ticket-v2"

    def test_strips_edge_dashes(self):
        assert names.ticket_slug("--x--") == "x"

    def test_idempotent(self):
        assert names.ticket_slug("lide-2600") == "lide-2600"

    @pytest.mark.parametrize("ticket", ["", "!!!", "---"])
    def test_empty_slug_refused(self, ticket):
        with pytest.raises(ValueError, match="empty slug"):
            names.ticket_slug(ticket)


class TestIds:
    def test_ticket(self):
        assert names.ticket_id("LIDE-2600") == "t.lide-2600"

    def test_ticket_idempotent(self):
        assert names.ticket_id("lide-2600") == "t.lide-2600"

    def test_run(self):
        assert names.run_id("ab12cd") == "run.ab12cd"

    def test_chain(self):
        assert names.chain_id("rdi") == "chain.rdi"

    def test_step(self):
        assert names.step_id("rdi", "build") == "chain.rdi.build"


class TestAgentId:
    def test_with_ticket(self):
        assert names.agent_id("lide-1", "add-oauth") == "a.lide-1.add-oauth"

    def test_no_ticket_is_host(self):
        assert names.agent_id(None, "add-oauth") == "a._host.add-oauth"

    @pytest.mark.parametrize("suffix", [0, 1])
    def test_no_dash_suffix_for_first(self, suffix):
        assert names.agent_id("t1", "x", suffix) == "a.t1.x"

    def test_dash_suffix_from_two(self):
        assert names.agent_id("t1", "x", 2) == "a.t1.x-2"
        assert names.agent_id(None, "x", 3) == "a._host.x-3"

    @pytest.mark.parametrize("slug", ["", "Bad", "has.dot", "-lead", "has space", "a/b"])
    def test_bad_slug_refused(self, slug):
        with pytest.raises(ValueError, match="invalid task slug"):
            names.agent_id("t1", slug)


class TestTicketOf:
    @pytest.mark.parametrize(
        ("identifier", "ticket"),
        [
            ("t.lide-1", "lide-1"),
            ("a.lide-1.review", "lide-1"),
            ("a.lide-1.review-2", "lide-1"),
            ("a._host.review", None),
            ("run.ab12cd", None),
            ("chain.rdi", None),
            ("chain.rdi.build", None),
            ("", None),
            ("t", None),
            ("t.", None),
        ],
        ids=["ticket", "agent", "agent-suffix", "host-agent", "run", "chain", "step", "empty", "bare-t", "empty-ticket"],
    )
    def test_ticket_of(self, identifier, ticket):
        assert names.ticket_of(identifier) == ticket


class TestDeliverable:
    def test_default(self):
        assert names.deliverable("a.t1.x") == "a.t1.x.mr"
        assert names.deliverable("a.t1.x", "") == "a.t1.x.mr"

    def test_without_dot_is_appended(self):
        assert names.deliverable("a.t1.x", "mr-fix") == "a.t1.x.mr-fix"

    def test_with_dot_is_used_as_is(self):
        assert names.deliverable("a.t1.x", "t.t1.mr.migration") == "t.t1.mr.migration"

    @pytest.mark.parametrize("branch", ["a/b", "a..b", ".x", "x.", "x.lock", "a.b.lock"])
    def test_bad_branch_refused(self, branch):
        with pytest.raises(ValueError, match="invalid name"):
            names.deliverable("a.t1.x", branch)


class TestCheckPart:
    @pytest.mark.parametrize("part", ["x", "mr-fix", "a.b", "lockx", "a.lockb"])
    def test_accepts(self, part):
        names.check_part(part)

    @pytest.mark.parametrize(
        "part", ["", ".", "/", "..", "a/b", "a..b", ".a", "a.", "a.lock", ".lock"],
    )
    def test_rejects(self, part):
        with pytest.raises(ValueError, match="invalid name"):
            names.check_part(part)


class TestDockerNameFilter:
    def test_anchored_and_escaped(self):
        assert names.docker_name_filter("t.us") == r"name=^t\.us$"

    def test_does_not_match_other_character(self):
        pattern = names.docker_name_filter("t.us").removeprefix("name=")
        assert re.search(pattern, "t.us")
        assert not re.search(pattern, "tXus")
        assert not re.search(pattern, "t.us-v2")
        assert not re.search(pattern, "xt.us")
