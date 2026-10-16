"""Container setup: arg building, image management, path translation."""

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from cld.config import Config, _load_toml
from cld.manifest import TicketManifest, manifest_labels, read_manifest
from cld.log import get_logger, log_subprocess, mask_secrets
from cld.vcs import get_backend

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

    Sets up security constraints, volume mounts (repo, claude config),
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

    # Workspace (required)
    args += [
        "-v", f"{host_repo_root}:{WORKSPACE_BASE}/origin",
        "-w", f"{WORKSPACE_BASE}/current",
    ]

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
        mnt = stage_home_ro(rel, cfg)
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

    # Workspace setup: gitignored files to symlink into workspace
    if cfg.ignore_gitignore:
        workspace_files = ":".join(cfg.ignore_gitignore)
        args += ["-e", f"WORKSPACE_FILES={workspace_files}"]
        log.debug(f"Workspace files to link: {workspace_files}")

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


def ticket_repo_files(manifest: TicketManifest) -> str:
    """Encode each repo's ``ignore_gitignore`` list as the ``CLD_REPO_FILES`` value.

    Format ``<name>=<colon-list>`` joined by ``;``; repos with nothing to link
    are omitted. Resolved host-side from each repo's own ``.cld/config.toml``
    at launch, so an edit takes effect on the next recreate (design section 4.1).
    """
    parts: list[str] = []
    for repo in manifest.repos:
        repo_config = Path(repo.path) / ".cld" / "config.toml"
        if not repo_config.is_file():
            continue
        if files := _load_toml(repo_config).get("ignore_gitignore", ()):
            parts.append(f"{repo.name}={':'.join(files)}")
    return ";".join(parts)


def ticket_repo_bootstrap(manifest: TicketManifest, cfg: Config) -> str:
    """Encode which repos want a first-boot ``poetry install`` as ``CLD_REPO_BOOTSTRAP``.

    Format ``<name>=<pyproject subdir>`` joined by ``;``; repos without the
    registry ``bootstrap`` key (ad-hoc repos included) are omitted. The subdir
    is the repo's own ``pyproject_dir`` (``.cld/config.toml``), default ``.``
    -- resolved host-side like ``ticket_repo_files`` (design section 4.2).
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
        parts.append(f"{repo.name}={subdir}")
    return ";".join(parts)


def build_ticket_container_args(manifest: TicketManifest, cfg: Config) -> list[str]:
    """Build the ``docker run`` argument list for a ticket container (v2).

    Deliberately separate from ``build_container_args``: the ticket kind
    mounts N repos RW at ``/workspace/origin/<name>``, works at the ticket
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

    # Per-repo RW origin mounts; the workspaces themselves live in the
    # container layer under the ticket root, which is the harness cwd.
    # Both container prefixes of a repo map to its origin host path so
    # host-facing messages naming workspace files resolve to something the
    # user can open (design section 6.4).
    path_map: dict[str, str] = {}
    for repo in manifest.repos:
        host_path = to_host_path(repo.path, cfg)
        origin = f"{WORKSPACE_BASE}/origin/{repo.name}"
        args += ["-v", f"{host_path}:{origin}"]
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
    args += stage_plugin_marketplaces(cfg, [Path(repo.path) for repo in manifest.repos])

    for rel in cfg.home_mounts_always:
        if mnt := stage_home_ro(rel, cfg):
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

    if repo_files := ticket_repo_files(manifest):
        args += ["-e", f"CLD_REPO_FILES={repo_files}"]
        log.debug("Workspace files to link: %s", repo_files)

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
    """
    def resolve(path: str, revision: str, mode: str) -> str:
        return resolve_anchor_checked(
            cfg, Path(path), revision, mode, caller_kind="ticket",
            exclude_session=exclude_session,
        )
    return resolve


_CONTAINER_SSH_AUTH_SOCK = "/run/host-ssh-agent.sock"


def stage_ssh_agent(cfg: Config) -> list[str]:
    """Return -v/-e args to forward the host ssh-agent socket into a devcontainer.

    Tri-state on ``cfg.ssh_auth_sock``:
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


def stage_home_ro(rel_path: str, cfg: Config) -> list[str]:
    """Stage ``$HOME/<rel_path>`` RO under ``/tmp/host-config/<rel_path>``.

    Returns the ``["-v", ...]`` arg pair, or ``[]`` if the source doesn't exist.
    The entrypoint copies the staged tree into ``$HOME`` (see ``copy_host_configs``).
    """
    local_path = Path.home() / rel_path
    if not local_path.exists():
        return []
    host_path = to_host_path(str(local_path.resolve()), cfg)
    return ["-v", f"{host_path}:{_RO_HOME_MOUNT_ROOT}/{rel_path}:ro"]


def _read_json_dict(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning(f"Cannot read {path}: {e}")
        return {}
    return data if isinstance(data, dict) else {}


def stage_plugin_marketplaces(cfg: Config, repo_roots: list[Path]) -> list[str]:
    """Mount directory/file-sourced plugin marketplaces RO at their host path.

    Claude's plugin registry stores these as absolute host paths, which do
    not exist in the container, so the marketplace fails to load. Mounting
    each at the identical path makes the registry entries resolve as-is.
    Sources are read from the host registry, the host user settings and each
    repo's project settings; other source kinds (github, git, ...) need no mount.
    """
    claude_dir = Path.home() / ".claude"
    sources: list[object] = list(_read_json_dict(claude_dir / "plugins/known_marketplaces.json").values())
    settings_files = [claude_dir / "settings.json"]
    for root in repo_roots:
        settings_files += [root / ".claude/settings.json", root / ".claude/settings.local.json"]
    for settings_file in settings_files:
        extra = _read_json_dict(settings_file).get("extraKnownMarketplaces")
        if isinstance(extra, dict):
            sources += list(extra.values())

    paths: set[str] = set()
    for entry in sources:
        source = entry.get("source") if isinstance(entry, dict) else None
        if not isinstance(source, dict) or not isinstance(source.get("path"), str):
            continue
        path = Path(source["path"]).expanduser()
        match source.get("source"):
            case "directory":
                pass
            case "file":
                path = path.parent
            case _:
                continue
        path = path.resolve()
        if not path.exists():
            log.warning(f"Plugin marketplace path {path} not found -- skipping")
            continue
        paths.add(to_host_path(str(path), cfg))

    args: list[str] = []
    for host_path in sorted(paths):
        args += ["-v", f"{host_path}:{host_path}:ro"]
    return args
