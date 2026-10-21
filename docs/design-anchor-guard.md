# Anchor guard — enforcing the anchor contract in-container

> Implemented. Behavior claims marked *(verified)* were checked on jj 0.45.1
> and the Claude Code build in the container image on 2026-10-12. The anchor contract itself is defined in
> `docs/design-anchor-change.md` and `docs/design-ticket-containers.md` §3;
> this document only enforces it.

## 1. What is enforced

Per repo workspace, with `A` = the recorded anchor change id:

- **Editable set:** `change_id(A)+::` — strict descendants of the anchor.
  The anchor itself is never editable. In isolated mode `A` is the scratch
  commit `B`; in shared mode it is the base commit, so the set widens to
  every descendant of the base. The same expression covers both modes.
- **Parent set:** `change_id(A)::` — new commits may be placed on the anchor
  or any of its descendants, never elsewhere.
- **Working copy:** `@` must be in the editable set, because watchman
  snapshots every file write into `@`.

`change_id()` is used instead of a commit hash so a host-side rebase of the
anchor (new hash, same change id) keeps the rules valid.

## 2. Three layers

No single place catches everything. The agent can reach jj through tools,
through any shell construct, and through scripts. Each layer covers the
gap of the one before it.

| Layer | Where | Catches | Misses |
|---|---|---|---|
| 1. jj immutability | jj workspace config, set at boot | every rewrite of the anchor, its ancestors or any commit outside the tree — from any caller, any command, any script | moving own work *out* of the tree; new commits outside the tree; bookmark moves; `--ignore-immutable` |
| 2. `PreToolUse` hook | Claude Code managed settings | file writes while `@` is outside the tree; jj commands whose destination is outside the tree; bypass flags; bookmark moves | jj calls hidden inside scripts, `eval`, `$(...)` |
| 3. `PostToolUse` hook | Claude Code managed settings | `@` ending up outside the tree after any Bash call, however it got there | the write that already happened in that call |

Layer 1 does the heavy lifting and needs no command parsing. Layer 2 blocks
the cases jj allows. Layer 3 is the catch-all feedback loop.

## 3. Layer 1 — jj native immutability

At boot, per jj workspace:

```
jj config set --workspace 'revset-aliases."immutable_heads()"' \
    "builtin_immutable_heads() | ~(change_id($A)+::)"
```

Immutable commits are `::immutable_heads()`, which here is everything
except the editable set. The editable set is closed under descendants, so
no editable commit is an ancestor of an immutable one.

Why workspace scope:

- The workspace config file lives at
  `~/.config/jj/workspaces/<hash>/config.toml` *(verified)*, inside the
  container's own home. The host and sibling containers sharing the same
  jj store never see it. Repo-scope config (`.jj/repo/config.toml`) would
  leak to them through the origin bind mount.
- Workspace config has higher precedence than user and repo config; only
  command-line `--config` beats it (blocked in layer 2).
- Host-side commands (`cld restart --reanchor`, `move_anchor_commit`) run
  in the origin workspace and are unaffected.

Behavior *(verified)*:

| Command | Result |
|---|---|
| `jj describe` / `edit` / `squash --into` on the anchor | refused: "Commit … is immutable" |
| `jj describe` on a sibling branch | refused |
| `jj describe` on own work | allowed |
| `jj rebase -r @ -o <outside>` | **allowed** — own work leaves the tree and becomes immutable, so it cannot even be moved back without `--ignore-immutable` |
| `jj new <outside>` / `jj new -A <outside>` | **allowed** |
| `jj bookmark create foo -r <outside>` | **allowed** |
| any rewrite with `--ignore-immutable` | **allowed** |

The **allowed** rows are what layer 2 exists for.

Side effects:

- The default `jj log` revset is built on `immutable_heads()`, so in the
  container it shows the anchor tree plus two ancestors. This is a
  welcome narrowing, not a regression.
- If the anchor change disappears (abandoned on the host), `change_id(A)`
  is empty and everything becomes immutable. This fails closed.
- A task-agent's deliverable bookmark is created on the anchor. "Squash
  into the deliverable branch" while the bookmark still sits on the anchor
  is a contract violation, and jj refuses it. Moving the bookmark to the
  work (`jj bookmark set`) is the intended path and stays allowed.
- Evaluating `~(…+::)` touches the whole commit index. Measure `jj log`
  latency on the largest registered repo during implementation.

## 4. Anchor registry file

The hooks need, per workspace: path, anchor, mode and backend. Today this
data is spread across three boot paths (ticket `*_ANCHOR` env file,
task-agent `AGENT_ANCHOR_HASH`, `cld run` `AGENT_ANCHOR_HASH`) with
different shapes. One file replaces the guesswork:

`/tmp/cld-anchors.jsonl` (overridable with `CLD_ANCHORS_FILE`, read by both
the helper and the hook), one JSON object per line:

```json
{"repo": "cld", "workspace": "/workspace/cld/cld", "anchor": "<change id>", "mode": "isolated", "backend": "jj"}
```

Written by one new helper in `imgs/claude-devcontainer/vcs-lib.sh`:

```
cld_guard_anchor REPO WORKSPACE ANCHOR MODE BACKEND
```

It appends the record and, for jj, runs the `jj config set --workspace`
from §3. It is called from all three boot paths, right after the anchor is
known:

- ticket loop: next to `cld_record_anchor_env` in
  `entrypoint-claude-devcontainer.sh`; the file is truncated at the start
  of the ticket branch, like `/tmp/cld-ticket-ready`;
- task-agent (v1 path): after `cld_boot_workspace`, with `/workspace/current`;
- `cld run`: in `imgs/claude-run/entrypoint-claude-run.sh` after
  `AGENT_ANCHOR_HASH` is set.

A failure of the `jj config set` call aborts the boot: a container running
without the guard is worse than one that does not start. An empty anchor
is skipped with a warning.

No registry file means "not a cld container": both hooks exit 0 silently.

## 5. Hook registration

In `imgs/claude-base/managed-settings.json` (baked into
`/etc/claude-code/managed-settings.json`):

```json
"hooks": {
  "PreToolUse": [
    {"matcher": "Write|Edit|MultiEdit|NotebookEdit|Bash",
     "hooks": [{"type": "command", "command": "python3 -P /opt/cld/cld/vcs/anchor_guard.py", "timeout": 15}]}
  ],
  "PostToolUse": [
    {"matcher": "Bash",
     "hooks": [{"type": "command", "command": "python3 -P /opt/cld/cld/vcs/anchor_guard.py", "timeout": 15}]}
  ]
}
```

Why managed settings:

- Root-owned and outside `$HOME`, so the host's bind-mounted `~/.claude`
  cannot shadow or remove it, and a user-level `disableAllHooks` cannot
  turn it off (docs: only managed-level `disableAllHooks` can).
- Applies to every container kind built on `claude-base`: tickets,
  task-agents and `cld run`.
- Hooks also fire for subagent tool calls (docs).
- A `PreToolUse` deny blocks the call even under
  `--dangerously-skip-permissions` *(verified)*.

Why a script path, not `-m cld.vcs.anchor_guard`: the module imports only
the standard library, and `-m` would first import the `cld.vcs` package and
its backends (~100 ms on every call). `-P` keeps the hook's cwd off
`sys.path` — the hook runs in Claude's cwd, which for this repo contains
its own `cld/` package.

One module, dispatching on `hook_event_name` and `tool_name`. It lives in
`cld/vcs/` beside `anchor.py`, whose revset conventions it reuses.

Deny output is the documented JSON on stdout with exit 0:
`hookSpecificOutput.permissionDecision = "deny"` plus a reason that names
the rule and the fix. The reason is what Claude reads, so it must say what
to do instead (for example: "`@` is outside the anchor tree; run
`jj new <anchor-tree commit>` first").

**Failure mode: fail open.** An internal error (crash, jj timeout) exits 1,
which Claude Code treats as non-blocking. Layer 1 still stands, and a
broken guard must not freeze the agent. The error goes to stderr for
debugging.

All jj calls the hook makes use `--ignore-working-copy`, so the hook never
snapshots and never races watchman for the working-copy lock.

## 6. Layer 2 — `PreToolUse` rules

### 6.1 File tools (`Write`, `Edit`, `MultiEdit`, `NotebookEdit`)

Path: `tool_input.file_path` (`notebook_path` for `NotebookEdit` — field
name unconfirmed in the docs, read both). Resolve with `realpath`.

1. Path inside a registered workspace's `.jj/`, or inside
   `~/.config/jj/workspaces/` → **deny**. These hold the layer 1 config
   and the workspace's link to the store.
2. Path inside a registered **jj** workspace → check
   `@ & change_id(A)+::` is non-empty; else **deny**.
3. Path inside a registered **git** workspace → check
   `git merge-base --is-ancestor A HEAD` and `HEAD != A`; else **deny**.
4. Anything else (scratchpad, `~/.claude`, `/tmp`, the host mount via a
   symlink) → allow. The host repo mount is out of scope by decision.

### 6.2 Bash

Parse `tool_input.command` with `shlex` (`punctuation_chars=True`), split
into simple commands on `;`, `&&`, `||`, `|`, `(`, `)`. Track `cd <dir>`
within the line to know each command's directory; start from the hook
input's `cwd`, which follows Claude's own `cd` (docs). Strip leading
`VAR=value` assignments. Recurse into the string argument of
`bash -c` / `sh -c`. Any command whose program basename is not `jj` is
ignored.

`shlex` rejects some lines bash accepts: an apostrophe in a comment or an
unquoted heredoc body, a trailing backslash. Such a line is allowed when the
word `jj` does not appear in it, and denied otherwise, since its jj commands
cannot be checked.

For each `jj` command:

- Workspace = `-R/--repository` value, else the command's directory,
  matched against the registry by path prefix.
- Global options may appear anywhere and in `--flag=value`, `-fvalue` and
  `-f value` forms; the parser handles all three.

Rules, first match wins:

| Rule | Commands | Action |
|---|---|---|
| Bypass flags | `--ignore-immutable`; `--config`/`--config-file` touching `revset-aliases` | deny |
| Config tamper | `config set`/`unset`/`edit` touching `revset-aliases` or with `--workspace` | deny |
| Workspace ops | `workspace add`/`forget`/`rename` | deny (already forbidden by the agent persona) |
| Foreign workspace | any mutating subcommand whose workspace is not in the registry (for example `-R /workspace/origin/<repo>`) | deny — layer 1 config does not apply there |
| Destination | `new` (positional, `-o`, `-A`), `rebase`/`duplicate`/`revert`/`split`/`squash` (`-o`/`--onto`, `-d`/`--destination`, `-A`/`--insert-after`/`--after`) | deny if `(<rev>) ~ change_id(A)::` is non-empty. Exception: a `rebase` `-o`/`--onto`/`-d`/`--destination` revset that also contains a commit in `change_id(A)::` is allowed, since the rebased commits keep that parent (the rebase skill uses `--onto 'A \| trunk()'`) |
| Bookmarks | `bookmark create`/`set`/`move`/`advance` (`-r`, `--to`, `--from`) | deny if the target is outside `change_id(A)::` |
| Bookmarks | `bookmark delete`/`forget`/`rename`/`move`/`set`/`advance` of a bookmark that now points outside `change_id(A)::` | deny — not this container's ref |
| Everything else | read-only commands, `edit`, `describe`, `abandon`, `undo`, `op *`, unknown subcommands | allow — layer 1 covers rewrites |

The destination check runs one jj call per revset:

```
jj --ignore-working-copy -R <ws> log --no-graph --limit 1 \
   -r '(<rev>) ~ change_id(A)::' -T 'change_id.short()'
```

Non-empty output → deny. If jj rejects the revset itself, allow: the real
command fails the same way.

Subcommand aliases (`b`, `operation`, bookmark one-letter forms) are
normalised. Only `file show`/`list`/`annotate`/`search` count as read-only;
`file chmod`/`track`/`untrack` mutate.

`-B`/`--insert-before`/`--before` needs no check: it rewrites the target, so layer 1
refuses it when the target is outside the tree.

Operation-log commands (`undo`, `op restore`, `op revert`, `op abandon`)
are allowed by decision. They can move `@` out of the tree; layer 3
reports that.

## 7. Layer 3 — `PostToolUse` on Bash

After every Bash call, for each registered jj workspace, check that
`change_id(A)` still resolves and that `@ & change_id(A)+::` is non-empty.
If not, return `{"decision": "block", "reason": …}` so Claude sees which
repo drifted and how to get back. It cannot undo the call; it stops the
next file write from landing outside the tree, and layer 2's file check
enforces that.

Cost: one jj call per repo per Bash call, with no snapshot.

## 8. Not covered

- jj invoked from scripts, `eval`, `xargs`, or Python `subprocess`: layer 2
  cannot see it. Layer 1 still blocks rewrites, and layer 3 reports a
  displaced `@`. Commits created outside the tree without moving `@` can
  slip through.
- Writes through the Bash tool (`sed -i`, redirects) while `@` is outside
  the tree: only layer 3, after the fact.
- This is a guard against mistakes, not a security boundary. The agent
  owns `/tmp` and `/opt/cld` and could edit the registry or the hook. The
  container remains the trust boundary.
- Git-backend repos: file writes only (§6.1 rule 3). No git command
  parsing yet.
- Writes to the host repo mount: out of scope. Since the store-only mounts
  (`docs/design-secrets-containment.md`), `/workspace/origin/<repo>` holds only
  the jj/git store. Its `working_copy` is read-only, so plain jj there fails;
  boot calls pass `--ignore-working-copy`.
- A `jj git init` (or any mutating jj command) under an unregistered
  `/workspace` path is denied as a foreign workspace.
- Hook latency: Python start dominates. The `python:3.13-slim` base ships
  the standard library without bytecode caches, and the container user
  cannot write them, so every hook run recompiles its imports (~300 ms).
  Precompiling the standard library in the base image would remove most of
  it.

## 9. Files touched

| File | Change |
|---|---|
| `cld/vcs/anchor_guard.py` | new — the hook (both events) |
| `imgs/claude-base/managed-settings.json` | `hooks` block |
| `imgs/claude-devcontainer/vcs-lib.sh` | `cld_guard_anchor` helper |
| `imgs/claude-devcontainer/entrypoint-claude-devcontainer.sh` | truncate registry; call helper in ticket loop and task-agent path |
| `imgs/claude-run/entrypoint-claude-run.sh` | call helper |
| `tests/test_anchor_guard.py` | new |

## 10. Tests

- Unit, no jj: Bash parsing — compound commands, `cd` tracking, `-R`,
  flag forms, `bash -c` recursion, env-assignment prefixes, non-jj
  commands untouched.
- Integration on a throwaway jj repo (pattern used to verify §3): base,
  sibling, anchor, own child; layer 1 config applied; feed hook JSON on
  stdin and assert deny/allow for every row of the §6.2 table and every
  §6.1 rule.
- No registry file → hook exits 0 with empty output.
- Hook crash → exit 1 (fail open).

## 11. Decisions

- Operation-log commands: allowed; layer 3 reports drift.
- Git-backend repos: file-write check only for now.
- Host repo mount writes (linked files such as `.env`): out of scope.
- Failure mode: fail open; layer 1 is the floor.
