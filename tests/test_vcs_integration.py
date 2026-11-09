"""Layer 1: VCS backend integration tests against real repositories."""

import subprocess

import pytest


pytestmark = pytest.mark.integration


# --- Helpers ------------------------------------------------------------------

def _current_rev(vcs):
    """Return the 'current' revision specifier for the backend."""
    return "@" if vcs.name == "jj" else "HEAD"


def _committed_rev(vcs):
    """After a jj commit, the committed change is @-. In git it's HEAD."""
    return "@-" if vcs.name == "jj" else "HEAD"


def _parent_rev(vcs, base="@"):
    """Parent of a revision."""
    if vcs.name == "jj":
        return f"{base}-"
    return f"{base}~1" if base != "HEAD" else "HEAD~1"


def _list_branches(vcs):
    """Raw branch/bookmark listing (list_branches() removed from VcsBackend)."""
    cmd = ["bookmark", "list"] if vcs.name == "jj" else ["branch", "-a"]
    return vcs.run(cmd).stdout


# --- Tests --------------------------------------------------------------------


class TestDetectRoot:
    def test_jj_finds_root(self, jj_repo):
        from cld.vcs.jj import JjBackend
        nested = jj_repo.repo_root / "a" / "b"
        nested.mkdir(parents=True)
        assert JjBackend.detect_root(nested) == jj_repo.repo_root

    def test_git_finds_root(self, git_repo):
        from cld.vcs.git import GitBackend
        nested = git_repo.repo_root / "a" / "b"
        nested.mkdir(parents=True)
        assert GitBackend.detect_root(nested) == git_repo.repo_root

    def test_jj_returns_none_outside(self, tmp_path):
        from cld.vcs.jj import JjBackend
        assert JjBackend.detect_root(tmp_path) is None

    def test_git_returns_none_outside(self, tmp_path):
        from cld.vcs.git import GitBackend
        assert GitBackend.detect_root(tmp_path) is None


class TestWorkspace:
    def test_create_and_forget(self, vcs_repo):
        ws_path = str(vcs_repo.repo_root / "ws-test")
        vcs_repo.create_workspace("ws-test", ws_path)
        assert (vcs_repo.repo_root / "ws-test").is_dir()
        vcs_repo.forget_workspace("ws-test", ws_path)

    def test_workspace_at_revision(self, vcs_repo):
        root = vcs_repo.repo_root
        # Seed commit already exists; add another file, commit
        (root / "second.txt").write_text("second\n")
        vcs_repo.commit("add second")

        # Resolve the commit BEFORE "add second"
        if vcs_repo.name == "jj":
            # After commit: @=empty, @-=add second, @--=seed
            seed = vcs_repo.resolve_revision("@--")
        else:
            seed = vcs_repo.resolve_revision("HEAD~1")

        ws_path = str(root / "ws-rev")
        vcs_repo.create_workspace("ws-rev", ws_path, seed)

        # In the workspace, second.txt should NOT exist
        assert not (root / "ws-rev" / "second.txt").exists()
        vcs_repo.forget_workspace("ws-rev", ws_path)


class TestDottedNames:
    """The naming convention puts dots into workspace and bookmark names."""

    def test_jj_dotted_workspace_and_bookmarks(self, jj_repo):
        ws = jj_repo.repo_root / "ws-dotted"
        jj_repo.create_workspace("t.x-y", str(ws))
        assert ws.is_dir()
        assert jj_repo.run(["log", "-r", "t.x-y@", "--no-graph", "-T", "change_id"]).stdout

        jj_repo.create_branch("t.x-y")
        jj_repo.create_branch("t.x-y.mr")
        listing = _list_branches(jj_repo)
        assert "t.x-y:" in listing
        assert "t.x-y.mr:" in listing

        exported = jj_repo.run(["git", "export"])
        assert "Failed" not in exported.stdout + exported.stderr
        jj_repo.forget_workspace("t.x-y", str(ws))


class TestAssertDeliverableFree:
    def test_missing_bookmark_passes(self, jj_repo):
        from cld.vcs.anchor import assert_deliverable_free, resolve_anchor
        assert_deliverable_free(jj_repo, "a.t.x.mr", resolve_anchor(jj_repo, "@"))

    def test_bookmark_inside_the_tree_passes(self, jj_repo):
        from cld.vcs.anchor import assert_deliverable_free, resolve_anchor
        jj_repo.create_branch("a.t.x.mr", "@")
        assert_deliverable_free(jj_repo, "a.t.x.mr", resolve_anchor(jj_repo, "@"))

    def test_bookmark_outside_the_tree_refuses(self, jj_repo):
        from cld.vcs.anchor import assert_deliverable_free, resolve_anchor
        jj_repo.create_branch("a.t.x.mr", "@-")
        with pytest.raises(RuntimeError, match="already exists outside"):
            assert_deliverable_free(jj_repo, "a.t.x.mr", resolve_anchor(jj_repo, "@"))

    def test_a_longer_bookmark_with_the_same_prefix_is_ignored(self, jj_repo):
        from cld.vcs.anchor import assert_deliverable_free, resolve_anchor
        jj_repo.create_branch("a.t.x.mr-fix", "@-")
        assert_deliverable_free(jj_repo, "a.t.x.mr", resolve_anchor(jj_repo, "@"))

    def test_git_backend_is_skipped(self, git_repo):
        from cld.vcs.anchor import assert_deliverable_free
        git_repo.create_branch("a.t.x.mr")
        assert_deliverable_free(git_repo, "a.t.x.mr", "HEAD")


class TestBranch:
    def test_create_and_list(self, vcs_repo):
        vcs_repo.create_branch("test-branch")
        branches = _list_branches(vcs_repo)
        assert "test-branch" in branches
        vcs_repo.delete_branch("test-branch")

    def test_delete(self, vcs_repo):
        vcs_repo.create_branch("to-delete")
        vcs_repo.delete_branch("to-delete")
        branches = _list_branches(vcs_repo)
        assert "to-delete" not in branches

    def test_set_branch_moves_pointer(self, vcs_repo):
        root = vcs_repo.repo_root

        # Mark current position
        if vcs_repo.name == "jj":
            rev_before = vcs_repo.resolve_revision("@")
        else:
            rev_before = vcs_repo.resolve_revision("HEAD")

        vcs_repo.create_branch("movable", rev_before)

        # Make a new commit
        (root / "new.txt").write_text("new\n")
        vcs_repo.commit("new commit")

        rev_after = vcs_repo.resolve_revision(_committed_rev(vcs_repo))

        vcs_repo.set_branch("movable", rev_after)
        resolved = vcs_repo.resolve_revision("movable")
        assert resolved == rev_after
        vcs_repo.delete_branch("movable")


class TestCommitAndChanges:
    def test_commit_captures_changes(self, vcs_repo):
        root = vcs_repo.repo_root
        (root / "committed.txt").write_text("content\n")
        vcs_repo.commit("test commit")
        assert not vcs_repo.has_changes()

    def test_has_changes_true_when_dirty(self, vcs_repo):
        (vcs_repo.repo_root / "dirty.txt").write_text("dirty\n")
        assert vcs_repo.has_changes()

    def test_has_changes_false_when_clean(self, vcs_repo):
        assert not vcs_repo.has_changes()


class TestDiff:
    def test_diff_shows_uncommitted_changes(self, vcs_repo):
        # Modify an existing tracked file (git diff HEAD ignores untracked files)
        (vcs_repo.repo_root / "README.md").write_text("modified content\n")
        output = vcs_repo.diff()
        assert "README.md" in output

    def test_diff_of_committed_revision(self, vcs_repo):
        (vcs_repo.repo_root / "rev.txt").write_text("in revision\n")
        vcs_repo.commit("revision commit")
        output = vcs_repo.diff(_committed_rev(vcs_repo))
        assert "rev.txt" in output

    def test_diff_between_two_revisions(self, vcs_repo):
        root = vcs_repo.repo_root

        # Mark base revision
        if vcs_repo.name == "jj":
            base_rev = vcs_repo.resolve_revision("@")
        else:
            base_rev = vcs_repo.resolve_revision("HEAD")

        (root / "between.txt").write_text("between\n")
        vcs_repo.commit("between commit")
        tip_rev = vcs_repo.resolve_revision(_committed_rev(vcs_repo))

        output = vcs_repo.diff_between(base_rev, tip_rev)
        assert "between.txt" in output

    def test_diff_stat_summary(self, vcs_repo):
        root = vcs_repo.repo_root
        # Modify tracked file + add a new one (staged for git)
        (root / "README.md").write_text("changed\n")
        (root / "stat_new.txt").write_text("new\n")
        if vcs_repo.name == "git":
            # git diff HEAD only sees tracked files; stage the new one
            subprocess.run(
                ["git", "add", "stat_new.txt"],
                cwd=root, check=True, capture_output=True,
            )
        count, names = vcs_repo.diff_stat_summary()
        assert count == 2
        assert "README.md" in names
        assert "stat_new.txt" in names


class TestFileShow:
    def test_reads_from_revision(self, vcs_repo):
        root = vcs_repo.repo_root
        (root / "showme.txt").write_text("visible content\n")
        vcs_repo.commit("add showme")
        rev = _committed_rev(vcs_repo)
        vcs_repo.create_branch("show-branch", rev)
        content = vcs_repo.file_show("show-branch", "showme.txt")
        assert content.strip() == "visible content"
        vcs_repo.delete_branch("show-branch")

    def test_returns_none_for_missing(self, vcs_repo):
        rev = _current_rev(vcs_repo)
        assert vcs_repo.file_show(rev, "nonexistent.txt") is None


class TestLog:
    def test_log_contains_message(self, vcs_repo):
        root = vcs_repo.repo_root
        (root / "logged.txt").write_text("log\n")
        vcs_repo.commit("unique log message xyz")
        rev = _committed_rev(vcs_repo)
        output = vcs_repo.log(rev)
        assert output.strip()

    def test_resolve_revision_returns_hex(self, vcs_repo):
        rev = _current_rev(vcs_repo)
        resolved = vcs_repo.resolve_revision(rev)
        assert len(resolved) >= 7
        assert all(c in "0123456789abcdef" for c in resolved)


class TestForkPoint:
    def test_finds_common_ancestor(self, vcs_repo):
        root = vcs_repo.repo_root

        if vcs_repo.name == "jj":
            # @ is the current (empty) working copy; the seed commit is @-
            base = vcs_repo.resolve_revision("@-")

            # Branch A
            vcs_repo.run(["new", base])
            (root / "branch_a.txt").write_text("a\n")
            vcs_repo.commit("branch a")
            vcs_repo.create_branch("branch-a", "@-")

            # Branch B (from same base)
            vcs_repo.run(["new", base])
            (root / "branch_b.txt").write_text("b\n")
            vcs_repo.commit("branch b")
            vcs_repo.create_branch("branch-b", "@-")
        else:
            base = vcs_repo.resolve_revision("HEAD")

            subprocess.run(
                ["git", "checkout", "-b", "branch-a"],
                cwd=root, check=True, capture_output=True,
            )
            (root / "branch_a.txt").write_text("a\n")
            vcs_repo.commit("branch a")

            subprocess.run(
                ["git", "checkout", base],
                cwd=root, check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "checkout", "-b", "branch-b"],
                cwd=root, check=True, capture_output=True,
            )
            (root / "branch_b.txt").write_text("b\n")
            vcs_repo.commit("branch b")

        fork = vcs_repo.fork_point("branch-a", "branch-b")
        assert fork == base

        vcs_repo.delete_branch("branch-a")
        vcs_repo.delete_branch("branch-b")


class TestSquash:
    def test_squash_combines_changes(self, vcs_repo):
        root = vcs_repo.repo_root

        if vcs_repo.name == "jj":
            (root / "squash1.txt").write_text("first\n")
            vcs_repo.commit("first")
            (root / "squash2.txt").write_text("second\n")
            vcs_repo.commit("second")
            # Now: @=empty, @-=second, @--=first
            vcs_repo.squash("@-", "@--")
            # After squash: @-=combined (first+second)
            assert vcs_repo.file_show("@-", "squash1.txt") is not None
            assert vcs_repo.file_show("@-", "squash2.txt") is not None
        else:
            (root / "squash1.txt").write_text("first\n")
            vcs_repo.commit("first")
            (root / "squash2.txt").write_text("second\n")
            vcs_repo.commit("second")
            vcs_repo.squash("HEAD", "HEAD~1")
            assert vcs_repo.file_show("HEAD", "squash1.txt") is not None
            assert vcs_repo.file_show("HEAD", "squash2.txt") is not None
