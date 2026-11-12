# Ticket containers (cld v2) — implementation design

> Companion to `PRODUCT_DESIGN.md`, which is authoritative on product behavior
> (spec section numbers below refer to it). This document decides
> implementation only; it never re-opens a spec ruling. Code cites are against
> the working copy of 2026-09-16 (see the v2 implementation inventory in the
> ticket notes). Decisions that need the user's ruling are marked
> **[NEEDS SIGN-OFF]**; everything else is architect-decided with the
> alternative stated inline.

## 1. Module layout

v2 is built **alongside** the v1 `cld master` / bare-devcontainer code (§8);
nothing is removed in this design. New logic goes into new modules so v1
removal later is a deletion, not a disentanglement.

| Module | Status | Contents |
|---|---|---|
| `cld/registry.py` | new | `RepoEntry`, registry load (from `Config`), TOML writes (add/rm), interactive picker, ad-hoc-path resolution (basename → subdir name, collision = error) |
| `cld/manifest.py` | new | `RepoManifestEntry` / `TicketManifest` dataclasses, JSON label codec, `resolve_manifest()` (args + registry → manifest), manifest diff for repo-set change, label readback from `docker inspect` |
| `cld/ticket.py` | new | ticket lifecycle: start/stop/restart/shutdown/status/logs, `cld claude` exec, `cld shell`, single-session refusal, host-side per-repo teardown (`bookmark forget` + `workspace forget` per manifest entry), readiness wait scaled by repo count |
| `cld/cli.py` | changed | registers the 8 lifecycle verbs as root-level `@app.command()`s beside `run`/`build`/`prompts` (`cli.py:130,1095,1139`), each taking `<ticket>` as its **own command's** positional — safe; only the root *group* callback must never take one (the typer hazard, `cli.py:120-123`). `repos` is a new sub-typer (`cli.py:1027-1031`). The root callback stays `invoke_without_command=True` for the v1 bare devcontainer while the two coexist. |
| `cld/cli_container.py` | changed | host-only stubs gain the new verbs (`cli_container.py:42-64` pattern); `repos` (`:353-374`) switches from `MASTER_TARGETS` env to the manifest env; `find_target_repo` callers (`:85,110`) route through the ticket-aware resolver (§6.5) |
| `cld/docker.py` | changed | `ticket_container_name()`, manifest label stamping, per-repo mounts/env in a new `build_ticket_container_args()`, prefix-map path translation (§6.4), overlap-check rework (§7) |
| `cld/config.py` | changed | `[repos.*]` table parsing into `Config.repos: dict[str, RepoEntry]` (`_load_toml` today rejects tables, `config.py:130-155`); template block in `config.default.toml` |
| `imgs/claude-devcontainer/entrypoint-claude-devcontainer.sh` | changed | grows a `TICKET_MODE` branch: the per-repo boot loop (§4). The v1 master/agent/bare branches stay untouched. |
| `imgs/claude-devcontainer/container-init.sh` | changed | `link_workspace_files` parameterized per repo (removed later, §12); ticket CLAUDE.md writer; `HOST_PROJECT_DIR` fix (§9) |
| `cld/vcs/scratch.py` | changed | default-payload mode (no `AGENT_SCRATCH` env, §4.2); docstring fix (§9) |
| `broker/cld-broker.sh`, `cld/broker.py` | changed | repo target parameter + manifest validation (§6.1) |
| `cld/messenger/identity.py` | changed | host-side `resolve_self` replacement (§6.5) |

Why `cld/ticket.py` + thin verbs in `cli.py`, not a `ticket_app` sub-typer:
the spec's daily verbs are `cld start` / `cld claude <ticket>` — root-level by
ruling (§5). A sub-typer (`cld ticket start`) would contradict the ruled
surface; a separate `cli_ticket.py` typer module registered into `app` buys
nothing over plain functions in `ticket.py` called from thin `cli.py`
commands, and the existing split (`docs/design-cli-split.md`) already
establishes "logic module + two CLI front-ends" as the pattern
(`cld/task_agent.py`).

Name check: none of `start claude shell stop restart shutdown status logs
repos` collides with an existing root command; v1 `master`/`agent` sub-apps
keep their own `restart`/`status`/`shutdown` namespaced under themselves.

## 2. Launch manifest

The manifest is the resolved repo set stamped at launch, the single source of
truth for `restart`, `status`, and broker validation (spec §3). It fixes v1's
parameter-dropping restart (`cli.py:422` relaunches with four empty strings).

### 2.1 Schema

One JSON document:

```json
{
  "v": 1,
  "ticket": "lide-2600",
  "repos": [
    {
      "name": "lide-api",
      "path": "/home/zbynek/projects/lide-api",
      "anchor_base": "<full commit hash A>",
      "anchor_mode": "isolated",
      "rev_source": "registry"
    }
  ]
}
```

- `name` — registry name = subdir name under the ticket root (unique by
  construction, spec §4). Ad-hoc paths store the basename here.
- `path` — absolute host path, stored alongside `name` so a registry rename
  only affects the *next* recreate (spec gap 7), never orphans a running
  ticket.
- `anchor_base` — resolved hash of A. The **effective** anchor is not in the
  manifest; see §3.
- `anchor_mode` — `isolated` | `shared`, per repo (`--shared-anchor <repo>`).
- `rev_source` — `arg` | `registry` | `trunk`; provenance for `status` output
  and for deciding what a repo-set-change diff shows. Informational only.
- `v` + a repos-list-of-objects shape keep the schema open for the future
  session dimension (spec gap 1): a later `sessions` key or per-repo
  `sessions` field extends without migration.

Durable identity facts only. Per-launch ephemera (workspace-file lists,
bootstrap flags, secrets paths) are **recomputed** from registry + repo config
on every `docker run` and passed as env (§4.1) — so editing a repo's
`.cld/config.toml` takes effect on the next recreate without a manifest
migration.

### 2.2 Persistence channel: labels

The manifest is persisted as container labels at `docker run`:

- `org.cld.kind=ticket`
- `org.cld.ticket=<slug>`
- `org.cld.session=t.<slug>` (existing key, kept for broker/messenger
  readers)
- `org.cld.manifest=<the JSON above>` — authoritative, read by Python via
  `docker inspect`
- `org.cld.repo.<name>=<host path>` — one flat label per repo, for shell
  readers (the broker script greps labels today, `broker/cld-broker.sh:160-170`;
  requiring jq or a Python helper inside the SSH ForceCommand path adds a
  dependency for no gain)

**Alternative — a manifest file** (host-side `~/.cld/tickets/<slug>/manifest.json`):
rejected as the primary channel. It desynchronizes from the container (file
edited or deleted while the container runs), needs its own GC on shutdown, and
labels already survive `docker stop` — `restart` reads them from the stopped
container before `docker rm`, exactly like the task-agent record readback
pattern (`cld/task_agent.py`). Labels die with `docker rm`, but `restart` is
the only consumer that outlives a container and it reads before removing.

**Alternative — per-repo label families only** (`org.cld.repo.<name>.anchor`,
`.mode`, …): rejected as authoritative form; reassembling a typed manifest
from a flat namespace is more code and loses versioning. The dual encoding
(JSON + per-repo path labels) is written by one function in `manifest.py` so
it cannot drift. **[NEEDS SIGN-OFF]** — the dual encoding is deliberate
redundancy; the alternative is teaching `cld-broker.sh` to parse JSON.

Label size is not a constraint: ~200 bytes/repo, and docker labels carry
kilobytes (v1 already ships whole target lists in `org.cld.targets`,
`docker.py:699-704`).

### 2.3 How restart reads it

`cld restart <ticket>`: inspect the (running or stopped) container →
`TicketManifest.from_labels()` → stop, rm → relaunch from the manifest.
Anchors are **not** re-resolved: `anchor_base` is already a hash, and the
entrypoint's reattach branch anchors on the per-repo bookmark anyway.
`cld start <ticket> <new set>` against an existing ticket diffs the new
resolved manifest against the labeled one (keyed by `name`; a changed `path`
or `anchor_base` for a kept name counts as a change), prints the diff, asks to
confirm, tears down leaving repos (as in shutdown), recreates. The recreated
manifest keeps each kept repo's old `anchor_base`/`anchor_mode`
(`keep_attached_anchors`): the workspace reattaches at its existing bookmark,
so the old anchor is still the reality the overlap check and `cld status` must
see -- a newly requested anchor only becomes real after shutdown + start. A
kept name whose `path` changed points at a different store and does take the
new anchor at once.

### 2.4 Moving the anchor: `cld restart --reanchor`

`cld restart <ticket> --reanchor <repo>@<rev>` (repeatable) moves a repo's
scratch anchor commit onto a new revision instead of leaving it where first
launch put it -- see the anchor-move design doc, sections 3 to 5, for the jj
rebase mechanics, the overlap-check exclusion, the stop/move/rm/relaunch
ordering and its undo. A bare `<repo>` (or `<repo>@`) means `<repo>@@`: the
revision defaults to `@`, the repo's working copy on the host. Refuses on a live claude session. On git
the scratch commit is immutable, so a new empty one is committed on the target host-side (`git commit-tree`),
and `refs/cld/anchor/<id>` moves onto it with a compare-and-swap; it refuses a scratch commit that carries
changes and a ticket with no anchor ref. The session branch follows only when it still points at the old
scratch commit. Work that stays on the old one -- a session branch with work, another branch (`<id>.mr`), a
detached HEAD -- would be resumed outside the anchor tree by the next reattach, where the guard blocks every
write, so `cld restart` works out every such ref from the ticket's worktree registration
(`git worktree list --porcelain`) and every local branch of the ticket (`<id>` and `<id>.*`, no other) holding
a scratch commit of the ticket outside the history of the new one, and prints one
`git rebase --onto <new> <old> [<branch>]` for each, with where to run it (in the ticket worktree for what it
has checked out). A branch that holds another ticket's anchor commit (not one below this ticket's own) is
left alone. In shared mode the old base counts as an old commit too, so work built on it but not through the
scratch commit gets a hint (it was in the shared tree, and is outside it now). The hints are printed even
when the relaunch fails, as the anchor has moved by then. A target that is the old scratch commit or builds on
it is refused: jj moves S into its own descendants by rewriting them, git never rewrites the agent's commits.
The printed undo is a reanchor onto the old base, which commits a new scratch commit again (the same one,
within the same second) and gets its own hints. Unlike jj, the other children of the target are not moved.
The host commands that change refs run with `-c core.hooksPath=/dev/null`, and `commit-tree` with
`commit.gpgsign=false`.

## 3. The effective-anchor problem

**The problem.** In isolated mode the editable boundary is scratch commit B,
created *in-container* after `docker run`
(`entrypoint-claude-devcontainer.sh:107-121`), but labels are immutable
post-create. v1 therefore labels base A (`resolve_anchor_checked` runs before
the container exists), which makes the overlap check over-block the base
itself — the known defect behind the placeholder-commit workaround for
parallel task-agents. Spec §8 requires v2 to record the **effective** per-repo
anchor.

**Decision: derive the effective anchor at check time from the jj store; the
label keeps only base A + mode + session.** **[NEEDS SIGN-OFF]** — this is the
design's biggest open choice and the spec explicitly deferred it.

The derivation is the revset the entrypoint's own recovery already uses
(`_cld_recover_anchor`, `entrypoint-claude-devcontainer.sh:19-34`): given a
live container's `(anchor_base, session, mode)`,

```
effective = heads(<anchor_base>+ & description(glob:'cld anchor: <session> *'))
            if mode == isolated and the revset is non-empty
          = anchor_base   otherwise (shared mode, or B not staged yet)
```

`resolve_anchor_checked` maps each live record through this before building
reach revsets. The same derivation applies uniformly to v1 headless kinds
(they carry `org.cld.session` + `org.cld.anchor` + `org.cld.anchor-mode`
labels already, `docker.py:788-797`), which fixes the same-base sibling
over-block for task-agents too — no placeholder commits needed.

Why this beats the two channels the spec named:

| Option | Verdict |
|---|---|
| **Host-side scratch staging per repo before launch** — host creates B for each repo pre-`docker run`, labels record B | Rejected. `jj workspace add` materializes a full working copy; doing it host-side in a tmpdir per repo per launch costs seconds-to-minutes and gigabytes on large repos, adds a host-side failure/cleanup path, and duplicates the staging code (the container path must survive for restart-recovery anyway). The cheap variant — `jj new <A> --no-edit -m 'cld anchor: …'` host-side, no working copy — would make B an **empty** commit, which spec §8 explicitly rejected (alternative B: "empty commits get squashed away accidentally"); shipping the `.cld-run/` payload without a working copy has no clean jj CLI. Staging stays in-container, unchanged and proven. |
| **Post-boot manifest channel** — entrypoint writes B back to a host-readable file (mailbox mount or `~/.cld/tickets/…`) that the check consults | Rejected. Splits the source of truth in two (labels for launch facts, file for B), needs lifecycle/GC for the file, needs a mount whose only purpose is this write-back, and still has the same boot-race window (B does not exist until staging runs). The jj store **already is** the post-boot channel — B's description is commit data, put there precisely so restarts can recover it. Reading it back host-side adds a channel that already exists instead of a new one. |

**Costs of the chosen option, stated plainly:**

- The overlap check runs one extra `jj log` probe per live container per
  target repo. Bounded by live-container count; the check already shells out
  per candidate (`docker.py:975-988`).
- **Boot-race window:** between `docker run` and in-container staging, the
  derivation finds no B and falls back to A — a second launch in that window
  is checked against A's full reach, i.e. v1's over-blocking behavior,
  transiently. For tickets that is a spurious *warning*; for headless kinds a
  transient over-block. Accepted: strictly no worse than v1, self-heals in
  seconds, and the check-then-launch race is a carried gap regardless (§7).
- The check needs read access to each target repo's store. It has it: the
  check runs host-side and manifest `path` is the host path.
- Git repos have the same scratch commit, found through `refs/cld/anchor/<id>` (isolated: S, shared: S's
  parent), but the overlap check still skips the git backend (no `merge-base` reach model), so no
  effective anchor is derived there. Git stays weaker for that (spec gap 8, narrowed): a git ticket has
  the scratch commit, the guard's file-write and drift checks, `--reanchor` and broker tests on committed
  work; the overlap check, watchman snapshots, the commit immutability layer, git command parsing in the
  guard, task-agents and broker `run`/`chain` are still jj only.

## 4. Boot contract

New `TICKET_MODE=1` branch in the devcontainer entrypoint. The v1
single-repo logic (`entrypoint-claude-devcontainer.sh:59-134`) is extracted
into functions in `vcs-lib.sh` parameterized by
`(origin_dir, workspace_dir, bookmark, base_rev, mode)` — the three-branch
warm-restart / reattach / first-launch logic, anchor recovery, watchman
enablement — and the ticket branch loops them over the manifest. The v1
branches call the same functions with their v1 arguments, so behavior is
shared, not forked.

### 4.1 Wire: what the launcher passes

- `-e TICKET_MODE=1`, `-e SESSION_NAME=t.<slug>`
- `-e CLD_TICKET_MANIFEST=<same JSON as the label>` — the boot loop's input,
  parsed with jq (already a hard dependency of `build_claude_config`,
  `container-init.sh:103-149`; the image ships it)
- ~~`-e CLD_REPO_FILES`~~ — removed with the store-only mounts (§12): no
  host checkout file reaches the container, so `ignore_gitignore` entries
  are only warned about at launch.
- `-e CLD_PATH_MAP=<JSON>` — §6.4.
- One store mount set per repo at `/workspace/origin/<name>`: the eleven
  `--mount` rows of `docs/design-secrets-containment.md` §3.1, never the host
  checkout (§12). It replaced the single mount + `-w` of v1 for this kind. No `-w` on
  `docker run`: the daemon would pre-create the ticket root as root:root
  before the entrypoint can mkdir it as the container user; the post-boot
  `docker exec -w /workspace/<slug>` calls (§7) supply the working directory.
- **Not passed:** `AGENT_SCRATCH`, `AGENT_REVISION_HINT`, `AGENT_MODEL`,
  brief/prompt. Anchors ride in the manifest; model and prompt belong to
  `cld claude` (spec §5); the scratch payload is synthesized in-container
  (§4.2).

### 4.2 Per-repo boot loop (first launch)

For each manifest entry, in manifest order:

1. `cd /workspace/origin/<name>`; detect backend (`cld/vcs/detect.py`).
2. Three-branch workspace logic against `/workspace/<slug>/<name>` with
   bookmark `t.<slug>`: warm restart (workspace dir exists in the
   container layer) / reattach (bookmark exists in the store) / first launch
   (`jj workspace add --name t.<slug> -r <anchor_base> …`).
   On git the same three branches run over a *locked* worktree (`git worktree add --lock --reason t.<slug>`)
   whose branch is `t.<slug>`. Warm restart is the existing worktree directory. Reattach is the anchor ref
   or the branch existing in the store: the worktree is re-added on the branch (or the detached HEAD) of
   this session's own stale registration, else on the branch, else on a new branch at the anchor ref. There is
   no blanket `git worktree prune` -- the store is shared with other tickets whose worktree paths do not
   exist in this container -- only this session's own stale registration is removed, after a detached HEAD
   holding commits nothing else holds is archived (`refs/cld/ended/t.<slug>/<stamp>-detached`, the name
   shutdown uses). When the archive ref cannot be written the boot stops and the registration is kept;
   once it is written, a failed add loses nothing. First launch resolves
   `<anchor_base>` to a commit, adds the worktree and stages S (below). A ticket with a branch but no anchor
   ref (from before the scratch commit) reattaches with the base as anchor and a warning.
3. First launch only: stage scratch B via
   `python3 -m cld.vcs.scratch --default-payload`, a new flag that synthesizes
   `{"session": …}` locally instead of requiring `AGENT_SCRATCH`
   (`stage_from_env`, `scratch.py:103-128`, keeps its env path for v1 kinds).
   v2 tickets have no brief, so the envelope wire is dead weight for them; B's
   shape (payload file + `cld anchor: <session> mode=<mode>` description) is
   unchanged, per the §8 ruling. On git the same module commits S in the worktree (`git commit
   --allow-empty --no-verify`, signing off; a missing name or email falls back to `cld` / `cld@localhost`,
   only when `git config` reads it empty, so a host identity that does not reach the container never
   fails the boot) and pins it with `refs/cld/anchor/<session>` (created only if
   absent). The ref lives outside `refs/heads` on purpose, so no branch list shows it and `git push --all`
   skips it; the session branch moves with the work, so the ref is how a restart finds S. If staging fails
   the worktree and branch are removed, so the next boot is a clean first launch.
4. Set the bookmark on the anchor (static, see the per-repo identities paragraph in PRODUCT_DESIGN.md); enable watchman fsmonitor + snapshot trigger per
   workspace (v1 `:130-134`). Git has neither: the session branch is the worktree branch and moves with
   each commit, and nothing snapshots uncommitted edits.
5. ~~Per-repo workspace-file symlinks~~ — removed with the store-only mounts
   (§12); origin holds no working-tree file to link.
6. Optional bootstrap: registry `bootstrap = true` runs `poetry install` in
   the repo's `pyproject_dir` (from the repo's `.cld/config.toml`, resolved
   host-side and shipped as `CLD_REPO_BOOTSTRAP`). Default off — replaces v1's unconditional
   depth-3 scan (`:162-170`), which does not scale to N repos. Alternative —
   free-form bootstrap command string: rejected for scope; a boolean covers
   the actual use-case and a command string is a later addition, not a
   redesign.

Per-repo boot failure is fatal for the whole boot (exit non-zero, no
sentinel): a ticket with a silently missing repo is worse than a failed
start, and `cld start` prints `docker logs` on readiness timeout so the
failing repo is named.

### 4.3 After the loop

1. **Ticket-root CLAUDE.md** (new; nothing writes one today) generated at
   `/workspace/<slug>/CLAUDE.md`: ticket name, one row per repo (name,
   anchor short-hash, mode, backend), the anchor contract sentence, and a note
   that each repo's own CLAUDE.md governs its subtree. Regenerated on every
   boot — it is derived state, never edited. When a row has the git backend
   the file adds a "Git repos" section (branch not bookmark, `git add` /
   `git commit`, the anchor ref, push `HEAD:t.<ticket>.mr`) and, with the
   broker wired, a note that broker tests see committed work only.
2. `build_claude_config` + `copy_host_configs` as today.
3. Generate the claude wrapper **without** a baked `AGENT_MODEL`
   (v1 `:172-182`); `--dangerously-skip-permissions --add-dir /opt/cld` stay
   in the wrapper (the in-container half), plus one `--add-dir` per repo
   workspace so each repo's `.claude/skills/` loads at startup, model comes per-invocation from
   `cld claude -- --model …`.
   Plugin marketplaces sourced from a host directory or file (host
   `known_marketplaces.json` and `extraKnownMarketplaces` in the host
   `~/.claude/settings.json`) are mounted RO at their identical host path
   (`stage_plugin_marketplaces`), so Claude's plugin registry resolves them
   instead of failing with `cache-miss`. A folder is mounted only if it holds
   `.claude-plugin/marketplace.json`, lies outside `~/.claude`, the mailbox
   root, every repo and the parents of `$HOME`, and has no symlink in its path.
   Entries in a repo's own `.claude/settings.json` are never mounted (§12).
4. Touch `/tmp/cld-ticket-ready`; PID 1 idles (`sleep infinity` + traps).
   **Traps:** TERM → plain `exit 0`. Unlike v1 master
   (`:242-251`), the ticket entrypoint never forgets bookmarks — `docker
   stop` is the *pause* verb (spec §5), so in-container TERM must not tear
   down. All forgetting moves host-side into `cld shutdown` (per-repo
   `jj bookmark forget` + `jj workspace forget` over the manifest, the
   `_forget_session_state` pattern, `cli.py:635-672`). On a git repo the
   same host-side step ends the lifecycle in this order: drop the ticket's
   worktree registration (after archiving a detached HEAD that holds its own
   commits as `refs/cld/ended/<id>/<stamp>-detached`), archive the branch tip as
   `refs/cld/ended/<id>/<stamp>` when it holds work above the scratch commit,
   delete the branch, delete `refs/cld/anchor/<id>` last. `forget_session_state` returns whether every step worked; the shutdown output prints the per-repo "forgotten" line only then (a repo that was not cleaned up gets a notice on stderr and the manual steps in the log, the archive `git update-ref` before `git branch -D`). The anchor ref is the
   lifecycle-alive marker, so a partial failure leaves it and the next start
   reattaches; each failure is a warning naming the manual command. This also removes the
   v1 asymmetry where restart needed a special USR1 signal to *avoid*
   cleanup.

### 4.4 Readiness scaling

Host-side wait (`_wait_for_container_ready`, `cli.py:250-261`) gains a
timeout parameter: `60 + 45 × n_repos` seconds (bootstrap-enabled repos are
the slow case; a flat 60 s already cuts it close for one repo with poetry).
While waiting, `cld start` polls the sentinel and echoes new `docker logs`
lines every 10 s, so an N-repo boot is visibly progressing instead of v1's
silence (spec gap 5).

### 4.5 `cld claude` and the single-session lock

`cld claude <ticket> [-- args…]` (with
`context_settings={"allow_extra_args": True, "ignore_unknown_options": True}`,
the pattern at `cli_container.py:25,328`) execs:

```
docker exec -it -w /workspace/<slug> t.<slug> claude <args…>
```

where `claude` is the in-container wrapper (permission-skip + `--add-dir`
baked). Single-session refusal (spec §7, POC ruling): the wrapper takes an
`flock` on `/tmp/cld-session.lock` (non-blocking; on failure prints who holds
it — the lock file carries pid + start time) for the life of the session;
`cld claude` needs no separate host-side probe because the refusal happens at
exec time with a clear message. Alternative — host-side `pgrep` before exec:
rejected, racy and claude's process name is not stable. Nothing here precludes
multi-session later: the lock is one line to lift, and cwd/transcript slugs
are per-ticket, not per-session.

Resume story is transcript-slug-driven and needs no cld state: per-ticket cwd
gives per-ticket slugs, so `-- --continue` / `-- --resume` scope to the
ticket (spec §7).

## 5. Registry

### 5.1 Reading

`Config` gains `repos: dict[str, RepoEntry]` with
`RepoEntry(path, default_rev="", bootstrap=False)`.
`_load_toml` (`config.py:130-155`) currently returns known scalar/array keys
only and would warn on a `repos` table; it gains explicit handling: `repos`
is accepted as a table-of-tables, each entry validated (path exists is *not*
checked at load — only at launch, so a stale entry breaks `cld start
<ticket> that-repo`, not every cld invocation). Registry names are validated
against the task-slug regex (`_TASK_SLUG_RE`, `docker.py:725`) since they
become subdir and label-key material. `config.default.toml` gains a commented
`[repos.<name>]` template block. No under-`$HOME` constraint (spec §4 drops
it; the check at `docker.py:688-696` was a placeholder artifact and is not
carried into any v2 path).

### 5.2 Writing: tomlkit

`cld repos add <name> <path>` / `cld repos rm <name>` write
`~/.config/cld/config.toml` in place. v1 has no config writer at all (only
the template copy, `config.py:106-112`).

**Decision: use `tomlkit`** for read-modify-write of the user config.
**[NEEDS SIGN-OFF]** — it is a new runtime dependency.

- The user config is a hand-edited file seeded from a heavily commented
  template. Any write path must preserve comments, ordering and unrelated
  keys, or `repos add` destroys the user's own config. tomlkit is the one
  round-trip TOML library; it is pure-Python, stable, and small.
- **Alternative — minimal writer, separate file** (`~/.config/cld/repos.toml`
  regenerated whole, trivial serializer): avoids the dependency and the
  comment problem, but contradicts the spec's ruled location (§4 shows the
  registry in `config.toml`) and splits config across two files.
- **Alternative — text surgery on config.toml** (append/delete `[repos.*]`
  blocks by regex): no dependency, but fragile against user edits inside the
  managed blocks and un-testable in the ways that matter. Rejected.

Writes are load → mutate → atomic replace (`os.replace` via a tempfile in the
same directory). `_load_toml`/tomllib remains the *read* path everywhere —
tomlkit is used only inside `registry.py` writes, so config loading gains no
dependency.

### 5.3 CRUD verbs and picker

- `cld repos` — table: name, path, default_rev, bootstrap, and a liveness
  column (which running tickets mount it — from `docker ps` label filters).
- `cld repos add <name> <path> [--default-rev R] [--bootstrap]`,
  `cld repos rm <name>`. `rm` refuses while a live or stopped ticket's
  manifest references the name+path (manifests store both, so the message can
  say which ticket).
- **Interactive picker** (TTY `cld start <ticket>` with no repos): numbered
  multi-select over the registry via `typer.prompt` ("comma-separated numbers,
  empty to abort"), then a per-selection anchor line offering
  `default_rev`/`trunk()` (git: the remote HEAD branch if it resolves, else
  `HEAD`) with an override prompt. Plain prompts, no new TUI
  dependency (nothing interactive exists in the codebase beyond typer
  prompts; a fuzzy-finder is a later nicety, not a launch requirement).
  Non-TTY with no repos: hard error naming the picker and the positional
  form (spec §5).
- **Zero-arg shortcut** (`cld start` with no ticket and no repos): skips the
  registry and the picker entirely. Resolves the cwd's repo root the same
  way `find_target_repo` does for task-agents, uses it as an ad-hoc path
  spec anchored on `@` (the launch happens from inside the repo, so its
  working copy is the intended base), and names the ticket after the repo
  directory (`repo_root.name`). Re-running it in the same repo hits the
  normal create-or-start path for that ticket name (warm start /
  already-running report), same as typing the ticket name explicitly: the
  spec is passed only when the ticket is absent, so a moved `@` never
  offers a recreate.

## 6. Repo-qualified interfaces (spec §10)

### 6.1 Broker: explicit repo target

Wire: `run-tests` and `graphql` gain a leading `--repo <name>` argument
inside the existing b64 argv (wire format `<action> <session> <b64-argv>`
unchanged, `cld/broker.py:57-85`).

Dispatcher (`broker/cld-broker.sh`): today `$REPO` comes from the caller's
single `org.cld.repo-root` label (`:749-751`). New resolution:

1. Caller has `org.cld.kind=ticket`: `--repo <name>` → `$REPO` from the
   caller's `org.cld.repo.<name>` label (grep, no JSON parsing — the reason
   for the dual encoding in §2.2). Unknown name → error listing the labeled
   names. No `--repo` and exactly one `org.cld.repo.*` label → use it
   (spec gap 6's single-repo interim, made permanent as a convenience).
   No `--repo` and several repos → error.
2. Caller is a v1 kind: `org.cld.repo-root` as today; `--repo` refused.

Validation is label-based like today's `validate_target` (`:160-170`):
labels are host-set and immutable, so a container cannot lie about its repo
set. Everything downstream of `$REPO` — `resolve_test_context`'s
`jj -R "$REPO" … -r "${session}@"` (`:102-106`), per-repo `.env` via
`resolve_secrets_env_file` (`:75-83`), `cld_conf_get` (`:57-61`) — already
keys off `$REPO` and multi-repos cleanly; the ticket owns a
`t.<slug>` workspace in every mounted repo, so `${session}@`
resolves per repo unchanged.

Client (`cld/broker.py`) and in-container CLI: `cld broker run-tests
[--repo NAME] …`, same for the graphql MCP path (`graphql_op`,
`broker.py:175-187`). Ticket containers get the broker key mount (same trust
level as v1 master: the interactive user's own session).

### 6.2 Per-repo secrets

- Per-repo `.env` needs no design: broker-side resolution is already
  per-`$REPO` (§6.1). No `.env` reaches a ticket container: origin holds only
  the store, and `ignore_gitignore` is off (§6.3). The broker reads `.env`
  without following symlinks and mounts only a private copy into its runner
  and server containers (`docs/design-secrets-containment.md` §6).
- (The per-repo MySQL config/mount/wrapper surface once designed here was
  removed entirely — cld carries no MySQL config plumbing anymore.)

### 6.3 `ignore_gitignore`

Off since the store-only mounts (§12). The launcher reads each repo's
`.cld/config.toml` at launch and warns once per listed file:
`ignore_gitignore: '<file>' skipped: the host checkout is no longer mounted`.
Neither `CLD_REPO_FILES` nor the v1 `WORKSPACE_FILES` is passed any more.

### 6.4 Path translation: prefix map

`CLD_PATH_MAP` env: JSON object, container prefix → host prefix, longest
prefix wins:

```json
{"/workspace/origin/lide-api": "/home/zbynek/projects/lide-api",
 "/workspace/<slug>/lide-api": "/home/zbynek/projects/lide-api",
 "/home/claude": "/home/zbynek"}
```

The workspace prefixes map to the origin host path so host-facing messages
that name workspace files resolve to something the user can open (inventory
area 8). `to_host_path` (`docker.py:356-372`) consults the map first and
falls back to the scalar `CLD_HOST_PROJECT_DIR`/`CLD_HOST_HOME` pair, which
v1 kinds keep setting — the ~10 existing call sites work unmodified on both
kinds, and `agent_loop.py:137`'s display keeps working because agents are v1
kinds (spec §11 defers them). `$HOME` mapping stays in the map as one more
prefix. `Config` gains `path_map: dict[str, str]` parsed from the env.

### 6.5 Messenger and in-container target resolution

- **In-container:** already generic — `SESSION_NAME` + `MAILBOX_MOUNT`
  (`identity.py:18-19`) make `t.<slug>` mailboxes work unchanged;
  `ensure_own_mailbox` likewise (`container-init.sh:43-56`).
- **Host-side `resolve_self`** (`identity.py:16-24`, today "the cwd repo's
  master"): new chain — `CLD_TICKET` env / `--ticket` flag → that ticket's
  container name; else if exactly one `t.*` container is running →
  it; else fall back to the v1 master resolution (coexistence); else error
  listing running tickets. A cwd-walk mapping cwd → tickets mounting that
  repo is deliberately not attempted: several tickets can mount one repo, so
  cwd is not an identity.
- **Shortnames:** ticket slugs join repo basenames in
  `mailbox.resolve_recipient` (`mailbox.py:695+`). A slug equal to a repo
  basename is ambiguous; resolution order becomes exact container name →
  ticket slug → repo basename, with an ambiguity error naming both.

### 6.6 In-container `cld repos` and config discovery

`cli_container.py`'s `repos` prints the manifest (name, origin path,
workspace path, anchor, mode) from `CLD_TICKET_MANIFEST`. The cwd-walk
project-config discovery (`config.py:115-127`) finds nothing from the ticket
root **by design** (spec §10): code needing a repo's `.cld/config.toml`
receives the repo name explicitly. Inside the container it cannot read
`/workspace/origin/<name>/.cld/config.toml`: origin holds only the store
(§12). The host resolves what the container needs at launch
(`CLD_REPO_BOOTSTRAP`).

## 7. Overlap check rework

`resolve_anchor_checked` (`docker.py:926-1004`) is reworked in place:

1. **Per-repo:** called once per manifest entry at ticket launch, with that
   repo's host path and revision. Signature gains `caller_kind`.
2. **Effective anchors:** every live record — v1 headless labels *and* v2
   manifests — is mapped through the §3 derivation before reach revsets are
   built. `docker_anchor_list` (`docker.py:888-923`) grows a sibling that
   also expands ticket manifests into per-repo records
   `{name, repo_root, anchor_base, session, mode, kind}`.
3. **Warn-not-block among tickets** (spec §6b): outcome depends on
   (caller_kind, occupant_kind):
   - ticket vs ticket → **warn** and proceed (stacked tickets are
     legitimate; the warning names the occupant and the shared commit).
   - ticket vs headless (`agent`/`task-agent`/`run`) → **block**, both
     directions. Spec rules headless keep strict blocking; a ticket anchoring
     inside a live task-agent's reach is the same silent-rewrite hazard, so
     blocking is symmetric. **[NEEDS SIGN-OFF]** — the spec only rules the
     headless-as-caller direction explicitly.
   - anything vs master/devcontainer → ignore, as today
     (`ANCHOR_BLOCKING_KINDS`, `docker.py:885`; tickets are *not* added to
     it — they occupy trees only at warn strength against other tickets).
4. **Carried gaps fixed while touching it** (spec gap 3): **[NEEDS SIGN-OFF]**
   on this scope cut —
   - *Exact-string repo-root matching* (`docker.py:966-971`): fixed —
     both sides normalized with `Path.resolve()` at record-build time, so
     symlinked paths stop bypassing the check.
   - *Fail-open on revset errors* (`_probe` returns False on failure,
     `docker.py:975-983`): fixed — a failed probe now counts as overlap:
     block for headless callers, warn ("could not verify") for ticket
     callers. A check that silently passes on error is worse than none.
   - *Running-only* (`docker_anchor_list(running_only=True)`): fixed for
     persistent kinds — stopped `ticket`/`agent`/`task-agent` containers
     still own their bookmarks and can be restarted, so they are included
     (at warn strength for stopped tickets); `run` corpses stay excluded
     (`--rm`, they cannot return).
   - *Git backends skipped* (`docker.py:962-964`): **carried.** Fixing it
     means a parallel `merge-base --is-ancestor` reach model over the git
     scratch commit and its anchor ref, which the check does not read yet; git
     repos keep a weaker overlap guarantee (spec gap 8, narrowed: git now has
     the scratch commit, the guard's file and drift checks and `--reanchor`)
     and the user-facing docs say so.
   - *Check-then-launch race*: **carried.** A host-wide lock around
     check+launch is the fix; it is orthogonal to this rework and the §3
     boot-race fallback keeps the window's behavior no worse than v1.

## 8. Coexistence with v1

v2 lands beside `cld master` / bare devcontainer; removal is a later cleanup
(spec §14 covers migration).

- **Names:** `t.<slug>` cannot collide with `cld_master_<repo>_<sha>`
  / `a.<ticket>.<slug>`; slug sanitation lives in `cld/names.py`
  (`docs/design-bookmark-naming.md`).
  Bookmarks/workspaces per repo likewise disjoint by prefix.
- **CLI:** root callback keeps launching the bare devcontainer; the new root
  verbs sit beside it. `cld master` keeps working unchanged. The only shared
  host code paths v2 edits under v1's feet are `to_host_path` (fallback
  preserved, §6.4), `resolve_anchor_checked` (v1 callers pass their kind and
  get strictly better behavior: effective anchors + the gap fixes) and
  `_load_toml` (additive).
- **Overlap across kinds:** one check sees all kinds (§7); a live master
  still never blocks anyone, a live ticket warns other tickets and blocks
  headless launches into its trees.
- **Broker/messenger:** dispatcher branches on caller kind (§6.1);
  `resolve_self` falls back to master resolution (§6.5). Baked skills that
  assume the master role are untouched (deferred with agents, spec §14).
- **Entrypoint:** one script, one new mode flag; v1 branches byte-identical
  except for the extraction into shared functions (covered by the existing
  e2e boot test, `tests/test_agent_e2e.py`).

## 9. Implementation-stage fixups (pre-existing defects, fix while touching)

1. **`scratch.py` docstring contradiction** (spec §8): the module header
   (`scratch.py:10-13,22-23`) claims `AGENT_ANCHOR_HASH` is A even in the
   default case; the entrypoint sets B for isolated mode
   (`entrypoint-claude-devcontainer.sh:111-121`) and the behavior is right.
   Rewrite the docstring to the spec §8 contract wording (isolated → B,
   shared → A). Same fix in `docs/design-anchor-change.md`'s 2026-08-19 note,
   which documents the wrong reading.
2. **Dead `HOST_PROJECT_DIR` branch in `container-init.sh`**
   (`:115-116`): `build_claude_config` reads `HOST_PROJECT_DIR` but the
   launcher sets `CLD_HOST_PROJECT_DIR` (`docker.py:593`), so the per-project
   MCP merge never fires. Fix to read `CLD_HOST_PROJECT_DIR` for v1 kinds;
   ticket containers merge global-scope MCP servers only (there is no single
   host project to merge from — per-repo project-scope merge is a later
   nicety, noted in the code).

## 10. Tests

- `tests/test_registry.py` (new): TOML round-trip preserves comments and
  unrelated keys; add/rm; name validation; picker parsing (prompt mocked).
- `tests/test_manifest.py` (new): schema codec, label round-trip, diff for
  repo-set change, `rev_source` provenance, forward-compat (unknown keys in a
  `v:1` manifest ignored).
- `tests/test_cli.py` / `test_cli_container.py`: new verbs dispatch; the root
  group still takes no positional (regression guard for the typer hazard);
  container stubs exit 2.
- `tests/test_docker.py`: ticket args builder (mounts, labels, env), path-map
  translation incl. longest-prefix and scalar fallback, overlap matrix
  (ticket/ticket warn, ticket/headless block both ways, effective-anchor
  derivation incl. the not-yet-staged fallback, fail-closed probe, symlinked
  path, stopped persistent container).
- `tests/test_vcs_integration.py` / a new `test_ticket_e2e.py`: two-repo
  ticket boots against real temp jj repos — per-repo bookmarks, scratch
  descriptions, warm-start vs reattach vs recreate, shutdown forgets
  everything, restart preserves anchors from labels.
- `tests/test_broker_sh.py`: `--repo` resolution against `org.cld.repo.*`
  labels, single-repo default, unknown-name error, v1 caller unchanged.
- `tests/test_mailbox.py` / `test_messenger_cli.py`: `resolve_self` chain,
  shortname ambiguity error.

Every check must be shown able to fail (mutate the guarded behavior once
during development) before it counts as coverage.

## 11. Staged implementation plan

Workstreams sized S/M/L; each lands green and independently testable.
Dependencies are hard (interface) dependencies; unmarked pairs can proceed in
parallel.

| # | Workstream | Contents | Size | Depends on |
|---|---|---|---|---|
| W1 | **Registry & config** | `[repos.*]` parsing in `config.py`, `RepoEntry`, `cld/registry.py` with tomlkit writes, `cld repos` CRUD verbs, picker, `config.default.toml` template | M | — |
| W2 | **Manifest & launcher** | `cld/manifest.py` (schema, codec, resolve, diff), `docker.py`: `ticket_container_name`, `build_ticket_container_args` (per-repo mounts, labels, `CLD_TICKET_MANIFEST`/`CLD_REPO_FILES`/`CLD_PATH_MAP`; `CLD_REPO_FILES` removed later, §12), per-repo secrets mounts | L | W1 |
| W3 | **Boot loop** | entrypoint `TICKET_MODE` branch, vcs-lib extraction, scratch `--default-payload`, per-repo watchman/symlinks/bootstrap, ticket-root CLAUDE.md, readiness sentinel, §9 fixups | L | W2 (env contract) |
| W4 | **Ticket lifecycle verbs** | `cld/ticket.py` + `cli.py` verbs: start (create/warm/diff-recreate), claude (exec + session lock), shell, stop, restart-from-manifest, shutdown (host-side per-repo forget), status roster/detail, logs; scaled readiness wait | L | W2, W3 |
| W5 | **Overlap rework** | effective-anchor derivation, per-repo check, warn path, caller-kind matrix, gap fixes (paths, fail-closed, stopped containers) | M | W2 (manifest readback); independent of W3/W4 |
| W6 | **Repo-qualified seams** | broker `--repo` (script + client + MCP), `to_host_path` prefix map, messenger `resolve_self` chain + shortname rules, `cli_container.py` repos/stubs | M | W2 (labels/env); independent of W3–W5 |
| W7 | **Docs & migration** | README workspace-claims cleanup, retire `design-master-*` pointers, migration notes (§14: seed registry from `master_targets`, muscle-memory table), user-facing git-guarantees note | S | W1–W6 |

Critical path: W1 → W2 → W3 → W4. W5 and W6 fork off W2 and merge
independently. A usable single-repo ticket exists at the end of W4 even if
W5/W6 lag (single-repo broker fallback works without W6, spec gap 6).

## 12. Implementation deltas

Where the landed code (W1–W6) deviates from or extends this document. Each
verified against the working copy.

- **Repo-name regex duplicated, not imported** (§5.1 said registry names are
  validated against `docker._TASK_SLUG_RE`): `cld/registry.py` defines its own
  identical `_REPO_NAME_RE` because importing it would cycle —
  `cld.config` imports from `cld.registry`, and `cld.docker` imports
  `cld.config`.
- **`CLD_REPO_BOOTSTRAP` env added to the wire** (§4.1 listed only
  `CLD_TICKET_MANIFEST`, `CLD_REPO_FILES`, `CLD_PATH_MAP`): which repos want a
  first-boot `poetry install`, and in which `pyproject_dir`, is resolved
  host-side (`ticket_repo_bootstrap`, `cld/docker.py`) and shipped as
  `<name>=<subdir>;…`.
- **Session flock lives in the entrypoint-generated wrapper, not
  `cld/ticket.py`** (the W4 row filed "claude (exec + session lock)" under
  `cld/ticket.py`): the `TICKET_MODE` branch of the entrypoint writes
  `/tmp/bin/claude` with the flock baked in; `exec_claude` does no locking.
  Extension: host-side `cld status` probes the same lock with a non-blocking
  `flock -n` to report a live session.
- **`resolve_manifest` grew a resolver hook, and its signature carries
  `mode`** (§2 defined `resolve_manifest()` as "args + registry → manifest"
  with no resolver): anchor pinning is injected as a
  `resolver(path, revision, mode)` callable so the launch path can pass the
  overlap-checking `ticket_anchor_resolver` (which needs the per-repo anchor
  mode for the shared-anchor probe) while `cld/manifest.py` stays free of
  `cld.docker` imports.
- **`docker_anchor_list` replaced by `docker_occupant_list`** (§7 said the
  lister "grows a sibling"): the old function is gone; one lister returns the
  unified per-repo record shape `{name, repo_root, anchor_base, session,
  mode, kind}` for both v1 headless labels and expanded ticket manifests.
- **GraphQL stop teardown keys off an `org.cld.gql-repo` label** (§6.1 covered
  only start-side `--repo` resolution): `do_graphql_start` stamps the served
  repo onto the server container, and `do_graphql_stop` forgets the jj
  workspace in *that* repo — a multi-repo ticket can pass a different
  `--repo` to stop than it did to start. Server identity stays per session
  (`cld_gql_<session>`): one server per ticket container at a time.
- **`CLD_TICKET` is the `--ticket` channel** (§6.5 named the env var and the
  flag as alternatives): `cld msg`'s `--ticket` flag is exported as
  `CLD_TICKET` by `cli_msg.py` before dispatch, so `resolve_self` reads one
  channel; the messenger verb modules stay signature-unchanged.
- **Readiness sentinel cleared at boot start** (§4.3 only specified touching
  it after the loop): the container layer survives `docker stop`, so a warm
  start still sees the previous boot's `/tmp/cld-ticket-ready`; `TICKET_MODE`
  removes it before any per-repo work, or the host-side wait would return
  early.
- **Stale /tmp/bin wrapper wiped at boot start** (§4.3 only specified
  generating the claude wrapper): the previous boot's generated wrapper also
  survives `docker stop`, and /tmp/bin is first on PATH, so on a warm start
  `which claude` resolves it instead of the real binary and the regenerated
  wrapper would exec its own path — an infinite self-exec loop that hangs
  `cld claude`. `TICKET_MODE` clears /tmp/bin next to the sentinel; the claude
  wrapper is rewritten after the loop as before.
- **Store-only origin mounts** (CLD-001, `docs/design-secrets-containment.md`;
  replaces the §4.1 `-v <path>:/workspace/origin/<name>` mount): each repo is
  mounted as the eleven `--mount` rows of that doc's §3.1, the jj/git store
  only, with host-trusted settings read-only, and the container gets the label
  `org.cld.origin-mount=store-v1`. A host preflight (`prepare_store_mounts`)
  runs before any host jj. The boot scripts run jj in origin only with
  `--ignore-working-copy`, and the launcher refuses an image without
  `org.cld.boot-api=2`. A warm start of a container without the `store-v1`
  label is refused with a `cld restart <ticket>` hint. `link_workspace_files`,
  `CLD_REPO_FILES` and `WORKSPACE_FILES` are gone; `ignore_gitignore` only
  warns. Repo-settings marketplaces are never mounted.
