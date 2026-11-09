# cld v2 — Ticket Containers

Product specification. Covers UX, workflows and use-cases; implementation
design is a follow-up document. Decisions were consulted with the user on
2026-09-16; each is marked **[ruled]** (user ruling) or **[rec]** (architect
recommendation, overridable).

## 1. The change

v1 centers on one container per repo (`cld master`): the user shells into the
container and works inside it — editor, jj, and claude all in-container.

v2 inverts the interaction model:

- **One container per ticket.** The ticket (usually a YouTrack id) is the unit
  of work, and the container is its sandbox.
- **A variable set of host repos is mounted at launch**, chosen interactively
  or on the command line.
- **The user never shells in.** From the host shell they run the harness
  (Claude Code) inside the container via a cld-wrapped `docker exec`.
- **The harness sees all mounted repos** in one working set.

What this buys, against v1's documented pain:

| v1 pain | v2 answer |
|---|---|
| Cross-repo work goes through messenger relays, empty placeholder dirs (4 documented defects) and throwaway peer containers | All the ticket's repos are files in one working set; one session spans them |
| The user's cockpit is inside the container (in-container nvim, shell) | The cockpit is the host terminal; the container is only a sandboxed executor |
| Every container shares cwd `/workspace/current`, so all Claude transcripts mix under one host slug (~359 files observed) | Per-ticket cwd gives per-ticket transcript slugs; resume works per ticket |
| Interactive session is invisible to cld (`logs` never captures it, `status` prints 2 lines) | Sessions are host-launched and enumerable per ticket |
| `restart` silently drops model/prompt/revision args | The launch manifest is persisted and restart preserves it |

## 2. Scope

**In scope:** ticket container lifecycle, repo registry, per-repo
workspace/anchor staging, harness invocation, user workflows, gaps and risks.

**Replaced by v2** [ruled]: `cld master` and the bare `cld` ephemeral
devcontainer. Both roles collapse into the ticket container: a one-repo ticket
covers the master use-case; a throwaway ticket name covers the ephemeral one.

**Out of scope:** `cld run` and `cld chain` (unchanged); `brokerctl` /
`otelctl`; any change to agents and task-agents — their product role is
re-examined in section 11, but all decisions there are deferred.

## 3. Concepts and identity

**Ticket container.** Named `t.<slug>` where `<slug>` is the
sanitized ticket name. One per ticket, enforced by the deterministic name.
Ticket names are free-form; by convention the YouTrack id (which also pairs the
container 1:1 with the `~/.claude/ego/tickets/<id>/` notes folder).

**Launch manifest.** The resolved repo set — per repo: registry name, host
path, anchor revision, anchor mode — is stamped into container labels at
launch. It is the single source of truth for `restart`, `status`, and broker
target validation. This fixes v1's parameter-dropping restart.

**Working set layout** [ruled: workspaces stay container-side, as in v1]:

```
/workspace/<slug>/            harness cwd — the "ticket root"
  <repo-a>/                   jj workspace (or git worktree) of repo-a
  <repo-b>/
/workspace/origin/<repo-a>/   the host repo's jj/git store only, never the checkout
/workspace/origin/<repo-b>/
```

Origin holds only `.jj` and `.git`, through the store mount set of
`docs/design-secrets-containment.md` section 3.1. Host settings stay read-only
there, and untracked host files such as `.env` never reach the container.

Workspace directories live in the container layer, exactly as v1's
`/workspace/current`: watchman + jj snapshot all content into the host-side
`.jj` store autonomously, so work is durable and host-visible without a
host mount. The entrypoint generates a small `CLAUDE.md` at the ticket root
listing the repos, their anchors, and the ticket name, so a fresh harness
session orients itself; each repo's own `CLAUDE.md` loads when working in its
subtree.

**Per-repo identities.** In each mounted repo's store, the ticket owns one jj
workspace and one bookmark, both named `t.<slug>` (same
quadruple-identity scheme as v1: container = workspace = bookmark = mailbox;
see `docs/design-bookmark-naming.md`).
The v1 invariant carries over per repo: the bookmark exists iff a live or
restart-paused ticket lifecycle owns it. The bookmark is static and always
sits on the active anchor (the scratch commit when isolated, the shared
commit when shared); it never follows the work, and only a reanchor moves it.

**Sessions.** Harness sessions are ordinary Claude Code sessions whose cwd is
the ticket root; transcripts land on the host under the per-ticket slug.
POC: one live session per container [ruled]; multiple concurrent sessions are
a wanted later feature and nothing in this design may preclude them (see
section 12).

## 4. Repo registry

[ruled] A named registry replaces `master_targets` and its placeholder
mechanism (whose four defects are documented in
`docs/design-master-target-selection.md`).

In `~/.config/cld/config.toml`:

```toml
[repos.lide-api]
path = "~/projects/lide-api"
default_rev = "trunk()"        # optional; falls back to trunk()

[repos.diskuze-api]
path = "~/projects/diskuze-api"
```

- Registry names are the subdirectory names inside the ticket root, so they
  are unique by construction. Ad-hoc paths are still accepted at launch (the
  basename becomes the subdir name; a collision is an error).
- No under-`$HOME` constraint (that was a placeholder artifact).
- `cld repos` lists the registry; `cld repos add <name> <path>` /
  `cld repos rm <name>` edit it.
- Per-repo `.cld/config.toml` inside each repo keeps its v1 meaning
  (`pyproject_dir`, graphql keys) and is read per repo. `ignore_gitignore` is
  off: listed files are skipped with a warning. `home_mounts_always` and
  `home_mounts_devcontainer` are read from the user config only.

## 5. Command surface

| Verb | Semantics |
|---|---|
| `cld start <ticket> [repo[@rev]…]` | Create-or-start. On a TTY with no repos given: interactive picker over the registry [ruled]. Repo args are registry names or paths, each with optional `@rev` anchor override. With *no arguments at all*: targets the cwd's repo directly (ad hoc, bypassing the registry) under a ticket named after the repo directory. |
| `cld claude <ticket> [-- args…]` | The daily verb: exec the harness at the ticket root. Everything after `--` passes through to claude (`-p`, `--resume`, `--continue`, `--model`). |
| `cld shell <ticket>` | Escape hatch: interactive bash in the container, for debugging the sandbox itself. |
| `cld stop <ticket>` | Pause: `docker stop`. Workspaces stay in place; `start` on a stopped ticket is a warm start. |
| `cld restart <ticket>` | Recreate the container from its persisted manifest. Workspaces reattach at their bookmarks. |
| `cld shutdown <ticket> [--all]` | End of ticket: teardown, forget the ticket's bookmark and workspace in every mounted repo. Commits always survive in the repo stores. |
| `cld status [<ticket>]` | Roster (all tickets: repos, state, live session) or detail (per-repo anchors, bookmark tips, session activity, uptime). |
| `cld logs <ticket>` | Entrypoint/boot logs. |
| `cld repos …` | Registry management (section 4). |

Notes:

- **Attach-to-harness must never be a bare positional on the root command**
  (`cld <ticket>`): a positional on the root group makes every subcommand
  unreachable in typer. `cld claude` is the spelling; a short alias may come
  later.
- **Changing the repo set** [ruled: no dynamic add/remove]: run
  `cld start <ticket> <new set>` against an existing ticket. cld shows the
  diff, asks to confirm, and recreates the container. Kept repos reattach at
  their bookmarks; repos leaving the set are torn down as in `shutdown`
  (bookmark and workspace forgotten, commits survive) [rec].
- **No launch-time prompt or model** [rec]: v1 master accepted `-p` and `-m`
  at launch (the prompt ran once, unattended, into docker logs; the model was
  baked into a wrapper). In v2 both belong to the `cld claude` invocation.
  Launch only prepares the sandbox.
- Non-TTY `cld start` with no repos is a hard error, not a hang.

## 6. Boot contract

On first `cld start`, per repo in the manifest:

1. Resolve the anchor revision: explicit `@rev` from the launch args, else the
   registry `default_rev`, else `trunk()` [rec; v1 defaulted to `@` — see
   section 8].
2. Create the workspace at the anchor; stage the scratch commit (isolated
   mode) or skip it (shared mode, per-repo opt-in `--shared-anchor <repo>`).
3. Set the ticket bookmark; enable watchman fsmonitor with the
   snapshot trigger.
4. Optional per-repo bootstrap (dependency install) controlled by a registry
   key — v1's unconditional depth-3 poetry scan does not scale to N repos.

Then: generate the ticket-root `CLAUDE.md`, merge the container-local claude
config, touch the readiness sentinel, and hold as PID 1. Readiness timeout
scales with the repo count.

`stop`/`start` reuses workspaces in place (v1 warm-restart branch);
`restart` recreates workspaces at the bookmarks (v1 reattach branch), per repo.

## 7. Harness invocation

`cld claude <ticket>` wraps `docker exec -it` and launches the wrapped claude:
`--dangerously-skip-permissions` (the container is the permission boundary),
`--add-dir /opt/cld` (baked skills), `--add-dir` per repo workspace (repo
skills, which claude would otherwise load only once it touches a file in the
repo), cwd = ticket root. Model and prompt are
per-invocation pass-through. Host plugin marketplaces sourced from a directory
or file are mounted RO at their identical host path so Claude's plugin registry
resolves them. Only folders that hold `.claude-plugin/marketplace.json`, lie
outside `~/.claude`, the mailbox root, every repo and the parents of `$HOME`,
and have no symlink in their path are mounted. Marketplaces listed in a repo's
own `.claude/settings.json` are never mounted. Each skipped entry warns.

- POC: if a session is already live in the container, refuse with a clear
  message naming it [ruled: single session for now].
- Resume: because transcripts are slugged per ticket, `cld claude <ticket> --
  --continue` resumes the ticket's latest conversation and `--resume` shows
  only that ticket's sessions. This is the v2 session-persistence story; cld
  itself keeps no session state.
- The in-container `cld` CLI keeps working inside harness sessions (broker,
  messenger, task-agent verbs), now repo-qualified where needed (section 10).

## 8. Anchor model

The v1 contract carries over, restated here once as the authoritative form
(v1 states it in two contradicting places — `cld/vcs/scratch.py` header says
the enforced anchor is A; the entrypoint sets B for isolated mode):

> Per repo, the launch resolves anchor **A** (hash-pinned). Isolated mode
> (default): a scratch commit **B** is created as a child of A, carrying the
> session marker and mode; the ticket may edit only **descendants of B**.
> Shared mode (explicit per-repo opt-in): the ticket may edit descendants of
> A. The anchor itself is never written to. The contract is policy, enforced
> by prompt and convention, not mechanism — unchanged from v1.

**Alternatives researched** (decision 6 asked for this):

| Alternative | Pros | Cons | Verdict |
|---|---|---|---|
| **A. Pinned anchor + scratch commit (v1)** | Proven in all v1 roles; parallel tickets at one base never overlap (distinct B each); anchor+mode recoverable from the store after crash/restart; same contract as headless kinds; overlap check has a concrete revset | Synthetic commit and `.cld-run/` payload ride into every descendant; policy-only | **Chosen** [ruled] |
| B. Bookmark-only (empty child, no committed payload) | Cleaner history | Loses the in-store recovery channel (anchor/mode survive precisely because they are commit data); empty commits get squashed away accidentally; brief delivery needs a new channel | Rejected |
| C. Shared-anchor subtree | Required when a ticket reworks an existing in-flight stack | Siblings overlap trivially | Kept as per-repo opt-in, never default |
| D. No contract (plain workspace, human discipline) | Zero ceremony | The harness runs permission-skipped; the anchor contract is the only boundary against rewriting arbitrary history; parallel tickets lose collision protection | Rejected |

**Default revision** [rec]: registry `default_rev`, falling back to `trunk()`.
v1's `@` default assumed launch from inside the repo; a v2 launch happens from
anywhere, and each repo's `@` is invisible at launch time and may be unrelated
WIP. Stacked tickets (anchoring on another ticket's branch) use an explicit
`repo@rev`. This is a deliberate behavior change from v1.

**Overlap semantics** [ruled: non-blocking among tickets; rec: warn]: when a
new ticket's anchor lies inside another live ticket's editable tree in the
same repo, warn and proceed — strict blocking would forbid legitimate stacked
tickets; silence (v1 masters) hides accidents. Headless kinds (`agent`,
`task-agent`, `run`) keep strict blocking against ticket anchors and each
other. Consequence: v2 must label the **effective** per-repo anchor (B in
isolated mode); v1 labels the base A, which makes the check over-block the
base itself.

## 9. Persistence model

| What | Lives | Survives `stop` | Survives `restart`/recreate | Survives `shutdown` |
|---|---|---|---|---|
| Commits, op log | Host `.jj` store per repo | yes | yes | yes |
| Ticket bookmark per repo | Host `.jj` store | yes | yes | forgotten (commits remain) |
| Uncommitted edits | Watchman-snapshotted into host store | yes | recoverable (workspace rebuilds at bookmark; snapshotted content is in the store) | recoverable via op log |
| Workspace dirs | Container layer | yes | rebuilt | gone |
| Venvs, caches, installed tools | Container layer | yes | **lost** | gone |
| Claude transcripts | Host `~/.claude`, per-ticket slug | yes | yes | yes (resume even after shutdown by recreating the ticket) |
| Container-local claude config (MCP merge) | Container layer | yes | rebuilt from host config | gone |

The one recurring cost is venv/cache loss on every recreate (restart, repo-set
change). Accepted for now; a per-ticket cache volume is a candidate later
improvement, listed in section 12.

## 10. Interfaces that must grow a repo dimension

v1 assumes one repo per container in these places; each becomes repo-qualified
in v2. Product-level requirements only:

- **Broker `run-tests` / `graphql`**: gain an explicit repo target, validated
  against the caller's launch-manifest label (today the broker derives the one
  repo from `org.cld.repo-root` and the actions take no target at all).
- **Secrets**: per-repo `.env` resolution becomes per-repo (keyed by registry
  name), not one fixed path per container.
- **`ignore_gitignore`**: off since the store-only mounts
  (`docs/design-secrets-containment.md`). Each listed file is skipped with a
  warning; no file from the host checkout reaches a workspace.
- **Path translation**: a prefix map (container path → host path per repo)
  replaces the scalar `CLD_HOST_PROJECT_DIR`.
- **Labels**: `org.cld.repo-root` (singular) is replaced by the launch
  manifest; anchor labels become per-repo and record the effective anchor.
- **Messenger**: one mailbox per ticket container, identity
  `t.<slug>`; the host-side `cld msg` identity resolution ("the cwd
  repo's master") needs a ticket-aware replacement.
- **In-container config discovery**: the cwd-walk from the ticket root finds
  no repo config — per-repo lookups become explicit by design.

## 11. Agents and task-agents — direction only

No decisions here [ruled out of scope]; recorded so the later workstream
starts from the product logic:

- The strongest v1 motivation for task-agents — cross-repo work needs a
  container per repo plus messenger relays — disappears: the ticket container
  reads and edits all its repos directly.
- Their remaining product value is **parallelism** (concurrent bounded work
  while the human continues) and **unattended work** (mailbox-driven turns).
- The natural evolution is ticket-scoped: task-agents spawned from a ticket
  container, anchored inside the ticket's trees, reporting to the ticket's
  mailbox. The standing per-repo `agent` role weakens further (memory
  `feedback_prefer_task_agents` already steers away from it).

## 12. Gaps, risks, open issues

1. **Multi-session (post-POC).** Concurrent harness sessions share the same
   per-repo workspaces, hence the same jj working copy `@` per repo — snapshot
   races and mid-edit conflicts are guaranteed without a convention.
   Likely shape: per-session workspaces or per-repo session assignment. The
   POC keeps single-session but must keep workspace naming and the manifest
   schema open for a session dimension.
2. **jj store contention.** Host and container write the same `.jj` store;
   watchman debounce is empirically under ~6 s. v1 docs call N-writer
   contention "the most likely place this POC surprises us"; v2 shifts the
   shape (N repos × 1 container) but does not resolve it.
3. **Carried overlap-check gaps**: git backends skipped entirely, fail-open on
   revset errors, exact-string repo path matching (symlinks bypass),
   running-containers-only, check-then-launch race. Should be fixed while the
   check is touched for per-repo labels; not designed here.
4. **Security posture unchanged**: host `~/.claude` mounted RW (tokens +
   session state writable), no outbound network firewall, harness runs
   permission-skipped. v2 widens per-container blast radius to N repos.
   Explicitly accepted for now; candidates for later hardening.
5. **Boot cost** scales with repo count (workspace add, snapshot, optional
   bootstrap per repo). Readiness timeout and progress output must scale too
   (v1's flat 60 s and silent boot will not).
6. **Interim broker limitation**: until the repo-target extension lands,
   in-container test runs work only for single-repo tickets.
7. **Registry renames** orphan persisted manifests (manifest stores name +
   path; a rename changes the subdir name on next recreate).
8. **Mixed jj/git repo sets** are allowed (per-repo backend detection exists),
   but git repos get weaker guarantees everywhere (no overlap check, worktree
   semantics); state this in user-facing docs.
9. **Docs drift to clean with v2**: README's stale workspace claims
   (`.cld/workspaces/<session>`, `cleanup-workspace.sh` — neither exists), the
   scratch.py-vs-entrypoint anchor contradiction, and the retired
   `design-master-*` documents.

## 13. Decision log

| # | Decision | Ruling | Source |
|---|---|---|---|
| 1 | Fate of v1 roles | Ticket container replaces both `cld master` and bare `cld` | user, 2026-09-16 |
| 2 | Workspace placement | Container-side, as v1 (watchman/jj sync makes content host-visible; current workflow is good) | user, 2026-09-16 |
| 3 | Repo set mutability | Fixed at launch; interactive picker; changes via restart with a new set | user, 2026-09-16 |
| 4 | Repo addressing | Named registry in user config replaces `master_targets` | user, 2026-09-16 |
| 5 | Session model | One claude session per container (POC); multi-session later, must not be precluded; cwd = ticket root | user, 2026-09-16 |
| 6 | Anchor policy | v1 contract per repo, non-blocking among tickets; alternatives researched (section 8) | user, 2026-09-16 |
| 6a | Default anchor rev | Registry `default_rev` → `trunk()` fallback (change from v1's `@`) | architect [rec] |
| 6b | Ticket-vs-ticket overlap | Warn, don't block; headless keep strict blocking | architect [rec] |
| 7 | Launch-time prompt/model | Dropped; both move to `cld claude` invocation | architect [rec] |
| 8 | Repos leaving the set | Torn down as in shutdown; commits survive | architect [rec] |

## 14. Migration from v1

- Shut down all v1 masters, devcontainers and agents before switching
  (bookmark/workspace hygiene in every repo store).
- Seed the registry from existing `master_targets` entries.
- Muscle memory: `cld master` → `cld start` + `cld claude`; in-container
  shell work → `cld shell`; `cld master shutdown` → `cld shutdown`.
- Baked skills and personas that assume the master role (`agent-start`,
  `task-agent-*`, master-relay wording) belong to the deferred agents
  workstream, not this one.
