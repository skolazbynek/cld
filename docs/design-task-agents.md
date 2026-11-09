# Task-scoped agents (`cld task-agent`)

> **Superseded in part (implemented):** the kickoff's middle layer is gone. The
> launcher composes N prompt refs plus `-p` into one brief host-side and ships it in
> the anchor scratch (`.cld-run/brief.md`); the supervisor layers the lifecycle
> preamble and then that brief. Wherever this document says `/config/persona.md`,
> `/config/task.md`, `AGENT_PERSONA_FILE` or a bare-persona-name argv rule, see
> `docs/design-prompt-chaining.md`. `AGENT_PERSONA` survives as the roster's display
> name (the first persona-kind ref).

> Status: design, POC scope. Date: 2026-08-11.
> Supersedes the mental model in which an agent is one immortal worker per repo.
> Feeds the implementer; this document is the spec, not the code.
> **Related (next step):** `docs/design-blackboard-coordination.md` — the VCS
> **blackboard**, a second (bookmark-based) coordination edge. This POC ships
> master↔agent **and** agent↔agent messaging over the mailbox (§7); only the
> blackboard edge is deferred to that follow-on.

## 1. The shift

Today a `cld agent` is a **repo**: exactly one per repo host-wide, immortal, one
fixed persona, one `claude -p --resume` session that processes mailbox messages
forever. There is no task identity, no per-launch prompt/persona, no completion
signal, and no auto-shutdown. The human owns its lifecycle.

We are inverting three of those properties. A **task-agent** is a **task**:

| Property | `cld agent` (today) | `cld task-agent` (this design) |
|---|---|---|
| Scope | one per repo | many per repo, one per task |
| Lifespan | immortal | bounded — lives until its task is done |
| Persona / task | fixed at build | chosen by the master at spawn |
| Lifecycle owner | the human | the **master** (with a manual host fallback) |
| Identity | `cld_agent_<repo>` (removed) | `a.<ticket>.<task-slug>` |

It is deliberately a **fusion of `cld run` and `cld agent`**: `cld run`'s
task+persona launch and branch deliverable, plus `cld agent`'s persistent,
resumable, conversational session. The new capability that neither has is
**interactive multi-turn steering** — the master clarifies, corrects, and
redirects the agent across many turns before deciding the task is done and
tearing it down.

A master runs a **control tower**: it spawns several task-agents (on its own
repo and/or registered sibling targets), drives each by messenger, and reaps
each when its task lands.

## 2. Scope and non-goals (POC)

**In scope:**
- New standalone command surface `cld task-agent` (start / status / logs /
  transcript / shutdown), coexisting with `cld agent`.
- Master-generated, human-readable, task-scoped names; many per repo.
- Per-spawn persona + task prompt + optional task file.
- Master-owned lifecycle: the master decides *done* and reaps its own fleet
  freely; teardown is bounded by **mechanical reap-readiness checks** rather than
  a human gate, and only the human can override a refusal (§7).
- Master-assigned deliverable branch that survives teardown; optionally pushed to
  the remote on wrap-up, which is what makes a cross-repo deliverable verifiable
  (§9).
- Sequential handoffs anchored only on **finished** work, enforced at spawn (§8).
- Agent↔agent messaging over the mailbox along **master-drawn edges** (§7): the
  master decides who may talk to whom; agents never cold-message and never spawn.
- Per-edge **absolute hop budget**, transport-enforced, to bound peer↔peer
  ping-pong (§10).
- Mailbox tree as the master's fleet registry; docker labels as host ground
  truth. A fleet **digest** surface so the master's per-turn sweep is cheap (§7).
- Configurable per-master cap (default 4); inbox archived (not deleted) on
  shutdown; manual host-side shutdown as the no-reaper escape hatch.

**Explicit non-goals for this POC:**
- **No turn-injected inbox notice.** The control tower is *manually cranked*:
  the master sweeps its fleet's inboxes each turn it takes. Push notification is
  a future addition. (Consequence in §7.)
- **No automatic reaper.** Orphaned agents (master died) are cleaned up manually
  from the host. (§10.)
- **No removal of `cld agent`** yet. It stays until task-agents prove it
  obsolete; removal is then a clean delete of the `agent` CLI + name derivation.
- **No VCS blackboard.** The second coordination edge — agent↔agent via watched
  bookmarks — is the next step (`docs/design-blackboard-coordination.md`). In
  this POC every edge is messaging over the mailbox.
- **No progress budget.** Peer-loop control ships with the **absolute** hop
  budget only. The progress budget (reset by VCS activity) is a separate design
  choice, deferred to post-POC (§10, §14) — its progress signal is not
  edge-scoped and needs its own design pass.
- **No spend ceiling.** Cost is reported, not capped (§14).
- No changes to `cld run` or `cld chain`.

## 3. Mental model and vocabulary

- **Master** — interactive, one per repo (unchanged). The control tower.
- **Task-agent** — headless peer container, one per task, spawned by a master.
- **Fleet** — the set of task-agents whose parent is a given master.
- **The graph** — the master decides, per agent at spawn, whom it may message
  (its *edges*): the master and a set of named peers, nobody else.
- **Peer** — another task-agent a given agent is allowed to message (§7).
- **Session bookmark** — the jj lifecycle pointer named after the container
  (`SESSION_NAME`); forgotten on shutdown, as today.
- **Deliverable branch** — a *separate*, durable bookmark the master assigns at
  spawn; the agent squashes its result into it; it **survives** teardown. This
  is the reconvene point.
- **Anchor** — as today: the immutable commit the agent's work descends from.
  Each task-agent gets its own anchor (default the target repo's `@` at spawn,
  overridable with `-r`, which may point at a *finished* sibling agent's
  deliverable branch but never inside a live agent's stack — §8).

## 4. Command surface

`cld task-agent` mirrors `cld run`'s launch ergonomics and `cld agent`'s
lifecycle verbs. Runs on the host directly, or from inside a master where it is
mediated by the host broker (same seam as `cld agent`, §9).

**Grammar is verb-first**, matching `cld agent <op>` / `cld master <op>` and the
rest of the CLI. An agent-name positional never occupies the subcommand slot (a
dynamic positional there is inconsistent with the existing surface and awkward in
Typer/Click).

```bash
# Spawn (master's own repo or a registered sibling target, cwd-based)
cld task-agent start @<persona> -p "<task>" [taskfile.md|@<name>] \
    [--branch <name>] [-m <model>] [-r <revision>] \
    [--peer <name>[:<hops>]]…
#   @<persona>       resolved like chain personas (repo prompts/personas, then cld)
#   -p / file        the concrete task handed to the agent at kickoff
#   --branch         deliverable branch name (default: the generated task-slug)
#   -r               anchor revset (default @; may be a *finished* sibling's
#                    deliverable branch — refused inside a live agent's stack, §8)
#   --peer           one edge — a peer this agent may message (§7), plus that edge's
#                    absolute hop budget (default: cfg.peer_absolute_limit, §10).
#                    Repeatable; default: no peers, master only. Container names
#                    cannot contain ':', so the delimiter is unambiguous.
#   --parent         stamped automatically when launched from a master; not user-set

# Roster + inspection
cld task-agent status [<name>]        # roster, or one agent: phase, cost, branch, anchor, peers
cld task-agent logs <name> [-n N]     # supervisor stderr (state + cost), NOT the conversation
cld task-agent transcript <name>      # the mailbox exchange (the actual conversation)

# Teardown
cld task-agent shutdown <name>        # stop + rm; forget session bookmark; archive mailbox
cld task-agent shutdown --all         # every task-agent (host-wide), by label
cld task-agent shutdown <name> --force  # host-only: override a reap-readiness refusal (§7)
```

Naming decisions:
- **Name:** `a.<ticket>.<task-slug>` (`a._host.<task-slug>` with no ticket), see
  `docs/design-bookmark-naming.md`. Enumeration keys on the **label**, not the name.
- **Slug:** the **master generates it** from the task (short, kebab-cased). It
  is a human-readable handle, not a UUID.
- **`<name>` accepts a bare slug at the CLI.** Every command taking `<name>`
  resolves a bare slug against the last dot part of every known identifier (`cld
  task-agent status add-oauth-login`), and accepts the full identifier too.
  Ambiguity (same slug live under two tickets) is an error naming the
  candidates. **Mailbox addressing is unaffected — that is always by full
  container name** (§7); the shortcut is a CLI affordance only, so a human never
  types `a.lide-1.add-oauth-login`.
- **Collisions:** before spawning, the master checks the registry (§6); on a
  live-name collision the launcher appends a short suffix (`-2`, `-3`, …). The
  master should prefer picking a fresh slug so names stay meaningful.

## 5. Lifecycle

```
spawn ─▶ KICKOFF ─▶ IDLE ⇄ PROCESSING ─▶ … ─▶ (master: done) ─▶ teardown
```

There is no `WRAP-UP` phase: wrap-up is an ordinary message processed in
`PROCESSING` like any other (§12 keeps the state machine unchanged).

1. **Spawn.** Master issues `cld task-agent start …`. Anchor is resolved and
   staged peer-side exactly as `cld run`/`cld agent` do (`AGENT_REVISION_HINT` +
   `AGENT_SCRATCH`, workspace add, scratch commit `B`, `AGENT_ANCHOR_HASH` ==
   `B` in isolated mode (the default; `A` in shared mode) -- see
   PRODUCT_DESIGN.md §8). The
   peer entrypoint sets the **session bookmark** and creates the **deliverable
   bookmark** at the anchor. Labels are stamped (§6). The container runs
   detached (`docker run -d`), not `--rm` — it is long-lived while working.
2. **Kickoff.** The supervisor boots one Claude session (no `--resume`) with the
   composed kickoff prompt (§11) and records the session id.
3. **Converse.** Each inbound message resumes that session (`--resume`), one at
   a time, strict FIFO. Reply = a `send()` back to the sender. Work is
   auto-committed via Watchman/jj (durability is free; nothing is lost on
   teardown).

   **Reply obligation is declared by the sender, not implied by arrival.** The
   envelope carries two fields: `expects_reply` (this message opens an obligation on
   the recipient) and `answers` (the message id this one discharges). Both default to
   "no", and there is **no hardcoded exception for the master channel** — a master
   that wants an answer asks for one like anybody else.

   An unconditional "every message you receive gets exactly one reply" was the
   original rule, and it is a loop engine: a reply is a message, so it obliges a
   reply, so an acknowledgment obliges an acknowledgment, forever. Observed in
   testing — two agents traded courtesies until the hop budget ran out, which means
   every exchange terminated by exhaustion rather than by agreement. The prior art is
   unanimous that the fix is a type distinction, not a better prompt: FIPA-ACL's
   `inform` vs `request`, JSON-RPC's notification (no `id` ⇒ the receiver MUST NOT
   respond — the protocol the messenger MCP already runs on), A2A's terminal task
   states, CAMEL's `<CAMEL_TASK_DONE>` after documenting this exact "infinite loop of
   mutual thanks", AutoGen's `is_termination_msg` alongside its blunt counter.

   **A reply may itself oblige a reply**, and must be able to: A asks, B needs a
   clarification first, A answers the clarification, B finally answers A. B's
   clarification does not discharge A's question — the root obligation persists
   through the sub-dialogue and is settled separately. So the rule is not "a reply
   cannot ask"; it is that *asking is an explicit act* and arrival is not.

   **The exactly-one-reply guarantee survives, scoped twice over.** It fires only for
   a message that set `expects_reply`, and only checks for a line addressed to *that
   message's sender*. The recipient-scoping matters independently: a plain "did the
   outbox grow" check (`outbox_changed_since`) is satisfied by an unrelated send, so
   an agent that answers a peer while the master waits would suppress the master's
   fallback. The `to` field is already on every outbox line, so that half is a scan of
   the tail, not a schema change. A claude *failure* is still reported to the sender
   unconditionally — it is information no other channel gives them, not an
   acknowledgment.

   **The guarantee is bounded by the edge budget.** A reply cannot be promised on a
   channel the transport has closed: once the edge to the sender is spent (§10),
   neither the agent's `send()` nor the supervisor's fallback is delivered. The
   guarantee holds per edge, while that edge is open — which is also what stops a
   spent edge from turning the guarantee into a loop engine.
4. **Done.** The master decides the task is finished (§7). It instructs the
   agent to **wrap up**: squash/rebase its work into the deliverable branch,
   optionally push that branch to the remote, and report it. The master verifies —
   locally with `jj log`/`jj diff` on its own repo, via the pushed branch/MR for a
   sibling repo, or not at all if the agent only self-reports (§9) — then shuts the
   agent down (gated, §7).
5. **Teardown.** `docker stop` → `docker rm` → **the caller** forgets the session
   bookmark (never the deliverable branch) and archives the agent's mailbox to
   `~/.cld/mailboxes/_archive/<name>/` (§6).

   **Cleanup is caller-side, not a supervisor SIGTERM act.** `request_stop` only
   sets a flag that is checked *between* messages, and a `claude -p` turn
   routinely outlasts `docker stop`'s 10 s grace, so a task-agent is normally
   SIGKILLed mid-turn and any "last act" in the supervisor never runs. Both
   cleanup steps are therefore performed by the shutdown caller (host CLI, or the
   broker's shutdown path when the master initiates) against the bind-mounted
   mailbox tree and the origin store — exactly as `_shutdown_persistent_container`
   already does for the bookmark today. The in-container SIGTERM handler stays as
   a best-effort fast path; nothing depends on it, and both operations are
   idempotent so running them twice is harmless. Mid-turn kill loses no work:
   Watchman snapshots are already in the store (§8, recovery path).

The supervisor is the **same state machine** as `agent_loop.py`, parametrized
for task mode (§12). The only behavioral deltas are: kickoff prompt composition,
identity/labels, deliverable-branch handling, and archive-on-shutdown.

## 6. Registry and discovery

Two views, reconciled by role:

- **Mailbox tree (master's roster).** The whole mailbox root is bind-mounted
  into the master, so the master enumerates its fleet by reading mailbox dirs
  directly — no docker socket, no broker. Each task-agent writes a small
  `meta.json` into its own mailbox dir at boot. The master filters
  `parent == self` (its own `SESSION_NAME`, which is stable across master
  restart) to rebuild its fleet — including **after a master restart**,
  satisfying rediscovery with no extra bookkeeping.

  **`meta.json` holds immutable spawn facts only** — `{parent, task, persona,
  deliverable_branch, anchor, peers, created_at}`, where `peers` maps each peer's
  full name to that edge's hop budget (§4, §10) — written once
  at boot and never updated. It deliberately carries
  **no `status`/`phase` field**: liveness already lives in `state.json`, which the
  supervisor rewrites on every transition (`phase`, `msg_count`,
  `cost_usd_total`, `current`). Two files with an overlapping status field would
  drift, and the stale one would be the one the master reads. Split by lifetime:
  `meta.json` = what this agent *is*, `state.json` = what it is *doing*. The
  `peers` mapping also makes the graph readable from the filesystem, which is what
  lets shutdown mechanically refuse to reap a live peer (§7).
- **Docker labels (host ground truth).** `cld task-agent status` on the host
  enumerates by label:
  - `org.cld.kind=task-agent` (distinct from `agent` — the real discriminator),
  - `org.cld.parent-master=<master session>`,
  - `org.cld.repo-root=<host path>`,
  - `org.cld.task=<task-slug>`.

Why both: a mailbox dir means *a task-agent was started*; a running container
means *it is alive*. A crashed agent leaves a mailbox dir but no container —
which is exactly the "mailbox exists, container dead" signal `cld task-agent
status` surfaces for manual cleanup (§10). The broker's `list-containers` action
is extended to carry `kind`, `parent`, and `task` so the in-master view matches
the host view.

## 7. Master interaction model & the agent graph (the control tower)

Primary pattern: **interleaved control tower**. The master fans work out to
several agents, then routes replies as they arrive — it does **not** block on
any single agent.

Because there is **no turn-injected notice** in this POC, the tower is manually
cranked. The *policy* is persona-level — at the start of each turn, reconcile the
fleet before yielding to the human — but it needs **two new read surfaces**,
because the obvious ones do not do the job:

- **Sweeping inboxes finds nothing.** A task-agent archives each message
  immediately after processing it and polls at 1 s, so a peer's `inbox/` is empty
  within about a second of delivery. Peer↔peer traffic lives in `archive/` and
  `outbox.log` by the time the master looks. §7's observability claim only holds
  if the sweep reads those, not `inbox/`.
- **`list_inbox` cannot read another mailbox.** Every messenger tool operates on
  the *calling* container's mailbox (`_own_name()`); `list_inbox` takes no name
  argument. "`list_inbox` across the roster" has no implementation today.

So the messenger gains two **read-only, fleet-scoped** tools (additive;
`list_inbox` keeps its own-mailbox semantics):

- `fleet_digest()` — one row per fleet member, from `meta.json` + `state.json`
  and directory mtimes: `{name, task, phase, msg_count, cost_usd_total, unread,
  last_activity}`. Cheap, bounded, and no message bodies.
- `read_mailbox(name, since=…)` — the full exchange for one agent: `inbox/` +
  `archive/` (received) and `outbox.log` (sent). `since` is exclusive, so the master
  passes the timestamp of the last entry it saw. There is deliberately no
  `include_archive` toggle: switching it off could only hide the *received* side, which
  is drained into `archive/` within ~1 s and is exactly what this tool exists to show.

Both are restricted to mailboxes whose `meta.json` has `parent == self`, so a
master reads its own fleet and nothing else.

**The digest is what makes the crank affordable.** Pulling N full inboxes every
turn fills the master's context with fleet chatter on turns where the human asked
something unrelated — degrading the tower exactly when it needs the room. Instead
the master calls `fleet_digest()` (cheap), compares `msg_count`/`last_activity`
against what it saw last turn, and calls `read_mailbox` only for members that
moved.

- *Pro:* zero new transport; works on today's mailbox; keeps the human in the
  loop.
- *Con:* nothing advances *in the master* between turns; if the human walks away,
  replies sit until the next prompt (the agents themselves keep working — they
  poll at 1 s). Accepted for POC; the turn-injected notice is the future upgrade.

**The master reaps its own fleet freely; what is gated is reaping something that
isn't ready.** Deciding done, asking for wrap-up, verifying the branch and tearing
the container down are all the tower's job, and a human confirmation on each one
would be friction without a protected asset behind it. Take the inventory: work
survives teardown as Watchman snapshots in the shared store (§8 recovery path), the
deliverable branch is a durable bookmark (D8), the conversation is archived not
deleted (D7), and cost/state live in the mailbox's `state.json`. The one thing
`docker rm` destroys is the agent's **Claude session context** — `~/.claude` is not
among `home_mounts_always`, so its transcripts are ephemeral and the recorded
`session_id` becomes unresumable. For a *finished* agent that context is spent by
definition, which makes reaping it effectively free.

So the design gates state, not permission. Three **reap-readiness checks**
enforced by the shutdown path, all of them filesystem or label reads, no human in
the loop:

1. **Not mid-turn** — the target's `state.json` phase is not `processing`.
   Shutdown waits briefly, then reports what it is waiting on.
2. **Not a live peer** — refuse if the target appears in another *live* fleet
   member's `meta.json` `peers` mapping, naming the dependents. This is the §10
   invariant: reaping mid-exchange silently breaks a second agent's reply
   guarantee.
3. **Own fleet only** — the master can only reap containers whose
   `org.cld.parent-master` is itself. Already mechanical via the label.

**Work-captured is not a fourth check — it is the §9 verification the master
already did.** §5 step 4 has the master verify the deliverable *before* reaping, so
a separate store-side "did the squash happen" test at teardown would be a second
mechanism answering a question §9 has already answered. The evidence standard is
§9's, and which form applies depends only on whether the agent worked in the
master's own repo:

- **Master's own repo** — local `jj log`/`jj diff` on the deliverable branch. The
  repo is mounted, so this is direct and free.
- **A different repo** — the pushed branch/MR where pushing is applicable, else the
  agent's **self-report**. Master has no filesystem view of a sibling target, and no
  broker action is added for one (D25).

So this precondition is an *obligation on the master*, not a refusal the launcher
can enforce — and for the self-report case it cannot be mechanical at all, since the
evidence is the agent's word. If the master gets it wrong and reaps early, the
outcome is §8's already-accepted recovery path: the work itself survives as Watchman
snapshots in the store (`jj log -r 'heads(all())'`), the deliverable branch is
merely empty or behind. That is the same risk §8 already documents for a premature
master call, so nothing new is being accepted here.

**`--force` is host-only.** The master reaps freely but cannot override a refusal;
`--force` is unreachable from the broker's action set, so it stays a human act.
That keeps a boundary the gated party can't forge, but puts it around the two
operations that have a victim — discarding uncaptured work, and breaking a third
agent's edge — instead of around every reap.

Rejected: a human authorization token per reap (a host-side file the master cannot
create). It gates the only fleet operation that *frees* resources, so a master
blocked from reaping rationally leaves finished agents idling — holding RAM and
counting against `max_task_agents`, causing the exhaustion the cap exists to
prevent. Spawning, which is what actually burns money and creates state, would have
stayed ungated. And anyone running a real fleet would pre-authorize permanently
after the first stall, leaving a step plus a false sense of safety. The
human-interrupt patterns this borrowed from (LangGraph `interrupt()`, AutoGen
`human_input_mode`, A2A `input-required`) all gate acts with *external* effects —
sending, merging, publishing. Reaping your own worker is internal cleanup.

What the token would have bought is **legibility** — a sync point where the human
sees the summary before the evidence disappears. That is a reporting problem, so it
gets a reporting fix: the master reports reaps in its turn output, `transcript`
reads the archived mailbox, and `status` shows the roster. The turn-injected notice
(§14) improves it further.

Consequence to accept: nothing stops the master reaping a **finished but unreviewed**
agent, so the deliverable branch becomes the sole review artifact. That is what the
human reviews anyway and it survives teardown — but it is another reason the master
should have cross-repo agents *push* their branch (§9) rather than leave it local,
where only someone with access to that host's store can see it.

Everything else stays persona-level guidance, where being wrong is recoverable:
slug freshness (§4), and summarizing a finished task to the human.

**Serial babysitting** (drive one agent to done before the next) remains
available simply by spawning one agent at a time; no separate mode is needed.

### The agent graph (agent↔agent over the mailbox)

Agents don't only talk to the master — they talk to each other over the same
mailbox, but only along edges the master draws.

- **The master draws the graph.** At spawn, the master tells each agent which
  peers it may message, by full name, in the kickoff prompt. An agent messages
  only the master or a named peer — it never cold-messages an agent it wasn't
  introduced to, and it never spawns one (spawning stays master-only).
- **Why peer edges at all.** Agents auto-poll their own inbox ~every second; the
  master is manually cranked (above). So a peer→peer handoff propagates at agent
  cadence, whereas the same handoff *relayed through the master* costs two
  crank-hops and a copy-paste. Brokered edges buy that speed without an open
  mesh's failure modes.
- **Edges are asymmetric, and that is accepted.** A peer can only be named once it
  exists, and `start` has no way to pin a slug (the master generates it, and the
  launcher may append a collision suffix — §4). So the **later-spawned** agent
  declares the edge and carries its budget; the earlier one has no peer entry and
  participates by *replying*, which the reply short-circuit always allows.
  Consequence for teardown: reap check 2 protects only the declared direction —
  reaping the declaring agent is refused, reaping the agent it points at is not.
- **Addressing** is by full container name — the repo-basename shortname is
  ambiguous with N agents per repo. The reply short-circuit (`msg['from']` is a
  real mailbox dir) is unaffected, so a named peer can always reply.
- **Correlation.** There are no threads; a subject-line convention carrying the
  task-slug / deliverable branch lets an agent juggling several peers keep
  exchanges straight.
- **Observability.** The master can read every fleet mailbox it reaches (bind
  mount), so its per-turn sweep covers peer↔peer traffic too — nothing on a
  sanctioned edge is invisible to it. This holds **only via `read_mailbox`'s
  `archive/` + `outbox.log` read** (above); a peer's `inbox/` is drained within
  ~1 s and shows nothing.

This is the **messaging** edge. A second, bookmark-based **dependency** edge —
the VCS blackboard — is deferred to the next step (§14,
`docs/design-blackboard-coordination.md`).

## 8. VCS model

- **Own anchor per agent.** Each task-agent branches off its own anchor in its
  own jj workspace, isolated in the shared origin store. Independent agents
  produce independent stacks.
- **Cross-agent visibility is free.** All workspaces write into the same origin
  `.jj/repo/store`, so any agent can `jj log` and see every other agent's
  commits/branches. Agents coordinate **via mailbox**; they "see each other"
  through the VCS tree only when they choose to look. The master owns
  coordination — it has no built-in role (sequencer/merger); the human assigns
  the role per task.
- **Sequential handoffs anchor only on *finished* work.** `-r` accepts any revset,
  including a sibling agent's deliverable branch, so the master can compose ad-hoc
  pipelines within one repo ("B, anchor on A's `contract` branch"). But it may
  **not** anchor inside a *live* agent's stack, and the launcher enforces that
  rather than trusting the master's ordering (§9).

  The hazard being closed: an anchor is immutable once B's workspace exists, while
  a live A can squash, rebase, or rewrite its deliverable branch at any moment.
  Anchoring B on a still-working A therefore pins B to a base its owner has since
  revised — silently, with no signal to either agent, and unfixable without
  re-anchoring B from scratch. Prior art agrees: the multi-agent-git studies that
  chain agents (CAID on OpenHands, worktree-per-agent) all gate a downstream task
  on its upstream having *landed*, not merely having started.

  **Teardown is the finished signal.** A torn-down agent's deliverable branch
  cannot move again — that is exactly the immutability B needs, and unlike a
  master-written "done" marker it is not forgeable by the party being gated. So a
  handoff serializes as: A finishes → master verifies → A is reaped (deliverable
  branch survives, §5) → B spawns anchored on A's branch.

  **Cost, stated plainly:** a chain of N steps needs N teardowns, so the master
  cannot overlap a link with its successor. It can perform them itself without
  asking anyone (§7), so the cost is sequencing, not human round-trips — but a
  chain whose steps are already known is better expressed as `cld chain`, which is
  what pre-planned pipelines are for. Parallel fan-out — the common case — is
  unaffected: independent agents anchor on the shared base, not on each other.
- **Session vs deliverable bookmark.** The session bookmark is lifecycle
  (forgotten on shutdown). The deliverable branch is the result (master-assigned
  at spawn, default = task-slug; the agent squashes into it; survives teardown).
  These are different strings, so no clash.
- **Self-merge on wrap-up.** When told to finish, the agent squashes/rebases its
  work into the deliverable branch and reports it. The master reconvenes N
  deliverable branches however the human directed (stack, merge, or leave
  parallel) and presents them.
- **Recovery path.** If an agent is torn down before it squashed (premature
  master call, or manual host shutdown), work is **not lost** — Watchman
  snapshots persist in the store; recover via `jj log -r 'heads(all())'`. The
  deliverable branch may just be empty/behind. Documented as accepted behavior.

## 9. Spawning and teardown mechanics

Reuses the sibling-launch seam (`docs/design-master-sibling-launch.md`):

- **From the host:** `cld task-agent start` builds container args
  (`build_container_args(..., task_agent=True)`), stages the anchor, and
  `docker run -d`.
- **From inside a master:** no docker socket; `cld task-agent` resolves the
  cwd's target (own repo or a `master_targets` sibling) and delegates to the
  broker. **Implemented as a separate `task-agent` action** rather than an extension
  of `agent`: the op sets differ, and only this one needs argv policing (`--force`
  denied, `--parent` host-stamped, persona must be a bare name), which is easier to
  audit as its own function. Target validation is shared with `agent`. `transcript`
  is *not* delegated -- the mailbox is bind-mounted, so it needs no host channel.
  This is what lets one master spawn task-agents on its own repo **and** across
  registered siblings (the cross-repo workflow).
- **Verification is local for own-repo, remote-or-self-reported for cross-repo.**
  §5 step 4 has the master verify the deliverable branch before reaping. On the
  master's own repo that is a local `jj log`/`jj diff` — the repo is mounted. On a
  **sibling** target the master has no filesystem view at all (sibling targets are
  empty placeholder directories, no bind mount), and **no broker action is added
  for this** — the broker's action set stays as small as possible. Two paths
  instead, in preference order:
  1. **Pushed branch (verifiable).** On wrap-up the agent pushes its deliverable
     branch to the remote (`jj git push --bookmark <branch>`) and
     reports the branch name plus the MR/branch URL. The master then inspects it
     through the same GitLab surface a human would — no filesystem view of the
     sibling repo needed, and the artifact is durable and reviewable after
     teardown. This is the recommended path for any cross-repo task, and the
     master should ask for it as part of the wrap-up instruction.
  2. **Self-reported (not verifiable).** If the branch was not pushed, the
     master's only evidence is the agent's own report. That is acceptable, but the
     master persona must **say so** and not claim verification. Local-only
     cross-repo deliverables are also invisible after teardown to anyone without
     access to that host's store.

  *Prerequisite for path 1:* agent containers get the SSH agent forwarded but ship
  no `~/.ssh/known_hosts`, so a first push to `git@gitlab.seznam.net` fails with
  `Host key verification failed` (a trust gap, not a reachability one). The
  task-agent boot path should seed it — `ssh-keyscan -t rsa,ecdsa,ed25519 <host>
  >> ~/.ssh/known_hosts` — or wrap-up will fail on the last step of every
  cross-repo task.

- **Teardown** is `docker stop && docker rm` followed by **caller-side** cleanup:
  forget the session bookmark (never the deliverable branch) and archive the
  mailbox (§5 step 5, §6). The in-container SIGTERM handler remains as a
  best-effort fast path but is not load-bearing, because the supervisor is
  normally SIGKILLed mid-turn. Both steps are idempotent.

**Cap enforcement.** A configurable per-master cap (`max_task_agents`, env
`CLD_MAX_TASK_AGENTS`, **default 4**) is checked **host-side at spawn** by
counting **running** containers with `org.cld.parent-master == <this master>`.
Over the cap → refuse with a clear error naming the running agents. (Host-side
because that is where docker truth lives; the in-master path enforces it through
the broker.)

- **Count running only.** The existing enumerator uses `docker ps -a` and includes
  stopped containers; the cap check must filter to running, or spawns get refused
  because of corpses.

**Live-stack anchor refusal.** Checked in the same host-side place as the cap, for
the same reason (fleet liveness and the origin store are both readable there; the
in-master path enforces it through the broker):

1. Resolve `-r` to a commit `X` as usual (`resolve_anchor`).
2. Collect the `anchor` field from `meta.json` for every **running** fleet member
   — each is that agent's `AGENT_ANCHOR_HASH`.
3. Refuse the spawn if `X` is in `<live-anchor>::` (descendants, inclusive) for any
   of them — one revset query, e.g. `jj log -r "<X> & (<a1>:: | <a2>:: | …)"`
   non-empty. Error names the owning agent and says to reap it first.

Anchoring on the **shared base** still works, which is the case that matters most:
a live agent's anchor is a *child* of the base, so the base is an ancestor and not
in the refused set. Two agents spawned from the same `@` both pass; an agent
anchored inside another live agent's work does not. Anchoring on human-merged work
in the main line always passes — nobody live owns it.
- **Why 4 and not 8–10.** Reported sweet spots for parallel coding agents cluster
  low: Anthropic's multi-agent researcher fans out to 3–5 subagents; the CAID
  worktree-per-agent study found 4 optimal (2 for some task classes); practitioners
  running git worktrees per agent report management overhead dominating around
  8–10. A low default also keeps the untested load low: N task-agents plus master,
  all with Watchman auto-snapshot, write into one shared `.jj/repo/store`, which
  multiplies op-log lock contention by `max_task_agents` over today's tested load
  of one agent. Treat N > 1 store contention as **unvalidated** until smoke-tested;
  it is the most likely place this POC surprises us.

## 10. Failure modes and manual control

- **Agent hangs / never replies.** No reaper this POC. The human runs
  `cld task-agent shutdown <name>` from the host, with `--force` if the hung agent
  fails a reap-readiness check (§7); `--all` clears everything.
- **Master dies with a live fleet.** Agents keep idle-polling an unread inbox
  (cheap, but they hold RAM). They are **not** auto-reaped. The human retains
  full host-side control: `cld task-agent status` shows the orphans (including
  "mailbox exists, container dead" cases), and `shutdown [--all]` clears them.
  Because parent linkage is stored (label + `meta.json`), a **relaunched master
  reattaches its fleet** via the mailbox registry — orphaning is recoverable,
  not fatal.
- **Dirty death (SIGKILL/OOM/host reboot).** Session bookmark may survive; same
  best-effort WARN + manual `jj bookmark forget` fallback as today. Deliverable
  branch and snapshots are unaffected.
- **Master tears down a depended-on peer.** The master owns teardown, so it could
  shut down agent B while agent A is mid-exchange with it; A's messages then go
  nowhere and the exactly-one-reply guarantee — enforced by B's now-dead
  supervisor — silently lapses. This is now **mechanically prevented**: shutdown
  refuses to reap an agent listed as a peer by another live fleet member (§7,
  reap-readiness check 2), overridable only with a host-side `--force` — so the
  master cannot do this to itself. For the `--force` and crash cases the transport now
  **refuses** a send to a torn-down agent instead of letting it vanish: its mailbox is
  archived, and delivering would resurrect the directory, shadow the archived
  `meta.json` behind an empty live one and quietly un-budget the edge. The sender gets
  an in-turn error naming the master to tell. A full tombstone/bounce — a receipt the
  *sender* can await — remains future work (§14).
- **Peer ping-pong.** Two agents on an edge can loop without terminating — the
  per-message turn cap bounds one message, not the round-trip count. Resolved by
  **peer-loop control** below.

### Peer-loop control (absolute hop budget)

Enforced by the **transport**, never the persona — the looping party can't police
itself.

**Identity.** A conversation *is* an edge, and an edge is its endpoints — the
`from`/`to` already on every message. No separate conversation label.

**One counter per edge, one limit** (master-set at spawn, per edge, via
`--peer <name>:<hops>`, defaulting to `cfg.peer_absolute_limit`): total delivered
messages over the edge's whole life. Only the master resets it. This is the runaway
ceiling, and for the POC it is the *whole* mechanism.

The limit rides on the edge rather than on the agent because edges are asymmetric
(§7): exactly one side declares a given edge, so exactly one side has an opinion
about its budget. The declaring side's number seeds the counter file and is
authoritative from then on, so the replying side inherits it instead of applying its
own default — see the persistence rule below.

> The **progress budget** (a second counter reset by VCS activity on the edge) is
> **deferred** — see §14. Its appeal is that it frees productive loops, but its
> signal is not edge-scoped: an agent's heads move for reasons unrelated to the
> edge (master-driven work, Watchman snapshotting a scratch file, an edit later
> reverted), so two agents can ping-pong indefinitely while both look productive.
> Making it work needs a narrower progress signal — most likely the deliverable
> branch tip moving — and that is its own design pass. Absolute-only also matches
> what the field actually ships: OpenAI Agents SDK `max_turns` (10), LangGraph
> `recursion_limit` (25), CrewAI `max_iter` (25), AutoGen
> `max_consecutive_auto_reply` (100) are all blunt absolute counters.

**Where the counter lives: the mailbox tree, not the supervisor.** The gate runs in
the messenger — the MCP `send()` tool and its CLI twin (below) — and the messenger
MCP server is a **stdio subprocess of the `claude` CLI**, which is itself a subprocess
of the supervisor — three distinct
processes. The supervisor therefore cannot observe or block a `send()`, and it
cannot produce the in-turn tool-error the gate needs; a "per-peer cache in the
supervisor" is both unreachable from the gate and non-durable (a restart would
silently reset the ceiling that exists precisely to stop runaways). So the state
goes where both endpoints and every restart can see it — the shared, durable
medium already in use:

- `<mailbox-root>/_edges/<a>--<b>.json`, endpoints **sorted** so both sides derive
  the same path: `{"count": n, "limit": m, "updated": ts}`.
- **The stored limit wins; the first send seeds it.** A sender supplies its own view
  of the budget (`meta.json`'s `peers[<peer>]`, else `cfg.peer_absolute_limit`), but
  it only takes effect when the file does not exist yet. Both endpoints therefore
  agree on one ceiling with no negotiation, and the declaring side's number governs.
- Written with the existing tmp-write + `rename()` pattern. Read-modify-write can
  in principle lose a concurrent increment; that is benign (the ceiling becomes
  slightly generous) and sends on an edge alternate by turn anyway, so no lock is
  warranted for the POC.
- The **envelope keeps a `hops` stamp**, but as an *audit/transcript* field only —
  the file is authoritative. (This revises D17: with shared state on disk, the
  envelope is no longer the sync channel.)

**Both send paths must pass the gate.** The messenger `send()` tool is not the only
way into the transport: `python -m cld.messenger.send` writes to a mailbox directly,
and it is what `.claude/skills/messenger-send` — baked into every devcontainer image
— instructs an agent to run. So the accounting lives in a shared transport function
both entry points call, and the silence rule below lives in the single write
primitive that *every* sender, the supervisor included, already goes through.
In-container enforcement is a guardrail against runaway, not a defence against an
adversary: an agent that writes its own Python can reach the filesystem regardless.

**The rule underneath the gate: a spent edge is silent.** Once an edge has delivered
its limit, the transport delivers nothing more over it — not a retry, not a
supervisor-synthesized reply, not a notice about the budget itself. That yields a
one-sentence invariant: **at most `limit` messages ever cross a peer edge, whoever
sends them.**

**Enforcement is a per-send gate:**
1. If the edge has already reached its limit → **refuse**: return an in-turn error
   telling the agent to escalate to the master rather than retry.
2. Otherwise deliver, then count, and stamp the envelope.

The order is load-bearing: the ceiling is checked *before* the delivery and the
counter incremented *after* it, so the limit-th message is the last one that lands
rather than the first one refused.

`send()` already returns a dict and already surfaces failures as `{"error": …}`, so
the refusal needs no new tool contract. On success the return carries
`{"hops": n, "limit": m}` — the agent can see where it stands without being told.

**No cap-notice to the peer, and no hop-exempt message of any kind on a peer edge.**
An earlier draft of this section had a blocked send deliver a hop-exempt notice so
the peer "isn't left waiting". That re-creates the runaway the budget exists to
prevent: a notice is itself a message, every inbound message obliges a reply (§5),
and the reply attempt is blocked — producing another notice, forever, at agent
cadence and one Claude turn per hop. The supervisor's synthesized fallback loops the
same way, because it writes to the mailbox directly and so cannot be stopped by a
gate that lives in `send()`. **Anything both hop-exempt and reply-obliging loops.**
The two properties must never coexist on one edge.

Nothing is stranded by the silence. The peer is idle-polling, not blocked, so a spent
edge simply goes quiet; and the blocked side escalates on the **master channel**,
which stays available because it is a *different edge* — not an exemption carved into
the spent one. That distinction is the whole mechanism. Telling the peer *why* it went
quiet is a reporting need, and reporting belongs to the referee that reads both
mailboxes (§7) — the same move this design already made when it replaced a per-reap
authorization token with "report reaps in turn output".

**The master channel is unbudgeted** for that reason: it is a separate edge, not an
exempt class. The budget applies to agent↔agent edges only; master↔agent traffic
(fan-out, wrap-up instructions, escalations) is never counted.

Consequence to accept: **a graceful landing must fit inside the budget.** There is no
free "resolved, moving on" send after the ceiling, so the limit-th message is the
agent's last word — which is what "converge as you approach the limit" (§11) is for.
An agent that burns its budget without landing leaves its peer with silence that only
the master can explain.

**Persona awareness (soft, layered on the hard gate).** Agents are told the budget
exists, can read their position from every `send()` return, and are instructed to
converge as it nears the limit and to notify the master rather than retry a blocked
send. Awareness improves the *quality* of the landing; the gate *guarantees* it.

### Clarification-regress control (per-edge ask budget)

The hop budget bounds *how much* two agents say; it does not bound the shape of what
they say. With obligation declared by the sender (§5 step 3), two distinct pathologies
remain, and one bound does not cover both:

- **Polite ping-pong** — each message discharges and re-opens, so nothing accumulates.
  Left to the persona (say nothing when nothing was asked) with the absolute hop
  budget as the backstop. Deliberately not mechanized further.
- **Clarification regress** — the agents keep asking each other and never answer the
  question the exchange started from. This one *is* mechanized, because it burns a
  Claude turn per hop and always ends by exhaustion.

**Depth is the wrong metric.** `|open obligations|` misses the common shape: B asks, A
answers, B asks again, A answers again sits at depth 1 forever while the root question
goes unanswered. Count instead:

> **`asks`** — obligation-opening sends on this edge while a **root** obligation (one
> opened when nothing was open) has stayed unanswered. Cleared only when `open`
> empties.

Legitimate clarification reaches 2. A regress grows without bound. Long answers, plain
informs and re-opened *new* roots don't touch it. The limit is `cfg.root_ask_limit`
(default **3**, env `CLD_ROOT_ASK_LIMIT`), global rather than per-edge — one number is
enough until a real fleet says otherwise.

**The gate refuses the ask, never the speech.** This is the whole difference from the
hop budget, and it is why the ask gate needs no D29 argument: past the limit an
`expects_reply` send is refused, while `answers`-bearing sends and plain informs still
deliver, and the master channel is untouched. Nothing becomes both hop-exempt and
reply-obliging, and the graceful landing is available *by construction* rather than
having to fit inside a ceiling. The refusal names the two legal moves, in order:

1. **Commit with a stated assumption** — answer the open question with your best
   interpretation and say what you assumed. The right default; blocking questions are
   for cases where proceeding under any assumption would be unsafe.
2. **Escalate to the master**, then answer once it rules.

**Escalation is the correct routing, not a fallback.** A regress between two peers is
nearly always evidence that the *master* under-specified one of the two tasks: neither
peer can resolve it because the missing information is in neither context. Which makes
`asks` a diagnostic about the master's own fan-out, so it is surfaced and not merely
bounded — `fleet_digest` carries `open_asks` / `open_with` / `oldest_open`, and a
rising count against a static `oldest_open` is a stall the master should rule on before
the gate fires. That also closes a gap §10 otherwise leaves: a peer exchange that
silently stops is invisible to the one party who can explain it.

**State lives beside the hop counter**, in `_edges/<sorted>.json` (`open`, `asks`,
`root_since`), for the same reasons: the gate runs in the MCP server — a grandchild of
the supervisor — must cover both send paths, and must survive a restart. Same
tmp-write + `rename()`, same no-lock decision; a lost increment makes the ceiling
slightly generous, which is the benign direction.

Not solved, and accepted: an agent that stamps `answers` dishonestly resets its own
counter. Same principle as the hop gate — a guardrail against runaway, not a defence
against an adversary — and a bogus discharge is visible to the master anyway, as a
root that "closed" with no content behind it.

## 11. Persona and prompt composition

The kickoff prompt is layered so task-scoping is uniform regardless of the
chosen role:

```
[task-agent lifecycle preamble]   ← baked (prompts/personas/task-agent.md)
[chosen persona @<persona>]        ← the role (architect / implementer / …)
[task from -p and/or task file]    ← the concrete work
```

The **lifecycle preamble** is a new base persona that frames the task-scoped
contract: you are bounded to this task; the master drives you and will tell you
when to wrap up; on wrap-up squash your work into the deliverable branch
`<branch>` and report it; a reply is a `send()` carrying `answers=<that message's
id>`, and it must go to the sender of the message you are answering (a send to a
peer does not discharge a reply owed to the master); **a message that did not ask
for a reply gets none — no acknowledgments**; set `expects_reply` only for a
question you cannot proceed without, and prefer stating an assumption over asking;
you may message only the master and the peers named here, no one
else; peer edges carry an absolute hop budget (§10) — every `send()` return tells
you your position, so converge as it nears the limit, and if a send is blocked
notify the master instead of retrying; a peer edge also bounds how many questions
may be open on it at once, and a refused ask means answer-with-an-assumption or
escalate, never re-ask; the master channel is never budgeted; you
have persistent memory across messages; the anchor invariant holds (only
descendants of `AGENT_ANCHOR_HASH` are yours). It replaces `agent.md`'s "immortal,
for as long as this container runs" framing with "bounded, until the task is
done."

The master imposes any **response-format restrictions per agent here** (via `-p`
/ the chosen persona) — there are no global message-format rules, by design.

## 12. Implementation touchpoints (for the implementer)

Conceptual, not code. Keep the supervisor single-copy; branch only the identity,
launch, and cleanup deltas so `cld agent` can later be deleted cleanly.

- `cld/cli.py` — new `task-agent` Typer command group, **verb-first**
  (`start` / `status [<name>]` / `logs <name>` / `transcript <name>` /
  `shutdown [<name>|--all] [--force]`). Reuses
  `_run_persistent_devcontainer` logic but **without** repo-deterministic
  start-or-attach — every `start` creates a new container. Add a bare-slug →
  full-name resolver scoped to the cwd's repo (error, listing candidates, on
  ambiguity). The three reap-readiness checks live here (§7) — phase, live-peer,
  own-fleet, all cheap reads; there is deliberately **no** store-side "did the
  squash happen" test, since verification is §9's job and already done by then.
  `--force` must stay host-only, i.e. never reachable through the broker.
- `cld/docker.py` — `task_agent_container_name(repo, slug, suffix)`;
  `build_container_args(..., task_agent=True)` sets `org.cld.kind=task-agent`,
  `org.cld.parent-master`, `org.cld.task`; a `task-agent` enumerator by label;
  cap check helper (**running containers only** — the existing enumerator uses
  `docker ps -a`); live-stack anchor refusal at spawn (§9) — one revset query
  against running members' `meta.json` anchors, next to the cap check.
- `cld/messenger/agent_loop.py` — parametrize for **task mode**: composed kickoff
  prompt, `meta.json` write at boot (immutable spawn facts, no `status` field),
  deliverable-branch awareness. Same state machine, no new phase. Fix the reply
  guarantee to be **recipient-scoped** (below). Archive-on-shutdown does *not*
  live here — see the caller-side note in §5 step 5.
- Entrypoint (`entrypoint-claude-devcontainer.sh`) — a task-agent branch that
  creates the deliverable bookmark at the anchor and execs the supervisor in
  task mode. Session-bookmark create/forget unchanged.
- `cld/broker.py` — route `task-agent` spawn/enumerate/shutdown through the
  broker when `in_master_container()`; extend `list-containers` parsing to carry
  `kind`/`parent`/`task`. No new broker seam.
- `broker/cld-broker.sh` — extend the `agent` action (or add `task-agent`)
  to launch/manage host-side task-agents, validated against master labels; the
  shutdown path runs the reap-readiness checks (§7) and the caller-side cleanup
  (bookmark forget + mailbox archive), and **must not accept `--force`**; the launch
  path enforces the cap + live-stack anchor refusal (§9). **No new read action** —
  cross-repo verification goes through the pushed branch, not the broker (§9).
- `cld/messenger/mailbox.py` —
  - `meta.json` read/write; `_archive/<name>/` on teardown; registry helpers
    filtered by `parent`.
  - **Recipient-scoped reply check** replacing `outbox_changed_since`: scan
    `outbox.log` past the snapshot for a line whose `to` matches the message's
    `from`. The `to` field is already written; no schema change (§5 step 3).
  - **`outbox.log` lines gain `subject` and `body`** so a transcript is complete
    from one mailbox with no cross-mailbox reads (§ transcript below).
  - Addressing already works for unique full names; the repo-basename shortname in
    `resolve_recipient` is unusable with N agents/repo — mailbox addressing is by
    **full name** (the reply short-circuit via `msg['from']` is unaffected). The
    CLI's bare-slug shortcut lives in `cli.py`, not here.
- `cld/mcp/messenger.py` — two additive read-only fleet tools, both restricted to
  mailboxes whose `meta.json` has `parent == self`: `fleet_digest()` and
  `read_mailbox(name, since=…)` (§7). `list_inbox` keeps its
  own-mailbox semantics. Update `list_agents`' docstring: `kind` now includes
  `task-agent`.
- `transcript` — a timestamp-ordered join of what the agent **received**
  (`inbox/` + `archive/`, full bodies) and what it **sent** (`outbox.log`). Must
  read both the live mailbox and `_archive/<name>/`, since teardown moves the
  whole dir. This is why the outbox line gains subject+body: otherwise the sent
  side is `{id, to, ts}` only and its bodies would have to be recovered from each
  recipient's archive, which may be GC'd or archived independently.
- Obligation fields (§5 step 3) — `write_message` gains `answers` / `expects_reply` on
  the envelope *and* the outbox line (so a transcript shows both sides of an
  obligation); `bump_edge` maintains the ledger; `process_one`'s fallback is gated on
  the incoming message's `expects_reply` and stamps `answers`; the inbound prompt states
  which case it is, since the model cannot see the envelope.
- Ask gate (§10) — `ask_spent` checked in `gated_send` **only** (unlike the closure rule,
  which has to sit in `write_message`): the supervisor's fallback never asks, so no
  sender that bypasses `gated_send` can reach the gate's subject. `edge_obligations`
  feeds `fleet_digest`.
- Peer-loop control (§10) — the messenger `send()` gate: one counter per edge vs
  that edge's limit (`meta.json`'s `peers[<peer>]`, else `peer_absolute_limit`;
  whatever the first send stored wins), persisted at
  `<mailbox-root>/_edges/<sorted-endpoints>.json` (**not** cached in the
  supervisor — the gate runs in the MCP server, a grandchild process, and
  in-memory state would not survive a restart). The check goes **before** the
  delivery and the increment **after** it, and a spent edge delivers nothing at all —
  including the supervisor's fallback reply, so the rule belongs in the shared write
  primitive rather than in `send()` alone (§10). No cap-notice, no exempt class on a
  peer edge. Envelope gains a `hops` audit stamp. `send()`'s success return carries
  `{"hops", "limit"}`. The master↔agent channel is unbudgeted because it is a
  different edge. Both send paths — the MCP tool and `python -m cld.messenger.send`,
  which a baked-in skill documents — must pass the gate.
- `cld/config.py` — `max_task_agents: int = 4` + `CLD_MAX_TASK_AGENTS`;
  `peer_absolute_limit: int = 10` + `CLD_PEER_ABSOLUTE_LIMIT` (the fallback when a
  `--peer` spec omits its own `:<hops>`); `root_ask_limit: int = 3` +
  `CLD_ROOT_ASK_LIMIT` (§10's ask budget). Both budget knobs are propagated into the
  container by `build_container_args`, since an in-container `Config.from_env()` sees
  no host user TOML.
- `prompts/personas/task-agent.md` — the lifecycle preamble (§11).
- Master persona / instructions — the control-tower behavior: each turn call
  `fleet_digest()` and `read_mailbox` only for members that moved (**not** a sweep
  of inboxes — they are drained within ~1 s); **draw the agent graph** (pass each
  agent its edges at spawn via repeatable `--peer <name>[:<hops>]`, remembering that
  only an already-spawned agent can be named, so the edge is declared by the
  later-spawned side — §7); own coordination; ask for a
  **pushed** deliverable branch on wrap-up for cross-repo tasks, and never claim to
  have verified a self-reported one (§9); summarize a finished task to the human and
  report reaps in its turn output so the human keeps a picture of the fleet without
  a blocking gate (§7). Peer addressing uses full container names;
  `resolve_recipient` already handles them, so brokered edges need no transport
  change.

  Two former persona invariants are now mechanical and need no prompt wording: not
  reaping a live peer or an agent whose work isn't landed (§7), and not anchoring on
  unfinished work (§8). The persona still needs to *understand* both — a handoff
  means reap-then-spawn, and a reap refusal means "wrap-up didn't finish", not "try
  again" — so it plans around the refusals instead of hitting them.

## 13. Decisions log

| # | Decision | Rationale |
|---|---|---|
| D1 | Standalone `cld task-agent`, coexists with `cld agent` | Prove the model before deleting the old path; clean later removal |
| D2 | Master reaps its own fleet freely; teardown is bounded by three **reap-readiness checks** (not mid-turn, not a live peer, own fleet), and `--force` is host-only | Teardown destroys only the agent's spent session context — work, branch, transcript and cost all survive. Gate *state*, not permission (§7) |
| D2b | Work-captured is **not** a teardown check; it is §9's verification, already performed at §5 step 4 | One mechanism per question: local `jj` for the master's own repo, pushed branch/MR or self-report for a different repo. A premature reap falls back to §8's recovery path, which the design already accepts (§7, §9) |
| D2a | **Rejected:** a per-reap human authorization token | Gates the only fleet op that frees resources (blocked masters leave agents idling against the cap) while leaving spawning — the op that actually spends — open; would be pre-authorized away after the first stall. The interrupt patterns it borrowed from gate *external* effects, not internal cleanup (§7) |
| D3 | Interleaved control tower, manually cranked (no auto-notice) | POC simplicity; notice is future work (§7) |
| D4 | Name `a.<ticket>.<task-slug>` (was `cld_agent_<repo>_<task-slug>`), master-generated slug, suffix on collision | Human-readable, task-scoped, unique |
| D5 | `org.cld.kind=task-agent` label is the discriminator, not the name prefix | Prefix is shared with repo-agent; label is unambiguous |
| D6 | Mailbox tree = master roster; docker labels = host truth | Master needs a socket-free roster; host needs liveness |
| D7 | Archive mailbox on shutdown (not delete) | Preserve the conversation transcript; keep registry honest |
| D8 | Deliverable branch master-assigned at spawn, default = slug, survives teardown | Reconvene point; distinct from lifecycle bookmark |
| D9 | `-r` may anchor on a **finished** sibling's deliverable branch; the launcher **refuses** an anchor inside a live agent's stack | Enables master-composed sequential handoffs while closing the "pinned to a base its owner later rewrote" hazard. Teardown is the finished signal because it is the only one the gated party can't forge (§8, §9) |
| D10 | Configurable per-master cap, default **4**, counting **running** containers, enforced host-side at spawn | Bound resource use; master is the ownership unit. 4 matches reported sweet spots (Anthropic 3–5 subagents, CAID 4) and keeps untested shared-store contention low (§9) |
| D11 | No reaper; manual host `shutdown`/`--all`; relaunched master reattaches fleet | Accepted POC limitation with a real escape hatch |
| D12 | Single supervisor, parametrized for task mode | One code path; `cld agent` stays deletable |
| D13 | Agent↔agent messaging is in the base POC, over the mailbox, along master-drawn edges | Peer handoffs at agent cadence, off the manually-cranked master's hot path (§7) |
| D14 | Master draws the graph: no cold-messaging; agents never spawn | Contain mesh failure modes; keep the master in control; spawning stays master-only for security |
| D15 | VCS blackboard (bookmark dependency edge) deferred to the next step | Keep the base POC to one coordination channel (§14) |
| D16 | Peer-loop control via per-edge hop budget, **transport**-enforced (not persona, not supervisor) | The looping party can't police itself. The gate sits in the transport, below **both** send paths (the MCP `send()` tool and `python -m cld.messenger.send`), which the supervisor can neither reach nor bypass (§10) |
| D17 | Conversation ≡ edge; identity is `from`/`to`; counters live in `_edges/<sorted>.json`, envelope carries a `hops` **audit stamp** only | Endpoints already identify the edge; shared on-disk state is the sync channel and survives restart, so the envelope need not be (§10) |
| D18 | **POC ships the absolute budget only**; the progress budget is deferred | Progress is not edge-scoped (heads move for unrelated reasons), so it needs its own design pass; absolute-only matches what every comparable framework ships (§10, §14) |
| D19 | Blocked send → escalate to master; no self-unblock path in the POC | Follows from D18 — self-unblock was a property of the progress budget. Revisit with it (§14) |
| D20 | The master↔agent channel is unbudgeted; there are **no** hop-exempt messages on a peer edge | Escalation must stay possible from a spent edge — and it is, because it travels a *different* edge. An exemption *on* the spent edge would loop instead (D29) |
| D29 | **A spent edge is silent**: the transport delivers nothing more over it, supervisor-synthesized replies included, and no cap-notice is sent | One rule with a one-sentence invariant (≤ `limit` messages per edge, whoever sends them) replaces an exempt class plus ad-hoc brakes. Exempting the messages that *announce the end* is what loops: hop-exempt + reply-obliging = runaway. Cost: the graceful landing must fit inside the budget (§5, §10) |
| D21 | Reply guarantee is **recipient-scoped**, not outbox-growth | With peer edges, a send to a peer would otherwise satisfy a reply owed to the master, suppressing the fallback (§5) |
| D30 | Reply obligation is **declared by the sender** (`expects_reply`) and discharged by `answers`; arrival obliges nothing, and the master channel gets no hardcoded exception. A reply may itself ask | "Every message gets a reply" makes an acknowledgment oblige an acknowledgment — observed looping two agents until the hop budget ran out. Every comparable protocol separates request from notification (FIPA-ACL, JSON-RPC, A2A, CAMEL, AutoGen). Letting a reply ask is what makes a clarification sub-dialogue work, since the root obligation must survive it (§5) |
| D31 | **Per-edge ask budget** (`root_ask_limit`, default 3) bounds the clarification regress, counting obligation-opening sends while a root stays unanswered — and refuses **the ask, not the edge** | Depth misses discharge-and-reopen; the hop budget only catches it after exhaustion. Refusing asks while answers still deliver keeps the graceful landing available by construction, so unlike D29 no exemption argument is needed. Surfaced in `fleet_digest` because a regress usually means the *master* under-specified a task (§10) |
| D22 | Teardown cleanup (bookmark forget + mailbox archive) is **caller-side** | SIGTERM is only checked between messages and `docker stop` grants 10 s, so a supervisor "last act" is normally SIGKILLed away (§5, §9) |
| D23 | Fleet observability via `fleet_digest()` + `read_mailbox()`, reading `archive/`+`outbox.log` | Inboxes drain within ~1 s and `list_inbox` cannot read another mailbox; the digest also keeps the per-turn crank from flooding master's context (§7) |
| D24 | `meta.json` = immutable spawn facts (incl. `peers` as a name → hop-budget mapping); liveness stays in `state.json` | Two files with a status field drift, and the stale one is the one master reads (§6) |
| D28 | The hop budget is declared **per edge** (`--peer <name>[:<hops>]`, repeatable) rather than per agent (`--peers` + `--peer-hops`) | Edges are asymmetric — only the later-spawned side can name a peer, so exactly one side has an opinion about that edge's budget. Per-edge removes the competition between a declarer's number and a replier's default (§7, §10) |
| D25 | Cross-repo deliverables are verified via the **pushed branch/MR**, else explicitly self-reported — **no new broker action** | Master has no filesystem view of sibling repos; a pushed branch is inspectable the way a human would inspect it, outlives teardown, and keeps the broker's action set minimal (§9) |
| D26 | Verb-first CLI grammar; `<name>` accepts a bare slug resolved in the cwd's repo | Consistent with `cld agent`/`cld master`, natural in Typer, and keeps humans from typing full container names (§4) |
| D27 | `outbox.log` lines carry `subject`+`body` | Makes `transcript` a single-mailbox read instead of a cross-mailbox reconstruction against possibly-archived peers (§12) |

## 14. Future work

- **The VCS blackboard (second coordination edge)** — see
  `docs/design-blackboard-coordination.md`. Adds a bookmark-based *dependency*
  edge (watched `ready/<task>` signals + a supervisor watcher) so pure "X is
  ready" handoffs need neither a message nor a live recipient. The next increment.
- **Progress budget for peer edges (the deferred half of §10).** A second
  per-edge counter, reset by *edge-scoped* progress, so productive loops aren't
  penalized by the absolute ceiling. The open design question is the signal: "any
  VCS change by either participant" is too broad (heads move for master-driven
  work, and Watchman snapshots any touched file), so the candidate is the
  **deliverable branch tip moving**. Brings back self-unblock (D19) and the
  soft-block tool-error.
- **Per-agent spend ceiling.** Today cost is reported (`state.json`'s
  `cost_usd_total`, `cld task-agent status`) but only *concurrency* is capped.
  Fan-out multiplies tokens hard — Anthropic measured ~15× a single chat for their
  multi-agent researcher — and here each of N agents resumes a full session per
  message at `--max-turns 30`. The supervisor already tracks the running total, so
  the natural shape is a per-agent USD limit that **escalates to the master instead
  of continuing** when hit. Deliberately out of the POC; revisit once real fleet
  runs give us a baseline to set the number from.
- Turn-injected inbox notice (removes the manual crank; enables true
  event-driven interleaving).
- Automatic reaper reconciling orphaned agents/bookmarks against `docker ps`.
- **Transport tombstone / bounce.** The first half shipped: a send to a *reaped* peer
  is refused in-turn rather than landing in an unread directory (§10). What is left is
  the harder case — a recipient whose container died without a teardown, so its mailbox
  still looks live — which needs a liveness signal the transport does not have.
- Retiring `cld agent` once task-agents subsume the standing-teammate use case.
- Mailbox `_archive/` GC policy (interacts with `transcript`, which reads
  `_archive/<name>/`).
