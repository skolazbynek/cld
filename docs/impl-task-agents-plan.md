# Implementation plan — `cld task-agent`

> Working to-do list + scratch for implementing `docs/design-task-agents.md`.
> Update it as work lands: tick boxes, record deviations under **Scratch**.
> Section refs (§N, DN) point into the design doc.

## Ordering

```
P1 (mailbox/config foundation)
 ├─▶ P2 (host launch plumbing + entrypoint)
 ├─▶ P3 (supervisor task mode + persona)
 ├─▶ P5 (messenger MCP: fleet tools + hop gate)
 └─▶ P4 (CLI surface + reap gating)   ← needs P1+P2
                                       └─▶ P6 (in-master path, broker, master persona, docs)
```
P2, P3, P5 are independent of each other once P1 exists. P4 needs P1+P2. P6 last —
it wires the in-master route and is the only part that needs a live fleet to validate.

---

## P1 — Mailbox & config foundation
**Status:** [x] **done** (2026-08-11) — detailed plan in [Appendix P1](#appendix-p1--detailed-implementation-plan);
outcome and deviations at the end of that appendix.

Pure filesystem + config work, no Docker, no MCP. Everything else builds on it, so it
goes first and lands with unit tests over temp dirs.

- Spawn-facts file per agent mailbox: immutable at boot, no liveness field (D24). Read
  and write helpers, plus a fleet-listing helper that filters by parent (§6).
- Reply guarantee becomes **recipient-scoped**: instead of "did the outbox grow", ask
  "did a line addressed to *this* sender appear after the snapshot" (D21, §5 step 3).
- Sent-message log lines carry subject and body so a transcript is a single-mailbox
  read (D27, §12).
- Transcript assembly: timestamp-ordered join of received (unread + archived) and sent,
  working against both a live mailbox and an archived one.
- Teardown archive: move a whole mailbox dir under the archive root, idempotently (D7).
- Per-edge hop counter store: one file per endpoint pair with sorted endpoints so both
  sides derive the same path; staged-write + rename; read/increment/limit primitives
  (§10). No locking, by decision.
- Two new config knobs with env overrides: per-master concurrency cap (default 4) and the
  per-edge absolute hop limit (default 10, used when a peer spec omits its own).
  Document both in the config table.

**Done when:** unit tests cover spawn-facts round-trip, recipient-scoped reply detection
(including the "sent to a peer, still owes the master" case), transcript ordering across
live + archived mailboxes, and hop-counter increment/limit; config resolution tests
extended for the two new keys.

**Touchpoints:** `cld/messenger/mailbox.py`, `cld/config.py`, `cld/config.default.toml`,
`tests/test_mailbox.py`, `tests/test_config.py`, `CLAUDE.md` (config table).

---

## P2 — Host launch plumbing & container boot
**Status:** [x] **done** (2026-08-12) — detailed plan in [Appendix P2](#appendix-p2--detailed-implementation-plan);
outcome and deviations at the end of that appendix. Entrypoint changes still need a
host-side e2e run (checklist in Appendix P2 §F).

Make a task-scoped container launchable from the host: identity, labels, guard rails at
spawn, and the boot-time deltas inside the container.

- Task-scoped container naming from repo + master-generated slug, with a collision
  suffix. The discriminator is a new container-kind label, not the name prefix (D4, D5).
- Container-arg construction gains a task-agent role: kind/parent-master/task labels,
  mailbox mount, and the per-spawn facts the container needs (deliverable branch, peer
  list, hop limit) passed as environment.
- A label-scoped enumerator for task-agents, plus a **running-only** variant — the
  existing enumerator includes stopped containers and would make the cap refuse spawns
  because of corpses (§9).
- Two spawn-time refusals, both host-side where Docker and the origin store are
  readable: the per-master concurrency cap, and an anchor that lands inside a *live*
  agent's stack (one revset query against running members' recorded anchors) (D9, D10).
- Container entrypoint gains a task-agent branch: create the deliverable bookmark at the
  anchor (distinct string from the session bookmark, so no clash), seed SSH host keys so
  a first push on wrap-up doesn't fail on host-key verification, and exec the supervisor
  in task mode (§8, §9).

**Done when:** a task-agent can be spawned from the host and reaches its idle state;
labels are inspectable; the cap and live-stack refusals fire with errors naming the
offending agents; the deliverable bookmark exists at the anchor after boot.

**Touchpoints:** `cld/docker.py`, `imgs/claude-devcontainer/entrypoint-claude-devcontainer.sh`,
`tests/test_docker.py`, `tests/test_docker_integration.py`.

---

## P3 — Supervisor task mode & lifecycle persona
**Status:** [x] **done** (2026-08-12) — detailed plan in [Appendix P3](#appendix-p3--detailed-implementation-plan);
outcome and deviations at the end of that appendix.

One supervisor, parametrized — no forked copy, so the old repo-agent path stays
cleanly deletable later (D12).

- Task mode selects a **composed** kickoff prompt: lifecycle preamble, then the chosen
  role persona, then the concrete task from the inline prompt and/or task file (§11).
  Persona resolution follows the chain convention (repo personas first, then cld's).
- Write the immutable spawn facts into the agent's own mailbox at boot; liveness keeps
  living in the existing state file (D24).
- Switch the reply guarantee to the recipient-scoped check from P1 (D21).
- Deliverable-branch awareness: the agent knows its branch name and what wrap-up means.
- Cleanup on teardown is explicitly **not** here — it is caller-side (D22); the existing
  in-container signal handler stays as a best-effort fast path only.
- New lifecycle preamble persona: bounded to one task; the master drives you and decides
  wrap-up; squash into the deliverable branch and report it; a reply must go to the
  sender of the message being answered; you may message only the master and the named
  peers; peer edges carry a hop budget and a blocked send means escalate, not retry; the
  master channel is never budgeted; anchor invariant holds. Replaces the old persona's
  "immortal" framing (§11).

**Done when:** supervisor tests cover task-mode kickoff composition, spawn-facts write,
and the recipient-scoped fallback reply; the preamble persona exists and is referenced
by the launch path.

**Touchpoints:** `cld/messenger/agent_loop.py`, `prompts/personas/task-agent.md`,
`tests/test_agent_loop.py`, `tests/test_prompts.py`.

---

## P4 — CLI surface and reap gating
**Status:** [x] **done** (2026-08-12) — detailed plan in [Appendix P4](#appendix-p4--detailed-implementation-plan);
outcome and deviations at the end of that appendix. Nothing has spawned a real container
yet: the host-side e2e checklists from P2 and P4 are both open.

The human- and master-facing command group. Verb-first, matching the rest of the CLI
(D26).

- New command group with start / status / logs / transcript / shutdown. Reuses the
  persistent-container launch logic but **without** repo-deterministic start-or-attach:
  every start creates a new container (§12).
- Start accepts persona, task prompt and/or task file, deliverable branch, model,
  revision, and repeatable `--peer <name>[:<hops>]` specs (§C.1) — validating positive
  limits and rejecting duplicate peer names.
- Status has two shapes: a fleet roster, and a single-agent detail view (phase, cost,
  branch, anchor, peers). It must also surface the "mailbox exists, container gone"
  case, which is the manual-cleanup signal (§6, §10).
- Logs is supervisor stderr; transcript is the mailbox conversation from P1 — two
  deliberately different surfaces.
- A bare-slug → full-name resolver scoped to the cwd's repo, erroring with the candidate
  list on ambiguity. CLI affordance only; mailbox addressing stays full-name (D26).
- Shutdown performs stop, remove, then **caller-side** cleanup: forget the session
  bookmark (never the deliverable branch) and archive the mailbox (D22). Gated by the
  three reap-readiness checks — not mid-turn, not a live peer, own fleet only — all
  cheap filesystem/label reads, with no store-side "did the squash happen" test, since
  that is verification's job and already done by then (D2, D2b).
- The override flag stays **host-only** and must never be reachable through the broker
  route added in P6 (§7).

**Done when:** CLI tests cover start argument wiring, slug resolution and ambiguity,
roster and single-agent status rendering, transcript output, each of the three reap
refusals, and that shutdown does bookmark-forget plus mailbox-archive exactly once.

**Touchpoints:** `cld/cli.py`, `tests/test_cli.py`.

---

## P5 — Messenger MCP: fleet observability & hop gate
**Status:** [x] **done** (2026-08-12) — detailed plan in [Appendix P5](#appendix-p5--detailed-implementation-plan);
outcome and deviations at the end of that appendix. Nothing has run in a container yet.

The two capabilities that make the control tower work at all: seeing the fleet cheaply,
and bounding peer loops.

- Two additive read-only tools, both restricted to mailboxes whose recorded parent is
  the caller: a cheap per-member **digest** (task, phase, message count, cost, unread,
  last activity) and a full single-mailbox read that includes the archive and the sent
  log. The existing own-inbox tool keeps its semantics. This matters because peer
  inboxes drain within about a second, so sweeping inboxes finds nothing (D23, §7).
- Send becomes a **gate**: count the message on its edge, and past the limit refuse it
  with an in-turn error telling the agent to escalate rather than retry. On success the
  return reports position and limit. The master↔agent channel is a different edge, so it
  is unbudgeted; the budget applies to agent↔agent only (D16, D20, §10).
- The rule the gate rests on: **a spent edge is silent** — the transport delivers nothing
  more over it, no sender excepted. That replaces §10's hop-exempt cap notice to the peer,
  which (together with the supervisor's synthesized reply) would have looped forever, since
  both are messages that announce the end yet still oblige a reply. The blocked side tells
  the master instead; nothing hangs, because the peer is idle-polling rather than blocked.
  Full reasoning and the ordering trap it implies: Appendix P5 §C.3. The spec was amended
  to match (§5, §10, §12, D16/D20 and a new D29).
- The message envelope carries a hop stamp as an audit field only; the on-disk counter
  is authoritative (D17).
- The gate must sit where **both** send paths pass: the MCP tool and
  `python -m cld.messenger.send`, which a baked-in skill tells agents to use (Appendix P5
  §A.2). Same for addressing — peers go by full container name, and a shortname that
  matches several task-agents must fail loudly instead of picking one.

**Done when:** MCP tests cover parent-scoped access refusal, digest shape, archive +
sent-log inclusion in the full read, the refusal at the limit, hop-exemption for the
master channel, and the invariant that at most `limit` messages ever cross an edge
whoever sends them.

**Touchpoints:** `cld/mcp/messenger.py`, `cld/messenger/mailbox.py` (gate primitives
from P1), `cld/messenger/send.py`, `.claude/skills/messenger-send/SKILL.md`,
`tests/test_messenger_mcp.py`, `tests/test_mailbox.py`.

---

## P6 — In-master route, broker action, master behavior, docs
**Status:** [x] **code done** (2026-08-12) — detailed plan in [Appendix P6](#appendix-p6--detailed-implementation-plan);
outcome at the end of that appendix. **Validation (§B.7) is outstanding**: it needs a docker
daemon, which the development container does not have.

Everything needed for a master to actually run the tower from inside its container,
where there is no Docker socket.

- Route task-agent spawn, enumeration, and shutdown through the host broker when
  running inside master; extend container-record parsing to carry kind, parent, and
  task so the in-master view matches the host view (§12).
- Broker gains task-agent launch/manage, validated against the master's host-set repo
  and target labels exactly as the existing agent action is. Its shutdown path runs the
  reap-readiness checks and the caller-side cleanup; it must **not** accept the override
  flag. Its launch path enforces the cap and the live-stack anchor refusal. **No new
  read action** — cross-repo verification goes through the pushed branch, not the broker
  (D25, §9).
- Master persona / instructions: per-turn call the digest and read only members that
  moved (never a sweep of inboxes); draw the agent graph by passing each agent its
  allowed peers at spawn; own coordination; ask for a **pushed** deliverable branch on
  cross-repo wrap-up and never claim to have verified a self-reported one; report reaps
  in turn output so the human keeps a picture of the fleet. It also needs to *understand*
  the two now-mechanical rules — a handoff is reap-then-spawn, and a reap refusal means
  wrap-up didn't finish — so it plans around refusals instead of hitting them (§12).
- Docs: `CLAUDE.md` architecture + command + config sections, and a short usage note.
- **Validation, and the most likely place this surprises us:** several task-agents plus
  a master, all with Watchman auto-snapshot, writing into one shared jj store multiplies
  op-log lock contention by the cap over today's tested load of one. Smoke-test more than
  one concurrent task-agent explicitly and record what happens (§9).

**Done when:** a master can spawn, drive, and reap a task-agent on its own repo and on a
registered sibling target; the override flag is provably unreachable through the broker;
the concurrent-agent smoke test is run and its outcome recorded below.

**Touchpoints:** `cld/broker.py`, `broker/cld-broker.sh`,
`prompts/personas/*` (master/control-tower guidance), `CLAUDE.md`,
`tests/test_host_docker.py`.

---

## Cross-cutting reminders

- Keep the supervisor single-copy; branch only identity, launch, and cleanup so the old
  repo-agent path can later be deleted cleanly (D12).
- Session bookmark and deliverable branch are different strings with different lifetimes:
  the first is forgotten on teardown, the second survives it (D8).
- Teardown cleanup is caller-side because a supervisor "last act" is normally killed
  mid-turn; both cleanup steps must be idempotent (D22).
- Explicitly out of scope this pass: VCS blackboard, progress budget, spend ceiling,
  turn-injected inbox notice, automatic reaper, removing the existing repo-agent command.

## Scratch

_(Record deviations, surprises, and decisions taken during implementation here.)_

- 2026-08-11: plan written from `docs/design-task-agents.md`; no code changes yet.
- 2026-08-11: P1 detailed plan written (appendix below). Baseline: `poetry run pytest
  tests/test_mailbox.py` = 28 passed. No pre-commit config in this repo; venv at
  `~/.cache/pypoetry/virtualenvs/cld-IVi5i_8T-py3.13`, tests run via `poetry run pytest`.
- 2026-08-11: **amendment to the design doc, agreed with the user** — the peer hop budget
  is declared per edge (`--peer <name>[:<hops>]`, repeatable) instead of a `--peers` list
  plus a per-agent `--peer-hops`. Rationale and consequences in Appendix §C.1. The design
  doc's §4, §10, §12 and D24's field list still describe the old shape; `docs/design-task-agents.md`
  needs a follow-up edit (not done yet — the spec is the user's to amend).
  Edge asymmetry itself is **accepted as designed**: the later-spawned agent declares the
  edge, the earlier one participates by replying, and reap check 2 protects only the
  declared direction.
- 2026-08-12: P6 code landed; **all six parts' code is in**. Fixed a pre-existing bug it
  tripped over: `typer.Exit` subclasses `RuntimeError`, so `_handle_errors` turned every
  deliberate exit code into 1 -- including the 0 an in-master broker dispatch raises on
  success, which means `cld agent status` inside master has always reported failure.
  **Everything left needs a docker daemon:** the N>1 store-contention smoke test (§9's
  flagged unknown) and the three e2e checklists (P2 §F, P4 §F, P5 §F). Nothing is committed.
- 2026-08-12: P5 landed. Two more holes closed on the way (a reap silently un-budgeting
  an edge; a delivery resurrecting a reaped agent's mailbox) -- Appendix P5 §H. The spec's
  §10 and §14 were amended for the second. **Only P6 remains**, plus the three host-side
  e2e checklists (P2, P4, P5 §F), all of which need a docker daemon.
- 2026-08-12: P5 planned, then **simplified after review**: the first draft answered the
  spent-edge loop with two mechanisms (a one-shot flag on the cap notice, a guard on the
  supervisor's fallback); the second replaces both with one transport rule — a spent edge
  delivers nothing — which drops the cap notice entirely, needs no supervisor edit, and
  reduces the loop argument to a one-line invariant. Appendix P5 §C.3. `docs/design-task-agents.md`
  `docs/design-task-agents.md` §5/§10/§12 + D16/D20/D29 and the summary doc were amended to
  match, on the user's go-ahead.
- 2026-08-13: **post-P6 amendment, from testing:** the unconditional reply guarantee looped
  two agents through mutual courtesies until the hop budget ran out. Reply obligation is now
  **declared by the sender** — `expects_reply` / `answers` on the envelope and the outbox
  line, no hardcoded exception for the master channel (the user's call), and the supervisor's
  fallback gated on the incoming `expects_reply`. A reply is explicitly allowed to ask, which
  is what makes a clarification sub-dialogue work; the regress that opens up instead is
  bounded by a second per-edge budget (`root_ask_limit`, default 3) that refuses **the ask,
  not the edge**, so a landing is always available. `fleet_digest` gained `open_asks` /
  `open_with` / `oldest_open`. Spec amended: §5 step 3, §10 (new "Clarification-regress
  control"), §11, §12, D30/D31; summary doc, CLAUDE.md, both personas and two skills follow.
  Test state: 524 passed / 11 skipped, same 2 pre-existing `resolve_prompt_ref` failures
  (30 new tests: obligation ledger, `ask_spent`, `edge_obligations`, the gated-send ask
  refusal, the "no fallback when nothing was asked" regression, config + docker knobs).
- 2026-08-12: P4 landed. `cld task-agent` is real; the open slug question is settled as
  `-n/--name` (Appendix P4 §C.2). Two host-side e2e checklists (P2 §F, P4 §F) are the only
  outstanding work from P1–P4, and both need a docker daemon. **P5 and P6 remain**; P5 needs
  nothing from P4, so either can go next.

---

# Appendix P1 — detailed implementation plan

## A. Ground truth (verified in the tree, not assumed)

`cld/messenger/mailbox.py` (206 lines) is the whole transport: `mailbox_dir`,
`ensure_mailbox`, `write_message`, `_read_dir_messages`, `list_inbox`, `read_message`,
`archive_message`, `oldest_inbox_id`, `outbox_snapshot`, `outbox_changed_since`,
`list_containers`, `resolve_recipient`. Subdir constants are module-level (`_TMP`,
`_INBOX`, `_ARCHIVE`, `_OUTBOX_LOG`). Timestamps come from a module-local `_now_iso()`
at microsecond precision.

Call sites that constrain the changes:

| What | Where | Note |
|---|---|---|
| `outbox_snapshot` + `outbox_changed_since` | `agent_loop.py:160,184` — **only** consumer | the reply guarantee |
| `write_message` | `agent_loop.py:178,187`, `mcp/messenger.py:53`, `messenger/send.py:27` | positional `(root, frm, to, subject, body)` everywhere |
| `state.json` path | built inline in `agent_loop.py:90` **and** `cli.py:446` | filename literal duplicated |
| `_atomic_write_json` | private in `agent_loop.py:52` | second copy will be needed for meta/edges |
| outbox line shape | written `agent_loop`-independently in `tests/fixtures/stub-messenger-agent/claude` | stub writes `{id, to, ts}` — no subject/body |

Two facts that make the D21 regression test cheap: the stub already sends to an arbitrary
`STUB_REPLY_TO`, so "answered a peer while owing the master a reply" needs **no stub
change** — just `STUB_REPLY_TO=peer` with a message whose `from` is `sender`. And
`_read_dir_messages` already models the "skip unreadable file, warn, continue" behavior
the new JSON-line readers should copy.

## B. Deliverables — exact surface

All of it in `cld/messenger/mailbox.py` (stays import-light: stdlib + `cld.log` only, so
the module remains `tmp_path`-testable and safe to import from the MCP grandchild
process). New module constants alongside the existing ones: `_META = "meta.json"`,
`_STATE = "state.json"`, `_ARCHIVE_ROOT = "_archive"`, `_EDGES = "_edges"`.

**1. Spawn facts (D24)**

```
ensure_meta(root, name, *, parent, task, persona, deliverable_branch,
            anchor, peers: dict[str, int]) -> dict
read_meta(root, name) -> dict | None
list_fleet(root, parent: str | None = None) -> list[dict]
```
- `peers` is a **mapping of peer name → that edge's hop limit**, not a list plus a
  separate scalar (see the amendment in §C.1). Reap check 2 and the graph read still only
  need the keys.
- `ensure_meta` stamps `created_at` itself and **writes only when absent**, returning the
  effective dict either way — that is what makes "immutable spawn facts, written once at
  boot" true across a warm `docker start` re-run of the entrypoint. One function, no
  `overwrite=` flag.
- Explicit keyword params rather than a passthrough dict: the schema then lives in one
  place, shared by the writer (P3) and every reader (P2/P4/P5).
- `list_fleet` returns each member's meta with `name` injected, skipping reserved dirs
  (any name starting with `_`) and any dir **without** `meta.json`. That last rule is the
  discriminator inside the mailbox tree: masters and repo-agents never write one, so they
  never appear in a fleet listing. No separate kind field needed.

**2. Liveness read (shared by P4 reap check + P5 digest)**

```
state_path(root, name) -> Path
read_state(root, name) -> dict | None
```
Centralizes the `state.json` filename that is currently a literal in two modules.

**3. Recipient-scoped reply check (D21)**

```
outbox_snapshot(root, name) -> int          # unchanged: line count is the snapshot token
replied_since(root, name, snapshot: int, recipient: str) -> bool
outbox_changed_since(...)                   # DELETED
```
`replied_since` reads the outbox, skips the first `snapshot` lines, and returns True on
the first remaining line whose `to` == `recipient`. Per-line `json.JSONDecodeError` is
skipped with a warning (mirroring `_read_dir_messages`), so a torn append can't crash the
supervisor's reply path.

**4. Outbox line carries the full message (D27)**

`write_message` appends `{id, to, subject, body, ts}` instead of `{id, to, ts}`.
Readers use `.get(key, "")` so pre-change lines still parse.

**5. Transcript assembly (D27, §12)**

```
resolve_mailbox_dir(root, name) -> Path | None    # live, else <root>/_archive/<name>
transcript(root, name) -> list[dict]
```
Entries are `{direction: "in"|"out", id, from, to, subject, body, ts}` sorted by `ts`;
received side = `inbox/` + `archive/` (full bodies), sent side = `outbox.log` with `from`
filled in as `name`. All transcript timestamps originate from `write_message`, so they
share one format and sort correctly as plain strings.

**6. Teardown archive (D7, D22)**

```
archive_mailbox(root, name) -> Path | None
```
`rename()` of the whole live dir to `<root>/_archive/<name>/`. Idempotent: live absent +
archived present → return the archived path; neither → `None`. Called after `docker rm`,
so nothing is writing into the dir.

**7. Per-edge hop counters (D16, D17, §10)**

```
edge_path(root, a, b) -> Path                                   # <root>/_edges/<sorted a>--<b>.json
read_edge(root, a, b) -> dict                                   # {count, limit, updated}; missing -> count 0
bump_edge(root, a, b, limit: int) -> tuple[int, bool]            # (hops, allowed)
```
- Endpoints sorted so both sides derive the same path.
- The stored `limit` is authoritative once written; `limit` from the caller only seeds an
  edge file that doesn't exist yet. With per-edge limits declared by whichever agent owns
  the edge (§C.1), the declaring side seeds and the replying side inherits — so the
  master's number governs both directions of the exchange.
- `bump_edge` persists **only when allowed**; a blocked send leaves the count at the
  ceiling, so every later send on that edge blocks too — the intended terminal state, and
  it can't grow unboundedly on a runaway.
- Read-modify-write with `tmp` + `rename()`; no lock, per §10 (a lost concurrent
  increment makes the ceiling slightly generous, and sends on an edge alternate by turn).
- P1 ships the counter only. The gate, hop-exemption and the `{hops, limit}` send return
  are P5 — which also reshapes `bump_edge` into a plain increment and moves the
  "may it pass" question into `edge_spent` (Appendix P5 §C.3). The cap-notice this note
  originally listed is gone: a spent edge delivers nothing.

**8. Config (§9, §10)**

`max_task_agents: int = 4` / `CLD_MAX_TASK_AGENTS`, and
`peer_absolute_limit: int = 10` / `CLD_PEER_ABSOLUTE_LIMIT` — the latter is now the
**fallback** used when a peer spec omits its own limit (§C.1). Both need: dataclass field
(new "task-agent" block next to the messaging fields), `_TOML_KEYS` entry, `_env_int` line
in `from_env`, commented entry in `cld/config.default.toml`, a row in CLAUDE.md's env
table, and coverage in `tests/test_config.py` (including the `_clear_env` fixture list).

## C. Decisions taken here (the design leaves these open)

1. **The hop limit is declared per edge, on the edge — amendment to §4/§10.** Instead of
   a peer *list* plus one per-agent `--peer-hops` scalar, each peer spec carries its own
   budget: `--peer <full-name>[:<hops>]`, repeatable (e.g.
   `--peer cld_agent_api_task:15`). Omitting `:<hops>` falls back to
   `cfg.peer_absolute_limit`. `--peer-hops` goes away; `--peers` (comma list) becomes the
   repeatable singular `--peer`, so there is no nested delimiter to parse. Docker
   container names cannot contain `:`, so the delimiter is unambiguous.

   *Why this is the right shape:* edges are declared **asymmetrically** — a name only
   exists once spawned, and `start` cannot pin a slug, so the later-spawned agent declares
   the edge and the earlier one participates by replying. Exactly one side therefore has
   an opinion about that edge's budget, which is the side the master just configured. A
   per-agent scalar would have applied one number to every edge an agent owns and left the
   replying side's own default to compete with it; per-edge removes the competition
   entirely. "Stored wins, caller seeds" survives as the file-level rule (§B.7) and is now
   trivially correct rather than a tie-break.

   *Consequences:* `meta.json`'s `peers` becomes a name → limit mapping and
   `peer_hops_limit` disappears (a shape change to D24's field list, same field name).
   Validation at spawn (P4): positive-int limits, a duplicate peer name is an error rather
   than last-wins. The P5 gate resolves *its* side's limit as
   `meta["peers"].get(peer, cfg.peer_absolute_limit)`, which also covers replying on an
   edge the agent never declared.
2. **Archive collision → suffix the newcomer + WARN.** Re-using a task slug after
   teardown would otherwise merge two different agents' conversations into one archived
   dir. The second archive lands at `_archive/<name>-2` with a warning; the resolver
   keeps returning the unsuffixed one. Accepted asymmetry for a case §4 already tells the
   master to avoid (prefer fresh slugs).
3. **Outbox keeps full bodies, untruncated.** That is the cost of D27's single-mailbox
   transcript; a task prompt duplicated into the log is small next to a container.
4. **No guard against a reserved-name recipient.** Nothing stops an agent from naming
   `_edges` as a recipient and creating `_edges/inbox/`. The master draws the graph, so
   this is not reachable by accident; adding validation would be defensive code for an
   impossible state.
5. **Meta presence is the task-agent marker** in the mailbox tree (see `list_fleet`),
   rather than duplicating the docker `kind` label into a file.

## D. Seams P1 leaves for later parts (nothing here is a TODO in the code)

- `write_message` gains an optional `hops` audit stamp in **P5**, not now.
- `ensure_meta` gets its only caller in **P3** (supervisor boot).
- `list_fleet` + `read_state` get callers in **P2** (live-anchor refusal), **P4** (roster,
  reap check 1), **P5** (digest).
- `archive_mailbox` gets its caller in **P4** (shutdown path) and the broker's shutdown
  path in **P6**.
- `bump_edge` gets its caller in **P5** (the `send()` gate).

## E. Cross-part edits P1 makes deliberately

Deleting `outbox_changed_since` breaks its one consumer, so P1 also does the two-line swap
in `agent_loop.process_one` (snapshot stays; the check becomes `replied_since(...,
msg["from"])`) and switches `agent_loop`'s inline `state.json` literal to `state_path`.
Rationale: every commit stays green, and the reply-guarantee bug is real for today's
repo-agent too, not only for task-agents. `agent_loop`'s private `_atomic_write_json` is
left alone — P3 owns that file's restructuring and can dedupe it against the mailbox
helper then.

## F. Commit sequence (each step green on its own)

1. **config**: two knobs + default TOML + CLAUDE.md rows + `test_config.py`.
2. **outbox schema + reply check**: richer outbox line, `replied_since`, delete
   `outbox_changed_since`, swap the `agent_loop` call site, update `TestOutboxSnapshot`,
   add the D21 regression test in `test_agent_loop.py`.
3. **meta + fleet**: `ensure_meta` / `read_meta` / `list_fleet`, reserved-name skipping,
   `state_path` / `read_state` (+ `agent_loop` line).
4. **archive + transcript**: `archive_mailbox`, `resolve_mailbox_dir`, `transcript`.
5. **edges**: `edge_path` / `read_edge` / `bump_edge`.
6. **docs**: mailbox module docstring (layout now includes `meta.json`, `state.json`,
   `_archive/`, `_edges/`) and CLAUDE.md's messenger bullet, whose stated reply rule
   ("snapshots its own outbox line count and checks it grew") becomes wrong at step 2.

## G. Test matrix (`tests/test_mailbox.py` unless noted)

| Area | Cases |
|---|---|
| `ensure_meta` | writes once; second call with different values returns the **original**; `read_meta` round-trip incl. the `peers` name→limit mapping; missing → `None` |
| `list_fleet` | filters by parent; excludes dirs without meta (master/repo-agent); excludes `_archive`/`_edges`; injects `name` |
| `read_state` | missing file → `None`; unreadable → `None`; round-trip of a written state |
| `replied_since` | reply to the sender → True; send to a **peer** only → False; no sends → False; pre-snapshot line to that recipient → False; malformed line skipped, later good line still found |
| outbox line | contains subject+body; legacy `{id,to,ts}` line still parses in transcript |
| `transcript` | interleaves in/out by ts; includes archived received; reads an archived mailbox by name; unknown name → empty; direction/`from` normalization |
| `archive_mailbox` | moves dir; idempotent second call; unknown name → `None`; collision suffixes + warns |
| edges | path symmetric under argument order; first bump seeds limit; increments persist; bump past limit → `allowed=False` and count not advanced further; stored limit beats caller's (the replying side inherits the declarer's budget) |
| `test_agent_loop.py` | **D21 regression:** message from `sender`, stub replies to `peer` → `sender` gets the synthesized fallback and `peer` got the stub's message |
| `test_config.py` | defaults 4 / 10; TOML override; `CLD_*` env override; both keys accepted without an "unknown key" warning |

Run: `poetry run pytest tests/test_mailbox.py tests/test_config.py tests/test_agent_loop.py -q`,
then the full suite before declaring P1 done.

## H. Risks

- **Silent consumer of the old outbox shape:** the test stub writes outbox lines by hand.
  It stays compatible (it writes `to`), but any *other* hand-rolled writer would not —
  grep before finishing step 2. Checked today: only the stub.
- **`_archive`/`_edges` inside the mailbox root** are new non-mailbox entries; anything
  that enumerates the root must skip them. Today only `list_fleet` (new) enumerates it;
  P4/P5 must keep using it rather than globbing.
- **Fleet listing cost** is one `meta.json` read per dir. Bounded by `max_task_agents`
  live plus archived-name dirs (which sit under `_archive/`, so they are not walked).

## I. Outcome (2026-08-11)

Landed as planned, all six steps. `cld/messenger/mailbox.py` gained
`resolve_mailbox_dir`, `replied_since` (+ private `_read_outbox`), `ensure_meta`,
`read_meta`, `list_fleet`, `state_path`, `read_state`, `archive_mailbox`, `transcript`,
`edge_path`, `read_edge`, `bump_edge`, plus private `_read_json` / `_write_json_atomic`;
`outbox_changed_since` is gone and outbox lines now carry subject+body.
`cld/config.py` gained `max_task_agents` (4) and `peer_absolute_limit` (10).
`agent_loop.py` took the two planned one-line edits (recipient-scoped check,
`mailbox.state_path`). Docs: CLAUDE.md config table + messenger bullets, mailbox module
docstring, and the design doc amended for D28 (per-edge `--peer` budgets) plus the
edge-asymmetry note in §7 and the stored-limit-wins rule in §10.

**Test state:** 306 passed (was 279 — 27 new: 21 mailbox, 3 config, 1 agent_loop
regression, 2 outbox/meta round-trips). Same **4 pre-existing failures** as before this
work, none of them in code P1 touched:

| Failure | Cause |
|---|---|
| `test_agent_loop.py::TestKickoff::test_persona_substituted_into_prompt` | asserts the kickoff prompt is in `argv` after `-p`, but `_run_claude` passes it on **stdin**; the assertion reads `--output-format` |
| `test_cli.py::TestBuildCommand::test_build_help` | expects `no-cache` in Typer's help output, which now wraps/ANSI-colors it |
| `test_prompts.py::TestResolvePromptRef` (×2) | tests unpack a tuple from `resolve_prompt_ref`, which returns a single `Path` |

The first one is a **P3 prerequisite**: kickoff-prompt composition is P3's deliverable and
that test is its natural guard, so P3 should fix it by teaching the stub `claude` fixture
to log stdin (e.g. `STUB_PROMPT_LOG`) and asserting against that. Left alone here to keep
P1's diff to its own scope. The other two are unrelated to task-agents.

**Deviation from the plan:** none of substance. The one shape change is that the private
outbox reader takes the `outbox.log` *path* rather than `(root, name)`, so `transcript`
can read an archived mailbox with the same helper.

---

# Appendix P2 — detailed implementation plan

## A. Ground truth (verified in the tree and empirically, not assumed)

**`build_container_args` role handling** (`cld/docker.py:348-555`). One `if master and agent:
raise` guard, then `if master or agent:` gates *three* separate things that a task-agent
also needs: `--name` + the `org.cld.*` label set + the mode env (l.381-389), and the
mailbox mount (l.492-514). `stage_broker` is `if master:` only — correct, a
task-agent gets no host channel. Everything else (task file mount, `-p`, model, persona
mount, ssh-agent) is added by the *launcher*, not here: see `cld/run.py:76-92` and
`cli.py:344-367`.

**The enumerator is probably broken, and the cap check depends on it.**
`_docker_kind_list` (`docker.py:593-621`) inspects with
`{{index .Labels "org.cld.repo-root"}}`, but `docker inspect` on a *container* exposes
labels at `.Config.Labels` — there is no top-level `.Labels` (that's a `docker ps`
format field). A bad field makes the template error, `inspect.returncode != 0`, and the
loop `continue`s — so `docker_master_list()` / `docker_agent_list()` plausibly return
`[]` and `cld master|agent shutdown --all` silently finds nothing. The parallel code in
`host_docker.py:102` uses `.Config.Labels`, which is the tell. **Unverified here — this
container has no docker daemon.** Existing coverage can't catch it
(`test_docker_integration.py::TestDockerAgentHelpers::test_list_returns_list` only
asserts the return is a list).

**The entrypoint would eat the task before the supervisor starts.**
`entrypoint-claude-devcontainer.sh:126-167` composes `AGENT_INLINE_PROMPT` + `/config/task.md`
into `COMPOSED_PROMPT` and runs `claude -- "$COMPOSED_PROMPT"` **before** the
`MASTER_MODE` / `AGENT_MODE` branches. For a task-agent the task must reach the
supervisor's composed kickoff (§11), not a one-shot pre-run, so task mode has to skip
that block. (Today's `cld agent -p …` does hit it — pre-existing behavior, left alone.)

**Readiness sentinel needs nothing new.** The `AGENT_MODE` branch touches
`/tmp/cld-agent-ready`; `/tmp` is per-container, so a task-agent reusing that path can't
collide with anything. P4 waits on the same file.

**jj semantics, verified with jj 0.37 in a scratch repo:**
- `X & (a1:: | a2::)` is non-empty exactly when `X` is a descendant **or equal** to some
  `a`, and empty otherwise — precisely §9's live-stack test, in one query.
- `jj bookmark create <name>` fails with "Bookmark already exists" if it does, so the
  deliverable bookmark must be created only when absent (reuse the entrypoint's existing
  `jj bookmark list -T 'name ++ "\n"' | grep -qx` idiom).
- `ssh-keyscan` and `ssh` are present in the image (`/usr/bin`) — pulled in by `git`'s
  Recommends, not an explicit dependency; `cld broker` already relies on the same.
- The repo's SSH remote is readable as `jj git remote list` → `origin git@host:path`.

**Other constraints:** `_handle_errors` (`cli.py:55-64`) already turns `RuntimeError` /
`ValueError` into a logged `Exit(1)`, so the new spawn checks should just raise.
`_forget_session_state` (`cli.py:683`) already does bookmark-forget + workspace-forget
and is what P4 will reuse. `get_backend(start)` **ignores `start` when `WORKSPACE_ORIGIN`
is set** (in-container), which is fine host-side and safe in tests (conftest's
`clean_env` unsets it). `stage_ssh_agent` is role-agnostic and `_run_persistent_devcontainer`
calls it for the headless agent too (`cli.py:367`) — so CLAUDE.md's "devcontainer only
(never headless `cld agent`)" note on `CLD_SSH_AUTH_SOCK` is **stale**, and task-agents
will legitimately want the socket for §9's push path.

**No docker daemon in this container.** Everything P2 adds must be unit-testable without
one; the entrypoint changes are verifiable only by `bash -n` plus a host-side e2e run
(checklist in §G).

## B. Deliverables — exact surface

**1. The spawn-fact carrier (`cld/docker.py`)**

```
@dataclass(frozen=True)
class TaskAgentSpec:
    slug: str
    parent_master: str          # "" when launched by a human on the host
    deliverable_branch: str
    peers: dict[str, int]       # peer full name -> that edge's hop budget
```
Passed as `build_container_args(..., task_agent: TaskAgentSpec | None = None)`; its
truthiness replaces the `task_agent=True` of §12 (see §C.1).

**2. Naming (`cld/docker.py`)**

```
task_agent_container_name(repo_root: Path, slug: str, suffix: int = 0) -> str
allocate_task_agent_name(repo_root: Path, slug: str) -> str
```
`cld_agent_<repo>_<slug>`, plus `-2`, `-3`, … from `allocate_*`, which probes
`_docker_status(...) != "absent"` (docker is the liveness ground truth; a stale archived
mailbox must not block a name). Slug validated `^[a-z0-9][a-z0-9-]*$` as a guard clause
with a clear `ValueError` — an invalid slug otherwise surfaces as an opaque `docker run`
failure.

**3. Role wiring in `build_container_args`**

- Guard becomes "at most one of master / agent / task_agent".
- `kind` label 3-way: `master` | `agent` | `task-agent`.
- Task mode env: `AGENT_MODE=1` **and** `TASK_AGENT_MODE=1` (§C.4).
- Extra labels: `org.cld.task=<slug>`, `org.cld.parent-master=<parent or "">`.
- Extra env carrying the spawn facts the supervisor turns into `meta.json`:
  `AGENT_DELIVERABLE_BRANCH`, `AGENT_PEERS`, `AGENT_PARENT_MASTER`, `AGENT_TASK_SLUG`.
- `CLD_PEER_ABSOLUTE_LIMIT` propagated into the container: in-container `Config.from_env()`
  sees no host user-TOML (`.config/cld` isn't among `home_mounts_always`), so without this
  the operator's configured fallback silently reverts to the dataclass default.
- Mailbox mount gate extended to task-agents (they are mailbox-driven by definition).

**4. Enumeration (`cld/docker.py`)**

```
_docker_kind_list(kind: str, *, running_only: bool = False) -> list[dict]
docker_task_agent_list(*, running_only: bool = False) -> list[dict]
```
Records gain `kind`, `parent`, `task` (existing `name` / `repo_root` / `session` keys stay,
so `docker_master_list` / `docker_agent_list` callers are untouched). `running_only` adds
`--filter status=running` rather than parsing status text — §9 requires counting **running**
containers only. Inspect template switched to `.Config.Labels` (§A).

**5. Spawn-time refusals (`cld/docker.py`)**

```
assert_task_agent_capacity(cfg: Config, parent_master: str) -> None
resolve_task_agent_anchor(cfg: Config, repo_root: Path, revision: str) -> str
```
- Capacity: count running task-agents whose `org.cld.parent-master` equals
  `parent_master`; over `cfg.max_task_agents` → `RuntimeError` naming the running agents
  and their tasks.
- Anchor: resolve `revision` → `X` via `resolve_anchor`, collect `meta.json` `anchor`
  values for every **running** task-agent in **this repo** (§C.3), drop empties, and run
  one revset (`X & (a1:: | a2:: | …)`). Non-empty → `RuntimeError` naming the owning agent
  and telling the caller to reap it first. Returns `X` so the caller doesn't resolve twice.
  jj-only: on a git backend, log and skip. (The note "peer-side staging is jj-only anyway" is
  superseded: a git ticket now stages a scratch commit too, see docs/design-ticket-containers.md
  section 3; the overlap check and task-agents are still jj only.)

**6. Entrypoint (`imgs/claude-devcontainer/entrypoint-claude-devcontainer.sh`)**

One task-mode block after the workspace/anchor setup and `cd /workspace/current`, plus one
guard:
- Create `$AGENT_DELIVERABLE_BRANCH` at `$AGENT_ANCHOR_HASH` **iff** it doesn't already
  exist and both values are non-empty (an anchor-recovery failure must not turn into
  `jj bookmark create X -r ""`). Covers all three boot paths (first launch, reattach, warm
  restart) because it reads the recovered anchor rather than re-deriving it.
- Skip the `COMPOSED_PROMPT` pre-run when `TASK_AGENT_MODE` is set (§A).
- Seed `~/.ssh/known_hosts` from the repo's SSH remote via a new `seed_known_hosts` helper
  in `container-init.sh` (mkdir 700 / file 600, non-fatal on no remote, an https remote, or
  no network — §C.7).

**7. Docs**

CLAUDE.md: container-side env table gains `TASK_AGENT_MODE`, `AGENT_DELIVERABLE_BRANCH`,
`AGENT_PEERS`, `AGENT_PARENT_MASTER`, `AGENT_TASK_SLUG`; the stale `CLD_SSH_AUTH_SOCK`
"never headless agent" parenthetical is corrected.

## C. Decisions taken here

1. **`TaskAgentSpec` instead of `task_agent=True` + four loose kwargs.** Four correlated
   values (slug, parent, branch, peers) all arrive together and are all spawn facts;
   threading them as separate keyword args bloats the signature and lets callers pass a
   half-populated set. Deviation from §12's literal signature, same behavior.
2. **A human-launched task-agent has an empty parent.** The alternative — auto-attributing
   it to whatever master exists for that repo — would silently hand the master authority to
   reap an agent it never spawned (§7's "own fleet only" check keys on exactly this label).
   Empty is honest: the agent is visible via `cld task-agent status` (labels) and simply
   isn't in any fleet. The cap then applies per parent value, empty included — the cap's
   real justification is shared-store contention (§9), which doesn't care who spawned what.
3. **The live-stack anchor refusal is scoped by repo, not by fleet.** §9 says "running fleet
   member", but the hazard is store-level: an anchor inside *any* live agent's stack in that
   repo gets rewritten under you, whether or not the same master owns it. Repo-scoping is a
   strict superset of the design's rule and costs nothing.
4. **`TASK_AGENT_MODE` is a modifier on `AGENT_MODE`, not a third mode.** Task-agents want
   everything the headless agent branch already does — mailbox precondition, readiness
   sentinel, `exec` the supervisor — so a separate top-level branch would duplicate three
   things to change one. The supervisor reads `TASK_AGENT_MODE` itself in P3.
5. **`AGENT_PEERS` is comma-separated `name:hops`.** The repo's list convention is
   colon-separated (`WORKSPACE_FILES`, `MASTER_TARGETS`), but `:` is now the name/limit
   delimiter, so the list separator has to be something else.
6. **The deliverable bookmark is created at the anchor B and never moved by cld.** It gives
   the branch a base to exist at; only the agent moves it (wrap-up squash), and only the
   caller deletes the *session* bookmark. Create-if-absent keeps restart/reattach from
   rewinding a branch that already advanced.
7. **`known_hosts` seeding is task-agent-only for now.** `cld agent` has the same gap
   (memory note: *agent container has no known_hosts*), but widening it is behavior change
   outside P2's scope; the helper lands in `container-init.sh` so a later part can call it
   from the plain agent branch with one line.
8. **The `.Labels` → `.Config.Labels` fix ships in P2.** The cap check is only as good as
   the enumerator under it; shipping a refusal that can never fire would be worse than not
   shipping it. Comes with a test asserting the template mentions `.Config.Labels`, since
   this environment can't exercise a daemon.

## D. Seams and open questions for later parts

- **P3 must write `meta.json`** before the anchor refusal can ever fire — until then there
  are no anchors to test against and every spawn passes. P2 tests it with synthetic meta
  files; the plan's ordering already puts P3 before P4's first real spawn.
- **P4 needs a slug input, and §4's grammar has none.** The master "generates the slug from
  the task", but `cld task-agent start` as specced takes only persona/prompt/branch/model/
  revision/peer. Recommendation: accept `-n/--name` as the slug (consistent with every other
  `cld` command, and it makes the pinned-name option from the `--peer` discussion available);
  fall back to deriving a slug from `--branch` if omitted. Flagging now, deciding in P4.
- **P4 launcher** owns the persona and task mounts, following `cld run`: persona at
  `/config/persona.md` with `AGENT_PERSONA_FILE` + `AGENT_PERSONA` (name, for `meta.json`),
  task file at `/config/task.md`, `AGENT_INLINE_PROMPT`, `AGENT_MODEL`, and
  `stage_ssh_agent`. P2 defines that wire but does not emit it.
- **P6** reuses `docker_task_agent_list`'s richer records for the broker's extended
  `list-containers`, and calls the same two refusals on the broker's launch path.

## E. Commit sequence (each step green on its own)

1. **Enumerator**: `.Config.Labels` fix + `running_only` + `kind`/`parent`/`task` in the
   records + `docker_task_agent_list`, with unit tests over a mocked `subprocess.run`.
2. **Naming**: `task_agent_container_name`, slug validation, `allocate_task_agent_name`.
3. **Role wiring**: `TaskAgentSpec`, the `build_container_args` branch, mailbox gate,
   `CLD_PEER_ABSOLUTE_LIMIT` propagation.
4. **Refusals**: `assert_task_agent_capacity`, `resolve_task_agent_anchor`.
5. **Entrypoint + container-init**: deliverable bookmark, task-mode prompt skip,
   `seed_known_hosts`; `bash -n` on both scripts; CLAUDE.md env-table rows.

## F. Test matrix

New `tests/test_docker.py` classes — all daemon-free, which is what makes them runnable
here (the existing `TestBuildContainerArgs` lives in `test_docker_integration.py` behind
`skip_no_docker`; task-agent arg assertions go in the unit file instead, and the plan notes
why):

| Area | Cases |
|---|---|
| naming | `cld_agent_<repo>_<slug>`; suffix form; invalid slug raises; `allocate_*` returns base name when free, `-2` when taken, `-3` when both taken (patched `_docker_status`) |
| role wiring | task-agent sets `kind=task-agent`, `org.cld.task`, `org.cld.parent-master`, `--name`, `AGENT_MODE=1`, `TASK_AGENT_MODE=1`; deliverable/peers/parent/slug env present and correctly encoded (`a:10,b:5`); mailbox mount present; **no** `--rm`; **no** broker key; `CLD_PEER_ABSOLUTE_LIMIT` propagated |
| exclusivity | any two of master/agent/task_agent raises `ValueError` |
| enumeration | template contains `.Config.Labels`; `running_only` adds `--filter status=running`; records carry kind/parent/task; `docker ps` failure → `[]` |
| capacity | under cap passes; at cap raises naming the running agents; different parents counted separately; empty-parent group counted on its own; stopped containers ignored |
| anchor refusal | anchor on the shared base passes with a live sibling (the case §9 calls out); anchor inside a live agent's stack raises naming the owner; anchor **equal** to a live anchor raises (inclusive `::`); empty/missing meta anchors ignored; no live agents → passes; git backend → skipped. Uses the real `jj_repo` fixture + synthetic `meta.json` files + patched enumerator, so it runs without docker |
| scripts | `bash -n` on the entrypoint and `container-init.sh` |

**Host-side e2e checklist** (cannot run in this container — record results in Scratch):
spawn a task-agent on a real repo; assert `docker inspect` shows the four labels; assert
`jj bookmark list` in the origin shows both `<session>` and the deliverable branch, the
latter at the anchor commit; assert the container reached `/tmp/cld-agent-ready` and the
supervisor is the PID-1 process; assert the task text was **not** consumed by a pre-run
`claude`; assert `~/.ssh/known_hosts` is non-empty when the repo has an SSH remote.

## G. Risks

- **No daemon here.** Steps 1-4 are fully unit-tested; step 5 gets `bash -n` and the e2e
  checklist above. The `.Labels` finding stays "likely" until someone runs it on a host.
- **`advance-bookmarks`.** If the origin's jj config enables bookmark auto-advance, a
  commit could move a bookmark that points at `@-` — which the deliverable bookmark does
  right after creation. Worth checking on the first real run; the fix would be to create it
  at B but keep the agent's first change a child of B (already true).
- **`ssh-keyscan` arrives via `git`'s Recommends.** Adding `--no-install-recommends` to the
  base image later would break both `seed_known_hosts` and `cld broker`.
- **Labels are host-set and unforgeable from inside a container**, which is what lets the
  cap and the parent check be trusted; keep any future "parent" input on the *launcher*
  side, never read from the container's own env.

## H. Outcome (2026-08-12)

All five steps landed. 347 tests pass (was 306 after P1 — 41 new), with the **same 4
pre-existing failures** and no new ones.

`cld/docker.py`:
- `_docker_kind_list(kind, *, running_only=False)` now inspects `.Config.Labels` and
  returns `{name, repo_root, session, kind, parent, task}`; `docker_task_agent_list`
  wraps it for the new kind.
- `TaskAgentSpec` (frozen, with `peers_env()`), `task_agent_container_name`,
  `allocate_task_agent_name`, `assert_task_agent_capacity`, `resolve_task_agent_anchor`.
- `build_container_args(..., task_agent=spec)`: 3-way role exclusivity, `kind=task-agent`,
  `org.cld.task` / `org.cld.parent-master`, `AGENT_MODE=1` + `TASK_AGENT_MODE=1`, the four
  spawn-fact env vars, `CLD_PEER_ABSOLUTE_LIMIT` propagation, mailbox mount.

`imgs/claude-devcontainer/`: entrypoint creates the deliverable bookmark at the anchor
(create-if-absent, warns when no anchor was recovered), calls `seed_known_hosts`, and skips
the one-shot `COMPOSED_PROMPT` pre-run in task mode. `container-init.sh` gained
`seed_known_hosts`.

CLAUDE.md: two new container-env rows; the stale `CLD_SSH_AUTH_SOCK` "never headless
`cld agent`" claim corrected.

**Two things the tests taught us, both worth remembering:**

1. **`"0" * 40` is jj's root commit**, an ancestor of everything — so a "stale anchor"
   test written with it makes the live-stack refusal fire correctly and looks like a bug in
   the check. Absent-hash tests must use something like `"dead" * 10`. (The refusal itself
   was right; the first version of the test was wrong.)
2. **`ssh-keygen -F` resolves `~` from the passwd database, not `$HOME`**, so the
   "already seeded" short-circuit silently read the wrong file and appended duplicate keys
   on every boot. Fixed by pinning `-f ~/.ssh/known_hosts`; verified idempotent against the
   real repo remote (6 lines after the first run, 6 after the second).

`seed_known_hosts` was exercised for real in this container: it seeds `github.com` from the
repo's `origin`, sets 700/600 perms, is idempotent, and skips https and local-path remotes.
All four remote URL forms (`git@host:path`, `ssh://user@host:port/path`, `ssh://host/path`,
https) parse as intended.

**Deviations from the plan:** none in substance. Two small refinements: the live-stack
refusal keeps the fast path at **one** revset query and only runs per-anchor probes when it
is about to refuse (so the error can name the owning agent), and it **fails open with a
warning** when a recorded anchor no longer resolves in the store — that is our own
bookkeeping failing, not a real hazard, and blocking a legitimate spawn over it would be
worse.

**Still unverified (needs a daemon):** the `.Labels` → `.Config.Labels` finding from §A is
now fixed and guarded by a test asserting the template, but the original breakage was never
reproduced here. The e2e checklist in §F is untouched — run it on a host before trusting
`cld task-agent start` end to end.

---

# Appendix P3 — detailed implementation plan

## A. Ground truth (verified in the tree, not assumed)

**The supervisor today** (`cld/messenger/agent_loop.py`, 270 lines). `AgentSupervisor`
takes `persona_path` and, in `kickoff()`, does
`Template(persona_path.read_text()).safe_substitute(REPO_BASENAME, REPO_ABS_PATH,
MAX_TURNS, CONTAINER_NAME, AGENT_ANCHOR_HASH)` — the anchor is read from `os.environ`
*inside* the method. `_run_claude` passes the prompt on **stdin** (`input=prompt`), never
in argv. `main()` resolves the persona as
`_CLD_ROOT/"prompts"/"personas"/f"{cfg.agent_kickoff_persona}.md"` (default `agent`).
P1 already switched the reply check to `mailbox.replied_since` and the state path to
`mailbox.state_path`.

**Why the prompt is on stdin, and why one test fails.** Memory + the design history: the
persona starts with `---` frontmatter, and when the prompt was an argv token claude's
option parser read `---…` as an unknown option and exited 1, wedging the agent at
`phase="kickoff"`. The fix moved the prompt to stdin. `test_persona_substituted_into_prompt`
still reads `argv[argv.index("-p") + 1]`, so it asserts against `--output-format` — it has
been testing nothing since that fix. **This is the P3 prerequisite**: the stub `claude`
fixture (`tests/fixtures/stub-messenger-agent/claude`, used *only* by
`tests/test_agent_loop.py`) must log stdin for kickoff composition to be testable at all.

**Frontmatter is still in the prompt.** `prompts/personas/agent.md` begins with a `---`
YAML block, and `kickoff()` does not strip it, so today's kickoff prompt opens with
metadata meant for discovery. Harmless over stdin, but noise — and `cld/prompts.py`
already has `stage_persona_without_frontmatter`, whose stripping logic is file-to-file and
so unusable for in-memory composition.

**Persona resolution is host-side, per P2's wire.** `cld/chain.py:persona_resolve(name,
repo_root, cld_root)` searches `<root>/prompts/personas/<name>[.md]`, repo first. P4 calls
it, mounts the file at `/config/persona.md`, and sets `AGENT_PERSONA` + `AGENT_PERSONA_FILE`
— so **P3 needs no resolver**, only to read the mounted file. That also gives fail-fast at
spawn instead of a container that boots and dies.

**The image already bakes what P3 adds.** `prompts/` → `/opt/cld/prompts/` (base
Dockerfile l.119) and `prompts` is in `base_extra_paths`, so adding
`prompts/personas/task-agent.md` changes the content hash and `ensure_image` rebuilds
automatically. `PYTHONPATH=/opt/cld` plus the entrypoint's `python3 -P` keeps the baked
supervisor from being shadowed by a `cld/` in the target repo.

**P2 supplies exactly these env vars:** `TASK_AGENT_MODE`, `AGENT_TASK_SLUG`,
`AGENT_PARENT_MASTER`, `AGENT_DELIVERABLE_BRANCH`, `AGENT_PEERS` (`name:hops` pairs,
comma-separated), `CLD_PEER_ABSOLUTE_LIMIT`, plus the pre-existing `SESSION_NAME`,
`AGENT_MODEL`, `AGENT_ANCHOR_HASH`, `AGENT_INLINE_PROMPT` and the `/config/task.md` mount.
`docker.TaskAgentSpec.peers_env()` writes that encoding; the parse counterpart doesn't
exist yet.

**Stale doc spotted:** CLAUDE.md's messenger section says kickoff runs "via
`prompts/personas/repo-agent.md`" — the file is `agent.md` (and `cfg.agent_kickoff_persona`
defaults to `agent`).

## B. Deliverables — exact surface

**1. `cld/prompts.py` — reusable stripping**

```
strip_frontmatter(text: str) -> str
```
Extracted from `stage_persona_without_frontmatter`, which then calls it (one behavior, one
implementation). Used by the composer for both prompt layers.

**2. `cld/docker.py` — the parse counterpart to `peers_env()`**

```
parse_peers_env(value: str) -> dict[str, int]
```
Lives next to `TaskAgentSpec.peers_env` so the round-trip is obvious and testable in one
place. Ignores empty segments; a malformed pair raises `ValueError`.

**3. `cld/messenger/agent_loop.py` — task mode**

```
@dataclass(frozen=True)
class TaskMode:
    slug: str
    parent_master: str
    deliverable_branch: str
    peers: dict[str, int]
    persona_name: str
    persona_path: Path
    preamble_path: Path
    task_text: str
    anchor: str

    @classmethod
    def from_env(cls) -> "TaskMode": ...
```
- `from_env` is the only env/file reader: it composes `task_text` from
  `AGENT_INLINE_PROMPT` + `/config/task.md` using the *same* convention the entrypoint uses
  for other modes (file body, then `## Additional Instructions`, then the inline prompt),
  and guard-clauses the structural requirements — slug, deliverable branch, a readable
  persona file, a baked preamble, and a non-empty task (a task-agent with no task is
  meaningless; `cld run` already refuses the same way).

```
compose_kickoff(task: TaskMode, *, session_name, repo_root, max_turns) -> str
```
Pure text assembly, no env, no I/O beyond reading the two prompt files: preamble →
persona → task, per §11. Both prompt layers are frontmatter-stripped and
`Template.safe_substitute`d with one shared map (`REPO_BASENAME`, `REPO_ABS_PATH`,
`MAX_TURNS`, `CONTAINER_NAME`, `AGENT_ANCHOR_HASH`, `DELIVERABLE_BRANCH`, `PARENT_MASTER`,
`TASK_SLUG`, `PERSONA`, `PEERS`). The **task text is appended verbatim** under its own
heading — it is user content, not a template, so a `$VAR` in a task description must
survive untouched.

`AgentSupervisor` gains two optional params: `task: TaskMode | None = None` and
`anchor: str = ""`.
- `anchor` replaces the `os.environ` read inside `kickoff()` — same value, passed by
  `main()` for both modes, so composition is testable without monkeypatching env.
- `__init__` writes `meta.json` via `mailbox.ensure_meta` when `task` is set (right after
  `ensure_mailbox`): boot-time, available to the master even if kickoff then fails, and
  write-once so a warm restart preserves the original facts (D24).
- `kickoff()` branches only on `self.task`: composed prompt in task mode, today's
  single-persona prompt otherwise. **No new phase**, no other state-machine change (§5).
- Frontmatter stripping applies in *both* modes — the block is discovery metadata that was
  never meant to be prompt content, and it has already caused one incident.
- `main()` builds `TaskMode.from_env()` when `TASK_AGENT_MODE` is set and passes it through.

**4. `prompts/personas/task-agent.md` — the lifecycle preamble (§11)**

The bounded-task contract, replacing `agent.md`'s "for as long as this container runs"
framing: you are scoped to one task; the master drives you and decides when to wrap up;
wrap-up means squashing your work into `${DELIVERABLE_BRANCH}` (and pushing it when the
master asks, for cross-repo verification) and reporting it; a reply is a `send()` and must
go to the sender of the message you are answering — a send to a peer does **not** discharge
a reply owed to the master; you may message only `${PARENT_MASTER}` and the peers in
`${PEERS}`, nobody else, and you never spawn agents; peer edges carry an absolute hop
budget, every `send()` return tells you where you stand, converge as it nears the limit and
**escalate to the master rather than retry** a blocked send; the master channel is never
budgeted; you keep memory across messages; only descendants of `${AGENT_ANCHOR_HASH}` are
yours and `jj workspace` belongs to the framework.

**5. Fixture + docs**

Stub `claude` gains `STUB_PROMPT_LOG` (write stdin to that path) — additive, env-gated, and
the stub is used by one test file only. CLAUDE.md: `repo-agent.md` → `agent.md`, and a
sentence on task-mode kickoff composition.

## C. Decisions taken here

1. **`TaskMode` + `compose_kickoff` rather than a second supervisor or a pre-composed
   prompt string.** D12 wants one supervisor with only identity/launch/cleanup branching.
   A dataclass keeps nine correlated values together, `from_env` isolates all env reading
   (so tests build a `TaskMode` directly), and `compose_kickoff` is pure — the part most
   worth testing is the part with no I/O.
2. **The task text is appended verbatim, never templated.** Substituting the task would
   silently rewrite `$` in a user's task description, and `safe_substitute` on unknown keys
   leaves confusing half-substituted text. Only the two prompt layers we ship are templates.
3. **`anchor` becomes a constructor param.** It's the same value from the same env var, but
   read at the edge (`main()`) instead of inside `kickoff()`, which is what makes task-mode
   composition testable without env patching.
4. **`meta.json` is written in `__init__`, not in `run()`/`kickoff()`.** Boot means boot:
   the master's roster should see a spawned agent's facts even if its first Claude call
   fails. `ensure_meta`'s write-once semantics make the warm-restart case correct for free.
5. **Frontmatter stripping applies to the existing repo-agent path too.** Same code path,
   and the alternative is deliberately keeping known-useless metadata in one mode's prompt.
6. **`meta.json`'s `task` holds the full task text.** It is a spawn fact and the honest
   record; the short human handle is the slug (already in `org.cld.task` and the container
   name). **Note for P5:** `fleet_digest` must truncate `task`, or a long task description
   will bloat the master's per-turn sweep — exactly what the digest exists to avoid.
7. **Composition of inline-prompt + task-file is duplicated** between the entrypoint (other
   modes) and `from_env` (task mode), deliberately: the alternative is the shell writing a
   composed file for Python to re-read, which trades a 6-line duplication for a file
   handoff between two processes.

## D. Seams for later parts

- **P4** must set `AGENT_PERSONA` + `AGENT_PERSONA_FILE` (mount at `/config/persona.md`) and
  a non-empty `AGENT_DELIVERABLE_BRANCH` / `AGENT_TASK_SLUG`, or the supervisor exits 1 with
  a named guard-clause error. It resolves the persona with `chain.persona_resolve` — worth
  moving that function to `cld/prompts.py` when P4 lands, since it will then have two callers
  and prompts.py is where prompt resolution lives.
- **P5** reads `meta.json`'s `peers` for the send gate's own-side limit and must truncate
  `task` in the digest (§C.6).
- The preamble's peer-budget wording assumes P5's `send()` return carries `{hops, limit}`;
  until P5 lands, that sentence describes an API that doesn't exist yet. Harmless (the
  agent simply won't see the numbers), and it keeps the persona from needing a second edit.

## E. Commit sequence (each step green on its own)

1. **`strip_frontmatter`** in `cld/prompts.py` + `stage_persona_without_frontmatter`
   refactor + tests.
2. **`parse_peers_env`** in `cld/docker.py` + round-trip tests against `peers_env()`.
3. **Stub stdin log** + rewrite `test_persona_substituted_into_prompt` to assert against it
   (fixes one of the four pre-existing failures, because P3 cannot verify kickoff
   composition without it).
4. **`TaskMode` + `compose_kickoff` + supervisor wiring** (`anchor` param, `meta.json` at
   init, task-mode kickoff, `main()` branch) + tests.
5. **`prompts/personas/task-agent.md`** + a test asserting every `${…}` placeholder in it is
   substituted by `compose_kickoff` (catches a variable-name typo, which is otherwise
   invisible until an agent reads a literal `${DELIVERABLE_BRANCH}`); CLAUDE.md touch-ups.

## F. Test matrix

| Area | Cases |
|---|---|
| `strip_frontmatter` | strips a leading block; leaves text without one; preserves a `---` *inside* the body; handles a block with no closing marker |
| `parse_peers_env` | round-trips `peers_env()`; empty string → `{}`; ignores trailing/duplicate commas; malformed pair (no colon, non-int hops) raises |
| `compose_kickoff` | layer order preamble → persona → task; every shared var substituted in **both** layers; task appended verbatim (a `$VAR` in the task survives); frontmatter gone from both layers; `PEERS` rendering for 0 / 1 / 2 peers |
| `TaskMode.from_env` | full env → all fields; task from inline only / file only / both (joined with `## Additional Instructions`); peers parsed; missing slug / branch / persona file / preamble / task each raise a distinct error |
| supervisor (task mode) | `meta.json` written at construction with parent/task/persona/branch/anchor/peers; second construction keeps the original; kickoff prompt (via `STUB_PROMPT_LOG`) contains all three layers; phases still `kickoff → idle`; `process_one` unchanged |
| supervisor (repo mode) | unchanged behavior, now with frontmatter stripped; `test_persona_substituted_into_prompt` asserts against the stdin log |
| persona file | no `${` placeholder survives `compose_kickoff`; the file exists where the image bakes it |

Run: `poetry run pytest tests/test_prompts.py tests/test_docker.py tests/test_agent_loop.py -q`,
then the full suite. Expected end state: **3** pre-existing failures instead of 4.

## G. Risks

- **The two `test_prompts.py::TestResolvePromptRef` failures are unrelated and stay.** They
  unpack `(path, kind)` from `resolve_prompt_ref`, which returns a bare `Path` — a contract
  that changed before this work started. P3 edits that file (adding `strip_frontmatter`
  tests) but will not touch them; say the word if you want them fixed in passing.
- **Image rebuild required** for the new preamble to exist in a container. Automatic via the
  content hash, but a stale local image means the supervisor exits 1 on a missing preamble —
  which is the intended loud failure, not a silent fallback.
- **`prompts/personas/task-agent.md` is prompt text, so it can only be reviewed, not
  tested,** beyond the placeholder check. Its wording is the whole behavioral contract for a
  task-agent; worth reading once as prose rather than as a diff.

## H. Outcome (2026-08-12)

All five steps landed. **389 tests pass (was 347) and the pre-existing failures are down
from 4 to 3** — `test_persona_substituted_into_prompt` is fixed, as planned, because
task-mode composition was untestable without it.

- `cld/prompts.py`: `strip_frontmatter(text)` extracted; `stage_persona_without_frontmatter`
  now calls it.
- `cld/docker.py`: `parse_peers_env(value)` next to `TaskAgentSpec.peers_env()`.
- `cld/messenger/agent_loop.py`: `TaskMode` (+ `from_env`), `_compose_task_text`,
  `_format_peers`, `compose_kickoff`; `AgentSupervisor` gained `task` and `anchor`, writes
  `meta.json` at construction in task mode, and branches only inside `kickoff()`; `main()`
  builds task mode when `TASK_AGENT_MODE` is set and exits 1 with a named error otherwise.
  Frontmatter is now stripped in **both** modes.
- `prompts/personas/task-agent.md`: the lifecycle preamble (§11).
- `tests/fixtures/stub-messenger-agent/claude`: `STUB_PROMPT_LOG` records stdin.
- CLAUDE.md: the messenger bullet now describes stdin delivery, frontmatter stripping and
  task-mode composition (and no longer claims kickoff uses `repo-agent.md`); the tree
  listing mentions both lifecycle personas.

**Two fixes prompted by reading the rendered prompt rather than the tests** — worth the
habit, since a persona is only as good as what actually reaches the agent:

1. **`REPO_BASENAME` said `current`.** The supervisor's `repo_root` is
   `/workspace/current`, so the preamble told the agent it was working in a repository
   called "current". `TaskMode` now carries `repo_name` from `CLD_HOST_PROJECT_DIR` (which
   the launcher sets on every container) and falls back to the workspace basename. The
   standing repo-agent persona has the same cosmetic issue via `agent.md`; left alone as
   out of scope.
2. **The peer list didn't nest.** `${PEERS}` rendered as a flat list separated by a blank
   line from the "these peers:" bullet, so it read as a sibling list. Peer lines are now
   indented two spaces and the blank line is gone.

Also verified: no *role* persona (architect/implementer/reviewer/…) uses `${…}`
placeholders, so only `agent.md` and `task-agent.md` depend on the substitution map — and
a test asserts the shipped preamble leaves no `${` behind after composition.

**Deviations from the plan:** only the `repo_name` field above (an addition to `TaskMode`
that the plan didn't anticipate). Everything else landed as specified.

**Not verified here:** the composed prompt has never been fed to a real claude, and the
new preamble needs an image rebuild to exist in a container (automatic via the content
hash). The wording is prose and should be read once as prose — `poetry run python -c` with
`compose_kickoff` prints it without a container.

---

# Appendix P4 — detailed implementation plan

> Scope: the `cld task-agent` command group (§4, §12) and the three reap-readiness
> checks (§7). Host-only; the in-master route is P6's.

## A. Ground truth (verified in the tree, not assumed)

1. **CLI group pattern.** `typer.Typer(help=…)` + `app.add_typer(x_app, name="…")`; each
   verb is `@x_app.command("verb")` + `@_handle_errors`. A verb-only group needs **no
   callback** (`master_app`/`agent_app` have one solely for their bare form). `_handle_errors`
   maps `RuntimeError`/`ValueError`/`CalledProcessError`/`FileNotFoundError` to
   `log.error` + exit 1 — so refusals raised from library code already render as clean
   CLI errors, and P2's two refusals need no wrapping.
2. **Reusable as-is:** `_wait_for_container_ready(name, sentinel, timeout=60)`;
   `_stop_and_remove_container(name)` (plain `docker stop` → SIGTERM → `docker rm`);
   `_forget_session_state(repo_root_str, session)` (forgets bookmark **and** workspace
   registration; warns rather than failing when the repo is gone or the backend isn't jj);
   `ensure_image`; `stage_home_ro`; `stage_ssh_agent`; `_format_age`.
3. **The readiness sentinel is `/tmp/cld-agent-ready`** — `TASK_AGENT_MODE` modifies the
   entrypoint's `AGENT_MODE` branch, which is what touches the sentinel and execs the
   supervisor. `_READY_SENTINEL` therefore needs no new entry; the task path just uses the
   agent one.
4. **`build_container_args(..., task_agent=spec)` already emits `--name`**, the four labels,
   `AGENT_MODE=1` + `TASK_AGENT_MODE=1`, the four spawn-fact env vars,
   `CLD_PEER_ABSOLUTE_LIMIT`, the mailbox mount, and **no `--rm`**. The launcher must *not*
   re-add `--name` (`launch_run` does, because the one-shot path doesn't take that branch).
5. **P2's refusals are plain library calls:** `assert_task_agent_capacity(cfg, parent_master)`
   and `resolve_task_agent_anchor(cfg, repo_root, revision) -> str` (raises on a live-stack
   anchor, otherwise returns the resolved hash).
6. **`resolve_anchor(vcs, "<hash>")` resolves a hash to itself**, so feeding P2's already
   resolved anchor into `anchor_env_args(cfg, session, anchor)` pins the launch to exactly
   the commit the refusal inspected, instead of resolving `-r` twice with a drift window
   between check and launch.
7. **`.gitconfig` is devcontainer-only** (`home_mounts_devcontainer`), while `.config/jj`
   is in `home_mounts_always` and so already handled inside `build_container_args`. A
   headless task-agent commits and may `jj git push`, so the launcher adds the devcontainer
   mounts exactly as `_run_persistent_devcontainer` does.
8. **Persona wire.** `cld run` mounts a system prompt at `/config/persona.md`; P3's
   `TaskMode.from_env` requires `AGENT_PERSONA_FILE` (must exist) and takes the display name
   from `AGENT_PERSONA`, falling back to the file stem. `compose_kickoff` already calls
   `strip_frontmatter`, so the launcher mounts the persona file **as-is** — no scratch copy,
   no `stage_persona_without_frontmatter`.
9. **`chain.persona_resolve(name, repo_root, cld_root)`** searches `<repo>/prompts/personas/`
   then `<cld_root>/prompts/personas/` and raises `FileNotFoundError` naming both. **No test
   imports it**, so P3 §D's requested move to `cld/prompts.py` is a two-line change plus one
   import fix in `chain.py`.
10. **A bare slug is recoverable from a container name.** The slug regex forbids `_`, so the
    slug is always `name.rsplit("_", 1)[-1]` even when the repo basename contains `_`; and
    for a known repo + slug the full name is computable with `task_agent_container_name`.
    That is what makes bare-slug resolution work for an agent whose container is gone (no
    label to read).
11. **Mailbox reads that exist:** `list_fleet(root, parent=None)`, `read_meta`, `read_state`,
    `transcript` (archive-aware via `resolve_mailbox_dir`), `archive_mailbox` (idempotent).
    `read_meta`/`read_state` are **live-dir only, deliberately**: making `read_meta`
    archive-aware would break `ensure_meta`'s write-once guard, since a re-used slug would
    find the dead agent's facts and skip writing the new ones.
12. **`state.json`'s `repo_root` is `/workspace/current`** — the in-container path, useless
    host-side. The host repo path of an agent comes from its `org.cld.repo-root` label.
13. **`_format_age` cannot parse mailbox timestamps.** Mailbox `_now_iso()` writes
    microseconds; `_format_age` parses `%S` only, so it silently returns the raw ISO string.
14. **Baseline: 389 passed, 41 skipped, 5 xfailed, 3 pre-existing failures**
    (`test_cli.py::TestBuildCommand::test_build_help`; two
    `test_prompts.py::TestResolvePromptRef` cases unpacking a tuple from a `Path`-returning
    function).
15. Typer **0.24.1**; the repo has no `list[str]` option yet, so `--peer`'s repeatability
    gets a one-command smoke check before anything is built on it.

## B. Deliverables — exact surface

All of it in **`cld/cli.py`**, in one delimited section after the agent group (§C.1).

### B.1 The group and its five verbs

```python
task_agent_app = typer.Typer(help="Task-scoped headless agents (docs/design-task-agents.md).")
app.add_typer(task_agent_app, name="task-agent")
```

| Verb | Signature (options abbreviated) |
|---|---|
| `start` | `<persona> [task_file]`, `-n/--name <slug>`, `-p/--prompt`, `--branch`, `-m/--model`, `-r/--revision`, `--peer <name>[:<hops>]` (repeatable), `--parent` (hidden) |
| `status` | `[<name>]` |
| `logs` | `<name>`, `-n/--tail N` (default 80) |
| `transcript` | `<name>` |
| `shutdown` | `[<name>]`, `--all`, `--force`, `--parent` (hidden) |

Every verb calls `_reject_in_master("cld task-agent <verb>")` first (P6 replaces those calls
with broker dispatch).

### B.2 `start` — order of operations

Refusals before anything expensive or side-effecting:

1. `require_docker()`; `cfg = Config.from_env()`; `setup_logging(cfg)`.
2. Task input: resolve `task_file` (`@name` via `resolve_prompt_ref`, else a real path) and
   require **task file or `-p`** — same error as `cld run`.
3. `repo_root = find_target_repo(cfg)`.
4. Persona: strip a leading `@`, then `persona_resolve(name, repo_root, cld_root)` — fails
   fast with the two searched directories named.
5. Slug: `-n/--name`, else `--branch`, else error. `branch = branch or slug`.
6. `peers = _parse_peer_specs(peer, cfg.peer_absolute_limit)`.
7. `assert_task_agent_capacity(cfg, parent)` — the cheaper refusal, and it touches no store.
8. `anchor = resolve_task_agent_anchor(cfg, repo_root, revision)`.
9. `ensure_image(...)` (devcontainer + base parent), as the other devcontainer launchers do.
10. `session = allocate_task_agent_name(repo_root, slug)` — validates the slug shape and
    appends `-2`/`-3` on a live collision.
11. Peer sanity: refuse `--peer <own session>`; **warn** (not refuse) for a peer that is
    neither a known task-agent container nor a live mailbox (§C.4).
12. Args: `build_container_args(repo_root, session, cfg, task_agent=TaskAgentSpec(...))`
    + `anchor_env_args(cfg, session, anchor)` + persona mount/env + task mount +
    `AGENT_INLINE_PROMPT` + `AGENT_MODEL` + `home_mounts_devcontainer` + `stage_ssh_agent`.
13. `docker run -d`, then `_wait_for_container_ready(session, _READY_SENTINEL["agent"])`;
    on timeout, error + `cld task-agent logs` hint + exit 1, **leaving the container up**
    (matching `_run_persistent_devcontainer`).
14. Print the handle block: full container name, task slug, persona, branch, anchor (12
    chars), peers with budgets, and the four follow-up commands (`status`, `logs`,
    `transcript`, messenger `send()` with the **full** name).

### B.3 Helpers

```python
_REAP_WAIT_SECONDS = 10

def _parse_peer_specs(specs: list[str], default_limit: int) -> dict[str, int]
def _task_agent_rows(cfg: Config) -> list[dict]
def _resolve_task_agent(cfg: Config, name: str) -> str
def _assert_reap_ready(cfg: Config, name: str, *, parent: str) -> None
def _reap_task_agent(cfg: Config, name: str, *, parent: str, force: bool) -> bool
def _print_task_agent_roster(rows: list[dict]) -> None
def _docker_logs(name: str, tail: int) -> None      # extracted from _do_logs
```

- **`_parse_peer_specs`** — `<name>[:<hops>]`; omitted `hops` → `default_limit`; `hops` must
  be a positive integer; duplicate name → error; empty name (`:5`) → error.
- **`_resolve_task_agent`** (D26) —
  1. `live` = task-agent container names ∪ `list_fleet` mailbox names.
  2. exact hit in `live` → return.
  3. slug matches (`c.rsplit("_", 1)[-1] == name`): one → return; several → prefer
     `task_agent_container_name(find_target_repo(cfg), name)` if it is among them, else
     error listing the candidates (D26's ambiguity error).
  4. nothing live → try `resolve_mailbox_dir` for `name` and for the cwd-repo's computed
     name, so a **reaped** agent still resolves for `transcript`.
  5. error naming the input plus `cld task-agent status`.
- **`_task_agent_rows`** — union of `docker_task_agent_list()` (all states) and
  `list_fleet(mailbox_root)`, keyed by name. Per row: `name`, `container`
  (`running`/`stopped`/**`gone`**), `phase`, `msgs`, `cost`, `branch`, `parent`, `repo`,
  `age` (from `meta.created_at`). `gone` = mailbox without a container, i.e. §6/§10's
  manual-cleanup signal.
- **`_assert_reap_ready`** — the three §7 checks, ordered **3 → 2 → 1** so the one that
  *waits* runs last and we never spend 10 s only to refuse for a different reason:
  1. **Own fleet** (check 3): when `parent` is non-empty and the target's
     `meta["parent"]` differs → refuse. Empty `parent` = the human, who has full authority;
     only the broker passes a value (§C.5).
  2. **Not a live peer** (check 2): any *other* fleet member whose container is **running**
     and whose `meta["peers"]` contains the target → refuse, naming the dependents. Scanned
     host-wide, not per parent — the broken reply guarantee is real regardless of who owns
     the dependent.
  3. **Not mid-turn** (check 1): poll `state.json` for up to `_REAP_WAIT_SECONDS`; if the
     phase is still `processing`, refuse naming the in-flight message (`current.subject`,
     `current.from`, `started_at`). A missing `state.json` is *not* a refusal — an agent
     that never started cannot be mid-turn.
- **`_reap_task_agent`** — `_assert_reap_ready` unless `force`; repo path from the container
  label, else `find_target_repo(cfg)` best-effort (§C.6); then `_stop_and_remove_container`
  → `_forget_session_state` (session bookmark + workspace; **never** the deliverable
  branch, which is a different string) → `mailbox.archive_mailbox`. All three steps
  idempotent, so a re-run of a partially-completed reap finishes it.

### B.4 `status`

- **No name:** the roster table (house style: computed column widths + `typer.echo`),
  **host-wide** (§C.3). Footer hint listing the `gone` rows and how to clear them.
- **With a name:** resolve, then print container state plus `meta.json` (task — truncated to
  one line, persona, branch, anchor, parent, peers with budgets, created) and `state.json`
  (phase, session id, messages, cost, current message). An **archive-only** hit prints
  "reaped — mailbox archived" plus the `transcript` pointer (§C.7).

### B.5 `logs` / `transcript`

- `logs <name>`: resolve → `_docker_logs(resolved, tail)`; error if the container is gone.
- `transcript <name>`: `mailbox.transcript(root, resolved)` rendered as
  `<ts>  -> / <-  <peer>  <subject>` followed by the body indented four spaces; empty →
  "no messages yet".

### B.6 `shutdown`

- `<name>` xor `--all`; neither → error, both → error.
- `--all` iterates every task-agent (label-scoped, host-wide) in a **progress loop**: repeat
  passes while any reap succeeds, so an A→B peer edge doesn't leave B refused just because
  the pass happened to try B first (§C.8). Refusals are reported and exit 1.
- `--force` bypasses all three checks; it is never passed by the broker (§7).

### B.7 Move `persona_resolve` to `cld/prompts.py`

P3 §D's follow-up, now that it has a second caller: move the function verbatim, import it
into `chain.py` from `cld.prompts`, leave the three chain call sites untouched.

### B.8 Small shared fixes

- `_format_age`: drop a fractional-seconds part before `strptime` so mailbox timestamps
  render as an age instead of a raw ISO string (A.13).
- `_do_logs`: extract `_docker_logs(name, tail)` and call it from both paths.

### B.9 `CLAUDE.md`

The `cld task-agent` block in **Key Commands** only. The architecture/design prose and the
master's control-tower guidance are P6's.

## C. Decisions taken here

1. **Everything lands in `cli.py`, not a new `cld/task_agent.py`.** I drafted the split
   (policy in a module, wiring in the CLI, mirroring `run.py`) and rejected it: `launch_run`
   was extracted because it has three callers, whereas every P4 function has exactly one —
   P6's broker route invokes the *CLI binary*, not these functions. The extraction would
   also force `_stop_and_remove_container` and `_forget_session_state` out of `cli.py` (a
   new module importing `cli` is a cycle), re-pointing eight working tests' patch targets to
   buy nothing. §12 says `cli.py`; so does "inline over extracted unless reuse is proven".
2. **`-n/--name` is the task slug** (the open question from P2 §D). Consistent with every
   other `cld` command, and it gives the master the pinned handle that peer edges need.
   Falls back to `--branch`; with neither, **error rather than generate** — §4 puts slug
   generation on the master and wants human-readable handles, and a synthesized
   `task-<n>` would make every downstream surface (roster, transcript, `--peer`) unreadable.
3. **The roster is host-wide, with a repo column.** `cld agent status` is per-repo because
   there is one agent per repo; task-agents are many per repo and the roster's second job is
   §10's orphan hunt, which a repo filter would hide. Host-wide also means `status` works
   from outside any repo — worth having in a cleanup command.
4. **An unknown `--peer` warns; only a self-edge refuses.** The design gives the master sole
   authority to draw edges and never asks the launcher to validate them, and a hard refusal
   would invent a spawn-ordering failure mode. A typo surfaces immediately anyway (the
   agent's first `send()` fails and it escalates), so a WARNING naming the unknown peer is
   the right strength — it reaches the master, which is the caller. `--peer <own name>` is
   different: it can only ever be a mistake, so it errors.
5. **`--parent` is a hidden option on both `start` and `shutdown`.** §4 says the parent is
   "stamped automatically when launched from a master, not user-set", but P6's broker runs
   host-side `cld task-agent …` in a fresh process, so the value has to cross that boundary
   as an argument. Hidden keeps it out of the human-facing help. Not a security hole: a
   forged `--parent` *gives away* reap authority over your own agent, and host CLI access is
   already total authority. It also lets P4 implement and test reap check 3 today instead of
   leaving check 3 of three to a different part.
6. **A reap whose container is already gone still archives the mailbox.** The host repo path
   normally comes from the container label; with no container there is none, so the bookmark
   forget falls back to `find_target_repo(cfg)` best-effort and `_forget_session_state`'s
   existing warning covers the rest. Forgetting a bookmark that doesn't exist in that repo
   is a no-op, so the fallback cannot damage another repo. Mailbox archiving is the part
   that always works, and it is the part that keeps the registry honest (D7).
7. **`status <name>` covers live-or-crashed agents; a reaped one is `transcript`'s job.**
   §7 pairs `status` with the roster and `transcript` with the archive, and the alternative
   (archive-aware `read_meta`) would break `ensure_meta`'s write-once guard (A.11). Status
   still *resolves* an archived name so it can say "reaped" and point at `transcript`,
   rather than claiming no such agent.
8. **`--all` respects the checks and loops to a fixpoint.** §10 calls `--all` the
   clear-everything hammer but never says it implies `--force`; keeping the checks on means
   a mid-turn agent isn't killed by a sweep, and `--all --force` remains the real hammer.
   The loop exists because check 2 is order-dependent: with an A→B edge, reaping B first is
   refused while reaping A first is allowed, so a single arbitrary-order pass would strand B.
   A mutual A↔B pair still refuses both — correctly, and `--force` is the answer.
9. **No `restart` verb.** The entrypoint and supervisor do support warm restart, but §4's
   verb list omits it and a task-agent's lifespan is the task. Adding it would raise a
   lifecycle question the design hasn't answered (does a restarted agent keep its kickoff?).
10. **`_REAP_WAIT_SECONDS = 10`, a module constant, not a config knob.** §7 says "waits
    briefly". Ten seconds matches `docker stop`'s grace and catches the "turn just ended"
    race; a turn that runs minutes is not something shutdown should block on. Promote it to
    config only if it turns out to be annoying in practice.

## D. Seams for later parts

- **P5** needs nothing from P4. They are independent; either order works.
- **P6** replaces each `_reject_in_master` call with broker dispatch, and its broker action
  invokes exactly this CLI: `start … --parent <master-session>` and
  `shutdown <name> --parent <master-session>`, **never** `--force`. Because `--force` is a
  CLI flag the broker simply never emits, §7's host-only property is satisfied by the
  broker's argv construction — worth a P6 test asserting the action rejects it.
- **P6 docs** own the CLAUDE.md architecture/config prose and the master persona; P4 adds
  only the Key Commands block.

## E. Commit sequence (each step green on its own)

1. **`persona_resolve` move** to `cld/prompts.py` + `chain.py` import; `--peer` repeatability
   smoke check (Typer 0.24.1); `_format_age` fractional-seconds fix; `_docker_logs`
   extraction. Pure refactors, existing tests must stay green.
2. **`start`**: the group, `_parse_peer_specs`, slug/branch derivation, the launch path.
3. **Read verbs**: `_resolve_task_agent`, `_task_agent_rows`, `status` (both shapes),
   `logs`, `transcript`.
4. **Reap**: `_assert_reap_ready` (three checks), `_reap_task_agent`, `shutdown`
   (`<name>` / `--all` / `--force` / `--parent`).
5. **Docs**: CLAUDE.md Key Commands block.

## F. Test matrix (`tests/test_cli.py`, via `CliRunner`)

| Area | Cases |
|---|---|
| slug + branch | `-n` wins; `--branch` fallback; neither → error; invalid slug shape → error naming the expected form; `branch` defaults to the slug |
| `--peer` parsing | `a` → default limit; `a:5`; two peers; duplicate name → error; `a:0` / `a:-1` / `a:x` → error; `:5` → error; `--peer <own session>` → error |
| `start` wiring | `TaskAgentSpec` carries slug/parent/branch/peers; persona mounted at `/config/persona.md` with `AGENT_PERSONA` + `AGENT_PERSONA_FILE`; task file mounted, `AGENT_INLINE_PROMPT`, `AGENT_MODEL`; `stage_ssh_agent` called; `docker run -d`; `anchor_env_args` gets the **resolved** anchor, not the raw `-r` |
| `start` refusals | capacity refusal exits 1 **and** `ensure_image` / `docker run` were never called; live-stack anchor refusal likewise; missing persona → error naming both searched dirs; no task and no `-p` → error; unknown peer → warning, spawn proceeds |
| readiness | sentinel timeout → exit 1, container **not** removed, hint mentions `cld task-agent logs` |
| resolver | full name; unique bare slug; ambiguous slug across two repos → error listing candidates; ambiguous resolved by cwd repo; unknown → error; archived full name resolves; archived bare slug resolves via the cwd repo |
| roster | running/stopped/`gone` rows; `gone` footer hint; phase/msgs/cost from `state.json`; empty → "No task-agents found." |
| detail | live agent prints meta + state fields; archived-only prints "reaped" + transcript pointer |
| transcript | in/out ordering with bodies; empty mailbox message; works against an archived mailbox |
| reap check 1 | phase `processing` → refuse naming the in-flight subject and sender (wait constant patched to ~0); phase `idle` → passes; missing `state.json` → passes |
| reap check 2 | a **running** peer-declaring member blocks and is named; a **stopped** one does not; the target's own `peers` never blocks itself |
| reap check 3 | `--parent` mismatch → refuse; match → passes; no `--parent` (human) → passes regardless |
| `--force` | bypasses each of the three refusals |
| reap effects | stop → rm → `_forget_session_state(repo, session)` → `archive_mailbox`, each exactly once; the deliverable branch is never an argument to any of them; re-running on an already-reaped name is a no-op that still exits 0 |
| `shutdown --all` | reaps every task-agent; A→B edge reaped in **one** invocation via the progress loop; mutual A↔B refuses both and exits 1; `<name>` + `--all` → error; neither → error |
| in-master | each of the five verbs exits 1 with the not-supported message |

Run `poetry run pytest tests/test_cli.py -q` per commit, then the full suite. Expected end
state: the same **3** pre-existing failures, no new ones.

**Host-side e2e checklist** (needs a daemon; record results in Scratch) — this is also the
first chance to clear P2's outstanding checklist, since P4 is what can finally spawn one:
spawn two task-agents on one repo; `docker inspect` shows all four labels; both bookmarks
exist in the origin and the deliverable one sits at the anchor; the kickoff prompt in
`cld task-agent logs` contains all three layers; `status` lists both and `status <slug>`
resolves bare; reap one and confirm the mailbox moved under `_archive/` while the
deliverable bookmark survived; reap-refuse the other by naming it as a peer of a live third.

## G. Risks

- **`start` is the first place all six parts meet.** Everything before it was unit-testable
  in isolation; this is the first code path that resolves a persona, stages an anchor, spawns
  a container, and hands a composed prompt to a real claude. Expect the first live run to
  find wiring bugs that no unit test here can (P2's e2e checklist is still open for exactly
  this reason).
- **Two positionals of different kinds** (`<persona> [task_file]`) is §4's grammar, and
  `cld task-agent start task.md` will read `task.md` as a persona name. The error is loud
  (`Persona 'task.md' not found in …/prompts/personas/`), so this is a usability wart, not a
  correctness one — but it is the shape of mistake a master will make.
- **Check 2 is one-directional by design** (§7: edges are asymmetric), so reaping the agent
  a live peer *points at* is refused while reaping the one *doing the pointing* is not. That
  is the spec's accepted asymmetry, not a gap to fix here.
- **`--all`'s progress loop re-enumerates docker each pass.** Bounded by the fleet size and
  only on an explicit sweep, so the cost is irrelevant — but it means `--all` is not atomic:
  an agent spawned mid-sweep may survive it.
- **Typer's `list[str]` option** is unexercised in this repo; step 1's smoke check exists so
  a surprise there is found before `--peer` has consumers.

## H. Outcome (2026-08-12)

All five steps landed. **465 tests pass (was 389 — 76 new), same 3 pre-existing failures**,
none of them touched by P4.

`cld/cli.py` — one new section, `cld task-agent` with all five verbs:
- `start`: persona positional (`@name` or bare) + optional task file, `-n` slug, `-p`,
  `--branch`, `-m`, `-r`, repeatable `--peer <name>[:<hops>]`, hidden `--parent`. Mounts the
  persona at `/config/persona.md` (`AGENT_PERSONA_FILE` + `AGENT_PERSONA`), the task at
  `/config/task.md`, adds `home_mounts_devcontainer` + `stage_ssh_agent`, `docker run -d`,
  waits on `/tmp/cld-agent-ready`, prints the handle block.
- `status` (roster / detail), `logs`, `transcript`, `shutdown` (`<name>` / `--all` /
  `--force` / hidden `--parent`).
- Helpers: `_parse_peer_specs`, `_format_peers`, `_known_task_agent_names`,
  `_cwd_repo_task_agent_name`, `_resolve_task_agent`, `_task_agent_rows`,
  `_print_task_agent_roster`, `_one_line`, `_print_task_agent_detail`,
  `_task_agent_record`, `_task_agent_parent`, `_assert_reap_ready`,
  `_task_agent_repo_root`, `_reap_task_agent`, `_reap_all_task_agents`.

Shared fixes, as planned: `persona_resolve` moved to `cld/prompts.py` (import fixed in
`chain.py`); `_format_age` tolerates the mailbox's microsecond timestamps; `_docker_logs`
extracted and shared with `_do_logs`; `_resolve_task_file` extracted and shared with
`cld run`; `docker_task_agent_status` added next to its two siblings in `cld/docker.py`.
CLAUDE.md gained the Key Commands block.

**Three fixes from reading the code back as prose rather than as a diff** — the same habit
that paid off in P3:

1. **A collision suffix broke every follow-up hint.** `start` printed
   `cld task-agent status <slug>`, but when `allocate_task_agent_name` appends `-2` the
   slug resolves to the *other*, already-running agent — so the hints pointed at the wrong
   container, and so did the readiness-timeout message. The handle is now derived from the
   allocated session (`session.rsplit("_", 1)[-1]`), which equals the slug in the ordinary
   case. Pinned by a test.
2. **Name allocation moved ahead of the two refusals.** It validates the slug shape, so
   leaving it after them meant `-n "Add OAuth"` was only rejected after a capacity check, a
   store query and possibly an image build. Now every input error precedes them, and the
   self-peer check has the final name to compare against.
3. **Reap check 3 now prefers the container label over `meta.json`.** Labels are host-set;
   `meta.json` is written by the container from env the host gave it. A compromised agent
   could rewrite its own `parent` to make its master's reaps refuse — a small self-inflicted
   DoS, closed for free by reading the label when the container still exists and falling
   back to `meta.json` only once it is gone. `_reap_all_task_agents` resolves owners with the
   same precedence, once for the whole sweep.

**Deviations from the plan:** none in surface, one in placement — `_resolve_task_file` was
extracted (the plan didn't mention it) so `start` and `cld run` share the `@name`/path
handling instead of duplicating it; `cld run` now resolves its repo root before the
task-file check, so outside a repo it reports "not a VCS repository" before "task file not
found". Both are exit-1 user errors.

**Test-harness note worth remembering:** `caplog` cannot see warnings emitted *inside* a
CLI invocation. `setup_logging` sets `propagate = False` on the `cld` logger, undoing the
propagation pytest forces at test setup, so a warning raised after that call never reaches
caplog's root handler. It does reach `result.output`, because `_LazyStderrHandler` resolves
`sys.stderr` at emit time and CliRunner has replaced it — so assert on the output, which is
what the caller actually sees. Direct library calls (no `setup_logging`) still work with
caplog, which is why the P2 tests do.

**Pre-existing wart, not introduced here and left alone:** `typer.Exit` subclasses
`RuntimeError` (via `click.exceptions.Exit`), so `_handle_errors` catches it and every
clean `Error: …` message from any command is followed by a redundant
`ERROR [cld.cli] Command failed: 1`. `cld run` with no arguments has always done this.

**Not verified here:** nothing has spawned a real container. P2's host-side e2e checklist
plus P4's own (Appendix P4 §F) are both still open and are now runnable in one sitting,
since `start` is what finally makes them possible.

---

# Appendix P5 — detailed implementation plan

> Scope: the messenger's two fleet read surfaces (§7, D23) and the per-edge hop gate
> (§10, D16–D20, **D29**). Independent of P4; needs only P1's primitives.
> Final version — supersedes the first draft, whose two-mechanism answer to the
> spent-edge loop was replaced by one transport rule (§C.3) and folded back into the spec.

## A. Ground truth (verified in the tree, not assumed)

1. **`cld/mcp/messenger.py` is thin by construction.** Five tools, each `_own_name()` +
   `_mailbox_root()` + one `mailbox.*` call. `_mailbox_root()` is the fixed
   `MAILBOX_MOUNT`, deliberately *not* `cfg.mailbox_root` (host-side field). Error
   convention: dict-returning tools return `{"error": …}`, list-returning tools return
   `[{"error": …}]` (`list_inbox`). New tools match that, rather than inventing a third
   shape.
2. **There is a second, ungated send path — and the shipped skill instructs agents to use
   it.** `python -m cld.messenger.send --to … --subject … --body-file …` calls
   `mailbox.write_message` directly, and `.claude/skills/messenger-send/SKILL.md` (baked
   into the image at `/opt/cld/.claude/skills/`, loaded in every devcontainer via
   `claude --add-dir /opt/cld`) documents exactly that command. A gate that lived only in
   the MCP tool would be bypassed by the *instructed* path, not just by a clever agent.
   This is the finding that shapes P5 (§C.1).
3. **That CLI path is also broken for peers today.** `send.py` calls
   `resolve_recipient(args.to)` **without** `root=`, so it skips the mailbox
   short-circuit and always enumerates containers — which needs a host channel no agent
   container has. Passing `root=root` (as the MCP tool already does) fixes peer sends and
   replies through the CLI path.
4. **The MCP server is registered only if the host's `~/.claude.json` already has an
   `mcpServers.messenger` entry.** `build_claude_config` *rewrites* that entry to the
   container's paths; it never creates one. So the MCP tools exist in a container only
   when the operator has the messenger configured on the host. Pre-existing for
   `cld agent`; a risk for P5 (§G) and the reason every skill reaches for the CLI verbs.
   It is also why the gate must not live *only* in the MCP layer.
5. **`import cld` works in the image** via `ENV PYTHONPATH=/opt/cld`; the image ships only
   `mcp[cli]`, `typer`, `pyyaml`. P5 adds no dependency.
6. **P1's edge primitives, as they stand:** `edge_path` (sorted endpoints), `read_edge` →
   `{count, limit, updated}` (defaults `{0, None, None}` when there is no file),
   `bump_edge(root, a, b, limit) -> (hops, allowed)` — **stored limit wins**, first send
   seeds it, blocked bump not persisted. Two things about it matter here:
   - It has **zero non-test callers** (verified), so reshaping it in commit 1 is
     behavior-neutral.
   - `bump_edge(..., limit=None)` on a fresh edge would raise `TypeError` today
     (`count > None`). The reshape in §C.3 removes the comparison, so the hazard goes away
     rather than needing a guard.
7. **`read_meta` / `read_state` are live-dir only** (P4 §A.11): making `read_meta`
   archive-aware would break `ensure_meta`'s write-once guard for a reused slug. Anything
   that must read a *reaped* member's facts needs its own resolver.
8. **`list_fleet(root, parent)` already does the parent filtering** the read tools need,
   and skips `_`-prefixed roots, so archived members never appear in a digest.
9. **`write_message` has no `hops` parameter**; its outbox line is a fixed key set
   (`id, to, subject, body, ts`) built inline in the append. **`transcript`'s "out" entries
   are likewise a fixed projection** of that line, so an added key is invisible there
   unless the projection is extended — otherwise `hops` would show on received messages
   (`{"direction": "in", **msg}`) and vanish on sent ones.
10. **The supervisor writes messages directly, twice**, both in `process_one`
    (`agent_loop.py`): the claude-failure error reply and the recipient-scoped fallback
    reply. Both ignore the return value, and both bypass `send()` by construction — which
    is what makes the reply guarantee a loop engine on a spent edge (§C.3).
11. **`resolve_recipient`'s shortname path picks arbitrarily among task-agents.** It
    rejects a basename matching *two repos*, then prefers `kind == "agent"`, then falls to
    `matches[0]`. With N task-agents in one repo and no repo agent, `to="myrepo"` delivers
    to whichever container docker listed first.
12. **Existing tests will not be disturbed by the new peer-edge branch** (verified): no
    test in `tests/test_mailbox.py` writes a message between two names that both have
    `meta.json`, and every `msg = write_message(...)` call site is an exempt-edge write, so
    all of them keep getting a dict. The only test class that must change is `TestEdges`.
13. Baseline: **465 passed, 3 pre-existing failures** (unchanged by P4).

## B. Deliverables — exact surface

### B.1 `cld/messenger/mailbox.py` — the rule, the gate, the read primitives

New:

```python
def task_summary(text: str, width: int = 160) -> str          # first line, truncated
def read_meta_resolved(root, name) -> dict | None             # live, else archived
def edge_spent(root, a, b) -> bool                            # stored limit reached?
def fleet_digest(root, parent) -> list[dict]
def gated_send(root, frm, to, subject, body, *, default_limit) -> dict
```

Reshaped, because fusing "count it" with "may it pass" is what allowed the ordering trap
in §C.3:

```python
def bump_edge(root, a, b, limit: int | None = None) -> int     # was -> (hops, allowed)
def write_message(root, frm, to, subject, body, *, peer_limit=None) -> dict | None
```

- **`edge_spent(root, a, b)`** — `read_edge`, then `limit is not None and count >= limit`.
  An unseeded edge (no file, or a stored `limit` of `None`) is never spent.
- **`bump_edge(root, a, b, limit=None)`** — increments and returns the new count. Seeds the
  stored limit from *limit* only when the stored one is `None`; a stored value always wins,
  so the declaring side governs both directions (§10). No refusal, no comparison.
- **`write_message`** — carries the one rule (§C.3). Order of operations:
  1. Peer edge? Both endpoints have `meta.json` (§C.2). If not → today's behavior exactly.
  2. `edge_spent(...)` → **deliver nothing**: log at WARNING (a policy refusal, not an
     error), return `None`. Checked *before* anything is created, so a refusal leaves no
     mailbox dir, no inbox file, no outbox line, and no counter change.
  3. Otherwise deliver as today, then `bump_edge(root, frm, to, limit)` where `limit` is
     the sender's `meta["peers"].get(to)` if present, else *peer_limit*, and stamp the
     returned count as `hops` on the envelope **and** the outbox line.
  `peer_limit` is only ever supplied by a caller with a config view (`gated_send`); the
  supervisor omits it, which is sound because the first message on any edge always goes
  through `gated_send` (§G).
- **`transcript`** — the "out" projection gains `hops` when the line has it (§A.9), so the
  audit stamp is visible on both directions of a `read_mailbox`.
- **`fleet_digest(root, parent)`** — one row per `list_fleet(root, parent)` member:
  `{name, task, phase, msg_count, cost_usd_total, unread, last_activity}` — §7's field list
  verbatim. `task` goes through `task_summary` (P3 §C.6: `meta.json` holds the *whole* task
  text, and the digest exists to keep the master's per-turn crank cheap). `unread` = files
  in `inbox/`. `last_activity` = newest mtime among `state.json`, `outbox.log`, `inbox/`
  and `archive/`, formatted like `_now_iso`. No message bodies, no docker.
- **`gated_send(root, frm, to, subject, body, *, default_limit)`** — one dict shape for
  every outcome, so both entry points have one thing to handle (§C.9):
  - unresolvable recipient → `{"error": <resolve_recipient's message>}`
  - refused → `{"error": …}` naming the edge, `count/limit` from `read_edge`, the
    escalate-don't-retry rule (D19) and *who* to escalate to: the sender's
    `meta["parent"]`, or a generic "your master" when that is empty (a human-launched
    agent has no parent — P2 §C.2)
  - delivered → `{"id", "hops", "limit"}` for a peer edge (`limit` re-read from the edge
    file so both sides quote the same ceiling), or just `{"id"}` when hop-exempt

### B.2 `cld/mcp/messenger.py` — two new tools, `send` rewired

| Tool | Behavior |
|---|---|
| `send(to, subject, body)` | Body becomes `mailbox.gated_send(...)` with `default_limit=Config.from_env().peer_absolute_limit`. Keeps its own `try/except RuntimeError` around `_own_name()` (identity is the caller's problem, not the transport's). Docstring gains: peers are addressed by **full container name**; what `hops`/`limit` mean; a refusal means escalate, never retry. |
| `fleet_digest()` | `mailbox.fleet_digest(root, parent=_own_name())`; `[{"error": …}]` without `SESSION_NAME`. |
| `read_mailbox(name, since="")` | Scope check first: `read_meta_resolved(root, name)` must exist and its `parent` must equal `_own_name()`, else `[{"error": …}]` naming the actual parent (or "not a task-agent mailbox" when there is no `meta.json`). Then `mailbox.transcript(root, name)` filtered to `ts > since` — exclusive, so pass the `ts` of the last entry you saw. Reads a reaped member's archived mailbox. No entry cap: the digest is the cheap surface, this is the deliberate "show me everything" one. |
| `list_agents(kind)` | Docstring only: `kind` now includes `task-agent` (§12). |

Both read tools are master surfaces by construction: a task-agent asking for its own
mailbox is refused (its `parent` is the master, not itself), which is fine — it has
`list_inbox` / `read_message` for that.

### B.3 `cld/messenger/send.py` — same gate, plus the `root=` fix

`resolve_self()` → `gated_send(root, frm, args.to, …, default_limit=cfg.peer_absolute_limit)`.
`{"error": …}` → message on stderr, `sys.exit(1)`. Success → the existing
`sent: <id>  <frm> -> <to>` line, plus ` (hop <n>/<limit>)` when the return carries them.
Resolution now goes through the mailbox short-circuit, so a peer send no longer needs a
host channel (§A.3).

### B.4 `cld/messenger/agent_loop.py` — untouched

The supervisor needs **no edit**. Both its `write_message` calls already go through the
chokepoint, so a spent edge refuses them like anything else, and both already ignore the
return value. That is the point of putting the rule in the transport rather than in each
sender (§C.3, §C.5).

### B.5 `resolve_recipient` — refuse an ambiguous shortname

When a shortname matches several task-agents in one repo and no repo agent exists, raise
naming the candidates instead of silently picking `matches[0]` (§A.11). The existing
"prefer the repo agent" behavior is unchanged.

### B.6 Docs

- `.claude/skills/messenger-send/SKILL.md`: peers are addressed by **full container name**
  (a shortname is for the one-agent-per-repo case); the command reports `hop n/limit`; a
  refusal is not to be retried or worked around — tell the master, whose channel is never
  budgeted.
- `CLAUDE.md`'s Messenger section: the two new tools, the hop gate and where it lives
  (both send paths), and one line for the silence rule.

## C. Decisions taken here

1. **The gate lives in the transport, not in the MCP `send()` tool.** D16 names `send()`
   because the MCP server is a grandchild process the supervisor cannot reach — that
   reasoning is about *which process*, and it still holds. What it missed is that two entry
   points reach the transport (§A.2), one of them documented in a shipped skill. So the
   accounting lives in `gated_send`, which both instructed paths call, and the *closure*
   rule lives one level lower in `write_message`, which every sender already goes through.
   In-container enforcement is a guardrail against runaway, not a defence against an
   adversary — an agent that writes its own Python can reach the filesystem regardless.
   D16's rationale was amended in the spec to match.
2. **Budgeted iff both endpoints have `meta.json`.** The tempting rule — "budgeted iff the
   recipient is in my `peers`" — under-counts by half: edges are asymmetric (§7), so the
   *named* peer has no `peers` entry and its replies would ride free, letting a ping-pong
   burn 2N messages against an N-hop budget. Keying on "both sides are task-agents" makes
   the reply direction count on the same shared counter, and makes the control plane exempt
   for free: masters have no `meta.json`. `peers` is then used only for the *limit seed*,
   which is exactly what §10 asks for. Corollary, accepted: a task-agent talking to the
   **standing repo agent** is unbudgeted, because a repo agent is nobody's fleet member.
   That channel is not a master-drawn peer edge, so it is out of the budget's remit.
3. **One rule: a spent edge is silent.** The transport delivers nothing more over it, no
   matter who asks — agent, supervisor, or the transport itself. The invariant is one
   sentence: *at most `limit` messages are ever delivered over a peer edge*, checkable by
   reading one function instead of tracing message flows.

   This replaces the first draft's two mechanisms (a one-shot flag on the cap notice plus a
   guard on the supervisor's fallback), which patched leaks instead of closing the hole.
   The hole: §10 exempted from the budget exactly the messages whose job is to *announce
   the end* — the cap notice and the synthesized fallback — while still delivering them as
   ordinary inbound messages that oblige a reply. **Anything both hop-exempt and
   reply-obliging loops** forever at agent cadence, one Claude turn per hop. Those two
   properties must never coexist on one edge.

   What falls out of the rule:
   - **No cap notice at all.** It would be a delivery on a closed edge. §10 wanted it so
     the peer "isn't left waiting", but that is a *reporting* need, and reporting goes
     through the referee: the blocked agent escalates on the master channel, unbudgeted
     because it is a **different edge** — not an exemption carved into this one. That
     distinction is the whole mechanism. Nothing hangs: the peer's supervisor is idle and
     polling, so "waiting" only means the conversation went quiet, and the master reads
     both mailboxes. §7 made this same move when it replaced the per-reap authorization
     token with "report reaps in turn output".
   - **No supervisor edit** (B.4) — the guard becomes the same rule applied to one more
     caller.
   - **A supervisor fallback consumes a hop** on an open edge. More honest than exempting
     it: it is a real delivered message on the edge.
   - **The cost:** an agent loses §10's free "resolved, moving on" send. A graceful landing
     must fit *inside* the budget — the limit-th message is the last word. "N messages, the
     last one final" is also an easier contract to hand an agent than "N plus a free one
     for emergencies", and the preamble's "converge as you approach the limit" already
     says it.
   - **Residual loss, accepted:** an agent that burns the budget without landing leaves the
     peer with silence only the master can explain.

   **The ordering trap, and why two primitives change shape.** `bump_edge` today fuses
   "increment" with "is it allowed", answering `count > limit` *after* incrementing. Ask
   that at delivery time and the limit-th message — the one that should land — is refused,
   because by then `count == limit`. So the check must come *before* the delivery
   (`edge_spent`: has the ceiling already been reached?) and the increment *after* it.
   `bump_edge` becomes a plain increment returning the new count; `edge_spent` owns the
   question. Cost in tests: `TestEdges` loses its 2-tuple assertions (six of them, plus
   three bare calls), and `test_blocked_bump_not_persisted` is **replaced rather than
   edited** — under the new shape a refusal never reaches `bump_edge`, so "not persisted"
   stops being a property of the counter and becomes one of `write_message`. Worth it: the
   fusion is what made the trap possible.
4. **`read_mailbox` drops `include_archive`.** The original §7 signature had it, but
   `False` could only ever hide the *received* side of an exchange (a task-agent archives
   each message within ~1 s of processing it — the very fact that motivated this tool), and
   every MCP parameter is prompt-visible surface the caller must reason about. `since`
   covers the real need: incremental reads on the master's crank. **The spec was amended**
   to the delivered signature, so §7 and §12 now read `read_mailbox(name, since=…)`.
5. **`write_message` signals refusal by returning `None`, not by raising.** Its two
   callers that must tolerate a refusal — the supervisor's error reply and its fallback —
   already ignore the return, so `None` needs no code at either site, while an exception
   would force a `try/except` around both. The transport logs the refusal itself, because
   it is the transport's decision. `gated_send` is the only caller that inspects the
   result.
6. **`task_summary` is promoted into `mailbox.py`; P4's `_one_line` becomes a caller.**
   Same logic, two consumers (the digest at 160 chars, the CLI detail view at 72), and the
   thing being summarized is a `meta.json` field — mailbox's own schema.
7. **`Config.from_env()` per send, no module-level cache.** Sends are not hot, the env is
   fixed for a container's life, and reading lazily keeps the operator's
   `peer_absolute_limit` honest without module state that tests must reset.
8. **`fleet_digest` reports no liveness beyond `phase`.** A mailbox-only surface by design —
   no docker socket exists in a master. `phase` (`stopped` is written on clean exit) plus
   `last_activity` is the proxy; docker truth is `cld task-agent status` on the host (P4)
   or the broker's `list-containers` (P6).
9. **`gated_send` returns one dict shape for every outcome**, including an unresolvable
   recipient. Two channels (raise for a bad name, return for a refusal) would make both
   entry points handle both. `_own_name()`'s `RuntimeError` stays with the caller, since
   identity is a property of the calling container, not of the transport.

## D. Seams for later parts

- **P6** owns the master's control-tower persona, which is what actually calls
  `fleet_digest()` each turn and `read_mailbox` only for members that moved. It also owns
  the decision I am deliberately *not* taking here: whether the two read surfaces need
  `python -m cld.messenger.*` CLI verbs (and skills) so the crank works even when the
  operator has no messenger MCP entry on the host (§A.4). Every existing messenger skill
  uses the CLI for exactly that reason, so this is a real question — but it belongs to
  whoever writes the persona.
- **P6's broker `list-containers`** already plans to carry `kind`; nothing in P5 depends on
  it.
- The task-agent preamble (P3) starts being accurate when this lands: the `send()` return
  finally carries `{hops, limit}`. Its "tell the master, which is never budgeted" line
  still holds. **P6 should re-read that paragraph** and consider making the landing rule
  explicit — the limit-th message is the last word, and no farewell is delivered after it.
- **The spec is already amended** for §C.3 (§5, §10, §12, D16, D20, new D29) and for the
  two-send-paths finding. No further spec work is owed by P5.

## E. Commit sequence (each step green on its own)

1. **Reshape edge accounting**: `bump_edge` → plain increment (seeds only an unseeded
   limit), `edge_spent` added, `TestEdges` rewritten (§C.3). Behavior-neutral — no
   non-test caller exists yet (§A.6).
2. **The rule in `write_message`**: peer-edge detection, check-before / count-after,
   `hops` on the envelope and the outbox line, `transcript`'s out-projection extended,
   `None` on refusal. **This is the commit that makes the invariant true**, and it makes
   the supervisor's replies edge-aware without touching `agent_loop.py`.
3. **Read primitives**: `task_summary`, `read_meta_resolved`, `fleet_digest` + tests.
4. **`gated_send`** + both entry points: MCP `send`, `cld/messenger/send.py` (with the
   `root=` fix), `resolve_recipient` ambiguity guard. Testable without FastMCP.
5. **The two read tools** (`fleet_digest`, `read_mailbox`) + `list_agents` docstring;
   `_one_line` → `task_summary`; the `messenger-send` skill; CLAUDE.md.

## F. Test matrix

| Area | Cases |
|---|---|
| `task_summary` | first line only; truncates with an ellipsis; empty → `""`; exactly-`width` text is not truncated |
| `read_meta_resolved` | live mailbox; archived mailbox; neither → `None`; **regression:** `ensure_meta` still ignores an archived meta of the same name (P4 §A.11) |
| `edge_spent` | no file → False; below the limit → False; at the limit → True; stored `limit: None` → False |
| `bump_edge` | returns the new count; seeds the limit when unseeded; a later differing limit never overwrites a stored one; `limit=None` on a fresh edge stores `None` and does not raise (§A.6) |
| `write_message` exempt | unchanged behavior when either endpoint lacks `meta.json`: dict returned, no `_edges/` file, no `hops` key |
| `write_message` peer edge | delivers exactly `limit` messages then returns `None`; the limit-th **does** land (the ordering trap); on refusal nothing is written — no inbox file, no outbox line, unchanged count, and no mailbox dir created for an unknown recipient; `hops` on envelope **and** outbox line; both directions share one counter |
| **the invariant** | a loop alternating `write_message` in both directions, mixed with direct supervisor-style calls, delivers exactly `limit` messages total — the one test standing in for the whole §C.3 argument |
| `transcript` | `hops` surfaces on both "in" and "out" entries; a line without it still yields the legacy shape (existing test must pass) |
| `fleet_digest` | own-parent rows only; `task` truncated; `unread` counts inbox files; phase/msgs/cost from `state.json`; missing `state.json` → defaults; `last_activity` advances when a message lands; empty root → `[]`; archived members absent; §7's exact key set |
| `gated_send` exempt | agent→master delivers, no `_edges/` file, return is `{"id"}` only |
| `gated_send` budgeted | edge file created; `{id, hops, limit}`; limit from the sender's `peers` entry; `default_limit` when the peer is unlisted; **replier inherits the declared limit** |
| `gated_send` refused | `{"error"}` quoting `count/limit`, naming the parent master, saying escalate-not-retry; falls back to "your master" when `parent` is empty; **no message delivered anywhere** — in particular no cap notice, which is the point of §C.3 |
| `gated_send` bad recipient | unresolvable name → `{"error"}`, not an exception (§C.9) |
| MCP `send` | delegates to `gated_send`; missing `SESSION_NAME` → `{"error"}`; existing tests unchanged |
| MCP `fleet_digest` | own fleet only; foreign-parent member absent; no `SESSION_NAME` → `[{"error"}]` |
| MCP `read_mailbox` | archive + sent included, ts-ordered; `since` exclusive; foreign mailbox refused naming the real parent; no-`meta.json` name refused; reaped member readable; caller's own mailbox refused |
| `cld.messenger.send` CLI | goes through the gate (refused → exit 1, nothing delivered); resolves a peer via the mailbox short-circuit with **no** container enumeration; prints the hop position on success |
| `resolve_recipient` | shortname matching two task-agents in one repo raises naming them; still prefers the repo agent when one exists |
| supervisor (no code change) | on a spent edge `process_one` completes and delivers nothing, the transport logging the refusal; on an unspent edge and on the master channel the fallback still lands (existing tests) |

**Host-side e2e** (with P2's and P4's checklists; needs a daemon): spawn two task-agents
with `--peer <other>:2`, have them exchange until refused, then confirm — the third send
returns the error, `_edges/<a>--<b>.json` shows `count: 2`, **no further message appears in
either inbox** (no notice, no fallback), the blocked side escalates to the master instead,
`read_mailbox` shows the exchange with `hops` stamps, and `fleet_digest()` lists both
members with their costs. Also confirm the messenger tools are actually registered inside a
task-agent (§A.4) — if they are not, `send()` does not exist and only the CLI path is live.

## G. Risks

- **The MCP tools are only as present as the operator's config** (§A.4). With no
  `mcpServers.messenger` entry on the host, a task-agent has no `send()` tool and no read
  tools: replies come only from the supervisor's fallback. The gate itself survives that,
  because it sits in the transport and the CLI path goes through it — which is the second
  reason for §C.1, beyond the skill.
- **An unseeded edge would be uncapped, but is unreachable.** If the first message on an
  edge were written by the supervisor (no `peer_limit`, no `peers` entry), the edge would
  store `limit: None` and never be spent. It cannot happen: a supervisor only ever
  *replies*, so some earlier message opened the edge, and the first message on any peer
  edge comes from an agent's `send()` → `gated_send`, which always supplies a limit. Stated
  rather than defended, per the no-impossible-state-guards rule — and `bump_edge` seeds an
  unseeded limit whenever one is later supplied, so even then the first configured send
  would cap it.
- **Lost increments under concurrency** are accepted by §10 (read-modify-write, no lock).
  With N task-agents the window widens slightly; the failure mode is a marginally generous
  ceiling, never a stuck edge.
- **`Config.from_env()` inside the MCP server** reads the *container's* view (env +
  `/workspace/current/.cld/config.toml`). P2 passes `CLD_PEER_ABSOLUTE_LIMIT` precisely so
  the operator's host value reaches it; a project TOML in the repo could override it, which
  is how every other knob resolves.
- **`write_message` returning `dict | None` is a widened contract.** Existing callers are
  the supervisor (ignores it at both sites) and the tests (all exempt-edge writes, §A.12);
  the stub fixture writes its own files. A future caller that assumes a dict gets an
  `AttributeError` rather than a silent drop — the failure direction to prefer.
- **A silent spent edge is only as legible as the master.** The peer learns nothing when an
  exchange is cut off — by design (§C.3) — so if the master is not cranking, the exchange
  stops with no visible reason. `read_mailbox` and the `_edges/` file both show it after
  the fact; that is the trade the invariant buys.

## H. Outcome (2026-08-12)

All five steps landed. **534 tests pass (was 465 — 69 new), same 3 pre-existing failures.**

- `cld/messenger/mailbox.py`: `edge_spent`, `bump_edge` reshaped to a plain increment,
  `read_edge` normalizing a partial file, `mailbox_reaped`, `read_meta_resolved`,
  `task_summary`, `_last_activity`, `fleet_digest`, `gated_send`; `write_message` carries
  the closure rule and the `hops` stamp; `transcript` forwards `hops` on sent entries;
  `resolve_recipient` refuses an ambiguous task-agent shortname.
- `cld/mcp/messenger.py`: `send` rewired to `gated_send`; `fleet_digest()` and
  `read_mailbox(name, since="")` added; `list_agents` docstring covers `task-agent`.
- `cld/messenger/send.py`: same gate, plus the `root=` fix so a peer send needs no host
  channel; prints `(hop n/limit)`.
- `cld/messenger/agent_loop.py`: **untouched**, as planned — and now covered by two tests
  that prove it (`TestSpentEdgeStopsTheFallback`).
- `cld/cli.py`: `_one_line` deleted in favor of `mailbox.task_summary(..., 72)`.
- New `tests/test_messenger_cli.py` (4 tests): the CLI send path is gated, and resolves a
  peer with no container enumeration. P5's headline finding would otherwise be untested.
- Docs: the `messenger-send` skill gained a hop-budget section and the full-name rule;
  CLAUDE.md gained the two tools and a hop-gate bullet.

**Two holes found by writing the tests and by reading the code back, neither in the plan:**

1. **Reaping a peer silently un-budgeted its edge.** `read_meta` is live-only, so once a
   peer's mailbox was archived, `write_message` stopped seeing a peer edge and the budget
   evaporated. Fixed by resolving the *recipient* side through the archive
   (`read_meta_resolved`). Found because a test I wrote for something else failed.
2. **Delivering to a reaped agent resurrected its mailbox.** `write_message` creates a
   missing mailbox — that is how a first message reaches a fresh container — so a
   supervisor reply to a peer reaped mid-turn would recreate the directory, shadow the
   archived `meta.json` behind an empty live one, and un-budget the edge from then on. Now
   refused outright, with its own error (`gated_send` distinguishes it from a spent edge by
   asking `edge_spent`). This is half of §14's tombstone/bounce item, so the spec's §10
   bullet and that item were both amended.

**A pre-existing test whose premise my change invalidated:**
`test_collision_suffixes_newcomer` resurrected an archived mailbox with `write_message` to
set up the collision. That path is now refused, so the setup switched to `ensure_mailbox`
— the real way a re-used slug gets a live mailbox (a fresh container at boot). The behavior
under test is unchanged.

**Clarification against the plan's wording:** B.1 said "deliver, then `bump_edge`". The
count *is* the envelope's audit stamp, so it cannot be assigned after the file is written —
the bump precedes the write. The ordering that matters is the one §C.3 argued for: the
check is before, the count after *the decision*. A crash between bump and write loses a
hop, which only tightens the ceiling.

**Small additions beyond the plan:** `gated_send`'s success return carries `to` (the
resolved name), which is what the CLI prints and what tells an agent who a shortname
actually reached; `read_edge` normalizes a partial file, since §10 has the master reset an
edge by hand.

**Read as prose, not just as tests** (`poetry run python` against a synthetic two-agent
fleet): the refusal error quotes `2/2` and names the master to escalate to, digest rows
truncate the task to its first line, and the transcript shows `hops` on both directions.

**Not verified here:** no container has run this. The e2e checklist in §F stands, and
§A.4's precondition — the operator's `~/.claude.json` must already have an
`mcpServers.messenger` entry — decides whether the MCP tools exist in a task-agent at all.
The CLI path is gated regardless, which is the second reason the gate sits in the transport.

---

# Appendix P6 — detailed implementation plan

> Scope: the in-master route (§9, §12), the broker action, the master's control-tower
> behavior (§7), docs, and the one piece of validation the design flags as most likely to
> surprise us (§9, N > 1 shared-store contention). Last part; needs P1–P5.

## A. Ground truth (verified in the tree, not assumed)

1. **The broker wire is `<action> <session> <base64-argv>`** in `$SSH_ORIGINAL_COMMAND`.
   The dispatcher validates the action charset, requires a matching `action_<name>`
   function, validates `session` against `^cld_master_[A-Za-z0-9_-]+$`, and resolves
   `$REPO` from that container's **host-set** `org.cld.repo-root` label. argv is decoded
   with `mapfile -d ''` and never `eval`'d, so a decoded token can only ever become an
   argument.
2. **`action_agent <target> <op> [args…]`** is the template to copy: `op` against a fixed
   set, `target` against the master's `org.cld.repo-root` **plus** its `org.cld.targets`
   label (host-set, unforgeable from inside), then a `.jj`/`.git` existence check, then
   `cd "$target" && exec cld agent …`. The target-validation block is ~8 lines that a
   task-agent action needs verbatim.
3. **`action_list_containers [kind]`** emits `name\tkind\trepo\traw-status`;
   `host_docker._parse_container_line` requires ≥4 fields and reads the first four, so
   appending fields would be backward-compatible — but nothing would consume them (§C.2).
4. **`_dispatch_agent_to_broker(cfg, op, extra)`** in `cli.py` is the container-side
   template: check `broker_available()`, resolve the target with `find_target_repo(cfg)`
   (which inside master is `resolve_master_target`, config/env only — no jj), then
   `raise typer.Exit(broker_agent_op(target, op, extra))`. It never returns.
5. **`cld broker` ships the whole argv inside the SSH command string**
   (`ssh … -- "$action $SESSION_NAME $payload"`, payload = `base64 -w0` of NUL-joined
   argv). A few KB of task text is ~1.35× that in base64 and well inside any sshd/ARG_MAX
   bound; a multi-hundred-KB task would not be (§G).
6. **`persona_resolve` accepts a path traversal.** Verified:
   `persona_resolve('../../../../../../etc/hostname', …)` returns `/etc/hostname` and it is
   readable. Host-side that is harmless — the human already has the file — but P6 is what
   makes it reachable *from a container*, which is a privilege escalation (§C.4).
7. **A task *file* path cannot cross the boundary.** `/workspace/current` is
   container-ephemeral (no host equivalent) and a sibling target is an empty placeholder
   with no bind mount, so a path that resolves inside master resolves to nothing — or to
   the wrong thing — on the host. Only `@name` refs and inline text can be forwarded
   (§C.3).
8. **Reading the fleet needs no host channel; only lifecycle does.** The mailbox root is
   bind-mounted into master, so P5's `fleet_digest()` / `read_mailbox()` and P4's
   `transcript` all work in-master today. Only spawn, roster-with-docker-state, logs and
   reap need the host.
9. **P4's name resolver degrades correctly in-master.** `_known_task_agent_names` unions
   docker with `list_fleet`; inside master `docker ps` fails (binary present, socket
   absent), so the union is mailbox-only — which is the right answer there. It does log an
   ERROR through `log_subprocess` on every call, which is noise worth suppressing (§B.2).
10. **P4 already has the hooks P6 needs:** hidden `--parent` on `start` and `shutdown`,
    `--force` as a plain flag the broker can simply never emit, and
    `_reap_all_task_agents(parent=…)` scoping a sweep to one master's fleet. `status` has
    **no** `--parent`, so an in-master roster would show every master's agents (§C.5).
11. **In-container guidance is delivered as skills**, not personas: `.claude/skills/` is
    baked to `/opt/cld/.claude/skills/` and auto-loaded via `claude --add-dir /opt/cld`.
    `agent-start` is the precedent for an operational, `user-invocable: true` skill with
    numbered steps; there is no master persona and nothing would load one (a master is an
    interactive container where the human runs `claude` themselves).
12. **`prompts/personas/` has no master/control-tower file**, and `AGENT_SYSTEM_PROMPT_FILE`
    is a `claude-run` mechanism — it is not read by the devcontainer entrypoint.
13. Baseline: **534 passed, 3 pre-existing failures**; `tests/test_host_docker.py` already
    has the mock shape for broker dispatch (`TestBrokerAgentOp`).

## B. Deliverables — exact surface

### B.1 `cld/broker.py`

```python
def broker_task_agent_op(target: str, op: str, extra_args: list[str] | None = None) -> int
```
Mirror of `broker_agent_op` against the `task-agent` action. No other change: the
enumeration seam stays as it is (§C.2).

### B.2 `cld/cli.py` — replace the five `_reject_in_master` calls

```python
def _dispatch_task_agent_to_broker(cfg: Config, op: str, extra_args: list[str]) -> None
```
Same shape as `_dispatch_agent_to_broker`, with a task-agent-specific "broker not
configured" message. Then per verb:

| Verb | In-master behavior |
|---|---|
| `start` | Rebuild argv from the parsed params and dispatch. `@ref` task files pass through **verbatim** (the host resolves them against the *target* repo); a plain path is read in-master and folded into the composed `-p` (§C.3). Never sends `--parent`; the broker sets it. |
| `status` | Dispatch, forwarding an optional `<name>`. The broker adds `--parent` so the roster is this master's fleet (§C.5). |
| `logs` | Dispatch with `<name>` and `-n <tail>`. |
| `shutdown` | Dispatch with `<name>` or `--all`. `--force` is refused **in-master, before dispatch**, with a message saying it is host-only — a clearer error than the broker's denial, which the agent would see as a raw exit code (§C.6). |
| `transcript` | **Runs locally** — the mailbox is mounted and no docker state is needed (§C.7). |

Plus: `status` gains a hidden `--parent` that filters the roster; `_known_task_agent_names`
skips the docker enumeration when `in_master_container()` (§A.9).

### B.3 `broker/cld-broker.sh` — `action_task_agent`

```sh
validate_target()      # extracted from action_agent, used by both
action_task_agent()    # <target> <op> [args…]
```
- `op` ∈ `start|status|logs|transcript|shutdown` (no `restart`: P4 has no such verb).
- `target` validated by the shared helper — `org.cld.repo-root` + `org.cld.targets`, then
  a repo existence check.
- **Refuse `--force` and any caller-supplied `--parent`** anywhere in argv, then append
  `--parent "$session"` itself. Rejecting rather than overriding keeps it auditable and
  avoids depending on which duplicate Click would win.
- **Refuse a persona argument containing `/`** as defense in depth behind §C.4's fix.
- `cd "$target" && exec cld task-agent "$op" "$@" --parent "$session"`.

### B.4 Three skills for the control tower (§7, §12)

Modeled on `agent-start`: numbered steps, a master-container precondition check, and an
explicit "report back" step. One per moment the master actually acts.

- **`task-agent-start`** — resolve the target (`cld master repos`), pick a short kebab slug
  from the task, choose a role persona, write the task, and **draw the graph**: `--peer` is
  repeatable and only an *already-spawned* agent can be named, so the later-spawned side
  declares the edge and carries its budget; the earlier one participates by replying.
  Mentions the cap and the live-stack anchor refusal as things to plan around, not hit.
- **`task-agent-fleet`** — the per-turn crank: `fleet_digest()`, compare `msg_count` /
  `last_activity` with last turn, `read_mailbox(name, since=…)` **only** for members that
  moved, route replies, and report what moved (including reaps) in the turn output. States
  plainly why not to sweep inboxes: they drain within ~1 s, so a sweep shows nothing and
  costs context.
- **`task-agent-wrapup`** — decide done → instruct wrap-up (for a **different** repo, ask
  for a *pushed* branch and say so) → verify by the standard that actually applies (local
  `jj log`/`jj diff` for the master's own repo; the pushed branch/MR for a sibling; a
  self-report is **not** verification and must not be described as one) → reap → report.
  Also carries the two now-mechanical rules: a handoff is **reap-then-spawn**, and a reap
  refusal means *wrap-up did not finish*, not "try again with --force" (which the master
  cannot do anyway).

### B.5 `cld/prompts.py` — close the traversal

`persona_resolve` validates the name against `^[A-Za-z0-9._-]+$` before joining, so a
persona is always a file *under* a `prompts/personas/` directory. Fixes §A.6 for every
caller, not just the broker route.

### B.6 Docs

- `CLAUDE.md`: task-agent bullet in **What This Repo Is**; the broker action list gains
  `task-agent` (and the note that it never accepts `--force`); a short in-master usage note
  under Messenger or the broker section.
- `docs/design-task-agents.md`: mark the in-master route as implemented where §9 describes
  it as prospective ("the broker's `agent` action is extended **or** a sibling `task-agent`
  action added" — record which).

### B.7 Validation (§9's "most likely place this surprises us")

Two host-side runs, results recorded in Scratch:
1. **Concurrency smoke test** — a master plus **3** task-agents on one repo, all with
   Watchman auto-snapshot, each editing files and committing. Watch for `jj` op-log lock
   contention: retries/timeouts in `cld task-agent logs`, wall-clock on `jj status`, and
   whether any agent's snapshot is lost. Record what happens even if nothing does — "we
   ran 3 and saw nothing" is the result the design is missing.
2. **The three open e2e checklists** (P2 §F, P4 §F, P5 §F) in one sitting, since P6 is when
   a master can finally drive the whole loop.

## C. Decisions taken here

1. **A separate `task-agent` broker action, not an extension of `agent`.** §9 left the
   choice open. Separate wins: the op sets differ (`transcript` vs `restart`), the argv
   policy differs (`--force`/`--parent` control), and an action is the broker's unit of
   auditing — `declare -F` is the allowlist, so one action per capability keeps "what can a
   container ask the host to do" readable. The shared part (target validation) is extracted
   rather than duplicated.
2. **The `list-containers` wire format is left alone**, deviating from §12's "extend
   parsing to carry `kind`/`parent`/`task`". That suggestion presumes the in-master roster
   is *built* in-master; delegating `status` whole (§B.2) computes it host-side where the
   labels already are, so the extra fields would have no consumer. `_parse_container_line`
   and `_list_via_local_docker` already return the same four keys, so the two views match
   today.
3. **A task *file* is folded into `-p` in-master; `@refs` pass through.** §A.7 makes a path
   unforwardable, and the two cases want opposite handling: an `@ref` must resolve against
   the *target* repo (host-side), while a real path only exists in-master. Composing the
   file body plus the inline prompt in-master reproduces exactly what the entrypoint and
   `TaskMode` would have composed, so the agent sees the same text either way.
4. **The persona traversal is fixed in `persona_resolve`, not only in the broker.** The
   broker's job is to constrain what a container may ask for, but a function whose contract
   is "a persona name under `prompts/personas/`" should not accept `../../etc/passwd` from
   *any* caller. The broker still rejects a persona containing `/` — cheap, and it keeps
   the trust boundary legible where it is enforced.
5. **`status` gains a hidden `--parent` that scopes the roster.** P4 made the roster
   host-wide on purpose (a human hunting orphans wants everything), but a master seeing
   other masters' fleets is noise and mild leakage. The detail view (`status <name>`) stays
   unscoped: it reads a mailbox the master can already read through the bind mount, so
   gating it would be theatre.
6. **`--force` is refused in-master before dispatch, in addition to the broker's refusal.**
   The broker's denial reaches the caller as a nonzero exit and a stderr line; a local
   refusal gives the actual reason ("host-only — a master cannot override a reap refusal").
   Two checks, but only one of them is the security boundary; the other is an error message.
7. **`transcript` runs locally in-master.** The mailbox is mounted and no docker state is
   involved, so delegating would add a host round-trip and make transcripts unavailable to
   a master with no broker key configured. This is the one verb where the local path is
   strictly better, and P4's resolver already degrades to the mailbox-only view there.
8. **Control-tower behavior ships as three skills, not a persona.** Nothing loads a persona
   for an interactive master (§A.11–12), and the repo's established vehicle for
   in-container guidance is a skill. Three rather than one, matching the five `messenger-*`
   skills' granularity: the three moments (spawn / crank / wrap-up-and-reap) have different
   triggers, and a single omnibus skill would be selected for all of them and read for none.
9. **The per-turn crank stays manual, as §7 says.** A skill is invoked, not standing, so
   "reconcile the fleet each turn" remains a thing the human asks for or the master chooses.
   A `UserPromptSubmit` hook injecting the digest would remove the crank — that is exactly
   §14's turn-injected notice, and it is out of scope here.

## D. Seams / what stays out

- **No new broker read action** (D25): cross-repo verification goes through the pushed
  branch, which is why `task-agent-wrapup` insists on a push for sibling repos.
- **Deferred, unchanged:** VCS blackboard, progress budget, spend ceiling, turn-injected
  notice, automatic reaper, retiring `cld agent` (§14).
- **`cld chain run` from inside master** remains unsupported; task-agents do not change
  that.
- If the concurrency smoke test finds real contention, the follow-up is a design question
  (serialize snapshots? lower the cap default?), not a P6 code change — record and stop.

## E. Commit sequence (each step green on its own)

1. **`persona_resolve` hardening** + tests (traversal rejected, ordinary names and
   `name.md` still resolve). Independent of everything else.
2. **Broker action**: `validate_target` extraction + `action_task_agent`; `bash -n` and a
   `shellcheck`-style read-through; README + CLAUDE.md action list.
3. **`broker_task_agent_op`** + `_dispatch_task_agent_to_broker` + the five verbs' in-master
   branches + `status --parent` + the `_known_task_agent_names` docker skip, with tests.
4. **Three skills** + the CLAUDE.md architecture bullet + the §9 note in the design doc.
5. **Validation**: run B.7's two host-side items and record the results in Scratch.

## F. Test matrix

| Area | Cases |
|---|---|
| `persona_resolve` | `../` traversal raises; absolute path raises; `implementer` and `implementer.md` still resolve; repo-before-cld precedence unchanged |
| `broker_task_agent_op` | forwards `("task-agent", target, op, *extra)`; propagates the exit code (mirrors `TestBrokerAgentOp`) |
| in-master `start` | dispatches instead of launching; argv carries persona, `-n`, composed `-p`, `--branch`, `-m`, `-r`, each `--peer`; an `@ref` task file is forwarded verbatim; a real path is read and folded into `-p`; **no** `--parent` in the argv |
| in-master `status`/`logs`/`shutdown` | dispatch with the right op and argv; `shutdown --force` refused locally with a host-only message; `--all` forwarded |
| in-master `transcript` | works **without** the broker (no `cld broker`, no docker) against the mounted mailbox |
| in-master resolver | `_known_task_agent_names` makes no docker call inside master and resolves a bare slug from mailboxes alone |
| `status --parent` | roster filtered to that master's fleet; detail view unaffected |
| broker script | `bash -n`; op allowlist rejects `restart` and garbage; `--force` and a caller-supplied `--parent` denied; persona containing `/` denied; target not in the labels denied; `--parent $session` appended |
| host-side (needs a daemon) | B.7's two runs, plus: a master spawns two task-agents on its own repo and one on a registered sibling, draws an edge between two of them, drives them to wrap-up, and reaps — with a reap refusal observed at least once (mid-turn or live-peer) and the `--force` denial confirmed through the broker |

Broker-script assertions run as `bash` snippets against the real script with a stubbed
`docker`/`cld` on `PATH` (the pattern the repo uses for `bash -n` checks today, extended
with stubs), since none of it is Python.

## G. Risks

- **The broker is the security boundary, and P6 widens it.** Before this, a container could
  ask the host for: tests, a container list, and `cld agent <op>`. After, it can ask for a
  *new container with a mounted file of its choosing* — which is why §C.4 matters and why
  the op/flag allowlists are explicit rather than pass-through. Worth re-reading
  `action_task_agent` as an adversary once it exists.
- **Argv size.** A task description forwarded as `-p` rides in the SSH command string
  (§A.5). Kilobytes are fine; a hundred-KB task would need a different channel (a staged
  file, which is a design change). Note the bound in the skill rather than silently
  truncating.
- **A 60 s readiness wait holds the SSH connection.** `cld task-agent start` host-side
  waits for the sentinel, so the master's Bash call blocks that long on a cold image build
  it may also trigger. Same property `agent-start` already has; worth a line in the skill so
  the master does not conclude it hung.
- **Concurrency is the real unknown** (§9). Three agents on one store is the first time this
  is exercised; the failure mode to watch for is a lost Watchman snapshot, not a crash,
  because that one is silent.
- **Skills are guidance, not enforcement.** Everything in B.4 is prose a model may skip.
  The rules that *matter* are already mechanical (cap, anchor refusal, reap checks, hop
  gate); the skills exist to keep the master from walking into them, and should be judged
  by whether they make the refusals rare rather than by whether they are followed.

## H. Outcome (2026-08-12)

Commits 1–4 landed. **553 tests pass (was 534 — 19 new), same 3 pre-existing failures.**
**Commit 5 (validation) could not run: no docker daemon in this environment** — it is the
only outstanding work in P1–P6 and is listed in Scratch.

- `cld/prompts.py`: `persona_resolve` validates the name against
  `^[A-Za-z0-9][A-Za-z0-9._-]*$`, closing the traversal §A.6 verified.
- `broker/cld-broker.sh`: `validate_target` extracted from `action_agent`;
  `action_task_agent` added (op allowlist, `--force` and caller `--parent` denied,
  persona-must-be-a-bare-name, `--parent "$session"` appended).
- `cld/broker.py`: `broker_task_agent_op`.
- `cld/cli.py`: `_dispatch_task_agent_to_broker`, `_task_agent_start_argv`, in-master
  branches on start/status/logs/shutdown, local `transcript`, `status --parent` scoping,
  `_task_agent_rows(cfg, parent)`, and the `_known_task_agent_names` docker skip.
- Three skills (`task-agent-start`, `task-agent-fleet`, `task-agent-wrapup`), plus
  cross-references from `agent-start` ("wrong skill for one bounded task") and
  `messenger-agents` (the new kind, and full names for task-agents).
- Docs: CLAUDE.md architecture bullet + broker action list + the in-master note in Key
  Commands; `broker/README.md` action list; `docs/design-task-agents.md` §9 records
  that a *separate* action was chosen and why.

**A pre-existing bug P6 tripped over, now fixed:** `typer.Exit` subclasses `RuntimeError`
(via `click.exceptions.Exit`), so `_handle_errors` caught it and re-raised `typer.Exit(1)`.
Every deliberate exit code became 1 — including the **0** that an in-master broker dispatch
raises on success, which means **`cld agent status` from inside master has always reported
failure** even when the broker succeeded (the output streams, so a human would not notice;
a script checking `$?` would). Fixed by re-raising `typer.Exit` ahead of the general
handler, which also removes the redundant `Command failed: 1` line after every clean
`Error: …` (the cosmetic wart recorded in P4 §H). Three regression tests pin it.

**Two bugs of my own, caught by running the thing rather than by tests:**

1. **The broker's persona check rejected ordinary task text.** My first version scanned
   *all* argv for a `/`, so `-p "fix cld/cli.py"` was denied. The check now applies only to
   `start`'s first positional, which is the only argument that becomes a mounted path.
2. **`--parent` was appended to every op, and `logs` has no such option** — so an in-master
   `cld task-agent logs` would have died on "No such option". It is now appended only for
   `start`/`status`/`shutdown`, i.e. where ownership, scope or authority actually apply. A
   read needs no authority, which is the same reasoning that leaves `status <name>`
   unscoped.

Both were found by exercising `cld-broker.sh` directly with a stubbed `docker`/`cld` on
`PATH` (eight cases: happy path, `--all`, `--force`, caller `--parent`, persona traversal,
bad op, unregistered target, registered sibling) — worth keeping as the way to test this
script, since none of it is Python.

**Deviations from the plan:** `transcript` was also dropped from the broker's op allowlist
(the plan had it there) — the container route never delegates it, and dead surface in a
security allowlist is worth removing. Otherwise as planned.

**Not verified here:** nothing has run in a container. The broker action has never been
exercised over real SSH, and §B.7's two items — the N>1 shared-store contention smoke test
and the three accumulated e2e checklists (P2 §F, P4 §F, P5 §F) — remain open.
