# Run and chain from a ticket container, through the broker

Ticket CLD-001 (internal). Date: 2026-10-27. Status: design, not implemented.

A ticket container can start a `cld run` or a `cld chain run` on any change inside its
own editable tree. The host broker launches it. When the job finishes, the ticket agent
pulls the result into its own changes with plain jj.

File references are paths in this repo.

## 1. Today

- `cld run` and `cld chain run` refuse inside a container: there is no docker socket
  (`cld/run.py:launch_run`, `cld/chain.py:run_chain`, the `run`/`chain` stubs in
  `cld/cli_container.py`).
- The broker already relays one launcher: `action_task_agent` in `broker/cld-broker.py`
  re-runs `cld task-agent start` on the host with a checked argv (only `@refs` as
  positionals, `--force` and `--parent` denied, `--` denied).
- The jj store is shared. A run's work lands as the bookmark `run.<name>`, a chain's as
  `chain.<name>` (see `docs/design-bookmark-naming.md`). A ticket mounting the same repo sees both at once.
- `launch_run` calls `resolve_anchor_checked`. That check refuses a base inside any live
  container's editable tree, including a ticket's. Every chain step goes through
  `launch_run`, so every step runs the check again.
- A run leaves its scratch commit (`.cld-run/brief.md`) and an `agent-output-<session>/`
  folder in history. A chain also commits `chain-outputs/<chain>/<step>.md`.
- Chain state lives on the host in `<repo>/.cld/chains/chain.<name>/state.json`. A
  detached runner process owns it. The host has `cld chain status`, but no way to cancel
  a chain.

## 2. Decisions

These were settled with the user. Each line gives the choice, then the main reason.

| Topic | Decision | Why |
|---|---|---|
| Where a job may start | On any change inside the caller's editable tree in that repo. | This is what the feature asks for. The child's work then also lies inside the ticket's tree, so the ticket may edit it. |
| Base rewrites | No guard. The ticket may rewrite a live child's base. jj rebases the child's work, and the child recovers its stale workspace the same way it does after a host rebase today. | Smallest change. The risk is accepted. |
| Overlap check for run and chain | Removed completely from `launch_run`, for every caller, the host user included. | Run and chain behave the same no matter who launched them. A hidden "skip this ticket" flag would link the child to its launcher. |
| Who launched it | The child does not know. No label, env var or prompt text names the ticket. Ticket stop or shutdown does not affect it. | The user wants run and chain to behave the same whether a user or an agent started them. |
| What may be launched | A chain by name (`@<name>`), from the `chains/` folder of the target repo or of cld. A run from `@ref` prompts plus inline `-p` text. The same `@ref` rules as `task-agent start`. | Same trust model as today. A chain file only holds prompts, models and timeouts, never commands. |
| Base revision | `-r` is required. The broker refuses a working-copy commit of any workspace as the base. | There is no guard, and a working-copy commit changes on every snapshot. |
| Interface | Container CLI verbs plus a skill. No MCP server. | Same pattern as `task-agent` and `run-tests`. |
| Verbs | `start`, `status`, `wait`, `cancel`. | Covers a wrong launch. Logs and results come from the bookmark in the shared store. |
| Finish signal | Polling only: `status` and a bounded `wait`. No messenger mail. | Simple. Keeps the broker stateless between calls. |
| Cap | Hardcoded: at most 2 live jobs per ticket (runs and chains together). A chain counts as 1. A finished job frees its slot. | Bounds load without limiting a long session. |
| Landing | The ticket agent decides everything: which commits to keep, and how to merge them. No helper. The skill only states facts. | The user wants the agent to own integration. |
| VCS | jj only. The broker refuses a git-backed repo. | The reach check and peer staging have no git version. |

Defaults I chose without asking (cheap to change):

- `--shared-anchor` is denied from the broker. A shared child would own the whole tree
  below its base, including the ticket's own later work.
- `--no-detach` is denied for chains. The broker call must return at once.
- The container verbs are `cld run <verb>` and `cld chain <verb>`, not one `cld job`.
  They keep the host command names the user already knows.

## 3. Shape

```
ticket container                     host broker (sshd ForceCommand)              host
----------------                     -------------------------------              ----
cld run start @refs -r R -p ...  --> action_run start
                                       check argv, repo, R, cap
                                       write the launch record
                                       chdir <repo>; exec `cld run ... -r <change id>`  --> run container
cld run status|wait|cancel ...   --> action_run status|cancel
                                       read the launch record (own jobs only)
                                       docker / jj / state.json probes
jj log -r run.<name>             (local: the store is shared)
jj rebase / squash / new ...     (local: the ticket integrates on its own)
```

Chains have the same shape, through `action_chain` and `cld chain run @<name> ...`.

## 4. Host changes

### 4.1 Remove the overlap check from run

In `cld/run.py:launch_run`, replace `resolve_anchor_checked(...)` with
`resolve_anchor(get_backend(repo_root), revision)`. That covers `cld run` and every
chain step.

Effects to keep in mind:

- Other launchers still see run containers as occupants (`docker_occupant_list`). A
  task-agent still may not anchor inside a live run's tree. That is unchanged.
- A shared-mode ticket that reanchors while a run lives inside its tree is still refused
  by check 2 of `resolve_anchor_checked`. This is accepted: ticket lifecycle is out of
  scope.
- `resolve_anchor_checked`'s `caller_kind="run"` path then has no caller. Drop it, or
  keep it for tests. The implementer decides.

### 4.2 New host verb: `cld chain cancel <name>`

A host user needs it too, so it goes in `cld/cli.py`, not in the broker.

- Read `<repo>/.cld/chains/chain.<name>/state.json`. Refuse if the status is not
  `running`.
- Send SIGINT to the runner's process group (`start_new_session=True` makes the pid a
  group leader). `run_chain` already turns `KeyboardInterrupt` into
  `failure_reason="interrupted"`.
- `docker stop` every name in `current_step_sessions`, because `wait_for_agent` only
  polls and will not stop them by itself.
- Wait a short time for the runner to write `status=interrupted`. If it does not, mark
  the state `interrupted` itself, so `status` never shows a dead chain as running.

### 4.3 Chain name lookup

`_resolve_chain_path` joins `@<name>` to `chains/` with no escape check. The broker only
sends names matching `^[A-Za-z0-9_-]+$` (§5.2). Also add the escape check on the host
(`resolve().is_relative_to(<chains dir>)`), the same way `cld.prompts` does.

## 5. Broker changes (`broker/cld-broker.py`)

Two new actions, `run` and `chain`, both with the ops `start`, `status` and `cancel`.
`wait` is only a container-side loop over `status` (§6).

### 5.1 Who may call

- Only `LabelCaller` with `kind == "ticket"`. Task-agents and run containers are denied:
  agents never spawn (design-task-agents D14).
- The repo comes from `Caller.repo(selector)`: mounted repos only, because the base must
  lie in the caller's own tree in that repo. It must be jj-backed (`host_jj` works), or
  the call is denied.

### 5.2 `start`: argv rules

Same style as `action_task_agent`: the option arity is listed explicitly, never guessed.

- Denied tokens, in both spellings (`--opt` and `--opt=...`): `--`, `--shared-anchor`,
  `--no-detach`, `--force`, `--parent`.
- Options that take a value: `-n/--name`, `-m/--model`, `-p/--prompt`, `-r/--revision`.
  `-r` is required.
- Run positionals: every one must be an `@ref`.
- Chain positionals: the first must be `@<name>` with `<name>` matching
  `^[A-Za-z0-9_-]+$`. Any others must be `@refs` (the chain's initial brief).
- `-n` must match `^[A-Za-z0-9_-]+$`, because it becomes a docker name and a bookmark.

### 5.3 `start`: the base check

All on the host, with `host_jj` (`--ignore-working-copy`, so the broker never snapshots a
host workspace):

1. Resolve `-r` to exactly one commit, then take its change id. Zero or several
   commits: deny. Passing a change id on, not the caller's revset, makes the host
   command see the same commit the broker checked.
2. **Inside the caller's tree.** Find the caller's effective anchor for that repo from
   its labels: in isolated mode the scratch commit S, found with the revset that
   `cld/docker.py:_effective_anchor` uses; in shared mode the base A. Deny unless
   `<R> & <effective>::` is non-empty. The broker is stdlib-only and does not import
   cld, so it repeats that revset. A test pins the two copies to each other.
3. **Not a working copy.** Deny if `<R> & working_copies()` is non-empty. The error tells
   the agent to run `jj new` first, or to pick the parent change.

### 5.4 `start`: the cap and the launch record

- Records live in `~/.cld/broker-jobs/<caller session>/`. The folder is 0700. There is
  one JSON file per job: `kind` (run|chain), `name` (the session or chain name), `repo`
  (host path), `base` (change id) and `started_at`.
- Take an exclusive `flock` on `<caller folder>/.lock` for the whole start, so two
  parallel starts cannot both pass the cap.
- Count the live jobs among this caller's records (§5.6). At 2 or more: deny, naming
  the live jobs.
- Pick the name before launch: `run.<-n or random hex>`, or the chain's name after `-n`
  (a chain start without `-n` is denied). Deny if a record or a live container/chain
  already has that name. `cld chain run` itself also refuses a name whose bookmark
  `chain.<name>` exists.
- Write the record, release the lock, then `chdir <repo>` and exec the host command
  with the change id from §5.3:
  - run: `cld run <@refs> -n <suffix> -r <change id> [-m] [-p]`
  - chain: `cld chain run @<name> <@refs> -n <name> -r <change id> [-m] [-p]`
- If the host command fails, the record stays, but it never counts as live (§5.6), so
  the failure costs no slot.

Nothing in the record reaches the child. The record exists only so the broker can
enforce the cap and scope `status` and `cancel` to the caller's own jobs.

### 5.5 `status [<name>]`

No name: one line per record of the caller, `name<TAB>kind<TAB>state<TAB>base`. With a
name (it must be one of the caller's records): a detail block.

| Field | Run | Chain |
|---|---|---|
| state | `running` if the container `<session>` is running. Otherwise from `agent-output-<session>/summary.json` on the bookmark `<session>`: `success`, `failed`, `no_changes` and so on. No container and no summary: `gone`. | `status` from `state.json`. If `running` but the pid is dead: `stale`. |
| bookmark | `<session>` | `chain.<name>` |
| cost, duration | From `result.json` / `summary.json`, when they exist. | `total_cost_usd`, plus started and finished times. |
| where to look | `agent-output-<session>/` on the bookmark | `chain-outputs/<name>/` on the chain bookmark, and the step list from `completed_steps` |
| failure | The first lines of `AGENT-FAILURE.md` | `failure_reason` |

Output is plain text and capped. It never prints host paths outside the repo.

### 5.6 What "live" means

- Run: the container `<session>` exists and is running.
- Chain: `state.json` says `running` and its pid is alive.

Anything else frees the slot. Records of finished jobs older than 7 days are removed on
each call.

### 5.7 `cancel <name>`

- The name must be one of the caller's records, and the job must be live.
- Run: `docker stop <session>`. Work committed so far stays on the bookmark.
- Chain: exec `cld chain cancel <name>` in the repo (§4.2).

## 6. Container changes

### 6.1 `cld/cli_container.py`

Replace the `run` and `chain` host-only stubs with sub-apps:

```
cld run   start [@refs…] -r <rev> [-n <name>] [-m <model>] [-p <text>] [--repo <name>]
cld run   status [<name>]
cld run   wait <name> [--timeout <seconds>]
cld run   cancel <name>
cld chain start @<chain> [@refs…] -r <rev> [-n <name>] [-m <model>] [-p <text>] [--repo <name>]
cld chain status|wait|cancel …        (same as run)
```

- Local file positionals are read in the container and folded into `-p`, as
  `task-agent start` does (design-prompt-chaining §4). Only `@refs` cross to the host.
- `-r` is evaluated on the host, against the shared store. The client still resolves it
  locally first (`jj log -r <rev> -T change_id`) and sends the change id, so that
  `@`-style revsets mean the ticket's workspace and not the host's. A working-copy
  result is refused here too, with the hint to run `jj new`.
- `wait`: calls `status <name>` every 15 seconds until the state is not
  `running`, or until `--timeout` runs out. The default and the maximum are 540 seconds,
  under the agent's 10-minute command limit. Exit 0 when finished, 124 on timeout, and
  print the final detail block.
- `cld/broker.py`: add helpers like `broker_task_agent_op` (`broker_run_op`,
  `broker_chain_op`). Add `run` and `chain` to `_REVISION_ACTIONS`, so the ticket's
  workspaces are snapshotted before `start`.

### 6.2 Skill `broker-run-chain`

New skill under `.claude/skills/`. Facts only, no landing recipe:

- When to use it rather than a task-agent: a bounded, non-interactive job on top of the
  ticket's own work.
- `-r` must be a committed change in your tree. Run `jj new` first if you want your
  current edits in the base.
- No guard: rewriting the base (or anything below it) while the job runs makes the job
  continue on the old content, and jj rebases its work onto the new one.
- The cap is 2 live jobs. Polling: `wait` with a timeout, in a loop, or in the
  background.
- What the result holds: the bookmark name; the scratch commit right above the base,
  whose description starts with `cld anchor:` and which holds `.cld-run/brief.md`; the
  `agent-output-<session>/` folder (summary, failure note); for chains, the
  `chain-outputs/<name>/` files. These are bookkeeping, not part of the deliverable,
  unless the agent decides otherwise.
- Integration is the agent's own call: which commits to keep, and whether to rebase,
  squash or merge.
- Cancel keeps the committed work on the bookmark.

Also add one line each to `CLAUDE.md` (Broker subsystem) and `broker/README.md` (action
list).

## 7. Security notes

- The broker never evals the argv. Positionals are `@refs` only, and option values go to
  `cld` as arguments, as today.
- The base check (§5.3) enforces the anchor contract on the host: a ticket cannot start
  work outside its own tree, nor in a repo it does not mount.
- Prompt refs and chain files are read from the host checkout, as for `task-agent start`.
  The container cannot write there.
- The child gets the same mounts and secrets as a host-launched `cld run`. No new secret
  reaches any container.
- The cap and `flock` bound how much one ticket can start. There is no cost cap beyond
  the run's own max turns and the chain's timeouts. This is a known limit.

## 8. Tests

- `tests/test_broker_script.py`:
  - argv denials: `--`, `--shared-anchor`, `--no-detach`, a bare path, a bad chain
    name, a bad `-n`, a missing `-r`;
  - non-ticket caller denied; unmounted repo denied; git repo denied;
  - base outside the tree, in both isolated and shared mode; base is a working copy;
    revset resolves to several commits;
  - cap: a third start denied; a finished job frees its slot; two parallel starts under
    the lock;
  - `status` and `cancel` limited to the caller's own records;
  - the effective-anchor revset matches `cld/docker.py:_effective_anchor`.
- `tests/test_run.py`: `launch_run` no longer calls the overlap check.
- `tests/test_cli.py` (host): `cld chain cancel` (signal, docker stop of step sessions,
  state fallback); the `@<name>` escape check.
- Container CLI tests: ref folding, local `-r` resolution to a change id, the `wait`
  loop's exit codes, the working-copy refusal.

## 9. Order of work

1. Host: remove the overlap check from run (§4.1), add the chain name escape check
   (§4.3). Small, and needed by everything after.
2. Host: `cld chain cancel` (§4.2).
3. Broker: `action_run` / `action_chain` with start, status and cancel (§5).
4. Container: the CLI verbs and broker helpers (§6.1).
5. Skill and doc lines (§6.2).

## 10. Out of scope

- Messenger notifications on finish.
- A guard against rewriting a live child's base.
- Any link between a child and the ticket's lifecycle.
- git-backed repos.
- An MCP surface.
- A cost cap.
