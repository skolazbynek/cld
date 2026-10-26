"""Tests for the shell glue of the runtests and graphqlserver images.

The jj workspace step shared by both entrypoints must never snapshot the
mounted store's own working copy -- the host user's checkout, possibly
mid-edit -- and forgetting the workspace must leave no stray commit behind.
Both rest on jj behaviour, so the real fragment runs against a real jj repo: a
jj upgrade that changes it fails here. The ssh wrapper (scripts/ssh-wrapper.sh)
runs with ssh, getent and mountpoint stubbed.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINTS = [ROOT / "runtests" / "entrypoint.sh", ROOT / "graphqlserver" / "entrypoint.sh"]
SCRIPTS = ROOT / "scripts"
SSH_WRAPPER = SCRIPTS / "ssh-wrapper.sh"


def _fragment(entrypoint: Path, repo: Path) -> str:
    """From `cd /repo` through the cd into the checked-out workspace, with the
    store mount point pointed at *repo*."""
    match = re.search(r'^cd /repo$.*?^cd "\$work/\$PROJECT_SUBDIR"$', entrypoint.read_text(),
                      re.MULTILINE | re.DOTALL)
    assert match, f"workspace fragment not found in {entrypoint}"
    return match.group(0).replace("/repo", str(repo))


def _jj_has_secure_config() -> bool:
    if shutil.which("jj") is None:
        return False
    found = re.search(r"(\d+)\.(\d+)", subprocess.run(["jj", "--version"], capture_output=True, text=True).stdout)
    return bool(found) and (int(found[1]), int(found[2])) >= (0, 38)


@pytest.fixture
def env(tmp_path):
    config = tmp_path / "jj-config.toml"
    config.write_text('[user]\nname = "test"\nemail = "test@localhost"\n')
    (tmp_path / "home").mkdir()
    return {**os.environ, "PATH": f"{SCRIPTS}:{os.environ['PATH']}", "JJ_CONFIG": str(config),
            "HOME": str(tmp_path / "home"), "HOSTNAME": "abc123",
            "GQL_WORKSPACE": "gql-abc123", "PROJECT_SUBDIR": "."}


def _jj(env: dict, repo: Path, *args: str) -> str:
    return subprocess.run(["jj", "-R", str(repo), "--ignore-working-copy", *args], env=env,
                          capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path, env):
    """A store whose working copy has an edit jj has not snapshotted yet."""
    repo = tmp_path / "repo"
    subprocess.run(["jj", "git", "init", str(repo)], env=env, capture_output=True, check=True)
    (repo / "tracked.txt").write_text("committed\n")
    subprocess.run(["jj", "-R", str(repo), "commit", "-m", "first"], env=env, capture_output=True, check=True)
    (repo / "host-edit.txt").write_text("in progress\n")
    return repo


def _run(entrypoint: Path, repo: Path, env: dict, revision: str, after: str = "") -> subprocess.CompletedProcess:
    script = "\n".join(["set -euo pipefail", _fragment(entrypoint, repo), after])
    return subprocess.run(["bash", "-c", script], env={**env, "REVISION": revision}, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("jj") is None, reason="needs jj")
@pytest.mark.parametrize("entrypoint", ENTRYPOINTS, ids=["runtests", "graphqlserver"])
class TestWorkspaceStep:
    def test_checks_out_the_revision_without_snapshotting_the_store(self, entrypoint, repo, env):
        result = _run(entrypoint, repo, env, "@-", after="cat tracked.txt; test ! -e host-edit.txt")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "committed\n"
        assert _jj(env, repo, "diff", "-r", "default@", "--name-only") == ""

    def test_forget_on_exit_leaves_no_stray_commit(self, entrypoint, repo, env):
        default_wc = _jj(env, repo, "log", "--no-graph", "-r", "default@", "-T", "commit_id")
        result = _run(entrypoint, repo, env, "@-", after="echo junk > byproduct.txt")
        assert result.returncode == 0, result.stderr
        assert _jj(env, repo, "workspace", "list", "-T", 'name ++ "\\n"') == "default\n"
        assert _jj(env, repo, "log", "--no-graph", "-r", "heads(all())", "-T", "commit_id") == default_wc

    def test_unknown_revision_fails_with_jj_error(self, entrypoint, repo, env):
        result = _run(entrypoint, repo, env, "no-such-rev")
        assert result.returncode != 0
        assert "no-such-rev" in result.stderr

    def test_workspace_add_failure_is_reported(self, entrypoint, repo, env):
        name = "runtests-abc123" if entrypoint.parent.name == "runtests" else "gql-abc123"
        subprocess.run(["jj", "-R", str(repo), "workspace", "add", "--name", name, str(repo.parent / "taken")],
                       env=env, capture_output=True, check=True)
        result = _run(entrypoint, repo, env, "@-")
        assert result.returncode != 0
        assert "already exists" in result.stderr

    def test_rewritten_revision_uses_the_current_commit(self, entrypoint, repo, env):
        """The caller snapshotted again after REVISION was resolved: its old
        commit is hidden, and building on it would revive it as a divergent twin."""
        old = _jj(env, repo, "log", "--no-graph", "-r", "@-", "-T", "commit_id")
        change = _jj(env, repo, "log", "--no-graph", "-r", "@-", "-T", "change_id")
        (repo / "tracked.txt").write_text("rewritten\n")
        subprocess.run(["jj", "-R", str(repo), "squash"], env=env, capture_output=True, check=True)
        result = _run(entrypoint, repo, env, old, after="cat tracked.txt")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "rewritten\n"
        assert _jj(env, repo, "log", "--no-graph", "-r", f"change_id({change})", "-T", 'commit_id ++ "\n"').count("\n") == 1


def _read_only(path: Path) -> None:
    """chmod -R a-w, the stand-in for a `readonly` bind mount."""
    for entry in [path, *(path.rglob("*") if path.is_dir() else [])]:
        entry.chmod(entry.stat().st_mode & ~0o222)


@pytest.fixture
def store_only(repo, env):
    """*repo* as the broker mounts it into a runner: only `.jj` and the git dir, no checkout, and the
    read-only rows of the mount set write-protected. Read-only binds are simulated with
    chmod, which root ignores."""
    for entry in repo.iterdir():
        if entry.name not in (".jj", ".git"):
            entry.unlink()
    for scope in ("--repo", "--workspace"):  # the launcher preflight creates the ids this way
        subprocess.run(["jj", "-R", str(repo), "--ignore-working-copy", "config", "path", scope], env=env,
                       capture_output=True, check=True)
    git = repo / ".git" if (repo / ".git").is_dir() else repo / ".jj/repo/store/git"
    (git / "hooks").mkdir(exist_ok=True)
    protected = [repo / ".jj/working_copy", repo / ".jj/repo/config-id", repo / ".jj/repo/store/git_target",
                 repo / ".jj/workspace-config-id", git / "config", git / "hooks"]
    for path in protected:
        _read_only(path)
    yield repo
    for entry in [repo, *repo.rglob("*")]:
        if not entry.is_symlink():
            entry.chmod(entry.stat().st_mode | 0o200)


@pytest.mark.skipif(not _jj_has_secure_config(), reason="needs jj 0.38+ (the jj ids exist)")
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores read-only modes")
@pytest.mark.parametrize("entrypoint", ENTRYPOINTS, ids=["runtests", "graphqlserver"])
class TestStoreOnlyRepo:
    """The broker mounts only the jj/git store at /repo (docs/design-secrets-containment.md
    section 5): the workspace step must work with nothing but `.jj` there, with the
    protected rows read-only, and never touch the default workspace."""

    def _tree(self, repo: Path) -> dict[str, tuple[int, int, bytes]]:
        return {str(p): (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes())
                for p in sorted(repo.rglob("*")) if p.is_file() and not p.is_symlink()
                and any(part in p.parts for part in ("working_copy", "config-id", "git_target", "workspace-config-id"))}

    def test_checks_out_the_revision_from_the_store_alone(self, entrypoint, store_only, env):
        default_wc = _jj(env, store_only, "log", "--no-graph", "-r", "default@", "-T", "commit_id")
        protected = self._tree(store_only)
        result = _run(entrypoint, store_only, env, "@-", after="cat tracked.txt")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "committed\n"
        assert {p.name for p in store_only.iterdir()} <= {".jj", ".git"}
        assert _jj(env, store_only, "log", "--no-graph", "-r", "default@", "-T", "commit_id") == default_wc
        assert _jj(env, store_only, "workspace", "list", "-T", 'name ++ "\\n"') == "default\n"
        assert self._tree(store_only) == protected

    def test_fresh_home_leaves_the_config_ids_alone(self, entrypoint, store_only, env, tmp_path):
        """The broker runs the images with an empty $HOME. Without a config record there, jj rewrites
        each config id by rename, which the read-only bind over it refuses (EBUSY)."""
        (tmp_path / "fresh").mkdir()
        protected = self._tree(store_only)
        result = _run(entrypoint, store_only, {**env, "HOME": str(tmp_path / "fresh")}, "@-", after="cat tracked.txt")
        assert result.returncode == 0, result.stderr
        assert self._tree(store_only) == protected


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS, ids=["runtests", "graphqlserver"])
def test_every_jj_call_in_the_store_ignores_its_working_copy(entrypoint, tmp_path):
    """Static: every jj call before the `cd` into the new workspace. /repo holds no checkout,
    so a plain jj there would snapshot every file as deleted (or fail on the read-only rows)."""
    fragment = _fragment(entrypoint, tmp_path).split('\ncd "$work"\n')[0]
    calls = re.findall(r"\bjj\b[^\n]*", re.sub(r"^\s*#.*$", "", fragment, flags=re.MULTILINE))
    assert len(calls) >= 4
    assert all("--ignore-working-copy" in call for call in calls)


class TestSshWrapper:
    """The broker runs the images as the host uid, which has no passwd entry
    there; ssh refuses to run without one, so the wrapper supplies it through
    nss_wrapper. It also never prompts, and trusts a new host key on first use."""

    def _run(self, tmp_path, passwd_entry: bool, cache_mounted: bool) -> dict:
        bindir = tmp_path / "bin"
        bindir.mkdir()
        stubs = {
            "getent": f"exit {0 if passwd_entry else 2}\n",
            "mountpoint": f"exit {0 if cache_mounted else 1}\n",
            "fake-ssh": 'printf "%s\\n" "$@" > "$OUT.argv"; env > "$OUT.env"; '
                        '[ -z "${NSS_WRAPPER_PASSWD:-}" ] || tail -n1 "$NSS_WRAPPER_PASSWD" > "$OUT.passwd"\n',
        }
        for name, body in stubs.items():
            (bindir / name).write_text("#!/bin/sh\n" + body)
            (bindir / name).chmod(0o755)
        cache = tmp_path / "cache"
        wrapper = tmp_path / "ssh"
        wrapper.write_text(SSH_WRAPPER.read_text().replace("/usr/bin/ssh", str(bindir / "fake-ssh"))
                           .replace("/cache", str(cache)))
        wrapper.chmod(0o755)
        home = tmp_path / "home"
        home.mkdir()
        out = tmp_path / "out"
        env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(home), "USER": "runtests", "OUT": str(out)}
        result = subprocess.run([str(wrapper), "git@gitlab.example", "git-upload-pack 'g/p.git'"],
                                env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        env_lines = Path(f"{out}.env").read_text().splitlines()
        return {
            "argv": Path(f"{out}.argv").read_text().splitlines(),
            "env": dict(line.partition("=")[::2] for line in env_lines),
            "passwd": Path(f"{out}.passwd").read_text() if Path(f"{out}.passwd").exists() else None,
            "home": home, "cache": cache,
        }

    def test_supplies_a_passwd_entry_for_an_unknown_uid(self, tmp_path):
        run = self._run(tmp_path, passwd_entry=False, cache_mounted=False)
        assert run["env"]["LD_PRELOAD"] == "libnss_wrapper.so"
        assert run["env"]["NSS_WRAPPER_GROUP"] == "/etc/group"
        assert run["passwd"] == f"runtests:x:{os.getuid()}:{os.getgid()}::{run['home']}:/bin/sh\n"

    def test_known_uid_runs_ssh_unwrapped(self, tmp_path):
        run = self._run(tmp_path, passwd_entry=True, cache_mounted=False)
        assert "LD_PRELOAD" not in run["env"]
        assert run["passwd"] is None

    def test_never_prompts_and_trusts_on_first_use(self, tmp_path):
        run = self._run(tmp_path, passwd_entry=True, cache_mounted=False)
        assert run["argv"] == ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                               "-o", f"UserKnownHostsFile={run['home']}/.ssh/known_hosts",
                               "git@gitlab.example", "git-upload-pack 'g/p.git'"]

    def test_learned_host_keys_persist_in_the_cache_mount(self, tmp_path):
        run = self._run(tmp_path, passwd_entry=True, cache_mounted=True)
        assert f"UserKnownHostsFile={run['cache']}/ssh/known_hosts" in run["argv"]
        assert (run["cache"] / "ssh").is_dir()
