"""Filesystem mailbox transport shared by the messenger MCP server and the agent supervisor.

Layout under a mailbox root (``~/.cld/mailboxes`` by default, see ``cld.config``):

    <root>/<container_name>/tmp/       -- write-here-first staging
    <root>/<container_name>/inbox/     -- unread messages, one <id>.json per message
    <root>/<container_name>/archive/   -- dealt-with messages
    <root>/<container_name>/outbox.log -- append-only trail of sent messages (full bodies)
    <root>/<container_name>/meta.json  -- task-agent spawn facts, written once at boot
    <root>/<container_name>/state.json -- supervisor liveness (written by agent_loop)
    <root>/_archive/<container_name>/  -- whole mailbox, moved here on teardown
    <root>/_edges/<a>--<b>.json        -- per-edge hop counter + obligation ledger

Entries under the root whose name starts with ``_`` are reserved and are not
mailboxes. ``meta.json`` and ``state.json`` are split by lifetime: what an agent
*is* versus what it is *doing*, so a stale copy of one can't shadow the other.

All mutations are same-filesystem ``rename()`` calls, so no reader ever observes
a partial write. Pure filesystem code -- no MCP/FastMCP imports here so it stays
unit-testable with ``tmp_path``. Container enumeration (the one non-filesystem
bit) is delegated to ``cld.broker`` and imported lazily inside
``list_containers``.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cld.log import get_logger
from cld.names import ticket_id

log = get_logger(__name__)

_TMP = "tmp"
_INBOX = "inbox"
_ARCHIVE = "archive"
_OUTBOX_LOG = "outbox.log"
_META = "meta.json"
_STATE = "state.json"

# Reserved entries directly under the mailbox root: not mailboxes. Anything
# whose name starts with "_" is skipped by mailbox enumeration (list_fleet).
_ARCHIVE_ROOT = "_archive"
_EDGES = "_edges"


# Microsecond precision (still valid RFC3339) so list_inbox's sort-by-ts stays
# correctly ordered even when two messages land in the same second.
_ISO_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime(_ISO_FMT)


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning("could not read %s: %s", path, e)
        return None


def _write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(path)


def mailbox_dir(root: Path, name: str) -> Path:
    """Return the mailbox directory for container *name* under *root*."""
    return root / name


def ensure_mailbox(root: Path, name: str) -> None:
    """Create ``tmp/``, ``inbox/``, ``archive/`` under the mailbox for *name*."""
    base = mailbox_dir(root, name)
    for sub in (_TMP, _INBOX, _ARCHIVE):
        (base / sub).mkdir(parents=True, exist_ok=True)


def mailbox_reaped(root: Path, name: str) -> bool:
    """True when only an archived mailbox is left for *name* -- it was torn down."""
    return not mailbox_dir(root, name).is_dir() and (root / _ARCHIVE_ROOT / name).is_dir()


def resolve_mailbox_dir(root: Path, name: str) -> Path | None:
    """Return *name*'s live mailbox dir, else its archived one, else None.

    Teardown moves the whole dir under ``_archive/``, so readers that must keep
    working after an agent is reaped (transcript) go through here.
    """
    live = mailbox_dir(root, name)
    if live.is_dir():
        return live
    archived = root / _ARCHIVE_ROOT / name
    return archived if archived.is_dir() else None


def write_message(
    root: Path, frm: str, to: str, subject: str, body: str, *,
    peer_limit: int | None = None, answers: str = "", expects_reply: bool = False,
) -> dict | None:
    """Atomically deliver a message into *to*'s inbox; record it in *frm*'s outbox log.

    Writes ``<to>/tmp/<id>.json`` then ``rename()``s into ``<to>/inbox/<id>.json``.
    Returns the full message dict (including the generated ``id`` and ``ts``), or
    ``None`` when the delivery was refused.

    **Reply obligation is declared, not implied.** *expects_reply* opens an obligation
    on the recipient; *answers* names the message id this one discharges. Arrival alone
    obliges nothing -- an unconditional "every message gets a reply" makes every
    acknowledgment oblige another one, which is the polite ping-pong that used to burn
    a whole edge budget. Both fields ride the envelope and the outbox line, and on a
    peer edge they also maintain that edge's obligation ledger (``bump_edge``).

    **A spent edge is silent** (docs/design-task-agents.md D29). On an agent<->agent
    edge that has already delivered its whole hop budget, nothing more goes over it --
    not a retry, not a supervisor-authored reply, not a notice about the budget itself.
    Every sender in the codebase passes through here, which is what makes that rule
    hold for all of them and bounds an edge at ``limit`` messages for its whole life.
    Exempting anything would reopen the loop the budget exists to close: an exempt
    message still obliges a reply, and the reply is refused, which produces another
    message...

    A **reaped** recipient is refused for the same reason: this function creates a
    missing mailbox (that is how a first message reaches a fresh container), so
    delivering to a torn-down agent would resurrect its directory, shadow the archived
    ``meta.json`` behind an empty live one, and quietly un-budget the edge. §10 already
    says messages to a reaped peer "go nowhere" -- this makes that a refusal instead of
    a silent write into a void.

    *peer_limit* seeds a brand-new edge when the sender's own ``meta["peers"]`` has no
    entry for the recipient; only a caller with a config view supplies it.
    """
    if mailbox_reaped(root, to):
        log.warning("refusing to deliver %s -> %s (%s): recipient was reaped", frm, to, subject)
        return None

    # Budgeted iff *both* endpoints are task-agents -- exactly the agent<->agent edge.
    # Keying on both sides rather than on the sender's `peers` mapping is what makes the
    # reply direction count too: edges are asymmetric, so the named peer has no entry of
    # its own. Ticket containers write no meta.json, so the whole control plane is
    # exempt for free. The recipient side reads through the archive so a
    # peer reaped mid-exchange cannot un-budget the edge; the sender is live by definition.
    sender_meta = read_meta(root, frm)
    peer_edge = sender_meta is not None and read_meta_resolved(root, to) is not None

    if peer_edge and edge_spent(root, frm, to):
        edge = read_edge(root, frm, to)
        log.warning(
            "refusing to deliver %s -> %s (%s): edge budget spent, %d/%d messages used",
            frm, to, subject, edge["count"], edge["limit"],
        )
        return None

    ensure_mailbox(root, to)
    ensure_mailbox(root, frm)

    msg = {
        "id": uuid.uuid4().hex,
        "from": frm,
        "to": to,
        "subject": subject,
        "body": body,
        "answers": answers,
        "expects_reply": expects_reply,
        "ts": _now_iso(),
    }
    if peer_edge:
        # Counted before the write because the count *is* the envelope's audit stamp
        # (D17). A crash between the two loses a hop, which only tightens the ceiling.
        limit = (sender_meta.get("peers") or {}).get(to, peer_limit)
        msg["hops"] = bump_edge(
            root, frm, to, limit,
            msg_id=msg["id"], answers=answers, expects_reply=expects_reply,
        )

    to_dir = mailbox_dir(root, to)
    tmp_path = to_dir / _TMP / f"{msg['id']}.json"
    inbox_path = to_dir / _INBOX / f"{msg['id']}.json"
    tmp_path.write_text(json.dumps(msg, indent=2))
    tmp_path.rename(inbox_path)
    log.info("delivered message %s: %s -> %s (%s)", msg["id"], frm, to, subject)

    # subject+body are duplicated here so a transcript is a single-mailbox read:
    # the recipient's copy may be archived or GC'd independently of ours.
    line = {k: msg[k] for k in ("id", "to", "subject", "body", "answers", "expects_reply", "ts")}
    if "hops" in msg:
        line["hops"] = msg["hops"]
    outbox_path = mailbox_dir(root, frm) / _OUTBOX_LOG
    with outbox_path.open("a") as f:
        f.write(json.dumps(line) + "\n")

    return msg


def _read_dir_messages(dir_path: Path) -> list[dict]:
    messages = []
    if not dir_path.is_dir():
        return messages
    for f in dir_path.glob("*.json"):
        try:
            messages.append(json.loads(f.read_text()))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("skipping unreadable mailbox file %s: %s", f, e)
    return messages


def list_inbox(root: Path, name: str, unread_only: bool = True) -> list[dict]:
    """List messages in *name*'s inbox (unread) or archive, sorted by ``ts``.

    Each entry is ``{id, from, subject, ts}``.
    """
    sub = _INBOX if unread_only else _ARCHIVE
    messages = _read_dir_messages(mailbox_dir(root, name) / sub)
    messages.sort(key=lambda m: m.get("ts", ""))
    return [{"id": m["id"], "from": m["from"], "subject": m["subject"], "ts": m["ts"]} for m in messages]


def read_message(root: Path, name: str, msg_id: str) -> dict | None:
    """Full read of one message by id. Searches inbox, then archive."""
    base = mailbox_dir(root, name)
    for sub in (_INBOX, _ARCHIVE):
        path = base / sub / f"{msg_id}.json"
        if path.is_file():
            try:
                return json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as e:
                log.warning("could not read message %s: %s", path, e)
                return None
    return None


def archive_message(root: Path, name: str, msg_id: str) -> bool:
    """Move ``inbox/<id>.json`` to ``archive/<id>.json``. No-op (True) if already archived."""
    base = mailbox_dir(root, name)
    archive_path = base / _ARCHIVE / f"{msg_id}.json"
    if archive_path.is_file():
        return True
    inbox_path = base / _INBOX / f"{msg_id}.json"
    if not inbox_path.is_file():
        return False
    (base / _ARCHIVE).mkdir(parents=True, exist_ok=True)
    inbox_path.rename(archive_path)
    log.info("archived message %s for %s", msg_id, name)
    return True


def oldest_inbox_id(root: Path, name: str) -> str | None:
    """Return the id of the oldest unread message (min mtime) for *name*, or None."""
    inbox = mailbox_dir(root, name) / _INBOX
    if not inbox.is_dir():
        return None
    files = list(inbox.glob("*.json"))
    if not files:
        return None
    oldest = min(files, key=lambda f: f.stat().st_mtime)
    return oldest.stem


def outbox_snapshot(root: Path, name: str) -> int:
    """Return the current line count of *name*'s outbox.log (0 if it doesn't exist yet)."""
    path = mailbox_dir(root, name) / _OUTBOX_LOG
    if not path.is_file():
        return 0
    with path.open() as f:
        return sum(1 for _ in f)


def _read_outbox(path: Path, skip: int = 0) -> list[dict]:
    if not path.is_file():
        return []
    lines = []
    for raw in path.read_text().splitlines()[skip:]:
        if not raw.strip():
            continue
        try:
            lines.append(json.loads(raw))
        except json.JSONDecodeError as e:
            log.warning("skipping unreadable outbox line in %s: %s", path, e)
    return lines


def replied_since(root: Path, name: str, snapshot: int, recipient: str) -> bool:
    """True if *name* has sent to *recipient* since *snapshot* was taken.

    Recipient-scoped on purpose: a plain "did the outbox grow" check is satisfied
    by an unrelated send (answering a peer, escalating to the master) and would
    suppress the supervisor's fallback for the message actually being answered.
    """
    lines = _read_outbox(mailbox_dir(root, name) / _OUTBOX_LOG, skip=snapshot)
    return any(line.get("to") == recipient for line in lines)


def ensure_meta(
    root: Path,
    name: str,
    *,
    parent: str,
    task: str,
    persona: str,
    deliverable_branch: str,
    anchor: str,
    peers: dict[str, int],
) -> dict:
    """Write *name*'s immutable spawn facts once; return them (existing ones win).

    ``peers`` maps each allowed peer's full container name to that edge's hop
    budget. Never overwrites: a warm container restart re-runs the entrypoint,
    and these facts -- ``created_at`` included -- must keep describing the
    original spawn. Liveness deliberately lives in ``state.json`` instead, so
    the two files can't drift.
    """
    existing = read_meta(root, name)
    if existing is not None:
        return existing
    ensure_mailbox(root, name)
    meta = {
        "parent": parent,
        "task": task,
        "persona": persona,
        "deliverable_branch": deliverable_branch,
        "anchor": anchor,
        "peers": dict(peers),
        "created_at": _now_iso(),
    }
    _write_json_atomic(mailbox_dir(root, name) / _META, meta)
    log.info("wrote spawn facts for %s (parent=%s, peers=%s)", name, parent, sorted(peers))
    return meta


def read_meta(root: Path, name: str) -> dict | None:
    """Read *name*'s spawn facts, or None if it has none (not a task-agent)."""
    return _read_json(mailbox_dir(root, name) / _META)


def read_meta_resolved(root: Path, name: str) -> dict | None:
    """Spawn facts from *name*'s live mailbox, else its archived one.

    Separate from ``read_meta`` on purpose: ``ensure_meta``'s write-once guard must only
    ever see the *live* dir, or a re-used task slug would inherit the reaped agent's
    facts instead of writing its own.
    """
    base = resolve_mailbox_dir(root, name)
    return _read_json(base / _META) if base else None


def list_fleet(root: Path, parent: str | None = None) -> list[dict]:
    """List task-agent mailboxes under *root*, optionally only those spawned by *parent*.

    A mailbox belongs to a task-agent iff it holds a ``meta.json`` -- ticket
    containers never write one -- so no kind field is needed here. Each entry is
    the spawn facts plus ``name``. Reserved root entries (``_archive/``,
    ``_edges/``) are skipped.
    """
    if not root.is_dir():
        return []
    fleet = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        meta = read_meta(root, entry.name)
        if meta is None or (parent is not None and meta.get("parent") != parent):
            continue
        fleet.append({"name": entry.name, **meta})
    return fleet


def state_path(root: Path, name: str) -> Path:
    """Path of *name*'s supervisor liveness file (phase, msg_count, cost, current)."""
    return mailbox_dir(root, name) / _STATE


def read_state(root: Path, name: str) -> dict | None:
    """Read *name*'s supervisor state, or None if it hasn't written one yet."""
    return _read_json(state_path(root, name))


def task_summary(text: str, width: int = 160) -> str:
    """First line of *text*, truncated -- ``meta.json``'s ``task`` holds the whole task."""
    lines = (text or "").strip().splitlines()
    if not lines:
        return ""
    return lines[0] if len(lines[0]) <= width else lines[0][: width - 1] + "…"


def _last_activity(base: Path) -> str:
    """Newest mtime among a mailbox's moving parts, as an ISO timestamp ("" if none)."""
    mtimes = [
        p.stat().st_mtime
        for p in (base / _STATE, base / _OUTBOX_LOG, base / _INBOX, base / _ARCHIVE)
        if p.exists()
    ]
    if not mtimes:
        return ""
    return datetime.fromtimestamp(max(mtimes), timezone.utc).strftime(_ISO_FMT)


def fleet_digest(root: Path, parent: str) -> list[dict]:
    """One cheap row per fleet member of *parent* -- no message bodies (§7, D23).

    What the master calls every turn: it compares ``msg_count`` / ``last_activity``
    against what it saw last turn and only then reads a mailbox in full. Sweeping
    inboxes instead would find nothing, since an agent archives each message within
    about a second of processing it -- and pulling N full inboxes every turn would
    flood the master's context on turns where the human asked about something else.

    ``open_asks`` / ``open_with`` / ``oldest_open`` make a stalling peer exchange
    legible from this one call, so the master can intervene while the ask budget still
    has room rather than learning about it from a refusal.
    """
    fleet = list_fleet(root, parent)
    names = [m["name"] for m in fleet]
    rows = []
    for meta in fleet:
        name = meta["name"]
        state = read_state(root, name) or {}
        base = mailbox_dir(root, name)
        rows.append({
            "name": name,
            "task": task_summary(meta.get("task", "")),
            "phase": state.get("phase", "unknown"),
            "msg_count": state.get("msg_count", 0),
            "cost_usd_total": state.get("cost_usd_total", 0.0),
            "unread": len(list((base / _INBOX).glob("*.json"))),
            "last_activity": _last_activity(base),
            **edge_obligations(root, name, [n for n in names if n != name]),
        })
    return rows


def archive_mailbox(root: Path, name: str) -> Path | None:
    """Move *name*'s whole mailbox under ``_archive/``; return its new location.

    Idempotent, because teardown may run it twice: already archived returns the
    existing path, no mailbox at all returns None. A name collision -- a re-used
    task slug -- suffixes the newcomer rather than merging two agents'
    conversations into one transcript.
    """
    live = mailbox_dir(root, name)
    dest = root / _ARCHIVE_ROOT / name
    if not live.is_dir():
        return dest if dest.is_dir() else None
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        suffix = 2
        while (root / _ARCHIVE_ROOT / f"{name}-{suffix}").exists():
            suffix += 1
        dest = root / _ARCHIVE_ROOT / f"{name}-{suffix}"
        log.warning("archive for %s already exists -- archiving this one as %s", name, dest.name)
    live.rename(dest)
    log.info("archived mailbox %s -> %s", name, dest)
    return dest


def transcript(root: Path, name: str) -> list[dict]:
    """Timestamp-ordered join of what *name* received and what it sent.

    Received side is ``inbox/`` + ``archive/``; sent side is ``outbox.log``,
    which carries subject+body precisely so this needs no cross-mailbox reads
    (a recipient's copy may be archived or gone). Works on an archived mailbox.
    Entries are message dicts plus ``direction`` ("in" / "out").
    """
    base = resolve_mailbox_dir(root, name)
    if base is None:
        return []
    entries = [
        {"direction": "in", **msg}
        for msg in _read_dir_messages(base / _INBOX) + _read_dir_messages(base / _ARCHIVE)
    ]
    entries += [
        {
            "direction": "out",
            "id": line.get("id", ""),
            "from": name,
            "to": line.get("to", ""),
            "subject": line.get("subject", ""),
            "body": line.get("body", ""),
            "answers": line.get("answers", ""),
            "expects_reply": line.get("expects_reply", False),
            "ts": line.get("ts", ""),
            # Received messages carry the hop stamp inside the envelope; sent ones are a
            # fixed projection, so it has to be forwarded explicitly or a transcript
            # would show hops in one direction only.
            **({"hops": line["hops"]} if "hops" in line else {}),
        }
        for line in _read_outbox(base / _OUTBOX_LOG)
    ]
    entries.sort(key=lambda e: e.get("ts", ""))
    return entries


def edge_path(root: Path, a: str, b: str) -> Path:
    """Hop-counter file for the *a*<->*b* edge; endpoints sorted so both sides agree."""
    x, y = sorted((a, b))
    return root / _EDGES / f"{x}--{y}.json"


def read_edge(root: Path, a: str, b: str) -> dict:
    """Hop + obligation state for an edge; never-used edge -> count 0, nothing open.

    ``{count, limit, open, asks, root_since, updated}``. ``open`` holds the ids of
    questions asked over this edge that nobody has answered yet, oldest first, and
    ``root_since`` is when the oldest of them was asked.

    Normalizes a partial file: resetting an edge in this POC means editing it by hand
    (§10), so a hand-written ``{"count": 0}`` must not KeyError downstream.
    """
    data = _read_json(edge_path(root, a, b)) or {}
    return {
        "count": data.get("count", 0),
        "limit": data.get("limit"),
        "open": data.get("open", []),
        "asks": data.get("asks", 0),
        "root_since": data.get("root_since"),
        "updated": data.get("updated"),
    }


def edge_spent(root: Path, a: str, b: str) -> bool:
    """True once the *a*<->*b* edge has delivered its whole budget.

    Asked *before* a delivery, never after: the limit-th message is the last one that
    lands, not the first one refused. An edge with no limit stored yet is never spent.
    """
    edge = read_edge(root, a, b)
    return edge["limit"] is not None and edge["count"] >= edge["limit"]


def bump_edge(
    root: Path, a: str, b: str, limit: int | None = None, *,
    msg_id: str = "", answers: str = "", expects_reply: bool = False,
) -> int:
    """Count one delivered message over the *a*<->*b* edge; return the new count.

    Also keeps the edge's **obligation ledger**: *answers* discharges that id,
    *expects_reply* opens *msg_id* and counts an ask. ``asks`` is cleared only when
    ``open`` empties, so an exchange that keeps re-opening questions without ever
    answering the one it started from cannot reset its own budget -- that regress is
    what ``ask_spent`` bounds.

    Pure accounting -- whether a message may pass is ``edge_spent`` / ``ask_spent``'s
    question, asked before the delivery. *limit* only seeds an edge that has none
    stored yet: the stored value wins from then on, so the side that declared the edge
    governs both directions (edges are asymmetric -- only an already-spawned agent can
    be named as a peer).
    """
    edge = read_edge(root, a, b)
    count = edge["count"] + 1
    open_ids = [i for i in edge["open"] if i != answers]
    asks = edge["asks"]
    if expects_reply:
        open_ids.append(msg_id)
        asks += 1
    root_since = edge["root_since"] if open_ids else None
    if open_ids and not root_since:
        root_since = _now_iso()
    path = edge_path(root, a, b)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(path, {
        "count": count,
        "limit": edge["limit"] if edge["limit"] is not None else limit,
        "open": open_ids,
        "asks": asks if open_ids else 0,
        "root_since": root_since,
        "updated": _now_iso(),
    })
    return count


def ask_spent(root: Path, a: str, b: str, limit: int) -> bool:
    """True once *limit* asks are outstanding under one unanswered question on this edge.

    Asked before a delivery, like ``edge_spent``: the limit-th ask is the last one that
    lands. It only counts while something is still open, so answering the root resets
    the budget and a productive exchange is never penalized. What it bounds is the
    clarification regress -- two agents that keep asking each other for clarification
    and never answer the question they started from. That shape defeats a depth cap
    (discharging and re-opening keeps the depth at one) and is only caught by the
    absolute hop budget after the edge is already exhausted.
    """
    edge = read_edge(root, a, b)
    return bool(edge["open"]) and edge["asks"] >= limit


def edge_obligations(root: Path, name: str, peers: list[str]) -> dict:
    """*name*'s unanswered-question state across its edges to *peers*.

    ``{open_asks, open_with, oldest_open}`` -- what the master needs to see a stall
    forming *before* the ask budget refuses anything; otherwise a regress is invisible
    to the only party that can resolve it (it usually means the master under-specified
    one of the two tasks). Counters are edge properties, so both endpoints report the
    same number for the edge they share.
    """
    open_with, open_asks, since = [], 0, []
    for peer in peers:
        edge = read_edge(root, name, peer)
        if not edge["open"]:
            continue
        open_with.append(peer)
        open_asks += edge["asks"]
        if edge["root_since"]:
            since.append(edge["root_since"])
    return {
        "open_asks": open_asks,
        "open_with": sorted(open_with),
        "oldest_open": min(since) if since else "",
    }


def _master_of(root: Path, name: str) -> str:
    return (read_meta(root, name) or {}).get("parent") or "your master"


def gated_send(
    root: Path, frm: str, to: str, subject: str, body: str, *,
    default_limit: int, ask_limit: int, answers: str = "", expects_reply: bool = False,
) -> dict:
    """Resolve *to*, deliver under the edge budget, and report the outcome as one dict.

    The gate every *instructed* send path goes through -- the messenger MCP tool and
    ``python -m cld.messenger.send`` alike, because a skill baked into the image tells
    agents to use the latter. The closure rule itself lives one level down in
    ``write_message``, so a supervisor-authored reply obeys it too.

    Two gates, and they refuse different things. The hop budget closes the whole edge:
    past it nothing is delivered at all. The **ask** budget refuses only the *asking* --
    ``expects_reply`` past ``ask_limit`` is turned away while answers, informs and the
    master channel keep flowing, so the graceful landing an exchange needs is always
    available instead of having to fit inside a budget.

    One dict shape for every outcome, so both entry points handle one thing:
    ``{"error": …}`` for an unresolvable recipient, a spent edge or a spent ask budget,
    else ``{"id", "to"}`` plus ``{"hops", "limit"}`` when the edge is budgeted and
    ``{"open_asks", "ask_limit"}`` while anything is unanswered on it.
    """
    try:
        resolved = resolve_recipient(to, root=root)
    except ValueError as e:
        return {"error": str(e)}

    if expects_reply and ask_spent(root, frm, resolved, ask_limit):
        edge = read_edge(root, frm, resolved)
        return {"error": (
            f"{edge['asks']} unanswered question(s) are already open on the edge to "
            f"{resolved} (ask limit {ask_limit}), so no further question can be asked "
            f"over it. Answer the open one with your best interpretation and state the "
            f"assumption you made, or ask {_master_of(root, frm)} to rule -- that "
            f"channel is never budgeted. Sending without expects_reply still works."
        )}

    msg = write_message(
        root, frm, resolved, subject, body,
        peer_limit=default_limit, answers=answers, expects_reply=expects_reply,
    )
    if msg is None:
        master = _master_of(root, frm)
        if not edge_spent(root, frm, resolved):
            return {"error": (
                f"{resolved} has been torn down -- its mailbox is archived, so nothing can "
                f"reach it any more. Tell {master} if that work still needs doing."
            )}
        edge = read_edge(root, frm, resolved)
        return {"error": (
            f"hop budget spent for the edge to {resolved}: {edge['count']}/{edge['limit']} "
            f"messages used, and a spent edge delivers nothing further. Do not retry and do "
            f"not work around it -- tell {master} instead; that channel is never budgeted."
        )}
    sent = {"id": msg["id"], "to": resolved}
    if "hops" in msg:
        edge = read_edge(root, frm, resolved)
        sent["hops"] = msg["hops"]
        sent["limit"] = edge["limit"]
        if edge["open"]:
            sent["open_asks"] = edge["asks"]
            sent["ask_limit"] = ask_limit
    return sent


def list_containers(kind: str | None = None) -> list[dict]:
    """Enumerate cld containers, ``{name, kind, repo, status}`` per entry.

    *kind* filters to one ``org.cld.kind`` value (``"task-agent"``,
    ``"ticket"``, or the legacy ``"agent"``); omit for all. Delegates to the
    host-docker seam: the local daemon on the host, the SSH broker in a
    container (there is no docker socket in-container). Stopped containers are
    included.
    """
    from cld.broker import list_cld_containers
    return list_cld_containers(kind)


def resolve_recipient(to: str, containers: list[dict] | None = None, root: Path | None = None) -> str:
    """Resolve a shortname (ticket slug or repo basename) or full container name
    to a full container name.

    When *root* is given and *to* already names an existing mailbox directory
    under it, return *to* directly -- this is the reply path (the recipient's
    full name comes from the message's ``from`` field), and it needs no container
    enumeration, so agents can reply without any host channel.

    Otherwise enumerate, in order (design-ticket-containers.md section 6.5):
    exact container name, then ticket slug, then repo basename -- a shortname
    matching both a ticket slug and a repo basename is an ambiguity error
    naming both. Among basename matches, an ``agent``-kind container wins:
    nothing launches that (removed) standing role any more, but one left over
    from an older image is still a single, unambiguous answer for its repo.
    Raises ValueError if *to* is a basename matching containers from two
    different repo roots (ambiguous), or isn't found at all.
    """
    if root is not None and mailbox_dir(root, to).is_dir():
        return to

    all_containers = containers if containers is not None else list_containers()

    for c in all_containers:
        if c["name"] == to:
            return to

    ticket_matches = [
        c for c in all_containers
        if c["kind"] == "ticket" and c["name"] == ticket_id(to)
    ]
    matches = [
        c for c in all_containers
        if c["kind"] != "ticket" and Path(c["repo"]).name == to
    ]
    if ticket_matches and matches:
        raise ValueError(
            f"Ambiguous shortname '{to}': it is ticket container "
            f"{ticket_matches[0]['name']} and the repo basename of "
            f"{', '.join(sorted(c['name'] for c in matches))}. "
            "Use the full container name."
        )
    if ticket_matches:
        return ticket_matches[0]["name"]
    if not matches:
        raise ValueError(f"No container found for '{to}' (not a known shortname or container name)")

    repos = {c["repo"] for c in matches}
    if len(repos) > 1:
        raise ValueError(
            f"Ambiguous shortname '{to}': matches containers from multiple repos: {sorted(repos)}"
        )

    for c in matches:
        if c["kind"] == "agent":
            return c["name"]
    if len(matches) > 1:
        # Task-agents are many per repo, so a repo basename no longer identifies one.
        # Picking the first would be a silent misdelivery to whichever container docker
        # happened to list first.
        raise ValueError(
            f"Ambiguous shortname '{to}': {len(matches)} task-agents share that repo "
            f"({', '.join(sorted(c['name'] for c in matches))}). Address a task-agent by "
            "its full container name."
        )
    return matches[0]["name"]
