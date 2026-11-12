"""Git backend implementation -- fallback when jujutsu is not available."""

import errno
import os
import re
import stat
import subprocess
from collections.abc import Iterator
from pathlib import Path

from cld.log import get_logger
from cld.vcs.base import VcsBackend
from cld.vcs.jj import check_git_dir

log = get_logger(__name__)

# Put in front of a host git command that changes refs: no hook runs, whatever a config the
# container could write says (defense in depth next to ``check_git_store``).
NO_HOOKS = ("-c", "core.hooksPath=/dev/null")

# In the environment of every host git command on a git store. A container can write
# ``refs/replace/*`` and ``<git dir>/info/grafts``, which show ``commit-tree`` and ``merge-base``
# fake objects and parents. Each variable switches off one of the two; the first does not
# cover the graft file.
STORE_ENV = {"GIT_NO_REPLACE_OBJECTS": "1", "GIT_GRAFT_FILE": "/dev/null"}

# Put in front of every host git command on a git store. A container can write
# ``objects/info/commit-graph``, which git trusts for parents, generation numbers and root trees
# without reading the commits, so a forged file shows ``merge-base --is-ancestor``,
# ``for-each-ref --contains`` and ``^{tree}`` fake answers.
STORE_GIT_FLAGS = ("-c", "core.commitGraph=false")

# ``extensions.worktreeConfig`` makes git also read ``<git dir>/config.worktree`` and
# ``<git dir>/worktrees/*/config.worktree``, which the container can write and which can
# set ``core.hooksPath``, ``core.fsmonitor`` or ``core.sshCommand``. The pattern finds the key
# in any letter case, also right behind a section header on one line; git counts a carriage
# return as whitespace. A lookalike only refuses too much.
WORKTREE_CONFIG_KEY = re.compile(rb"(?:^|\])[ \t\r]*worktreeconfig(?![\w-])", re.IGNORECASE | re.MULTILINE)
_CONFIG_MAX_BYTES = 1 << 20
_BLANK = " \t\r"
_CONFIG_HEADER = re.compile(r'\[[ \t\r]*([^\]\s"]+)(?:[ \t\r]+"(?:[^"\\\n]|\\.)*")?[ \t\r]*\]')
_CONFIG_KEY = re.compile(r"([A-Za-z][A-Za-z0-9-]*)[ \t\r]*(?:=[ \t\r]*(.*))?$")
_ESCAPES = {"n": "\n", "t": "\t", "b": "\b"}


def _config_value(raw: str | None, lines: Iterator[str]) -> str:
    """A config value as git reads it: quotes and escapes resolved, an unquoted ``#`` or ``;``
    starting a comment. A backslash at the end of the line (also before a CRLF) continues the value in
    the next of *lines*; a comment never continues."""
    value, quoted = "", False
    while raw is not None:
        chars = iter(raw.removesuffix("\r"))
        raw = None
        for char in chars:
            if char == "\\":
                if (escaped := next(chars, None)) is None:
                    raw = next(lines, None)
                else:
                    value += _ESCAPES.get(escaped, escaped)
            elif char == '"':
                quoted = not quoted
            elif char in "#;" and not quoted:
                break
            else:
                value += char
    return value.strip(_BLANK)


def config_include_paths(data: bytes) -> list[str]:
    """The values of ``include.path`` and ``includeIf.<condition>.path`` in the config *data*.
    A leading byte-order mark is skipped, as git does."""
    section, found = "", []
    lines = iter(data.decode(errors="surrogateescape").removeprefix("\ufeff").split("\n"))
    for line in lines:
        rest = line.lstrip(_BLANK)
        while rest and rest[0] not in "#;":
            if rest[0] == "[":
                if not (header := _CONFIG_HEADER.match(rest)):
                    break
                section = header[1].lower().split(".")[0]
                rest = rest[header.end():].lstrip(_BLANK)
                continue
            key = _CONFIG_KEY.match(rest)
            if key and key[2] is not None and key[1].lower() == "path" and section in ("include", "includeif"):
                found.append(_config_value(key[2], lines))
            break
    return found


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _include_inside_git_dir(git_dir: Path, value: str) -> bool:
    """Whether the include path *value* of the config in *git_dir* names a file inside it. A
    relative path resolves against the folder of the including file: the git dir."""
    target = os.path.normpath(os.path.join(git_dir, os.path.expanduser(value)))
    roots = {os.path.normpath(git_dir), os.path.realpath(git_dir)}
    return any(_inside(path, root) for path in {target, os.path.realpath(target)} for root in roots)


def _planted_worktree_configs(repo: Path, git_dir: str) -> list[str]:
    """The ``config.worktree`` files of the git dir that exist, whatever the extension says: one
    becomes active once the host turns the extension on. Paths are relative to *repo*."""
    root = repo / git_dir
    found = [f"{git_dir}/config.worktree"] if os.path.lexists(root / "config.worktree") else []
    if not os.path.lexists(root / "worktrees"):
        return found
    if not stat.S_ISDIR(os.lstat(root / "worktrees").st_mode):
        return [*found, f"{git_dir}/worktrees (not a directory)"]
    try:
        with os.scandir(root / "worktrees") as entries:
            return found + sorted(
                f"{git_dir}/worktrees/{entry.name}/config.worktree" for entry in entries
                if os.path.lexists(os.path.join(entry.path, "config.worktree"))
            )
    except OSError as e:
        return [*found, f"{git_dir}/worktrees (cannot be read: {e.strerror})"]


def check_worktree_config(repo: Path, git_dir: str = ".git") -> str | None:
    """Why no host git may run on the store with the git dir *git_dir* of *repo*, None when it
    may: its config cannot be read as a regular file, sets ``extensions.worktreeConfig``, or
    includes a file inside the git dir; or a ``config.worktree`` file exists, also with the
    extension off; or a ``shallow`` file exists. A missing config is left to the mount row check.
    Creates nothing and runs no tool."""
    config = f"{git_dir}/config"
    try:
        # O_NONBLOCK keeps a FIFO planted in place of the file from blocking the open.
        fd = os.open(repo / config, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as e:
        return f"{config} cannot be read: {'a symlink' if e.errno == errno.ELOOP else e.strerror}"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return f"{config} is not a regular file"
        with os.fdopen(fd, "rb", closefd=False) as file:
            data = file.read(_CONFIG_MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > _CONFIG_MAX_BYTES:
        return f"{config} is larger than 1024 KiB"
    if WORKTREE_CONFIG_KEY.search(data):
        return (
            f"{config} sets extensions.worktreeConfig: a container can plant config.worktree files that host git "
            "(status) and host jj (git fetch) act on, with core.sshCommand, core.fsmonitor or core.hooksPath. "
            "Check them, then run `git config --unset extensions.worktreeConfig` (once sparse-checkout is not needed)"
        )
    if included := next((v for v in config_include_paths(data) if _include_inside_git_dir(repo / git_dir, v)), None):
        return (
            f"{config} includes '{included}', a file inside the git dir that the container can write "
            "(hooks, fsmonitor, ssh command). Check it, then remove the include"
        )
    if planted := _planted_worktree_configs(repo, git_dir):
        return (
            f"{planted[0]} exists: the container can write it, and it becomes active once extensions.worktreeConfig "
            "is turned on (core.sshCommand, core.fsmonitor, core.hooksPath). Check it, then delete it"
        )
    if os.path.lexists(repo / git_dir / "shallow"):
        return (
            f"{git_dir}/shallow exists: the container can write it, and it cuts the history host git sees "
            "(merge-base, for-each-ref --contains). Check it, then delete it; a truly shallow clone needs "
            "`git fetch --unshallow` first"
        )
    return None


def check_git_store(repo: Path) -> str | None:
    """Why no host git may run on the git-only repo *repo*, None when it may: the git dir
    fails ``check_git_dir`` or ``check_worktree_config``."""
    return check_git_dir(repo, ".git") or check_worktree_config(repo)


class GitBackend(VcsBackend):
    """VCS backend that delegates all operations to the ``git`` CLI.

    Used as a fallback when jujutsu is not installed. Maps jj-style workspace
    isolation to git worktrees, bookmarks to branches, and revsets to standard
    git revision syntax.
    """

    @property
    def name(self) -> str:
        return "git"

    @property
    def dir_name(self) -> str:
        return ".git"

    # -- repository discovery -------------------------------------------------

    @classmethod
    def detect_root(cls, start: Path | None = None) -> Path | None:
        """Walk upward from *start* looking for a ``.git/`` directory or file."""
        d = start or Path.cwd()
        while d != d.parent:
            git_path = d / ".git"
            if git_path.is_dir() or git_path.is_file():
                return cls._resolve_worktree_root(d)
            d = d.parent
        return None

    @staticmethod
    def _resolve_worktree_root(path: Path) -> Path:
        """If path is a git worktree, resolve to the main repository root."""
        if not (path / ".git").is_file():
            return path
        args = ["-C", str(path), "rev-parse",
                "--path-format=absolute", "--git-common-dir"]
        log.debug("$ git %s", " ".join(args))
        result = subprocess.run(["git"] + args, capture_output=True, text=True)
        log.debug("-> rc=%d", result.returncode)
        if result.returncode != 0:
            log.warning(
                "git %s failed (rc=%d): %s",
                " ".join(args),
                result.returncode,
                (result.stderr or "")[:400],
            )
            return path
        return Path(result.stdout.strip()).parent

    @staticmethod
    def _current_worktree_branch(path: Path) -> str:
        """Return the branch name checked out in the worktree at *path*."""
        args = ["-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"]
        log.debug("$ git %s", " ".join(args))
        result = subprocess.run(["git"] + args, capture_output=True, text=True)
        log.debug("-> rc=%d", result.returncode)
        if result.returncode != 0:
            log.warning(
                "git %s failed (rc=%d): %s",
                " ".join(args),
                result.returncode,
                (result.stderr or "")[:400],
            )
            return ""
        branch = result.stdout.strip()
        return branch if branch != "HEAD" else ""

    # -- helpers --------------------------------------------------------------

    def run(self, args: list[str], **kwargs) -> subprocess.CompletedProcess:
        """Run git with ``STORE_ENV`` and ``STORE_GIT_FLAGS``, so no replace ref, graft or commit-graph the
        container wrote is honoured. Setting ``GIT_GRAFT_FILE`` at all makes git print a deprecation
        hint, which is silenced."""
        kwargs["env"] = {**(kwargs.get("env") or os.environ), **STORE_ENV}
        return super().run(["-c", "advice.graftFileDeprecated=false", *STORE_GIT_FLAGS, *args], **kwargs)

    def _run_git(self, args: list[str], **kwargs) -> subprocess.CompletedProcess:
        """Run a git command against this repository."""
        return self.run(args, **kwargs)

    # -- workspace / worktree isolation ---------------------------------------

    def create_workspace(self, name: str, path: str, revision: str = "") -> str:
        """Create a git worktree with a new branch *name* at *revision*.

        Equivalent to jj's ``workspace add``. The branch is created automatically
        by ``git worktree add -b``.
        """
        cmd = ["worktree", "add", "-b", name, path]
        if revision:
            cmd.append(revision)
        result = self._run_git(cmd)
        return result.stdout + result.stderr

    def forget_workspace(self, name: str, path: str = "") -> str:
        """Remove a git worktree and optionally delete the tracking branch.

        *path* must be provided for git (unlike jj where name suffices).
        """
        output = ""
        if path:
            result = self._run_git(["worktree", "remove", "--force", path])
            output = result.stdout + result.stderr
        # Also prune stale worktree entries
        self._run_git(["worktree", "prune"])
        return output

    # -- branch management ----------------------------------------------------

    def create_branch(self, name: str, revision: str = "") -> str:
        """Create a git branch at *revision* (default: HEAD)."""
        cmd = ["branch", name]
        if revision:
            cmd.append(revision)
        return self._run_git(cmd).stdout

    def set_branch(self, name: str, revision: str) -> str:
        """Force-update a git branch to point at *revision*."""
        return self._run_git(["branch", "-f", name, revision]).stdout

    def delete_branch(self, name: str) -> str:
        """Delete a git branch (force-delete to handle unmerged branches)."""
        return self._run_git(["branch", "-D", name]).stdout

    # -- change creation and manipulation -------------------------------------

    def commit(self, message: str) -> str:
        """Stage all changes and commit.

        Unlike jj (which auto-tracks), git requires an explicit ``add -A`` step.
        """
        self._run_git(["add", "-A"])
        result = self._run_git(["commit", "-m", message])
        return result.stdout + result.stderr

    def squash(self, from_rev: str, into_rev: str) -> str:
        """Squash changes from *from_rev* into *into_rev* via cherry-pick and amend."""
        original = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
        self._run_git(["checkout", into_rev])
        self._run_git(["cherry-pick", "--no-commit", from_rev])
        result = self._run_git(["commit", "--amend", "--no-edit"])
        if original and original != "HEAD":
            self._run_git(["checkout", original])
        return result.stdout + result.stderr

    # -- diff -----------------------------------------------------------------

    def diff(self, revision: str = "", *, stat: bool = False) -> str:
        """Show the diff for a revision or the working tree.

        Without *revision*: working tree vs HEAD.
        With *revision*: changes introduced by that commit (parent..commit).
        """
        if not revision:
            cmd = ["diff", "HEAD"]
        else:
            parent_check = self._run_git(["rev-parse", f"{revision}~1"])
            base = f"{revision}~1" if parent_check.returncode == 0 else "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
            cmd = ["diff", f"{base}..{revision}"]
        if stat:
            cmd.append("--stat")
        result = self._run_git(cmd)
        if result.returncode != 0:
            return f"Error: {result.stderr.strip()}"
        return result.stdout

    def diff_between(self, from_rev: str, to_rev: str) -> str:
        """Generate a unified diff between two git revisions."""
        result = self._run_git(["diff", f"{from_rev}..{to_rev}"])
        if result.returncode != 0:
            return f"Error: {result.stderr.strip()}"
        return result.stdout

    def has_changes(self) -> bool:
        """Check whether the working tree has uncommitted changes."""
        result = self._run_git(["status", "--porcelain"])
        return bool(result.stdout.strip())

    def diff_stat_summary(self) -> tuple[int, str]:
        """Parse ``git diff --stat`` output to extract file count and names.

        Examines both staged and unstaged changes against HEAD.
        """
        result = self._run_git(["diff", "HEAD", "--stat"])
        if result.returncode != 0:
            return 0, ""
        lines = [l for l in result.stdout.strip().splitlines() if "|" in l]
        names = [l.split("|")[0].strip() for l in lines]
        return len(names), ", ".join(names)

    # -- file access ----------------------------------------------------------

    def file_show(self, revision: str, path: str) -> str | None:
        """Read a file from a git revision via ``git show <rev>:<path>``."""
        result = self._run_git(["show", f"{revision}:{path}"])
        if result.returncode != 0:
            return None
        return result.stdout

    # -- history / log --------------------------------------------------------

    def log(self, revision: str = "", template: str = "") -> str:
        """Run ``git log`` for a revision.

        *template* is treated as a git ``--format`` string if provided.
        Defaults to a compact one-line-per-commit format.
        """
        cmd = ["log"]
        if revision:
            cmd.append(revision)
        if template:
            cmd += [f"--format={template}"]
        else:
            cmd += ["--oneline", "-20"]
        result = self._run_git(cmd)
        if result.returncode != 0:
            return f"Error: {result.stderr.strip()}"
        return result.stdout

    def resolve_revision(self, revision: str) -> str:
        """Resolve a git revision spec to a full commit SHA."""
        result = self._run_git(["rev-parse", revision])
        if result.returncode != 0:
            raise RuntimeError(f"Failed to resolve revision '{revision}': {result.stderr.strip()}")
        return result.stdout.strip()

    def get_description(self, revision: str) -> str:
        """Get the commit message of a git revision."""
        result = self._run_git(["log", "--format=%B", "-1", revision])
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    # -- merge-base -----------------------------------------------------------

    def fork_point(self, branch_a: str, branch_b: str) -> str:
        """Find the merge-base (common ancestor) of two git branches."""
        result = self._run_git(["merge-base", branch_a, branch_b])
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to find merge-base of '{branch_a}' and '{branch_b}': "
                f"{result.stderr.strip()}"
            )
        return result.stdout.strip()
