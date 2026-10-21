# Launching sibling containers from inside master

> **Superseded (2026-09-16) by `docs/design-ticket-containers.md`**: the
> ticket container (v2) replaces the master role; a multi-repo ticket mounts
> its repos directly, with no sibling launches or placeholder directories.
> The mechanics below still describe the shipped v1 behaviour, which keeps
> working during coexistence, except the mounts: no container mounts the host
> checkout read-write any more. Origin holds only the jj/git store
> (`docs/design-secrets-containment.md`).

> **Update (docker socket removed).** This document describes the original
> socket-mediated design, where `cld <cmd>` inside master ran `docker run`
> directly over a mounted `/var/run/docker.sock`. The socket has since been
> **removed from all containers** (it was equivalent to host root). The
> supported in-master launch surface is now narrowed to **`cld agent`** (start
> + restart/shutdown/status/logs) and is **mediated by the host broker**:
> `cld agent` resolves the cwd's target (as below), then delegates to the
> broker's `agent` action, which runs host-side `cld agent` for that repo
> (validated against master's host-set `org.cld.targets` label). Bare `cld`,
> `cld run`, `cld master`, `cld chain`, and interactive attach from inside
> master are **not** supported. See `cld/broker.py`,
> `broker/cld-broker.sh`, and the README "No docker socket in containers"
> section. The cwd→target resolution, `master_targets` placeholders, and
> `cld master repos` below are unchanged; only the launch *mechanism* moved
> from socket to broker.
>
> **Superseding design (not yet implemented):** the cwd→target resolution and
> the placeholder directories are replaced by an explicit `--repo <name|path>`
> in `docs/design-master-target-selection.md`, which also documents where the
> "container-path == host-path" claim below is wrong (placeholders are mirrored
> under the container `$HOME`).

## Goal

`cld <anything>` invoked inside a **master** container behaves identically to
`cld <anything>` on the host, targeting the repo the user `cd`'d into. Every
launch produces a **peer** container (sibling of master), not a nested one.
This covers ephemeral `cld`, `cld agent`, `cld master`, `cld run`, and
`cld chain run`. `restart` / `shutdown` / `status` / `logs` follow the same
rule because they operate on peers through the shared Docker daemon.

## UX invariant

One rule, applied everywhere:

> **`cld <cmd>` inside master == `cld <cmd>` on the host, for the repo at
> `$PWD`.**

Consequences:

- Target selection is **cwd-based only**. No `--repo` flag, no aliasing.
- Enumeration and lifecycle from master go through the broker (`list-containers`
  / `agent` actions), which run `docker`/`cld` on the host's single daemon, so
  `status`, `logs`, `shutdown --all` see every peer regardless of origin --
  same picture as the host, without a socket in the container.
- Messenger MCP already works this way; nothing changes there.
- The `agent-start` skill remains a thin wrapper around `cd <target> && cld agent`.

## Concrete flows

```bash
# Master's own repo (RW-mounted at /workspace/origin; the only bind mount
# of a target repo master ever has)
cd /workspace/current      # or /workspace/origin
cld agent                  # start / attach; idempotent per repo
cld agent restart          # tear down + relaunch peer
cld agent shutdown         # docker rm; peer forgets its own bookmark
cld run @architect         # one-shot detached peer
cld chain run mychain.yaml
cld                        # ephemeral interactive peer

# A named sibling repo (from master_targets)
cd /home/zet/projects/foo  # empty placeholder directory; no mount, ls is empty
cld agent                  # peer agent launched with -v /home/zet/projects/foo
                           #   :/workspace/origin:rw against the host filesystem
```

Discovery of what's reachable from this master:

```bash
cld master repos
# /home/zet/projects/cld   own
# /home/zet/projects/foo   target
# /home/zet/projects/bar   target
```

`cld master repos` reads the master's `master_targets` config plus the
`CLD_HOST_PROJECT_DIR` env var for own repo. No docker-inspect, no jj reads,
no side effects.

## Design rules

1. **One peer-side code path for anchor work.** Both anchor resolution
   (via `jj log -r <hint>` in the peer) and anchor staging
   (`stage_from_env` in `cld/vcs/scratch.py`) run inside the peer
   container's entrypoint, uniformly for host launches, master's own repo,
   and sibling targets. The host only resolves the base revision to a
   commit hash (skipped when running inside master, since master has no jj
   view); everything else happens peer-side.
2. **Master has no filesystem view of any sibling repo, and never writes to
   any target repo.** Sibling repos are not bind-mounted into master at all
   (see "target registration" below). Master's `/workspace/origin` is RW for
   its own workspace's needs but never receives a target-repo write from cld
   during a peer launch or shutdown. The isolation is physical for siblings
   and behavioral for own repo. Any target-repo write is done by the peer.
3. **Target registration is name-only, not mount-based.** `master_targets`
   lists host paths master is allowed to launch peers against. The
   directories exist as empty placeholders inside master (created by the
   entrypoint) so `cd` and cwd-walk work; they contain no repo content.
4. **`cld master repos` is discovery-only.** Reads config, no state, no
   side effects.
5. **Host cld semantics are the reference.** Any drift between host cld and
   cld-inside-master is a bug in the master-side path.

## Delegated anchor work (the crux)

Host cld and master-delegated cld now share a single peer-side staging
pipeline: the host only resolves the base revision, and the peer container
creates the anchor commit `B` inside its own ephemeral workspace at
`/workspace/current`. The origin working copy is never touched -- crucial
for the common jj case where the user's `@` **is** the anchor `A` (an older
design that ran `jj split --onto A` on the host rewrote `A` in that case).

```
host / master:
  target_host_path = resolve_target(cwd)          # config lookup + own repo
  revision_hint    = -r flag / resolved A hash    # peer will resolve if symbolic
  scratch          = {"session": ...}             # in memory
  docker run
      -e AGENT_REVISION_HINT=<revision_hint>
      -e AGENT_SCRATCH=<base64-json>
      -v <target_host_path>:/workspace/origin:rw

peer container entrypoint:
  A = jj log -r "$AGENT_REVISION_HINT" -T commit_id       # in /workspace/origin
  jj workspace add --name "$SESSION_NAME" -r "$A" /workspace/current
  B_HASH = $(cd /workspace/current && python3 -m cld.vcs.scratch)
      # ↳ writes AGENT_SCRATCH files into .cld-run/*, `jj commit -m "cld anchor: $SESSION_NAME" .cld-run`,
      #   prints B (== the just-created scratch commit's id) to stdout.
  AGENT_ANCHOR_HASH = B (isolated, default) or A (shared) -- see PRODUCT_DESIGN.md §8
  jj bookmark set "$SESSION_NAME" -r @
```

Why this works everywhere:

- **Host launch (non-master)**: host resolves `A` to a pinned commit hash
  from its own jj view, passes it as `AGENT_REVISION_HINT`. No writes to
  origin's working copy.
- **Master's own repo**: master has no jj view of the target, so it passes
  the revision hint as an unresolved string; the peer's own
  `/workspace/origin` is RW and its entrypoint resolves + workspace-adds
  locally.
- **Siblings**: identical to master's own repo -- the sibling repo is
  mounted RW into the peer container.
- **`@ == A` case is safe**: `jj workspace add -r <A>` creates a fresh empty
  child of `A` in the secondary workspace; the origin's main workspace `@`
  stays exactly where it was. No rewrite of `A`, no divergence.

## Target registration and discovery

`master_extra_mounts_ro` is **replaced** by `master_targets`. Migration is
a rename plus a semantic shift: the values are still expanded host paths,
but they are no longer bind-mounted into master. They are registered as
launchable targets and materialized as empty placeholder directories.

```toml
master_targets = ["~/projects/foo", "~/work/bar"]
```

- **Master entrypoint** creates each path as an empty directory at boot
  (`mkdir -p`). `~` expands against `CLD_HOST_HOME` so container-path ==
  host-path. Nothing is bind-mounted; the placeholder is purely to make
  `cd <path>` succeed inside master's shell.
- **Cld target resolution inside master** consults the list rather than
  walking for `.jj/`. Cwd (or ancestor) matching a `master_targets` entry
  or matching `CLD_HOST_PROJECT_DIR` (own repo) selects that target's host
  path. Anything else is an explicit error with a hint to add the path to
  `master_targets`.
- **`cld master repos`** (new) reads config, prints `<path>  <role>` per
  line where role is `own` or `target`. Errors when run outside a master
  container.
- **Peer envelope**: launched peer containers gain `AGENT_REVISION_HINT`
  (unresolved) and `AGENT_SCRATCH` (base64 JSON) env vars. No new env vars
  on host cld's launch path.

Losing browsing: master can no longer `ls`, `cat`, or `jj log` inside a
sibling repo (previously an incidental benefit of the RO mount). If that
matters, launch an ephemeral peer (`cld` bare) against the target for a
throwaway inspection shell. This design does not add an optional
browse-only mount; if the need proves real, it becomes a separate
orthogonal config knob and does not alter target registration.

## Shutdown / bookmark cleanup

The peer container is the entity that *creates* the bookmark on launch (its
entrypoint runs `jj bookmark set <session>`). Symmetrically, the peer is
also the entity that *forgets* it on shutdown. Master never writes to the
target's jj store at any point in the shutdown flow -- master's shutdown
code path is just `docker stop && docker rm`, regardless of whether the
target is own repo or a sibling.

Mechanism -- peer self-cleanup on SIGTERM:

```
master:
  docker stop <peer>           # sends SIGTERM to PID 1
  docker rm   <peer>            # after the peer exits

peer container (PID 1 traps SIGTERM):
  jj bookmark forget "$SESSION_NAME"   # writes to /workspace/origin (RW)
  exit 0
```

Where the trap lives per peer type:

- **Agent** (`cld agent`): the supervisor daemon already handles SIGTERM to
  finish the current message. Extend its shutdown to call `jj bookmark
  forget "$SESSION_NAME"` as its last act before exiting.
- **Master** (`cld master`): the entrypoint's PID 1 shell traps SIGTERM,
  runs the forget, exits. The interactive attach sessions are `docker exec`
  processes, unaffected.
- **`cld run`** (`--rm`, one-shot): out of scope for this section. Its
  bookmark is the deliverable and is intentionally retained.

Why this is preferred over master-side forgetting:

- Master's RO view of every target repo is preserved absolutely across all
  code paths (Rule 2). No selective RW mounts, no `docker exec` reach-ins,
  no throwaway sidecar containers.
- One shutdown code path in master -- no branching between own repo and
  sibling. Matches the "one code path" ambition of Rule 1.
- Symmetric with launch: whoever creates the bookmark forgets it.

Failure mode: if the peer dies dirty (SIGKILL, OOM, host reboot before the
trap ran), the bookmark survives. Handled as best-effort with a WARN and
the manual `jj bookmark forget` command in the log, matching the current
`_forget_session_state` fallback wording. A sweeper command (out of
scope) may later reconcile stale bookmarks against `docker ps` output.

Host cld today does the forget directly on its own machine
(`_forget_session_state`). Migrating host cld to the same peer
self-cleanup mechanism is the natural follow-up (drops the helper, removes
a code path). Not required by this design but recommended for consistency
with Rule 5.

## Out of scope

- No bind mounts of sibling repos into master, RO or otherwise. The
  optional browse-only mount is a possible future addition, not part of
  this design.
- No repo aliasing or name-based `--repo` flag.
- No changes to messenger, mailbox, or chain internals.
- No changes to host cld's inline anchor work.
- No support for repos not listed in `master_targets` (the user must
  restart master with the path added — same UX as any config change).

## Consequences worth noting

- Master's own repo and sibling repos are treated identically at every
  step: launch, restart, shutdown, status. No branching by target kind in
  master's code paths.
- If the peer entrypoint fails during staging, the peer container exits
  non-zero. `cld run` (`--rm`) leaves nothing behind; `cld agent` / `cld master`
  leave a stopped container the user can `logs` and remove. No half-staged
  state on any target repo (staging is atomic per `jj op restore`).
- Anchor-related failures now surface through the peer's logs rather than
  synchronously to the caller. Callers wait for the peer's readiness
  sentinel (or `--rm` exit); a staging failure means the sentinel never
  appears and the existing 60 s readiness timeout fires.
