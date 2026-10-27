---
name: broker-run-chain
description: >
  Start, watch and cancel a headless `cld run` or `cld chain` from a ticket
  container (`cld run start|status|wait|cancel`, `cld chain ...`). The host
  broker launches it on top of a change in your tree; its work lands as a
  bookmark in the shared jj store. Invoke when the user wants a bounded,
  non-interactive job run on top of your own work: a one-shot run, or a
  named multi-step chain.
user-invocable: true
---

# Run and chain through the cld broker

A ticket container has no docker socket. `cld run` and `cld chain` ask the host
broker to launch the job. The job does not know who started it. Ticket stop or
shutdown does not affect it.

## When to use it

Use it for a **bounded, non-interactive job on top of your own work**: one agent
with a prompt (`run`), or a prepared multi-step pipeline (`chain`). For an agent you
steer, message and reap, use a task-agent (skill `task-agent-start`).

## Start

```bash
cld run   start [@refs...] -r <rev> [-n <name>] [-m <model>] [-p <text>] [--repo <name>]
cld chain start @<chain> [@refs...] -r <rev> [-n <name>] [-m <model>] [-p <text>] [--repo <name>]
```

- `-r` is required. It must be a **committed change in your own tree** of that repo.
  A working-copy commit is refused: run `jj new` first if you want your current
  edits in the base, then pass the parent change.
- `-r` is resolved here against your workspace, so `@-` means your parent change.
  The broker receives the change id.
- Refs: `@refs` resolve on the host. A path in your filesystem is read here and
  folded into `-p`. A chain takes `@<chain-name>` first (from the repo's or cld's
  `chains/`), then the `@refs` of its initial brief.
- `-n` names the job: letters, digits, `_` and `-`. A run's session is `run_<name>`,
  a chain's name is the name itself. Without `-n` a run gets a random one.
- `--repo <name>` only when the ticket mounts more than one repo (`cld repos`).
- Prints `started: <name>`. Use that name for the other verbs.
- Not available: `--shared-anchor`, `--no-detach`, `--force`, `--parent`.

## Cap

At most **2 live jobs per ticket**, runs and chains together. A chain counts as 1.
A finished job frees its slot. A third start is refused and names the live jobs.

## Watch

```bash
cld run status                  # lines: name<TAB>kind<TAB>state<TAB>base
cld run status <name>           # key: value lines; name, kind, state come first
cld run wait <name> [--timeout <seconds>]
cld run cancel <name>
```

`cld chain ...` is the same. Only your own jobs are visible.

- `wait` polls `status` every 15 s until the state is not `running`. The default and
  the maximum are 540 s, under the 10-minute command limit. Exit 0: finished, the
  detail block is printed. Exit 124: still running; call it again.
- Waiting more than a few minutes: loop `wait` calls, or run it with
  `run_in_background`.
- A run's state is `running`, then from its summary: `success`, `failed`,
  `no_changes` and so on, or `gone`. A chain's state comes from its own state file;
  `stale` means it says `running` but its process is dead.

## No guard on the base

Nothing stops you from rewriting the base, or anything below it, while a job runs.
The job keeps working on the old content, and jj rebases its work onto the new
content.

## What the result holds

Read it with plain jj in the shared store (`jj log -r <bookmark>`).

- The bookmark: `run_<name>` for a run, `chain_<name>` for a chain.
- A scratch commit right above the base. Its description starts with `cld anchor:`
  and it holds `.cld-run/brief.md`.
- The `agent-output-<session>/` folder: summary and, on failure, the failure note.
- For a chain, also the `chain-outputs/<name>/` files, one per step.

The scratch commit and the output folders are bookkeeping, not part of the
deliverable, unless you decide otherwise.

## Integration

Your call. Which commits to keep, and whether to rebase, squash or merge, is up to
you. The tooling does not do it.

## Cancel

`cancel` stops a live job. Work committed so far stays on its bookmark. A job that
is not live cannot be cancelled.

## Failures

| Message | What to do |
|---|---|
| `the host broker is not configured for this container` | Not wired for this container. Tell the user and stop. |
| `... is a working-copy commit` | Run `jj new`, pass the parent change. |
| `must resolve to exactly one change` | Narrow the `-r` revset. |
| `denied: ...` from the broker | The message names the rule: base outside your tree, repo not mounted, cap reached, bad name. Fix the cause or tell the user. |
| ssh `Connection refused` / `timed out` | The broker is not running on the host. Ask the user to run `cld-brokerctl start`. |

Never hand-build an `ssh` call to the broker.
