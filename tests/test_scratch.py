"""Unit tests for cld.vcs.scratch peer-side staging.

Staging now happens inside the peer container's ephemeral workspace at
/workspace/current (a jj secondary workspace pointing at A). The tests use a
real jj secondary workspace under tmp_path to exercise the same code path.
"""

import os
import subprocess
from pathlib import Path

import pytest

from cld.names import anchor_ref
from cld.vcs.anchor import find_anchor_commit, move_anchor_commit, revert_ops
from cld.vcs.jj import JjBackend
from cld.vcs.scratch import (
    SCRATCH_DIR,
    decode_scratch_envelope,
    encode_scratch_envelope,
    stage_from_env,
    stage_in_workspace,
)


def _add_second_commit(path: Path) -> str:
    """Advance the seed repo by one commit; return the parent commit hash (A)."""
    (path / "a.txt").write_text("hello\n")
    subprocess.run(
        ["jj", "commit", "-m", "add a.txt"],
        cwd=path, check=True, capture_output=True,
    )
    result = subprocess.run(
        ["jj", "log", "-r", "@-", "--no-graph", "-T", "commit_id"],
        cwd=path, check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _origin_wc_commit(origin_root: Path) -> str:
    """Return the commit id currently at the origin main workspace's @."""
    return subprocess.run(
        ["jj", "log", "-r", "@", "--no-graph", "-T", "commit_id"],
        cwd=origin_root, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _make_peer_workspace(origin: JjBackend, anchor: str, tmp_path: Path) -> Path:
    """Create a jj secondary workspace at *anchor* (mimics the container's `jj workspace add`)."""
    peer = tmp_path / "peer-workspace"
    subprocess.run(
        ["jj", "workspace", "add", "--name", "peer", "-r", anchor, str(peer)],
        cwd=origin.repo_root, check=True, capture_output=True,
    )
    return peer


class TestStageInWorkspace:
    def test_produces_child_of_anchor_with_scratch(self, jj_repo, tmp_path):
        anchor = _add_second_commit(jj_repo.repo_root)
        peer = _make_peer_workspace(jj_repo, anchor, tmp_path)

        b_hash = stage_in_workspace(
            peer, "sess_a",
            {"task.md": b"# task body\n", "sub/x.txt": b"y\n"},
        )

        parents = subprocess.run(
            ["jj", "log", "-r", b_hash, "--no-graph", "-T",
             'parents.map(|p| p.commit_id()).join(",")'],
            cwd=jj_repo.repo_root, check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert anchor in parents

        summary = subprocess.run(
            ["jj", "log", "-r", b_hash, "--no-graph", "--summary", "-T", '""'],
            cwd=jj_repo.repo_root, check=True, capture_output=True, text=True,
        ).stdout
        assert f"{SCRATCH_DIR}/task.md" in summary
        assert f"{SCRATCH_DIR}/sub/x.txt" in summary

    def test_origin_working_copy_untouched(self, jj_repo, tmp_path):
        """The critical invariant: origin's @ (main workspace WC) does not move."""
        anchor = _add_second_commit(jj_repo.repo_root)
        pre = _origin_wc_commit(jj_repo.repo_root)

        peer = _make_peer_workspace(jj_repo, anchor, tmp_path)
        stage_in_workspace(peer, "sess_b", {"session": b"sess_b\n"})

        post = _origin_wc_commit(jj_repo.repo_root)
        assert post == pre, "peer-side staging must not move origin's @"
        # And no .cld-run/ leaked into the origin working copy.
        assert not (jj_repo.repo_root / SCRATCH_DIR).exists()

    def test_at_is_a_case_does_not_rewrite_anchor(self, jj_repo, tmp_path):
        """The typical jj case: user's origin @ IS the anchor A. Staging must not rewrite A."""
        anchor = _origin_wc_commit(jj_repo.repo_root)  # @ IS A
        peer = _make_peer_workspace(jj_repo, anchor, tmp_path)

        b_hash = stage_in_workspace(peer, "sess_c", {"session": b"sess_c\n"})

        # A remains visible at its original commit hash (not rewritten, not hidden).
        vis = subprocess.run(
            ["jj", "log", "-r", "all()", "--no-graph", "-T",
             "commit_id ++ \"\\n\""],
            cwd=jj_repo.repo_root, check=True, capture_output=True, text=True,
        ).stdout
        assert anchor in vis
        # B is a distinct child of A.
        assert b_hash != anchor
        parents = subprocess.run(
            ["jj", "log", "-r", b_hash, "--no-graph", "-T",
             'parents.map(|p| p.commit_id()).join(",")'],
            cwd=jj_repo.repo_root, check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert anchor in parents

    def test_empty_payload_stages_an_empty_b(self, jj_repo, tmp_path):
        """No payload: B is empty, and a stray working-copy file stays out of it."""
        anchor = _add_second_commit(jj_repo.repo_root)
        peer = _make_peer_workspace(jj_repo, anchor, tmp_path)
        (peer / "stray.txt").write_text("not scratch\n")

        b_id = stage_in_workspace(peer, "sess_empty", {})

        assert _rev(jj_repo, b_id, "empty") == "true"
        assert "cld anchor: sess_empty mode=isolated" in _rev(jj_repo, b_id, "description")
        assert not (peer / SCRATCH_DIR).exists()

    def test_returns_the_change_id_of_b(self, jj_repo, tmp_path):
        """A host-side rebase of B rewrites its commit id, never its change id."""
        anchor = _add_second_commit(jj_repo.repo_root)
        peer = _make_peer_workspace(jj_repo, anchor, tmp_path)
        b_id = stage_in_workspace(peer, "sess_id", {"session": b"sess_id\n"})
        assert b_id == _rev(jj_repo, "description(glob:'cld anchor: sess_id *')", "change_id")


def _git(repo, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo.repo_root), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def _make_git_peer(repo, base: str, tmp_path: Path, branch: str = "peer") -> Path:
    """A git worktree at *base* (mimics the container's `git worktree add -b`)."""
    peer = tmp_path / "peer-worktree"
    _git(repo, "worktree", "add", "--no-track", "-b", branch, str(peer), base)
    return peer


class TestStageInWorktree:
    def test_stages_a_child_of_the_base_with_the_message(self, git_repo, tmp_path):
        base = _git(git_repo, "rev-parse", "HEAD")
        peer = _make_git_peer(git_repo, base, tmp_path)

        s_hash = stage_in_workspace(peer, "t.sess", {"task.md": b"# body\n"}, mode="shared")

        assert _git(git_repo, "rev-parse", f"{s_hash}^") == base
        assert _git(git_repo, "log", "-1", "--format=%B", s_hash) == "cld anchor: t.sess mode=shared"
        assert _git(git_repo, "show", "--name-only", "--format=", s_hash) == f"{SCRATCH_DIR}/task.md"
        assert _git(git_repo, "rev-parse", "peer") == s_hash

    def test_empty_payload_stages_an_empty_commit_and_skips_strays(self, git_repo, tmp_path):
        base = _git(git_repo, "rev-parse", "HEAD")
        peer = _make_git_peer(git_repo, base, tmp_path)
        (peer / "stray.txt").write_text("not scratch\n")

        s_hash = stage_in_workspace(peer, "t.sess", {})

        assert _git(git_repo, "diff", "--name-only", f"{s_hash}^", s_hash) == ""
        assert (peer / "stray.txt").exists()

    def test_pins_the_commit_by_the_hidden_ref(self, git_repo, tmp_path):
        base = _git(git_repo, "rev-parse", "HEAD")
        peer = _make_git_peer(git_repo, base, tmp_path)

        s_hash = stage_in_workspace(peer, "t.sess", {})

        assert _git(git_repo, "rev-parse", anchor_ref("t.sess")) == s_hash
        assert anchor_ref("t.sess") not in _git(git_repo, "branch", "--all", "--format=%(refname)")

    def test_an_existing_ref_is_not_overwritten(self, git_repo, tmp_path):
        base = _git(git_repo, "rev-parse", "HEAD")
        peer = _make_git_peer(git_repo, base, tmp_path)
        _git(git_repo, "update-ref", anchor_ref("t.sess"), base)

        with pytest.raises(RuntimeError, match="update-ref"):
            stage_in_workspace(peer, "t.sess", {})

        assert _git(git_repo, "rev-parse", anchor_ref("t.sess")) == base

    def test_a_failing_pre_commit_hook_does_not_break_it(self, git_repo, tmp_path):
        hook = git_repo.repo_root / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        peer = _make_git_peer(git_repo, _git(git_repo, "rev-parse", "HEAD"), tmp_path)

        stage_in_workspace(peer, "t.sess", {"task.md": b"x\n"})

    def test_signing_configured_does_not_break_it(self, git_repo, tmp_path):
        _git(git_repo, "config", "commit.gpgsign", "true")
        _git(git_repo, "config", "gpg.program", "false")
        peer = _make_git_peer(git_repo, _git(git_repo, "rev-parse", "HEAD"), tmp_path)

        stage_in_workspace(peer, "t.sess", {})

    def test_no_identity_fails_loudly(self, git_repo, tmp_path, monkeypatch):
        peer = _make_git_peer(git_repo, _git(git_repo, "rev-parse", "HEAD"), tmp_path)
        _git(git_repo, "config", "--unset", "user.name")
        _git(git_repo, "config", "--unset", "user.email")
        _git(git_repo, "config", "user.useConfigOnly", "true")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"):
            monkeypatch.delenv(var, raising=False)

        with pytest.raises(RuntimeError, match="git commit failed"):
            stage_in_workspace(peer, "t.sess", {})


_VCS_LIB =Path(__file__).resolve().parent.parent / "imgs/claude-devcontainer/vcs-lib.sh"


class TestRecoverAnchorSessionGlob:
    """cld_recover_anchor (vcs-lib.sh) globs for its own session's scratch
    description; the glob must not match a sibling session whose name extends
    this one (t.x-1 vs t.x-12)."""

    def _recover(self, repo_root: Path, bookmark: str, session: str) -> str:
        result = subprocess.run(
            ["bash", "-c",
             f'source "{_VCS_LIB}" && cd "{repo_root}" && '
             f'cld_recover_anchor "{bookmark}" "{session}"'],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def _staged_sibling(self, jj_repo, tmp_path) -> str:
        """Scratch commit of session t.x-12, with a work commit on
        top, carrying bookmark t.x-1 -- the sibling's scratch is at
        the probed bookmark. Returns the scratch commit id."""
        anchor = _add_second_commit(jj_repo.repo_root)
        peer = _make_peer_workspace(jj_repo, anchor, tmp_path)
        scratch = stage_in_workspace(peer, "t.x-12", {"session": b"t.x-12\n"})
        (peer / "work.txt").write_text("work\n")
        subprocess.run(
            ["jj", "commit", "-m", "work"], cwd=peer, check=True, capture_output=True,
        )
        subprocess.run(
            ["jj", "bookmark", "create", "t.x-1", "-r", scratch],
            cwd=peer, check=True, capture_output=True,
        )
        return scratch

    def test_own_session_matches(self, jj_repo, tmp_path):
        """Positive control: the exact session still recovers its scratch."""
        scratch = self._staged_sibling(jj_repo, tmp_path)
        assert self._recover(jj_repo.repo_root, "t.x-1", "t.x-12") == scratch

    def test_sibling_prefix_session_does_not_match(self, jj_repo, tmp_path):
        """Session x-1 has no scratch of its own; x-12's must not stand in."""
        self._staged_sibling(jj_repo, tmp_path)
        assert self._recover(jj_repo.repo_root, "t.x-1", "t.x-1") == ""


class TestPushBookmarkTemplate:
    """cld_set_push_bookmark_template (vcs-lib.sh): `jj git push --change` names
    its bookmark `<session>.mr-<id>`, in the workspace config only."""

    def _set(self, workspace: Path, backend: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", "-c",
             f'source "{_VCS_LIB}" && cld_set_push_bookmark_template "{workspace}" "{backend}"'],
            capture_output=True, text=True, env={**os.environ, "SESSION_NAME": "a.t1.fix"},
        )

    def test_template_names_the_session(self, jj_repo):
        result = self._set(jj_repo.repo_root, "jj")
        assert result.returncode == 0, result.stderr
        shown = subprocess.run(
            ["jj", "config", "get", "templates.git_push_bookmark"],
            cwd=jj_repo.repo_root, capture_output=True, text=True,
        )
        assert shown.stdout.strip() == '"a.t1.fix.mr-" ++ change_id.short()'

    def test_git_is_left_alone(self, tmp_path):
        assert self._set(tmp_path, "git").returncode == 0
        assert not (tmp_path / ".jj").exists()


class TestScratchEnvelope:
    def test_roundtrip(self):
        scratch = {"session": b"hello world\n", "extra.md": b"\x00\x01\x02"}
        encoded = encode_scratch_envelope(scratch)
        assert isinstance(encoded, str)
        decoded = decode_scratch_envelope(encoded)
        assert decoded == scratch

    def test_decode_malformed_raises(self):
        with pytest.raises(RuntimeError):
            decode_scratch_envelope("not-base64!@#$")


class TestStageFromEnv:
    def test_stages_using_env(self, jj_repo, tmp_path, monkeypatch):
        anchor = _add_second_commit(jj_repo.repo_root)
        peer = _make_peer_workspace(jj_repo, anchor, tmp_path)

        monkeypatch.setenv("SESSION_NAME", "sess_env")
        monkeypatch.setenv("WORKSPACE_CURRENT", str(peer))
        monkeypatch.setenv(
            "AGENT_SCRATCH",
            encode_scratch_envelope({"session": b"sess_env\n"}),
        )
        b_hash = stage_from_env()
        assert b_hash

        show = subprocess.run(
            ["jj", "log", "-r", b_hash, "--no-graph", "--summary", "-T", '""'],
            cwd=jj_repo.repo_root, check=True, capture_output=True, text=True,
        )
        assert SCRATCH_DIR in show.stdout

    def test_missing_scratch_errors(self, jj_repo, monkeypatch):
        monkeypatch.setenv("SESSION_NAME", "sess_env")
        monkeypatch.delenv("AGENT_SCRATCH", raising=False)
        with pytest.raises(RuntimeError, match="AGENT_SCRATCH is required"):
            stage_from_env()

    def test_missing_session_errors(self, monkeypatch):
        monkeypatch.delenv("SESSION_NAME", raising=False)
        with pytest.raises(RuntimeError, match="SESSION_NAME is required"):
            stage_from_env()


class TestStageFromEnvDefaultPayload:
    """`--default-payload` (ticket kind): no AGENT_SCRATCH, B is staged empty
    in-container (design-ticket-containers.md 4.2)."""

    def test_stages_without_agent_scratch(self, jj_repo, tmp_path, monkeypatch):
        anchor = _add_second_commit(jj_repo.repo_root)
        peer = _make_peer_workspace(jj_repo, anchor, tmp_path)

        monkeypatch.setenv("SESSION_NAME", "t.lide-2600")
        monkeypatch.setenv("WORKSPACE_CURRENT", str(peer))
        monkeypatch.delenv("AGENT_SCRATCH", raising=False)
        b_hash = stage_from_env(default_payload=True)

        # B carries no files, only the `cld anchor: <session> mode=<mode>` description.
        assert _rev(jj_repo, b_hash, "empty") == "true"
        assert "cld anchor: t.lide-2600 mode=isolated" in _rev(jj_repo, b_hash, "description")
        assert not (peer / SCRATCH_DIR).exists()

    def test_mode_env_still_lands_in_description(self, jj_repo, tmp_path, monkeypatch):
        anchor = _add_second_commit(jj_repo.repo_root)
        peer = _make_peer_workspace(jj_repo, anchor, tmp_path)

        monkeypatch.setenv("SESSION_NAME", "t.t2")
        monkeypatch.setenv("WORKSPACE_CURRENT", str(peer))
        monkeypatch.setenv("AGENT_ANCHOR_MODE", "shared")
        b_hash = stage_from_env(default_payload=True)

        desc = subprocess.run(
            ["jj", "log", "-r", b_hash, "--no-graph", "-T", "description"],
            cwd=jj_repo.repo_root, check=True, capture_output=True, text=True,
        ).stdout
        assert "mode=shared" in desc

    def test_env_path_still_requires_agent_scratch(self, jj_repo, monkeypatch):
        """The v1 wire keeps its hard requirement when the flag is off."""
        monkeypatch.setenv("SESSION_NAME", "sess_env")
        monkeypatch.delenv("AGENT_SCRATCH", raising=False)
        with pytest.raises(RuntimeError, match="AGENT_SCRATCH is required"):
            stage_from_env(default_payload=False)


# --- Anchor-move helpers (cld.vcs.anchor) ------------------------------------
#
# Mirrors scratchpad design "move a ticket container's anchor commit"
# section 7 step 1: find_anchor_commit / move_anchor_commit / revert_ops.


def _rev(jj_repo, revset: str, template: str = "commit_id") -> str:
    result = jj_repo.run(["log", "-r", revset, "--no-graph", "-T", template])
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _op_head(jj_repo) -> str:
    result = jj_repo.run(["op", "log", "--no-graph", "-T", "id", "-n", "1"])
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _new_commit(path: Path, filename: str, content: str, message: str) -> None:
    (path / filename).write_text(content)
    subprocess.run(["jj", "commit", "-m", message], cwd=path, check=True, capture_output=True)


def _stage_and_work(jj_repo, tmp_path, session, mode="isolated", base=None):
    """Stage S (as the container entrypoint would), bookmark *session* on the
    anchor (S when isolated, the base when shared) and add one work commit C
    on top -- the shape ``move_anchor_commit`` operates on. Returns (base,
    s_hash, c_hash)."""
    base = base if base is not None else _add_second_commit(jj_repo.repo_root)
    peer = _make_peer_workspace(jj_repo, base, tmp_path)
    s_hash = stage_in_workspace(peer, session, {"session": f"{session}\n".encode()}, mode=mode)
    subprocess.run(["jj", "bookmark", "create", session, "-r", base if mode == "shared" else s_hash],
                    cwd=peer, check=True, capture_output=True)
    (peer / "work.txt").write_text("work\n")
    subprocess.run(["jj", "commit", "-m", "C"], cwd=peer, check=True, capture_output=True)
    c_hash = _rev(jj_repo, "@-") if False else subprocess.run(
        ["jj", "log", "-r", "@-", "--no-graph", "-T", "commit_id"],
        cwd=peer, check=True, capture_output=True, text=True,
    ).stdout.strip()
    return base, s_hash, c_hash


class TestFindAnchorCommit:
    def test_finds_the_staged_scratch_commit(self, jj_repo, tmp_path):
        session = "t.find"
        _base, s_hash, _c = _stage_and_work(jj_repo, tmp_path, session)
        assert find_anchor_commit(jj_repo, session) == s_hash

    def test_finds_the_scratch_commit_below_a_shared_bookmark(self, jj_repo, tmp_path):
        session = "t.find_shared"
        _base, s_hash, _c = _stage_and_work(jj_repo, tmp_path, session, mode="shared")
        assert find_anchor_commit(jj_repo, session) == s_hash

    def test_no_match_raises(self, jj_repo, tmp_path):
        with pytest.raises(RuntimeError, match="found 0"):
            find_anchor_commit(jj_repo, "t.nonexistent")

    def test_several_matches_raises(self, jj_repo, tmp_path):
        """Two unrelated scratch commits both descendants of the commit that
        carries the bookmark -- heads() returns both, which is unsafe to pick
        from automatically."""
        session = "t.dup"
        base = _add_second_commit(jj_repo.repo_root)
        jj_repo.run(["new", base])
        (jj_repo.repo_root / "one.txt").write_text("one\n")
        subprocess.run(["jj", "commit", "-m", f"cld anchor: {session} mode=isolated"],
                        cwd=jj_repo.repo_root, check=True, capture_output=True)
        first = _rev(jj_repo, "@-")
        jj_repo.run(["new", base])
        (jj_repo.repo_root / "two.txt").write_text("two\n")
        subprocess.run(["jj", "commit", "-m", f"cld anchor: {session} mode=isolated"],
                        cwd=jj_repo.repo_root, check=True, capture_output=True)
        second = _rev(jj_repo, "@-")
        subprocess.run(["jj", "bookmark", "create", session, "-r", base],
                        cwd=jj_repo.repo_root, check=True, capture_output=True)
        with pytest.raises(RuntimeError, match="found 2"):
            find_anchor_commit(jj_repo, session)


class TestMoveAnchorCommit:
    def test_isolated_move_leaves_old_children_behind(self, jj_repo, tmp_path):
        """isolated: A --- C stays behind; T --- S' is the new shape."""
        session = "t.iso"
        base, s_hash, c_hash = _stage_and_work(jj_repo, tmp_path, session)
        jj_repo.run(["new", "root()", "-m", "T"])
        target = _rev(jj_repo, "@")

        ops = move_anchor_commit(jj_repo, s_hash, target, "isolated", session)
        assert ops

        new_s = _rev(jj_repo, "description(glob:'cld anchor: " + session + " *')")
        assert _rev(jj_repo, f"parents({new_s})") == target
        # C is left behind, reparented straight onto base (S's old parent).
        assert _rev(jj_repo, f"{c_hash} & {base}::") == c_hash

    def test_shared_move_inserts_after_target(self, jj_repo, tmp_path):
        """shared: T's old children move onto S'; T --- S' --- <old children>."""
        session = "t.shared"
        base, s_hash, _c = _stage_and_work(jj_repo, tmp_path, session, mode="shared")
        jj_repo.run(["new", "root()", "-m", "T"])
        target = _rev(jj_repo, "@")
        jj_repo.run(["new", target])
        (jj_repo.repo_root / "d1.txt").write_text("d1\n")
        subprocess.run(["jj", "commit", "-m", "D1"], cwd=jj_repo.repo_root, check=True, capture_output=True)
        jj_repo.run(["new", target])
        (jj_repo.repo_root / "d2.txt").write_text("d2\n")
        subprocess.run(["jj", "commit", "-m", "D2"], cwd=jj_repo.repo_root, check=True, capture_output=True)

        ops = move_anchor_commit(jj_repo, s_hash, target, "shared", session)
        assert ops

        new_s = _rev(jj_repo, "description(glob:'cld anchor: " + session + " *')")
        assert _rev(jj_repo, f"parents({new_s})") == target
        # D1/D2 were themselves rewritten (reparented onto S') by the rebase,
        # so they must be re-found by description rather than their stale
        # pre-move commit ids.
        new_d1 = _rev(jj_repo, 'description(exact:"D1\n")')
        new_d2 = _rev(jj_repo, 'description(exact:"D2\n")')
        assert _rev(jj_repo, f"{new_d1} & {new_s}::") == new_d1
        assert _rev(jj_repo, f"{new_d2} & {new_s}::") == new_d2

    def test_target_inside_own_work_is_allowed(self, jj_repo, tmp_path):
        """The anchor may move into its own descendants (design decision)."""
        session = "t.selfmove"
        _base, s_hash, c_hash = _stage_and_work(jj_repo, tmp_path, session)

        ops = move_anchor_commit(jj_repo, s_hash, c_hash, "isolated", session)
        assert ops
        # C is both S's old child and the rebase target, so jj rewrites it as
        # part of the same transaction -- re-find it by description rather
        # than its stale pre-move commit id.
        new_s = _rev(jj_repo, "description(glob:'cld anchor: " + session + " *')")
        new_c = _rev(jj_repo, 'description(exact:"C\n")')
        assert _rev(jj_repo, f"parents({new_s})") == new_c

    def test_isolated_bookmark_follows_the_scratch_commit(self, jj_repo, tmp_path):
        """The bookmark sits on S and jj moves it along with the rebase."""
        session = "t.bm"
        base, s_hash, c_hash = _stage_and_work(jj_repo, tmp_path, session)
        assert _rev(jj_repo, session) == _rev(jj_repo, s_hash)  # sanity: bookmark starts on S
        jj_repo.run(["new", "root()", "-m", "T"])
        target = _rev(jj_repo, "@")

        move_anchor_commit(jj_repo, s_hash, target, "isolated", session)

        new_s = _rev(jj_repo, "description(glob:'cld anchor: " + session + " *')")
        assert _rev(jj_repo, session) == new_s

    def test_shared_bookmark_moves_to_the_target(self, jj_repo, tmp_path):
        """The bookmark sits on the shared commit; it is not rewritten by the
        rebase, so it must be force-moved onto the target."""
        session = "t.bmshared"
        base, s_hash, _c = _stage_and_work(jj_repo, tmp_path, session, mode="shared")
        assert _rev(jj_repo, session) == base  # sanity: bookmark starts on the shared commit
        jj_repo.run(["new", "root()", "-m", "T"])
        target = _rev(jj_repo, "@")

        move_anchor_commit(jj_repo, s_hash, target, "shared", session)

        assert _rev(jj_repo, session) == target

    def test_already_in_place_makes_no_new_op(self, jj_repo, tmp_path):
        session = "t.noop"
        base, s_hash, _c = _stage_and_work(jj_repo, tmp_path, session)

        op_before = _op_head(jj_repo)
        ops = move_anchor_commit(jj_repo, s_hash, _rev(jj_repo, base, "change_id"), "isolated", session)
        assert ops == []
        assert _op_head(jj_repo) == op_before

    def test_conflict_raises_and_reverts(self, jj_repo, tmp_path):
        """A target that already touched the same scratch path in a
        conflicting way must raise and leave the repo exactly as it was."""
        session = "t.conflict"
        base, s_hash, _c = _stage_and_work(jj_repo, tmp_path, session)
        jj_repo.run(["new", base])
        (jj_repo.repo_root / SCRATCH_DIR).mkdir(exist_ok=True)
        (jj_repo.repo_root / SCRATCH_DIR / "session").write_text("colliding content\n")
        subprocess.run(["jj", "commit", "-m", "T"], cwd=jj_repo.repo_root, check=True, capture_output=True)
        target = _rev(jj_repo, "@-")

        with pytest.raises(RuntimeError, match="conflict"):
            move_anchor_commit(jj_repo, s_hash, target, "isolated", session)
        # revert_ops undoes by appending new operations, not by rewinding the
        # op log, so op-log-head equality isn't the right check here -- the
        # repo state (no conflicts, S back on its original parent) is.
        assert _rev(jj_repo, "conflicts()") == ""
        assert _rev(jj_repo, f"parents({s_hash})") == base


class TestRevertOps:
    def test_reverts_newest_first(self, jj_repo, tmp_path):
        session = "t.revert"
        base, s_hash, _c = _stage_and_work(jj_repo, tmp_path, session)
        jj_repo.run(["new", "root()", "-m", "T"])
        target = _rev(jj_repo, "@")

        op_before = _op_head(jj_repo)
        ops = move_anchor_commit(jj_repo, s_hash, target, "isolated", session)
        assert ops

        revert_ops(jj_repo, ops)
        assert _rev(jj_repo, f"parents({s_hash})") == base
        # The bookmark should have followed the revert back onto S.
        assert _rev(jj_repo, session) == _rev(jj_repo, s_hash)


# --- git: the same helpers over the hidden anchor ref --------------------------


GIT_SESSION = "t.mv"


def _git_stage(git_repo, tmp_path, files=None, work=False):
    """Stage S (as the container entrypoint would) in a worktree on branch GIT_SESSION, and
    create a target commit T next to the base A. Returns (base, s_hash, target, peer)."""
    base = _git(git_repo, "rev-parse", "HEAD")
    peer = _make_git_peer(git_repo, base, tmp_path, branch=GIT_SESSION)
    s_hash = stage_in_workspace(peer, GIT_SESSION, files or {})
    if work:
        (peer / "work.txt").write_text("work\n")
        _git(git_repo, "-C", str(peer), "add", "work.txt")
        _git(git_repo, "-C", str(peer), "commit", "-q", "-m", "C")
    (git_repo.repo_root / "t.txt").write_text("target\n")
    _git(git_repo, "add", "t.txt")
    _git(git_repo, "commit", "-q", "-m", "T")
    return base, s_hash, _git(git_repo, "rev-parse", "HEAD"), peer


class TestFindAnchorCommitGit:
    def test_finds_the_pinned_scratch_commit(self, git_repo, tmp_path):
        _base, s_hash, _t, _peer = _git_stage(git_repo, tmp_path, work=True)
        assert find_anchor_commit(git_repo, GIT_SESSION) == s_hash

    def test_a_missing_ref_raises_with_the_fix(self, git_repo):
        with pytest.raises(RuntimeError, match="no anchor ref refs/cld/anchor/t.nope.*start it once"):
            find_anchor_commit(git_repo, "t.nope")

    def test_a_ref_on_a_foreign_commit_raises(self, git_repo):
        _git(git_repo, "update-ref", anchor_ref("t.odd"), "HEAD")
        with pytest.raises(RuntimeError, match="not a scratch commit"):
            find_anchor_commit(git_repo, "t.odd")


class TestMoveAnchorCommitGit:
    @pytest.mark.parametrize("mode", ["isolated", "shared"])
    def test_s_is_recommitted_as_a_child_of_the_target(self, git_repo, tmp_path, mode):
        _base, s_hash, target, _peer = _git_stage(git_repo, tmp_path)
        message = _git(git_repo, "log", "-1", "--format=%B", s_hash)

        ops = move_anchor_commit(git_repo, s_hash, target, mode, GIT_SESSION)

        new = _git(git_repo, "rev-parse", anchor_ref(GIT_SESSION))
        assert new != s_hash
        assert _git(git_repo, "rev-parse", f"{new}^") == target
        assert _git(git_repo, "log", "-1", "--format=%B", new) == message
        assert _git(git_repo, "rev-parse", f"{new}^{{tree}}") == _git(git_repo, "rev-parse", f"{target}^{{tree}}")
        assert ops[-1] == f"{anchor_ref(GIT_SESSION)} {s_hash} {new}"
        assert find_anchor_commit(git_repo, GIT_SESSION) == new

    def test_a_branch_at_s_follows(self, git_repo, tmp_path):
        _base, s_hash, target, _peer = _git_stage(git_repo, tmp_path)
        ops = move_anchor_commit(git_repo, s_hash, target, "isolated", GIT_SESSION)
        new = _git(git_repo, "rev-parse", anchor_ref(GIT_SESSION))
        assert _git(git_repo, "rev-parse", f"refs/heads/{GIT_SESSION}") == new
        assert len(ops) == 2
        assert ops[0].startswith(f"refs/heads/{GIT_SESSION} ")

    def test_a_branch_with_work_stays_on_the_old_commit(self, git_repo, tmp_path):
        _base, s_hash, target, peer = _git_stage(git_repo, tmp_path, work=True)
        tip = _git(git_repo, "rev-parse", f"refs/heads/{GIT_SESSION}")
        ops = move_anchor_commit(git_repo, s_hash, target, "isolated", GIT_SESSION)
        assert _git(git_repo, "rev-parse", f"refs/heads/{GIT_SESSION}") == tip
        assert _git(git_repo, "rev-parse", f"{tip}^") == s_hash
        assert len(ops) == 1

    def test_already_in_place_changes_nothing(self, git_repo, tmp_path):
        base, s_hash, _target, _peer = _git_stage(git_repo, tmp_path)
        assert move_anchor_commit(git_repo, s_hash, base, "isolated", GIT_SESSION) == []
        assert _git(git_repo, "rev-parse", anchor_ref(GIT_SESSION)) == s_hash

    def test_a_scratch_commit_with_a_brief_is_refused(self, git_repo, tmp_path):
        _base, s_hash, target, _peer = _git_stage(git_repo, tmp_path, files={"brief.md": b"x\n"})
        with pytest.raises(RuntimeError, match="carries changes"):
            move_anchor_commit(git_repo, s_hash, target, "isolated", GIT_SESSION)
        assert _git(git_repo, "rev-parse", anchor_ref(GIT_SESSION)) == s_hash

    def test_an_unresolvable_target_is_refused(self, git_repo, tmp_path):
        _base, s_hash, _target, _peer = _git_stage(git_repo, tmp_path)
        with pytest.raises(RuntimeError, match="could not resolve the reanchor target"):
            move_anchor_commit(git_repo, s_hash, "no-such-rev", "isolated", GIT_SESSION)

    def test_a_ref_moved_meanwhile_fails_without_changes(self, git_repo, tmp_path):
        base, s_hash, target, _peer = _git_stage(git_repo, tmp_path)
        _git(git_repo, "update-ref", anchor_ref(GIT_SESSION), base)
        with pytest.raises(RuntimeError, match="update-ref"):
            move_anchor_commit(git_repo, s_hash, target, "isolated", GIT_SESSION)
        assert _git(git_repo, "rev-parse", anchor_ref(GIT_SESSION)) == base
        assert _git(git_repo, "rev-parse", f"refs/heads/{GIT_SESSION}") == s_hash


class TestRevertOpsGit:
    def test_restores_the_ref_and_the_branch(self, git_repo, tmp_path):
        _base, s_hash, target, _peer = _git_stage(git_repo, tmp_path)
        ops = move_anchor_commit(git_repo, s_hash, target, "isolated", GIT_SESSION)

        revert_ops(git_repo, ops)

        assert _git(git_repo, "rev-parse", anchor_ref(GIT_SESSION)) == s_hash
        assert _git(git_repo, "rev-parse", f"refs/heads/{GIT_SESSION}") == s_hash

    def test_a_ref_moved_since_fails(self, git_repo, tmp_path):
        base, s_hash, target, _peer = _git_stage(git_repo, tmp_path)
        ops = move_anchor_commit(git_repo, s_hash, target, "isolated", GIT_SESSION)
        _git(git_repo, "update-ref", anchor_ref(GIT_SESSION), base)
        with pytest.raises(RuntimeError, match="update-ref"):
            revert_ops(git_repo, ops)
