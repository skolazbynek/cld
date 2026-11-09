# Implementation plan: naming convention

Design: `docs/design-bookmark-naming.md`. Read it first; this plan only says
where and how. No backwards compatibility: old names are not read anywhere.

## Target names

| Object | Identifier (container, workspace, session bookmark, mailbox, broker id) | Built from |
|---|---|---|
| Ticket | `t.<ticket>` | `ticket_slug(ticket)` |
| Task-agent | `a.<ticket>.<agent>`, no ticket: `a._host.<agent>` | ticket from the parent identifier; `<agent>` = slug, `-N` on collision |
| Run | `run.<run>` | `-n`, default random 6 hex chars |
| Chain | `chain.<chain>` | `-n`, required |
| Chain step | `chain.<chain>.<step>` | step name, no group index |

Deliverable (task-agent MR) default: `<agent id>.mr`. A `--branch` value
without a dot is appended (`<agent id>.<value>`); with a dot it is used as-is.

## Step 0: confirm dotted jj workspace names

Before anything else, add a test to `tests/test_vcs_integration.py` (follow
its existing real-jj fixtures) that, in a temp repo:

- `jj workspace add --name t.x-y <dir>` succeeds,
- `jj log -r 't.x-y@'` resolves,
- a bookmark `t.x-y` and `t.x-y.mr` both exist and `jj git export` reports
  no failure.

Run it with `cld broker run-tests tests/test_vcs_integration.py -k <name>`.
If `<name>@` does not resolve, stop and report: the design needs a change.
Inside this container `jj workspace add` is blocked by a hook; the broker runs
tests on the host, where it is not.

## Step 1: `cld/names.py`

New module, the single source of every identifier. Standard library only.

- `SEP = "."`, `HOST_TICKET = "_host"`
- `ticket_slug(ticket)` (moved from `cld/docker.py`, same behavior)
- `ticket_id(ticket) -> "t.<slug>"`
- `agent_id(ticket_slug | None, slug, suffix=0) -> "a.<t|_host>.<slug>[-N]"`
  (`-N` only for suffix > 1, as today); validates the slug with the existing
  `_TASK_SLUG_RE` (move it here)
- `run_id(name) -> "run.<name>"`, `chain_id(name) -> "chain.<name>"`,
  `step_id(chain, step) -> "chain.<chain>.<step>"`
- `ticket_of(identifier) -> str | None`: `t.<t>` and `a.<t>.<x>` give `<t>`;
  `a._host.<x>`, other kinds and `""` give `None`
- `deliverable(agent_identifier, branch_arg) -> str`
- `check_part(part)`: raises `ValueError` on empty, `.`, `/`, `..`, a leading
  `.`, or a trailing `.lock`
- `docker_name_filter(identifier) -> "name=^" + re.escape(identifier) + "$"`

New `tests/test_names.py`: every function, including `_host`, `-N`,
`ticket_of` for each kind, `deliverable` both forms, `check_part` rejections,
and that the escaped filter does not match `tXus` for `t.us`.

## Step 2: tickets and host plumbing

- `cld/docker.py`:
  - remove `ticket_slug` (re-export not needed; update importers),
    `build_session_name`, `task_agent_container_name`; `ticket_container_name`
    becomes `names.ticket_id` (rename callers, or keep the function as a thin
    alias only if that keeps the diff much smaller; prefer renaming).
  - `allocate_task_agent_name(ticket_slug | None, slug)`: loops `names.agent_id`
    with suffixes against `_docker_status`, as today.
  - docstrings around lines 956-980 and the `stage_broker` docstring (line
    1477, "any `cld_*` session") updated.
  - the effective-anchor revset (around line 1210): unchanged logic; fix the
    comment example (`cld_ticket_x-1`).
- `cld/ticket.py`: all `ticket_container_name` calls (lines 113, 175, 248,
  294, 355, 477, 520, 683, 720) -> `ticket_id`. Line 673:
  `name.removeprefix("cld_ticket_")` -> `name.removeprefix("t.")`.
- `cld/messenger/identity.py` line 34: `ticket_id`.
- `cld/messenger/mailbox.py` line 725: `f"cld_ticket_{to}"` -> `ticket_id(to)`.
- `cld/cli.py` line 626: `ticket_id`.
- `cld/agent_runtime.py` line 24: `names.docker_name_filter(session_name)`.
  Grep for any other `--filter name=` and route it the same way.
- `host_identity_name` (`cld_master_*`) is not a spawned object: unchanged.

## Step 3: task-agents

- `cld/cli.py` `task_agent_start` (lines 240-330):
  - ticket = `names.ticket_of(parent)`; `session =
    allocate_task_agent_name(ticket, slug)`.
  - `branch = names.deliverable(session, branch)` (default `.mr`); run
    `check_part` on each part a user supplied. Help text of `--branch` and
    `-n` updated (the slug no longer falls back from `--branch` if `--branch`
    contains a dot; require `-n` in that case).
  - Refuse at start if the deliverable bookmark already exists and points
    outside the new agent's anchor tree. Use the existing revset helpers the
    overlap check uses; the entrypoint's warn-and-leave path
    (`entrypoint-claude-devcontainer.sh` around line 183) stays as a fallback.
- `cld/task_agent.py`:
  - `resolve_task_agent` (line 73): a bare slug matches identifiers whose last
    dot part equals it (`c.rsplit(".", 1)[-1] == name`). Remove
    `_cwd_repo_task_agent_name`; when several match, raise the existing
    ambiguity error listing them.
  - docstring at lines 77-78 updated.

## Step 4: runs

- `cld/run.py` line 82: `session = session_name or names.run_id(name or
  secrets.token_hex(3))`.
- `broker/cld-broker.py` `_job_pick_name` / `_job_command` (lines 1364-1377):
  `run.` instead of `run_` (default `run.<6 hex>`), `name.removeprefix("run.")`
  for the `-n` passed to `cld run`.

## Step 5: chains

- `cld/chain.py`:
  - `chain_branch(chain)` -> `names.chain_id(chain.name)`.
  - `step_session(chain, step)` -> `names.step_id(chain.name, step.name)`;
    drop the `group_idx` parameter and update both callers (lines 482, 517,
    659).
  - `chain_state_dir` (line 416): `.cld/chains/<chain_id>`.
  - `apply_name_override`: the override is now mandatory; raise if empty.
- `cld/cli.py` `chain run` (line 876): `-n/--name` required, help "Chain name
  (required, must be unused)". Lines 932-933: `names.chain_id(chain.name)`.
- In-use check, in `cld chain run` before anything is launched, after
  `_gc_or_refuse`:
  - a running container whose name starts with `chain.<name>.` exists
    (`docker ps --filter name=^chain\.<name>\.`, escaped), or
  - the bookmark `chain.<name>` exists in the target repo
    (`vcs.run(["bookmark", "list", "-T", ...])` or the backend's existing
    branch-exists helper).
  Each refusal names its reason and tells the user to pick another `-n` (or
  delete the bookmark).
- `broker/cld-broker.py`:
  - chain start (around line 1118): refuse without `-n`.
  - `_job_pick_name` for chains: the requested name is required.
  - `_chain_state` (line 1241): `.cld/chains/chain.{job.name}/state.json`.
  - job status (line 1344): `"bookmark": f"chain.{job.name}"`.
  - these strings duplicate `cld/names.py`; add a one-line comment pointing
    there (the broker is standalone and cannot import it).

## Step 6: broker session check

- `_SESSION_RE` (line 1982) -> `[A-Za-z0-9][A-Za-z0-9_.-]*`, and refuse any
  session containing `..` at both use sites (lines 1999, 2038). It stays a
  format check that keeps `/` and `..` out of paths built from the caller id.
- Header comment (lines 23-26): drop the `cld_agent_*` wording.

## Step 7: container side

- `imgs/claude-devcontainer/vcs-lib.sh`, where the workspace config is set
  (around line 236): for ticket and task-agent containers, also
  `jj config set --workspace templates.git_push_bookmark '"<id>.mr-" ++ change_id.short()'`
  with `<id>` = `$SESSION_NAME`. Not for run containers.
- `imgs/claude-devcontainer/container-init.sh` `write_ticket_claude_md`: add a
  "Bookmark names" section: session `t.<ticket>` (never move it), MRs
  `t.<ticket>.mr` / `t.<ticket>.mr-<word>`, other `t.<ticket>.<word>`, never
  `internal.`. The slug is `$2`.
- No other script change: session bookmark, workspace name and container name
  remain one string (`$SESSION_NAME`).

## Step 8: prompts, skills, docs

- `prompts/personas/task-agent.md`: deliverable is `${DELIVERABLE_BRANCH}`
  (already templated); add: extra bookmarks are
  `${CONTAINER_NAME}.<word>`, never `internal.`.
- `.claude/skills/task-agent-start`, `task-agent-wrapup`, `task-agent-fleet`,
  `broker-run-chain`, and the messenger skills: replace old example names
  (`cld_ticket_*`, `cld_agent_*`, `run_*`, `chain_*`) and describe `--branch`
  and the required chain `-n`.
- `CLAUDE.md`, `README.md`, `QUICK_START.md`, `PRODUCT_DESIGN.md`,
  `MANUAL_TESTS.md`, `broker/README.md`, and `docs/design-*.md` that show old
  names as current behavior: replace them. Historic decision tables may keep
  old names where they describe past decisions.
- `grep -rn -E "cld_ticket|cld_agent|run_[a-z0-9<{]|chain_[a-z<{]"` must come
  back empty outside historic notes, `cld_master_`, config keys
  (`run_image`, `chain_max_parallel`, ...) and `.cld/` state from old runs.

## Step 9: tests

- Update the existing tests that hard-code old names (about 22 files; start
  from the grep above over `tests/`). Change expectations, not behavior.
- New tests (beyond `tests/test_names.py` and step 0):
  - task-agent: ticket from a `t.` parent, from an `a.` parent, `_host` with
    none; `-N` allocation; `--branch` both forms; refusal when the deliverable
    exists outside the tree.
  - `resolve_task_agent` with dotted names and ambiguity.
  - chain: `-n` required (CLI and broker), both in-use refusals, step names
    without the group index.
  - broker: `_SESSION_RE` accepts `t.x`, `a._host.y-2`, refuses `t..x`,
    `t/x`, `.t`.
  - docker filter escaping in `wait_for_agent`.
- Run the full suite through the broker: `cld broker run-tests`. All green
  before finishing.

## Done when

- Full suite green through the broker.
- The step-8 grep is clean.
- One jj change per step or per logical group, each described, all descending
  from the anchor.
