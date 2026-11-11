"""Ticket container lifecycle (cld v2): the logic behind the root-level verbs.

One function per verb, called from thin ``cld/cli.py`` commands -- the
"logic module + thin CLI front-end" pattern of ``cld/task_agent.py``
(docs/design-ticket-containers.md section 1). The launch manifest stamped
into container labels at ``docker run`` is the single source of truth here:
``restart`` relaunches from it (fixing v1's parameter-dropping restart),
``status`` renders it, ``shutdown`` walks it to forget each repo's bookmark
and workspace. Commits always survive teardown in the repo stores.
"""

import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import typer

from cld.agent_runtime import format_age
from cld.config import Config
from cld.docker import (
    _docker_status,
    base_extra_paths,
    build_ticket_container_args,
    container_has_store_mounts,
    devcontainer_extra_paths,
    ensure_image,
    prepare_store_mounts,
    stage_home_ro,
    stage_ssh_agent,
    ticket_anchor_resolver,
)
from cld.log import get_logger, log_subprocess
from cld.manifest import (
    ManifestDiff,
    RepoManifestEntry,
    TicketManifest,
    diff_manifests,
    keep_attached_anchors,
    read_manifest,
    resolve_manifest,
)
from cld.names import ticket_id, ticket_slug
from cld.registry import pick_repos
from cld.vcs import get_backend
from cld.vcs.anchor import find_anchor_commit, move_anchor_commit, revert_ops
from cld.vcs.jj import check_jj_ids

log = get_logger(__name__)

_READY_SENTINEL = "/tmp/cld-ticket-ready"
_SESSION_LOCK = "/tmp/cld-session.lock"
_LOG_ECHO_INTERVAL = 10


# --- Readiness ------------------------------------------------------------------


def ready_timeout(n_repos: int) -> int:
    """Boot budget scaled by repo count (design section 4.4): each repo adds a
    workspace add + snapshot + optional poetry install, and v1's flat 60 s cut
    it close even for one bootstrap-enabled repo."""
    return 60 + 45 * n_repos


def _echo_new_log_lines(container: str, seen: int) -> int:
    """Print the container's boot-log lines past *seen*; return the new count."""
    result = subprocess.run(
        ["docker", "logs", container], capture_output=True, text=True,
    )
    if result.returncode != 0:
        return seen
    lines = ((result.stdout or "") + (result.stderr or "")).splitlines()
    for line in lines[seen:]:
        typer.echo(f"  [boot] {line}")
    return len(lines)


def wait_ticket_ready(container: str, n_repos: int, timeout: int = 0) -> bool:
    """Poll the boot sentinel until *timeout* (default: scaled by repo count),
    echoing new boot-log lines every 10 s so an N-repo boot is visibly
    progressing instead of v1's silence (spec gap 5). False on timeout."""
    deadline = time.time() + (timeout or ready_timeout(n_repos))
    last_echo = time.time()
    seen = 0
    while time.time() < deadline:
        probe = subprocess.run(
            ["docker", "exec", container, "test", "-f", _READY_SENTINEL],
            capture_output=True,
        )
        if probe.returncode == 0:
            return True
        if time.time() - last_echo >= _LOG_ECHO_INTERVAL:
            seen = _echo_new_log_lines(container, seen)
            last_echo = time.time()
        time.sleep(1)
    return False


# --- Launch ---------------------------------------------------------------------


def _launch(cfg: Config, manifest: TicketManifest) -> None:
    """``docker run -d`` from a resolved manifest, then the scaled readiness wait.

    Anchors ride in the manifest (already pinned to change ids); the same host-side
    extras every interactive kind gets -- devcontainer home mounts, the
    forwarded ssh-agent -- ride along, recomputed per launch.
    """
    container = ticket_id(manifest.ticket)
    cld_root = Path(__file__).resolve().parent.parent
    ensure_image(
        cfg.devcontainer_image,
        cld_root / "imgs/claude-devcontainer/Dockerfile.claude-devcontainer",
        cld_root,
        extra_paths=devcontainer_extra_paths(cld_root),
        parent_image=(
            cfg.base_image,
            cld_root / "imgs/claude-base/Dockerfile.claude-base",
            cld_root,
            base_extra_paths(cld_root),
        ),
    )

    args = build_ticket_container_args(manifest, cfg)
    skipped = []
    repo_paths = [Path(repo.path) for repo in manifest.repos]
    for rel in cfg.home_mounts_devcontainer:
        if mnt := stage_home_ro(rel, cfg, repo_paths):
            args += mnt
        else:
            skipped.append(rel)
    if skipped:
        log.warning("Optional host paths not found (skipped): %s", ", ".join(skipped))
    args += stage_ssh_agent(cfg)
    args += [cfg.devcontainer_image]

    log.info(
        "Starting ticket container %s (detached, %d repo(s))...",
        container, len(manifest.repos),
    )
    subprocess.run(["docker", "run", "-d"] + args, check=True)

    if not wait_ticket_ready(container, len(manifest.repos)):
        _print_logs(container, 40)
        raise RuntimeError(
            f"ticket container '{container}' did not become ready within "
            f"{ready_timeout(len(manifest.repos))} s (boot log above; "
            f"`cld logs {manifest.ticket}` has the rest)"
        )


def _resolve_launch_manifest(
    cfg: Config, slug: str, specs: list[str], shared: list[str],
) -> TicketManifest:
    """Args + registry -> manifest, via the interactive picker on a TTY when no
    repos are given. Non-TTY with no repos is a hard error, not a hang (spec
    section 5). Anchors resolve through the overlap-checking ticket resolver."""
    if not specs:
        if not sys.stdin.isatty():
            raise RuntimeError(
                f"no repos given: pass them positionally (`cld start {slug} "
                "<repo[@rev]...>`) -- the interactive registry picker needs a TTY"
            )
        specs = [f"{name}@{revision}" for name, _entry, revision in pick_repos(cfg.repos)]
    return resolve_manifest(
        slug, specs, cfg.repos, shared=shared, resolver=ticket_anchor_resolver(cfg),
    )


def _start_banner(manifest: TicketManifest) -> None:
    typer.echo(f"Ticket '{manifest.ticket}' ready ({ticket_id(manifest.ticket)}).")
    for repo in manifest.repos:
        typer.echo(
            f"  {repo.name}: {repo.anchor_base[:12]} "
            f"({repo.anchor_mode}, rev from {repo.rev_source})"
        )
    typer.echo(f"  Attach:  cld claude {manifest.ticket}")
    typer.echo(f"  Status:  cld status {manifest.ticket}")


def _entry_change(old, new) -> str:
    parts = []
    if old.path != new.path:
        parts.append(f"path {old.path} -> {new.path}")
    if old.anchor_base != new.anchor_base:
        # A kept repo reattaches at its existing bookmark; the new anchor only
        # takes effect after `cld shutdown` + `cld start`, so the recreated
        # manifest keeps the old one (keep_attached_anchors).
        parts.append(
            f"anchor {old.anchor_base[:12]} -> {new.anchor_base[:12]} "
            "(takes effect only after shutdown + start; until then the "
            f"manifest keeps {old.anchor_base[:12]} and the workspace "
            "reattaches at the existing bookmark; to move it now instead: "
            f"`cld restart <ticket> --reanchor {new.name}@<rev>`)"
        )
    if old.anchor_mode != new.anchor_mode:
        parts.append(
            f"mode {old.anchor_mode} -> {new.anchor_mode} "
            "(takes effect only after shutdown + start)"
        )
    return ", ".join(parts)


def _is_git(path: str) -> bool:
    """True when the repo at *path* is driven through git (no jj backend); a path
    with no repo reads as jj, the default wording."""
    try:
        return get_backend(Path(path)).name == "git"
    except RuntimeError:
        return False


def _print_manifest_diff(diff: ManifestDiff) -> None:
    for repo in diff.added:
        typer.echo(f"  + {repo.name}  {repo.path}  {repo.anchor_base[:12]} ({repo.anchor_mode})")
    for repo in diff.removed:
        detail = (
            "torn down: container unmounted, its session branch and worktree stay in the repo; commits survive"
            if _is_git(repo.path)
            else "torn down: bookmark and workspace forgotten; commits survive"
        )
        typer.echo(f"  - {repo.name}  {repo.path}  ({detail})")
    for old, new in diff.changed:
        typer.echo(f"  ~ {new.name}  {_entry_change(old, new)}")


def _warm_start(container: str, slug: str) -> None:
    """Restart a stopped ticket container in place: workspaces are reused, the
    entrypoint's warm-restart branch reattaches each repo (spec section 6)."""
    manifest = read_manifest(container)
    if not container_has_store_mounts(container):
        raise RuntimeError(
            f"'{container}' mounts the full host checkout; run `cld restart {slug}`"
        )
    subprocess.run(["docker", "start", container], check=True)
    if not wait_ticket_ready(container, len(manifest.repos)):
        _print_logs(container, 40)
        raise RuntimeError(
            f"ticket container '{container}' did not become ready within "
            f"{ready_timeout(len(manifest.repos))} s after a warm start"
        )
    typer.echo(f"Ticket '{slug}' started (warm -- workspaces reused in place).")


def start_ticket(cfg: Config, ticket: str, specs: list[str], shared: list[str]) -> None:
    """Create-or-start (spec section 5).

    Absent: resolve the repo set (positional specs, or the registry picker on
    a TTY) and launch. Stopped: warm start. Running: report. An existing
    ticket given an explicitly different repo set is diffed, confirmed and
    recreated -- repos leaving the set are torn down as in shutdown, kept
    repos reattach at their bookmarks (design section 2.3).
    """
    slug = ticket_slug(ticket)
    container = ticket_id(slug)
    status = _docker_status(container)

    if status == "absent":
        manifest = _resolve_launch_manifest(cfg, slug, specs, shared)
        _launch(cfg, manifest)
        _start_banner(manifest)
        return

    if specs:
        old = read_manifest(container)
        new = _resolve_launch_manifest(cfg, slug, specs, shared)
        diff = diff_manifests(old, new)
        if diff:
            typer.echo(f"Ticket '{slug}' exists with a different repo set:")
            _print_manifest_diff(diff)
            if not typer.confirm("Recreate the ticket container with the new set?"):
                raise typer.Abort()
            _stop_and_remove(container)
            for repo in diff.removed:
                forget_session_state(repo.path, container)
            # Kept repos reattach at their bookmarks, so the launched manifest
            # keeps their old anchors -- the newly resolved ones only become
            # real after shutdown + start (see keep_attached_anchors).
            kept = keep_attached_anchors(old, new)
            _launch(cfg, kept)
            _start_banner(kept)
            return
        # Same resolved set -- fall through to plain create-or-start semantics.

    if status == "stopped":
        _warm_start(container, slug)
        return
    if not container_has_store_mounts(container):
        log.warning(
            "'%s' mounts the full host checkout; run `cld restart %s`", container, slug,
        )
    typer.echo(f"Ticket '{slug}' is already running. Attach: cld claude {slug}")


# --- Exec verbs -----------------------------------------------------------------


def _require_running(ticket: str) -> tuple[str, str]:
    """Return ``(slug, container)`` of a *running* ticket, or raise with the fix."""
    slug = ticket_slug(ticket)
    container = ticket_id(slug)
    status = _docker_status(container)
    if status != "running":
        hint = (
            f"`cld start {slug}` warm-starts it" if status == "stopped"
            else f"`cld start {slug} <repo[@rev]...>` creates it"
        )
        raise RuntimeError(f"ticket container '{container}' is {status}; {hint}")
    return slug, container


def exec_claude(ticket: str, extra_args: list[str]) -> None:
    """Exec the harness at the ticket root (spec section 7).

    ``/tmp/bin/claude`` is the in-container wrapper: it owns
    ``--dangerously-skip-permissions --add-dir /opt/cld``, one ``--add-dir``
    per repo workspace (repo skills) and the
    single-session flock, whose refusal prints straight onto this tty because
    the exec is interactive -- no host-side probe needed (design section 4.5).
    Everything after ``--`` on the command line arrives here as *extra_args*
    and passes through to claude untouched (``--model``, ``--continue``, ...).
    """
    slug, container = _require_running(ticket)
    os.execvp("docker", [
        "docker", "exec", "-it", "-w", f"/workspace/{slug}", container,
        "/tmp/bin/claude", *extra_args,
    ])


def exec_shell(ticket: str) -> None:
    """Interactive bash at the ticket root -- the escape hatch for debugging
    the sandbox itself (spec section 5). Login shell, so /tmp/bin is on PATH.

    Sources the per-repo ``*_ANCHOR`` env file before handing off to the login
    shell: a login shell doesn't read it on its own (no fixed dotfile the
    entrypoint can rely on across images), and `docker exec` never inherits
    env the entrypoint exported at its own runtime.
    """
    slug, container = _require_running(ticket)
    os.execvp("docker", [
        "docker", "exec", "-it", "-w", f"/workspace/{slug}", container,
        "/bin/bash", "-c",
        '[ -f /tmp/cld-ticket-anchors.sh ] && . /tmp/cld-ticket-anchors.sh; exec /bin/bash -l',
    ])


# --- Stop / restart / shutdown ----------------------------------------------------


def _stop_and_remove(container: str) -> None:
    """Plain stop + rm, idempotent. The ticket entrypoint's TERM trap tears
    nothing down (stop is the pause verb; all forgetting is host-side), so this
    needs no restart-vs-shutdown signal split."""
    log.info("Stopping container: %s", container)
    subprocess.run(["docker", "stop", container], capture_output=True)
    subprocess.run(["docker", "rm", container], capture_output=True)


def stop_ticket(ticket: str) -> None:
    """Pause: ``docker stop``. Workspaces stay in place for a warm start."""
    slug = ticket_slug(ticket)
    container = ticket_id(slug)
    status = _docker_status(container)
    if status == "absent":
        raise RuntimeError(f"no ticket container '{container}'")
    if status == "stopped":
        typer.echo(f"Ticket '{slug}' is already stopped.")
        return
    subprocess.run(["docker", "stop", container], check=True)
    typer.echo(
        f"Ticket '{slug}' stopped (paused -- workspaces stay in place; "
        f"`cld start {slug}` warm-starts it)."
    )


def _parse_reanchor(manifest: TicketManifest, specs: list[str]) -> dict[str, str]:
    """``REPO@REV`` parsing for ``--reanchor`` (design section 4).

    A spec splits at the first ``@``; if the part before it names a manifest
    repo, the rest is the revision. A bare repo name, or ``REPO@``, defaults
    the revision to ``@`` -- the repo's working copy on the host. Otherwise,
    on a one-repo ticket, the whole spec is the revision (so ``main@origin``
    works without a repo prefix). Unlike ``_split_rev`` (cld/manifest.py)
    this never touches the filesystem.
    """
    names = {repo.name for repo in manifest.repos}
    result: dict[str, str] = {}
    for spec in specs:
        name, sep, rev = spec.partition("@")
        if spec in names:
            rev = "@"
        elif sep and name in names:
            rev = rev or "@"
        else:
            if len(manifest.repos) != 1:
                raise RuntimeError(
                    f"'{spec}': not REPO@REV against a known repo, and the ticket "
                    f"has {len(manifest.repos)} repos -- known: {', '.join(sorted(names))}"
                )
            name, rev = manifest.repos[0].name, spec
        if not rev:
            raise RuntimeError(f"'{spec}': empty revision after '@'")
        if name in result:
            raise RuntimeError(f"'{name}': named twice in --reanchor")
        result[name] = rev
    return result


def _reanchor_restart(
    cfg: Config, container: str, manifest: TicketManifest, reanchor: list[str],
) -> None:
    """The ``--reanchor`` path of ``restart_ticket`` (design section 5).

    Validates everything -- parsing, the live-session refusal, jj-only, S
    found, T resolved -- before touching the container, so a rejected request
    stops nothing. Then stops (not removes) a running container, moves each
    repo's anchor commit, and only removes + relaunches once every move has
    succeeded, so a stop without a successful move still has the old
    container there to warm-start back into.
    """
    slug = manifest.ticket
    targets = _parse_reanchor(manifest, reanchor)
    if _docker_status(container) == "running" and _session_state(container) == "live":
        raise RuntimeError(
            f"a live claude session is running in '{container}'; end it before reanchoring"
        )
    by_name = {repo.name: repo for repo in manifest.repos}
    resolver = ticket_anchor_resolver(cfg, exclude_session=container)
    plan = []  # (name, backend, s_hash, target)
    for name, revision in targets.items():
        repo = by_name[name]
        backend = get_backend(Path(repo.path))
        if backend.name != "jj":
            raise RuntimeError(f"'{name}': --reanchor needs jj, this repo is '{backend.name}'")
        s_hash = find_anchor_commit(backend, container)
        target = resolver(repo.path, revision, repo.anchor_mode)
        plan.append((name, backend, s_hash, target))

    was_running = _docker_status(container) == "running"
    if was_running:
        subprocess.run(["docker", "stop", container], check=True)

    done: list[tuple[object, list[str]]] = []
    moved: dict[str, tuple[str, str]] = {}
    try:
        for name, backend, s_hash, target in plan:
            repo = by_name[name]
            ops = move_anchor_commit(backend, s_hash, target, repo.anchor_mode, container)
            done.append((backend, ops))
            moved[name] = (repo.anchor_base, target)
    except RuntimeError as e:
        for backend, ops in reversed(done):
            revert_ops(backend, ops)
        if was_running:
            _warm_start(container, slug)
        raise RuntimeError(f"reanchor failed, rolled back: {e}") from e

    def _moved_entry(repo: RepoManifestEntry) -> RepoManifestEntry:
        if repo.name not in moved:
            return repo
        return replace(repo, anchor_base=moved[repo.name][1], rev_source="arg")

    new_manifest = replace(manifest, repos=tuple(_moved_entry(r) for r in manifest.repos))
    subprocess.run(["docker", "rm", container], capture_output=True)
    _launch(cfg, new_manifest)
    for name, (old_base, new_base) in moved.items():
        typer.echo(
            f"  {name}: anchor {old_base[:12]} -> {new_base[:12]} "
            f"(undo: cld restart {slug} --reanchor {name}@{old_base})"
        )
    typer.echo(f"Ticket '{slug}' recreated with {len(moved)} repo anchor(s) moved.")


def restart_ticket(cfg: Config, ticket: str, reanchor: tuple[str, ...] = ()) -> None:
    """Recreate the container from its persisted manifest (spec section 5).

    Labels die with ``docker rm``, so the manifest is read back first -- this
    read-before-remove is what fixes v1's parameter-dropping restart. Anchors
    are not re-resolved: ``anchor_base`` is already a pinned change id, and each repo's
    workspace reattaches at its bookmark anyway (design section 2.3) --
    unless *reanchor* names repos to move first (design-anchor-move section 5).
    """
    slug = ticket_slug(ticket)
    container = ticket_id(slug)
    if _docker_status(container) == "absent":
        raise RuntimeError(
            f"no ticket container '{container}' to restart; `cld start {slug}` creates one"
        )
    manifest = read_manifest(container)
    # Before the container is stopped or removed, and before any host jj call.
    for repo in manifest.repos:
        prepare_store_mounts(Path(repo.path), repo.name)
    if reanchor:
        _reanchor_restart(cfg, container, manifest, list(reanchor))
        return
    _stop_and_remove(container)
    _launch(cfg, manifest)
    ref = " or ".join(sorted({"branches" if _is_git(repo.path) else "bookmarks" for repo in manifest.repos}))
    typer.echo(
        f"Ticket '{slug}' recreated from its manifest "
        f"({len(manifest.repos)} repo(s) reattached at their {ref})."
    )


def _teardown(container: str) -> None:
    """Stop, remove, and forget the ticket's bookmark + workspace in every
    mounted repo (per the manifest). Commits survive in each repo's store."""
    manifest = None
    try:
        manifest = read_manifest(container)
    except (RuntimeError, ValueError) as e:
        log.warning(
            "cannot read the manifest of '%s': %s. The container is removed "
            "anyway; forget its per-repo state manually with "
            "`jj bookmark forget %s && jj workspace forget %s` in each mounted jj repo "
            "(in a git repo the session branch and worktree registration are not forgotten "
            "by shutdown: `git worktree list`, then `git branch -D %s`).",
            container, e, container, container, container,
        )
    _stop_and_remove(container)
    for repo in manifest.repos if manifest else ():
        forget_session_state(repo.path, container)
    typer.echo(
        f"Shut down ticket container: {container} (commits survive in every repo store)"
    )
    for repo in manifest.repos if manifest else ():
        if _is_git(repo.path):
            typer.echo(
                f"  {repo.name} (git): branch {container} and its worktree registration stay in the repo"
            )


def shutdown_ticket(ticket: str) -> None:
    """End of ticket: teardown plus per-repo bookmark/workspace forget."""
    container = ticket_id(ticket)
    if _docker_status(container) == "absent":
        typer.echo(f"No ticket container '{container}'.")
        return
    _teardown(container)


def shutdown_all_tickets() -> None:
    """Tear down every ticket container on this host, running or stopped."""
    tickets = list_tickets()
    if not tickets:
        typer.echo("No ticket containers found.")
        return
    for name, _state in sorted(tickets):
        _teardown(name)


def forget_session_state(repo_root_str: str, session: str) -> None:
    """Drop the session's bookmark and workspace registration from a repo's jj store.

    Shared by the v1 agent/task-agent teardown and per-repo ticket
    teardown. Best-effort: both entries are independent (bookmark = named
    commit pointer, workspace = registered working-copy path). A workspace
    left behind makes the next first-launch `jj workspace add --name <session>`
    fail with "Workspace named X already exists", leaving the workspace empty.
    """
    repo_root = Path(repo_root_str)
    if not repo_root.is_dir():
        log.warning(
            "Cannot clean up session state for %s: repo_root %s no longer exists. "
            "Recover manually with: cd <repo> && jj bookmark forget %s && jj workspace forget %s "
            "(a git repo: `git worktree list`, then `git branch -D %s`)",
            session, repo_root, session, session, session,
        )
        return
    try:
        backend = get_backend(repo_root)
    except RuntimeError as e:
        log.warning(
            "Cannot clean up session state for %s in %s: %s. "
            "Recover manually with: cd %s && jj bookmark forget %s && jj workspace forget %s "
            "(a git repo: `git worktree list`, then `git branch -D %s`)",
            session, repo_root, e, repo_root, session, session, session,
        )
        return
    if backend.name != "jj":
        return
    if reason := check_jj_ids(backend.repo_root):
        log.warning(
            "Not cleaning up session state for %s in %s: %s, so no host jj runs there. "
            "Fix the repo, then: cd %s && jj bookmark forget %s && jj workspace forget %s",
            session, repo_root, reason, repo_root, session, session,
        )
        return
    for cmd in (["bookmark", "forget", session], ["workspace", "forget", session]):
        result = backend.run(cmd)
        if result.returncode != 0:
            log.warning(
                "jj %s failed (rc=%d): %s. "
                "The next launch may reattach to stale state; "
                "recover with: cd %s && jj %s",
                " ".join(cmd), result.returncode, result.stderr.strip(),
                repo_root, " ".join(cmd),
            )


# --- Status / logs ----------------------------------------------------------------


def list_tickets() -> list[tuple[str, str]]:
    """``(container name, docker state)`` of every ticket container, running or
    stopped. A docker failure reads as no tickets."""
    cmd = ["docker", "ps", "-a", "--filter", "label=org.cld.kind=ticket",
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


def _session_state(container: str) -> str:
    """'live' while a harness session holds the in-container session flock.

    Probes the same lock the claude wrapper takes (design section 4.5) with a
    non-blocking flock, which neither takes nor disturbs a held lock. rc 0
    means the lock was free, rc 1 means held; anything else is an exec failure.
    """
    result = subprocess.run(
        ["docker", "exec", container, "flock", "-n", _SESSION_LOCK, "true"],
        capture_output=True,
    )
    if result.returncode == 0:
        return "none"
    return "live" if result.returncode == 1 else "?"


def _uptime(container: str) -> str:
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.StartedAt}}", container],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return ""
    return format_age(result.stdout.strip())


def _bookmark_tip(repo_path: str, bookmark: str) -> str:
    """Current commit of the ticket's bookmark (jj) or session branch (git) in
    *repo_path*'s store; '-' where it cannot be read (forgotten bookmark or
    branch, missing repo)."""
    path = Path(repo_path)
    if not path.is_dir():
        return "-"
    try:
        backend = get_backend(path)
    except RuntimeError:
        return "-"
    if backend.name != "jj":
        result = backend.run(["rev-parse", "--short=12", f"refs/heads/{bookmark}"])
        tip = (result.stdout or "").strip()
        return tip if result.returncode == 0 and tip else "-"
    if reason := check_jj_ids(backend.repo_root):
        log.warning("No bookmark lookup in %s: %s", backend.repo_root, reason)
        return "-"
    result = backend.run([
        "log", "--ignore-working-copy", "--no-graph",
        "-r", bookmark, "-T", "commit_id.short(12)",
    ])
    tip = (result.stdout or "").strip()
    return tip if result.returncode == 0 and tip else "-"


def _print_table(rows: list[tuple], indent: str = "") -> None:
    widths = [max(len(str(row[i])) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        line = "  ".join(f"{str(cell):<{w}}" for cell, w in zip(row, widths)).rstrip()
        typer.echo(f"{indent}{line}")


def print_ticket_roster() -> None:
    """All tickets: repos, container state, live-session flag (spec section 5)."""
    tickets = list_tickets()
    if not tickets:
        typer.echo("No ticket containers. `cld start <ticket>` creates one.")
        return
    rows: list[tuple] = [("TICKET", "STATE", "SESSION", "REPOS")]
    for name, state in sorted(tickets):
        try:
            manifest = read_manifest(name)
            slug = manifest.ticket
            repos = ", ".join(r.name for r in manifest.repos)
        except (RuntimeError, ValueError) as e:
            slug = name.removeprefix("t.")
            repos = f"<unreadable manifest: {e}>"
        session = _session_state(name) if state == "running" else "-"
        rows.append((slug, state, session, repos))
    _print_table(rows)


def print_ticket_detail(ticket: str) -> None:
    """One ticket: per-repo anchors, modes, bookmark tips, session, uptime."""
    slug = ticket_slug(ticket)
    container = ticket_id(slug)
    status = _docker_status(container)
    if status == "absent":
        raise RuntimeError(
            f"no ticket container '{container}'; `cld start {slug}` creates one"
        )
    manifest = read_manifest(container)
    typer.echo(f"Ticket: {manifest.ticket} ({container})")
    if status == "running":
        typer.echo(f"  Container: running (started {_uptime(container)})")
        typer.echo(f"  Session:   {_session_state(container)}")
    else:
        typer.echo(f"  Container: {status}")
        typer.echo("  Session:   -")
    typer.echo("  Repos:")
    rows: list[tuple] = [("NAME", "ANCHOR", "MODE", "REV_SOURCE", "BOOKMARK_TIP", "PATH")]
    for repo in manifest.repos:
        rows.append((
            repo.name, repo.anchor_base[:12], repo.anchor_mode, repo.rev_source,
            _bookmark_tip(repo.path, container), repo.path,
        ))
    _print_table(rows, indent="    ")


def _print_logs(container: str, tail: int) -> None:
    result = subprocess.run(
        ["docker", "logs", "--tail", str(tail), container],
        capture_output=True, text=True,
    )
    if result.stdout:
        typer.echo(result.stdout, nl=False)
    if result.stderr:
        typer.echo(result.stderr, nl=False, err=True)


def print_ticket_logs(ticket: str, tail: int) -> None:
    """Entrypoint/boot logs of the ticket container (spec section 5)."""
    container = ticket_id(ticket)
    if _docker_status(container) == "absent":
        raise RuntimeError(f"no ticket container '{container}'")
    _print_logs(container, tail)
