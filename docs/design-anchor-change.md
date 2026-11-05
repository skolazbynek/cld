# Unified Anchor-Change UX

> **Note (2026-07):** the "host stages `B` via `jj split --onto A`" details in
> this document have been superseded. Anchor staging now runs peer-side inside
> the container's ephemeral workspace via `stage_in_workspace` /
> `stage_from_env` in `cld/vcs/scratch.py`. The origin working copy is never
> touched (fixes the `@ IS A` case, which previously rewrote change `A`). See
> CLAUDE.md § *Anchor change contract* and docs/design-master-sibling-launch.md
> § *Delegated anchor work* for the current wire and staging flow. Goals,
> invariants, and the descendant contract below still apply.
>
> **Note (2026-08-19, corrected 2026-09-16):** the enforced/exported
> `AGENT_ANCHOR_HASH` depends on the anchor *mode* (PRODUCT_DESIGN.md §8,
> docs/design-ticket-containers.md §3). Isolated mode (default): the anchor is the
> scratch commit `B` itself, exactly as §4.2/§4.4 below describe -- only B's
> own descendants are editable. Shared mode (explicit opt-in): the anchor is
> `A`, so any pre-existing descendant of `A` -- not only descendants of `B`
> -- is editable. An earlier revision of this note claimed the anchor is
> always `A`; that contradicted the entrypoint's actual behavior and is the
> documented contradiction the v2 spec settles.

## 1. Goal & invariant

Every `cld` subcommand (`agent`, `devcontainer`, `review`, `loop`, `chain run`)
shares one notion of an **anchor change**:

> The anchor change is the immutable root from which every change produced by
> the command must descend. On invocation, the command creates a single empty
> **editable root** as a direct child of the anchor; all host-side setup
> (scratch files, composed task inputs, diff patches) and all in-container
> work happen on the editable root or on changes the command explicitly
> creates above it. The anchor is never written to.

Default anchor: the current change (`@` in jj, `HEAD` in git).
Override: `-r` / `--revision`, accepted by **every** subcommand.

Tree shape the contract guarantees:

```
anchor                (immutable; user's @ or -r, hash-pinned)
  └─ editable_root    (empty child created by the command; host writes here)
        └─ … command-specific structure …
```

What lives above `editable_root` is the command's choice (single agent
commit, per-iteration stack for `loop`, per-step stack for `chain`,
free-form edits for `devcontainer`). The shared contract enforces only the
boundary between the anchor and `editable_root` and the requirement that
all host writes happen inside the workspace directory.

## 2. Current state (per command)

| Command       | `-r` accepted | Default rev                          | Anchor hash pinned? | Empty editable root? | Scratch file location |
|---------------|---------------|--------------------------------------|---------------------|----------------------|-----------------------|
| `agent`       | yes           | empty → jj's internal default (`@-`) | no                  | jj: yes (by `workspace add` side-effect); git: no | host `<repo>/.cld/` |
| `devcontainer`| yes           | empty → jj's internal default (`@-`) | no                  | jj: yes; git: no     | n/a |
| `review`      | **no**        | n/a (inherits from `agent`)          | no                  | as `agent`           | host `<repo>/.cld/` |
| `loop`        | yes           | `@` / `HEAD`                         | resolved to hash    | no — branch points directly at anchor hash | host `<repo>/.cld/` |
| `chain run`   | yes           | `@` / `HEAD`                         | resolved to hash    | no                   | host `<repo>/.cld/` |

CLI help text on `agent`/`devcontainer` declares "default: `@-` for jj,
`HEAD` for git", which contradicts the task statement. `loop`/`chain`
already match the target default.

## 3. Gaps & bugs

1. **Default-rev policy is inconsistent.** `agent`/`devcontainer` advertise
   and forward "last committed" (`@-`); `loop`/`chain` use current (`@`);
   `review` has no knob. Target: `@` everywhere.

2. **Anchor is not hash-pinned at command entry.** Between resolution and
   `jj workspace add` / branch creation, `@` can shift (concurrent shell,
   jj auto-snapshot moving `@` after a file edit). All commands must
   resolve the anchor to a concrete commit ID **once**, up front, and use
   that hash thereafter.

3. **No explicit empty editable root on top of anchor.** jj's
   `workspace add` happens to create such a child as a side-effect; git's
   `worktree add -b NAME PATH REV` does not (HEAD == anchor on entry; the
   first agent commit retroactively becomes the empty child). Both
   backends must end in the same shape.

4. **Anchor immutability isn't enforced or surfaced.** Nothing prevents an
   agent (or interactive devcontainer user) from `jj edit <anchor>` or
   `jj squash --into <anchor>`. Exit reports never name the anchor, so a
   user can't easily diff `anchor..tip`.

5. **Host scratch files live outside the anchor tree.** Host code writes
   `.cld/loop-impl-iter*.md`, `.cld/loop-diff-*.patch`,
   `.cld/loop-review-iter*.md`, `.cld/chain-<chain>-<step>.md`,
   `.cld/persona-<chain>-<step>.md`, `.cld/review-diff-*.patch`,
   `.cld/review-task-*.md` into the **caller's** working-copy directory via
   `cld_tmpdir(repo_root)`. `.cld/` is gitignored, so jj doesn't snapshot
   them into host's `@`, but they are nonetheless host-side artifacts
   produced by the command outside the anchor tree:
     - Two simultaneous `cld` runs in the same repo collide on filenames.
     - Cleanup in `_cleanup_temp_files` is wildcard-based and
       indiscriminate — interrupts one command, wipes another's files.
     - The files have no anchor-rooted location, so the workspace they
       belong to cannot be reconstructed without host access.

6. **`loop`/`chain` ignore the secondary-workspace hint.** When invoked
   from a secondary jj workspace, `find_repo_context` returns a non-empty
   `workspace_revision`; `agent`/`devcontainer` use it as the default rev,
   but `loop`/`chain` hard-code `"@" / "HEAD"`. Effect: in a secondary
   workspace, `cld loop` anchors at the **main** workspace's `@`.

7. **No branch-collision check.** `loop` and `chain` call
   `vcs.create_branch` on `loop_<name>` / `chain_<name>` unconditionally.
   A second run with the same `-n` fails partway with half-set-up state.

8. **Legacy non-preinit path in agent entrypoint.**
   `entrypoint-claude-agent.sh` carries a fallback that re-implements
   anchor resolution in shell when `WORKSPACE_PREINITIALIZED` is not set.
   Source of silent drift.

9. **`review` has no `-r`.** No way to pick the workspace baseline.

10. **Anchor not propagated to the container.** `AGENT_REVISION` is
    forwarded but only `devcontainer` and the agent's legacy fallback
    use it. With immutability part of the contract, the container needs
    the anchor hash to guard against rewrites.

## 4. Design

### 4.1 Shared abstraction (`cld/vcs/anchor.py`)

```
resolve_anchor(vcs, revision: str) -> str
    # revision == "" -> resolves "@" (jj) or "HEAD" (git),
    #   respecting vcs.workspace_revision when in a secondary workspace.
    # Returns a concrete commit hash. Never returns a symbolic name.

create_editable_root(vcs, anchor_hash: str,
                     workspace_path: Path, branch: str) -> None
    # Creates a workspace at workspace_path whose @ is an empty descendant
    # of anchor_hash, with branch/bookmark pointing at it.
    #
    # jj:  jj workspace add --name <branch> -r <anchor_hash> <workspace_path>
    #      jj bookmark create <branch> -r @          (run inside the new ws)
    # git: git worktree add -b <branch> <workspace_path> <anchor_hash>
    #      git -C <workspace_path> commit --allow-empty -m "cld: editable root"
    #
    # Postcondition (both backends): workspace_path's @ is empty,
    # branch points at @, and @'s only parent is anchor_hash.

assert_descendant(vcs, anchor_hash: str, candidate: str) -> None
    # Raises if candidate does not have anchor_hash in its ancestry.
    # Cheap: jj log -r 'ancestors(<c>) & <anchor>' / git merge-base --is-ancestor.
```

These three helpers form the entire shared contract. There is no separate
"seed" or "inputs" concept; whatever structure a command wants above
`editable_root` is built with the existing `VcsBackend` methods.

### 4.2 Command-side opening sequence

```
1. A        = resolve_anchor(vcs, args.revision)             (jj resolve to hash)
2. session  = build_session_name(...)                        (branch/bookmark)
3. B        = stage_anchor_with_scratch(vcs, A, session,
                  scratch_files)                             (see §4.4)
              # jj split --onto A -m "cld anchor: <session>" .cld-run
              # B is a child of A containing only .cld-run/*;
              # the user's own working-copy change stays where it was,
              # minus the extracted .cld-run/ files.
4. launch the container with AGENT_ANCHOR_HASH=B and no bind-mount at
   /workspace/current -- the container entrypoint creates its own
   ephemeral workspace at /workspace/current on top of B (jj store lives
   in /workspace/origin/.jj/repo via the writable store mount rows) and sets a
   bookmark <session> on the anchor (B when isolated, A when shared). The
   bookmark is static: it never follows the work, and only a reanchor moves it.
5. on each subsequent advance of the persistent branch:
       assert_descendant(B, new_tip)
6. exit report prints B's hash and uses B..tip for inspect commands
   (§4.6). No host-side workspace directory is ever created.
```

Restart of a persistent container (`cld master restart` / `cld agent
restart`) skips steps 1--3: the container entrypoint sees the existing
bookmark `<session>` in the origin store and reattaches by pointing a
fresh workspace at the old workspace's tip (read before that workspace is
forgotten); the bookmark itself only locates the anchor. Watchman-driven autosnapshots
during the previous session's runtime guarantee that uncommitted edits
made just before shutdown are visible on reattach.

Shutdown of a persistent container (`cld master shutdown` / `cld agent
shutdown`), in contrast, is terminal for the session. The host forgets
the bookmark `<session>` from the origin store after `docker rm`, so the
next `cld <role>` launch runs steps 1--3 again as a fresh lifecycle and
`-r/--revision` is honored. Commits and op-log snapshots made during the
prior life stay in the store (reachable via `jj log -r 'heads(all())'`
and by change ID); only the named pointer is dropped. Invariant: bookmark
`<session>` exists in the origin store iff a live-or-restart-paused
lifecycle owns that session.

### 4.3 Per-command changes

**`cld agent`**
- CLI help: default-rev wording becomes "current change (`@` / `HEAD`)".
- Host: replace `effective_revision = revision or workspace_rev` with
  `resolve_anchor`. Drop the bespoke `_create_agent_workspace`; use
  `create_editable_root`.
- Container: keep the `WORKSPACE_PREINITIALIZED` path only. **Delete**
  the legacy fallback in `entrypoint-claude-agent.sh`.
- After the container exits, run `assert_descendant(anchor, "@-")` on the
  agent's branch before any host-side post-processing. Failure goes into
  `summary.json` as `"anchor_violation"`.

**`cld devcontainer`**
- CLI help wording fix as above.
- Host pre-creates the workspace via `create_editable_root` (same as
  `agent` does), passing `WORKSPACE_PREINITIALIZED=1`. The container
  entrypoint loses its workspace-creation branch entirely; both
  entrypoints take the same pre-initialised path.
- On exit, print the anchor hash next to the workspace branch.

**`cld review`**
- Add `-r` / `--revision` option (default `@`/`HEAD`).
- Resolve anchor and forward to `launch_agent` via the existing
  `revision=` param. Diff between feature/trunk is independent of the
  anchor — the anchor only sets the review agent's workspace baseline.

**`cld loop`**
- Replace `start_commit = vcs.resolve_revision(revision or default_rev)`
  with `resolve_anchor` (honors secondary-workspace hint).
- Replace `vcs.create_branch(loop_branch, start_commit)` with
  `create_editable_root(vcs, anchor, ws_path, loop_branch)`. Per-iteration
  agents stack their changes above `editable_root`; loop chooses whether
  to commit per-iteration inputs before each impl agent or to leave them
  in the working change.
- Before each `vcs.set_branch(loop_branch, impl_session)`, call
  `assert_descendant(anchor, impl_session)`. On failure, abort the
  iteration and leave the impl branch for inspection.
- Exit report: replace `jj log -r '{loop_branch}::@'` with
  `jj log -r '{anchor}..{loop_branch}'`. Print anchor hash.

**`cld chain run`**
- Same anchor resolution and `create_editable_root` call as `loop`.
- `initialise_chain_branch` becomes a thin wrapper around
  `create_editable_root`.
- `advance_chain_branch` runs `assert_descendant(anchor, session)`
  before `set_branch`.

### 4.4 Scratch-file isolation

Host scratch files (composed task inputs, diff patches, persona stagings)
are extracted into the anchor commit `B` via `jj split`, then read by the
in-container agent from `/workspace/current/.cld-run/*`. The host-side
sequence is:

```
1. write scratch files to <repo>/.cld-run/<file>            (host WC)
2. jj status                                                (force snapshot)
3. jj split --onto <A> -m "cld anchor: <session>" .cld-run
     -> extracts .cld-run/ into a new child B of A,
        leaves the user's own commit intact minus scratch
4. return B's commit hash                                   (== AGENT_ANCHOR_HASH)
```

`.cld-run/` is a reserved, **not-gitignored** top-level directory
(`stage_anchor_with_scratch` asserts on `.gitignore` and refuses if it is
excluded). The directory exists only briefly on the host during staging;
after the split, it is gone from the user's working copy and lives solely
inside `B`.

Mechanics:

- `stage_anchor_with_scratch(vcs, anchor_hash, session, scratch_files:
  dict[str, bytes]) -> str` in `cld/vcs/scratch.py` implements the
  sequence above. It is **jj-only**: the git backend raises
  `NotImplementedError`.
- On any failure between step 1 and step 3, the function best-effort
  restores the pre-staging op via `jj op restore` and deletes the host
  scratch files, logging the exact `jj op restore <op-id>` command the
  user can run if auto-recovery itself fails.
- The container reads scratch input from its own workspace at
  `/workspace/current/.cld-run/*` -- the files are visible there because
  the workspace is anchored on top of `B`.
- No host-side workspace directory exists, so there are no wildcard
  scratch-file cleanup paths. Files die when the container's ephemeral
  workspace dies.
- Concurrency: session-specific commit descriptions (`cld anchor: <session>`)
  and per-session bookmarks mean parallel staging is safe as long as jj's
  own concurrent-op semantics allow (which they do: `jj op log` merges
  divergent ops).

`CLAUDE.md` documents `.cld-run/` as the cld-reserved scratch directory
that appears only briefly on the host during anchor staging. No change to
repo `.gitignore`.

### 4.5 Container-side guard

Add one helper to `vcs-lib.sh`:

```
vcs_assert_descendant ANCHOR REV   # exit 1 if REV does not descend from ANCHOR
```

`entrypoint-claude-agent.sh` reads the anchor hash from a new env var
`AGENT_ANCHOR_HASH` (set by the host alongside `WORKSPACE_PREINITIALIZED`)
and asserts:
- once before `vcs_commit`,
- once before `vcs_squash_into_parent`.

On failure, write `AGENT-FAILURE.md` with a machine-readable reason
(`anchor_violation: <observed parent> is not a descendant of <anchor>`)
and exit non-zero. This is the in-container half of the host-side
post-step check, catching damage that happens between commit and squash.

### 4.6 Output / reporting

Every exit report gains:

```
Anchor:    <12-char hash>   (resolved from "<symbolic input>", e.g. "@", "main", or user-supplied rev)
```

Inspect commands switch to the anchor-relative form:

- jj:  `jj log -r '<anchor>..<branch>'`, `jj diff --from <anchor> --to <branch>`
- git: `git log <anchor>..<branch>`, `git diff <anchor>..<branch>`

## 5. Backwards-compatibility notes

- `AGENT_REVISION` env var is preserved for now but only as a hash.
  `AGENT_ANCHOR_HASH` is added. Update CLAUDE.md's container-side table.
- Behavioural change for users who currently rely on
  `cld agent`/`devcontainer` defaulting to `@-`: they now default to
  `@`. Matches the task contract and what `loop`/`chain` already do.
  Note in CHANGELOG / release notes.
- Removing the agent entrypoint's legacy non-preinit branch means direct
  `docker run claude-agent:latest ...` (bypassing `cld agent`) stops
  working unless the caller pre-creates the workspace. Acceptable: that
  path is not a supported entry point.
- `.cld-run/` becomes a reserved directory name inside agent workspaces.
  Repos that already use this name at the top level would conflict;
  scan repos at upgrade time (one-line grep) before shipping.

## 6. Implementation order (suggested)

1. Add `cld/vcs/anchor.py` with `resolve_anchor`,
   `create_editable_root`, `assert_descendant`; unit tests against both
   backends.
2. Migrate `cld agent` host path to use the helpers; delete legacy
   container fallback in `entrypoint-claude-agent.sh`.
3. Migrate `cld devcontainer` host path; collapse the container's
   workspace-creation branch.
4. Add `-r` to `cld review`; route through `resolve_anchor`.
5. Migrate `cld loop` (anchor + editable_root + post-step
   `assert_descendant`); add anchor printing.
6. Migrate `cld chain run`; same shape.
7. Move scratch-file writers (`launch_review`, `_compose_review_prompt`,
   `_compose_iter_prompt`, `compose_task`,
   `_stage_persona_without_frontmatter`) from `<repo>/.cld/` to
   `<workspace_path>/.cld-run/`. Update template paths from
   `/workspace/origin/.cld/...` to `/workspace/current/.cld-run/...`.
   Delete `cld_tmpdir` and `_cleanup_temp_files`.
8. Container-side `vcs_assert_descendant` + entrypoint guards;
   propagate `AGENT_ANCHOR_HASH`.
9. CLI help text + CLAUDE.md update (default-rev wording, env-var
   table, `.cld-run/` description).
10. End-to-end tests: `cld <each command> -r <hash>` then verify
    `assert_descendant(<hash>, <session_branch>)` holds; deliberate-
    mutation test in a sandbox confirming both the host-side check and
    the container-side guard fire.
