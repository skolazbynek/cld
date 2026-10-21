# The cld broker (`runtests` + host-side actions over SSH)

**Status:** Phases 1–4 implemented (`runtests/`, `broker/`, cld plumbing). The broker now
serves more than test running: `run-tests`, `list-containers`, `agent`, `task-agent`.
Amended 2026-08-18 (§15) to also wire `cld agent` and `cld task-agent`, not just
`cld master`.
**Date:** 2026-07-29
**Related:** Option A (SSH forced command) brainstorm; supersedes the in-container
`.env`-injection approach for the *test-running* use case.

## 0. Naming (one word: **broker**)

The feature had three names -- `host-broker.sh` on the host, `brokerctl.sh` beside it,
`host-run` in the container. It has one now:

| | Name |
|---|---|
| the dispatcher sshd runs as its `ForceCommand` | `broker/cld-broker.sh` |
| the operator's control script for that sshd | `broker/cld-brokerctl.sh` |
| its broker-wide config | `/etc/cld/broker.conf` (`broker/broker.conf.sample`) |
| the in-container client | `cld broker <action> [args…]` (`cld/broker.py`) |
| config keys | `broker_key`, `broker_endpoint`, `broker_known_hosts` |
| container env / mounts | `CLD_BROKER_ENDPOINT`, `/run/secrets/broker-key`, `/run/secrets/broker-known-hosts` |

The client is Python now, not a generated shell wrapper: one implementation of the ssh
call for both `cld broker` and everything that reaches the host through `cld/broker.py`
(container enumeration, the `agent` / `task-agent` lifecycles). The action name is a
positional (`cld broker run-tests -k login`), matching the `action_<name>` functions in
the dispatcher and the `<action> <session> <base64-argv>` wire format; there is no
implicit default action anymore.

### Operator migration

A host set up with the old names needs three edits (nothing else moved -- the wire
format, the action set and the keypair are unchanged):

1. **sshd config:** point `ForceCommand` at the new path, e.g.
   `ForceCommand /path/to/cld/broker/cld-broker.sh` (sample: `broker/sshd_cld_broker.conf`).
2. **broker config:** `mv "$CLD_BROKER_DIR/host-broker.conf" "$CLD_BROKER_DIR/broker.conf"`
   and update `SetEnv CLD_BROKER_CONF=…/broker.conf` in the sshd config.
3. **cld config:** rename `host_broker_key` / `host_broker_endpoint` /
   `host_broker_known_hosts` to `broker_key` / `broker_endpoint` / `broker_known_hosts`
   in `~/.config/cld/config.toml`. The old spellings are a **hard error**, not a
   warning: ignoring them would leave the broker silently off, which breaks every
   task-agent launch.

Then `broker/cld-brokerctl.sh restart`, and `cld master restart` so the container picks
up the new mounts and env.

## 1. Problem

Running a target repo's test suite needs authentication secrets (MySQL / Redis /
etc.) that live in a gitignored `.env` on the host. Two hard requirements:

1. **Secrets must never be readable by `claude`** and the raw `.env` must not be
   mounted into the `cld master` container.
2. **Tests must run against `claude`'s in-progress change** (the master
   container's jj workspace), **without disturbing the host user's current VCS
   state** (`@`). The user keeps absolute control over what change their host
   working copy is on.

The chosen approach keeps secrets entirely on the host: the master container can
only *trigger* a fixed host-side command over SSH (Option A), and that command
launches an **isolated, ephemeral `runtests` container** that materializes the
change under test in its own jj workspace and runs pytest. `claude` sees only the
streamed test output.

## 2. Locked decisions

| # | Decision |
|---|---|
| 1 | The docker socket is **no longer mounted** into any cld container. **Master has no docker socket** — the broker (SSH) is `claude`'s only host channel, so secrets genuinely stay away from `claude`. In-container docker needs (peer enumeration, sibling `cld agent` launches) route through broker actions; see `cld/broker.py` and the `list-containers`/`agent` actions in `broker/cld-broker.sh`. |
| 2 | A **private copy of `.env` is mounted** into the `runtests` container (`claude` cannot reach that container). The broker reads `.env` without following symlinks, writes a 0600 copy in its own stage dir and mounts only that (`docs/design-secrets-containment.md` §6). |
| 3 | `runtests` accepts an **arbitrary `REVISION`**; when run ad-hoc it **defaults to `@`**. In the brokered path the broker supplies the change. |
| 4 | The broker passes **the current change of the `cld master` container's jj workspace**. |
| 5 | The broker is **purely store-reading**: it resolves the master session bookmark's tip from the jj store. It does **not** `docker exec` into master to force a snapshot (accepts minor watchman-snapshot lag). |
| 6 | `runtests` is a **completely separate, self-contained project**: its own directory, minimal Debian base, its own Dockerfile/scripts, **no dependency on the cld app** (no `import cld`, no cld env-var names, not in the cld image hierarchy). Single job: run pytest at a revision. |
| 7 | `runtests` is **startable with arbitrary pytest arguments** (argv passthrough). |
| 8 | Name of the container/image/project: **`runtests`**. |

## 3. Key insight

Docker is **not** what protects the host's VCS state — **jj's multi-workspace
model is**. `jj workspace add --name <ws> -r <rev> <dir>` creates an *independent*
working copy (its own `@`, its own directory) sharing only `.jj/repo/store`. It
**never moves the default workspace's `@`, bookmarks, or working copy** — the same
guarantee `cld master`/`agent` already rely on.

- **jj secondary workspace** → VCS-view isolation (test the change without touching host `@`).
- **Docker** → environment + secret isolation (right deps/certs, no host pollution, `claude` sees only stdout).

## 4. Architecture

```
┌─ STANDALONE PROJECT: runtests/ ───────┐   ┌─ cld-side glue (host) ───────────────┐
│ own Dockerfile + entrypoint, Debian   │   │ cld-broker.sh  (SSH ForceCommand)    │
│ jj + python + poetry + CA certs       │◀──│ resolves master session → REVISION   │
│ single job: pytest @ a revision       │   │ (store-reading), then docker run     │
│ zero cld coupling                     │   │ --rm runtests …                      │
└───────────────────────────────────────┘   └──────────────────────────────────────┘
                                                        ▲
                                          ┌─ cld plumbing (in master image) ─┐
                                          │ restricted SSH key + host-gateway │
                                          │ `cld broker` client on PATH       │
                                          └───────────────────────────────────┘
```

Three units, one coupling point (the broker):

- **`runtests`** — generic tool. Knows a store, a revision, a `.env`, pytest args.
- **broker** — the only cld-aware piece; translates "master's current change" → a plain `REVISION`.
- **cld plumbing** — mounts the restricted key, adds host-gateway, installs the `cld broker` client.

## 5. Component A — the `runtests` container (standalone)

Lives in a **separate top-level directory** `runtests/` (self-contained; movable to
its own repo later with no edits):

```
runtests/
  Dockerfile          # FROM debian:stable-slim; install jj, python3, poetry, CA certs
  entrypoint.sh       # the ~25-line single job
  README.md           # standalone usage, no cld references
```

**Image:** minimal Debian + `jj`, `python3`, `poetry`, build deps, and the internal
CA bundle (needed for TLS to the DBs). Nothing from cld.

**Contract (the entire interface):**

| Input | Via | Default | Meaning |
|---|---|---|---|
| jj store | `-v <repo>:/repo` standalone; from the broker, the store-only `--mount` rows at `/repo` (`docs/design-secrets-containment.md` §3.1) | — | repo whose store holds the change |
| `REVISION` | `-e` | `@` | the change to test (any revset) |
| secrets | `-v <.env>:/secrets/.env:ro` standalone; from the broker, a read-only `--mount` of its private copy | — | `.env`, sourced into env |
| `SECRETS_FILE` | `-e` | `/secrets/.env` | override secrets path |
| `PROJECT_SUBDIR` | `-e` | `.` | where `pyproject.toml` lives |
| `POETRY_INSTALL_ARGS` | `-e` | `--all-extras --all-groups` | `poetry install` flags (install test deps however declared) |
| `PYTEST_ADDOPTS` | `-e` | `--tb=short --disable-warnings -q --maxfail=30` | default pytest opts; explicit argv wins |
| `OUTPUT_MAX_BYTES` | `-e` | `65536` | cap on returned output (last N bytes = the summary) |
| venv cache | `-v <dir>:/cache` (rw) | — | from the broker: the repo's own folder `<VENV_CACHE_ROOT>/repos/<name>-<hash>/`, never shared between repos (`runtests/README.md`) |
| pytest args | container **argv** | `(none)` | passed straight to pytest |

Image ships **Poetry 2.x** (reads PEP 621 `[project]` metadata; 1.x does not).

**Standalone usage (no cld involved):**
```
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v /path/to/repo:/repo -v /path/to/repo/.env:/secrets/.env:ro \
  -e REVISION=xyz  runtests:latest  -k login -x tests/unit
```

**`entrypoint.sh` (sketch):**
```bash
#!/usr/bin/env bash
set -euo pipefail
: "${REVISION:=@}"; : "${PROJECT_SUBDIR:=.}"; : "${SECRETS_FILE:=/secrets/.env}"
export HOME="${HOME:-/tmp}"

# jj needs an author identity to create the workspace's working-copy commit.
export JJ_CONFIG=/tmp/jj-config.toml
printf '[user]\nname = "runtests"\nemail = "runtests@localhost"\n' > "$JJ_CONFIG"

cd /repo
ws="runtests-${HOSTNAME:-$$}"                                  # unique per container; no SIGPIPE pipe
work="$HOME/rt-workspace"                                      # under $HOME: writable with --user
# --ignore-working-copy: never snapshot /repo's working copy (the host user's)
trap 'jj -R /repo --ignore-working-copy workspace forget "$ws" >/dev/null 2>&1 || true' EXIT

rev=$(jj --ignore-working-copy log --no-graph -n1 -r "$REVISION" -T commit_id)
jj --ignore-working-copy workspace add --name "$ws" -r "$rev" "$work"   # ← never touches host @
cd "$work" && jj new "$rev"                                    # the checkout add refused without a snapshot
cd "$work/$PROJECT_SUBDIR"

set -a; [ -f "$SECRETS_FILE" ] && . "$SECRETS_FILE"; set +a      # secrets → env for pytest
poetry install -q --no-interaction
poetry run pytest "$@"                                          # arbitrary args; exit code preserved
```
`set -e` + no `exec` means a failing pytest exits with pytest's code and still fires
the `EXIT` trap that forgets the workspace.

## 6. Component B — the host broker (cld glue, Option A `ForceCommand`)

Runs on the host as the host user (has secrets, docker, jj). It is **not** pinned
to a repo: it serves any repo that has a running master, resolving the target from
that master container's host-set `org.cld.repo-root` label (Option A of §13). The
scoping boundary is the fixed `ForceCommand` + action set, not a repo pin.

**Broker config (host-side):** `RUNTESTS_IMAGE`, `PATH`, `SSH_AUTH_SOCK` — broker-wide
only, nothing per-repo. `SSH_AUTH_SOCK` is opt-in and only the two launcher actions
export it (`stage_agent_socket`): a container spawned on a master's behalf otherwise
gets no ssh-agent, because sshd builds a fresh environment for the forced command.
It must name a socket that outlives one ssh session, which is also why connection
agent-forwarding cannot serve here — the container outlives the connection that
launched it.

**Dispatch model.** `$SSH_ORIGINAL_COMMAND` is `<action> <session> <base64-argv>`.
The action resolves to a shell function `action_<name>` (adding an action = defining
a function; unknown ⇒ denied). Shared context is prepared once, then the function runs.

**`cld-broker.sh` (sketch):**
```bash
read -r action session payload <<<"$SSH_ORIGINAL_COMMAND"
[[ "$action" =~ ^[a-z][a-z0-9-]*$ ]] || exit 2
fn="action_${action//-/_}"; declare -F "$fn" >/dev/null || exit 2      # unknown action
[[ "$session" =~ ^cld_master_[A-Za-z0-9_-]+$ ]] || exit 2

# Repo from the calling master's label (trusted, host-set) — no whitelist, no caller path.
REPO=$(docker inspect "$session" --format '{{index .Config.Labels "org.cld.repo-root"}}')
[ -n "$REPO" ] && { [ -d "$REPO/.jj" ] || [ -d "$REPO/.git" ]; } || exit 3   # no such master

REV=$(jj -R "$REPO" log --no-graph -n1 -r "$session" -T commit_id) || exit 3  # store-reading only

# Project subdir: <repo>/. by default; overridable by pyproject_dir in <repo>/.cld/config.toml.
PROJECT_SUBDIR=$(cld_conf_get "$REPO/.cld/config.toml" pyproject_dir); : "${PROJECT_SUBDIR:=.}"
SECRETS_ENV_FILE="$REPO/$PROJECT_SUBDIR/.env"     # (now: absolute or '..' PROJECT_SUBDIR refused)

mapfile -d '' args < <(printf %s "$payload" | base64 -d)   # no eval, no host injection
"$fn" "${args[@]}"

# action_run_tests: docker run --rm <store rows of $REPO at /repo> [<private copy of .env>:/secrets/.env,readonly]
#                   -e REVISION=$REV -e PROJECT_SUBDIR=$PROJECT_SUBDIR $RUNTESTS_IMAGE "$@"
```

**Safe arbitrary-args over SSH.** The container-side `cld broker` wrapper NUL-joins and
base64-encodes its argv; the broker decodes into an argv array and never `eval`s it.
Result: arbitrary pytest args are allowed, but they can only ever be argv to pytest —
never a new host command.

## 7. Component C — cld plumbing (in the master image)

**Config (`cld/config.py`):**
```python
broker_key: str = ""                              # host path to dedicated PRIVATE key; empty = off
broker_endpoint: str = "host.docker.internal:2222"
broker_known_hosts: str = ""                      # pinned host key
```

**`build_container_args` (guarded on `broker_key`):**
```
--add-host=host.docker.internal:host-gateway          # Linux, Docker 20.10+
-v <key>:/run/secrets/broker-key:ro
-v <known_hosts>:/run/secrets/broker-known-hosts:ro
-e CLD_BROKER_ENDPOINT=host.docker.internal:2222
```

**`cld broker` wrapper (installed by `container-init.sh` when `CLD_BROKER_ENDPOINT` set):**
```bash
payload=$(printf '%s\0' "$@" | base64 -w0)
exec ssh -i /run/secrets/broker-key \
  -o UserKnownHostsFile=/run/secrets/broker-known-hosts \
  -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes \
  -p "${CLD_BROKER_ENDPOINT##*:}" broker@"${CLD_BROKER_ENDPOINT%%:*}" \
  -- "run-tests $SESSION_NAME $payload"
```

**Host setup (once, pre-launch):** dedicated hardened `sshd` (own port bound to the
docker bridge gateway) with `Match User cld-broker` / `ForceCommand
/opt/cld/cld-broker.sh`, plus the dedicated keypair. `cld` only ships the client
side (key mount + wrapper + image name); the sshd + broker script are host setup.

## 8. End-to-end workflow

```
master> cld broker run-tests -k login -x tests/                       (claude or user)
  → ssh (restricted key) → host sshd → ForceCommand cld-broker.sh
      → validate action=run-tests, session=cld_master_…
      → REV = tip of session bookmark   (store-reading; may lag watchman by seconds)
      → docker run --rm runtests  -e REVISION=REV  store rows  .env copy  -- -k login -x tests/
          → jj workspace add -r REV /work        (host @ untouched)
          → source .env; poetry install; pytest -k login -x tests/
          → stdout/stderr + exit code
  ← streamed back over SSH ← ← ← ← ← ← ← master shows results; secrets never present
```

## 9. Security model & isolation boundary

- **Scoped, pre-configured, not dynamic:** the command lives host-side in the broker
  config + `ForceCommand`. The container can only pass a session id (validated) and
  pytest argv. It cannot change *what* runs or run anything else on the host.
- **Secret isolation depends on the socket being gone** (decision 1). With no socket,
  `cld broker` is `claude`'s only host channel, and it returns only test output. A
  private 0600 copy of `.env` is mounted into the ephemeral `--rm` runner, which `claude`
  cannot reach. The broker reads `.env` and `.cld/config.toml` without following a
  symlink. Copies older than 24 h are swept from the stage dir.
- **Store-only runner mounts:** runner and server containers get the jj/git store at
  `/repo` as the eleven rows of `docs/design-secrets-containment.md` §3.1, never the host
  checkout, with `--cap-drop=ALL` and `no-new-privileges`. The broker refuses a runner or
  server image whose `org.cld.jj-version` label is missing or below 0.38. Every host jj
  call goes through `host_jj`, which first checks the jj ids, `commondir`, `modules/`,
  `git_target` and the host jj version.
- **VCS isolation:** `jj workspace add` guarantees the host user's `@` is never moved.
- **Repo scoping (multi-repo, §13):** the target repo comes from the calling master's
  host-set label, never caller input, so a request can only reach a repo that already
  has a master — not an arbitrary host path.
- **Blast radius:** a leaked key unlocks the defined actions for any repo that has a
  running master. No per-master key isolation (accepted; single-user host).

| Unit | Knows about | Coupling |
|---|---|---|
| `runtests` | store, revision, `.env`, pytest args | **none** (standalone) |
| broker | cld session naming, repo label, cld-config secrets key, image | the single bridge |
| cld plumbing | key/endpoint, `cld broker`, image name | client side only |

## 10. Caveats

1. **Snapshot freshness** — store-reading the workspace tip can be seconds behind
   `claude`'s latest edit (watchman lag). The container client therefore
   snapshots its workspaces right before `run-tests` / `graphql`
   (`cld/broker.py:snapshot_workspaces`); the broker itself still only reads.
2. **Concurrent store access** — master's watchman writes while `runtests` does
   `workspace add`; jj supports concurrent workspaces via its op-log, but this is
   the one behavior to explicitly exercise. Every jj call the runner makes on
   `/repo` passes `--ignore-working-copy`: a plain one snapshotted the host
   user's checkout, racing their own jj commands into divergent changes, and a
   `forget` run from a workspace gone stale mid-run failed and left the
   workspace behind.
3. **Transient store writes** — `workspace add`/`forget` add ephemeral objects
   (GC'd later); they never touch the user's `@` or bookmarks.
4. **Ownership** — the runner runs `--user host-uid:gid` so store writes match host
   ownership; `HOME=/tmp` since that uid may not exist in the minimal image.
5. **CA bundle & poetry layout** — the image must bake the internal CAs;
   `PROJECT_SUBDIR` covers non-root `pyproject.toml`. Monorepos with several
   projects need a small extension.

## 11. Implementation plan

**Phase 1 — `runtests` standalone (no cld). ✅ done.**
- `runtests/Dockerfile` (Debian slim + jj + python3 + poetry + vendored CA certs), `entrypoint.sh`, `README.md`, `build.sh`, `ca-certs/`.
- *Verified:* image builds; against a real jj repo the entrypoint materializes `@` in an isolated `/work`, the origin `@` is unchanged, the mounted `.env` reaches pytest (missing → WARN + fail), pytest's exit code propagates, arbitrary argv (`-k`) passes through, and the workspace is `forget`-ten on exit. (The cross-host bind mount itself is exercised only in real deployment; this sandbox's docker daemon lives in a separate namespace.)
- *Implementation note:* the workspace name is derived from the container hostname, not `tr … | head` — the latter trips `set -o pipefail` via SIGPIPE.

**Phase 2 — host broker + sshd. ✅ done.**
- `broker/cld-broker.sh`, `broker.conf.sample`, `sshd_cld_broker.conf`, `keygen.sh`, `README.md`.
- *Verified (with jj/docker shims):* a valid `run-tests <session> <b64>` resolves the revision via `jj -R <repo> log -r <session>` and builds the correct `docker run` with decoded pytest argv; bad action, path-traversal sessions, and shell-metachar sessions all exit non-zero. `keygen.sh` produces the client/host keys, `restrict`-prefixed authorized_keys, and the known_hosts line.

**Phase 3 — cld plumbing. ✅ done.**
- Config fields `broker_key` / `broker_endpoint` / `broker_known_hosts` (§7), `stage_broker()` wired master-only into `build_container_args`, `cld broker` wrapper in `container-init.sh` (supports `[user@]host:port`), docs (CLAUDE.md, config template, `.claude/skills/cld broker run-tests-tests/`), and `TestStageBroker` unit tests.
- *Verified end-to-end, including from a live master:* `stage_broker` mounts/gateway/endpoint tests pass; the generated `cld broker` wrapper parses endpoints and emits the exact base64(NUL-joined) payload the broker decodes. With the host-side sshd running, `docker exec`'d into a real running `cld_master_<repo>_*` container, `cld broker -q --collect-only` correctly resolved the repo via the container's `org.cld.repo-root` label, the broker ran `runtests` against that session's current change, and pytest collected the suite with exit 0 -- confirming the label-based multi-repo resolution (not just a single hard-pinned repo) works against a real master container, not just a synthetic session.

**Phase 4 — operation UX. ✅ done (operation); init/test still optional.**
- `broker/cld-cld-brokerctl.sh` drives the daemon: `start` / `restart` / `shutdown` / `status` / `logs`. Starts sshd detached (no `-D`), tracks it by PID file, logs to `$CLD_BROKER_DIR/sshd.log`, idempotent `start`, surfaces the `/run/sshd` privsep hint on failure. First-time setup stays manual (`broker-setup-home.md`); day-to-day is seamless.
- *Verified:* control logic (status/shutdown/bad-verb/logs) exercised with a fake pidfile; sshd itself not run in this sandbox (not installed).
- Still optional: `cld broker init|test` to scaffold keys/sshd/authorized_keys and smoke-test end to end.

## 12. Deferred / out of scope

- Docker socket removal from `cld master` (assumed done; tracked elsewhere).
- Materializing tests on the host directly (rejected — Docker gives the right env).
- Short-lived credential minting (Shape 2) — not feasible in this environment.
- Monorepo multi-project test orchestration.

## 13. Multi-repo (any repo, no whitelist)

The broker is not pinned to a repo. Per request it learns the target from the
**calling master container's `org.cld.repo-root` label** — set host-side at launch
(`build_container_args`), so it is trusted, not caller input:

```
REPO = docker inspect <session> --format '{{index .Config.Labels "org.cld.repo-root"}}'
```

`<session>` == the calling container's `--name`, and the regex pins it to
`cld_master_*` or `cld_agent_*` (the latter covers both the standing repo agent and
task-agents; see §14). A session that names no running master/agent/task-agent
resolves to empty → denied. This covers *any* repo, because the label is set at
launch for every one of those roles, so it always exists once the container is up.

**Secrets per repo.** Default `<repo>/.env`; overridable by `pyproject_dir` in the
repo's own `<repo>/.cld/config.toml` (the broker reads that one flat key directly — no
docker label, no cld import). Relative to the repo root; an absolute or `..` value is
refused. Both files are read with no symlink followed below the repo. A missing file is
fine: the runner just runs without it.

**PROJECT_SUBDIR follows `pyproject_dir`.** `pyproject.toml` and `.env` are assumed to
share a directory, named directly by `pyproject_dir` (→ `.` by default, `svc` for a
`svc/pyproject.toml` + `svc/.env` layout). One knob covers both.

**Decisions (this round):** (A) resolve via the container label — no whitelist, no
per-repo broker config; (spoof) **no** guard — one shared key, so any master can
target any *active* master's repo; results stream back but raw secrets do not; bounded
and accepted on a single-user host; (secrets) repo-root `.env` default, per-project cld
config override rather than a label; (subdir) `PROJECT_SUBDIR` = `pyproject_dir` directly,
since `pyproject.toml` and `.env` always share a directory — one dedicated config key.

## 14. Output limiting (context hygiene)

A failing run of a large suite can be tens of thousands of lines; feeding all of it
back would clog the caller's context. Two bounds live in the `runtests` entrypoint,
both overridable:

- **`PYTEST_ADDOPTS` default** `--tb=short --disable-warnings -q --maxfail=30`. Short
  tracebacks, no warning noise, and stop after 30 failures — past ~30 it's almost
  always a structural problem (e.g. DB connectivity) with every test failing, so the
  rest adds nothing. Explicit argv passed to the container still wins (verified: a
  caller `--maxfail=3` overrides the default).
- **`OUTPUT_MAX_BYTES` cap** (default 64 KiB) over the combined install+pytest log:
  the entrypoint buffers to a file and, if it exceeds the cap, emits only the last N
  bytes (pytest's summary sits at the end) with a truncation notice on stderr. pytest's
  exit code is preserved either way.

Rejected for now: structured junit digest and full-log-on-demand (design §5 option 5) —
the flag defaults + cap were judged sufficient. Revisit if digests still get large.

## 15. Amendment (2026-08-18): agent and task-agent access

Originally the broker was wired only into `cld master` (`stage_broker` called only
`if master:`, session regex `^cld_master_…$`). Extended to also cover the standing
repo agent (`cld agent`) and task-agents (`cld task-agent`) at the user's request, so
they can run `cld broker run-tests` too instead of only master.

**What changed:**
- `stage_broker` is now called for `master`, `agent`, and `task_agent` roles in
  `build_container_args` (`cld/docker.py`) — all three get the key/known_hosts mount
  and `CLD_BROKER_ENDPOINT`.
- The dispatcher's session regex (`broker/cld-broker.sh`) now accepts `cld_agent_*`
  in addition to `cld_master_*` — this one prefix covers both the standing repo agent
  and task-agents, since kind is a label (`org.cld.kind`), not part of the name.

**What did not change:** the `agent` / `task-agent` launcher actions (spawning
siblings) stay master-only in practice, unaffected by the regex broadening — they
gate on the `org.cld.targets` label via `validate_target`, and that label is only
ever set on a `cld master` container (from `master_targets`, master-only in
`build_container_args`). An agent or task-agent session always fails
`validate_target` for lack of any registered target. Only `run-tests` and
`list-containers` are actually reachable from those roles — the broadening is scoped
to those two by construction, not by an added check.

**The new authorization boundary is a prompt, not a mechanism.** The broker itself
cannot distinguish an agent invoking `cld broker run-tests` on its own initiative
from one relaying an explicit instruction from its master — both look identical on
the wire (a validated `cld_agent_*` session running the `run-tests` action). The
constraint that agents/task-agents may only invoke it with master's explicit
per-run authorization is instructed in their persona prompts
(`prompts/personas/agent.md`, `prompts/personas/task-agent.md`) and the
`broker-run-tests` / `broker-graphql-server` skills, not enforced by the broker, sshd, or
`build_container_args`. This trades a larger blast radius (every agent and
task-agent container — potentially several per master, per `max_task_agents` —
now holds a broker-key mount, versus one master) for the ability to run tests from
those contexts, accepted on the same single-user-host basis as the rest of §9's
security model (a leaked key already unlocked the broker's actions for any repo
with a running master; it now does the same for any repo with a running master,
agent, or task-agent).

## 16. Amendment (2026-08-26): the graphql action

Adds `graphql`, a broker action running a project's GraphQL server on the host
and executing credentialed queries against it — see
`docs/impl-graphql-broker-plan.md` for the full design and
`docs/graphql-mcp.md` for the container-side interface. Before this, the
`graphql-tester` MCP ran the server as a subprocess *inside* the calling
container, meaning the container held the server's environment directly,
including any DB/API credentials in it. That is no longer true: the server
now runs from the repo's jj/git store (never the checkout), on the exact jj revision the calling
container is on, in its own `graphqlserver` container on the host, sourcing
a private copy of the repo's real `.env`.

**Rulings made for this feature (settled, not reopened by future work
without a fresh discussion):**

- **Revision resolution: `${session}@`, not the bookmark.** The workspace
  *tip* (uncommitted working-copy state), not whatever the session's bookmark
  currently points to — the same fix this amendment's revision-semantics
  change applies everywhere else the broker resolves "what the caller is
  looking at" (`resolve_test_context`, `resolve_graphql_context`). Before,
  `-r "$session"` resolved the bookmark, which can trail an in-progress edit
  by an arbitrary number of commits; a caller testing an uncommitted change
  got tested (or served) something else. Blast radius of getting this wrong:
  every broker action that resolves a revision silently serves stale code
  with no error, the single easiest way for this feature to look like it
  works while actually testing the wrong thing.
- **Slow path only.** Every `start`/`restart` pays a fresh `docker run` +
  `poetry install` — no fast-restart path (e.g. reusing a warm container and
  just re-checking-out the revision) is built. Simplicity over server startup
  latency; revisit only if the slow path proves actually disruptive in
  practice, not preemptively.
- **`set_env` is deleted entirely**, not merely broker-routed. The old MCP's
  `set_env` let a container inject an environment variable into the server
  process; now that the server holds real secrets, that would be a container
  → host credential-adjacent channel with no legitimate use the repo's own
  `.env` doesn't already cover.
- **The role gate is prompt-based only**, exactly like `run-tests` (§15) —
  never mechanical. The broker cannot distinguish an agent invoking `cld
  broker graphql start` on its own initiative from one relaying an explicit
  instruction from its master; the constraint lives in persona/skill text
  (`prompts/personas/agent.md`, `prompts/personas/task-agent.md`, the
  `broker-run-tests` skill), not in `cld-broker.sh`, sshd, or
  `build_container_args`.
- **Queries go through the broker; raw URLs need an allowlist.** `query`/
  `introspect` accept `"local"` (this session's own server), an alias
  configured in the repo's `.env` (`CLD_GRAPHQL_URL_<ALIAS>` +
  `CLD_GRAPHQL_AUTH_<ALIAS>`/`CLD_GRAPHQL_COOKIE_<ALIAS>`, credentials
  attached host-side, never visible to the container), or a raw `http(s)://`
  URL. A raw URL gets no credentials and is denied unless its hostname is in
  the operator's `GRAPHQL_URL_ALLOWLIST` (`broker/broker.conf.sample`) —
  otherwise a container gains the host as an unrestricted SSRF pivot to
  reach internal services it cannot itself route to. Default (unset/empty)
  is no raw URLs at all.

**What did not change:** `graphql` reuses the same session-resolution,
label-based repo-scoping, and base64(NUL-joined)-argv-never-`eval`'d
machinery as every other action (§9); it adds no new trust boundary beyond
what §15 already established for agent/task-agent broker access. Output
(logs, query responses) is masked (`mask_output`, mirroring
`cld/log.py:mask_secrets`) and byte-capped host-side before it reaches the
container, the same principle §6/§9 already apply elsewhere — by the time
output reaches the container, masking it there is too late.
