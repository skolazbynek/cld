#!/usr/bin/env python3
# cld host broker -- the sshd ForceCommand target for the restricted broker key.
# sshd ignores whatever command the client asked for and delivers it in
# $SSH_ORIGINAL_COMMAND, which must have the shape:
#
#     <action> <session> <base64-argv>
#
# The broker serves ANY repo that has a running task-agent or ticket
# container -- no per-repo config, no whitelist. It resolves the target
# repo from the calling container's host-set labels (established at launch,
# not caller input): the single `org.cld.repo-root` for v1 kinds, or -- for a
# multi-repo ticket container -- the flat `org.cld.repo.<name>` labels, picked
# by a leading `--repo <name>` in the run-tests/graphql argv
# (LabelCaller.repo). The caller controls only: the action, a validated
# session id, and the decoded argv. Nothing is ever eval'd.
#
# Two domains, joined only by the Caller protocol: a caller source answers
# who called and what they may reach, and the actions consume those answers as
# plain values. Two sources exist: LabelCaller (docker labels) for the sshd
# route, and CliCaller (command-line flags, via `cld-brokerctl run`) for
# running an action from the host without any container.
#
# v1 sessions are all named `cld_agent_*` (task-agents -- the prefix is a
# leftover of the removed standing repo agent; kind is a label, not a name, see
# cld/docker.py:task_agent_container_name), plus the v2 ticket containers. Any
# of these may call `run-tests` / `list-containers` / `list-repos`. The
# `task-agent` launcher action's `start` resolves its `--repo` against the
# caller's mounted repos first, then the host registry (`cld repos add`, the
# operator's own allowlist) -- see resolve_task_agent_repo; its other ops need
# no repo at all (docs/design-ticket-task-agents.md).
#
# The regex below is a format check only, not an authorization boundary --
# the session doubles as the docker container name, and the label read below
# (set at launch, trusted) is what actually gates repo/target access.
#
# ADD AN ACTION: define a function named `action_<name>` (hyphens in <name>
# become underscores, so `run-tests` -> `action_run_tests`). It receives the
# Caller the dispatcher built and the decoded argv as a list, and asks the
# Caller only for what it needs (repo, revision) -- run-tests does it
# via `resolve_test_context`.
#
# Standard library only: the host's python3 runs this file directly, with no
# venv and no `cld` import.
import argparse
import base64
import errno
import hashlib
import http.client
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import time
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import cache, cached_property
from pathlib import Path
from typing import IO, NamedTuple, NoReturn, Protocol, Self, TextIO

# Broker settings: the process environment overlaid with broker.conf (load_conf).
CONF: dict[str, str] = {}

_DEFAULTS = {
    "RUNTESTS_IMAGE": "runtests:latest",
    "GRAPHQL_IMAGE": "graphqlserver:latest",
    # poetry install dominates a graphql start/restart on a venv cache miss (no
    # fast restart yet -- see docs/impl-graphql-broker-plan.md §11), so the
    # readiness budget has to cover it, not just a normal server boot.
    "GRAPHQL_START_TIMEOUT": "300",
    "GRAPHQL_QUERY_TIMEOUT": "30",
    "GRAPHQL_OUTPUT_MAX_BYTES": "65536",
    "SECRETS_STAGE_DIR": "~/.cld/broker-secrets",
    "TESTS_RUNS_ROOT": "~/.cld/runs",
    # GRAPHQL_URL_ALLOWLIST is deliberately left out here (see check_url_allowlisted):
    # empty means "no raw URLs at all", and that has to be the default, not a value
    # defaulted in code that's easy to lose track of.
}

_ASSIGNMENT_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", re.DOTALL)

# Where cld mounts the host ssh-agent into a container (cld/docker.py:
# _CONTAINER_SSH_AUTH_SOCK), and where the runner images get it.
_CALLER_AGENT_SOCK = "/run/host-ssh-agent.sock"
_RUNNER_AGENT_SOCK = "/run/ssh-agent.sock"


class Caller(Protocol):
    """The caller-derived inputs of every action. Methods are lazy, so an action
    never pays for -- or fails on -- an input it does not use."""

    name: str  # session id: names the caller's own resources (graphql server, task-agent parent)

    def repo(self, selector: str) -> str:
        """The host repo to act on; *selector* is the `--repo` name, "" if absent."""
        ...

    def mounted_repo(self, selector: str) -> str | None:
        """The host repo mounted in the caller under the name *selector*, None if none."""
        ...

    def revision(self, repo: str) -> str | None:
        """The caller's current commit in *repo*; None if it cannot be resolved."""
        ...

    def ssh_agent(self) -> str:
        """Host ssh-agent socket the caller's runs may use, "" for none."""
        ...


class Scope(NamedTuple):
    """A resolved (session, repo) pair: what the graphql ops act on."""

    session: str
    repo: str


class SecretsRefused(Exception):
    """A secrets or config file the broker will not read or mount; args[0] is the reason."""


class Secrets(NamedTuple):
    """A repo's `.env` as read at resolve time. *staged* is the private copy the
    containers mount (None when absent or not staged), *data* the bytes read."""

    source: str
    project_subdir: str
    staged: str | None
    data: bytes | None

    def mount_args(self) -> list[str]:
        return [] if self.staged is None else ["--mount", f"type=bind,src={self.staged},dst=/secrets/.env,readonly"]

    def values(self) -> dict[str, str]:
        return {} if self.data is None else parse_env_text(self.data.decode(errors="surrogateescape"))


class GraphqlConfig(NamedTuple):
    command: str
    port: str
    health_path: str


def _die(message: str, code: int) -> NoReturn:
    print(message, file=sys.stderr)
    sys.exit(code)


def _setting(key: str) -> str:
    return CONF.get(key) or _DEFAULTS.get(key, "")


def parse_env_text(text: str) -> dict[str, str]:
    """`[export] KEY=VALUE` lines of a shell-style env file, with the shell's
    quoting rules. Nothing is expanded or executed: `$VAR` stays literal, and
    any other line is skipped."""
    values = {}
    for line in text.splitlines():
        try:
            tokens = shlex.split(line)
        except ValueError:
            continue
        if tokens[:1] == ["export"]:
            tokens = tokens[1:]
        if tokens and (match := _ASSIGNMENT_RE.fullmatch(tokens[0])):
            values[match[1]] = match[2]
    return values


def parse_env_file(path: str) -> dict[str, str]:
    return parse_env_text(Path(path).read_text(errors="surrogateescape"))


def load_conf() -> None:
    """Overlay broker.conf on the environment. A conf assignment to a variable
    that is already exported (PATH above all) reaches child processes, the way
    sourcing it into a shell would; anything else stays broker-internal."""
    path = os.environ.get("CLD_BROKER_CONF") or "/etc/cld/broker.conf"
    try:
        parsed = parse_env_file(path)
    except OSError:
        parsed = {}
    CONF.update(os.environ, **parsed)
    os.environ.update({key: value for key, value in parsed.items() if key in os.environ})


# ---- process helpers -------------------------------------------------------

def _capture(argv: Sequence[str], stderr: int | None = subprocess.DEVNULL,
             cwd: str | None = None) -> subprocess.CompletedProcess[bytes]:
    """Run *argv* with stdout captured. A missing binary is exit 127, as in a shell."""
    try:
        return subprocess.run(argv, stdout=subprocess.PIPE, stderr=stderr, cwd=cwd)
    except OSError as e:
        if stderr is None:
            print(f"{argv[0]}: {e.strerror}", file=sys.stderr)
        return subprocess.CompletedProcess(argv, 127, b"")


def _text(result: subprocess.CompletedProcess[bytes]) -> str:
    # Trailing newlines stripped, like a shell command substitution.
    return os.fsdecode(result.stdout).rstrip("\n")


def _out(*argv: str) -> str | None:
    """stdout of *argv*, stderr discarded; None if it failed."""
    result = _capture(argv)
    return None if result.returncode else _text(result)


def _out_strict(*argv: str, stderr: int | None = subprocess.DEVNULL) -> str:
    """stdout of *argv*; exits the broker with its status if it failed. Kept for
    the lookups whose failure has always ended the request with no message."""
    result = _capture(argv, stderr)
    if result.returncode:
        sys.exit(result.returncode)
    return _text(result)


def _first_line(text: str) -> str:
    return text.split("\n", 1)[0]


def _exec(argv: list[str]) -> NoReturn:
    """Replace the broker with *argv*, so its stdout/stderr/exit code flow straight back."""
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execvp(argv[0], argv)
    except OSError as e:
        _die(f"{argv[0]}: {e.strerror}", 127 if isinstance(e, FileNotFoundError) else 126)


def _emit(data: bytes, stream: TextIO) -> None:
    stream.flush()
    stream.buffer.write(data)
    stream.buffer.flush()


def _label(container: str, key: str) -> str | None:
    return _out("docker", "inspect", container, "--format", f'{{{{index .Config.Labels "{key}"}}}}')


def _is_repo(path: str) -> bool:
    return os.path.isdir(f"{path}/.jj") or os.path.isdir(f"{path}/.git")


def _rm_container(name: str) -> None:
    _capture(["docker", "rm", "-f", name])


def _forget_workspace(repo: str, workspace: str) -> None:
    try:
        host_jj(repo, "workspace", "forget", workspace)
    except StoreRefused as e:
        print(f"[cld-broker] workspace {workspace} not forgotten: {e}", file=sys.stderr)


# ---- the secrets resolver (docs/design-secrets-containment.md §6) ------------
# The repo is the agent's to write, so every read of `.env` or `.cld/config.toml`
# walks the path one component at a time with O_NOFOLLOW and refuses a symlink
# anywhere below the repo. Symlinks above the repo are the operator's.

_SECRETS_MAX_BYTES = 1 << 20
# A staged copy is replaced on each use; one this old belongs to a run that is gone.
_STAGE_MAX_AGE = 24 * 3600
# What the stage dir holds: a copy named after its source, or a tempfile of an interrupted copy.
_STAGE_PREFIX = ".stage-"
_STAGE_NAME_RE = re.compile(rf"[0-9a-f]{{16}}\.env|{re.escape(_STAGE_PREFIX)}\w{{8}}")


def _csv_unsafe(value: str) -> bool:
    """Docker parses a --mount value as CSV: a comma or a quote in a path adds or ends a field."""
    return "," in value or '"' in value


def _below_repo_parts(rel: str) -> list[str]:
    parts = [part for part in rel.split("/") if part != "."]
    if rel.startswith("/") or ".." in parts or "" in parts:
        raise SecretsRefused("path below the repo must be relative, without '..' or empty parts")
    return parts


def _walk_read(root: str, parts: list[str], max_bytes: int) -> bytes | None:
    """Bytes of the regular file at *parts* below the open-able directory *root*, None if
    absent. Raises SecretsRefused on a symlink, a non-regular file or an oversize file."""
    try:
        dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise SecretsRefused(f"cannot read {root}: {e.strerror}") from None
    try:
        for part in parts[:-1]:
            try:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
            except FileNotFoundError:
                return None
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise SecretsRefused(f"symlink or non-directory at {part}") from None
                raise SecretsRefused(f"cannot read {part}: {e.strerror}") from None
            os.close(dir_fd)
            dir_fd = next_fd
        # O_NONBLOCK keeps a FIFO planted in place of the file from blocking the open.
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
        except FileNotFoundError:
            return None
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise SecretsRefused(f"symlink at {parts[-1]}") from None
            raise SecretsRefused(f"cannot read {parts[-1]}: {e.strerror}") from None
    finally:
        os.close(dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SecretsRefused(f"{parts[-1]} is not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as file:
            data = file.read(max_bytes + 1)
    except OSError as e:
        raise SecretsRefused(f"cannot read {parts[-1]}: {e.strerror}") from None
    finally:
        os.close(fd)
    if len(data) > max_bytes:
        raise SecretsRefused(f"{parts[-1]} is larger than {max_bytes // 1024} KiB")
    return data


def read_repo_file(repo: str, rel: str, max_bytes: int = _SECRETS_MAX_BYTES) -> bytes | None:
    """Bytes of the regular file *rel* in *repo*, None if absent; the walk of
    resolve_secrets without the copy. Raises SecretsRefused."""
    return _walk_read(repo, _below_repo_parts(rel), max_bytes)


def _mailbox_root() -> str:
    try:
        data = tomllib.loads((Path.home() / ".config" / "cld" / "config.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        data = {}
    return os.path.expanduser(CONF.get("CLD_MAILBOX_ROOT") or data.get("mailbox_root") or "~/.cld/mailboxes")


def _stage_dir() -> str:
    """The checked directory holding the private copies. Creates it 0700 on first use and
    removes the copies a crashed run left behind."""
    path = os.path.expanduser(_setting("SECRETS_STAGE_DIR"))
    if _csv_unsafe(path):
        raise SecretsRefused("the secrets stage directory path contains a comma or a quote")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass  # checked below
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SecretsRefused("the secrets stage directory is not a private directory of the broker user")
    real = Path(path).resolve()
    forbidden = (_mailbox_root(), CONF.get("VENV_CACHE_ROOT", "~/.cld/cache"), _setting("TESTS_RUNS_ROOT"),
                 "~/.claude", *host_registry().values())
    if any(root and real.is_relative_to(Path(os.path.expanduser(root)).resolve()) for root in forbidden):
        raise SecretsRefused("the secrets stage directory is inside a directory the containers write")
    for entry in os.scandir(path):
        if (_STAGE_NAME_RE.fullmatch(entry.name) and entry.is_file(follow_symlinks=False)
                and time.time() - entry.stat(follow_symlinks=False).st_mtime > _STAGE_MAX_AGE):
            Path(entry.path).unlink(missing_ok=True)
    return path


def resolve_secrets(repo: str, env_file: str, *, operator_path: bool = False, stage: bool = True) -> Secrets:
    """Read *env_file* without following a symlink below *repo* and, with *stage*, copy the
    bytes into a 0600 file of the broker's own. Only that copy is ever mounted. With
    *operator_path* the file is the operator's own, outside the repo: only its basename is
    not followed. Raises SecretsRefused."""
    if operator_path:
        root, parts = os.path.realpath(os.path.dirname(env_file)), [os.path.basename(env_file)]
    elif env_file.startswith(f"{repo}/"):
        root, parts = repo, _below_repo_parts(env_file.removeprefix(f"{repo}/"))
    else:
        raise SecretsRefused("path is outside the repo")
    subdir = "/".join(parts[:-1]) or "."
    data = _walk_read(root, parts, _SECRETS_MAX_BYTES)
    if not stage:
        return Secrets(env_file, subdir, None, data)
    staged = f"{_stage_dir()}/{hashlib.sha256(env_file.encode()).hexdigest()[:16]}.env"
    if data is None:
        Path(staged).unlink(missing_ok=True)
        return Secrets(env_file, subdir, None, None)
    fd, tmp = tempfile.mkstemp(prefix=_STAGE_PREFIX, dir=os.path.dirname(staged))  # 0600
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
        os.replace(tmp, staged)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return Secrets(env_file, subdir, staged, data)


# ---- the repo store: runner mounts and host jj (docs/design-secrets-containment.md §3.1, §5) ----
# The runner and server containers get only the jj/git store, never the checkout. The
# rows must equal cld/docker.py:_store_rows (the broker cannot import cld; see
# TestStoreMountParity). Nothing here creates a file or runs jj to repair a store.

_ORIGIN_MOUNT_LABEL = "org.cld.origin-mount=store-v1"

# (id file, legacy config file the id guards), relative to the repo.
_JJ_IDS = (
    (".jj/repo/config-id", ".jj/repo/config.toml"),
    (".jj/workspace-config-id", ".jj/workspace-config.toml"),
)


class StoreLayout(NamedTuple):
    jj: bool
    git_dir: str  # relative to the repo


class StoreRefused(Exception):
    """A repo store the broker will not run jj on or mount; args[0] is the denial message."""


def _kind(path: str) -> str | None:
    """``dir``, ``file``, ``symlink`` or ``other`` by ``lstat``; None when missing.
    Raises StoreRefused when the path cannot be examined (ENOTDIR, EACCES, ...)."""
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return None
    except OSError as e:
        raise StoreRefused(f"denied: cannot examine {path}: {e.strerror}") from None
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "dir"
    return "file" if stat.S_ISREG(mode) else "other"


def _not_prepared(repo: str) -> StoreRefused:
    return StoreRefused(f"denied: {repo} store is not prepared -- start a ticket on it once")


def _unsafe_store(repo: str, reason: str) -> StoreRefused:
    return StoreRefused(f"denied: repo {repo}: {reason} -- cannot mount its store safely")


def _require_kind(repo: str, rel: str, expected: str) -> None:
    kind = _kind(f"{repo}/{rel}")
    if kind != expected:
        raise _unsafe_store(repo, f"{rel} is {'missing' if kind is None else f'a {kind}'}, expected a {expected}")


# Both places a jj repo's git dir can be, and what the read-only `commondir` row holds: git and jj
# take their config and hooks from the folder that file names, "." being the git dir itself. These
# equal GIT_DIRS and COMMONDIR_PLACEHOLDER of cld/vcs/jj.py (TestStoreMountParity).
_GIT_DIRS = (".git", ".jj/repo/store/git")
_COMMONDIR_PLACEHOLDER = "."


def check_git_dir(repo: str, git_dir: str) -> None:
    """The launcher's cld/vcs/jj.py:check_git_dir. The git dir root is read-write for the agent: a
    `commondir` other than the placeholder moves the config and hooks host git and jj load to a
    folder it writes, and `modules/` holds submodule configs and hooks that no row protects.
    Raises StoreRefused."""
    commondir = f"{git_dir}/commondir"
    if _kind(f"{repo}/{commondir}") is not None:
        try:
            content = read_repo_file(repo, commondir, len(_COMMONDIR_PLACEHOLDER))
        except SecretsRefused:
            content = None
        if content != _COMMONDIR_PLACEHOLDER.encode():
            raise _unsafe_store(repo, f"{commondir} exists: git and jj would load their config and hooks "
                                      "from the folder it names. Check it, then delete it")
    if _kind(f"{repo}/{git_dir}/modules") is not None:
        raise _unsafe_store(repo, f"{git_dir}/modules exists: git submodules are not supported yet")


def check_jj_ids(repo: str) -> None:
    """Both jj ids of *repo* must be regular files, and neither git dir may fail check_git_dir.
    With an id missing any jj call would migrate a legacy config file, planted or not, into the
    host's jj config (design A10). Raises StoreRefused."""
    for rel, legacy in _JJ_IDS:
        if (kind := _kind(f"{repo}/{rel}")) is None:
            if _kind(f"{repo}/{legacy}") is not None:
                raise _unsafe_store(repo, f"unmigrated jj config {legacy}: check it, then run jj once on the host")
            raise _not_prepared(repo)
        if kind != "file":
            raise _unsafe_store(repo, f"{rel} is a {kind}, expected a file")
    for git_dir in _GIT_DIRS:
        check_git_dir(repo, git_dir)


@cache
def _host_jj_version() -> str:
    """Output of ``jj --version``, empty when jj is not installed. Asked once per broker
    process. cwd is away from any repo: even this call must not load a repo config."""
    return _text(_capture(["jj", "--version"], cwd="/"))


def require_host_jj() -> None:
    """The launcher's minimum (cld/docker.py:_check_jj_version) on the broker's own jj: its
    PATH comes from broker.conf, and a jj older than 0.38 also reads the agent-writable repo
    config files. Raises StoreRefused."""
    version = _host_jj_version()
    found = re.search(r"(\d+)\.(\d+)", version)
    if not found or (int(found[1]), int(found[2])) < _JJ_MIN_VERSION:
        raise StoreRefused(f"denied: host jj '{version or 'not found'}' is older than "
                           f"{'.'.join(map(str, _JJ_MIN_VERSION))}")


def host_jj(repo: str, *args: str, snapshot: bool = False) -> str | None:
    """stdout of host ``jj -R <repo> <args>``, None if it failed or *repo* has no jj store.
    Every host jj call on a repo goes through here: it runs after check_jj_ids, the git_target
    check and require_host_jj, and never without them. Without *snapshot* the call leaves the
    working copy alone. Raises StoreRefused."""
    if _kind(f"{repo}/.jj") is None:
        return None
    check_jj_ids(repo)
    _git_target_layout(repo)
    require_host_jj()
    return _out("jj", "-R", repo, *(() if snapshot else ("--ignore-working-copy",)), *args)


def store_rows(layout: StoreLayout) -> list[tuple[str, str, bool]]:
    """The eleven mount rows as ``(path, kind, readonly)``; the path is the same under the host
    repo and under the container root. The ``commondir`` row is a placeholder that keeps the
    agent from creating that file."""
    git = layout.git_dir
    rows = [
        (".jj", "dir", False),
        (".jj/working_copy", "dir", True),
        (".jj/repo", "dir", False),
        (".jj/repo/config-id", "file", True),
        (".jj/repo/store", "dir", False),
        (".jj/repo/store/git_target", "file", True),
    ] if layout.jj else []
    rows += [(git, "dir", False), (f"{git}/config", "file", True), (f"{git}/hooks", "dir", True),
             (f"{git}/commondir", "file", True)]
    if layout.jj:
        rows.append((".jj/workspace-config-id", "file", True))
    return rows


def _git_target_layout(repo: str) -> StoreLayout:
    """The layout of a jj repo, if ``git_target`` names the repo's own git dir."""
    try:
        raw = read_repo_file(repo, ".jj/repo/store/git_target", 4096)
    except SecretsRefused as e:
        raise _unsafe_store(repo, str(e)) from None
    store = f"{repo}/.jj/repo/store"
    target = os.path.normpath(os.path.join(store, (raw or b"").decode(errors="surrogateescape").strip()))
    bases = {os.path.normpath(repo), os.path.realpath(repo)}
    for git_dir in _GIT_DIRS:
        if target in {os.path.normpath(os.path.join(base, git_dir)) for base in bases}:
            return StoreLayout(jj=True, git_dir=git_dir)
    raise _unsafe_store(repo, f"git_target points to {target}, outside this repo's git dirs")


def store_layout(repo: str) -> StoreLayout:
    """The launcher preflight (cld/docker.py:prepare_store_mounts) without its repairs: it
    refuses where the launcher would create an id, ``hooks`` or the ``commondir`` placeholder.
    Raises StoreRefused."""
    if _kind(f"{repo}/.jj") is not None:
        _require_kind(repo, ".jj", "dir")
        if _kind(f"{repo}/.jj/repo") == "file":
            raise _unsafe_store(repo, "secondary jj workspace (.jj/repo is a file)")
        for rel, expected in ((".jj/repo", "dir"), (".jj/repo/store", "dir"), (".jj/repo/store/git_target", "file")):
            _require_kind(repo, rel, expected)
        check_jj_ids(repo)
        layout = _git_target_layout(repo)
    elif (git_kind := _kind(f"{repo}/.git")) == "file":
        raise _unsafe_store(repo, "git worktree (.git is a file)")
    elif git_kind is None:
        raise _unsafe_store(repo, "neither a .jj nor a .git directory")
    else:
        layout = StoreLayout(jj=False, git_dir=".git")
    check_git_dir(repo, layout.git_dir)
    for rel, expected, _ in store_rows(layout):
        if rel in (f"{layout.git_dir}/hooks", f"{layout.git_dir}/commondir") and _kind(f"{repo}/{rel}") is None:
            raise _not_prepared(repo)
        _require_kind(repo, rel, expected)
    return layout


def store_mounts(repo: str, root: str) -> list[str]:
    """``--mount`` arguments of the store rows, host repo at *repo*, container side at *root*.
    ``--mount`` instead of ``-v``: a missing source fails the run and is not created on the host."""
    if _csv_unsafe(repo):
        raise _unsafe_store(repo, "its path contains a comma or a quote, which would split the --mount value")
    args: list[str] = []
    for rel, _, readonly in store_rows(store_layout(repo)):
        args += ["--mount", f"type=bind,src={repo}/{rel},dst={root}/{rel}" + (",readonly" if readonly else "")]
    return args


_JJ_VERSION_LABEL = "org.cld.jj-version"
_JJ_MIN_VERSION = (0, 38)


def require_jj_image(image: str) -> None:
    """Exit 3 unless *image* labels a jj of 0.38 or newer (set from the JJ_VERSION build arg).
    Older jj also reads the repo's own config files, which the container can write."""
    version = _label(image, _JJ_VERSION_LABEL) or ""
    found = re.match(r"(\d+)\.(\d+)", version)
    if not found or (int(found[1]), int(found[2])) < _JJ_MIN_VERSION:
        _die(f"denied: image {image} has no label {_JJ_VERSION_LABEL} of "
             f"{'.'.join(map(str, _JJ_MIN_VERSION))} or newer (found '{version}') -- rebuild it with its build.sh", 3)


def store_container_args(repo: str, image: str) -> list[str]:
    """What replaces ``-v <repo>:/repo`` in a runner or server ``docker run`` of *image*: the store
    at /repo, the label that keeps the launcher from warning about the container, and no
    capabilities and no new privileges, so code under test cannot remount the read-only rows. Exits 3 on a refused store
    or an image whose jj is too old."""
    try:
        mounts = store_mounts(repo, "/repo")
    except StoreRefused as e:
        _die(str(e), 3)
    require_jj_image(image)
    return [*mounts, "--label", _ORIGIN_MOUNT_LABEL, "--cap-drop=ALL", "--security-opt=no-new-privileges"]


def _commit_id(repo: str, revset: str, *, snapshot: bool = False) -> str | None:
    try:
        return host_jj(repo, "log", "--no-graph", "-n1", "-r", revset, "-T", "commit_id", snapshot=snapshot)
    except StoreRefused as e:
        _die(str(e), 3)


# Read a single quoted-string key from a flat cld TOML config (<repo>/.cld/config.toml).
# Empty if the file or key is absent. Avoids a TOML parser in the broker.
def cld_conf_get(repo: str, key: str) -> str:
    try:
        data = read_repo_file(repo, ".cld/config.toml")
    except SecretsRefused as e:
        _die(f"denied: config file {repo}/.cld/config.toml: {e}", 3)
    if data is None:
        return ""
    pattern = re.compile(rf'\s*{re.escape(key)}\s*=\s*"([^"]*)"')
    return next((match[1] for line in data.decode(errors="surrogateescape").splitlines()
                 if (match := pattern.match(line))), "")


# The host repo registry (`cld repos add`): name -> path of each [repos.<name>]
# table in the user config, the file cld/config.py:_user_config_path reads -- the
# broker runs as that same operator. Hand-edited, hence a real TOML parser rather
# than cld_conf_get's line scan. {} when the file is absent or unparsable.
def host_registry() -> dict[str, str]:
    try:
        data = tomllib.loads((Path.home() / ".config" / "cld" / "config.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    repos = data.get("repos")
    if not isinstance(repos, dict):
        return {}
    return {name: os.path.expanduser(entry["path"]) for name, entry in repos.items()
            if isinstance(entry, dict) and isinstance(entry.get("path"), str) and entry["path"]}


# ---- caller sources -------------------------------------------------------

@dataclass
class LabelCaller:
    """The sshd route's Caller: the session container's host-set docker labels
    (org.cld.kind, org.cld.repo-root or org.cld.repo.<name>)
    and its jj workspace. Labels are set at launch, never by the caller, which
    is what makes them an authorization source."""

    name: str
    kind: str
    repo_root: str  # empty for a ticket, which carries org.cld.repo.<name> labels instead

    # The session doubles as the container name. A ticket's repo stays unresolved
    # here: it depends on the action's --repo, see repo().
    @classmethod
    def from_session(cls, session: str) -> Self:
        if (kind := _label(session, "org.cld.kind")) is None:
            _die(f"no cld container for session {session}", 3)
        repo_root = ""
        if kind != "ticket":
            repo_root = _label(session, "org.cld.repo-root") or ""
            if not (repo_root and _is_repo(repo_root)):
                _die(f"no task-agent container for session {session}", 3)
        return cls(session, kind, repo_root)

    # Repo selection (docs/design-ticket-containers.md section 6.1): a ticket
    # container mounts several repos, resolved against its flat
    # `org.cld.repo.<name>` labels -- the flat encoding keeps this to plain line
    # parsing. No selector with exactly one labeled repo uses it (the single-repo
    # convenience); several without one is an error. A v1 kind keeps its
    # repo-root and refuses a selector. Names are matched against enumerated
    # labels, never spliced into an inspect format string, so a hostile name
    # cannot template-inject.
    def repo(self, selector: str) -> str:
        if self.kind != "ticket":
            if selector:
                _die(f"denied: --repo is only for ticket containers ({self.name} is '{self.kind or 'unlabeled'}')", 2)
            return self.repo_root
        mounted = " ".join(self._mounts) or "none"
        if not selector:
            if len(self._mounts) != 1:
                _die(f"denied: {self.name} mounts {len(self._mounts)} repos -- pass --repo <name> (mounted: {mounted})", 3)
            repo = next(iter(self._mounts.values()))
        else:
            repo = self._mounts.get(selector, "")
        if not repo:
            _die(f"denied: repo '{selector}' is not mounted in {self.name} (mounted: {mounted})", 3)
        if not _is_repo(repo):
            _die(f"denied: repo path '{repo}' for {self.name} is not a repo", 3)
        return repo

    # A v1 kind mounts its repo-root under no name.
    def mounted_repo(self, selector: str) -> str | None:
        return self._mounts.get(selector) if self.kind == "ticket" else None

    # name -> host path of every org.cld.repo.<name> label, read once per request.
    @cached_property
    def _mounts(self) -> dict[str, str]:
        labels = _capture(["docker", "inspect", self.name, "--format",
                           "{{range $k, $v := .Config.Labels}}{{$k}}={{$v}}{{println}}{{end}}"])
        return {key.removeprefix("org.cld.repo."): path
                for key, _, path in (line.partition("=") for line in os.fsdecode(labels.stdout).splitlines())
                if key.startswith("org.cld.repo.")}

    # "<session>@" is the *workspace* tip, not the session bookmark. The bookmark
    # is set once at first container launch
    # (imgs/claude-devcontainer/entrypoint-claude-devcontainer.sh) and never moved
    # again, so it freezes at the container's first `jj commit` -- the normal state
    # once an agent starts working, which left this resolving one change behind
    # the code the caller just wrote. Workspace name == bookmark name == container
    # name == session (verified via `jj workspace list`), and `<session>@` is
    # the same workspace-scoped-`@` idiom `cld/vcs/detect.py` already uses.
    # --ignore-working-copy matters here too: without it, `jj -R <repo> log`
    # snapshots the *host's* default workspace as a side effect, which would
    # violate the broker's store-reading-only posture (docs/design-cld-broker.md
    # §9) for no freshness gain -- watchman already wrote the container's own
    # snapshot independently.
    def revision(self, repo: str) -> str | None:
        return _commit_id(repo, f"{self.name}@")

    # The agent already forwarded into the caller (cld/docker.py:stage_ssh_agent),
    # so a run can do with the key exactly what the caller itself can, no more:
    # a container launched without an agent gets none here either.
    def ssh_agent(self) -> str:
        return _out("docker", "inspect", self.name, "--format",
                    f'{{{{range .Mounts}}}}{{{{if eq .Destination "{_CALLER_AGENT_SOCK}"}}}}'
                    "{{.Source}}{{end}}{{end}}") or ""


@dataclass
class CliCaller:
    """The host route's Caller: every input given on the command line by the
    operator (`cld-brokerctl run`), no container involved. The operator already
    holds everything the broker guards, so nothing here is an authorization
    boundary."""

    name: str
    repo_path: str
    revset: str

    # One repo, given by -C: a ticket's --repo selector has nothing to select here.
    def repo(self, selector: str) -> str:
        if selector:
            _die(f"denied: --repo selects a ticket container's mounted repo -- on the host pass -C <path> "
                 f"(using {self.repo_path})", 2)
        return self.repo_path

    # -C is the host's selector, so no repo has a name here.
    def mounted_repo(self, selector: str) -> str | None:
        return None

    # No --ignore-working-copy, unlike LabelCaller: the default revset `@` is the
    # operator's own working copy, and snapshotting it first is what makes
    # uncommitted edits part of the run.
    def revision(self, repo: str) -> str | None:
        return _commit_id(repo, self.revset, snapshot=True)

    # `cld-brokerctl run` clears the environment, so the operator's agent comes
    # from broker.conf, as for the task-agent launcher (stage_agent_socket).
    def ssh_agent(self) -> str:
        return CONF.get("SSH_AUTH_SOCK", "")


# ---- actions --------------------------------------------------------------
# Each runs on the host as your user and receives the decoded pytest/tool argv.
# `_exec` so the child's stdout/stderr/exit code flow straight back.

# Shared by resolve_test_context, resolve_graphql_config, and graphql alias
# lookup: the project's own .env, following pyproject_dir the same way
# PROJECT_SUBDIR does for run-tests -- pyproject.toml and .env are assumed to
# share a directory (docs/design-cld-broker.md §13). No revision resolution
# here -- callers that only need the secrets path (e.g. a graphql alias lookup
# for a repo whose graphql_command isn't even set, or a git-backed repo where
# jj would exit 3) must not pay for jj resolution they don't need.
def resolve_secrets_env_file(repo: str, *, stage: bool = True) -> Secrets:
    subdir = cld_conf_get(repo, "pyproject_dir") or "."
    source = f"{repo}/.env" if subdir == "." else f"{repo}/{subdir}/.env"
    try:
        return resolve_secrets(repo, source, stage=stage)
    except SecretsRefused as e:
        _die(f"denied: secrets file {source}: {e}", 3)
    except OSError as e:
        _die(f"denied: secrets file {source}: cannot stage a copy: {e.strerror}", 3)


def resolve_revision(caller: Caller, repo: str) -> str:
    if (rev := caller.revision(repo)) is None:
        _die(f"cannot resolve revision for workspace {caller.name} in {repo}", 3)
    return rev


# run-tests needs the session's current revision + the project's .env; resolve
# those lazily here (not in the shared dispatcher) so read-only actions like
# list-containers don't depend on the session's revision being resolvable.
def resolve_test_context(caller: Caller, repo: str) -> tuple[str, Secrets]:
    return resolve_revision(caller, repo), resolve_secrets_env_file(repo)


# The caller's ssh-agent for the runner images, which fetch `git+ssh://`
# dependencies with it (scripts/ssh-wrapper.sh). Never fatal: without a live
# socket the run goes on keyless, and only an ssh dependency then fails.
def _agent_mount(sock: str) -> list[str]:
    if not sock:
        return []
    try:
        live = stat.S_ISSOCK(os.stat(sock).st_mode)
    except OSError:
        live = False
    if not live:
        print(f"[cld-broker] ssh-agent socket {sock} is not a live socket -- running without it",
              file=sys.stderr)
        return []
    return ["-v", f"{sock}:{_RUNNER_AGENT_SOCK}", "-e", f"SSH_AUTH_SOCK={_RUNNER_AGENT_SOCK}"]


def venv_cache_dir(root: str, repo: str) -> str:
    """The cache folder of one repo below *root*. Code under test writes it, and a venv or a
    downloaded wheel in it is later run or installed by a run of the same repo, with that repo's
    `.env`: the folder must not be shared with another repo. Named by the repo's real path."""
    real = os.path.realpath(repo)
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", os.path.basename(real))
    return f"{root}/repos/{name}-{hashlib.sha256(real.encode()).hexdigest()[:16]}"


def _venv_cache_args(repo: str, image: str) -> list[str]:
    """The -v/-e args wiring *repo*'s own host-side venv + poetry download cache
    (scripts/venv-cache.sh) into a run of *image*; none when the cache is
    disabled. The image id is part of the cache key -- a container cannot read its own."""
    # Not _setting: VENV_CACHE_ROOT set empty in the conf disables the cache.
    if not (root := CONF.get("VENV_CACHE_ROOT", os.path.expanduser("~/.cld/cache"))):
        return []
    cache = venv_cache_dir(root, repo)
    for sub in ("venvs", "poetry", "locks"):
        os.makedirs(f"{cache}/{sub}", exist_ok=True)
    image_id = _out_strict("docker", "image", "inspect", "--format", "{{.Id}}", image, stderr=None)
    return ["-v", f"{cache}:/cache", "-e", f"VENV_CACHE_IMAGE_ID={image_id}"]


def run_tests(repo: str, rev: str, secrets: Secrets, agent: str, argv: list[str]) -> NoReturn:
    _exec([
        "docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
        *store_container_args(repo, _setting("RUNTESTS_IMAGE")), *secrets.mount_args(), *_agent_mount(agent),
        *_venv_cache_args(repo, _setting("RUNTESTS_IMAGE")),
        "-e", f"REVISION={rev}", "-e", f"PROJECT_SUBDIR={secrets.project_subdir}",
        _setting("RUNTESTS_IMAGE"), *argv,
    ])


def action_run_tests(caller: Caller, argv: list[str]) -> None:
    name, argv = parse_repo_arg(argv)
    repo = caller.repo(name)
    run_tests(repo, *resolve_test_context(caller, repo), caller.ssh_agent(), argv)


# Enumerate cld containers for the messenger / `cld task-agent status`.
# Read-only: the sole argv is an optional kind filter
# (task-agent|ticket, or the legacy agent). Emits one
# tab-separated `name<TAB>kind<TAB>repo<TAB>raw-status` line per container.
def action_list_containers(caller: Caller, argv: list[str]) -> None:
    kind = argv[0] if argv else ""
    label = f"label=org.cld.kind={kind}" if kind else "label=org.cld.kind"
    result = _capture(["docker", "ps", "-a", "--filter", label, "--format", "{{.Names}}\t{{.Status}}"],
                      stderr=None)
    for line in _text(result).splitlines():
        name, _, status = line.partition("\t")
        if not name:
            continue
        labels = _out("docker", "inspect", name, "--format",
                      '{{index .Config.Labels "org.cld.kind"}}|{{index .Config.Labels "org.cld.repo-root"}}')
        if labels is None:
            continue
        ckind, _, repo = labels.partition("|")
        print(f"{name}\t{ckind}\t{repo}\t{status}")
    sys.exit(result.returncode)


# The host registry for `cld repos` in a container: one tab-separated
# `name<TAB>path<TAB>mounted` line per entry, mounted being yes/no for the caller.
# A read of the operator's own config, so no argv and nothing to authorize.
def action_list_repos(caller: Caller, argv: list[str]) -> None:
    for name, path in host_registry().items():
        print(f"{name}\t{path}\t{'yes' if caller.mounted_repo(name) else 'no'}")


# The task-agent launcher: a container that pushes a deliverable branch
# needs the host user's ssh-agent, and `stage_ssh_agent` (cld/docker.py) forwards
# whatever socket $SSH_AUTH_SOCK names. sshd builds a fresh session environment,
# so that variable only exists here if broker.conf sets it -- and a conf
# assignment is not exported, hence this. Agent *forwarding* over the broker
# connection is not an alternative: that socket dies with the ssh session, while
# the container outlives it. Never fatal, matching stage_ssh_agent: a dead path
# is warned about and the launch proceeds without a key, as it did before.
def stage_agent_socket() -> None:
    if not (sock := CONF.get("SSH_AUTH_SOCK")):
        return
    try:
        live = stat.S_ISSOCK(os.stat(sock).st_mode)
    except OSError:
        live = False
    if live:
        os.environ["SSH_AUTH_SOCK"] = sock
    else:
        print(f"[cld-broker] SSH_AUTH_SOCK={sock} is not a live socket -- "
              "launching without agent forwarding", file=sys.stderr)
        os.environ.pop("SSH_AUTH_SOCK", None)


# run-tests / graphql / task-agent start take an optional leading `--repo <name>`,
# the selector a multi-repo ticket caller passes to Caller.repo (or, for start,
# resolve_task_agent_repo). Consume it off an action's argv, returning the name
# ("" if absent) and the rest.
def parse_repo_arg(argv: list[str]) -> tuple[str, list[str]]:
    match argv:
        case ["--repo", *rest]:
            name, rest = (rest[0], rest[1:]) if rest else ("", [])
        case [first, *rest] if first.startswith("--repo="):
            name = first.removeprefix("--repo=")
        case _:
            return "", argv
    if not name:
        _die("denied: --repo needs a repo name", 2)
    return name, rest


# task-agent start's repo: the caller's own mount by name, else the host registry.
# Broader than Caller.repo on purpose -- a new container needs no mount in the
# caller, only a path the operator registered (docs/design-ticket-task-agents.md
# §3.1). run-tests / graphql keep Caller.repo's mounted-only contract. No
# selector keeps Caller.repo's single-repo default.
def resolve_task_agent_repo(caller: Caller, selector: str) -> str:
    if not selector:
        return caller.repo("")
    if not (repo := caller.mounted_repo(selector) or host_registry().get(selector)):
        _die(f"denied: '{selector}' is not one of {caller.name}'s mounted repos and is not registered on "
             f"the host either -- `cld repos add {selector} <path>` on the host first, or pick a mounted name", 3)
    if not _is_repo(repo):
        _die(f"denied: repo '{selector}' at '{repo}' is not a repo", 3)
    return repo


# Launch / manage a task-scoped agent (docs/design-task-agents.md §9). `start`
# takes a leading `--repo <name>` (resolve_task_agent_repo); the other ops act on
# an agent by name, which already carries its repo, so they need none. Three argv
# rules make this safe to expose to a container:
#
#   --force   denied outright. Overriding a reap-readiness refusal is a human act;
#             a container must not be able to discard uncaptured work or break a
#             third agent's edge (§7).
#   --parent  denied from the caller and put by us before the caller's tokens as the
#             validated session, so an agent's recorded owner is host-set and cannot be forged.
#   --        denied: it ends option parsing on the host, so a token after it would
#             be a positional that skips the `@ref` check.
#   prompts   every positional must be an `@ref`. Refs are resolved host-side and their
#             text composed into the new container's brief, so a bare path would let a
#             container read any host file the user can. The container-side client folds
#             its own local files into `-p` instead (docs/design-prompt-chaining.md §4);
#             cld.prompts contains the escape check for the refs themselves.
def action_task_agent(caller: Caller, argv: list[str]) -> None:
    if not argv:
        _die("denied: task-agent needs an op", 2)
    op, *args = argv
    # Exactly the ops the container route delegates. `transcript` is absent on
    # purpose: the mailbox is bind-mounted into the caller, never needing the host.
    if op not in ("start", "status", "logs", "shutdown"):
        _die(f"denied: bad task-agent op '{op}'", 2)
    stage_agent_socket()

    # Both spellings of each denial: click accepts `--opt=value`, so matching the bare
    # token alone would let `--parent=<other-master>` through and forge an owner.
    for arg in args:
        if arg == "--":
            _die("denied: '--' is not accepted", 2)
        if arg == "--force" or arg.startswith("--force="):
            _die("denied: --force is host-only", 2)
        if arg == "--parent" or arg.startswith("--parent="):
            _die("denied: --parent is set by the broker", 2)

    # start's positionals are prompt refs, and only `@refs` may cross: they name files
    # in the *target repo's* prompts tree, which the host resolves. Option values are
    # still not ref-checked -- task text legitimately contains slashes, so checking them
    # would deny ordinary work like -p "fix cld/cli.py" -- but arity is enumerated, not
    # guessed: only these options of `cld task-agent start` take a *separate* value, so
    # only they swallow the next token. Every other `-*` token (a flag, or the
    # `--opt=value` form, which carries its value inside the token) consumes nothing;
    # assuming it did skipped the positional after it, letting a bare path cross.
    if op == "start":
        selector, args = parse_repo_arg(args)
        skip_value = False
        for arg in args:
            if skip_value:
                skip_value = False
            elif arg in ("-n", "--name", "-p", "--prompt", "--branch", "-m", "--model",
                         "-r", "--revision", "--peer"):
                skip_value = True
            elif not arg.startswith(("-", "@")):
                _die(f"denied: prompt ref '{arg}' must be an @ref, not a path", 2)
        # Host-side start finds its repo from cwd.
        repo = resolve_task_agent_repo(caller, selector)
        try:
            os.chdir(repo)
        except OSError:
            _die(f"cannot cd to {repo}", 3)
    # --parent is what makes ownership host-set: it stamps the spawn, scopes the roster
    # and gates the reap. `logs` has no such option (a read needs no authority), so
    # adding it there would just make cld reject the argv.
    parent = [] if op == "logs" else ["--parent", caller.name]
    _exec(["cld", "task-agent", op, *parent, *args])


# ---- graphql: server lifecycle + credentialed queries over the broker -----
# See docs/impl-graphql-broker-plan.md for the design; this block implements
# §3-§6. One server per session (`cld_gql_<session>`, jj workspace
# `gql-<session>`), started from the `graphqlserver` image (§2) at the
# session's current revision, ops: start/stop/restart/status/logs (server
# lifecycle) and query/introspect/endpoints (client, credentialed by the
# broker so the container never holds a token).

# Per-repo graphql config (§3), soft: does NOT require graphql_command, so
# status/logs/stop/endpoints still work (or report "not configured") for a
# repo that has never been wired up. Only resolve_graphql_context (below,
# used by `start`) hard-requires it -- that's the op about to spend a
# poetry install on it. cld_conf_get parses double-quoted strings only:
# `graphql_port = 8000` silently yields nothing, it must be `= "8000"`.
def resolve_graphql_config(repo: str) -> GraphqlConfig:
    config = GraphqlConfig(
        cld_conf_get(repo, "graphql_command"),
        cld_conf_get(repo, "graphql_port") or "8000",
        cld_conf_get(repo, "graphql_health_path") or "/graphql",
    )
    # Both are repo-written and end up in a URL, `-p`, `docker port` and an env var.
    if not (config.port.isascii() and config.port.isdecimal() and 0 < int(config.port) < 65536):
        _die(f"denied: graphql_port '{config.port}' in {repo}/.cld/config.toml is not a port number", 3)
    parts = urllib.parse.urlsplit(config.health_path)
    if not config.health_path.startswith("/") or parts.scheme or parts.netloc:
        _die(f"denied: graphql_health_path '{config.health_path}' in {repo}/.cld/config.toml "
             "must be a path starting with '/'", 3)
    return config


def local_url(host_port: str, health_path: str) -> str:
    """The server's URL from its own parts: the health path (resolve_graphql_config checked it)
    can only add path and query, never change the host."""
    parts = urllib.parse.urlsplit(health_path)
    return urllib.parse.urlunsplit(("http", host_port, parts.path, parts.query, ""))


# start's context: config (hard-required -- no guessed default, the old MCP's
# `poetry run python manage.py` default was a lide-api-shaped guess and is not
# being carried over) + the secrets path.
def resolve_graphql_context(repo: str) -> tuple[Secrets, GraphqlConfig]:
    secrets = resolve_secrets_env_file(repo)
    config = resolve_graphql_config(repo)
    if not config.command:
        _die(f"denied: graphql_command is not set in {repo}/.cld/config.toml -- add "
             'graphql_command = "..." (quotes required; cld_conf_get silently '
             "ignores an unquoted value)", 3)
    return secrets, config


# GRAPHQL_BIND resolved lazily (only `start` publishes a port) and only if the
# operator hasn't pinned one in broker.conf. Bridge gateway, not 0.0.0.0 --
# binding the wildcard address would expose a real-credentials server to the
# whole LAN, mirroring the sshd's own bridge-only posture
# (broker/sshd_cld_broker.conf).
def resolve_graphql_bind() -> str:
    if bind := CONF.get("GRAPHQL_BIND"):
        return bind
    gateway = _out("docker", "network", "inspect", "bridge", "-f", "{{(index .IPAM.Config 0).Gateway}}")
    return "172.17.0.1" if gateway is None else gateway


# Nothing reaps what `start` spawns: cld-brokerctl.sh manages only the sshd,
# and no host-side hook fires when a session container is reaped. Run at the
# top of `start` and `status` so staleness never survives past the next
# lookup, and exposed standalone as `cld-brokerctl.sh graphql-sweep` (the
# `graphql-sweep` CLI route, main_cli) for an operator to reap after a host
# reboot without starting any session. Never fatal -- a sweep failure must
# not block the caller's own request. *announce* prints a line per reaped server.
def sweep_gql_orphans(announce: bool = False) -> None:
    servers = _capture(["docker", "ps", "-a", "--filter", "label=org.cld.gql-session", "--format", "{{.Names}}"],
                       stderr=None)
    for container in _text(servers).split():
        if (session := _label(container, "org.cld.gql-session")) is None:
            continue
        # Deliberately "exists", not "running": a stopped-but-not-removed
        # session container is expected to resume (cld persistence model, see
        # CLAUDE.md), and reaping its graphql server on every stop would
        # defeat the point of this being an *orphan* sweep -- only a session
        # that is actually gone (container removed) should cost its server.
        if not _capture(["docker", "inspect", session]).returncode:
            continue  # owning session exists -- keep
        repo = _label(container, "org.cld.gql-repo")
        if announce:
            print(f"reaping orphaned graphql server {container} (session {session} gone)")
        _rm_container(container)
        if repo:
            _forget_workspace(repo, f"gql-{session}")


# Two host-side filters applied to everything logs/query/introspect return --
# nothing but run-tests bounds broker output today (docs/impl-graphql-broker-plan.md
# §6). Masking happens here, not client-side in the container's MCP: a server
# holding real DB credentials can echo a DSN or token in an error body, and by
# the time the container sees it, it's too late. Deliberately duplicates
# cld/log.py's mask_secrets() patterns (cross-reference:
# cld/log.py:_SECRET_KV_RE / _SECRET_PATH_RE / _SECRET_URL_RE) -- the broker
# must not depend on `cld` being importable by the host's python3. Applied line
# by line, so no pattern ever matches across a newline.
_MASKS = (
    (re.compile(r"(TOKEN|KEY|SECRET|PASSWORD)=[^\s,'\"]+", re.IGNORECASE), r"\1=<redacted>"),
    (re.compile(r"/run/secrets/[A-Za-z0-9._/-]+"), "/run/secrets/<redacted>"),
    (re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/@\s]*@"), r"\1<redacted>@"),
    (re.compile(r'"(token|key|secret|password)"\s*:\s*"[^"]*"', re.IGNORECASE), r'"\1": "<redacted>"'),
)


def _mask_line(line: str) -> str:
    for pattern, replacement in _MASKS:
        line = pattern.sub(replacement, line)
    return line


def mask_output(data: bytes) -> bytes:
    text = data.decode(errors="surrogateescape")
    return "\n".join(map(_mask_line, text.split("\n"))).encode(errors="surrogateescape")


# Cap output to *max_bytes*. `keep`=head for query (the response starts with
# `data`), `keep`=tail for logs (recent lines matter most). Trailing newlines
# are dropped before measuring. Truncation notice goes to stderr, never mixed
# into the capped stdout.
def cap_output(data: bytes, max_bytes: int, keep: str) -> None:
    data = data.rstrip(b"\n")
    if len(data) > max_bytes:
        print(f"[cld-broker] graphql output truncated to {max_bytes} of {len(data)} bytes "
              "(raise GRAPHQL_OUTPUT_MAX_BYTES for more)", file=sys.stderr)
        data = data[:max_bytes] if keep == "head" else data[-max_bytes:]
    _emit(data, sys.stdout)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: urllib.request.Request, fp: IO[bytes], code: int, msg: str,
                         headers: http.client.HTTPMessage, newurl: str) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def http_post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> tuple[int, bytes] | None:
    """POST the way `curl -sS` did: every HTTP status (a redirect included, which
    is not followed) is a response, URL userinfo becomes basic auth, and only a
    transport failure -- reported on stderr -- returns None. Shared by the
    readiness probe and query execution."""
    parts = urllib.parse.urlsplit(url)
    if parts.username is not None:
        credentials = f"{urllib.parse.unquote(parts.username)}:{urllib.parse.unquote(parts.password or '')}"
        headers = {**headers, "Authorization": f"Basic {base64.b64encode(credentials.encode()).decode()}"}
        url = urllib.parse.urlunsplit(parts._replace(netloc=parts.netloc.rpartition("@")[2]))
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (OSError, ValueError, http.client.HTTPException) as e:
        # The text of these errors can quote the URL, which may carry userinfo from the repo's .env.
        cause = e.reason if isinstance(getattr(e, "reason", None), BaseException) else e
        reason = cause.strerror if isinstance(cause, OSError) and cause.strerror else type(cause).__name__
        print(f"[cld-broker] graphql request failed: {reason}", file=sys.stderr)
        return None


def _alias_key(alias: str) -> str:
    return alias.upper().replace("-", "_")


# Aliases come from the repo's own .env (the same per-repo secrets file
# run-tests already mounts) -- no new secret store, no new broker config:
#   CLD_GRAPHQL_URL_<ALIAS>    = https://dev.example/graphql
#   CLD_GRAPHQL_AUTH_<ALIAS>   = Bearer eyJ...        # full Authorization value
#   CLD_GRAPHQL_COOKIE_<ALIAS> = session=...          # full Cookie value
# Parsed into a local dict over the broker's own settings, so the broker's
# environment is never polluted by whatever else the repo's .env defines.
def lookup_alias(alias: str, secrets: Secrets) -> tuple[str, str, str]:
    values = dict(CONF)
    values.update(secrets.values())
    key = _alias_key(alias)
    return (values.get(f"CLD_GRAPHQL_URL_{key}", ""), values.get(f"CLD_GRAPHQL_AUTH_{key}", ""),
            values.get(f"CLD_GRAPHQL_COOKIE_{key}", ""))


# If the broker will POST to any URL the container names, the container gains
# the host as an egress channel and SSRF pivot -- it could reach an internal
# service it cannot itself route to. It cannot leak a credential that way
# (raw URLs get no auth, see resolve_target), but the channel is new. Gated on
# an explicit hostname allowlist; empty (the default) means no raw URLs at
# all, aliases only. Exact hostname match, no substring/prefix/wildcard.
def check_url_allowlisted(target: str) -> None:
    if target.startswith("http://"):
        host = target.removeprefix("http://")
    elif target.startswith("https://"):
        host = target.removeprefix("https://")
    else:
        _die(f"denied: unsupported URL scheme in '{target}' (http/https only)", 2)
    host = host.split("/", 1)[0]     # strip path
    host = host.split("?", 1)[0]     # strip query (in case no path preceded it)
    host = host.split("#", 1)[0]     # strip fragment
    host = host.rpartition("@")[2]   # strip userinfo (user:pass@) -- must precede the port strip
    host = host.split(":", 1)[0]     # strip port
    if host not in [h for h in _setting("GRAPHQL_URL_ALLOWLIST").split(" ") if h]:
        _die(f"denied: host '{host}' is not in GRAPHQL_URL_ALLOWLIST -- set it in "
             "broker.conf to permit raw-URL graphql targets (empty = none allowed)", 3)


# Resolves <target> (§5.1) into (url, authorization, cookie):
#   local              this session's own server, discovered via `docker port` --
#                      with the alias `local`'s auth/cookie from the repo's .env, if set
#   [A-Za-z0-9_-]+     an alias, looked up in the repo's .env -- auth attached
#   http(s)://…        the URL verbatim, gated on the allowlist -- never any auth
#   anything else      denied
def resolve_target(scope: Scope, target: str) -> tuple[str, str, str]:
    if target == "local":
        config = resolve_graphql_config(scope.repo)
        host_port = _first_line(_out("docker", "port", f"cld_gql_{scope.session}", f"{config.port}/tcp") or "")
        if not host_port:
            _die(f"denied: no local graphql server running for {scope.session} -- start one first", 3)
        _, auth, cookie = lookup_alias("local", resolve_secrets_env_file(scope.repo, stage=False))
        return local_url(host_port, config.health_path), auth, cookie
    if target.startswith(("http://", "https://")):
        check_url_allowlisted(target)
        return target, "", ""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", target):
        _die(f"denied: bad target '{target}'", 2)
    secrets = resolve_secrets_env_file(scope.repo, stage=False)
    url, auth, cookie = lookup_alias(target, secrets)
    if not url:
        _die(f"denied: unknown graphql alias '{target}' -- set CLD_GRAPHQL_URL_{_alias_key(target)} in "
             f"{secrets.source}", 3)
    return url, auth, cookie


# Argv: query <target> <query-string> [variables-json]. The body is built with
# json.dumps (never string concatenation, so a query containing quotes can't
# corrupt it) and never appears in any child process's argv, where it would be
# visible in the host's process table.
def do_graphql_query(scope: Scope, target: str, query: str, variables: str = "") -> None:
    url, auth, cookie = resolve_target(scope, target)

    try:
        body = json.dumps({"query": query, "variables": json.loads(variables) if variables else {}})
    except json.JSONDecodeError as e:
        print(f"bad variables JSON: {e}", file=sys.stderr)
        _die("denied: malformed variables JSON for graphql query", 2)

    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = auth
    if cookie:
        headers["Cookie"] = cookie

    if (response := http_post(url, body.encode(), headers, float(_setting("GRAPHQL_QUERY_TIMEOUT")))) is None:
        # Never the resolved URL: an alias URL from .env can hold credentials.
        raw = urllib.parse.urlsplit(target)
        shown = f"{raw.scheme}://{raw.hostname}" if raw.scheme in ("http", "https") else target
        _die(f"graphql request to '{shown}' failed (connection error)", 3)
    status, data = response

    cap_output(mask_output(data), int(_setting("GRAPHQL_OUTPUT_MAX_BYTES")), "head")
    print()

    if not 200 <= status <= 299:
        _die(f"graphql endpoint returned HTTP {status}", 1)


# Kept broker-side so the container doesn't need to ship the introspection
# document itself.
_GRAPHQL_INTROSPECTION_QUERY = "{__schema{queryType{name} mutationType{name} types{name kind description fields{name description type{name kind ofType{name kind ofType{name kind}}} args{name type{name kind ofType{name kind}}}} inputFields{name type{name kind ofType{name kind ofType{name kind}}}} enumValues{name description}}}}"


# alias names only, one per line, discovered by scanning .env for
# CLD_GRAPHQL_URL_* -- never any value. Printed in the alias's canonical
# lower-case/hyphenated spelling (_alias_key is lossy -- both `-` and `_`
# fold to `_` -- so this is *a* valid spelling that round-trips through
# lookup_alias, not necessarily byte-identical to however the operator
# originally wrote it in .env).
def do_graphql_endpoints(scope: Scope) -> None:
    text = (resolve_secrets_env_file(scope.repo, stage=False).data or b"").decode(errors="surrogateescape")
    for alias in re.findall(r"^CLD_GRAPHQL_URL_([A-Za-z0-9_]+)", text, re.MULTILINE):
        print(alias.replace("_", "-").lower())


# One tab-separated line: state<TAB>port<TAB>endpoint<TAB>revision<TAB>container<TAB>stale.
# state in running|starting|exited|not_started. stale is "true"/"false" when
# there's a serving revision to compare against the session's current tip,
# empty otherwise (not_started, exited, or the tip couldn't be resolved).
# Parsed client-side the same way _parse_container_line (cld/broker.py)
# parses list-containers.
def print_gql_status_line(state: str, port: str, endpoint: str, revision: str, container: str,
                          stale: str) -> None:
    print("\t".join((state, port, endpoint, revision, container, stale)))


def _probe(url: str, timeout: float) -> bool:
    # {__typename} is valid at the root of every GraphQL schema, unlike the old
    # in-process MCP's `query { hello }` probe, which could never succeed
    # against a real schema (docs/impl-graphql-broker-plan.md §4.3).
    response = http_post(url, b'{"query":"{__typename}"}', {"Content-Type": "application/json"}, timeout)
    return response is not None and response[0] == 200 and b'"errors"' not in response[1]


def do_graphql_status(scope: Scope, tip: str | None) -> None:
    sweep_gql_orphans()
    config = resolve_graphql_config(scope.repo)
    cname = f"cld_gql_{scope.session}"
    if _capture(["docker", "inspect", cname]).returncode:
        print_gql_status_line("not_started", "", "", "", "", "")
        return
    if _out_strict("docker", "inspect", cname, "--format", "{{.State.Running}}") != "true":
        print_gql_status_line("exited", "", "", "", cname, "")
        return
    # Read the actually-serving revision back off the container's own env
    # rather than re-resolving the session's *current* tip -- they can differ
    # once the caller has kept editing since this server started.
    env = _out_strict("docker", "inspect", cname, "--format", "{{range .Config.Env}}{{println .}}{{end}}")
    rev = "\n".join(line.removeprefix("REVISION=") for line in env.splitlines()
                    if line.startswith("REVISION="))
    # Compare against the current tip so the caller can tell a running server
    # is serving stale code without a second call. The tip is optional --
    # status has to keep answering on a git-backed repo, or after
    # graphql_command was removed, same as stop does -- so an unresolvable tip
    # (None) leaves stale empty rather than aborting the op.
    stale = str(rev != tip).lower() if tip and rev else ""
    host_port = _first_line(_out_strict("docker", "port", cname, f"{config.port}/tcp"))
    # docker port publishing early (or `poetry install` still running behind
    # it) doesn't mean the server itself answers yet -- a live probe is the
    # only way to tell "running" from "starting" here, same probe do_graphql_start
    # waits on.
    if not host_port or not _probe(local_url(host_port, config.health_path), 3):
        print_gql_status_line("starting", "", "", rev, cname, stale)
        return
    print_gql_status_line("running", host_port.rpartition(":")[2], local_url(host_port, config.health_path),
                          rev, cname, stale)


# `docker rm -f` sends SIGKILL, so the entrypoint's own `trap EXIT` (and the
# fact that it `exec`s into $GQL_COMMAND, which drops the trap even on a
# graceful exit) never gets a chance to forget the jj workspace -- it would
# leak into the user's store. So `stop` forgets it explicitly, unconditionally
# (docs/impl-graphql-broker-plan.md §4.4). Deliberately does NOT call
# resolve_graphql_context / resolve_graphql_config -- teardown must work even
# for a repo whose graphql_command was since unset or removed.
def do_graphql_stop(scope: Scope) -> None:
    cname = f"cld_gql_{scope.session}"
    # Forget the workspace in the repo the server was STARTED for (its own
    # org.cld.gql-repo label), not scope.repo: a multi-repo ticket can pass a
    # different --repo to stop than it did to start. Falls back to scope.repo for
    # a container without the label (or already gone).
    gql_repo = _label(cname, "org.cld.gql-repo")
    _capture(["docker", "stop", "-t", "10", cname])
    _rm_container(cname)
    _forget_workspace(gql_repo or scope.repo, f"gql-{scope.session}")


def _dump_logs(cname: str) -> None:
    logs = _capture(["docker", "logs", "--tail", "50", cname], stderr=subprocess.STDOUT)
    _emit(mask_output(logs.stdout), sys.stderr)


# Port allocation: let Docker do it. No probing for a free port, no deriving
# one from a hash -- publish with an empty host port and read it back via
# `docker port`. Race-free, and no port range to own.
def do_graphql_start(scope: Scope, rev: str, agent: str) -> None:
    sweep_gql_orphans()
    secrets, config = resolve_graphql_context(scope.repo)
    cname, wsname = f"cld_gql_{scope.session}", f"gql-{scope.session}"

    # start is idempotent: a running server already answers, so return its
    # existing endpoint rather than erroring -- it's what makes the MCP's
    # start_server safe to call without checking status first. Idempotence is
    # per repo, though: a multi-repo ticket asking for a different repo than
    # the running server's own org.cld.gql-repo label must be refused, not
    # handed the wrong repo's endpoint. An unlabeled container (pre-label
    # image) keeps the old always-idempotent behavior.
    if not _capture(["docker", "inspect", cname]).returncode:
        if _out_strict("docker", "inspect", cname, "--format", "{{.State.Running}}") == "true":
            if (gql_repo := _label(cname, "org.cld.gql-repo")) and gql_repo != scope.repo:
                _die(f"denied: graphql server for {scope.session} is already running for repo "
                     f"{gql_repo}, not {scope.repo} -- `graphql stop` it first", 3)
            do_graphql_status(scope, rev)
            return
        # Stopped/crashed leftover from a previous start -- clear it before
        # relaunching so `docker run --name` below doesn't collide. Its workspace lives in
        # the repo it was started for, as in do_graphql_stop.
        left_repo = _label(cname, "org.cld.gql-repo")
        _rm_container(cname)
        _forget_workspace(left_repo or scope.repo, wsname)

    bind = resolve_graphql_bind()

    # The store at /repo is read-write where `jj workspace add` writes objects. The
    # server serves from its own workspace and never touches the caller's @ or bookmarks.
    _out_strict(
        "docker", "run", "-d", "--name", cname,
        "--label", f"org.cld.gql-session={scope.session}",
        "--label", f"org.cld.gql-repo={scope.repo}",
        "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
        *store_container_args(scope.repo, _setting("GRAPHQL_IMAGE")), *secrets.mount_args(), *_agent_mount(agent),
        *_venv_cache_args(scope.repo, _setting("GRAPHQL_IMAGE")),
        "-e", f"REVISION={rev}", "-e", f"PROJECT_SUBDIR={secrets.project_subdir}",
        "-e", f"GQL_WORKSPACE={wsname}", "-e", f"GQL_COMMAND={config.command}",
        "-e", f"GQL_PORT={config.port}",
        "-p", f"{bind}:0:{config.port}",
        _setting("GRAPHQL_IMAGE"),
        stderr=None,
    )

    host_port = _first_line(_out_strict("docker", "port", cname, f"{config.port}/tcp", stderr=None))
    if not (host_port := host_port.rpartition(":")[2]):
        print(f"denied: could not read back the published port for {cname}", file=sys.stderr)
        _rm_container(cname)
        sys.exit(3)

    timeout = int(_setting("GRAPHQL_START_TIMEOUT"))
    deadline = time.monotonic() + timeout
    ready = died = False
    while time.monotonic() < deadline:
        # Check the container is still alive before probing it -- otherwise a
        # crash (bad graphql_command, poetry install failure) just busy-loops
        # the probe against a dead port until the full timeout elapses.
        if _out("docker", "inspect", cname, "--format", "{{.State.Running}}") != "true":
            died = True
            break
        if _probe(local_url(f"{bind}:{host_port}", config.health_path), 5):
            ready = True
            break
        time.sleep(2)

    if died:
        print(f"[cld-broker] graphql server for {scope.session} exited before becoming healthy "
              "-- last logs:", file=sys.stderr)
        _dump_logs(cname)
        _rm_container(cname)
        _forget_workspace(scope.repo, wsname)
        sys.exit(3)

    if not ready:
        print(f"[cld-broker] graphql server for {scope.session} did not become healthy within "
              f"{timeout}s -- last logs:", file=sys.stderr)
        _dump_logs(cname)
        do_graphql_stop(scope)
        sys.exit(3)

    do_graphql_status(scope, rev)


# Only start (and restart) needs the caller's revision -- a hard requirement
# there; status takes it softly for the stale field; the other ops never ask.
def action_graphql(caller: Caller, argv: list[str]) -> None:
    name, argv = parse_repo_arg(argv)
    scope = Scope(caller.name, caller.repo(name))
    op, args = (argv[0], argv[1:]) if argv else ("", [])
    match op:
        case "start":
            do_graphql_start(scope, resolve_revision(caller, scope.repo), caller.ssh_agent())
        case "stop":
            do_graphql_stop(scope)
            print("stopped")
        case "restart":
            do_graphql_stop(scope)
            do_graphql_start(scope, resolve_revision(caller, scope.repo), caller.ssh_agent())
        case "status":
            do_graphql_status(scope, caller.revision(scope.repo))
        case "logs":
            tail = args[0] if args and args[0] else "50"
            if not re.fullmatch(r"[0-9]+", tail):
                _die(f"denied: bad tail count '{tail}'", 2)
            if int(tail) > 2000:  # a debugging aid, not a log export
                tail = "2000"
            cname = f"cld_gql_{scope.session}"
            if _capture(["docker", "inspect", cname]).returncode:
                _die(f"no graphql server for {scope.session} -- start one first (a failed start "
                     "already printed its last logs)", 3)
            logs = _capture(["docker", "logs", "--tail", tail, cname], stderr=subprocess.STDOUT)
            cap_output(mask_output(logs.stdout), int(_setting("GRAPHQL_OUTPUT_MAX_BYTES")), "tail")
            sys.exit(logs.returncode)
        case "query":
            if len(args) < 2:
                _die("denied: query needs <target> <query-string> [variables-json]", 2)
            do_graphql_query(scope, *args[:3])
        case "introspect":
            do_graphql_query(scope, args[0] if args and args[0] else "local", _GRAPHQL_INTROSPECTION_QUERY)
        case "endpoints":
            do_graphql_endpoints(scope)
        case _:
            _die(f"denied: bad graphql op '{op}'", 2)


# Template for a second action -- copy, rename, point at its image, enable by
# uncommenting. Callable from the container as `cld broker lint …`.
# def action_lint(caller: Caller, argv: list[str]) -> None:
#     repo = caller.repo("")
#     rev, secrets = resolve_test_context(caller, repo)
#     _exec(["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
#            *store_container_args(repo, _setting("LINT_IMAGE") or "runlint:latest"), "-e", f"REVISION={rev}",
#            "-e", f"PROJECT_SUBDIR={secrets.project_subdir}",
#            _setting("LINT_IMAGE") or "runlint:latest", *argv])


# ---- dispatch -------------------------------------------------------------

def decode_argv(payload: str) -> list[str]:
    """Arbitrary argv, decoded from base64(NUL-joined argv). Never eval'd, so it
    can only ever become arguments to the action's command."""
    try:
        data = base64.b64decode(payload)
    except ValueError:
        _die("denied: bad argv payload", 2)
    args = data.split(b"\0")
    if args[-1] == b"":
        args.pop()
    return [os.fsdecode(arg) for arg in args]


_SESSION_RE = re.compile(r"cld_[A-Za-z0-9_-]+")


# Resolve the action to an action_* function. The charset check keeps the
# constructed name to [a-z0-9_], and the function must actually exist.
def resolve_action(action: str) -> Callable[[Caller, list[str]], None]:
    if not re.fullmatch(r"[a-z][a-z0-9-]*", action):
        _die("denied: bad action", 2)
    if not callable(fn := globals().get(f"action_{action.replace('-', '_')}")):
        _die(f"denied: unknown action '{action}'", 2)
    return fn


def main_ssh() -> None:
    command = _first_line(os.environ.get("SSH_ORIGINAL_COMMAND", "")).strip(" \t")
    action, session, payload = (re.split(r"[ \t]+", command, maxsplit=2) + ["", ""])[:3]
    fn = resolve_action(action)
    if not _SESSION_RE.fullmatch(session):
        _die("denied: bad session id", 2)

    # The sshd route's caller source: the calling container's host-set labels.
    # The container name == session, and the labels are set at launch (trusted),
    # so the caller cannot point at an arbitrary host path -- only a repo the host
    # mounted for it. Everything past the labels (repo selection, revision)
    # is resolved lazily inside each action_* function, so read-only
    # actions don't pay for -- or fail on -- resolution they don't need.
    fn(LabelCaller.from_session(session), decode_argv(payload))


def _repo_root(path: str) -> str:
    start = Path(path).resolve()
    if not (root := next((str(p) for p in (start, *start.parents) if _is_repo(str(p))), "")):
        _die(f"no repo at or above {start} -- pass -C <repo>", 3)
    return root


def main_cli(argv: list[str]) -> None:
    """The host route (`cld-brokerctl run`): the Caller comes from the command
    line instead of docker labels. Every token after the action goes to it as
    given, so its own flags (pytest's -k, -r, ...) never clash with these."""
    if argv == ["graphql-sweep"]:  # needs no repo, session or revision
        sweep_gql_orphans(announce=True)
        return
    parser = argparse.ArgumentParser(prog="cld-brokerctl run",
                                     description="Run one broker action as the host operator.")
    parser.add_argument("-C", dest="repo", default=".", help="repo to act on (default: the one containing cwd)")
    parser.add_argument("-r", dest="revset", default="@", help="jj revset to act on (default: @, your working copy)")
    parser.add_argument("-s", dest="session", help="session name for the caller's own resources "
                                                   "(default: cld_host_<repo dir>)")
    parser.add_argument("action")
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    fn = resolve_action(args.action)
    repo = _repo_root(args.repo)
    session = args.session or "cld_host_" + re.sub(r"[^A-Za-z0-9_-]", "-", Path(repo).name)
    if not _SESSION_RE.fullmatch(session):
        _die(f"denied: bad session id '{session}'", 2)
    fn(CliCaller(session, repo, args.revset), args.argv)


# sshd's ForceCommand runs this file with no arguments and delivers the client's
# request in $SSH_ORIGINAL_COMMAND; a client cannot add arguments. So arguments
# can only come from a host shell, which makes them a safe route selector.
def main() -> None:
    load_conf()
    if len(sys.argv) > 1:
        main_cli(sys.argv[1:])
    else:
        main_ssh()


if __name__ == "__main__":
    main()
