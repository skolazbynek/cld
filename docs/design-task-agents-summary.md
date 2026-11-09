# Task-scoped agents (`cld task-agent`) — summary

> Summarized from `docs/design-task-agents.md`. End state and workflow only.

## What it is

A **task-agent** is a headless peer container scoped to one task, spawned by a
master and reaped when the task is done — a fusion of `cld run` (task+persona
launch, branch deliverable) and `cld agent` (persistent, resumable,
conversational session).

| Property | Value |
|---|---|
| Scope | many per repo, one per task |
| Lifespan | bounded — lives until its task is done |
| Persona / task | chosen by the master at spawn |
| Lifecycle owner | the master (with a manual host fallback) |
| Identity | `a.<ticket>.<task-slug>` |

A master runs a **control tower**: it spawns several task-agents (on its own
repo and/or registered sibling targets), drives each by messenger, and reaps
each when its task lands.

## Command surface

```bash
# Spawn
cld task-agent start @<persona> -p "<task>" [taskfile.md|@<name>] \
    [--branch <name>] [-m <model>] [-r <revision>] \
    [--peer <name>[:<hops>]]…

# Roster + inspection
cld task-agent status [<name>]        # roster, or one agent: phase, cost, branch, anchor, peers
cld task-agent logs <name> [-n N]     # supervisor stderr (state + cost)
cld task-agent transcript <name>      # the actual mailbox conversation

# Teardown
cld task-agent shutdown <name>                 # stop + rm; forget session bookmark; archive mailbox
cld task-agent shutdown --all                  # every task-agent (host-wide)
cld task-agent shutdown <name> --force         # host-only: override a reap-readiness refusal
```

`<name>` accepts a bare, master-generated task slug (resolved against the
cwd's repo) or the full container name. Mailbox addressing always uses the
full container name.

## Lifecycle

```
spawn ─▶ KICKOFF ─▶ IDLE ⇄ PROCESSING ─▶ … ─▶ (master: done) ─▶ teardown
```

1. **Spawn.** Master runs `cld task-agent start`. The anchor is resolved and
   staged peer-side; the peer entrypoint sets the session bookmark and
   creates a **deliverable bookmark** at the anchor. Labels are stamped. The
   container runs detached, long-lived while working.
2. **Kickoff.** The supervisor boots one Claude session with a composed
   kickoff prompt (lifecycle preamble + chosen persona + task) and records
   the session id.
3. **Converse.** Each inbound message resumes that session, one at a time,
   strict FIFO; a reply always goes to the sender of the message being
   answered. Whether a reply is owed at all is **declared by the sender** —
   messages carry `expects_reply` and `answers`, and arrival alone obliges
   nothing, including on the master channel. Work is auto-committed via
   Watchman/jj, so nothing is lost before teardown.
4. **Done.** The master decides the task is finished and asks the agent to
   wrap up: squash/rebase work into the deliverable branch, optionally push
   it to the remote, and report it. The master verifies (local `jj
   log`/`jj diff` for its own repo, the pushed branch/MR for a sibling repo,
   or accepts a self-report) then shuts the agent down.
5. **Teardown.** `docker stop` → `docker rm` → the caller forgets the
   session bookmark (never the deliverable branch) and archives the agent's
   mailbox to `~/.cld/mailboxes/_archive/<name>/`.

## The control tower workflow

The master fans work out to several agents and routes replies as they
arrive — it never blocks on a single agent. Each turn, before yielding to
the human, the master reconciles its fleet:

1. Call `fleet_digest()` — one cheap row per fleet member
   (`{name, task, phase, msg_count, cost_usd_total, unread, last_activity}`).
2. Compare against last turn's digest; call `read_mailbox(name)` only for
   members that moved, to pull the full exchange (`inbox/` + `archive/` +
   `outbox.log`).
3. Route replies, give new instructions, or decide a task is done.

Deciding "done", verifying a deliverable and reaping the agent are all free —
the master does them on its own judgment, with no human confirmation step.
Teardown only refuses when the target isn't ready: mid-turn (`phase ==
processing`), listed as a live peer of another fleet member, or not part of
this master's fleet. Overriding a refusal needs `--force`, which is host-only
— so the master can reap, but cannot reap past a refusal.

Serial babysitting (drive one agent to done before starting the next)
requires no separate mode — it's just spawning one agent at a time.

### Agent graph (agent↔agent messaging)

Agents can talk to each other over the mailbox, but only along edges the
master draws at spawn (repeatable `--peer`, named by full container name). An
agent never cold-messages a peer it wasn't introduced to, and never spawns
another agent — spawning stays master-only. Peer↔peer handoffs move at agent
polling cadence (~1s) instead of waiting on the manually-cranked master.

Edges are asymmetric: only an already-spawned agent can be named, so the
later-spawned side declares the edge and the earlier one participates by
replying. Teardown's live-peer refusal therefore protects only the declared
direction.

Each peer edge carries an absolute hop budget (`--peer <name>:<hops>`,
defaulting to config): total messages allowed over the edge's life, enforced in
the transport beneath every send path. Hitting the limit refuses the send and
tells the agent to escalate to the master instead.

The rule beneath the budget is that **a spent edge is silent** — nothing more is
delivered over it, not even the supervisor's synthesized reply, so at most
`limit` messages ever cross an edge. There is no notice to the peer and no
hop-exempt message on a peer edge: a notice would itself oblige a reply, and an
exempt reply-obliging message is exactly a runaway. The master↔agent channel
stays available because it is a *different* edge, not an exemption on the spent
one; a peer that goes quiet learns why from the master, which reads both
mailboxes.

A second, narrower budget on the same edge bounds the **clarification regress** —
two agents that keep asking each other and never answer the question they started
from. It counts questions left open (not conversation depth, which
discharge-and-reopen defeats) and clears when the root question is answered. Past
the limit it refuses **the ask, not the edge**: answers and plain updates still
deliver, so an exchange can always be landed, and the two ways out are to commit
with a stated assumption or to ask the master to rule. `fleet_digest` reports
`open_asks` / `open_with` / `oldest_open`, because a regress usually means the
master under-specified one of the two tasks and only the master can fix that.

## VCS model

- Each task-agent branches off its own anchor in its own jj workspace; all
  workspaces share the origin store, so any agent can see every other
  agent's commits.
- A **deliverable branch** is a separate, durable bookmark the master
  assigns at spawn (default = task slug). The agent squashes its result
  into it on wrap-up, and it survives teardown — this is the reconvene
  point.
- Sequential handoffs are supported: `-r` may anchor a new agent on a
  **finished** sibling's deliverable branch, but the launcher refuses to
  anchor inside a still-live agent's stack. A finished sibling means it has
  been torn down (its deliverable branch can no longer move).
- If an agent is torn down before it squashed, work isn't lost — Watchman
  snapshots remain in the store, recoverable via `jj log -r 'heads(all())'`.

## Cross-repo verification

From inside a master, `cld task-agent` delegates to the host broker to
launch/manage agents on the master's own repo or a registered sibling
target. For a sibling repo the master has no filesystem view, so
cross-repo deliverables are verified by having the agent push its
deliverable branch and report the branch/MR URL (inspected through GitLab
like a human would); if not pushed, the result is self-reported only, and
the master states that plainly rather than claiming verification.

## Caps and safety

- A configurable per-master cap on concurrently running task-agents
  (default 4), enforced host-side at spawn by counting running containers
  labeled with that master as parent.
- Live-stack anchor refusal at spawn: a new agent cannot anchor on a commit
  that's a descendant of another *live* agent's anchor.
- No automatic reaper. Orphaned agents (master died, or hung/unresponsive)
  are cleaned up manually via `cld task-agent shutdown [--all]` from the
  host. A relaunched master reattaches its fleet automatically via the
  mailbox registry.

## Out of scope for this iteration

No VCS blackboard (bookmark-based dependency edge — separate design), no
turn-injected inbox notification (control tower stays manually cranked), no
automatic reaper, no per-agent spend ceiling, and `cld agent` is not
removed yet.
