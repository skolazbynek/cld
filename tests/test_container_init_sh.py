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
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

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

    BOOKMARK = "cld_ticket_ro"

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
                "SESSION_NAME": self.BOOKMARK}

    def _boot(self, env: dict, body: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", "-c", f"set -e\n. {VCS_LIB_SH}\n{body}"],
                              capture_output=True, text=True, env=env)

    def _jj(self, env: dict, repo: Path, *args: str) -> str:
        return subprocess.run(["jj", "-R", str(repo), "--ignore-working-copy", *args], env=env,
                              capture_output=True, text=True, check=True).stdout

    def _ops(self, env: dict, repo: Path) -> list[str]:
        return self._jj(env, repo, "op", "log", "--no-graph", "-T", 'id.short() ++ " " ++ description ++ "\\n"').splitlines()

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
