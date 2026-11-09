# Task-agents from ticket containers — implementation design

> Companion to `docs/design-ticket-containers.md` (ticket containers, the
> registry, the `--repo` selector convention) and `docs/design-task-agents.md`
> (task-agent lifecycle, spawn/reap, messaging). Both are implemented; this
> document closes the gap between them. Code cites are against the working
> copy of 2026-10-07. Three forks were settled with the user before this was
> written, the parser choice after; all are recorded as plain decisions (§6).
> Implemented 2026-10-08; §6 D6-D7 record where the code departs from the
> sketches below.

## 1. The gap, stated precisely

A ticket container cannot spawn, inspect or reap a task-agent today. Two
independent bugs stack in the broker-mediated path
(`cld/cli_container.py` → SSH → `broker/cld-broker.py`):

1. **`LabelCaller.targets()` is empty for a ticket caller.**
   `from_session` (`cld-broker.py:263-272`) only sets `repo_root` for a
   non-ticket kind — a ticket has no single repo-root, it carries
   `org.cld.repo.<name>` labels instead. `targets()` (`:326-328`) returns
   `[self.repo_root, *org.cld.targets]`, both empty for a ticket (the
   `org.cld.targets` label was only ever set by the now-removed master role).
   `validate_target` (`:519-523`) therefore refuses every target for every
   ticket caller, unconditionally.
2. **The container passes a container-internal path anyway.**
   `_dispatch_task_agent_to_broker` (`cli_container.py:108-124`) computes
   `target = str(find_target_repo(cfg))` — a cwd-walk for `.jj`/`.git`
   (`docker.py:253-260`) — and that walk runs *inside the container*, so it
   returns something like `/workspace/<slug>/<name>`. `validate_target`
   compares this against a list of **host** paths. The two could not match
   even if bug 1 were fixed.

Both the broker's own module header (`cld-broker.py:26-31`) and
`find_target_repo`'s docstring (`docker.py:256-259`, "the ticket-aware
resolver... will need it") already flag this as unfinished. This document is
that resolver.

A second, smaller gap compounds it: `status`/`logs`/`shutdown` go through the
exact same `<target> <op>` broker argv and so are *also* gated by
`validate_target` (`action_task_agent`, `cld-broker.py:540-548`) — even
though none of them need a repo at all. A task-agent's identity no longer
carries its repo (`a.<ticket>.<slug>`); ownership for these ops is
`--parent` + the reap-readiness checks already in `cld/cli.py`
(`_assert_reap_ready`, `:417-465`), not a repo match. Fixing bug 1 without
noticing this would just make every read/reap op silently depend on an
irrelevant cwd.

## 2. Goals

- A ticket container can run `cld task-agent start --repo <name> …` for
  **any** repo in the operator's registry (`cld repos add`,
  `docs/design-ticket-containers.md` §5), whether or not that repo is
  mounted into the running ticket.
- `cld task-agent status|logs|shutdown` work from inside a ticket container
  at all (today they don't), independent of cwd or mounted repos.
- A ticket container can discover what's in the registry before spawning,
  not just what it happens to have mounted.
- Existing behavior is untouched: host-side `cld task-agent start` (run
  directly, no broker), `run-tests`/`graphql`'s `--repo` (mounted-only, a
  different authorization question — see §3.1), and everything downstream of
  "a resolved host path" (anchor staging, the capacity cap, labels,
  the entrypoint) — none of it changes.

## 3. Design

### 3.1 Repo selection for `task-agent start`: mount, then registry

**Decision:** reuse the `--repo <name>` spelling ticket containers already
use for `run-tests`/`graphql` (`docs/design-ticket-containers.md` §6.1), but
give it a broader resolution *specifically for `task-agent start`*: try the
caller's own mounted repos first, then fall back to the host registry.

This is a deliberate asymmetry with `run-tests`/`graphql`, not an oversight:
those actions execute *inside* the repo they're given (jj log probes, a
secrets `.env` lookup, an ephemeral test container bind-mounting that host
path) as a proxy for "this ticket is entitled to touch this checkout."
`task-agent start` launches a brand-new, independent container — nothing
about the *caller's* mount state is mechanically required, only whether the
operator has allowed cld to reach that path at all. The registry
(`~/.config/cld/config.toml`'s `[repos.*]`, written by `cld repos add` on the
host) already *is* that allowlist — host-operator-curated, not a per-ticket
secret (`docs/design-ticket-containers.md` §5.2). Letting `task-agent start`
reach any registered repo is strictly narrower than "any path," and exactly
matches the stated workflow: research or confirm something on a repo you
didn't anchor the ticket on.

`Caller.repo()` (`cld-broker.py:282-303`, `:352-356`) keeps its current
mounted-only contract unchanged — `run-tests`/`graphql` must not start
silently reaching unmounted repos. A new, narrower helper covers the spawn
case only:

```python
def resolve_task_agent_repo(caller: Caller, selector: str) -> str:
    """Resolve task-agent start's target: caller's own mount by name, else
    the host registry. Broader than Caller.repo() on purpose (see design doc
    §3.1) -- the new container need not already be mounted anywhere."""
    mounted = caller.mounted_repo(selector)   # None, not a _die(), on a miss
    if mounted is not None:
        return mounted
    registered = _host_registry_repo(selector) if selector else None
    if registered is not None:
        if not _is_repo(registered):
            _die(f"denied: registered repo '{selector}' at '{registered}' is not a repo", 3)
        return registered
    _die(
        f"denied: '{selector or '<empty>'}' is not one of {caller.name}'s mounted repos "
        f"and is not registered on the host either -- `cld repos add {selector or '<name>'} <path>` "
        "on the host first, or pick a mounted name", 3,
    )
```

`Caller` grows one new protocol method, `mounted_repo(selector) -> str |
None`, which is the non-dying half of today's `repo()`:

- `LabelCaller`: ticket kind — same label scan as `repo()` (`:287-296`), but
  returns `None` instead of calling `_die` on "no selector + several
  mounts" or "selector not found." Non-ticket kind — `self.repo_root` when
  `selector` is empty, `None` when a selector is given (a single-repo
  container has nothing to select among; falling through to the registry
  for a named repo is still correct and new for this kind, though spawning
  task-agents from a bare task-agent is not reachable today — agents never
  spawn, `docs/design-task-agents.md` D14).
- `CliCaller`: `self.repo_path` when `selector` is empty, `None` otherwise
  (the human operator already has a shell; `-C <path>` is the host
  equivalent of a registry name, so no fallback is needed here).

`_host_registry_repo(name)` lives beside `cld_conf_get` (`:239-245`) and
reads the **same** file `cld/config.py:_user_config_path()` does
(`Path.home() / ".config/cld/config.toml"` — the broker runs as the
operator's own user, so `Path.home()` agrees with the host `cld`'s view).
**Parser (settled, D6):** `cld_conf_get` is a flat `key = "value"`
regex scan and does not understand `[section]` headers, so it cannot read
`[repos.<name>]` tables as-is. Two options:

| Option | Verdict |
|---|---|
| Extend the line-scanner to track the current `[section]` header, pick `path = "..."` when the section matches `repos.<name>` | Keeps the file's stated "standard library only, no cld import" posture (`cld-broker.py:43-44`) with the same tool it already uses; a few more lines, same mental model as `cld_conf_get`. |
| `import tomllib` (stdlib since 3.11) and parse properly | Correct for every TOML edge case (quoting, inline tables) the regex scanner would mishandle; still satisfies "standard library only" since tomllib ships in the stdlib. Needs the host's `python3` to be ≥3.11 — true for any host running the rest of cld (`pyproject.toml` already requires it), so this is likely free. |

Chosen: `tomllib` — the registry file is hand-edited (§5.2 of the
ticket-containers design), so a hand-rolled line scanner misreading a quoted
path or an inline comment is a worse failure mode than a stdlib parser.

### 3.2 `action_task_agent` rework

Current shape takes `<target> <op> [args...]` and validates `target` for
every op (`cld-broker.py:540-586`). New shape drops the target positional
entirely; `start` carries its own `--repo` selector inside `args` (parsed by
the existing `parse_repo_arg`, `:504-514`, already used by run-tests/graphql
for the identical spelling); the other three ops need no repo at all:

```python
def action_task_agent(caller: Caller, argv: list[str]) -> None:
    if not argv:
        _die("denied: task-agent needs an op", 2)
    op, *args = argv
    if op not in ("start", "status", "logs", "shutdown"):
        _die(f"denied: bad task-agent op '{op}'", 2)
    stage_agent_socket()

    for arg in args:                      # unchanged (--force / --parent denial)
        ...

    if op == "start":
        for arg in args:                  # unchanged (ref-must-be-@ref check)
            ...
        repo_name, args = parse_repo_arg(args)
        repo = resolve_task_agent_repo(caller, repo_name)
        os.chdir(repo)
        _exec(["cld", "task-agent", "start", *args, "--parent", caller.name])
        return

    parent = [] if op == "logs" else ["--parent", caller.name]
    _exec(["cld", "task-agent", op, *args, *parent])
```

`stage_agent_socket()` stays unconditional (today's behavior) since it's a
no-op setup step unrelated to repo resolution — not worth special-casing for
three ops that will never use the socket.

Consequence worth stating: `status`/`logs`/`shutdown` no longer `os.chdir`
anywhere, and no longer depend on cwd at all — which also means they now
*work* from inside a ticket container for the first time (today they are
dead code paths, gated by the same empty `targets()`).

### 3.3 Registry discovery

**Decision (settled with the user):** add a read-only listing rather than
requiring the name be known in advance.

New broker action `list-repos`, parallel to `list-containers`
(`cld-broker.py:460-477`): reads the full `[repos.*]` table via the §3.1
reader and the caller's own mounted names (the label scan already inside
`Caller.repo`/`mounted_repo`), emits `name\tpath\tmounted` lines (`mounted`
= `yes`/`no` for *this* caller). No new authorization surface — it's a
read of the operator's own config, already fully readable by anyone with
broker access (the broker already runs as that operator).

Container-side `cld repos` (`cli_container.py:300-329`) gains a second
block when the broker is reachable:

```
name        origin path                      workspace path                   anchor        mode
lide-api    /workspace/origin/lide-api       /workspace/lide-2600/lide-api    1a2b3c4d5e6f  isolated

Also registered on the host (not mounted here):
fastapi-deps   ~/projects/fastapi-dependencies
```

Falls back to today's manifest-only output when `broker_available()` is
false (same graceful-degradation shape the command already has for the v1
"own repo" case).

### 3.4 Container CLI (`cli_container.py`)

`task_agent_start` (`:182-201`) gains:

```python
repo: str = typer.Option(
    "", "--repo",
    help="Repo to target: a name mounted in this ticket, or anything in "
         "`cld repos` on the host. Omit when this ticket mounts exactly one repo.",
),
```

threaded into `_task_agent_start_argv` (`:127-170`) as a leading `--repo
<name>` (consistent with how `run-tests`/`graphql` already prepend it,
`docs/design-ticket-containers.md` §6.1) when non-empty.

`_dispatch_task_agent_to_broker` (`:108-124`) loses its `target =
str(find_target_repo(cfg))` line entirely — there is no longer a target
positional to compute. The docstring's "spawning happens host-side for the
cwd-selected target repo" (`:112-113`) becomes "...for the repo named by
`--repo`, or the host registry fallback" — cwd stops mattering for every
task-agent op, which also removes a footgun today's broken path silently
had: a user `cd`'d into the wrong mounted repo's subdirectory would (if the
bugs above didn't already block it outright) have silently spawned against
the wrong repo.

### 3.5 What does *not* change

Once `repo` is a resolved host path, the broker's existing `os.chdir(repo)`
+ `_exec(["cld", "task-agent", "start", ...])` pattern is unchanged, and
everything downstream of that `cld task-agent start` host-side invocation
(`cli.py:236-365`) already works on a plain host path regardless of whether
it happens to be bind-mounted into any ticket: `find_target_repo` re-walks
cwd from the directory the broker just `chdir`'d into (a real host
filesystem path, so the walk succeeds normally), `resolve_anchor_checked`,
`assert_task_agent_capacity`, `build_container_args`, the entrypoint's boot
sequence, labels, mailbox mount — none of it is ticket-aware and none of it
needs to become so. The fix is isolated to `broker/cld-broker.py` (repo
resolution, dropping target-gating from three ops) and `cld/cli_container.py`
(pass `--repo` instead of relying on cwd, add discovery). No changes to
`cld/docker.py`, `cld/task_agent.py`, `cld/ticket.py`, `cld/manifest.py`, or
`imgs/claude-devcontainer/entrypoint-claude-devcontainer.sh`.

Cross-repo verification on wrap-up (pushed branch, else self-report,
`docs/design-task-agents.md` §9, D25) already covers "the target repo isn't
the one I'm sitting in" — a registry-only repo is just another instance of
that same case, not a new one. No change needed there either, beyond saying
so plainly in the skill (§4).

## 4. Skills (updated in place)

**Decision (settled with the user):** update the existing shared skills
rather than fork ticket-specific copies. They are baked once into the base
image and loaded identically in every container kind
(`imgs/claude-base/Dockerfile.claude-base:121-129`, via `--add-dir
/opt/cld`), so there is one copy to get right, and v1 master's wording was
already stale (the master role itself is gone from the CLI — `cld master`
no longer exists; only `cld task-agent` and `cld run` remain per
`CLAUDE.md`'s Roles section).

- **`task-agent-start`** (`.claude/skills/task-agent-start/SKILL.md`):
  - §"Prerequisite": "inside a master" → "inside a ticket container."
  - §"Step 1: Resolve the target repo": replace the `cld repos` → `own
    | target` example (the retired `master_targets` model,
    `docs/design-master-sibling-launch.md`) with the §3.3 listing, and
    replace "`cd` to the target" with picking `--repo <name>` directly — no
    `cd` is needed or used anymore. Make the two cases explicit: a name
    already mounted in this ticket, or a registry-only name for the "I need
    to check something on a repo I didn't anticipate" workflow this design
    exists for.
  - §"Step 5: Spawn": add `--repo <name>` to the command template.
  - Everything else (slug, peers, model choice, the cap/anchor refusals,
    wrap-up handoff) is unchanged — none of it was ever master-specific.
- **`task-agent-fleet`**: wording only, "master" → "ticket container" (its
  mechanism — `fleet_digest`/`read_mailbox`/the send gate — is already fully
  generic).
- **`task-agent-wrapup`**: wording only; explicitly note that a registry-only
  target repo takes the same pushed-branch-or-self-report path as any other
  cross-repo deliverable (§3.5) — nothing new to teach, just say it covers
  this case too.
- **`messenger-agents`**: drop `--kind master` from the example (dead), note
  `ticket` as a kind instead (already a valid `org.cld.kind` value; the
  listing's underlying `list_cld_containers`/`cld msg agents` is unaffected
  since mailbox addressing was always container-kind-agnostic).
- **`messenger-send`/`-inbox`/`-read`/`-archive`**: no content depends on
  caller kind; skim-checked, no changes expected. Confirm during
  implementation rather than assume.

## 5. Tests

- `tests/test_broker_script.py` (or wherever the broker's Python is
  unit-tested today — verify the actual file before writing new tests):
  - `resolve_task_agent_repo`: mounted hit; registry hit when unmounted;
    both miss → the combined error naming both facts; registry path that
    doesn't exist on disk → "not a repo."
  - `_host_registry_repo`: round-trip against a real `[repos.<name>]`
    table (quoted paths, a repo with `default_rev`/`bootstrap` present,
    an unknown name → `None`, a malformed file → `None` not a crash).
  - `action_task_agent`: `start` with `--repo <mounted>`; `start` with
    `--repo <registry-only>`; `start` with no `--repo` and exactly one
    mount (today's single-repo convenience, preserved); `start` with no
    `--repo` and several mounts → error listing them; `status`/`logs`/
    `shutdown` succeed with **no** target validation at all, from a ticket
    caller (the regression test for bug 1); `--force`/`--parent` spoofing
    still denied (unchanged behavior, re-assert it survives the rewrite).
  - `list-repos`: full registry returned; `mounted` column correct for a
    ticket caller with a partial mount set; empty registry → empty-but-valid
    output, not an error.
- `tests/test_cli_container.py`: `--repo` threads into the broker argv in
  the right position; `_dispatch_task_agent_to_broker` no longer calls
  `find_target_repo` for any op (grep-level assertion is fine, since the
  whole point is that cwd stops mattering); `repos()` prints the "Also
  registered" block when a fake broker response is injected, and omits it
  when the broker is unavailable (existing fallback path unchanged).
- Manual/e2e (needs a real docker daemon + registry entry, same constraint
  the existing task-agent e2e checklists carry, `docs/impl-task-agents-plan.md`
  Appendix P2 §F): from inside a real ticket container, `cld repos` shows a
  registry-only entry; `cld task-agent start --repo <that-name> …` spawns
  against it; `cld task-agent status`/`shutdown` work without `cd`ing
  anywhere first.

Every check must be shown able to fail (mutate the guarded behavior once
during development) before it counts as coverage — same standard the other
design docs in this repo hold themselves to.

## 6. Decisions log

| # | Decision | Rationale |
|---|---|---|
| D1 | `--repo <name>` on `task-agent start` resolves mount-first, registry-fallback; `run-tests`/`graphql`'s `--repo` stays mounted-only | Spawning a new container doesn't require the target to already be mounted in the caller; running tests/queries inside the caller's own checkout does. One spelling, two call sites, deliberately different breadth (§3.1) — settled with the user over the alternative of a separate `--target-repo` flag or requiring a mount first |
| D2 | A new `list-repos` broker action exposes the full host registry, not just the caller's mounts | The workflow this design targets is "a repo I didn't anticipate needing" — requiring the name be already known defeats that. Settled with the user over the smaller-surface alternative |
| D3 | Existing shared skills are edited in place, not forked per container kind | They are baked once and loaded identically everywhere (`--add-dir /opt/cld`); the "master" wording they still carry is already stale since the master role is gone from the CLI. Settled with the user over maintaining parallel ticket-specific copies |
| D4 | `status`/`logs`/`shutdown` drop target/cwd resolution entirely, relying only on `--parent` + the existing reap-readiness checks | They never needed a repo — a task-agent's identity already encodes it. Fixing D1's bug without this leaves three ops silently dependent on an irrelevant cwd (§1, §3.2) |
| D5 | `Caller.mounted_repo()` is a new, non-dying method; `Caller.repo()`'s contract and call sites (`run-tests`, `graphql`) are untouched | Isolates the broader task-agent lookup from the two actions whose correctness depends on "mounted in me" staying a hard requirement — zero regression risk on the existing, working paths |
| D6 | The registry reader (`host_registry()`) uses stdlib `tomllib` | Settled with the user (§3.1). The hand-edited file needs a real parser |
| D7 | Implementation departures: `mounted_repo(selector)` only answers for a name (a v1 kind and `CliCaller` mount nothing by name, so they return `None`); `start` without `--repo` goes to `Caller.repo("")`, which keeps its single-repo default and its error listing the mounts. `targets()`, `validate_target` and `cld-brokerctl run -t` are removed | One code path for the no-selector case instead of two. The target allowlist had no reader left once `task-agent` lost its positional; the host operator reaches any registered repo with `start --repo <name>` instead of `-t` |

## 7. Staged implementation plan

| # | Workstream | Contents | Size | Depends on |
|---|---|---|---|---|
| W1 | **Broker repo resolution** | `Caller.mounted_repo()` on `LabelCaller`/`CliCaller`; `_host_registry_repo()`; `resolve_task_agent_repo()`; `action_task_agent` rework (§3.2); `list-repos` action | M | — |
| W2 | **Container CLI** | `--repo` on `cli_container.py`'s `task_agent_start`; drop `find_target_repo` from `_dispatch_task_agent_to_broker`; extend `repos()` with the registry block (§3.3, §3.4) | S | W1 (action shape + `list-repos`) |
| W3 | **Skills** | Update `task-agent-start`, `task-agent-fleet`, `task-agent-wrapup`, `messenger-agents` in place (§4) | S | W2 (so the documented commands actually exist) |
| W4 | **Tests + e2e** | Unit coverage for W1/W2 (§5); the manual e2e checklist, run on a host with a docker daemon | M | W1, W2 |

Critical path: W1 → W2 → W3. W4's unit portion can land alongside W1/W2 per
workstream; the e2e checklist is last, same constraint every other
task-agent/ticket e2e check in this repo already carries (no daemon in a dev
container).
