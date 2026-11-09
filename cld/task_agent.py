"""Task-agent helpers shared by the host CLI and the container CLI.

Plain functions, no typer command wiring: `cld/cli.py` owns the host lifecycle
(spawn, reap) and `cld/cli_container.py` the container surface, but both resolve
names, read the mailbox tree and render the same rows.
"""

from pathlib import Path

import typer

from cld.agent_runtime import format_age
from cld.config import Config
from cld.docker import (
    docker_task_agent_list,
    docker_task_agent_status,
    in_master_container,
)
from cld.log import get_logger
from cld.messenger import mailbox

log = get_logger(__name__)


def mailbox_root(cfg: Config) -> Path:
    return Path(cfg.mailbox_root).expanduser()


def parse_peer_specs(specs: list[str], default_limit: int) -> dict[str, int]:
    """Parse repeatable ``--peer <name>[:<hops>]`` into a name -> hop-budget mapping.

    A spec without ``:<hops>`` gets the configured absolute limit (§10). Container
    names cannot contain ':', so the delimiter is unambiguous.
    """
    peers: dict[str, int] = {}
    for spec in specs:
        name, sep, hops = spec.partition(":")
        if not name:
            raise ValueError(f"--peer {spec!r}: missing peer name (expected <name>[:<hops>])")
        if name in peers:
            raise ValueError(f"--peer {name}: named twice")
        if sep and not (hops.isdigit() and int(hops) > 0):
            raise ValueError(f"--peer {spec!r}: hop budget must be a positive integer")
        peers[name] = int(hops) if sep else default_limit
    return peers


def format_peers(peers: dict[str, int]) -> str:
    """Peer edges as `<name> (<n> hops)`, for the launch banner and the detail view."""
    return ", ".join(f"{p} ({h} hops)" for p, h in sorted(peers.items())) or "none"


def known_task_agent_names(cfg: Config) -> set[str]:
    """Names of task-agents this host knows: live containers plus mailboxes with spawn facts.

    Inside master there is no docker socket, so the mailbox tree (which *is* bind-mounted)
    is the whole view -- asking docker would only log a failed `docker ps` per call.
    """
    names = set() if in_master_container() else {c["name"] for c in docker_task_agent_list()}
    return names | {m["name"] for m in mailbox.list_fleet(mailbox_root(cfg))}


def resolve_task_agent(cfg: Config, name: str) -> str:
    """Resolve a bare task slug -- or a full identifier -- to a full identifier.

    A CLI affordance only (D26): mailbox addressing is always by full name. A
    bare slug matches every identifier whose last dot part equals it (the slug
    never contains a dot), so `add-oauth` finds `a.lide-1.add-oauth`; several
    tickets using one slug is an ambiguity. Archived mailboxes resolve by full
    name, so `transcript` keeps working after a reap.
    """
    live = known_task_agent_names(cfg)
    if name in live:
        return name

    matches = sorted(c for c in live if c.rsplit(".", 1)[-1] == name)
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise RuntimeError(
            f"'{name}' is ambiguous -- it matches {', '.join(matches)}. Use the full "
            "identifier."
        )

    if mailbox.resolve_mailbox_dir(mailbox_root(cfg), name) is not None:
        return name
    raise RuntimeError(
        f"no task-agent named '{name}' (neither live nor archived). "
        "See `cld task-agent status`."
    )


def task_agent_rows(cfg: Config, parent: str = "") -> list[dict]:
    """Roster rows: every task-agent container, plus mailboxes whose container is gone.

    A mailbox means the agent was started; a container means it is alive. The pair
    that doesn't line up -- mailbox, no container -- is §10's manual-cleanup signal,
    so it gets its own `gone` state rather than being dropped.

    *parent* scopes the roster to one master's fleet. Empty (a human on the host) shows
    everything, which is what makes this the surface for hunting orphans; the broker
    passes a value so a master sees its own fleet rather than every master's.
    """
    root = mailbox_root(cfg)
    rows: dict[str, dict] = {}
    for c in docker_task_agent_list():
        if parent and c["parent"] != parent:
            continue
        status = docker_task_agent_status(c["name"])
        rows[c["name"]] = {
            "name": c["name"],
            "container": "gone" if status == "absent" else status,
        }
    for m in mailbox.list_fleet(root, parent or None):
        row = rows.setdefault(m["name"], {"name": m["name"], "container": "gone"})
        row["created"] = m.get("created_at", "")
    for name, row in rows.items():
        state = mailbox.read_state(root, name) or {}
        row["phase"] = state.get("phase", "-")
        row["msgs"] = state.get("msg_count", 0)
        row["cost"] = state.get("cost_usd_total", 0.0)
        row.setdefault("created", "")
    return [rows[key] for key in sorted(rows)]


def print_task_agent_roster(rows: list[dict]) -> None:
    if not rows:
        typer.echo("No task-agents found.")
        return
    name_w = max(len("NAME"), *(len(r["name"]) for r in rows))
    cont_w = max(len("CONTAINER"), *(len(r["container"]) for r in rows))
    phase_w = max(len("PHASE"), *(len(str(r["phase"])) for r in rows))
    typer.echo(
        f"{'NAME':<{name_w}}  {'CONTAINER':<{cont_w}}  {'PHASE':<{phase_w}}  MSGS  COST      AGE"
    )
    for r in rows:
        typer.echo(
            f"{r['name']:<{name_w}}  {r['container']:<{cont_w}}  {str(r['phase']):<{phase_w}}  "
            f"{r['msgs']:>4}  ${r['cost']:<8.4f} {format_age(r['created']) if r['created'] else '-'}"
        )
    gone = [r["name"] for r in rows if r["container"] == "gone"]
    if gone:
        typer.echo(f"\n{len(gone)} mailbox(es) with no container: {', '.join(gone)}")
        typer.echo("  Clear each with: cld task-agent shutdown <name>")


def print_task_agent_detail(cfg: Config, name: str) -> None:
    root = mailbox_root(cfg)
    status = docker_task_agent_status(name)
    typer.echo(f"Task-agent: {name}")
    typer.echo(f"  Container:  {'gone' if status == 'absent' else status}")

    if not mailbox.mailbox_dir(root, name).is_dir():
        # Reaped: teardown moved the whole mailbox under the archive root. Detail
        # is a live-agent view by design (§7 pairs the archive with `transcript`).
        typer.echo("  Mailbox:    reaped (archived)")
        typer.echo(f"  Read the conversation with: cld task-agent transcript {name}")
        return

    meta = mailbox.read_meta(root, name)
    if meta is None:
        typer.echo("  Spawn facts: none yet (meta.json is written when the supervisor boots)")
    else:
        peers = meta.get("peers") or {}
        typer.echo(f"  Task:       {mailbox.task_summary(meta.get('task', ''), 72)}")
        typer.echo(f"  Persona:    {meta.get('persona', '')}")
        typer.echo(f"  Branch:     {meta.get('deliverable_branch', '')}")
        typer.echo(f"  Anchor:     {(meta.get('anchor') or '')[:12] or '-'}")
        typer.echo(f"  Anchor mode: {task_agent_record(cfg, name).get('anchor_mode') or '-'}")
        typer.echo(f"  Parent:     {meta.get('parent') or '<none -- launched on the host>'}")
        typer.echo(f"  Peers:      {format_peers(peers)}")
        typer.echo(f"  Created:    {meta.get('created_at', '')}")

    state = mailbox.read_state(root, name)
    if state is None:
        typer.echo("  Supervisor state: unavailable (not started yet)")
        return
    typer.echo(f"  Phase:      {state.get('phase')}")
    typer.echo(f"  Messages:   {state.get('msg_count')}")
    typer.echo(f"  Cost:       ${state.get('cost_usd_total', 0.0):.4f}")
    current = state.get("current")
    if current:
        typer.echo(
            f"  Processing: {current.get('subject')} (from {current.get('from')}, "
            f"since {current.get('started_at')})"
        )


def print_task_agent_transcript(cfg: Config, name: str) -> None:
    entries = mailbox.transcript(mailbox_root(cfg), name)
    if not entries:
        typer.echo(f"No messages for {name}.")
        return
    for e in entries:
        outgoing = e["direction"] == "out"
        typer.echo(
            f"{e['ts']}  {'->' if outgoing else '<-'} "
            f"{e['to'] if outgoing else e['from']}  {e['subject']}"
        )
        for line in (e.get("body") or "").splitlines():
            typer.echo(f"    {line}")
        typer.echo("")


def task_agent_record(cfg: Config, name: str) -> dict:
    """A task-agent's host-set label record, or {} once its container is gone."""
    for c in docker_task_agent_list():
        if c["name"] == name:
            return c
    return {}


def task_agent_parent(cfg: Config, name: str) -> str:
    """The master owning *name*, from the container label if there still is one.

    Labels are host-set, so a container cannot rewrite its own parent to slip out of
    reap check 3. ``meta.json`` is the fallback once the container is gone -- by then
    it is the only record left.
    """
    rec = task_agent_record(cfg, name)
    if rec:
        return rec["parent"]
    return (mailbox.read_meta(mailbox_root(cfg), name) or {}).get("parent", "")
