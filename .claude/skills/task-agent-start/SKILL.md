---
name: task-agent-start
description: >
  Spawn a task-scoped agent (`cld task-agent start`) from a ticket container: pick a
  slug and its prompt refs, write the task, and draw its peer edges. Invoke when the
  user wants to hand a bounded piece of work to a fresh agent, fan work out across
  repos, or add an agent to a running fleet.
user-invocable: true
---

# Task-agent: spawn one

A task-agent is bounded to **one task**, driven by you, and torn down when its work
has landed. It is the only standing headless role left: the immortal one-per-repo
`cld agent` is gone.

## Prerequisite: inside a ticket container, with a host channel

```bash
cld broker --help >/dev/null 2>&1 && [ -d /var/cld/mailboxes ] && echo ticket-ok
```

Spawning and reaping need the host broker (there is no docker socket in-container).
*Reading* the fleet does not — `fleet_digest()`, `read_mailbox()` and
`cld task-agent transcript` all work off the mounted mailbox tree, so if the check
fails you can still observe and message agents, just not create them.

## Step 1: Resolve the target repo

```bash
cld repos
```

The first lines are the repos mounted in this ticket. A second block, "Also registered
on the host (not mounted here)", lists every other repo the operator registered. Either
kind of name is a valid `--repo <name>` in Step 6; there is no `cd`, the current
directory does not matter.

- **A mounted repo**: you can read its code and verify the agent's work locally.
- **A registered-only repo**: for checking or changing something in a repo this ticket
  did not anticipate. You have no filesystem view of it, which is what makes the
  *pushed branch* rule in `task-agent-wrapup` matter.

`--repo` can be omitted only when this ticket mounts exactly one repo and that repo is
the target. A name that is neither mounted nor registered is refused; the operator adds
it on the host with `cld repos add <name> <path>`.

## Step 2: Choose the slug, the prompt refs and the branch

- **Slug** (`-n`): short, kebab-case, describes the task — `add-oauth`, not `task-1`.
  It becomes the agent's identifier `a.<ticket>.<slug>` (`<ticket>` is yours, `_host`
  when started from the host): container name, mailbox and session bookmark. Any
  command takes the bare slug as its handle. Prefer a fresh slug over re-using one; a
  live collision gets a `-2` suffix and stops being self-explanatory.
- **Prompt refs**: an ordered list of `@refs` — the role persona first
  (`@personas/implementer`), then any prepared task prompt (`@some-task`). Personas and
  task files are interchangeable blocks; they are appended in the order you pass them.
  From inside the container only `@refs` cross to the host; a path in *your* filesystem is
  read here and folded into `-p` for you.
- **`--branch`**: only when the deliverable bookmark should differ from the default
  `<agent id>.mr`. A value without a dot is appended to the agent id
  (`--branch mr-fix` gives `<agent id>.mr-fix`); a value with a dot is used as is. The
  start is refused when that bookmark already exists outside the new agent's tree.

## Step 3: Write the task

The task is the agent's whole brief; it will not see this conversation. State the goal,
the constraints, what "done" looks like, and any response-format requirement you want
back. Pass it with `-p`, or point at a `@<prompt-name>` from the target repo's
`prompts/`. Keep it to a few KB — it travels to the host inside the broker's argv.

## Step 4: Draw the peer edges

`--peer <full-container-name>[:<hops>]`, repeatable. An agent may message **only** you
and the peers you name here.

Two rules that follow from how edges work:

- **Only an already-spawned agent can be named**, so the *later*-spawned side declares
  the edge. Spawn A, then spawn B with `--peer <A>`; B can then message A, and A may
  reply (a reply is always allowed).
- **The declared budget governs the whole edge** — total messages, both directions, for
  its life. `:<hops>` sets it; omitted, it is the configured default. When it is spent
  the edge goes **silent** in both directions and the blocked agent escalates to you.
  So size it for the exchange you expect, and expect to be the one who breaks a tie.

Peers are addressed by full container name; a repo basename is ambiguous with several
task-agents per repo.

## Step 5: Decide on a model

- `haiku`: For a quick, straightforward, small and well-scoped tasks. Not very useful for task-agents.
- `sonnet`: Good for well-scoped task-agents: implementation, advice on another repo, first-pass reviews. Use often, with well-prepared prompts.
- `opus`: Good for hard thinking - research, making important decisions, working open tasks, open-ended designing. Use when needed.

## Step 6: Spawn

```bash
cld task-agent start --repo <name> [@refs...] -n <slug> -p "<task>" \
    [--branch <name>] [-m <model>] [-r <revision>] [--peer <name>[:<hops>]]…
```

Two refusals to plan around rather than hit:

- **The cap** (`max_task_agents`, default 4 running per ticket container). Reap something
  finished first — see `task-agent-wrapup`.
- **The anchor.** `-r` may point at a *finished* sibling's deliverable branch, but not
  inside a **live** agent's stack, because that agent can still rewrite it. A sequential
  handoff is therefore **reap-then-spawn**, not spawn-on-top.

The command stages the anchor, starts the container detached and waits for readiness —
up to 60 s, longer if it has to build the image first. That is normal, not a hang.

## Step 7: Report back

Give the user the container name, the task slug, the deliverable branch, the peers with
their budgets, and how to watch it (`cld task-agent status <slug>`, then the
`task-agent-fleet` skill for the ongoing loop).
