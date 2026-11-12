"""Tests for the claude-devcontainer boot scripts: container-init.sh's
broker-dependent setup, and the TICKET_MODE warm-start wrapper cleanup in the
entrypoint. A warm start (`docker stop` then `docker start`) keeps /tmp, so
the previous boot's generated `claude` wrapper in /tmp/bin -- first on PATH --
shadows `which claude`, and the freshly regenerated wrapper would exec its
stale predecessor's path, an infinite self-exec loop. The code under test is
taken from the real scripts and run via bash.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from cld.ticket import _rebase_hints
from cld.vcs.anchor import find_anchor_commit, move_anchor_commit
from cld.vcs.anchor_guard import Anchor, check_file
from cld.vcs.git import GitBackend

CONTAINER_INIT_SH = (
    Path(__file__).resolve().parent.parent
    / "imgs" / "claude-devcontainer" / "container-init.sh"
)
ENTRYPOINT_SH = CONTAINER_INIT_SH.parent / "entrypoint-claude-devcontainer.sh"


def _extract_entrypoint_fragment(start: str, end: str) -> str:
    """First region of the entrypoint from a line containing `start` through
    the next line containing `end`, inclusive. First match wins, so the
    TICKET_MODE blocks (which precede the v1 flow) are the ones extracted."""
    text = ENTRYPOINT_SH.read_text()
    match = re.search(
        rf"^[ \t]*{re.escape(start)}[^\n]*$.*?^[ \t]*{re.escape(end)}[^\n]*$",
        text, re.MULTILINE | re.DOTALL,
    )
    assert match, f"fragment {start!r}..{end!r} not found in {ENTRYPOINT_SH}"
    return match.group(0)


class TestTicketWarmStartWrapperCleanup:
    """A warm start (`docker stop` then `docker start`) keeps /tmp, so the
    previous boot's generated wrapper in /tmp/bin -- first on PATH -- shadows
    `which claude`, and the regenerated wrapper would exec its stale
    predecessor's path: an infinite self-exec loop (`cld claude` hangs). The
    TICKET_MODE branch must wipe /tmp/bin before anything resolves a binary.
    Runs the real entrypoint fragment with /tmp substituted into a sandbox."""

    def _warm_boot(self, tmp_path):
        real = tmp_path / "realbin"
        real.mkdir()
        binary = real / "claude"
        binary.write_text("#!/bin/bash\n")
        binary.chmod(0o755)
        fake_tmp = tmp_path / "faketmp"
        bindir = fake_tmp / "bin"
        bindir.mkdir(parents=True)
        # What the previous (cold) boot generated into /tmp/bin: a wrapper
        # pointing at the real binary. On the warm boot it shadows that
        # binary on PATH.
        stale_claude = bindir / "claude"
        stale_claude.write_text(
            f"#!/bin/bash\nexec {real}/claude --dangerously-skip-permissions \"$@\"\n"
        )
        stale_claude.chmod(0o755)

        def sub(fragment: str) -> str:
            return fragment.replace("/tmp", str(fake_tmp))

        cleanup = _extract_entrypoint_fragment(
            "rm -f /tmp/cld-ticket-ready", "rm -f /tmp/bin/*")
        claude_block = _extract_entrypoint_fragment(
            "CLAUDE_BIN=$(which claude)", "chmod +x /tmp/bin/claude")
        assert "flock" in claude_block, "expected the TICKET_MODE claude block"
        script = "\n".join([
            f'export PATH="{bindir}:{real}:$PATH"',
            sub(cleanup),
            sub(claude_block),
        ])
        result = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True,
            env=dict(os.environ),
        )
        assert result.returncode == 0, result.stderr
        return real, bindir

    def test_regenerated_claude_wrapper_execs_the_real_binary(self, tmp_path):
        real, bindir = self._warm_boot(tmp_path)
        wrapper = (bindir / "claude").read_text()
        assert f"exec {real}/claude --dangerously-skip-permissions" in wrapper
        # Self-referential wrapper = the warm-start infinite exec loop.
        assert str(bindir) not in wrapper


def _source_container_init(tmp_path, body: str, broker_wired: bool, known_hosts: bool = True) -> None:
    """Source the real container-init.sh with /tmp and the broker secrets
    mounts moved into a sandbox, then run *body*."""
    fake_tmp = tmp_path / "faketmp"
    fake_tmp.mkdir(exist_ok=True)
    secrets = tmp_path / "secrets"
    secrets.mkdir(exist_ok=True)
    if broker_wired:
        (secrets / "broker-key").write_text("key\n")
        if known_hosts:
            (secrets / "broker-known-hosts").write_text("[host]:2222 ssh-ed25519 AAAA\n")
    # /tmp first: the sandbox paths substituted in may themselves be under /tmp.
    init = (CONTAINER_INIT_SH.read_text()
            .replace("/tmp", str(fake_tmp))
            .replace("/run/secrets", str(secrets)))
    script = tmp_path / "container-init.sh"
    script.write_text(init)
    run_env = {**os.environ, "HOME": str(tmp_path / "home")}
    run_env.pop("CLD_BROKER_ENDPOINT", None)
    if broker_wired:
        run_env["CLD_BROKER_ENDPOINT"] = "host.docker.internal:2222"
    (tmp_path / "home").mkdir(exist_ok=True)
    result = subprocess.run(["bash", "-c", f". {script}\n{body}"], capture_output=True, text=True, env=run_env)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("jq") is None, reason="needs jq")
class TestGraphqlMcpBuiltIn:
    """graphql-tester is only a broker client, so every broker-wired container
    gets it -- whether or not the host's ~/.claude.json registers it."""

    def _servers(self, tmp_path, broker_wired: bool, host_servers: dict) -> dict:
        host_config = tmp_path / "faketmp" / "host-config"
        host_config.mkdir(parents=True)
        (host_config / ".claude.json").write_text(json.dumps({"mcpServers": host_servers}))
        _source_container_init(tmp_path, "build_claude_config", broker_wired)
        return json.loads((tmp_path / "home" / ".claude.json").read_text())["mcpServers"]

    def test_added_when_the_broker_is_wired(self, tmp_path):
        servers = self._servers(tmp_path, broker_wired=True, host_servers={})
        assert servers["graphql-tester"]["args"] == ["/opt/cld/cld/mcp/graphql.py"]

    def test_absent_without_broker_or_host_entry(self, tmp_path):
        assert "graphql-tester" not in self._servers(tmp_path, broker_wired=False, host_servers={})

    def test_host_entry_rewritten_to_the_container_path(self, tmp_path):
        host = {"graphql-tester": {"type": "stdio", "command": "/host/run-graphql.sh", "args": []}}
        servers = self._servers(tmp_path, broker_wired=False, host_servers=host)
        assert servers["graphql-tester"]["args"] == ["/opt/cld/cld/mcp/graphql.py"]


class TestTicketClaudeMdBrokerSection:
    """The boot-written ticket CLAUDE.md points a fresh session at the broker
    for tests and the live server -- only where the broker is wired."""

    def _claude_md(self, tmp_path, broker_wired: bool, known_hosts: bool = True) -> str:
        root = tmp_path / "ticket"
        root.mkdir()
        _source_container_init(tmp_path, f"write_ticket_claude_md {root} t '| cld | abc | isolated | jj |\n'",
                               broker_wired, known_hosts)
        return (root / "CLAUDE.md").read_text()

    def test_section_when_wired(self, tmp_path):
        text = self._claude_md(tmp_path, broker_wired=True)
        assert "cld broker run-tests" in text
        assert "broker-graphql-server" in text

    def test_no_section_without_broker(self, tmp_path):
        text = self._claude_md(tmp_path, broker_wired=False)
        assert "Anchor contract" in text
        assert "cld broker" not in text

    def test_no_section_without_known_hosts(self, tmp_path):
        """The client pins the host key, so a key alone is not a usable broker."""
        assert "cld broker" not in self._claude_md(tmp_path, broker_wired=True, known_hosts=False)


class TestTicketClaudeMdBookmarkNames:
    def test_names_the_ticket_bookmarks(self, tmp_path):
        root = tmp_path / "ticket"
        root.mkdir()
        _source_container_init(tmp_path, f"write_ticket_claude_md {root} lide-1 '| cld | abc | isolated | jj |\n'",
                               broker_wired=False)
        text = (root / "CLAUDE.md").read_text()
        assert "## Bookmark names" in text
        assert "`t.lide-1` is the session bookmark: never move or delete it." in text
        assert "`t.lide-1.mr`" in text
        assert "`t.lide-1.mr-<word>`" in text
        assert "`t.lide-1.<word>`" in text
        assert "Never use `internal.`" in text


class TestTicketClaudeMdGitRepos:
    """A row whose backend is git adds git wording; a table of jj rows reads as before."""

    JJ_ROW = "| cld | abc | isolated | jj |"
    GIT_ROW = "| api | def | isolated | git |"

    def _claude_md(self, tmp_path, rows: list[str], broker_wired: bool = False) -> str:
        root = tmp_path / "ticket"
        root.mkdir()
        table = "".join(f"{row}\n" for row in rows)
        _source_container_init(tmp_path, f"write_ticket_claude_md {root} lide-1 '{table}'", broker_wired)
        return (root / "CLAUDE.md").read_text()

    def test_git_row_adds_the_git_section(self, tmp_path):
        text = self._claude_md(tmp_path, [self.GIT_ROW])
        assert "## Git repos" in text
        assert "the branch `t.lide-1`" in text
        assert "`t.lide-1` is the session branch: never delete it" in text
        assert "Commit with `git add` and `git commit`" in text
        assert "The session branch advances\n  with each commit" in text
        assert "`git push origin HEAD:t.lide-1.mr`" in text
        # The jj text stays in place above it.
        assert "`t.lide-1` is the session bookmark: never move or delete it." in text

    def test_git_section_names_the_anchor_ref(self, tmp_path):
        text = self._claude_md(tmp_path, [self.GIT_ROW])
        assert "hidden ref\n  `refs/cld/anchor/t.lide-1`" in text
        assert "only `cld restart --reanchor` does" in text

    def test_mixed_table_gets_the_git_section_too(self, tmp_path):
        assert "## Git repos" in self._claude_md(tmp_path, [self.JJ_ROW, self.GIT_ROW])

    def test_jj_table_has_no_git_wording(self, tmp_path):
        text = self._claude_md(tmp_path, [self.JJ_ROW], broker_wired=True)
        assert "Git repos" not in text
        assert "git add" not in text
        assert "committed work only" not in text

    def test_broker_section_says_git_tests_see_committed_work_only(self, tmp_path):
        text = self._claude_md(tmp_path, [self.GIT_ROW], broker_wired=True)
        assert "uncommitted ones included" in text
        assert "For a git repo the broker sees committed work only" in text
        assert "the tip of the\nbranch `t.lide-1`" in text

    def test_no_broker_note_without_the_broker(self, tmp_path):
        assert "committed work only" not in self._claude_md(tmp_path, [self.GIT_ROW])


class TestTicketClaudeMdOriginLine:
    """Origin is only the store now, and the generated CLAUDE.md says so."""

    def test_origin_holds_only_the_store(self, tmp_path):
        root = tmp_path / "ticket"
        root.mkdir()
        _source_container_init(tmp_path, f"write_ticket_claude_md {root} t '| cld | abc | isolated | jj |\n'",
                               broker_wired=False)
        text = (root / "CLAUDE.md").read_text()
        assert ("/workspace/origin/<repo> holds only the shared jj/git store. "
                "Run jj and edit files in your workspace.") in text
        assert "read-only" not in text


VCS_LIB_SH = CONTAINER_INIT_SH.parent / "vcs-lib.sh"
REPO_ROOT = CONTAINER_INIT_SH.parent.parent.parent


def _tree_state(root: Path) -> dict[str, tuple[int, int]]:
    """Mtime and size of every file under *root* (or of *root* itself)."""
    paths = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
    return {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in paths}


def _make_read_only(*paths: Path) -> None:
    """chmod stand-in for the read-only bind mounts: a dir loses write (no new
    files, no renames inside), a file loses write."""
    for path in paths:
        for sub in [path, *path.rglob("*")]:
            sub.chmod(sub.stat().st_mode & ~0o222)


def _restore_writable(*paths: Path) -> None:
    for path in paths:
        for sub in [path, *path.rglob("*")]:
            sub.chmod(sub.stat().st_mode | 0o200)


class _RealJjOrigin:
    """A real jj origin whose working-copy record, git config and hooks are read-only,
    as the container mounts them. Read-only is simulated with chmod, which blocks new
    files in a directory but not an atomic replace of a file."""

    BOOKMARK = "t.ro"

    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        config = tmp_path / "jj-config.toml"
        config.write_text('[user]\nname = "test"\nemail = "test@localhost"\n')
        (tmp_path / "home").mkdir()
        # Out of the caller's own workspace: see tests/test_docker.py:jj_home.
        monkeypatch.chdir(tmp_path)
        python_bin = str(Path(sys.executable).parent)
        return {**os.environ, "JJ_CONFIG": str(config), "HOME": str(tmp_path / "home"),
                "PATH": f"{python_bin}:{os.environ['PATH']}", "PYTHONPATH": str(REPO_ROOT),
                "SESSION_NAME": self.BOOKMARK,
                # The scratch commit of a git ticket needs an identity; the container mounts a .gitconfig.
                "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@localhost",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@localhost"}

    def _boot(self, env: dict, body: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", "-c", f"set -e\n. {VCS_LIB_SH}\n{body}"],
                              capture_output=True, text=True, env=env)

    def _jj(self, env: dict, repo: Path, *args: str) -> str:
        return subprocess.run(["jj", "-R", str(repo), "--ignore-working-copy", *args], env=env,
                              capture_output=True, text=True, check=True).stdout

    def _ops(self, env: dict, repo: Path) -> list[str]:
        return self._jj(env, repo, "op", "log", "--no-graph", "-T", 'id.short() ++ " " ++ description ++ "\\n"').splitlines()

    def _git_origin(self, tmp_path, env) -> Path:
        origin = tmp_path / "git-origin"
        git = ["git", "-C", str(origin), "-c", "user.name=t", "-c", "user.email=t@localhost"]
        subprocess.run(["git", "init", "-q", str(origin)], env=env, check=True)
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "seed"], env=env, check=True)
        subprocess.run([*git, "update-ref", "refs/remotes/origin/main", "HEAD"], env=env, check=True)
        return origin

    def _worktrees(self, env, origin: Path) -> dict[str, list[str]]:
        """Registered worktree path -> its attribute lines (branch, locked ...)."""
        out = subprocess.run(["git", "-C", str(origin), "worktree", "list", "--porcelain"],
                             env=env, capture_output=True, text=True, check=True).stdout
        blocks = [b.splitlines() for b in out.strip().split("\n\n")]
        return {b[0].removeprefix("worktree "): b[1:] for b in blocks}

    def _git(self, env, origin: Path, *args: str) -> str:
        return subprocess.run(["git", "-C", str(origin), *args], env=env, capture_output=True,
                              text=True, check=True).stdout.strip()

    def _boot_git(self, env, origin: Path, workspace: Path, mode: str = "isolated", session: str = "",
                  base: str = "origin/main") -> subprocess.CompletedProcess:
        session = session or self.BOOKMARK
        return self._boot({**env, "SESSION_NAME": session},
                          f'cld_boot_worktree {origin} {workspace} {session} {base} {mode}\n'
                          'echo "anchor=$CLD_BOOT_ANCHOR first=$CLD_BOOT_FIRST_LAUNCH"')

    def _anchor_of(self, result: subprocess.CompletedProcess) -> str:
        return result.stdout.splitlines()[-1].split()[0].removeprefix("anchor=")

    @pytest.fixture
    def jj_origin(self, tmp_path, env):
        origin = tmp_path / "origin"
        subprocess.run(["jj", "git", "init", "--colocate", str(origin)], env=env, capture_output=True, check=True)
        (origin / "README.md").write_text("seed\n")
        subprocess.run(["jj", "-R", str(origin), "commit", "-m", "seed"], env=env, capture_output=True, check=True)
        (origin / ".git" / "hooks").mkdir(exist_ok=True)
        protected = [origin / ".jj" / "working_copy", origin / ".git" / "config", origin / ".git" / "hooks"]
        _make_read_only(*protected)
        yield origin
        _restore_writable(*protected)


@pytest.mark.skipif(shutil.which("jj") is None or shutil.which("git") is None, reason="needs jj and git")
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
class TestBootWithReadOnlyWorkingCopy(_RealJjOrigin):
    """The origin's jj working-copy record, git config and hooks are read-only
    binds: booting a workspace must go through the store only. Atomic replace of
    a file is not blocked by chmod -- so git's config is also compared by content."""

    def _protected_state(self, origin: Path) -> dict:
        return {name: _tree_state(origin / path) for name, path in
                {"working_copy": ".jj/working_copy", "config": ".git/config", "hooks": ".git/hooks"}.items()} | {
            "config_bytes": (origin / ".git" / "config").read_bytes(),
            "checkout": sorted(p.name for p in origin.iterdir()),
        }

    def _first_launch(self, env: dict, origin: Path, workspace: Path) -> subprocess.CompletedProcess:
        return self._boot(env, f'cld_boot_workspace {origin} {workspace} {self.BOOKMARK} "@-" isolated default\n'
                               'echo "anchor=$CLD_BOOT_ANCHOR first=$CLD_BOOT_FIRST_LAUNCH"')

    def test_first_launch_creates_the_workspace(self, tmp_path, env, jj_origin):
        workspace = tmp_path / "ws"
        before, ops_before = self._protected_state(jj_origin), self._ops(env, jj_origin)
        result = self._first_launch(env, jj_origin, workspace)
        assert result.returncode == 0, result.stderr
        assert "first=1" in result.stdout
        assert (workspace / "README.md").read_text() == "seed\n"
        anchor = self._jj(env, jj_origin, "log", "--no-graph", "-r", self.BOOKMARK, "-T", "change_id")
        assert f"anchor={anchor} " in result.stdout
        assert self.BOOKMARK in self._jj(env, jj_origin, "workspace", "list", "-T", 'name ++ "\\n"').split()
        assert self._protected_state(jj_origin) == before
        assert not [op for op in self._ops(env, jj_origin)[: -len(ops_before)] if "snapshot" in op]

    def test_reattach_after_the_workspace_dir_is_lost(self, tmp_path, env, jj_origin):
        workspace = tmp_path / "ws"
        first = self._first_launch(env, jj_origin, workspace)
        assert first.returncode == 0, first.stderr
        shutil.rmtree(workspace)
        before = self._protected_state(jj_origin)
        result = self._first_launch(env, jj_origin, workspace)
        assert result.returncode == 0, result.stderr
        assert "reattaching workspace" in result.stdout
        assert (workspace / "README.md").read_text() == "seed\n"
        assert result.stdout.splitlines()[-1] == first.stdout.splitlines()[-1].replace("first=1", "first=0")
        assert self._protected_state(jj_origin) == before

    def test_reattach_resumes_at_the_workspace_tip_and_keeps_the_bookmark(self, tmp_path, env, jj_origin):
        workspace = tmp_path / "ws"
        assert self._first_launch(env, jj_origin, workspace).returncode == 0
        bookmark_before = self._jj(env, jj_origin, "log", "--no-graph", "-r", self.BOOKMARK, "-T", "commit_id")
        (workspace / "work.txt").write_text("work\n")
        subprocess.run(["jj", "-R", str(workspace), "commit", "-m", "work"], env=env, capture_output=True, check=True)
        shutil.rmtree(workspace)
        result = self._first_launch(env, jj_origin, workspace)
        assert result.returncode == 0, result.stderr
        assert (workspace / "work.txt").read_text() == "work\n"
        assert self._jj(env, jj_origin, "log", "--no-graph", "-r", self.BOOKMARK, "-T", "commit_id") == bookmark_before

    def test_jj_boot_needs_nothing_but_the_store(self, tmp_path, env, jj_origin):
        """The container's origin is the .jj and .git dirs of the host repo and
        no checkout: boot and the workspace content come from the store alone."""
        store = tmp_path / "store-only"
        store.mkdir()
        for name in (".jj", ".git"):
            shutil.copytree(jj_origin / name, store / name, symlinks=True)
        workspace = tmp_path / "ws"
        result = self._first_launch(env, store, workspace)
        assert result.returncode == 0, result.stderr
        assert sorted(p.name for p in store.iterdir()) == [".git", ".jj"]
        assert (workspace / "README.md").read_text() == "seed\n"

    def test_git_boot_needs_nothing_but_the_store(self, tmp_path, env):
        origin = tmp_path / "git-origin"
        git = ["git", "-C", str(origin), "-c", "user.name=t", "-c", "user.email=t@localhost"]
        subprocess.run(["git", "init", "-q", str(origin)], env=env, check=True)
        (origin / "README.md").write_text("seed\n")
        subprocess.run([*git, "add", "README.md"], env=env, check=True)
        subprocess.run([*git, "commit", "-q", "-m", "seed"], env=env, check=True)
        subprocess.run([*git, "update-ref", "refs/remotes/origin/main", "HEAD"], env=env, check=True)
        store = tmp_path / "store-only"
        store.mkdir()
        shutil.copytree(origin / ".git", store / ".git", symlinks=True)
        workspace = tmp_path / "ws"
        result = self._boot(env, f"cld_boot_worktree {store} {workspace} {self.BOOKMARK} origin/main")
        assert result.returncode == 0, result.stderr
        assert (workspace / "README.md").read_text() == "seed\n"

    def test_git_worktree_boot_does_not_track(self, tmp_path, env):
        origin = tmp_path / "git-origin"
        git = ["git", "-C", str(origin), "-c", "user.name=t", "-c", "user.email=t@localhost"]
        subprocess.run(["git", "init", "-q", str(origin)], env=env, check=True)
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "seed"], env=env, check=True)
        subprocess.run([*git, "remote", "add", "origin", str(tmp_path / "remote")], env=env, check=True)
        subprocess.run([*git, "update-ref", "refs/remotes/origin/main", "HEAD"], env=env, check=True)
        (origin / ".git" / "hooks").mkdir(exist_ok=True)
        protected = [origin / ".git" / "config", origin / ".git" / "hooks"]
        _make_read_only(*protected)
        try:
            config_before = (origin / ".git" / "config").read_bytes()
            workspace = tmp_path / "ws"
            result = self._boot(env, f"cld_boot_worktree {origin} {workspace} {self.BOOKMARK} origin/main")
            assert result.returncode == 0, result.stderr
            assert (workspace / ".git").exists()
            assert (origin / ".git" / "config").read_bytes() == config_before
        finally:
            _restore_writable(*protected)

    def test_git_worktree_is_locked_with_the_session_as_reason(self, tmp_path, env):
        origin = self._git_origin(tmp_path, env)
        workspace = tmp_path / "ws"
        result = self._boot(env, f"cld_boot_worktree {origin} {workspace} {self.BOOKMARK} origin/main")
        assert result.returncode == 0, result.stderr
        assert f"locked {self.BOOKMARK}" in self._worktrees(env, origin)[str(workspace)]

    def test_git_boot_keeps_other_tickets_worktree_registrations(self, tmp_path, env):
        """Their paths do not exist here; a blanket `git worktree prune` would drop them."""
        origin = self._git_origin(tmp_path, env)
        for other, lock in (("t.other", ["--lock", "--reason", "t.other"]), ("t.legacy", [])):
            subprocess.run(["git", "-C", str(origin), "worktree", "add", *lock, "-b", other,
                            str(tmp_path / other), "HEAD"], env=env, check=True, capture_output=True)
            shutil.rmtree(tmp_path / other)
        workspace = tmp_path / "ws"
        result = self._boot(env, f"cld_boot_worktree {origin} {workspace} {self.BOOKMARK} origin/main")
        assert result.returncode == 0, result.stderr
        registered = self._worktrees(env, origin)
        assert str(tmp_path / "t.other") in registered
        assert str(tmp_path / "t.legacy") in registered

    def test_git_reboot_replaces_only_its_own_stale_registration(self, tmp_path, env):
        origin = self._git_origin(tmp_path, env)
        subprocess.run(["git", "-C", str(origin), "worktree", "add", "--lock", "--reason", "t.other",
                        "-b", "t.other", str(tmp_path / "other"), "HEAD"],
                       env=env, check=True, capture_output=True)
        shutil.rmtree(tmp_path / "other")
        workspace = tmp_path / "ws"
        boot = f"cld_boot_worktree {origin} {workspace} {self.BOOKMARK} origin/main"
        assert self._boot(env, boot).returncode == 0
        # The container layer died with the workspace; the registration stays.
        shutil.rmtree(workspace)
        result = self._boot(env, boot)
        assert result.returncode == 0, result.stderr
        assert "reattaching worktree" in result.stdout
        assert (workspace / ".git").exists()
        registered = self._worktrees(env, origin)
        assert f"locked {self.BOOKMARK}" in registered[str(workspace)]
        assert str(tmp_path / "other") in registered

    def test_git_first_launch_isolated_anchors_on_the_scratch_commit(self, tmp_path, env):
        origin = self._git_origin(tmp_path, env)
        base = self._git(env, origin, "rev-parse", "origin/main")
        workspace = tmp_path / "ws"
        result = self._boot_git(env, origin, workspace)
        assert result.returncode == 0, result.stderr
        assert "first=1" in result.stdout
        scratch = self._git(env, origin, "rev-parse", f"refs/cld/anchor/{self.BOOKMARK}")
        assert self._anchor_of(result) == scratch
        assert self._git(env, origin, "rev-parse", f"{scratch}^") == base
        assert self._git(env, workspace, "rev-parse", "HEAD") == scratch
        assert self._git(env, origin, "log", "-1", "--format=%B", scratch) == f"cld anchor: {self.BOOKMARK} mode=isolated"
        assert self._git(env, origin, "rev-parse", self.BOOKMARK) == scratch
        assert f"locked {self.BOOKMARK}" in self._worktrees(env, origin)[str(workspace)]

    def test_git_first_launch_shared_anchors_on_the_base(self, tmp_path, env):
        origin = self._git_origin(tmp_path, env)
        base = self._git(env, origin, "rev-parse", "origin/main")
        result = self._boot_git(env, origin, tmp_path / "ws", mode="shared")
        assert result.returncode == 0, result.stderr
        assert self._anchor_of(result) == base
        scratch = self._git(env, origin, "rev-parse", f"refs/cld/anchor/{self.BOOKMARK}")
        assert self._git(env, origin, "log", "-1", "--format=%B", scratch).endswith("mode=shared")

    def test_git_staging_failure_leaves_a_clean_first_launch(self, tmp_path, env):
        origin = self._git_origin(tmp_path, env)
        workspace = tmp_path / "ws"
        # The scratch module needs the session name: without it staging fails after the worktree exists.
        no_session = {k: v for k, v in env.items() if k != "SESSION_NAME"}
        result = self._boot(no_session, f"cld_boot_worktree {origin} {workspace} {self.BOOKMARK} origin/main isolated")
        assert result.returncode != 0
        assert "anchor staging failed" in result.stderr
        assert not workspace.exists()
        assert self._git(env, origin, "branch", "--list", self.BOOKMARK) == ""
        assert self._boot_git(env, origin, workspace).returncode == 0

    def test_git_first_launch_without_any_identity_uses_the_fallback(self, tmp_path, env):
        """No GIT_AUTHOR_* variable, no global config and git not guessing one: the boot still stages."""
        origin = self._git_origin(tmp_path, env)
        self._git(env, origin, "config", "user.useConfigOnly", "true")
        no_identity = {k: v for k, v in env.items() if not k.startswith(("GIT_AUTHOR", "GIT_COMMITTER"))}
        no_identity |= {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        no_identity.pop("EMAIL", None)
        result = self._boot_git(no_identity, origin, tmp_path / "ws")
        assert result.returncode == 0, result.stderr
        scratch = self._git(env, origin, "rev-parse", f"refs/cld/anchor/{self.BOOKMARK}")
        assert self._git(env, origin, "log", "-1", "--format=%an <%ae>", scratch) == "cld <cld@localhost>"

    def test_git_warm_restart_recovers_the_anchor_from_the_ref(self, tmp_path, env):
        origin = self._git_origin(tmp_path, env)
        workspace = tmp_path / "ws"
        first = self._boot_git(env, origin, workspace)
        (workspace / "work.txt").write_text("work\n")
        self._git(env, workspace, "add", "work.txt")
        self._git(env, workspace, "commit", "-q", "-m", "work")
        result = self._boot_git(env, origin, workspace)
        assert result.returncode == 0, result.stderr
        assert "warm restart" in result.stdout
        assert self._anchor_of(result) == self._anchor_of(first)
        assert "first=0" in result.stdout

    def test_git_reattach_after_dir_loss_resumes_the_branch(self, tmp_path, env):
        origin = self._git_origin(tmp_path, env)
        workspace = tmp_path / "ws"
        first = self._boot_git(env, origin, workspace)
        (workspace / "work.txt").write_text("work\n")
        self._git(env, workspace, "add", "work.txt")
        self._git(env, workspace, "commit", "-q", "-m", "work")
        tip = self._git(env, workspace, "rev-parse", "HEAD")
        shutil.rmtree(workspace)
        result = self._boot_git(env, origin, workspace)
        assert result.returncode == 0, result.stderr
        assert "reattaching worktree" in result.stdout
        assert "first=0" in result.stdout
        assert self._anchor_of(result) == self._anchor_of(first)
        assert (workspace / "work.txt").read_text() == "work\n"
        assert self._git(env, workspace, "rev-parse", "HEAD") == tip
        assert f"locked {self.BOOKMARK}" in self._worktrees(env, origin)[str(workspace)]

    def test_git_reattach_resumes_a_detached_head(self, tmp_path, env):
        origin = self._git_origin(tmp_path, env)
        workspace = tmp_path / "ws"
        first = self._boot_git(env, origin, workspace)
        self._git(env, workspace, "checkout", "-q", "--detach")
        self._git(env, workspace, "commit", "-q", "--allow-empty", "-m", "detached work")
        tip = self._git(env, workspace, "rev-parse", "HEAD")
        shutil.rmtree(workspace)
        result = self._boot_git(env, origin, workspace)
        assert result.returncode == 0, result.stderr
        assert self._git(env, workspace, "rev-parse", "HEAD") == tip
        assert self._git(env, workspace, "branch", "--show-current") == ""
        assert self._anchor_of(result) == self._anchor_of(first)
        assert f"locked {self.BOOKMARK}" in self._worktrees(env, origin)[str(workspace)]

    def _ended(self, env, origin: Path) -> dict[str, str]:
        out = self._git(env, origin, "for-each-ref", "--format=%(refname) %(objectname)",
                        f"refs/cld/ended/{self.BOOKMARK}/")
        return dict(line.split() for line in out.splitlines())

    def _lose_a_detached_commit(self, env, origin: Path, workspace: Path) -> str:
        """Boot, commit on a detached HEAD, lose the directory; return that commit."""
        assert self._boot_git(env, origin, workspace).returncode == 0
        self._git(env, workspace, "checkout", "-q", "--detach")
        self._git(env, workspace, "commit", "-q", "--allow-empty", "-m", "detached work")
        tip = self._git(env, workspace, "rev-parse", "HEAD")
        shutil.rmtree(workspace)
        return tip

    def test_git_reattach_archives_the_detached_head_before_dropping_the_registration(self, tmp_path, env):
        origin = self._git_origin(tmp_path, env)
        workspace = tmp_path / "ws"
        tip = self._lose_a_detached_commit(env, origin, workspace)
        result = self._boot_git(env, origin, workspace)
        assert result.returncode == 0, result.stderr
        archived = self._ended(env, origin)
        assert list(archived.values()) == [tip]
        assert re.fullmatch(rf"refs/cld/ended/{re.escape(self.BOOKMARK)}/\d{{8}}T\d{{6}}Z-detached", next(iter(archived)))
        assert self._git(env, workspace, "rev-parse", "HEAD") == tip

    def test_git_reattach_archives_a_detached_head_once(self, tmp_path, env):
        origin = self._git_origin(tmp_path, env)
        workspace = tmp_path / "ws"
        self._lose_a_detached_commit(env, origin, workspace)
        assert self._boot_git(env, origin, workspace).returncode == 0
        shutil.rmtree(workspace)
        assert self._boot_git(env, origin, workspace).returncode == 0
        assert len(self._ended(env, origin)) == 1

    def test_git_reattach_archives_nothing_for_a_head_the_branch_holds(self, tmp_path, env):
        origin = self._git_origin(tmp_path, env)
        workspace = tmp_path / "ws"
        assert self._boot_git(env, origin, workspace).returncode == 0
        self._git(env, workspace, "checkout", "-q", "--detach")
        shutil.rmtree(workspace)
        assert self._boot_git(env, origin, workspace).returncode == 0
        assert self._ended(env, origin) == {}

    def test_git_failed_reattach_keeps_the_detached_commits(self, tmp_path, env):
        """The add fails (no room for the directory) after the old registration is gone."""
        origin = self._git_origin(tmp_path, env)
        workspace = tmp_path / "ws"
        tip = self._lose_a_detached_commit(env, origin, workspace)
        tmp_path.chmod(0o555)
        try:
            result = self._boot_git(env, origin, workspace)
        finally:
            tmp_path.chmod(0o755)
        assert result.returncode != 0
        assert "git worktree add failed (reattach)" in result.stderr
        assert str(workspace) not in self._worktrees(env, origin)
        assert list(self._ended(env, origin).values()) == [tip]
        assert self._boot_git(env, origin, workspace).returncode == 0
        assert self._git(env, origin, "merge-base", "--is-ancestor", tip, next(iter(self._ended(env, origin)))) == ""

    def test_git_reattach_resumes_another_checked_out_branch(self, tmp_path, env):
        origin = self._git_origin(tmp_path, env)
        workspace = tmp_path / "ws"
        assert self._boot_git(env, origin, workspace).returncode == 0
        self._git(env, workspace, "checkout", "-q", "-b", f"{self.BOOKMARK}.mr")
        shutil.rmtree(workspace)
        assert self._boot_git(env, origin, workspace).returncode == 0
        assert self._git(env, workspace, "branch", "--show-current") == f"{self.BOOKMARK}.mr"

    def test_git_branch_without_a_ref_reattaches_on_the_base_with_a_warning(self, tmp_path, env):
        """A ticket booted before the scratch commit existed: branch, no anchor ref."""
        origin = self._git_origin(tmp_path, env)
        base = self._git(env, origin, "rev-parse", "origin/main")
        self._git(env, origin, "branch", self.BOOKMARK, base)
        result = self._boot_git(env, origin, tmp_path / "ws", base=base)
        assert result.returncode == 0, result.stderr
        assert "first=0" in result.stdout
        assert self._anchor_of(result) == base
        assert "no anchor ref" in result.stderr
        assert self._git(env, origin, "for-each-ref", "refs/cld/") == ""

    def test_git_ref_without_a_branch_reattaches_with_a_new_branch_at_the_scratch_commit(self, tmp_path, env):
        origin = self._git_origin(tmp_path, env)
        workspace = tmp_path / "ws"
        first = self._boot_git(env, origin, workspace)
        shutil.rmtree(workspace)
        self._git(env, origin, "worktree", "remove", "-f", "-f", str(workspace))
        self._git(env, origin, "branch", "-D", self.BOOKMARK)
        result = self._boot_git(env, origin, workspace)
        assert result.returncode == 0, result.stderr
        assert "first=0" in result.stdout
        assert self._anchor_of(result) == self._anchor_of(first)
        assert self._git(env, workspace, "branch", "--show-current") == self.BOOKMARK
        assert self._git(env, workspace, "rev-parse", "HEAD") == self._anchor_of(first)

    def test_git_second_ticket_boot_keeps_the_first_tickets_registration(self, tmp_path, env):
        origin = self._git_origin(tmp_path, env)
        first, second = tmp_path / "first", tmp_path / "second"
        assert self._boot_git(env, origin, first, session="t.one").returncode == 0
        shutil.rmtree(first)
        assert self._boot_git(env, origin, second, session="t.two").returncode == 0
        registered = self._worktrees(env, origin)
        assert "locked t.one" in registered[str(first)]
        assert "locked t.two" in registered[str(second)]
        assert self._boot_git(env, origin, first, session="t.one").returncode == 0
        assert self._git(env, origin, "rev-parse", "--verify", "refs/cld/anchor/t.two")


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
class TestGitReanchorThenReattach(_RealJjOrigin):
    """The host moves the scratch commit of a git ticket (cld/vcs/anchor.py) and works out the
    rebase hints (cld/ticket.py), both unmocked; then the container reattaches through the locked
    registration of its lost worktree (vcs-lib.sh). Wherever the agent was working, the printed
    commands bring that work back into the anchor tree, which the guard checks."""

    SCENARIOS = ["session-branch", "session-branch-no-work", "other-branch", "detached-head", "other-branch-checked-out-nowhere"]

    def _work(self, env, workspace: Path, scenario: str) -> None:
        def commit() -> None:
            (workspace / "work.txt").write_text("work\n")
            self._git(env, workspace, "add", "work.txt")
            self._git(env, workspace, "commit", "-q", "-m", "work")

        match scenario:
            case "session-branch":
                commit()
            case "other-branch":
                self._git(env, workspace, "checkout", "-q", "-b", f"{self.BOOKMARK}.mr")
                commit()
            case "detached-head":
                self._git(env, workspace, "checkout", "-q", "--detach")
                commit()
            case "other-branch-checked-out-nowhere":
                self._git(env, workspace, "checkout", "-q", "-b", f"{self.BOOKMARK}.mr")
                commit()
                self._git(env, workspace, "checkout", "-q", self.BOOKMARK)

    def _guard_denies(self, workspace: Path, anchor: str) -> bool:
        anchors = [Anchor(repo="api", workspace=str(workspace), anchor=anchor, mode="isolated", backend="git")]
        reason = check_file(str(workspace / "x.txt"), anchors)
        assert reason is None or "outside the anchor tree" in reason
        return reason is not None

    @pytest.fixture
    def host_identity(self, monkeypatch, env):
        """The host-side commit-tree needs an identity of the calling process."""
        for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
            monkeypatch.setenv(key, env[key])

    @pytest.mark.parametrize("scenario", SCENARIOS)
    def test_the_printed_commands_bring_the_work_back_into_the_tree(self, tmp_path, env, host_identity, scenario):
        origin = self._git_origin(tmp_path, env)
        workspace = (tmp_path / "ws").resolve()
        assert self._boot_git(env, origin, workspace).returncode == 0
        self._work(env, workspace, scenario)
        shutil.rmtree(workspace)

        backend = GitBackend(origin)
        old = find_anchor_commit(backend, self.BOOKMARK)
        self._git(env, origin, "commit", "--allow-empty", "-q", "-m", "target")
        target = self._git(env, origin, "rev-parse", "HEAD")
        move_anchor_commit(backend, old, target, "isolated", self.BOOKMARK)
        hints = _rebase_hints(backend, self.BOOKMARK, old, str(workspace))

        result = self._boot_git(env, origin, workspace)
        assert result.returncode == 0, result.stderr
        new = self._anchor_of(result)
        assert new == self._git(env, origin, "rev-parse", f"refs/cld/anchor/{self.BOOKMARK}") != old
        assert "locked" in " ".join(self._worktrees(env, origin)[str(workspace)])
        # A branch that sat on S follows it; the rest resumes on the old one, where the guard blocks.
        assert self._guard_denies(workspace, new) == (scenario in ("session-branch", "other-branch", "detached-head"))
        assert bool(hints) == (scenario != "session-branch-no-work")
        for hint in hints:
            assert hint.in_worktree == (scenario != "other-branch-checked-out-nowhere")
            subprocess.run(shlex.split(hint.command), cwd=workspace, env=env, check=True, capture_output=True)

        assert not self._guard_denies(workspace, new)
        if scenario not in ("session-branch-no-work",):
            assert (workspace / "work.txt").read_text() == "work\n"
        if scenario == "detached-head":
            assert self._git(env, workspace, "branch", "--show-current") == ""
        if scenario == "other-branch-checked-out-nowhere":
            self._git(env, workspace, "merge-base", "--is-ancestor", new, f"{self.BOOKMARK}.mr")
        assert _rebase_hints(backend, self.BOOKMARK, old, str(workspace)) == []

    def test_the_printed_undo_moves_the_anchor_back_onto_the_old_base(self, tmp_path, env, host_identity):
        origin = self._git_origin(tmp_path, env)
        base = self._git(env, origin, "rev-parse", "origin/main")
        workspace = (tmp_path / "ws").resolve()
        assert self._boot_git(env, origin, workspace).returncode == 0
        shutil.rmtree(workspace)
        backend = GitBackend(origin)
        old = find_anchor_commit(backend, self.BOOKMARK)
        self._git(env, origin, "commit", "--allow-empty", "-q", "-m", "target")
        moved = move_anchor_commit(backend, old, self._git(env, origin, "rev-parse", "HEAD"), "isolated", self.BOOKMARK)
        assert moved
        new = find_anchor_commit(backend, self.BOOKMARK)

        # `cld restart <ticket> --reanchor <repo>@<old base>` asks for exactly this move.
        undone = move_anchor_commit(backend, new, base, "isolated", self.BOOKMARK)

        assert undone
        back = find_anchor_commit(backend, self.BOOKMARK)
        assert self._git(env, origin, "rev-parse", f"{back}^") == base
        assert self._git(env, origin, "rev-parse", f"refs/heads/{self.BOOKMARK}") == back
        assert _rebase_hints(backend, self.BOOKMARK, new, str(workspace)) == []
        result = self._boot_git(env, origin, workspace)
        assert result.returncode == 0, result.stderr
        assert self._anchor_of(result) == back
        assert not self._guard_denies(workspace, back)


@pytest.mark.skipif(shutil.which("jj") is None or shutil.which("git") is None, reason="needs jj and git")
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
class TestPlainJjInOriginFails(_RealJjOrigin):
    """Design A3: a plain jj call in origin fails on the read-only working copy and adds
    no operation, so a boot script that forgets the flag stops instead of snapshotting
    the empty origin checkout into the operator's working copy."""

    def test_status_fails_and_adds_no_operation(self, env, jj_origin):
        ops_before = self._ops(env, jj_origin)
        working_copy_before = _tree_state(jj_origin / ".jj" / "working_copy")
        result = subprocess.run(["jj", "st"], cwd=jj_origin, env=env, capture_output=True, text=True)
        assert result.returncode != 0
        assert "working copy" in result.stderr.lower()
        assert self._ops(env, jj_origin) == ops_before
        assert _tree_state(jj_origin / ".jj" / "working_copy") == working_copy_before

    def test_the_store_helper_reads_the_same_origin(self, env, jj_origin):
        """The flag is what makes the same call work (the boot scripts' only way in)."""
        ops_before = self._ops(env, jj_origin)
        result = self._boot(env, f'cld_store_jj {jj_origin} log --no-graph -n 1 -r @- -T description')
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "seed"
        assert self._ops(env, jj_origin) == ops_before


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
class TestGitHooksOffAfterBoot:
    """A worktree runs the hooks of the host repo (a read-only mount), and a host
    hook may call a host-only path. Like jj, a container never runs git hooks."""

    def _commit_in_worktree(self, tmp_path, boot: bool) -> tuple[subprocess.CompletedProcess, str]:
        home = tmp_path / "home"
        home.mkdir()
        host_config = tmp_path / "faketmp" / "host-config"
        host_config.mkdir(parents=True)
        (host_config / ".gitconfig").write_text("[user]\n\tname = host-user\n\temail = host@localhost\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env |= {"HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1"}
        identity = ["-c", "user.name=t", "-c", "user.email=t@localhost"]
        origin, workspace = tmp_path / "origin", tmp_path / "ws"
        subprocess.run(["git", "init", "-q", str(origin)], env=env, check=True)
        subprocess.run(["git", "-C", str(origin), *identity, "commit", "-q", "--allow-empty", "-m", "seed"],
                       env=env, check=True)
        subprocess.run(["git", "-C", str(origin), "worktree", "add", "-q", "-b", "t.hook", str(workspace)],
                       env=env, check=True)
        hooks = origin / ".git" / "hooks"
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\nexec /nonexistent/host/venv/bin/pre-commit \"$@\"\n")
        hook.chmod(0o755)
        _make_read_only(hooks)
        try:
            if boot:
                _source_container_init(tmp_path, "copy_host_configs", broker_wired=False)
            (workspace / "work.txt").write_text("work\n")
            subprocess.run(["git", "-C", str(workspace), "add", "work.txt"], env=env, check=True)
            commit = subprocess.run(["git", "-C", str(workspace), "commit", "-m", "work"],
                                    env=env, capture_output=True, text=True)
            author = subprocess.run(["git", "-C", str(workspace), "log", "-1", "--format=%an"],
                                    env=env, capture_output=True, text=True).stdout.strip()
        finally:
            _restore_writable(hooks)
        return commit, author

    def test_a_failing_host_hook_blocks_the_commit_without_the_boot_step(self, tmp_path):
        commit, _ = self._commit_in_worktree(tmp_path, boot=False)
        assert commit.returncode != 0

    def test_a_failing_host_hook_does_not_block_the_commit_after_boot(self, tmp_path):
        commit, author = self._commit_in_worktree(tmp_path, boot=True)
        assert commit.returncode == 0, commit.stderr
        assert author == "host-user"

    def test_the_setting_is_written_without_any_staged_host_config(self, tmp_path):
        _source_container_init(tmp_path, "copy_host_configs", broker_wired=False)
        hooks_dir = subprocess.run(
            ["git", "config", "--file", str(tmp_path / "home" / ".gitconfig"), "core.hooksPath"],
            capture_output=True, text=True, check=True).stdout.strip()
        assert Path(hooks_dir).is_dir()
        assert list(Path(hooks_dir).iterdir()) == []


_JJ_COMMAND = re.compile(r"(?:^\s*|[;&|(]\s*|\b(?:then|do)\s+|\bif\s+!?\s*|\$\(\s*)jj\b")
_BOOT_SCRIPTS = (
    VCS_LIB_SH,
    ENTRYPOINT_SH,
    REPO_ROOT / "imgs" / "claude-run" / "entrypoint-claude-run.sh",
)


def _plain_jj_on_origin(script: str) -> list[str]:
    """Logical lines (continuations joined, comments dropped) that run jj against an
    origin or a repo path without ``--ignore-working-copy``."""
    flagged = []
    for line in script.replace("\\\n", " ").splitlines():
        if line.lstrip().startswith("#") or not _JJ_COMMAND.search(line):
            continue
        on_origin = re.search(r"(?i:origin)|\s(?:-R|--repository)\b", line)
        if on_origin and "--ignore-working-copy" not in line:
            flagged.append(line.strip())
    return flagged


class TestNoPlainJjInOrigin:
    """Design D2: every jj call the boot scripts make in origin passes
    ``--ignore-working-copy`` (through ``cld_store_jj``, or the flag itself). Origin
    holds only the store, so a call without it fails at boot on a real host."""

    @pytest.mark.parametrize("script", _BOOT_SCRIPTS, ids=lambda path: path.name)
    def test_no_plain_jj_on_origin(self, script):
        assert script.is_file()
        assert _plain_jj_on_origin(script.read_text()) == []

    @pytest.mark.parametrize(
        "line",
        [
            '(cd "$origin" && jj st)',
            'jj -R "$WORKSPACE_ORIGIN" log',
            'if jj bookmark list -R "$REPO_ORIGIN"; then',
            'x=$(cd "$origin" && \\\n  jj log)',
        ],
        ids=["cd-origin", "dash-R", "if-condition", "continuation"],
    )
    def test_the_scan_flags_a_plain_call(self, line):
        assert _plain_jj_on_origin(line) == [line.replace("\\\n", " ").strip()]

    @pytest.mark.parametrize(
        "line",
        [
            'cld_store_jj "$origin" log',
            '(cd "$origin" && jj --ignore-working-copy "$@")',
            '(cd "$workspace" && jj new "$rev")',
            '# (cd "$origin" && jj st)',
            'echo "Error: jj workspace add failed (origin)"',
        ],
        ids=["helper", "flag", "workspace", "comment", "message"],
    )
    def test_the_scan_passes_a_safe_line(self, line):
        assert _plain_jj_on_origin(line) == []
