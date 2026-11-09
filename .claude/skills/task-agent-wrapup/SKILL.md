---
name: task-agent-wrapup
description: >
  Land a task-agent's work and tear it down: instruct wrap-up, verify the deliverable
  branch by the standard that actually applies, then reap and report. Invoke when an
  agent's task looks finished, when the user asks to close one out or free a slot, or
  when handing one agent's result to the next.
user-invocable: true
---

# Task-agent: wrap up and reap

An agent does not decide it is done — you do. Wrap-up is an ordinary message it
processes like any other; there is no special phase.

## Step 1: Instruct wrap-up

`send()` the agent an explicit instruction: squash or rebase its work into its
deliverable branch (`<agent id>.mr` unless `--branch` said otherwise;
`cld task-agent status <slug>` shows the name), then report what landed.

**If the agent worked in a repo not mounted in this ticket, ask it to push:**

```
jj git push --bookmark <branch>
```

You have no filesystem view of a repo that is only registered on the host, so a pushed
branch is the only artifact
you can actually inspect — and the only one that outlives the container for anyone who
does not have access to that host's jj store.

## Step 2: Verify — by the standard that applies

| Where the agent worked | Evidence |
|---|---|
| **A repo mounted in this ticket** | Local: `jj -R <workspace path> log -r <branch>`, `jj diff -r <branch>` (paths from `cld repos`). Direct and free. |
| **A registered-only repo, branch pushed** | The branch/MR through the same GitLab surface a human would use. |
| **A registered-only repo, not pushed** | The agent's own word. **This is not verification.** |

In the third case, say so to the user in exactly those terms. Do not write "verified" or
"confirmed" about a self-report — you checked nothing, and the work is invisible to
anyone without access to that host's store.

## Step 3: Reap

```bash
cld task-agent shutdown <slug>
```

This stops and removes the container, forgets the session bookmark, and archives the
mailbox. What survives: the **deliverable branch** (a durable bookmark), the
**conversation** (`cld task-agent transcript <slug>` reads the archive), and the cost in
its `state.json`. What is destroyed: the agent's Claude session context — which is spent
by definition once its task has landed.

**Read a refusal as information, not an obstacle.** Shutdown checks three things, all
cheap reads:

- **Mid-turn** — it is still processing a message. Wait for the reply.
- **A live peer depends on it** — another running agent named it as a peer, so reaping it
  would break that agent's reply guarantee. Land or reap the dependent first; the
  refusal names it.
- **Not your fleet** — you can only reap agents you spawned.

A refusal means **wrap-up did not finish**. Fix that, don't route around it: `--force` is
host-only and the broker refuses it, deliberately — the two things it overrides both have
a victim (uncaptured work, a third agent's exchange).

## Step 4: Handoffs are reap-then-spawn

To build on a finished agent's work, reap it **first**, then spawn the next one with
`-r <its deliverable branch>`. The launcher refuses an anchor inside a *live* agent's
stack, because a live agent can still rewrite it — teardown is what makes a branch safe
to build on.

## Step 5: Report

Tell the user: what landed, on which branch, how it was verified (or that it was not),
the push URL if there is one, anything the agent deliberately left out, the total cost,
and that the agent was reaped. This is the user's only record — the container is gone.
