"""CLI entry point for cld."""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import typer

from cld.chain import ParallelGroup, apply_name_override, chain_state_dir, load_chain, print_chain_report, run_chain, validate_chain
from cld.chain_state import ChainState, StateWriter, write_state, _utcnow_iso
from cld.config import Config, _user_config_path
from cld.docker import (
    TaskAgentSpec,
    allocate_task_agent_name,
    anchor_env_args,
    assert_task_agent_capacity,
    base_extra_paths,
    build_container_args,
    devcontainer_extra_paths,
    docker_task_agent_list,
    docker_task_agent_status,
    ensure_image,
    find_repo_root,
    find_target_repo,
    prepare_store_mounts,
    require_docker,
    resolve_anchor_checked,
    run_extra_paths,
    stage_home_ro,
    stage_ssh_agent,
    to_host_path,
)
from cld.agent_runtime import format_age
from cld.cli_msg import handle_errors as _handle_errors, msg_app
from cld.messenger import mailbox
from cld.registry import add_repo, remove_repo, ticket_repo_mounts, tickets_referencing
from cld.run import launch_run
from cld.log import get_logger, setup_logging
from cld.prompts import compose_brief, list_prompt_items, resolve_prompt_args
from cld.ticket import (
    exec_claude,
    exec_shell,
    forget_session_state as _forget_session_state,
    print_ticket_detail,
    print_ticket_logs,
    print_ticket_roster,
    restart_ticket,
    shutdown_all_tickets,
    shutdown_ticket,
    start_ticket,
    stop_ticket,
)
from cld.task_agent import (
    format_peers,
    known_task_agent_names,
    mailbox_root,
    parse_peer_specs,
    print_task_agent_detail,
    print_task_agent_roster,
    print_task_agent_transcript,
    resolve_task_agent,
    task_agent_parent,
    task_agent_record,
    task_agent_rows,
)
from cld.vcs import get_backend
from cld.vcs.anchor import resolve_anchor

log = get_logger(__name__)

app = typer.Typer(no_args_is_help=True)

_SHARED_ANCHOR_HELP = (
    "Anchor directly on -r/--revision instead of a fresh isolated child: the "
    "container can then touch any pre-existing descendant of the anchor, not "
    "just its own. Refused if another live container is already anchored "
    "somewhere in that tree. Default is isolated -- use this only for "
    "explicit, human-approved cleanup/consolidation work."
)


def _anchor_mode(shared_anchor: bool) -> str:
    return "shared" if shared_anchor else "isolated"


def _reject_in_container() -> None:
    """Refuse `python3 -m cld` inside a container: this app needs a docker daemon.

    The container surface is its own app (cld/cli_container.py), installed as `cld`
    in the devcontainer image -- see docs/design-cli-split.md.

    ``HUB_MODE`` is still checked although no launcher sets it since the master
    role went away: the same env is what ``in_master_container()`` keys the
    broker dispatch on, and both flip together in that rework.
    """
    if os.environ.get("HUB_MODE") or os.environ.get("AGENT_MODE"):
        typer.echo(
            "Error: this is the host cld, which needs a docker daemon. Inside a "
            "container run `cld` instead (task-agent, msg, repos, prompts).",
            err=True,
        )
        raise typer.Exit(1)


def _version_callback(value: bool):
    if value:
        from cld import __version__
        typer.echo(f"cld {__version__}")
        raise typer.Exit()


@app.callback()
@_handle_errors
def main(
    version: bool = typer.Option(False, "--version", callback=_version_callback, is_eager=True, help="Show version and exit"),
):
    """Run Claude Code in Docker containers with VCS workspace isolation.

    The root group takes no positionals: click resolves a group callback's first
    positional as a subcommand name, so a stray one is always a usage error.
    Prompt refs live on the real commands -- `cld run`, `cld task-agent start`,
    `cld chain run`.
    """
    _reject_in_container()


@app.command()
@_handle_errors
def run(
    refs: Optional[list[str]] = typer.Argument(None, help="Prompt refs in order: @<ref> from prompts/, or a file path"),
    name: str = typer.Option("", "-n", "--name", help="Session name suffix"),
    model: str = typer.Option("", "-m", "--model", help="Claude model (e.g. opus, sonnet)"),
    revision: str = typer.Option("", "-r", "--revision", help="Anchor revision (default: current change -- @ for jj, HEAD for git)"),
    prompt: str = typer.Option("", "-p", "--prompt", help="Inline task description, appended after the refs"),
    shared_anchor: bool = typer.Option(False, "--shared-anchor", help=_SHARED_ANCHOR_HELP),
):
    """Launch a one-shot autonomous Claude agent (headless, --rm, commits to a branch)."""
    cfg = Config.from_env()
    setup_logging(cfg)
    cld_root = Path(__file__).resolve().parent.parent
    brief, _ = _compose_from_args(refs or [], prompt, find_repo_root(), cld_root)

    log.info(
        "run: name=%s, model=%s, revision=%s, refs=%s, prompt=%s, shared_anchor=%s",
        name or "<auto>",
        model or "<default>",
        revision or "<default>",
        ", ".join(refs or []) or "<none>",
        "<provided>" if prompt else "<none>",
        shared_anchor,
    )

    launch_run(cfg, brief, name=name, model=model, revision=revision, shared_anchor=shared_anchor)


def _compose_from_args(
    refs: list[str], prompt: str, repo_root: Path, cld_root: Path
) -> tuple[str, list[tuple[Path, str]]]:
    """Resolve refs and compose the brief, turning input errors into clean exits.

    Returns the brief and the resolved `(path, kind)` refs, so a caller that needs the
    kinds (the persona a task-agent displays) does not resolve the same list twice.
    """
    try:
        resolved = resolve_prompt_args(refs, repo_root, cld_root)
    except (FileNotFoundError, ValueError) as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    for path, kind in resolved:
        log.info("prompt %s: %s", kind, path)
    if not resolved and not prompt:
        typer.echo("Error: Provide at least one prompt ref, --prompt, or both", err=True)
        raise typer.Exit(1)
    return compose_brief([p for p, _ in resolved], prompt), resolved


def _wait_for_container_ready(name: str, sentinel: str, timeout: int = 60) -> bool:
    """Poll container until *sentinel* file exists inside it. Returns False on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = subprocess.run(
            ["docker", "exec", name, "test", "-f", sentinel],
            capture_output=True,
        )
        if result.returncode == 0:
            return True
        time.sleep(1)
    return False


_READY_SENTINEL = "/tmp/cld-agent-ready"


def _docker_logs(name: str, tail: int) -> None:
    """Print the tail of a container's log. Supervisor output arrives on stderr."""
    result = subprocess.run(
        ["docker", "logs", "--tail", str(tail), name],
        capture_output=True, text=True,
    )
    if result.stdout:
        typer.echo(result.stdout, nl=False)
    if result.stderr:
        typer.echo(result.stderr, nl=False, err=True)


def _stop_and_remove_container(name: str) -> None:
    """Stop and remove a container. Idempotent.

    A plain `docker stop` (SIGTERM): the supervisor's own exit handler forgets
    the session bookmark, and the reap path forgets it again caller-side
    (`_reap_task_agent`) because that supervisor is usually SIGKILLed mid-turn
    and never gets to run.
    """
    log.info("Stopping container: %s", name)
    subprocess.run(["docker", "stop", name], capture_output=True)
    subprocess.run(["docker", "rm", name], capture_output=True)


# --- Task-scoped agents (headless, many per repo, master-owned lifecycle) -----
task_agent_app = typer.Typer(
    help="Task-scoped headless agents: one per task, bounded lifespan (see docs/design-task-agents.md).",
)
app.add_typer(task_agent_app, name="task-agent")

# Reap-readiness check 1 waits this long for an in-flight turn to finish before
# refusing (docs/design-task-agents.md §7 "waits briefly"). Matches `docker stop`'s
# grace period: enough for a turn that is about to end, not enough to block on one
# that just started.
_REAP_WAIT_SECONDS = 10


@task_agent_app.command("start")
@_handle_errors
def task_agent_start(
    refs: Optional[list[str]] = typer.Argument(None, help="Prompt refs in order: @<ref> from prompts/ (e.g. @personas/implementer), or a file path"),
    name: str = typer.Option("", "-n", "--name", help="Task slug, kebab-case (default: --branch)"),
    prompt: str = typer.Option("", "-p", "--prompt", help="Inline task description, appended after the refs"),
    branch: str = typer.Option("", "--branch", help="Deliverable branch name (default: the task slug)"),
    model: str = typer.Option("", "-m", "--model", help="Claude model (e.g. opus, sonnet)"),
    revision: str = typer.Option("", "-r", "--revision", help="Anchor revision (default: current change)"),
    peer: list[str] = typer.Option([], "--peer", help="A peer this agent may message: <container-name>[:<hops>]. Repeatable."),
    parent: str = typer.Option("", "--parent", hidden=True, help="Owning master session (set by the host broker)"),
    shared_anchor: bool = typer.Option(False, "--shared-anchor", help=_SHARED_ANCHOR_HELP),
):
    """Spawn a task-scoped agent. Every start creates a new container (no start-or-attach)."""
    cfg = Config.from_env()
    setup_logging(cfg)
    require_docker()

    cld_root = Path(__file__).resolve().parent.parent
    repo_root = find_target_repo(cfg)
    # Before any host jj call on the repo (the anchor resolution below).
    prepare_store_mounts(repo_root)
    brief, resolved = _compose_from_args(refs or [], prompt, repo_root, cld_root)
    # Display only: the roster's Persona column and the launch banner. A task-agent with
    # no persona ref is coherent -- the lifecycle preamble is always layered in.
    role = next((p.stem for p, kind in resolved if kind == "persona"), "")
    slug = name or branch
    if not slug:
        typer.echo(
            "Error: a task slug is required -- pass -n/--name <slug> (short, kebab-case, "
            "naming the task) or --branch, which it falls back to",
            err=True,
        )
        raise typer.Exit(1)
    branch = branch or slug
    peers = parse_peer_specs(peer, cfg.peer_absolute_limit)

    # Validates the slug shape, and settles the name before the refusals so every
    # input error surfaces ahead of them. Allocation reserves nothing -- it only
    # probes docker for a free name.
    session = allocate_task_agent_name(repo_root, slug)
    if session in peers:
        raise ValueError(f"--peer {session} names this agent itself")
    unknown = sorted(set(peers) - known_task_agent_names(cfg))
    if unknown:
        # Not a refusal: the master owns the graph, and only an already-spawned
        # agent can be named, so a name this host hasn't seen is usually a typo
        # -- which its first send() would surface anyway (§7).
        log.warning(
            "peer(s) not known to this host: %s. Peers are addressed by full container "
            "name; check `cld task-agent status`.", ", ".join(unknown),
        )

    # Both refusals are host-side, before anything is built or spawned: the cap
    # counts running siblings, the anchor check reads the origin store (§9).
    assert_task_agent_capacity(cfg, parent)
    mode = _anchor_mode(shared_anchor)
    anchor = resolve_anchor_checked(cfg, repo_root, revision, mode, caller_kind="task-agent")

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

    log.info(
        "task-agent: name=%s, task=%s, persona=%s, branch=%s, anchor=%s, mode=%s, peers=%s, parent=%s",
        session, slug, role or "<none>", branch, anchor[:12], mode,
        ",".join(f"{p}:{h}" for p, h in sorted(peers.items())) or "none",
        parent or "<none>",
    )

    args = build_container_args(
        repo_root, session, cfg,
        task_agent=TaskAgentSpec(
            slug=slug, parent_master=parent, deliverable_branch=branch, peers=peers,
        ),
        anchor_hash=anchor, anchor_mode=mode,
    )
    # The already-resolved anchor, not `revision`: the launch must pin exactly the
    # commit the overlap check inspected. The brief rides in the same envelope.
    args += anchor_env_args(cfg, anchor, brief=brief, mode=mode)
    args += ["-e", f"AGENT_PERSONA={role}"]
    if model:
        args += ["-e", f"AGENT_MODEL={model}"]

    skipped = []
    for rel in cfg.home_mounts_devcontainer:
        mnt = stage_home_ro(rel, cfg, [repo_root])
        if mnt:
            args += mnt
        else:
            skipped.append(rel)
    if skipped:
        log.warning("Optional host paths not found (skipped): %s", ", ".join(skipped))

    args += stage_ssh_agent(cfg)
    args += [cfg.devcontainer_image]

    log.info("Starting task-agent (detached)...")
    subprocess.run(["docker", "run", "-d"] + args, check=True)

    # The handle every read verb takes, which is not always the slug: a live-name
    # collision appends a suffix, and the hints have to point at *this* agent.
    handle = session.rsplit("_", 1)[-1]

    if not _wait_for_container_ready(session, _READY_SENTINEL):
        typer.echo(
            f"Error: task-agent '{session}' did not become ready within 60 s. "
            f"It is still running -- check: cld task-agent logs {handle}",
            err=True,
        )
        raise typer.Exit(1)

    typer.echo(f"Task-agent '{session}' started for {repo_root}.")
    typer.echo(f"  Task:       {slug}")
    typer.echo(f"  Persona:    {role or '-'}")
    typer.echo(f"  Branch:     {branch}")
    typer.echo(f"  Anchor:     {anchor[:12]} ({mode})")
    typer.echo(f"  Peers:      {format_peers(peers)}")
    typer.echo(f"  Status:     cld task-agent status {handle}")
    typer.echo(f"  Logs:       cld task-agent logs {handle}")
    typer.echo(f"  Transcript: cld task-agent transcript {handle}")
    typer.echo(f"  Send:       messenger MCP send(to=\"{session}\", ...)")


@task_agent_app.command("status")
@_handle_errors
def task_agent_status(
    name: Optional[str] = typer.Argument(None, help="Task slug or container name; omit for the roster"),
    parent: str = typer.Option("", "--parent", hidden=True, help="Scope the roster to this master's fleet (set by the host broker)"),
):
    """Roster of every task-agent on this host, or one agent in detail."""
    cfg = Config.from_env()
    setup_logging(cfg)
    if name:
        print_task_agent_detail(cfg, resolve_task_agent(cfg, name))
        return
    print_task_agent_roster(task_agent_rows(cfg, parent))


@task_agent_app.command("logs")
@_handle_errors
def task_agent_logs(
    name: str = typer.Argument(..., help="Task slug or container name"),
    tail: int = typer.Option(80, "-n", "--tail", help="Number of lines to show"),
):
    """Tail a task-agent's supervisor log (state + cost), NOT its conversation."""
    cfg = Config.from_env()
    setup_logging(cfg)
    require_docker()
    resolved = resolve_task_agent(cfg, name)
    if docker_task_agent_status(resolved) == "absent":
        typer.echo(
            f"Error: container {resolved} is gone, so its log is gone with it. "
            f"The conversation survives: cld task-agent transcript {name}",
            err=True,
        )
        raise typer.Exit(1)
    _docker_logs(resolved, tail)


@task_agent_app.command("transcript")
@_handle_errors
def task_agent_transcript(
    name: str = typer.Argument(..., help="Task slug or container name"),
):
    """Print the mailbox conversation: what the agent received and what it sent.

    Reads the mailbox, so it keeps working after a reap -- the container's log does not.
    """
    cfg = Config.from_env()
    setup_logging(cfg)
    print_task_agent_transcript(cfg, resolve_task_agent(cfg, name))


def _assert_reap_ready(cfg: Config, name: str, *, parent: str) -> None:
    """The three reap-readiness checks (§7). Raises RuntimeError naming the refusal.

    Ordered so the one that *waits* runs last -- no point spending
    ``_REAP_WAIT_SECONDS`` on an in-flight turn only to refuse for another reason.
    All three are filesystem or label reads; there is deliberately **no** store-side
    "did the squash happen" test, because that is §9's verification and the master
    already did it before asking (D2b).
    """
    root = mailbox_root(cfg)

    # 3. Own fleet only. An empty *parent* is the human on the host, who has full
    # authority; only a master-initiated reap (via the broker) passes a value.
    if parent:
        owner = task_agent_parent(cfg, name)
        if owner != parent:
            raise RuntimeError(
                f"refusing to reap {name}: its parent master is {owner or '<none>'}, "
                f"not {parent}. A master reaps only its own fleet."
            )

    # 2. Not a live peer. Reaping mid-exchange silently breaks the dependent's
    # exactly-one-reply guarantee, which the dead supervisor can no longer honor.
    running = {c["name"] for c in docker_task_agent_list(running_only=True)}
    dependents = sorted(
        m["name"] for m in mailbox.list_fleet(root)
        if m["name"] != name and m["name"] in running and name in (m.get("peers") or {})
    )
    if dependents:
        raise RuntimeError(
            f"refusing to reap {name}: it is a live peer of {', '.join(dependents)}. "
            "Reap them first, or wait for the exchange to land."
        )

    # 1. Not mid-turn. The work itself survives (watchman snapshots), but the reply
    # its sender is waiting for does not.
    deadline = time.time() + _REAP_WAIT_SECONDS
    while True:
        state = mailbox.read_state(root, name) or {}
        if state.get("phase") != "processing":
            return
        if time.time() >= deadline:
            current = state.get("current") or {}
            raise RuntimeError(
                f"refusing to reap {name}: it is mid-turn on '{current.get('subject')}' "
                f"from {current.get('from')} (since {current.get('started_at')}). "
                "Wait for it to reply, or override with --force."
            )
        time.sleep(1)


def _task_agent_repo_root(cfg: Config, name: str) -> str:
    """Host path of the repo a task-agent runs against; "" when it can't be determined.

    Normally the container's own label. Once the container is gone there is no label
    left (``state.json``'s ``repo_root`` is the in-container path), so fall back to
    the cwd's repo: forgetting a bookmark that isn't there is a no-op, so a wrong
    guess is harmless, and the mailbox archive happens either way.
    """
    rec = task_agent_record(cfg, name)
    if rec:
        return rec["repo_root"]
    try:
        return str(find_target_repo(cfg))
    except RuntimeError:
        return ""


def _reap_task_agent(cfg: Config, name: str, *, parent: str, force: bool) -> None:
    """Stop, remove, then the caller-side cleanup (D22). Every step idempotent."""
    if not force:
        _assert_reap_ready(cfg, name, parent=parent)
    repo_root = _task_agent_repo_root(cfg, name)
    _stop_and_remove_container(name)
    # The session bookmark, never the deliverable branch: different strings with
    # different lifetimes (D8). Caller-side because the supervisor is normally
    # SIGKILLed mid-turn, so its own last act doesn't run (D22).
    if repo_root:
        _forget_session_state(repo_root, name)
    else:
        log.warning(
            "could not determine the repo for %s, so its session bookmark stays in place. "
            "Recover with: cd <repo> && jj bookmark forget %s && jj workspace forget %s",
            name, name, name,
        )
    mailbox.archive_mailbox(mailbox_root(cfg), name)
    typer.echo(f"Reaped task-agent: {name}")


def _reap_all_task_agents(cfg: Config, *, parent: str, force: bool) -> None:
    """Reap every task-agent, repeating while any pass makes progress.

    Check 2 is order-dependent: with an A->B peer edge, reaping B is refused while A
    lives but allowed once A is gone, so one arbitrary-order pass would strand B. A
    mutual A<->B pair still refuses both -- correctly; that is what --force is for.
    Mailboxes whose container is already gone are swept too, since they are the
    orphans this is meant to clear.
    """
    # Label first, meta.json for names whose container is already gone -- same
    # precedence as task_agent_parent, resolved once for the whole sweep.
    owners = {c["name"]: c["parent"] for c in docker_task_agent_list()}
    for m in mailbox.list_fleet(mailbox_root(cfg)):
        owners.setdefault(m["name"], m.get("parent", ""))
    targets = [n for n, owner in owners.items() if not parent or owner == parent]
    if not targets:
        typer.echo("No task-agents found.")
        return

    remaining = sorted(targets)
    refused: dict[str, str] = {}
    while remaining:
        stuck: list[str] = []
        refused = {}
        for name in remaining:
            try:
                _reap_task_agent(cfg, name, parent=parent, force=force)
            except RuntimeError as e:
                stuck.append(name)
                refused[name] = str(e)
        if len(stuck) == len(remaining):
            break
        remaining = stuck

    for name in remaining:
        log.error("%s", refused[name])
    if remaining:
        typer.echo(
            f"{len(remaining)} task-agent(s) refused; add --force to override.", err=True
        )
        raise typer.Exit(1)


@task_agent_app.command("shutdown")
@_handle_errors
def task_agent_shutdown(
    name: Optional[str] = typer.Argument(None, help="Task slug or container name"),
    all_: bool = typer.Option(False, "--all", help="Reap every task-agent on this host"),
    force: bool = typer.Option(False, "--force", help="Override a reap-readiness refusal (host-only)"),
    parent: str = typer.Option("", "--parent", hidden=True, help="Restrict to this master's fleet (set by the host broker)"),
):
    """Stop and remove a task-agent, forget its session bookmark, archive its mailbox."""
    cfg = Config.from_env()
    setup_logging(cfg)
    if all_ == bool(name):
        typer.echo("Error: pass a task slug/container name, or --all -- not both", err=True)
        raise typer.Exit(1)
    require_docker()
    if name:
        _reap_task_agent(cfg, resolve_task_agent(cfg, name), parent=parent, force=force)
        return
    _reap_all_task_agents(cfg, parent=parent, force=force)


# --- Mailbox messaging (shared with the container CLI) ------------------------
app.add_typer(msg_app, name="msg")


# --- Ticket containers (v2 lifecycle verbs; see docs/design-ticket-containers.md) ---

_TICKET_HELP = "Ticket name (e.g. LIDE-2600); sanitized into the container slug"
_DEFAULT_TICKET_HELP = _TICKET_HELP + "; defaults to the cwd repo's directory name"
# `cld claude <ticket> -- <args>`: everything after `--` lands in ctx.args and
# passes through to claude untouched.
_PASSTHROUGH_ARGS = {"allow_extra_args": True, "ignore_unknown_options": True}


def _default_ticket(cfg: Config, ticket: str | None) -> str:
    """The ticket a bare verb targets: the one bare `cld start` names after the cwd repo."""
    return ticket or find_target_repo(cfg).name


@app.command()
@_handle_errors
def start(
    ticket: Optional[str] = typer.Argument(None, help=_TICKET_HELP),
    repos: Optional[list[str]] = typer.Argument(
        None,
        help="Repos to mount: registry names or paths, each with an optional @rev anchor override",
    ),
    shared_anchor: list[str] = typer.Option(
        [], "--shared-anchor", metavar="REPO",
        help="Mount REPO anchored directly on its revision (shared mode, repeatable). "
        + _SHARED_ANCHOR_HELP,
    ),
):
    """Create or start a ticket container (interactive repo picker on a TTY with no repos).

    Against an existing ticket, an explicitly different repo set shows the
    diff, asks to confirm, and recreates the container; repos leaving the set
    are torn down as in shutdown (commits survive).

    With no arguments at all, targets the cwd's repo directly -- ad hoc,
    regardless of registration in `cld repos` -- under a ticket named after
    the repo directory.
    """
    require_docker()
    cfg = Config.from_env()
    setup_logging(cfg)
    if ticket is None:
        repo_root = find_target_repo(cfg)
        ticket = repo_root.name
        repos = [str(repo_root)]
    start_ticket(cfg, ticket, repos or [], shared_anchor)


@app.command(context_settings=_PASSTHROUGH_ARGS)
@_handle_errors
def claude(
    ctx: typer.Context,
    ticket: Optional[str] = typer.Argument(None, help=_DEFAULT_TICKET_HELP),
):
    """Exec the Claude harness at the ticket root; everything after -- passes to claude."""
    require_docker()
    cfg = Config.from_env()
    setup_logging(cfg)
    extra_args = ctx.args
    if ticket and ticket.startswith("-"):
        # Bare `cld claude -- --continue`: click fills the empty ticket slot
        # with the first passthrough arg.
        ticket, extra_args = None, [ticket, *ctx.args]
    exec_claude(_default_ticket(cfg, ticket), extra_args)


@app.command()
@_handle_errors
def shell(ticket: Optional[str] = typer.Argument(None, help=_DEFAULT_TICKET_HELP)):
    """Interactive bash in the ticket container -- for debugging the sandbox itself."""
    require_docker()
    cfg = Config.from_env()
    setup_logging(cfg)
    exec_shell(_default_ticket(cfg, ticket))


@app.command()
@_handle_errors
def stop(ticket: Optional[str] = typer.Argument(None, help=_DEFAULT_TICKET_HELP)):
    """Pause a ticket container (docker stop); `cld start` warm-starts it."""
    require_docker()
    cfg = Config.from_env()
    setup_logging(cfg)
    stop_ticket(_default_ticket(cfg, ticket))


@app.command()
@_handle_errors
def restart(
    ticket: Optional[str] = typer.Argument(None, help=_DEFAULT_TICKET_HELP),
    reanchor: list[str] = typer.Option(
        [], "--reanchor", metavar="REPO@REV",
        help="Move a repo's anchor commit onto REV before recreating "
        "(repeatable; REV defaults to '@', the host working copy; "
        "bare REV allowed on a one-repo ticket).",
    ),
):
    """Recreate a ticket container from its persisted manifest; workspaces reattach at their bookmarks.

    With --reanchor, moves each named repo's anchor commit first (design
    docs/anchor-move design section 3-5).
    """
    require_docker()
    cfg = Config.from_env()
    setup_logging(cfg)
    restart_ticket(cfg, _default_ticket(cfg, ticket), reanchor)


@app.command()
@_handle_errors
def shutdown(
    ticket: Optional[str] = typer.Argument(None, help=_DEFAULT_TICKET_HELP),
    all_: bool = typer.Option(False, "--all", help="Shut down every ticket container on this host"),
):
    """End a ticket: teardown plus per-repo bookmark/workspace forget (commits survive)."""
    if all_ and ticket:
        typer.echo("Error: pass a ticket, or --all -- not both", err=True)
        raise typer.Exit(1)
    require_docker()
    cfg = Config.from_env()
    setup_logging(cfg)
    if all_:
        shutdown_all_tickets()
    else:
        shutdown_ticket(_default_ticket(cfg, ticket))


@app.command()
@_handle_errors
def status(ticket: Optional[str] = typer.Argument(None, help=_TICKET_HELP)):
    """Ticket roster, or one ticket in detail (anchors, bookmark tips, session)."""
    require_docker()
    setup_logging(Config.from_env())
    if ticket:
        print_ticket_detail(ticket)
    else:
        print_ticket_roster()


@app.command()
@_handle_errors
def logs(
    ticket: Optional[str] = typer.Argument(None, help=_DEFAULT_TICKET_HELP),
    tail: int = typer.Option(80, "-n", "--tail", help="Number of lines to show"),
):
    """Entrypoint/boot logs of a ticket container."""
    require_docker()
    cfg = Config.from_env()
    setup_logging(cfg)
    print_ticket_logs(_default_ticket(cfg, ticket), tail)


# --- Repo registry ------------------------------------------------------------
repos_app = typer.Typer(help="Named repo registry for ticket containers.")
app.add_typer(repos_app, name="repos")


@repos_app.callback(invoke_without_command=True)
@_handle_errors
def repos_list(ctx: typer.Context):
    """List registered repos and which ticket containers mount them."""
    if ctx.invoked_subcommand is not None:
        return
    cfg = Config.from_env()
    setup_logging(cfg)
    if not cfg.repos:
        typer.echo("No repos registered. `cld repos add <name> <path>` registers one.")
        return
    mounts = ticket_repo_mounts()
    rows = []
    for name in sorted(cfg.repos):
        entry = cfg.repos[name]
        host_path = str(Path(entry.path).expanduser())
        tickets = sorted(c for c, m in mounts.items() if m.get(name) == host_path)
        rows.append((
            name, entry.path, entry.default_rev or "trunk()",
            "yes" if entry.bootstrap else "", ", ".join(tickets),
        ))
    headers = ("NAME", "PATH", "DEFAULT_REV", "BOOTSTRAP", "TICKETS")
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    for row in (headers, *rows):
        typer.echo("  ".join(f"{cell:<{w}}" for cell, w in zip(row, widths)).rstrip())


@repos_app.command("add")
@_handle_errors
def repos_add(
    name: str = typer.Argument(..., help="Registry name; becomes the ticket subdir name"),
    path: str = typer.Argument(..., help="Host path of the repo"),
    default_rev: str = typer.Option("", "--default-rev", help="Anchor revision offered at launch (default: trunk())"),
    bootstrap: bool = typer.Option(False, "--bootstrap", help="Run poetry install in the repo's pyproject_dir on first boot"),
):
    """Register a repo in ~/.config/cld/config.toml."""
    cfg = Config.from_env()  # ensures the user config exists
    setup_logging(cfg)
    add_repo(_user_config_path(), name, path, default_rev=default_rev,
             bootstrap=bootstrap)
    typer.echo(f"registered '{name}' -> {path}")


@repos_app.command("rm")
@_handle_errors
def repos_rm(name: str = typer.Argument(..., help="Registry name to remove")):
    """Remove a repo from the registry (refused while a ticket container mounts it)."""
    cfg = Config.from_env()
    setup_logging(cfg)
    entry = cfg.repos.get(name)
    if entry:
        holders = tickets_referencing(name, entry.path)
        if holders:
            raise RuntimeError(
                f"repo '{name}' is mounted by ticket container(s): {', '.join(holders)}; "
                "shut them down first"
            )
    remove_repo(_user_config_path(), name)
    typer.echo(f"removed '{name}'")


@app.command()
@_handle_errors
def build(no_cache: bool = typer.Option(False, "--no-cache", help="Force rebuild without cache")):
    """Build base, devcontainer, and run images (base first)."""
    require_docker()
    cfg = Config.from_env()
    setup_logging(cfg)
    log.info("build: no_cache=%s", no_cache)
    cld_root = Path(__file__).resolve().parent.parent
    ensure_image(
        cfg.base_image,
        cld_root / "imgs/claude-base/Dockerfile.claude-base",
        cld_root,
        extra_paths=base_extra_paths(cld_root),
        force=True, no_cache=no_cache,
    )
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
        force=True, no_cache=no_cache,
    )
    ensure_image(
        cfg.run_image,
        cld_root / "imgs/claude-run/Dockerfile.claude-run",
        cld_root / "imgs/claude-run",
        extra_paths=run_extra_paths(cld_root),
        parent_image=(
            cfg.base_image,
            cld_root / "imgs/claude-base/Dockerfile.claude-base",
            cld_root,
            base_extra_paths(cld_root),
        ),
        force=True, no_cache=no_cache,
    )


@app.command()
def prompts():
    """List available prompt templates with descriptions."""
    cfg = Config.from_env()
    setup_logging(cfg)
    log.info("prompts list")
    prompts_dir = Path(__file__).resolve().parent.parent / "prompts"
    if not prompts_dir.exists():
        typer.echo("No prompts directory found.", err=True)
        raise typer.Exit(1)

    items = list_prompt_items(prompts_dir)
    if not items:
        typer.echo("No prompts found.")
        return

    width = max(len(name) for name, _ in items)
    for name, desc in items:
        typer.echo(f"  {name:<{width}}  {desc}")


chain_app = typer.Typer(help="Multi-agent chain orchestrator.")
app.add_typer(chain_app, name="chain")


@chain_app.command("run")
@_handle_errors
def chain_run(
    chain_file: str = typer.Argument(..., help="Path to chain YAML file or @<name>"),
    refs: Optional[list[str]] = typer.Argument(None, help="Prompt refs for the initial task, in order: @<ref> or a file path"),
    prompt: str = typer.Option("", "-p", "--prompt", help="Inline initial task description, appended after the refs"),
    name: str = typer.Option("", "-n", "--name", help="Chain session name suffix"),
    model: str = typer.Option("", "-m", "--model", help="Override default model"),
    revision: str = typer.Option("", "-r", "--revision", help="Starting revision"),
    no_detach: bool = typer.Option(
        False, "--no-detach", "--foreground",
        help="Run synchronously in the foreground (do not detach).",
    ),
):
    """Run a multi-agent chain end to end (detached by default)."""
    chain_path = _resolve_chain_path(chain_file)
    if not chain_path.is_file():
        typer.echo(f"Error: chain file not found: {chain_file}", err=True)
        raise typer.Exit(1)
    cfg = Config.from_env()
    setup_logging(cfg)
    cld_root = Path(__file__).resolve().parent.parent
    repo_root = find_repo_root()
    # Before any host jj call on the repo (the anchor resolution below).
    prepare_store_mounts(repo_root)
    brief, _ = _compose_from_args(refs or [], prompt, repo_root, cld_root)

    log.info(
        "chain run: file=%s, name=%s, model=%s, no_detach=%s",
        chain_file, name or "<auto>", model or "<default>", no_detach,
    )

    if no_detach:
        if model:
            os.environ["CLD_CHAIN_DEFAULT_MODEL"] = model
            cfg = Config.from_env()
        result = run_chain(
            cfg, chain_path, initial_task=brief,
            revision=revision, name_suffix=name,
        )
        print_chain_report(result, get_backend())
        raise typer.Exit(0 if result.success else 1)

    # --- Detached mode ---
    chain = load_chain(chain_path)
    validate_chain(chain, repo_root, cld_root)
    chain = apply_name_override(chain, name)

    # Pin the anchor in the foreground (like every container launcher) so it tracks where the
    # user is at invocation, not where the detached child happens to boot.
    # Resolve before GC so a bad -r errors without disturbing any prior archive.
    anchor_hash = resolve_anchor(get_backend(), revision)
    log.info("Chain anchor: %s", anchor_hash[:12])

    state_dir = chain_state_dir(repo_root, chain.name)
    _gc_or_refuse(state_dir, chain.name)
    state_dir.mkdir(parents=True, exist_ok=True)

    initial_state = ChainState(
        schema_version=1,
        kind="chain",
        chain_name=chain.name,
        chain_session=f"chain_{chain.name}",
        chain_branch=f"chain_{chain.name}",
        chain_file=str(chain_path.resolve()),
        anchor_hash=anchor_hash,
        pid=0,
        started_at=_utcnow_iso(),
        finished_at=None,
        log_file=str(state_dir / "chain.log"),
        status="running",
        total_steps=len(chain.steps),
        current_index=0,
        current_kind="pending",
        current_step_name="",
        current_step_sessions=[],
        completed_steps=[],
        total_cost_usd=0.0,
        failure_reason="",
        inputs={
            "brief": brief,
            "revision": revision,
            "model": model,
            "name": name,
        },
    )
    write_state(state_dir / "state.json", initial_state.to_dict())

    child_env = os.environ.copy()
    if model:
        child_env["CLD_CHAIN_DEFAULT_MODEL"] = model

    pid = _spawn_chain_runner(state_dir, repo_root, child_env)
    (state_dir / "pid").write_text(f"{pid}\n")

    # Record the child pid immediately so `cld chain status` doesn't briefly
    # report the chain as stale (pid 0) during the window before the child boots.
    initial_state.pid = pid
    write_state(state_dir / "state.json", initial_state.to_dict())

    _print_chain_launch_banner(chain.name, pid, state_dir)


def _spawn_chain_runner(state_dir: Path, repo_root: Path, child_env: dict) -> int:
    """Detach the background chain runner; return its pid."""
    log_fh = open(state_dir / "chain.log", "ab", buffering=0)
    child = subprocess.Popen(
        [sys.executable, "-m", "cld", "chain", "_chain-runner", str(state_dir)],
        stdout=log_fh, stderr=log_fh,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        cwd=str(repo_root),
        env=child_env,
    )
    log_fh.close()
    return child.pid


@chain_app.command("_chain-runner", hidden=True)
def _chain_runner(
    state_dir: str = typer.Argument(...),
):
    """Internal: execute a previously-staged chain run."""
    state_dir_path = Path(state_dir)
    state = ChainState.load(state_dir_path / "state.json")

    cfg = Config.from_env()
    setup_logging(cfg)
    writer = StateWriter(state_dir_path / "state.json", state)

    writer.update(pid=os.getpid())

    model_override = state.inputs.get("model", "")
    if model_override:
        os.environ["CLD_CHAIN_DEFAULT_MODEL"] = model_override
        cfg = Config.from_env()

    try:
        result = run_chain(
            cfg,
            Path(state.chain_file),
            initial_task=state.inputs.get("brief", ""),
            revision=state.inputs.get("revision", ""),
            name_suffix=state.inputs.get("name", ""),
            state_writer=writer,
            anchor_hash=state.anchor_hash,
        )
        final_status = "success" if result.success else "failed"
        reason = result.failure_reason if not result.success else ""
        try:
            writer.mark_finished(final_status, reason=reason)
        except Exception as write_err:
            log.error("Failed to write final chain state: %s", write_err)
        print_chain_report(result, get_backend())
        raise typer.Exit(0 if result.success else 1)
    except typer.Exit:
        raise
    except KeyboardInterrupt:
        writer.mark_finished("interrupted", reason="SIGINT")
        raise
    except BaseException as e:
        writer.mark_finished("failed", reason=f"{type(e).__name__}: {e}")
        raise


@chain_app.command("validate")
@_handle_errors
def chain_validate(
    chain_file: str = typer.Argument(..., help="Path to chain YAML file or @<name>"),
):
    """Parse and validate a chain file. Exits 0 on success, 1 on any error."""
    chain_path = _resolve_chain_path(chain_file)
    if not chain_path.is_file():
        typer.echo(f"Error: chain file not found: {chain_file}", err=True)
        raise typer.Exit(1)
    cfg = Config.from_env()
    setup_logging(cfg)
    log.info("chain validate: %s", chain_file)
    repo_root = find_repo_root()
    cld_root = Path(__file__).resolve().parent.parent
    chain = load_chain(chain_path)
    validate_chain(chain, repo_root, cld_root)
    typer.echo(f"OK: '{chain.name}' has {len(chain.steps)} top-level step(s)")


@chain_app.command("list")
@_handle_errors
def chain_list():
    """List available chain definitions."""
    cfg = Config.from_env()
    setup_logging(cfg)
    log.info("chain list")
    repo_root = find_repo_root()
    cld_root = Path(__file__).resolve().parent.parent

    seen: set[str] = set()
    items: list[tuple[str, str, str]] = []  # (source, name, description)

    for source, base in (("workspace", repo_root), ("builtin", cld_root)):
        chains_dir = base / "chains"
        if not chains_dir.is_dir():
            continue
        for path in sorted(chains_dir.glob("*.y*ml")):
            try:
                chain = load_chain(path)
            except ValueError as e:
                items.append((source, path.name, f"[invalid: {e}]"))
                continue
            if chain.name in seen:
                continue
            seen.add(chain.name)
            items.append((source, chain.name, chain.description or ""))

    if not items:
        typer.echo("No chains found.")
        return
    name_w = max(len(it[1]) for it in items)
    src_w = max(len(it[0]) for it in items)
    for src, name, desc in items:
        typer.echo(f"  [{src:<{src_w}}]  {name:<{name_w}}  {desc}")


@chain_app.command("dry-run")
@_handle_errors
def chain_dry_run(
    chain_file: str = typer.Argument(..., help="Path to chain YAML file or @<name>"),
):
    """Print the execution plan without launching agents."""
    chain_path = _resolve_chain_path(chain_file)
    if not chain_path.is_file():
        typer.echo(f"Error: chain file not found: {chain_file}", err=True)
        raise typer.Exit(1)
    cfg = Config.from_env()
    setup_logging(cfg)
    log.info("chain dry-run: %s", chain_file)
    repo_root = find_repo_root()
    cld_root = Path(__file__).resolve().parent.parent
    chain = load_chain(chain_path)
    validate_chain(chain, repo_root, cld_root)

    typer.echo(f"Chain: {chain.name}")
    if chain.description:
        typer.echo(f"  {chain.description}")
    typer.echo(f"  Default model: {chain.defaults.model}")
    typer.echo()
    typer.echo(f"Plan ({len(chain.steps)} top-level item{'s' if len(chain.steps) != 1 else ''}):")
    for i, item in enumerate(chain.steps, start=1):
        if isinstance(item, ParallelGroup):
            typer.echo(f"  {i}. PARALLEL ({len(item.siblings)} sibling(s)):")
            for s in item.siblings:
                model = s.model or chain.defaults.model
                typer.echo(f"       - {s.name} (persona={s.persona}, model={model})")
        else:
            model = item.model or chain.defaults.model
            typer.echo(f"  {i}. {item.name} (persona={item.persona}, model={model})")


@chain_app.command("status")
@_handle_errors
def chain_status(
    all_: bool = typer.Option(False, "-a", "--all", help="Include terminated chains."),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON."),
    watch: float = typer.Option(0.0, "-w", "--watch", help="Refresh every N seconds; 0 = single shot."),
):
    """List status of running (and optionally all) chains in this repo."""
    _render_status(all_, json_out, watch)


@chain_app.command("ps", hidden=True)
@_handle_errors
def chain_ps(
    all_: bool = typer.Option(False, "-a", "--all", help="Include terminated chains."),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON."),
    watch: float = typer.Option(0.0, "-w", "--watch", help="Refresh every N seconds; 0 = single shot."),
):
    """Alias for 'cld chain status'."""
    _render_status(all_, json_out, watch)


def _render_status(all_: bool, json_out: bool, watch: float) -> None:
    cfg = Config.from_env()
    setup_logging(cfg)
    repo_root = find_repo_root()
    chains_dir = repo_root / ".cld" / "chains"

    def render_once() -> None:
        rows = _collect_chain_rows(chains_dir, include_terminal=all_)
        if json_out:
            typer.echo(json.dumps(rows, indent=2))
            return
        _print_status_table(rows)

    if not watch:
        render_once()
        return

    while True:
        if sys.stdout.isatty():
            os.system("clear")
        render_once()
        time.sleep(watch)


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _collect_chain_rows(chains_dir: Path, include_terminal: bool) -> list[dict]:
    rows = []
    if not chains_dir.is_dir():
        return rows
    for state_file in sorted(chains_dir.glob("*/state.json")):
        if state_file.parent.name.endswith(".prev"):
            continue  # archived previous run, not a current chain
        try:
            state = ChainState.load(state_file)
        except Exception:
            continue
        display_status = state.status
        if state.status == "running" and not _is_pid_alive(state.pid):
            display_status = "stale"
        if not include_terminal and display_status not in ("running", "stale"):
            continue
        started_ago = format_age(state.started_at)
        stage = f"{state.current_index + 1}/{state.total_steps}" if state.status == "running" else "-"
        rows.append({
            "name": state.chain_name,
            "stage": stage,
            "current": state.current_step_name or "-",
            "status": display_status,
            "started": started_ago,
            "cost_usd": state.total_cost_usd,
            "pid": state.pid,
            "log_file": state.log_file,
        })
    return rows


def _print_status_table(rows: list[dict]) -> None:
    if not rows:
        typer.echo("No chains found.")
        return
    headers = ("NAME", "STAGE", "CURRENT", "STATUS", "STARTED", "COST")
    name_w = max(len(headers[0]), *(len(r["name"]) for r in rows))
    stage_w = max(len(headers[1]), *(len(r["stage"]) for r in rows))
    cur_w = max(len(headers[2]), *(len(r["current"]) for r in rows))
    status_w = max(len(headers[3]), *(len(r["status"]) for r in rows))
    started_w = max(len(headers[4]), *(len(r["started"]) for r in rows))
    typer.echo(
        f"{'NAME':<{name_w}}  {'STAGE':<{stage_w}}  {'CURRENT':<{cur_w}}  "
        f"{'STATUS':<{status_w}}  {'STARTED':<{started_w}}  COST"
    )
    for r in rows:
        typer.echo(
            f"{r['name']:<{name_w}}  {r['stage']:<{stage_w}}  {r['current']:<{cur_w}}  "
            f"{r['status']:<{status_w}}  {r['started']:<{started_w}}  ${r['cost_usd']:.2f}"
        )


def _gc_or_refuse(state_dir: Path, chain_name: str) -> None:
    """Handle an existing state dir: refuse if running+alive, GC if terminal/stale."""
    state_file = state_dir / "state.json"
    if not state_file.exists():
        return
    try:
        state = ChainState.load(state_file)
    except Exception:
        # Corrupt state: rename and proceed
        _rename_to_prev(state_dir)
        return
    if state.status == "running" and _is_pid_alive(state.pid):
        raise RuntimeError(
            f"Chain '{chain_name}' is already running (pid {state.pid}). "
            f"Use 'cld chain status' to inspect or kill it first."
        )
    _rename_to_prev(state_dir)


def _rename_to_prev(state_dir: Path) -> None:
    prev = state_dir.parent / (state_dir.name + ".prev")
    if prev.exists():
        shutil.rmtree(prev)
    state_dir.rename(prev)


def _print_chain_launch_banner(chain_name: str, pid: int, state_dir: Path) -> None:
    log_file = state_dir / "chain.log"
    typer.echo(f"\nChain '{chain_name}' detached.")
    typer.echo(f"  Runner PID:       {pid}")
    typer.echo(f"  Log file:         {log_file}")
    typer.echo(f"  Follow progress:  tail -f {log_file}")
    typer.echo(f"  Status:           cld chain status")
    typer.echo(f"  Wait:             while kill -0 {pid} 2>/dev/null; do sleep 5; done")


def _resolve_chain_path(arg: str) -> Path:
    """Resolve `@name` shortcut or treat as relative/absolute path."""
    if arg.startswith("@"):
        short = arg[1:]
        if not short.endswith((".yaml", ".yml")):
            short = f"{short}.yaml"
        repo_root = find_repo_root()
        cld_root = Path(__file__).resolve().parent.parent
        for base in (repo_root, cld_root):
            cand = base / "chains" / short
            if cand.is_file():
                return cand
        return repo_root / "chains" / short
    return Path(arg)


if __name__ == "__main__":
    app()
