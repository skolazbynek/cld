"""Container setup: arg building, image management, path translation."""

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

from cld.config import Config, _load_toml
from cld.manifest import TicketManifest, manifest_labels, read_manifest
from cld.log import get_logger, log_subprocess, mask_secrets
from cld.vcs import get_backend
from cld.vcs.jj import COMMONDIR_PLACEHOLDER, check_git_dir

log = get_logger(__name__)

# Static structural constants (Dockerfile- and shell-script-coupled, not user-tunable).
CONTAINER_USER = "claude"
CONTAINER_HOME = f"/home/{CONTAINER_USER}"
WORKSPACE_BASE = "/workspace"
MAILBOX_MOUNT = "/var/cld/mailboxes"

# All RO $HOME mounts are staged under /tmp/host-config/<rel> and copied into
# $HOME by the entrypoint (see copy_host_configs in container-init.sh).
# Allowlist only -- avoid leaking gh/aws/gcloud/etc creds.
_RO_HOME_MOUNT_ROOT = "/tmp/host-config"

# Broker key + pinned known_hosts land here (RO). See stage_broker.
_BROKER_KEY_MOUNT = "/run/secrets/broker-key"
_BROKER_KNOWN_HOSTS_MOUNT = "/run/secrets/broker-known-hosts"

# Image label: the boot scripts run jj in origin with --ignore-working-copy.
BOOT_API_LABEL = "org.cld.boot-api"
BOOT_API_VERSION = "2"
# Container label: the container mounts the protected store rows and no checkout.
ORIGIN_MOUNT_LABEL = "org.cld.origin-mount"
ORIGIN_MOUNT_VALUE = "store-v1"


def find_repo_root(start: Path | None = None) -> Path:
    """Locate the VCS repository root (jj or git) by walking up from *start*.

    Delegates to the VCS auto-detection layer. Exits on failure.
    """
    try:
        backend = get_backend(start)
    except RuntimeError as e:
        log.error(str(e))
        sys.exit(1)
    return backend.repo_root


def find_repo_context(start: Path | None = None) -> tuple[Path, str]:
    """Return (repo_root, workspace_revision_hint).

    workspace_revision_hint is non-empty when invoked from a secondary jj workspace
    or git worktree, set to the appropriate revision so the container starts from
    the caller's current working copy rather than the main workspace default.
    """
    try:
        backend = get_backend(start)
    except RuntimeError as e:
        log.error(str(e))
        sys.exit(1)
    log.info("Repository: %s (VCS: %s)", backend.repo_root, backend.name)
    return backend.repo_root, backend.workspace_revision


def build_session_name(prefix: str, suffix: str = "") -> str:
    """Generate a session name like ``prefix_suffix`` or ``prefix_<random>``."""
    return f"{prefix}_{suffix or secrets.token_hex(3)}"


def require_docker() -> None:
    """Verify the ``docker`` CLI is available, exit otherwise."""
    if not shutil.which("docker"):
        log.error("Docker is not installed.")
        sys.exit(1)


CONTENT_HASH_LABEL = "org.cld.content-hash"

_HASH_IGNORE_PARTS = {"__pycache__", ".git", ".jj", ".venv", "node_modules"}


def _hash_ignored(p: Path) -> bool:
    return any(part in _HASH_IGNORE_PARTS or part.endswith(".pyc") for part in p.parts)


def _hash_walk(p: Path) -> Iterable[Path]:
    if p.is_file():
        yield p
        return
    for entry in sorted(p.rglob("*")):
        if entry.is_file() and not _hash_ignored(entry):
            yield entry


def _content_hash(paths: list[Path], parent_hash: str | None) -> str:
    """Deterministic content hash over the given files/dirs and an optional parent hash."""
    h = hashlib.sha256()
    if parent_hash:
        h.update(b"parent:" + parent_hash.encode() + b"\n")
    for p in sorted(paths):
        # Use relpath under p.parent so the path component is stable across machines.
        for entry in _hash_walk(p):
            rel = entry.relative_to(p.parent).as_posix()
            h.update(f"{rel}\0".encode())
            h.update(entry.read_bytes())
            h.update(b"\0")
    hexdigest_short = h.hexdigest()[:16]
    log.debug(
        "computed content hash %s (parent=%s)",
        hexdigest_short,
        parent_hash[:8] if parent_hash else "<root>",
    )
    return hexdigest_short


def _image_label(image: str, label: str) -> str:
    """Read a Docker label off an image. Empty string if image or label is missing."""
    cmd = ["docker", "inspect", "--format", f'{{{{ index .Config.Labels "{label}" }}}}', image]
    result = subprocess.run(cmd, capture_output=True, text=True)
    log_subprocess(log, cmd, result)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def base_extra_paths(cld_root: Path) -> list[Path]:
    return [
        cld_root / "imgs/claude-devcontainer/container-init.sh",
        cld_root / "imgs/claude-devcontainer/vcs-lib.sh",
        cld_root / "cld",
        cld_root / "prompts",
        cld_root / ".claude/skills",
    ]


def devcontainer_extra_paths(cld_root: Path) -> list[Path]:
    return [cld_root / "imgs/claude-devcontainer/entrypoint-claude-devcontainer.sh"]


def run_extra_paths(cld_root: Path) -> list[Path]:
    return [
        cld_root / "imgs/claude-run/entrypoint-claude-run.sh",
        cld_root / "imgs/claude-run/run-system-prompt.md",
    ]


def ensure_image(
    image: str,
    dockerfile: Path,
    context: Path,
    *,
    extra_paths: list[Path] | None = None,
    parent_image: tuple[str, Path, Path, list[Path]] | None = None,
    force: bool = False,
    no_cache: bool = False,
    quiet: bool = False,
) -> str:
    """Build a Docker image if it's missing or its baked content has drifted from source.

    Stamps every build with a `CONTENT_HASH_LABEL` Docker label whose value hashes the
    Dockerfile + every path in `extra_paths` (recursively, sorted, ignoring caches/VCS).
    Rebuilds when the existing image's label doesn't match the recomputed hash.

    Pass parent_image=(name, dockerfile, context, extra_paths) to ensure a base image
    is built first; the parent's hash is folded into this image's hash so a base
    rebuild propagates.
    Pass force=True to always build. Pass no_cache=True to build with --no-cache.
    Pass quiet=True to capture docker build output (logged at INFO line-by-line)
    instead of streaming to the inherited stdout. Required when running under
    MCP stdio servers where stdout = JSON-RPC and must stay clean.
    Returns the content hash of the (now-current) image.
    """
    if in_master_container():
        # No docker daemon inside master (socket removed). Image building is the
        # host's job, and container launches from inside master are delegated to
        # the host broker (which runs host-side `cld`, ensuring images there), so
        # this should never be reached. Fail clearly if it somehow is.
        raise RuntimeError(
            f"image '{image}' cannot be ensured from inside a master container "
            "(no docker daemon). Container launches are delegated to the host "
            "broker; build images on the host with `cld build`."
        )

    parent_hash: str | None = None
    if parent_image:
        parent_name, parent_dockerfile, parent_context, parent_extras = parent_image
        parent_hash = ensure_image(
            parent_name, parent_dockerfile, parent_context,
            extra_paths=parent_extras, force=force, no_cache=no_cache, quiet=quiet,
        )

    expected = _content_hash([dockerfile] + (extra_paths or []), parent_hash)

    exists = bool(subprocess.run(
        ["docker", "images", "-q", image], capture_output=True, text=True,
    ).stdout.strip())
    existing = _image_label(image, CONTENT_HASH_LABEL) if exists else ""

    if exists and not force and existing == expected:
        return expected

    log.debug(
        "Hash check: existing=%s expected=%s",
        existing[:8] if existing else "<missing>",
        expected[:8],
    )
    if force:
        log.info(f"Rebuilding '{image}' (forced, hash {expected[:8]})...")
    elif not exists:
        log.info(f"Image '{image}' not found. Building (hash {expected[:8]}, may take 5+ minutes)...")
    elif not existing:
        log.info(f"Rebuilding '{image}' (no content-hash label; hash {expected[:8]})...")
    else:
        log.info(f"Rebuilding '{image}' (stale: {existing[:8]} -> {expected[:8]})...")

    cmd = ["docker", "build", "-f", str(dockerfile), "-t", image,
           "--label", f"{CONTENT_HASH_LABEL}={expected}"]
    if no_cache:
        cmd.append("--no-cache")
    cmd.append(str(context))
    log.info("Running docker build for %s (this may take several minutes)", image)
    if quiet:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            log.info("docker build: %s", line.rstrip())
        rc = proc.wait()
        if rc != 0:
            raise subprocess.CalledProcessError(rc, cmd)
    else:
        subprocess.run(cmd, check=True)
    log.info("docker build for %s succeeded", image)
    return expected


def in_master_container() -> bool:
    """True when ``HUB_MODE`` is set in this process's environment.

    The flag used to mark a `cld master` "hub" container (broker dispatch,
    mailbox, sibling-target resolution). The master role is gone and nothing
    launches a container with ``HUB_MODE`` any more, so this is False
    everywhere in practice; the remaining callers (the broker client's
    host-vs-broker dispatch above all) keep their branch pending that
    dispatch's own rework, rather than being silently rewired here.
    """
    return bool(os.environ.get("HUB_MODE"))


def find_target_repo(cfg: Config) -> Path:
    """Return the host path of the repo to target for a peer launch.

    Delegates to `find_repo_context()` (cwd-walk for .jj/.git); *cfg* is kept
    in the signature because callers pass it and the ticket-aware resolver
    (design-ticket-containers.md section 6.5) will need it.
    """
    return find_repo_context()[0]


def anchor_env_args(
    cfg: Config, revision: str, brief: str = "", mode: str = "isolated",
) -> list[str]:
    """Return the docker `-e` args carrying anchor info to a peer container.

    The host resolves the base revision to a change id from its jj view; the
    peer entrypoint uses it as the base for ``jj workspace add`` and then creates
    the scratch commit B inside that workspace via ``stage_from_env``. See
    docs/design-anchor-change.md. Only ever called host-side: a container has
    no docker daemon, so it never builds a peer's args itself (see
    cld/broker.py).

    ``mode``: ``"isolated"`` (default) -- the entrypoint exports B itself as
    ``AGENT_ANCHOR_HASH``, so only B's own descendants are editable. ``"shared"``
    -- the entrypoint exports A (B's parent) instead, so any pre-existing
    descendant of A is editable too (docs/design-ticket-containers.md
    section 3). *revision*
    is expected to already be resolved to a change id by the caller (via
    ``resolve_anchor_checked``) when the overlap check matters; passing a
    symbolic revision still works (``resolve_anchor`` is idempotent on a change id).
    """
    from cld.vcs import get_backend
    from cld.vcs.anchor import resolve_anchor
    from cld.vcs.scratch import encode_scratch_envelope

    # The composed brief rides in the envelope, so it lands in anchor commit B as
    # .cld-run/brief.md: one channel instead of the old /config/{persona,task}.md mounts
    # plus AGENT_INLINE_PROMPT, and readable by the agent mid-task. See
    # docs/design-prompt-chaining.md §3. Without a brief, B is empty.
    scratch = {"brief.md": brief.encode()} if brief else {}
    payload = encode_scratch_envelope(scratch)

    hint = resolve_anchor(get_backend(), revision)
    log.info("Anchor base: %s (mode=%s)", hint[:12], mode)

    return [
        "-e", f"AGENT_REVISION_HINT={hint}",
        "-e", f"AGENT_SCRATCH={payload}",
        "-e", f"AGENT_ANCHOR_MODE={mode}",
    ]


def to_host_path(path: str, cfg: Config) -> str:
    """Translate a container-internal path to the corresponding host path.

    Consults ``cfg.path_map`` first (the ``CLD_PATH_MAP`` prefix map ticket
    containers get, longest prefix wins -- design-ticket-containers.md
    section 6.4), then falls back to ``cfg.host_project_dir`` /
    ``cfg.host_home`` (the scalar ``CLD_HOST_PROJECT_DIR`` /
    ``CLD_HOST_HOME`` pair v1 kinds keep setting) to map ``/workspace/*``
    and ``$HOME`` paths back to their host-side locations. No-op on the host.
    """
    for prefix in sorted(cfg.path_map, key=len, reverse=True):
        if path == prefix or path.startswith(prefix + "/"):
            return cfg.path_map[prefix] + path[len(prefix):]
    if cfg.host_project_dir:
        # /workspace/current is container-ephemeral (no host equivalent under
        # the new layout); only /workspace/origin maps back to the host repo.
        prefix = "/workspace/origin"
        if path.startswith(prefix):
            path = cfg.host_project_dir + path[len(prefix):]
    if cfg.host_home and path.startswith(CONTAINER_HOME):
        path = cfg.host_home + path[len(CONTAINER_HOME):]
    return path


@dataclass(frozen=True)
class TaskAgentSpec:
    """Immutable spawn facts for one task-scoped agent (docs/design-task-agents.md).

    ``parent_master`` is empty when a human launched the agent directly on the
    host: it is the label a master's fleet operations key on, so attributing an
    unrequested agent to a master would hand it authority to reap one it never
    spawned. ``peers`` maps each allowed peer's full container name to that
    edge's hop budget (D28).
    """

    slug: str
    parent_master: str = ""
    deliverable_branch: str = ""
    peers: dict[str, int] = field(default_factory=dict)

    def peers_env(self) -> str:
        """Encode ``peers`` for the container: ``name:hops`` pairs, comma-separated.

        Comma rather than the repo's usual colon-separated list convention,
        because ``:`` is the name/budget delimiter inside each pair.
        """
        return ",".join(f"{name}:{hops}" for name, hops in sorted(self.peers.items()))


def parse_peers_env(value: str) -> dict[str, int]:
    """Inverse of ``TaskAgentSpec.peers_env`` -- read by the supervisor in the container."""
    peers: dict[str, int] = {}
    for segment in value.split(","):
        segment = segment.strip()
        if not segment:
            continue
        name, _, hops = segment.partition(":")
        if not name or not hops.isdigit():
            raise ValueError(f"malformed peer spec {segment!r}: expected '<name>:<hops>'")
        peers[name] = int(hops)
    return peers


@dataclass(frozen=True)
class StoreLayout:
    """Which VCS store a host repo has. ``git_dir`` is relative to the repo."""

    jj: bool
    git_dir: str


_JJ_MIN_VERSION = (0, 38)

# (id file, legacy config file, jj flag that creates the id), relative to the repo.
_JJ_IDS = (
    (".jj/repo/config-id", ".jj/repo/config.toml", "--repo"),
    (".jj/workspace-config-id", ".jj/workspace-config.toml", "--workspace"),
)


def _refuse_store(name: str, reason: str) -> NoReturn:
    sys.exit(f"cld: repo '{name}': {reason} -- cannot mount its store safely")


def _kind(path: Path) -> str | None:
    """``dir``, ``file``, ``symlink`` or ``other`` by ``lstat``; None when missing."""
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "dir"
    return "file" if stat.S_ISREG(mode) else "other"


def _require_kind(name: str, repo: Path, rel: str, expected: str) -> None:
    kind = _kind(repo / rel)
    if kind != expected:
        found = "missing" if kind is None else f"a {kind}"
        _refuse_store(name, f"{rel} is {found}, expected a {expected}")


def _store_rows(layout: StoreLayout) -> list[tuple[str, str, bool]]:
    """The eleven mount rows of the design (section 3.1) as ``(path, kind, readonly)``.

    The path is the same under the host repo and under the container root. The
    read-write rows make the parent of each read-only path a mount point, so
    the agent cannot rename the parent and plant a copy. The ``commondir`` row
    is a placeholder that keeps the agent from creating that file: git and jj
    take their config and hooks from the folder it names.
    """
    git = layout.git_dir
    rows = [
        (".jj", "dir", False),
        (".jj/working_copy", "dir", True),
        (".jj/repo", "dir", False),
        (".jj/repo/config-id", "file", True),
        (".jj/repo/store", "dir", False),
        (".jj/repo/store/git_target", "file", True),
    ] if layout.jj else []
    rows += [
        (git, "dir", False), (f"{git}/config", "file", True), (f"{git}/hooks", "dir", True),
        (f"{git}/commondir", "file", True),
    ]
    if layout.jj:
        rows.append((".jj/workspace-config-id", "file", True))
    return rows


def bind_mount_args(src: str, dst: str, *, readonly: bool) -> list[str]:
    """One ``--mount`` argument pair.

    ``--mount`` instead of ``-v``: a missing source fails the launch and is
    not created on the host. Docker parses the value as CSV, so a comma or a
    quote in a path would add or end a field: both are refused with a
    ``ValueError``.
    """
    if bad := next((value for value in (src, dst) if "," in value or '"' in value), None):
        raise ValueError(f"'{bad}' contains a comma or a quote, which would split the --mount value")
    return ["--mount", f"type=bind,src={src},dst={dst}" + (",readonly" if readonly else "")]


def store_mount_args(layout: StoreLayout, host_repo: str, root: str) -> list[str]:
    """``--mount`` arguments for the store rows, host repo at *host_repo*, container side at *root*."""
    args: list[str] = []
    for rel, _, readonly in _store_rows(layout):
        args += bind_mount_args(f"{host_repo}/{rel}", f"{root}/{rel}", readonly=readonly)
    return args


def _check_jj_version(name: str) -> None:
    cmd = ["jj", "--version"]
    try:
        # cwd away from any repo: even this call must not load a repo config
        result = subprocess.run(cmd, capture_output=True, text=True, cwd="/")
    except FileNotFoundError:
        _refuse_store(name, "jj is not installed on the host")
    log_subprocess(log, cmd, result)
    version = re.search(r"(\d+)\.(\d+)", result.stdout)
    if not version or (int(version[1]), int(version[2])) < _JJ_MIN_VERSION:
        _refuse_store(
            name,
            f"host jj '{result.stdout.strip()}' is older than "
            f"{'.'.join(map(str, _JJ_MIN_VERSION))}",
        )


def _git_target_layout(name: str, repo: Path) -> StoreLayout:
    """Check that ``git_target`` names the repo's own git dir, and return the layout."""
    store = repo / ".jj/repo/store"
    target = os.path.normpath(os.path.join(store, (store / "git_target").read_text().strip()))
    bases = {os.path.normpath(repo), os.path.realpath(repo)}
    for git_dir in (".git", ".jj/repo/store/git"):
        if target in {os.path.normpath(os.path.join(base, git_dir)) for base in bases}:
            return StoreLayout(jj=True, git_dir=git_dir)
    _refuse_store(name, f"git_target points to {target}, outside this repo's git dirs")


def _create_jj_id(name: str, repo: Path, rel: str, flag: str) -> None:
    cmd = ["jj", "-R", str(repo), "--ignore-working-copy", "config", "path", flag]
    result = subprocess.run(cmd, capture_output=True, text=True)
    log_subprocess(log, cmd, result)
    if _kind(repo / rel) is None:
        _refuse_store(name, f"jj did not create {rel}: {(result.stderr or '').strip()}")


def _check_jj(name: str, repo: Path) -> StoreLayout:
    """The jj checks that must pass before the first jj call; returns the layout."""
    if _kind(repo / ".jj/repo") == "file":
        _refuse_store(name, "secondary jj workspace (.jj/repo is a file)")
    _check_jj_version(name)
    _require_kind(name, repo, ".jj/repo", "dir")
    _require_kind(name, repo, ".jj/repo/store", "dir")
    _require_kind(name, repo, ".jj/repo/store/git_target", "file")
    # Any jj call migrates a legacy config file next to a missing id into the
    # host jj config, so every refusal comes before the first jj call.
    for rel, legacy, _flag in _JJ_IDS:
        if _kind(repo / rel) is None and _kind(repo / legacy) is not None:
            _refuse_store(
                name, f"unmigrated jj config {legacy}: check it, then run jj once on the host",
            )
    return _git_target_layout(name, repo)


def _check_rows(name: str, repo: Path, layout: StoreLayout, creatable: Iterable[str] = ()) -> None:
    """``lstat`` each mount source: refuse a symlink or the wrong kind. A
    *creatable* source that is missing is left for the caller to create."""
    for rel, kind, _readonly in _store_rows(layout):
        if rel not in creatable or _kind(repo / rel) is not None:
            _require_kind(name, repo, rel, kind)


def prepare_store_mounts(host_repo: Path, name: str = "") -> StoreLayout:
    """Host preflight for the store mounts (design section 3.2).

    Finds the layout, checks the jj version and the settings the read-only
    rows protect, and creates what may be created: the two jj ids, an empty
    ``hooks`` folder and the ``commondir`` placeholder. Exits with a refusal
    rather than mount an unsafe store.
    """
    name = name or host_repo.name
    if _kind(host_repo / ".jj") is not None:
        _require_kind(name, host_repo, ".jj", "dir")
        layout = _check_jj(name, host_repo)
    elif _kind(host_repo / ".git") == "file":
        _refuse_store(name, "git worktree (.git is a file)")
    elif _kind(host_repo / ".git") is None:
        _refuse_store(name, "neither a .jj nor a .git directory")
    else:
        layout = StoreLayout(jj=False, git_dir=".git")
    # Before the first jj call: that call would load a planted commondir.
    if reason := check_git_dir(host_repo, layout.git_dir):
        _refuse_store(name, reason)
    hooks = f"{layout.git_dir}/hooks"
    commondir = f"{layout.git_dir}/commondir"
    ids = [rel for rel, _legacy, _flag in _JJ_IDS] if layout.jj else []
    _check_rows(name, host_repo, layout, [hooks, commondir, *ids])
    for rel, _legacy, flag in _JJ_IDS if layout.jj else ():
        if _kind(host_repo / rel) is None:
            _create_jj_id(name, host_repo, rel, flag)
    if _kind(host_repo / hooks) is None:
        (host_repo / hooks).mkdir()
        (host_repo / hooks).chmod(0o755)
    if _kind(host_repo / commondir) is None:
        (host_repo / commondir).write_text(COMMONDIR_PLACEHOLDER)
    _check_rows(name, host_repo, layout)
    return layout


def require_boot_api(*images: str) -> None:
    """Exit unless every image carries the boot API label: an older entrypoint
    or boot script runs plain jj in origin and fails on the read-only working
    copy. The base image counts too: the derived images bake in its scripts."""
    for image in images:
        if _image_label(image, BOOT_API_LABEL) != BOOT_API_VERSION:
            sys.exit(
                f"cld: image '{image}' lacks the label {BOOT_API_LABEL}={BOOT_API_VERSION} "
                "-- rebuild the cld images with `cld build`"
            )


def container_has_store_mounts(container: str) -> bool:
    """Whether *container* was launched with the store mounts and no checkout (``docker inspect`` takes either kind)."""
    return _image_label(container, ORIGIN_MOUNT_LABEL) == ORIGIN_MOUNT_VALUE


def refuse_unprotected_containers(name: str, host_repo: str) -> None:
    """Exit while any container mounts *host_repo* without the current store mounts label.

    Such a container has the read-write checkout, so it can replace a protected
    source between the preflight and ``docker run`` (or any later ``docker start``).
    A failed listing counts as a hit: nothing says no old container is there.
    """
    cmd = ["docker", "ps", "-a", "--filter", f"volume={host_repo}",
           "--format", f'{{{{.Names}}}}\t{{{{.Label "{ORIGIN_MOUNT_LABEL}"}}}}']
    result = subprocess.run(cmd, capture_output=True, text=True)
    log_subprocess(log, cmd, result)
    if result.returncode != 0:
        _refuse_store(name, f"docker ps failed, so old containers mounting {host_repo} cannot be ruled out")
    old = [
        container for container, _, label in
        (line.partition("\t") for line in result.stdout.strip().splitlines())
        if container and label != ORIGIN_MOUNT_VALUE
    ]
    if old:
        _refuse_store(
            name,
            f"container(s) {', '.join(old)} mount the full checkout without the protected store "
            "mounts: restart each one (`cld restart <ticket>` for a ticket) or stop and remove it",
        )


def _repo_store_args(name: str, repo: Path, host_repo: str, root: str) -> list[str]:
    """Preflight one repo and return its store mounts; refuse while an unprotected container uses it."""
    try:
        # The row names are fixed, so these two cover every mount value; refuse
        # before the preflight makes its first jj call.
        bind_mount_args(host_repo, root, readonly=False)
    except ValueError as e:
        _refuse_store(name, str(e))
    # Before the preflight: it creates files and runs jj, which an old container can race.
    refuse_unprotected_containers(name, host_repo)
    layout = prepare_store_mounts(repo, name)
    return store_mount_args(layout, host_repo, root)


def build_container_args(
    repo_root: Path,
    session_name: str,
    cfg: Config,
    *,
    task_agent: TaskAgentSpec | None = None,
    anchor_hash: str = "",
    anchor_mode: str = "isolated",
) -> list[str]:
    """Build the base ``docker run`` argument list every launcher needs.

    Sets up security constraints, volume mounts (repo store, claude config),
    and environment variables. No docker socket is mounted (see the
    note in the body). Devcontainer-only
    mounts (gitconfig, bashrc, nvim) are added by the launcher in cli.py.

    ``task_agent`` selects the one persistent v1 role left (the task-scoped
    agent): it adds the ``org.cld.kind`` label set and mounts the shared
    mailbox tree. Without it the container is a one-shot `cld run`: ``--rm``,
    anonymous, no broker or mailbox. Ticket containers (v2) have their own
    builder, ``build_ticket_container_args``.

    ``anchor_hash``/``anchor_mode``, when given, are stamped as host-set
    ``org.cld.anchor``/``org.cld.anchor-mode`` labels on every role including
    the plain ``cld run`` one-shot agent -- see ``resolve_anchor_checked`` and
    docs/design-ticket-containers.md section 3. Immutable for the container's
    lifetime, so
    they're the source of truth the overlap check reads back via
    ``docker_occupant_list``.
    """
    home = os.path.expanduser("~")
    host_home = to_host_path(home, cfg)
    host_repo_root = to_host_path(str(repo_root), cfg)

    args: list[str] = []

    if task_agent:
        # AGENT_MODE is the entrypoint's headless-supervisor branch and
        # TASK_AGENT_MODE modifies it (same mailbox precondition, readiness
        # sentinel and supervisor exec); the task-scoped role is the only
        # producer of either since the standing repo agent was removed. Labels
        # are host-set, so the cap and the own-fleet check can trust them; the
        # env vars are what the in-container supervisor turns into meta.json.
        args += [
            "--name", session_name,
            "--label", "org.cld.kind=task-agent",
            "--label", f"org.cld.repo-root={host_repo_root}",
            "--label", f"org.cld.session={session_name}",
            "--label", f"org.cld.task={task_agent.slug}",
            "--label", f"org.cld.parent-master={task_agent.parent_master}",
            "-e", "AGENT_MODE=1",
            "-e", "TASK_AGENT_MODE=1",
            "-e", f"AGENT_TASK_SLUG={task_agent.slug}",
            "-e", f"AGENT_PARENT_MASTER={task_agent.parent_master}",
            "-e", f"AGENT_DELIVERABLE_BRANCH={task_agent.deliverable_branch}",
            "-e", f"AGENT_PEERS={task_agent.peers_env()}",
            # In-container Config.from_env() sees no host user TOML, so the
            # operator's configured budgets have to be passed in.
            "-e", f"CLD_PEER_ABSOLUTE_LIMIT={cfg.peer_absolute_limit}",
            "-e", f"CLD_ROOT_ASK_LIMIT={cfg.root_ask_limit}",
            "-e", f"CLD_AGENT_MAX_TURNS={cfg.agent_max_turns}",
        ]
    else:
        args += ["--rm"]
        if anchor_hash:
            # `cld run`: no other org.cld.* labels today (its `--name` is set by
            # the caller in cld/run.py), but it still needs repo-root + anchor so
            # docker_occupant_list can see it in the overlap check.
            args += [
                "--label", "org.cld.kind=run",
                "--label", f"org.cld.repo-root={host_repo_root}",
                "--label", f"org.cld.session={session_name}",
            ]

    if anchor_hash:
        args += [
            "--label", f"{ANCHOR_LABEL}={anchor_hash}",
            "--label", f"{ANCHOR_MODE_LABEL}={anchor_mode}",
        ]

    # Security and resources
    args += [
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--cpus=2.0",
        "--memory=4g",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-e", f"HOME={CONTAINER_HOME}",
    ]

    # Workspace (required). Origin holds only the store rows, never the checkout.
    require_boot_api(cfg.base_image, cfg.devcontainer_image if task_agent else cfg.run_image)
    args += [
        "-w", f"{WORKSPACE_BASE}/current",
        "--label", f"{ORIGIN_MOUNT_LABEL}={ORIGIN_MOUNT_VALUE}",
    ]
    args += _repo_store_args(
        repo_root.name, repo_root, host_repo_root, f"{WORKSPACE_BASE}/origin",
    )

    args += stage_ssl_certs(cfg)

    # Claude session state (required)
    # rw needed for OAuth token refresh and session state writes; tradeoff: agent can both
    # read OAuth tokens and overwrite session state. Consider ro + tmpfs overlay in the future.
    local_claude_dir = Path(home) / ".claude"
    if not local_claude_dir.is_dir():
        log.error(f"{local_claude_dir} not found -- Claude auth and session state unavailable")
        sys.exit(1)
    args += ["-v", f"{host_home}/.claude:{CONTAINER_HOME}/.claude:rw"]
    args += stage_plugin_marketplaces(cfg, [repo_root])

    # RO $HOME mounts: all staged under /tmp/host-config/<rel>, then copied
    # into $HOME by the entrypoint. Devcontainer-only entries are added by cli.py.
    for rel in cfg.home_mounts_always:
        log.debug("home_mounts_always: attempting ~/%s", rel)
        mnt = stage_home_ro(rel, cfg, [repo_root])
        if mnt:
            args += mnt
        else:
            log.warning(f"~/{rel} not found -- skipping")

    # Session
    args += ["-e", f"SESSION_NAME={session_name}"]
    log.info(f"Session name: {session_name}")

    # Host-path plumbing: lets in-container code translate /workspace/* and
    # $HOME back to host paths (path translation, sibling-target resolution).
    # Always set -- these used to ride along with the docker socket mount, which
    # has since been removed (the host channel is now the broker; see
    # cld/broker.py and docs/design-cld-broker.md).
    args += [
        "-e", f"CLD_HOST_PROJECT_DIR={host_repo_root}",
        "-e", f"CLD_HOST_HOME={host_home}",
    ]

    warn_skipped_workspace_files(cfg.ignore_gitignore)

    # No docker socket is mounted into any container (it was equivalent to host
    # root). In-container docker needs -- peer enumeration above all -- go
    # through the host broker over SSH (see cld/broker.py,
    # broker/cld-broker.py). The broker key is mounted for the persistent role
    # (task-agent) by stage_broker below.

    # Host test broker (persistent role): mount the restricted key +
    # known_hosts and make the broker reachable.
    # No-op unless broker_key is set. Task-agents get this so they can run
    # `cld broker run-tests`, but their persona instructs them to only invoke
    # it with explicit per-run authorization from their master -- see
    # prompts/personas/task-agent.md.
    #
    # Mailbox tree -- shared RW mount so every task-agent and ticket container
    # sees the same mailbox filesystem.
    if task_agent:
        args += stage_broker(cfg)
        args += stage_otel(cfg, session_name)
        args += stage_mailbox(cfg)

    log.debug("Container args: %s", mask_secrets(repr(args)))
    return args


def warn_skipped_workspace_files(files: Iterable[str]) -> None:
    """Warn once per ``ignore_gitignore`` entry: no launcher passes files while the
    host checkout is not mounted (design section 4.1)."""
    for file in files:
        log.warning(
            "ignore_gitignore: '%s' skipped: the host checkout is no longer mounted", file,
        )


def warn_skipped_repo_files(manifest: TicketManifest) -> None:
    """``warn_skipped_workspace_files`` for each repo's own ``.cld/config.toml``."""
    for repo in manifest.repos:
        repo_config = Path(repo.path) / ".cld" / "config.toml"
        if repo_config.is_file():
            warn_skipped_workspace_files(_load_toml(repo_config).get("ignore_gitignore", ()))


def _bootstrap_subdir_ok(subdir: object) -> bool:
    """``pyproject_dir`` comes from the repo and lands in ``CLD_REPO_BOOTSTRAP``, split on ``;`` and ``=``."""
    return (
        isinstance(subdir, str) and not Path(subdir).is_absolute()
        and ".." not in Path(subdir).parts and not {";", "="} & set(subdir)
    )


def ticket_repo_bootstrap(manifest: TicketManifest, cfg: Config) -> str:
    """Encode which repos want a first-boot ``poetry install`` as ``CLD_REPO_BOOTSTRAP``.

    Format ``<name>=<pyproject subdir>`` joined by ``;``; repos without the
    registry ``bootstrap`` key (ad-hoc repos included) are omitted. The subdir
    is the repo's own ``pyproject_dir`` (``.cld/config.toml``), default ``.``
    -- resolved host-side from the same file as ``warn_skipped_repo_files``.
    """
    parts: list[str] = []
    for repo in manifest.repos:
        entry = cfg.repos.get(repo.name)
        if not entry or not entry.bootstrap:
            continue
        repo_config = Path(repo.path) / ".cld" / "config.toml"
        subdir = "."
        if repo_config.is_file():
            subdir = _load_toml(repo_config).get("pyproject_dir", ".") or "."
        if not _bootstrap_subdir_ok(subdir):
            log.warning(
                "repo '%s': pyproject_dir %r skipped: not a relative path without '..', ';' or '='",
                repo.name, subdir,
            )
            continue
        parts.append(f"{repo.name}={subdir}")
    return ";".join(parts)


def build_ticket_container_args(manifest: TicketManifest, cfg: Config) -> list[str]:
    """Build the ``docker run`` argument list for a ticket container (v2).

    Deliberately separate from ``build_container_args``: the ticket kind
    mounts the store of N repos at ``/workspace/origin/<name>``, works at the ticket
    root ``/workspace/<slug>``, carries the manifest as labels (authoritative
    JSON plus flat per-repo path labels), and replaces the scalar
    ``CLD_HOST_PROJECT_DIR``/``CLD_HOST_HOME`` pair with the ``CLD_PATH_MAP``
    prefix map -- v1 kinds keep their scalar plumbing untouched.

    Anchors ride only in the manifest label: no ``org.cld.anchor`` labels here.
    The effective per-repo anchor is derived from the jj store at check time.
    """
    session = ticket_container_name(manifest.ticket)
    home = os.path.expanduser("~")
    host_home = to_host_path(home, cfg)

    args = ["--name", session]
    for key, value in manifest_labels(manifest, session).items():
        args += ["--label", f"{key}={value}"]

    # Security and resources (same posture as every other kind)
    args += [
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--cpus=2.0",
        "--memory=4g",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-e", f"HOME={CONTAINER_HOME}",
    ]

    require_boot_api(cfg.base_image, cfg.devcontainer_image)
    args += ["--label", f"{ORIGIN_MOUNT_LABEL}={ORIGIN_MOUNT_VALUE}"]

    # Per-repo origin: the store rows only, no checkout; the workspaces
    # themselves live in the container layer under the ticket root, which is the
    # harness cwd.
    # Both container prefixes of a repo map to its origin host path so
    # host-facing messages naming workspace files resolve to something the
    # user can open (design section 6.4).
    path_map: dict[str, str] = {}
    for repo in manifest.repos:
        host_path = to_host_path(repo.path, cfg)
        origin = f"{WORKSPACE_BASE}/origin/{repo.name}"
        args += _repo_store_args(repo.name, Path(repo.path), host_path, origin)
        path_map[origin] = host_path
        path_map[f"{WORKSPACE_BASE}/{manifest.ticket}/{repo.name}"] = host_path
    path_map[CONTAINER_HOME] = host_home
    # No -w: the daemon would pre-create the ticket root as root:root before
    # the entrypoint can mkdir it as the container user; the post-boot
    # `docker exec -w` calls supply the working directory instead.

    args += stage_ssl_certs(cfg)

    # Claude session state (required; rw for OAuth refresh, as in v1)
    if not (Path(home) / ".claude").is_dir():
        log.error(f"{home}/.claude not found -- Claude auth and session state unavailable")
        sys.exit(1)
    args += ["-v", f"{host_home}/.claude:{CONTAINER_HOME}/.claude:rw"]
    repo_paths = [Path(repo.path) for repo in manifest.repos]
    args += stage_plugin_marketplaces(cfg, repo_paths)

    for rel in cfg.home_mounts_always:
        if mnt := stage_home_ro(rel, cfg, repo_paths):
            args += mnt
        else:
            log.warning(f"~/{rel} not found -- skipping")

    args += [
        "-e", "TICKET_MODE=1",
        "-e", f"SESSION_NAME={session}",
        "-e", f"CLD_TICKET_MANIFEST={manifest.to_json()}",
        "-e", f"CLD_PATH_MAP={json.dumps(path_map)}",
    ]
    log.info(f"Session name: {session}")

    warn_skipped_repo_files(manifest)

    if bootstrap := ticket_repo_bootstrap(manifest, cfg):
        args += ["-e", f"CLD_REPO_BOOTSTRAP={bootstrap}"]
        log.debug("Bootstrap repos: %s", bootstrap)

    # The interactive user's own session, so it gets the full persistent-role
    # wiring: broker, otel, mailbox.
    args += stage_broker(cfg)
    args += stage_otel(cfg, session)
    args += stage_mailbox(cfg)

    log.debug("Ticket container args: %s", mask_secrets(repr(args)))
    return args


def host_identity_name(repo_root: Path) -> str:
    """The messenger identity the *host* acts as for *repo_root*.

    Named after the v1 master container that used to own this mailbox, and
    kept byte-identical to it so existing mailbox directories keep resolving
    (cld/messenger/identity.py). No container carries this name any more: it
    is the last-resort sender/reader identity for a host-side `cld msg` that
    resolves to no ticket.
    """
    sha = hashlib.sha1(str(repo_root).encode()).hexdigest()[:8]
    return f"cld_master_{repo_root.name}_{sha}"


def ticket_slug(ticket: str) -> str:
    """Sanitize a free-form ticket name into the slug that names the container,
    workspaces, bookmarks and mailbox (``LIDE-2600`` -> ``lide-2600``)."""
    slug = re.sub(r"[^a-z0-9]+", "-", ticket.lower()).strip("-")
    if not slug:
        raise ValueError(f"ticket name {ticket!r} sanitizes to an empty slug")
    return slug


def ticket_container_name(ticket: str) -> str:
    """Deterministic container name for a ticket: ``cld_ticket_<slug>``.

    One container per ticket, enforced by the name (PRODUCT_DESIGN.md
    section 3). Idempotent on an already-sanitized slug.
    """
    return f"cld_ticket_{ticket_slug(ticket)}"


_TASK_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def task_agent_container_name(repo_root: Path, slug: str, suffix: int = 0) -> str:
    """Container name for a task-scoped agent: ``cld_agent_<repo>_<slug>[-<suffix>]``.

    The ``cld_agent_`` prefix was deliberately shared with the (now removed)
    standing repo agent -- the ``org.cld.kind`` label is the discriminator, not
    the name (see docs/design-task-agents.md D5). *suffix* > 1 is the collision
    disambiguator.
    """
    if not _TASK_SLUG_RE.match(slug):
        raise ValueError(
            f"invalid task slug {slug!r}: expected kebab-case (lowercase letters, "
            "digits and dashes, starting with a letter or digit)"
        )
    base = f"cld_agent_{repo_root.name}_{slug}"
    return f"{base}-{suffix}" if suffix > 1 else base


def allocate_task_agent_name(repo_root: Path, slug: str) -> str:
    """Return the first task-agent name for *slug* that no container holds yet.

    Docker is the liveness ground truth here: an archived mailbox from a reaped
    agent must not block its slug from being reused (§4). The master should still
    prefer a fresh slug so names stay meaningful.
    """
    suffix = 0
    while True:
        name = task_agent_container_name(repo_root, slug, suffix)
        if _docker_status(name) == "absent":
            return name
        log.info("task-agent name %s is taken; trying the next suffix", name)
        suffix = max(suffix, 1) + 1


def _docker_status(name: str) -> str:
    """Return 'running', 'stopped', or 'absent' for the named container."""
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Status}}", name],
        capture_output=True, text=True,
    )
    log_subprocess(log, ["docker", "inspect", name], result)
    if result.returncode != 0:
        return "absent"
    return "running" if result.stdout.strip() == "running" else "stopped"


def docker_task_agent_status(name: str) -> str:
    return _docker_status(name)


# `docker inspect` exposes a container's labels at .Config.Labels; there is no
# top-level .Labels (that's a `docker ps --format` field) and referencing a
# missing *field* fails the whole template, which would silently drop every
# container. A missing label *key* is fine -- index on a map yields "".
_INSPECT_LABELS = (
    ("repo_root", "org.cld.repo-root"),
    ("session", "org.cld.session"),
    ("kind", "org.cld.kind"),
    ("parent", "org.cld.parent-master"),
    ("task", "org.cld.task"),
    ("anchor", "org.cld.anchor"),
    ("anchor_mode", "org.cld.anchor-mode"),
)
_INSPECT_FORMAT = "|".join(f'{{{{index .Config.Labels "{label}"}}}}' for _, label in _INSPECT_LABELS)


def _docker_kind_list(kind: str, *, running_only: bool = False) -> list[dict]:
    """Return containers of *kind* ('task-agent', 'ticket') with their org.cld.* labels.

    Records are ``{name, repo_root, session, kind, parent, task}``; the last two are
    empty for roles that don't set them. ``running_only`` filters docker-side --
    the task-agent cap counts running containers only, and `docker ps -a` would
    otherwise let stopped corpses refuse a spawn (see docs/design-task-agents.md §9).
    """
    filters = ["--filter", f"label=org.cld.kind={kind}"]
    if running_only:
        filters += ["--filter", "status=running"]
    result = subprocess.run(
        ["docker", "ps", "-a", *filters, "--format", "{{.Names}}"],
        capture_output=True, text=True,
    )
    log_subprocess(log, ["docker", "ps", "-a", *filters], result)
    if result.returncode != 0:
        return []
    keys = [key for key, _ in _INSPECT_LABELS]
    containers: list[dict] = []
    for name in result.stdout.strip().splitlines():
        if not name:
            continue
        inspect = subprocess.run(
            ["docker", "inspect", name, "--format", _INSPECT_FORMAT],
            capture_output=True, text=True,
        )
        log_subprocess(log, ["docker", "inspect", name], inspect)
        if inspect.returncode != 0:
            continue
        values = (inspect.stdout.strip().split("|") + [""] * len(keys))[:len(keys)]
        containers.append({"name": name, **dict(zip(keys, values, strict=True))})
    return containers


def docker_task_agent_list(*, running_only: bool = False) -> list[dict]:
    """Return all task-agent containers with their org.cld.* labels."""
    return _docker_kind_list("task-agent", running_only=running_only)


def assert_task_agent_capacity(cfg: Config, parent_master: str) -> None:
    """Raise if *parent_master* already runs ``cfg.max_task_agents`` task-agents.

    Running containers only -- stopped corpses must not refuse a spawn. The empty
    parent (a human-launched agent with no master) is counted as its own group:
    the cap exists to bound shared jj-store contention, which doesn't care who
    spawned what. See docs/design-task-agents.md §9.
    """
    running = [c for c in docker_task_agent_list(running_only=True) if c["parent"] == parent_master]
    if len(running) < cfg.max_task_agents:
        return
    listed = ", ".join(f"{c['name']} ({c['task'] or 'no task'})" for c in running)
    raise RuntimeError(
        f"task-agent cap reached for {parent_master or 'host-launched agents (no master)'}: "
        f"{len(running)}/{cfg.max_task_agents} running [{listed}]. Reap one with "
        "`cld task-agent shutdown <name>`, or raise max_task_agents / CLD_MAX_TASK_AGENTS."
    )


ANCHOR_LABEL = "org.cld.anchor"
ANCHOR_MODE_LABEL = "org.cld.anchor-mode"

_OCCUPANT_INSPECT_FORMAT = (
    '{{index .Config.Labels "org.cld.repo-root"}}|'
    '{{index .Config.Labels "org.cld.anchor"}}|'
    '{{index .Config.Labels "org.cld.anchor-mode"}}|'
    '{{index .Config.Labels "org.cld.kind"}}|'
    '{{index .Config.Labels "org.cld.session"}}'
)

# Kinds whose reach strictly blocks another container's anchor. Headless roles
# (task-agent, run) can silently rewrite their stack with nobody watching, so
# they block; any other kind carrying an anchor label does not. Tickets occupy
# trees too, but they are collected separately (from their manifest label) and
# only warn against other tickets -- stacked tickets are legitimate, see
# resolve_anchor_checked.
#
# "agent" -- the removed standing per-repo role -- stays in the set on purpose:
# nothing launches one any more, but a container left over from an older cld
# image still owns its tree and must still refuse an overlapping anchor.
# Dropping it would silently widen what a new container may claim.
ANCHOR_BLOCKING_KINDS = {"agent", "task-agent", "run"}


def _docker_names_with_state(label_filter: str) -> list[tuple[str, str]]:
    """``(name, state)`` of every container -- running or stopped -- carrying
    *label_filter*. A docker failure reads as no containers, like every other
    lister here: without a daemon there is nothing to launch against either."""
    cmd = ["docker", "ps", "-a", "--filter", f"label={label_filter}",
           "--format", "{{.Names}}\t{{.State}}"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    log_subprocess(log, cmd, result)
    if result.returncode != 0:
        return []
    rows: list[tuple[str, str]] = []
    for line in result.stdout.strip().splitlines():
        name, _, state = line.partition("\t")
        if name:
            rows.append((name, state))
    return rows


def docker_occupant_list() -> list[dict]:
    """Every per-repo tree occupant the overlap check must consider, host-wide.

    Two sources, one record shape
    ``{name, repo_root, anchor_base, session, mode, kind}``: v1 headless kinds
    via their ``org.cld.anchor`` labels, and ticket containers via their
    manifest label expanded into one record per mounted repo. A ticket whose
    manifest cannot be read still occupies its (now unknown) trees, so it
    yields one *unverifiable* record -- empty ``repo_root``/``anchor_base``
    plus a ``manifest_error`` -- that ``resolve_anchor_checked`` must treat as
    a failed probe against every repo, not silently drop (fail-closed).

    Stopped persistent containers (ticket, agent, task-agent) still own their
    bookmarks and can be restarted into their trees, so they are included;
    stopped ``run`` corpses cannot return (``--rm``) and are not. A kind
    outside ``ANCHOR_BLOCKING_KINDS`` never occupies from the label scan.
    ``repo_root`` is realpath-normalized at record-build time so a symlinked
    launch path cannot bypass the check.
    """
    records: list[dict] = []
    for name, state in _docker_names_with_state(ANCHOR_LABEL):
        inspect = subprocess.run(
            ["docker", "inspect", name, "--format", _OCCUPANT_INSPECT_FORMAT],
            capture_output=True, text=True,
        )
        log_subprocess(log, ["docker", "inspect", name], inspect)
        if inspect.returncode != 0:
            continue
        repo_root, anchor, anchor_mode, kind, session = (
            inspect.stdout.strip().split("|") + [""] * 5
        )[:5]
        if kind not in ANCHOR_BLOCKING_KINDS:
            continue
        if kind == "run" and state != "running":
            continue
        records.append({
            "name": name, "repo_root": str(Path(repo_root).resolve()),
            "anchor_base": anchor, "session": session or name,
            "mode": anchor_mode or "isolated", "kind": kind,
        })
    for name, _state in _docker_names_with_state("org.cld.kind=ticket"):
        try:
            manifest = read_manifest(name)
        except (RuntimeError, ValueError, KeyError) as e:
            records.append({
                "name": name, "repo_root": "", "anchor_base": "",
                "session": name, "mode": "isolated", "kind": "ticket",
                "manifest_error": str(e),
            })
            continue
        for repo in manifest.repos:
            records.append({
                "name": name, "repo_root": str(Path(repo.path).resolve()),
                "anchor_base": repo.anchor_base, "session": name,
                "mode": repo.anchor_mode, "kind": "ticket",
            })
    return records


def _effective_anchor(vcs, occupant: dict) -> str:
    """Map an occupant record to its effective anchor.

    In isolated mode the editable boundary is the scratch commit B, staged
    in-container as a child of the labeled base A *after* launch -- labels are
    immutable, so B is derived from the jj store at check time using the same
    revset the entrypoint's restart recovery uses (cld_recover_anchor,
    imgs/claude-devcontainer/vcs-lib.sh): searched among the session
    bookmark's ancestors, not A's children, because the host may rebase B
    off A. An empty result is the transient window between ``docker run``
    and staging; falling back to A only ever over-blocks, since A's reach is
    a superset of B's. Shared mode edits descendants of A itself, so A *is*
    the effective anchor there.
    """
    if occupant["mode"] != "isolated":
        return occupant["anchor_base"]
    revset = (
        f"heads(ancestors(bookmarks(exact:\"{occupant['session']}\")) & "
        # The trailing space before the wildcard is load-bearing: every staged
        # description is 'cld anchor: <session> mode=<mode>' (stage_in_workspace),
        # and without it session cld_ticket_x-1 would also match cld_ticket_x-12.
        f"description(glob:'cld anchor: {occupant['session']} *'))"
    )
    result = vcs.run(["log", "-r", revset, "--no-graph", "-T", "change_id", "-n", "1"])
    if result.returncode != 0:
        log.warning(
            "could not derive the effective anchor of %s, falling back to its base %s: %s",
            occupant["name"], occupant["anchor_base"][:12], (result.stderr or "").strip(),
        )
        return occupant["anchor_base"]
    return result.stdout.strip() or occupant["anchor_base"]


def resolve_anchor_checked(
    cfg: Config,
    repo_root: Path,
    revision: str,
    mode: str = "isolated",
    caller_kind: str = "agent",
    exclude_session: str = "",
) -> str:
    """Resolve *revision* to an anchor id (``resolve_anchor``: a change id on
    jj), checking overlap with every other cld container's reach in the same repo.

    A container's "reach" -- the set of commits it may touch -- is the
    descendant tree of its *effective* anchor: scratch commit B in isolated
    mode, the base A in shared mode (``_effective_anchor``). Occupants are
    every headless container plus every ticket container mounting this repo,
    running or stopped-but-restartable (``docker_occupant_list``); repo
    identity is realpath-normalized on both sides. Two overlap conditions:

    1. Always: *revision* resolves to a commit inside an occupant's reach. An
       occupant may squash or rebase its own stack at any moment, so anchoring
       inside it pins the new container to a base its owner has since revised
       -- silently, and unfixable without re-anchoring from scratch.
    2. Only when ``mode == "shared"``: an occupant's effective anchor falls
       inside *this* anchor's reach (``descendants(revision)``). A shared
       anchor's reach is its own full descendant tree, so a shared spawn must
       not claim a tree with an occupant already inside it -- two isolated
       siblings off the same base are unaffected, since an isolated
       container's reach starts one commit below the shared base.

    The outcome depends on the (caller, occupant) kind pair: ticket vs ticket
    overlap warns and proceeds (stacked tickets are legitimate; silence would
    hide accidents); every other pairing blocks, including any caller kind
    this function does not recognize.

    Fail-closed: a probe that cannot be evaluated counts as overlap -- block
    for non-ticket callers, warn ("could not verify") for ticket callers. A
    check that silently passes on error is worse than none. A ticket container
    whose manifest cannot be read has unknown repo paths, so it counts as
    unverifiable against *every* repo checked, with the same block/warn split.

    jj-only -- peer-side anchor staging has no git equivalent; git repos keep
    weaker guarantees. Anchoring on a finished (reaped) sibling's deliverable
    branch still passes in either mode.

    ``exclude_session``, when set, drops the occupant record whose ``name``
    matches it before the check runs -- a ticket reanchoring itself must not
    be blocked or warned about its own live reach (``cld/ticket.py``
    ``_reanchor_restart``).
    """
    from cld.vcs import get_backend
    from cld.vcs.anchor import resolve_anchor

    vcs = get_backend(repo_root)
    anchor = resolve_anchor(vcs, revision)
    if vcs.name != "jj":
        log.debug("live-anchor overlap check skipped: %s backend has no equivalent", vcs.name)
        return anchor

    host_repo = str(Path(to_host_path(str(repo_root), cfg)).resolve())
    occupants = [
        c for c in docker_occupant_list()
        if c["name"] != exclude_session
        and (c.get("manifest_error") or (c["repo_root"] == host_repo and c["anchor_base"]))
    ]
    if not occupants:
        return anchor

    def _probe(revset: str) -> bool | None:
        """True: overlap. False: disjoint. None: could not evaluate."""
        result = vcs.run(["log", "-r", revset, "--no-graph", "-T", "commit_id", "-n", "1"])
        if result.returncode != 0:
            log.warning(
                "could not evaluate the anchor overlap check (%s): %s",
                revset, (result.stderr or "").strip(),
            )
            return None
        return bool(result.stdout.strip())

    def _refuse_or_warn(occupant: dict, message: str) -> None:
        if caller_kind == "ticket" and occupant["kind"] == "ticket":
            log.warning("%s Proceeding: stacked tickets are legitimate.", message)
            return
        raise RuntimeError(
            f"refusing to anchor on {anchor[:12]}: {message} Reap/stop it first -- "
            "teardown is what makes its deliverable branch safe to anchor on -- "
            "or anchor outside its tree instead."
        )

    def _unverified(occupant: dict, detail: str) -> None:
        message = (
            f"could not verify anchor {anchor[:12]} against the reach of "
            f"{occupant['name']} ({occupant['kind']}, {detail})."
        )
        if caller_kind == "ticket":
            log.warning("%s Proceeding: ticket overlap is warn-strength.", message)
            return
        raise RuntimeError(f"refusing to anchor on {anchor[:12]}: {message}")

    for occupant in occupants:
        if manifest_error := occupant.get("manifest_error"):
            # An unreadable ticket manifest leaves no repo paths to match, so
            # this occupant is unverifiable against every repo the caller
            # checks -- the same fail-closed split as a failed probe.
            _unverified(occupant, f"unreadable manifest: {manifest_error}")
            continue
        effective = _effective_anchor(vcs, occupant)
        inside = _probe(f"{anchor} & {effective}::")
        if inside is None:
            _unverified(occupant, f"anchor {occupant['anchor_base'][:12]}, "
                                  f"revset {anchor} & {effective}::")
        elif inside:
            _refuse_or_warn(occupant, (
                f"{anchor[:12]} is inside the live reach of {occupant['name']} "
                f"({occupant['kind']}, effective anchor {effective[:12]}), which "
                "can still rewrite that stack."
            ))
        if mode != "shared":
            continue
        occupied = _probe(f"{effective} & {anchor}::")
        if occupied is None:
            _unverified(occupant, f"anchor {occupant['anchor_base'][:12]}, "
                                  f"revset {effective} & {anchor}::")
        elif occupied:
            _refuse_or_warn(occupant, (
                f"a shared anchor at {anchor[:12]} claims the whole tree, and "
                f"{occupant['name']} ({occupant['kind']}) is already inside it "
                f"at {effective[:12]}."
            ))

    return anchor


def ticket_anchor_resolver(
    cfg: Config, exclude_session: str = "",
) -> Callable[[str, str, str], str]:
    """The ``resolve_manifest`` resolver for the ticket launch path: pins each
    repo's ``(path, revision, mode)`` through the overlap check with ticket
    semantics -- warn on other tickets' trees, block on headless reach.

    ``exclude_session``, forwarded to ``resolve_anchor_checked``, is set by a
    reanchoring restart to the ticket's own container name so it is not
    checked against its own live reach.

    Runs the store preflight first: no host jj may touch the repo before it.
    """
    def resolve(path: str, revision: str, mode: str) -> str:
        prepare_store_mounts(Path(path))
        return resolve_anchor_checked(
            cfg, Path(path), revision, mode, caller_kind="ticket",
            exclude_session=exclude_session,
        )
    return resolve


_CONTAINER_SSH_AUTH_SOCK = "/run/host-ssh-agent.sock"


def stage_ssh_agent(cfg: Config) -> list[str]:
    """Return -v/-e args to forward the host ssh-agent socket into a devcontainer.

    The host's ``~/.ssh/known_hosts`` rides along, so the container trusts the
    same host keys the host already accepted. Tri-state on ``cfg.ssh_auth_sock``:
      - ``""`` (explicit) -> disabled, return [].
      - ``None`` (unset)  -> auto-detect from $SSH_AUTH_SOCK.
      - path              -> use that host socket path.
    Never fatal: on any resolution or validation failure, warn and skip.
    """
    if cfg.ssh_auth_sock == "":
        log.debug("SSH agent forward: explicitly disabled via ssh_auth_sock=''")
        return []
    sock = cfg.ssh_auth_sock or os.environ.get("SSH_AUTH_SOCK", "")
    if not sock:
        log.debug("SSH agent forward: no SSH_AUTH_SOCK available -- skipping")
        return []
    if not Path(sock).is_socket():
        log.warning("SSH_AUTH_SOCK=%s is not a socket -- skipping agent forward", sock)
        return []
    host_sock = to_host_path(sock, cfg)
    log.info("SSH agent socket forwarded: %s -> %s", sock, _CONTAINER_SSH_AUTH_SOCK)
    return [
        "-v", f"{host_sock}:{_CONTAINER_SSH_AUTH_SOCK}",
        "-e", f"SSH_AUTH_SOCK={_CONTAINER_SSH_AUTH_SOCK}",
        *stage_home_ro(".ssh/known_hosts", cfg),
    ]


def stage_ssl_certs(cfg: Config) -> list[str]:
    """Return docker args shadowing the baked CA bundle with a host-supplied one.

    Internal (Seznam) roots are baked into the base image trust store, so no
    mount is needed by default. ``cfg.ssl_certs_path`` is an explicit escape
    hatch (dir or PEM file) -- opt in only, and it *replaces* rather than merges.
    """
    if not cfg.ssl_certs_path:
        return []
    ssl_path = Path(cfg.ssl_certs_path)
    log.info("SSL: replacing baked CA bundle with %s (opt-in via ssl_certs_path)", ssl_path)
    if ssl_path.is_dir():
        return ["-v", f"{ssl_path}:/etc/ssl/certs:ro",
                "-e", "NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt"]
    return ["-v", f"{ssl_path}:/etc/ssl/cert.pem:ro",
            "-e", "SSL_CERT_FILE=/etc/ssl/cert.pem",
            "-e", "REQUESTS_CA_BUNDLE=/etc/ssl/cert.pem",
            "-e", "NODE_EXTRA_CA_CERTS=/etc/ssl/cert.pem"]


def stage_mailbox(cfg: Config) -> list[str]:
    """Return the ``-v`` args mounting the shared mailbox tree RW."""
    mailbox_root = Path(cfg.mailbox_root).expanduser()
    host_mailbox_root = to_host_path(str(mailbox_root), cfg)
    if host_mailbox_root == str(mailbox_root):
        # Bare host: our own filesystem view already *is* the host view.
        mailbox_root.mkdir(parents=True, exist_ok=True)
    else:
        # Nested (cld running inside another container): the real host path
        # isn't in our filesystem view, and with no docker socket mounted
        # there is no way (nor any wish) to reach across to the host to
        # create it -- container isolation from the host is a hard
        # requirement. If the path doesn't already exist on the real host,
        # `docker run` will auto-create it as root and the non-root
        # container user won't be able to write into it.
        log.warning(
            "Mailbox root %s is outside this process's filesystem view "
            "(nested cld). If it doesn't already exist on the real host, "
            "create it there manually before continuing: "
            "mkdir -p %s && chown %d:%d %s",
            host_mailbox_root, host_mailbox_root, os.getuid(), os.getgid(), host_mailbox_root,
        )
    log.info("Mailbox mounted: %s -> %s", host_mailbox_root, MAILBOX_MOUNT)
    return ["-v", f"{host_mailbox_root}:{MAILBOX_MOUNT}:rw"]


def stage_broker(cfg: Config) -> list[str]:
    """Return docker args wiring a container to the cld broker.

    Mounts the restricted broker private key (and, if given, the pinned
    known_hosts) RO, adds a host-gateway alias so the container can reach the
    host-side sshd, and sets ``CLD_BROKER_ENDPOINT`` so the in-container client
    (``cld broker <action>``) can reach it. No-op unless ``cfg.broker_key`` is set.
    Called for task-agent and ticket containers
    (see the call site in ``build_container_args``); the broker's sshd accepts
    any ``cld_*`` session and resolves its role from the ``org.cld.kind`` label
    set at launch, not from the name -- see ``broker/cld-broker.py``. See
    docs/design-cld-broker.md.
    """
    if not cfg.broker_key:
        return []
    key = Path(cfg.broker_key).expanduser()
    if not key.is_file():
        log.warning("broker_key set but not found: %s", key)
        return []
    host_key = to_host_path(str(key.resolve()), cfg)
    args = [
        "--add-host", "host.docker.internal:host-gateway",
        "-v", f"{host_key}:{_BROKER_KEY_MOUNT}:ro",
        "-e", f"CLD_BROKER_ENDPOINT={cfg.broker_endpoint}",
    ]
    if cfg.broker_known_hosts:
        known = Path(cfg.broker_known_hosts).expanduser()
        if known.is_file():
            host_known = to_host_path(str(known.resolve()), cfg)
            args += ["-v", f"{host_known}:{_BROKER_KNOWN_HOSTS_MOUNT}:ro"]
        else:
            log.warning("broker_known_hosts set but not found: %s", known)
    else:
        log.warning(
            "broker_key set but broker_known_hosts is empty -- "
            "cld broker run-tests's strict host-key check will fail without a pinned known_hosts"
        )
    log.info("Host test broker wired: key -> %s, endpoint %s", _BROKER_KEY_MOUNT, cfg.broker_endpoint)
    return args


def stage_otel(cfg: Config, session_name: str) -> list[str]:
    """Return docker args pointing Claude Code's own OTEL export at a host collector.

    Adds a host-gateway alias (same mechanism as ``stage_broker``) and sets
    the standard ``OTEL_*`` env vars Claude Code reads natively -- no cld-side
    daemon or receiver involved, just Claude Code talking OTLP/http to
    whatever collector is listening at ``cfg.otel_endpoint``. The collector is
    a plain, cld-independent OpenTelemetry Collector; see otel/README.md.
    Session attribution uses the standard ``service.name`` resource attribute
    (not a cld-specific one), so the collector and any aggregation on top of
    it stay usable outside this repo. No-op unless ``cfg.otel_endpoint`` is set.
    """
    if not cfg.otel_endpoint:
        return []
    return [
        "--add-host", "host.docker.internal:host-gateway",
        "-e", "CLAUDE_CODE_ENABLE_TELEMETRY=1",
        "-e", "OTEL_METRICS_EXPORTER=otlp",
        "-e", "OTEL_EXPORTER_OTLP_PROTOCOL=http/json",
        "-e", f"OTEL_EXPORTER_OTLP_ENDPOINT=http://{cfg.otel_endpoint}",
        "-e", f"OTEL_RESOURCE_ATTRIBUTES=service.name={session_name}",
    ]


def _home_mount_skip_reason(rel_path: str, protected_repos: Iterable[Path]) -> str | None:
    """Why ``$HOME/<rel_path>`` must not be mounted, or None.

    The source is checked once and docker resolves it again at every
    ``docker start``: it must stay inside ``$HOME`` with no symlink to swap,
    and clear of every repo.
    """
    rel = Path(rel_path)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        return "not a relative path below $HOME without '..'"
    home = Path.home()
    prefixes = [home.joinpath(*rel.parts[:i]) for i in range(1, len(rel.parts) + 1)]
    if link := next((p for p in prefixes if p.is_symlink()), None):
        return f"{link} is a symlink"
    if repo := next((repo for repo in protected_repos if _overlaps(home / rel, repo)), None):
        return f"it overlaps the repo {repo}"
    return None


def stage_home_ro(rel_path: str, cfg: Config, repo_roots: Iterable[Path] = ()) -> list[str]:
    """Stage ``$HOME/<rel_path>`` RO under ``/tmp/host-config/<rel_path>``.

    Returns the ``["--mount", ...]`` arg pair, or ``[]`` if the source doesn't
    exist or fails ``_home_mount_skip_reason`` (a warning names the reason).
    Overlap is checked against the registry and *repo_roots*.
    The entrypoint copies the staged tree into ``$HOME`` (see ``copy_host_configs``).
    """
    repos = [*(Path(entry.path).expanduser() for entry in cfg.repos.values()), *repo_roots]
    if reason := _home_mount_skip_reason(rel_path, repos):
        log.warning("home mount '%s' skipped: %s", rel_path, reason)
        return []
    local_path = Path.home() / rel_path
    if not local_path.exists():
        return []
    host_path = to_host_path(str(local_path.resolve()), cfg)
    try:
        return bind_mount_args(host_path, f"{_RO_HOME_MOUNT_ROOT}/{rel_path}", readonly=True)
    except ValueError as e:
        log.warning("home mount '%s' skipped: %s", rel_path, e)
        return []


def _read_json_dict(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning(f"Cannot read {path}: {e}")
        return {}
    return data if isinstance(data, dict) else {}


_MARKETPLACE_MARKER = ".claude-plugin/marketplace.json"


def _extra_marketplaces(settings_file: Path) -> list[object]:
    extra = _read_json_dict(settings_file).get("extraKnownMarketplaces")
    return list(extra.values()) if isinstance(extra, dict) else []


def _marketplace_paths(entries: Iterable[object]) -> list[tuple[str, Path]]:
    """``(kind, path)`` of each directory or file source; other kinds need no mount."""
    paths: list[tuple[str, Path]] = []
    for entry in entries:
        source = entry.get("source") if isinstance(entry, dict) else None
        if not isinstance(source, dict) or not isinstance(source.get("path"), str):
            continue
        if source.get("source") in ("directory", "file"):
            paths.append((source["source"], Path(source["path"]).expanduser()))
    return paths


def _path_forms(path: Path) -> tuple[Path, Path]:
    return Path(os.path.normpath(path)), Path(os.path.realpath(path))


def _overlaps(a: Path, b: Path) -> bool:
    """Whether *a* and *b* are the same folder or one holds the other, comparing
    the literal and the resolved form of each."""
    return any(x == y or x in y.parents or y in x.parents for x in _path_forms(a) for y in _path_forms(b))


def _marketplace_skip_reason(
    kind: str, path: Path, protected_repos: list[Path], agent_dirs: list[Path],
) -> str | None:
    """Why a marketplace folder must not be mounted, or None (design section 4.2).

    The mount is checked once and docker resolves its source again at every
    ``docker run`` and ``docker start``, so the folder must be one no agent
    container can write: not inside (or holding) ``agent_dirs`` or a repo,
    and with no symlink in its path, so that nothing can be swapped later.
    """
    if not path.is_absolute():
        return "not an absolute path"
    if not os.path.lexists(path):
        return "not found"
    if kind == "directory":
        folder, marker = path, path / _MARKETPLACE_MARKER
        links = [folder, marker.parent, marker]
    else:
        folder, marker = path.parent, path
        links = [folder, marker]
        if marker.name != "marketplace.json":
            return "a file source must be named marketplace.json"
    if (link := next((p for p in links if p.is_symlink()), None)) is not None:
        return f"{link} is a symlink"
    if not folder.is_dir():
        return "not a directory"
    if not marker.is_file():
        return f"marker {marker} is missing or not a regular file"
    literal, real = _path_forms(folder)
    if literal != real:
        return f"a symlink is in its path (the real path is {real})"
    home = Path.home().resolve()
    if folder == home or folder in home.parents:
        return "it is $HOME or contains it"
    for repo in protected_repos:
        if _overlaps(folder, repo):
            return f"it overlaps the repo {repo}"
    for agent_dir in agent_dirs:
        if _overlaps(folder, agent_dir):
            return f"it overlaps {agent_dir}, which agent containers write"
    return None


def stage_plugin_marketplaces(cfg: Config, repo_roots: list[Path]) -> list[str]:
    """Mount directory/file-sourced plugin marketplaces RO at their host path.

    Claude's plugin registry stores these as absolute host paths, which do
    not exist in the container, so the marketplace fails to load. Mounting
    each at the identical path makes the registry entries resolve as-is.
    Sources are read from the host registry and the host user settings;
    other source kinds (github, git, ...) need no mount. Settings are
    writable by agents, so a folder is mounted only if it passes
    ``_marketplace_skip_reason`` (design section 4.2): that check holds for
    every later ``docker start`` too, as it demands a folder no agent can
    write. The mount is ``--mount ...,readonly`` of the checked path. Paths
    listed in a repo's own settings are never mounted: any commit can change
    them. Both cases warn on every launch.
    """
    claude_dir = Path.home() / ".claude"
    user_sources = [
        *_read_json_dict(claude_dir / "plugins/known_marketplaces.json").values(),
        *_extra_marketplaces(claude_dir / "settings.json"),
    ]
    candidates: list[tuple[str, Path, Path | None]] = [
        (kind, path, None) for kind, path in _marketplace_paths(user_sources)
    ]
    for root in repo_roots:
        for settings_file in (root / ".claude/settings.json", root / ".claude/settings.local.json"):
            candidates += [
                (kind, path, settings_file)
                for kind, path in _marketplace_paths(_extra_marketplaces(settings_file))
            ]
    if not candidates:
        return []

    # Every repo a container of this host mounts, not only this launch's.
    protected_repos = [
        *(Path(entry.path).expanduser() for entry in cfg.repos.values()),
        *repo_roots,
        *(Path(occupant["repo_root"]) for occupant in docker_occupant_list() if occupant["repo_root"]),
    ]
    agent_dirs = [claude_dir, Path(cfg.mailbox_root).expanduser()]
    seen: set[tuple[str, Path]] = set()
    mounts: dict[str, list[str]] = {}
    for kind, path, repo_settings in candidates:
        if (kind, path) in seen:
            continue
        seen.add((kind, path))
        reason = (
            f"listed in {repo_settings}, which the repo controls"
            if repo_settings
            else _marketplace_skip_reason(kind, path, protected_repos, agent_dirs)
        )
        if not reason:
            host_path = to_host_path(str(path if kind == "directory" else path.parent), cfg)
            try:
                mounts[host_path] = bind_mount_args(host_path, host_path, readonly=True)
            except ValueError as e:
                reason = str(e)
        if reason:
            log.warning("plugin marketplace: '%s' skipped: %s", path, reason)

    return [arg for host_path in sorted(mounts) for arg in mounts[host_path]]
