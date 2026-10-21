# Manual Test Plan: Task Agents

Prerequisites for every section below: run from inside a `cld master` container, with
the host broker configured.

```bash
[ -x cld broker ] && [ -d /var/cld/mailboxes ] && echo master-ok
```

If this fails, spawn/reap steps won't work but read-only steps (`status`,
`transcript`, `fleet_digest`) still will — note that when you hit the failure.

---

## 1. Start a task agent in the master's own repo

**Steps**

1. `cld master repos` — confirm the master's own repo is listed, tagged `own`.
2. `cd` into that path (should already be cwd inside master).
3. `cld task-agent start <persona> -n smoke-own -p "Add a one-line comment to README.md and report back."`

**Observe / confirm success**

- Command output prints container name, slug, persona, branch, anchor hash, and
  pointer commands (`status`, `logs`, `transcript`).
- `docker ps --filter label=org.cld.kind=task-agent --filter name=smoke-own` shows a
  running container.
- `docker inspect <container>` labels include `org.cld.kind=task-agent`,
  `org.cld.parent-master=<this-master>`, `org.cld.repo-root`, `org.cld.task=smoke-own`.
- `ls /var/cld/mailboxes/<container>/` shows `meta.json` present.
- `cld task-agent status smoke-own` shows `Container: running`, `Phase: kickoff` then
  `idle` within ~60s.
- `jj log` in the repo shows a new deliverable bookmark (default name = slug) at the
  anchor, plus the session workspace bookmark.

**Debug if it fails**

- Spawn times out after 60s: `Error: task-agent '<session>' did not become ready
  within 60 s.` → run `cld task-agent logs smoke-own` (supervisor stderr) and
  `docker logs <container>`.
- `task-agent cap reached for <master>: N/N running [...]` → reap an existing agent
  (`cld task-agent shutdown <name>`) or raise `max_task_agents`/`CLD_MAX_TASK_AGENTS`.
- `Error: the host broker is not configured for this master...` → check
  `broker_key`/`CLD_BROKER_KEY` config; read-only commands still work.

---

## 2. Start a task agent in a different (sibling) repo

**Steps**

1. `cld master repos` — pick a path tagged `target` (a registered `master_targets`
   entry).
2. `cd <that-path>` (an empty placeholder dir inside master — no real repo content
   visible locally).
3. `cld task-agent start <persona> -n smoke-sibling -p "<task text>"`

**Observe / confirm success**

- Same container/label/mailbox checks as §1, but `org.cld.repo-root` points at the
  sibling repo's host path, and the peer container launches with the store-only
  `--mount` rows of `<sibling-host-path>` at `/workspace/origin` and the label
  `org.cld.origin-mount=store-v1` (visible via `docker inspect`). See §10.
- Because the master has no filesystem view of the sibling repo, you cannot `jj log`
  locally for this one — verification of committed work must wait for wrap-up (§4).

**Debug if it fails**

- `Error: anchor revision resolves inside a live agent's stack` → another live
  task-agent (or the master) already owns that anchor; reap it first
  (task-agent handoffs are strictly reap-then-spawn).
- Broker-side failures (wrong target, SSH key/known_hosts issues reaching the sibling
  host) surface as broker errors on stdout — cross-check
  `cld broker run-tests -k login -x <target>` manually if the error is opaque.

---

## 3. Set and control peer edge (communication) limits

Peer edges are declared once, at spawn time, on the *later*-spawned side only — the
earlier agent may only reply, regardless of its own `peers` map.

**Steps**

1. Start a first agent: `cld task-agent start <persona> -n peer-a -p "..."`. Note its
   full container name (from the start output).
2. Start a second agent that declares a peer edge with a small budget for easy
   testing: `cld task-agent start <persona> -n peer-b -p "..." --peer <peer-a-container>:3`
3. Instruct `peer-b` (via `messenger MCP send()` or `python -m cld.messenger.send`, with
   `--expects-reply` so it gets answers back) to message `peer-a` repeatedly (more than
   3 times) as part of its task, to exhaust the budget. Note that the ask budget
   (`root_ask_limit`, default 3) fires first if `peer-b` keeps *asking* without either
   side answering — that refusal names the ask limit, and unlike the hop refusal the
   edge stays usable for answers.

**Observe / confirm the edge is set correctly**

- `cat /var/cld/mailboxes/peer-b/meta.json` — `peers` map contains
  `{"<peer-a-container>": 3}`.
- `docker inspect <peer-b-container>` — env var `AGENT_PEERS` contains
  `<peer-a-container>:3`.
- `cld task-agent status peer-b` — detail view prints `Peers: <peer-a> (3 hops)`.

**Observe / confirm enforcement while messages flow**

- After each successful send: `cat /var/cld/mailboxes/_edges/peer-a--peer-b.json`
  (endpoints sorted alphabetically) — `count` increments toward `limit: 3`.
- `cld task-agent transcript peer-b` (and `peer-a`) — budgeted messages are annotated
  with a `hops` value; both directions consume the same counter.
- MCP `send()` return value on a successful budgeted send:
  `{"id":..., "to":..., "hops": n, "limit": 3}`.

**Observe / confirm violation (budget exhausted)**

- Once `count == limit`, the next send in either direction: MCP `send()` returns
  `{"error": "..."}` naming the spent edge; no new file appears in the recipient's
  `inbox/` or `archive/`; `_edges/peer-a--peer-b.json` `count` stays frozen at 3.
- The sender's transcript typically shows a follow-up message escalating to its
  *master* (never-budgeted channel) reporting the spent budget — confirm this appears
  rather than a retry.
- Confirm the skill guidance holds: do **not** tell the agent to retry on that edge —
  it mechanically cannot. Recovery is either manual relay by the master, or reap +
  respawn the edge with a larger `:<hops>` value.

**Note on topology vs. budget**

- There is no hard recipient allow-list at the transport layer beyond "both sides
  have `meta.json`" (task-agent to task-agent) → budgeted; the "may only talk to
  declared peers" rule is enforced by *persona instruction*, not code. As an extra
  manual check, try instructing an agent to cold-message a third, undeclared but
  live agent: the send is not rejected by name (assuming a resolvable mailbox and an
  unspent/absent edge counter), confirming this is a soft rule — document this
  observation if testing for topology enforcement expectations.

**Debug if edges misbehave**

- Wrong/missing budget: re-check the `--peer name:hops` syntax at spawn — it can only
  be set at spawn time, not modified on a live agent.
- Edge never increments: confirm both sides actually have `meta.json` (i.e. both are
  task-agents, not a master or standing `cld agent`, which are exempt/unbudgeted).
- Send to a reaped peer: refused via `mailbox_reaped()` — confirm
  `/var/cld/mailboxes/<peer>/` is gone and only `_archive/<peer>/` exists.

---

## 4. Check results / completed output

Task agents deliver via a jj/git bookmark, not a `result.json`/`summary.json` file
(that mechanism belongs to `cld run`/`cld chain`, not task agents).

**Steps**

1. Instruct the agent to wrap up (via messenger `send()`): ask it to squash its work
   into its deliverable branch and report back.
2. For a sibling-repo agent, also instruct it to push:
   `jj git push --bookmark <branch>`.

**Observe / confirm**

- Own repo (mounted, direct): `jj log -r <deliverable-branch>` and
  `jj diff -r <deliverable-branch>` in the master's repo.
- Sibling repo, pushed: check the pushed branch/MR via GitLab (branch list or MR UI),
  since the master has no filesystem view of the sibling.
- Sibling repo, not pushed: only the agent's self-report is available — treat this as
  unverified, not confirmed.
- Cost/message stats: `cat /var/cld/mailboxes/<name>/state.json` —
  `msg_count`, `cost_usd_total`, `phase`.
- Transcript should show the wrap-up report message to the master:
  `cld task-agent transcript <name>`.

**Debug if results are missing/wrong**

- Nothing committed: recall that Watchman auto-commits every turn into the shared jj
  store regardless of squash — recover via `jj log -r 'heads(all())'` in the target
  repo even if the agent never explicitly wrapped up.
- Push failed with `Host key verification failed`: task-agent containers get SSH
  agent forwarding but ship no `known_hosts` — seed via `ssh-keyscan` before the first
  cross-repo push (known issue, not a bug to chase further).
- Claude produced no reply (agent looks stuck): check transcript for the literal
  fallback string `(no reply produced; last text: ...)` — supervisor synthesizes this
  when a turn produces nothing.
- Mid-turn crash: `cld task-agent logs <name>` shows `claude exited <code>` with
  stdout/stderr; the sender still gets a `failed: <error>` reply instead of a hang.

---

## 5. Reap agents (wrap-up / teardown)

**Steps**

1. Confirm the agent is not mid-turn: `cat /var/cld/mailboxes/<name>/state.json` —
   `phase` should be `idle`, not `processing`.
2. Confirm no other live agent depends on it as a peer: check other agents'
   `meta.json` `peers` maps for `<name>`.
3. `cld task-agent shutdown <name>`
   (or `cld task-agent shutdown --all` to reap the whole fleet).

**Observe / confirm clean teardown**

- `docker ps -a` no longer lists the container; `docker inspect <name>` errors with
  "no such object".
- `/var/cld/mailboxes/<name>/` is gone; `/var/cld/mailboxes/_archive/<name>/` now
  holds the same contents (`state.json`, `meta.json`, `outbox.log`, `archive/`).
- `jj bookmark list` — the session bookmark is forgotten; the deliverable branch
  bookmark still exists (deliverable survives, lifecycle pointer does not).
- `cld task-agent status <name>` reports `Container: gone`, `Mailbox: reaped
  (archived)`.
- `cld task-agent transcript <name>` still works after reap (reads the archive) —
  confirm this as part of the test, since it's easy to assume transcripts vanish with
  the container.

**Debug refusals**

- `refusing to reap <name>: it is mid-turn on '<subject>' ... Wait for it to reply, or
  override with --force.` (waits up to 10s automatically) — either wait, or use
  `--force` (host-only; broker refuses `--force` requests originating from inside a
  master, by design).
- `refusing to reap <name>: it is a live peer of <dependent1>, ...` → reap the
  dependents first, or wait for the pending exchange to land.
- `refusing to reap <name>: its parent master is <owner>, not <this-master>.` → a
  master can only reap its own fleet; confirm via `org.cld.parent-master` label or
  `meta.json.parent`.
- Orphaned mailbox with no container (`cld task-agent status` shows `CONTAINER:
  gone`): clear with `cld task-agent shutdown <name>` — this is a stated recovery
  path, worth deliberately triggering (e.g. `docker rm -f` the container out-of-band)
  to confirm the cleanup command still succeeds.

---

## 6. End-to-end regression pass

Run once as a final smoke test after any change to task-agent code:

1. Spawn two agents, one own-repo and one sibling-repo, with a peer edge between them
   at a small hop budget (e.g. 2).
2. Drive them to exchange messages until the edge is exhausted; confirm the block
   behavior from §3.
3. Wrap up both, confirm deliverables per §4 (own-repo via `jj log`, sibling via
   pushed branch).
4. Reap both via `cld task-agent shutdown --all`; confirm both archived cleanly per
   §5.
5. Confirm `cld task-agent status` shows an empty roster and no orphaned mailboxes
   remain: `ls /var/cld/mailboxes/` should show only `_archive/` and `_edges/`.

---

## 7. GraphQL broker (`cld broker graphql`, `docs/impl-graphql-broker-plan.md`)

Prerequisites: the target repo has `graphql_command` set in `.cld/config.toml`
(binding `0.0.0.0`, see `docs/graphql-mcp.md`), and `GRAPHQL_IMAGE` is built
(`graphqlserver/build.sh` or equivalent).

### 7.1 Start / query / restart-after-edit / stop

**Steps**

1. `start_server` (via the `graphql-tester` MCP, or `cld broker graphql start`
   directly). First call pays for `docker run` + `poetry install` — allow up
   to `GRAPHQL_START_TIMEOUT` (default 300s).
2. `server_status` — expect `running`, with `port`/`endpoint`/`revision`
   populated and `revision` matching the calling session's current `@`.
3. `introspect` then `query { __typename }` against `target="local"` —
   expect a schema summary, then a clean `{"data": {"__typename": "..."}}`.
4. Edit a file the server serves, then `restart_server` — expect the new
   `container` id/`revision` in the follow-up `server_status` (a fresh
   `docker run`, ruling B: no fast-restart path).
5. `stop_server` — expect `server_status` to report `not_started` afterward,
   and `docker ps -a` / `jj bookmark list` to show the `cld_gql_<session>`
   container and `gql-<session>` workspace both gone.

**Observe / confirm success**

- `docker inspect cld_gql_<session>` labels include `org.cld.gql-session` and
  `org.cld.gql-repo`.
- `jj bookmark list` in the repo shows a `gql-<session>` workspace bookmark
  while running, forgotten after `stop`.

### 7.1b Signed-in local query

**Steps**

1. Add `CLD_GRAPHQL_AUTH_LOCAL='Bearer <token>'` (quoted; or `CLD_GRAPHQL_COOKIE_LOCAL`)
   to the repo's `.env`.
2. `query` a field that needs a signed-in user against `target="local"`.

**Observe / confirm**

- The query succeeds as that user; without the variable it fails the same
  way an anonymous request does.
- Nothing in the container's environment or the MCP response shows the
  token.

### 7.2 Orphan sweep

**Steps**

1. `start_server`, then kill the owning session container out-of-band
   (`docker rm -f <session-container>`) without going through `stop_server`.
2. From a *different* live session in the same repo, call `server_status` or
   `start_server` (both call `sweep_gql_orphans` first).

**Observe / confirm**

- The orphaned `cld_gql_<old-session>` container and its `gql-<old-session>`
  jj workspace are gone after the sweep-triggering call.
- A session container that merely **stopped** (not removed) does *not* get
  its graphql server reaped — `sweep_gql_orphans` keys off container
  existence, not running state (see the comment at
  `broker/cld-broker.py:sweep_gql_orphans`).

### 7.3 Unknown alias / denied raw URL

**Steps**

1. `query` with `target="nope-not-configured"` — expect a denial naming the
   unknown alias, not a broker crash.
2. `list_endpoints` — confirm it lists only the aliases actually configured
   (`CLD_GRAPHQL_URL_<ALIAS>` in the repo's secrets env file), and returns
   cleanly (empty list, not an error) for a repo with **zero** aliases
   configured — this is M2: `do_graphql_endpoints` must not fail
   the request when nothing matches.
3. `query` with `target="http://some-unlisted-host.example/graphql"` (no
   `GRAPHQL_URL_ALLOWLIST` entry) — expect `denied: host '...' is not in
   GRAPHQL_URL_ALLOWLIST ...`.

### 7.4 Missing `graphql_command`

**Steps**

1. In a repo/`.cld/config.toml` with no `graphql_command` set, call
   `start_server`.

**Observe / confirm**

- Denial names the missing `graphql_command`, not a generic broker/docker
  error.

### 7.5 `check_url_allowlisted` URL-parsing cases (C1)

A userinfo component (`user:pass@host`) ahead of the port-strip let a URL
like `http://allowed.example:80@evil.com/` bypass the allowlist entirely —
`allowed.example` matched, but curl actually connected to `evil.com`. Fixed
by stripping userinfo *before* the port (`broker/cld-broker.py:
check_url_allowlisted`). With `GRAPHQL_URL_ALLOWLIST="allowed.example"`:

| URL | Expected |
|---|---|
| `http://allowed.example/graphql` | allowed |
| `http://evil.com/graphql` | denied (host not in allowlist) |
| `http://evilallowed.example/graphql` | denied (exact match only, no suffix match) |
| `http://allowed.example:80@evil.com/` | denied — resolves to `evil.com`, not `allowed.example` |
| `http://169.254.169.254/latest/meta-data/@allowed.example/` | denied — the `@` is in the *path*, host is still `169.254.169.254` |

Verify directly against the function in isolation (no container needed):

```bash
python3 -c '
import importlib.util, sys
spec = importlib.util.spec_from_file_location("b", "broker/cld-broker.py")
b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
b.CONF["GRAPHQL_URL_ALLOWLIST"] = "allowed.example"
b.check_url_allowlisted(sys.argv[1])' "http://allowed.example:80@evil.com/"
echo "rc=$?"
# expect: denied: host 'evil.com' is not in GRAPHQL_URL_ALLOWLIST ...  /  rc=3
```

### 7.6 git+ssh dependency

**Steps**

1. In a scratch change of a repo, add a dependency pinned to a private GitLab
   repo over ssh (`poetry add git+ssh://git@<gitlab>/<group>/<pkg>.git`).
2. From a container whose `ssh-add -l` lists a key with access, run
   `cld broker run-tests -k <any>`, then `cld broker graphql start`.
3. Run once more from a container started without an agent
   (`ssh_auth_sock = ""` in its cld config).

**Observe / confirm**

- Step 2: both install the dependency; the first run logs `Permanently added
  '<gitlab>'`, and `~/.cld/cache/ssh/known_hosts` holds the host key
  afterwards. A second run adds nothing.
- Step 3: the install fails with `Permission denied (publickey)` -- the run
  never gets an agent its caller lacks.
- Editing that key in `~/.cld/cache/ssh/known_hosts` makes the next run fail
  with `REMOTE HOST IDENTIFICATION HAS CHANGED`.

---

## 8. Ticket container boot (cld v2, `TICKET_MODE`)

Run from the **host** (not inside a container). Prerequisites: image rebuilt
after the entrypoint change (`cld build`), at least two jj test repos
registered (`cld repos add <name> <path>`).

Until the W4 lifecycle verbs land, drive launches directly from the W2/W3
building blocks:

```bash
cld_ticket_run() {  # usage: cld_ticket_run <ticket> <repo> [repo...]
  python3 - "$@" <<'PY'
import subprocess, sys
from cld.config import Config
from cld.docker import build_ticket_container_args
from cld.manifest import resolve_manifest
cfg = Config.from_env()
manifest = resolve_manifest(sys.argv[1], list(sys.argv[2:]), cfg.repos)
args = build_ticket_container_args(manifest, cfg)
subprocess.run(["docker", "run", "-d", *args, "claude-devcontainer:latest"], check=True)
PY
}
```

With W4, the same checks apply behind `cld start` / `cld stop` /
`cld restart` / `cld shutdown`.

### 8.1 Cold start (single repo, isolated)

**Steps**

1. `cld_ticket_run smoke-t1 <repo-a>`
2. Wait for boot: `docker exec cld_ticket_smoke-t1 ls /tmp/cld-ticket-ready`
   (retry until present; boot logs: `docker logs cld_ticket_smoke-t1`).

**Observe / confirm**

- Logs show, in order: `[cld] repo 1/1: <repo-a> (base=<hash12>, mode=isolated)`,
  `[cld] first launch, base=…`, `[cld] anchor=… (mode=isolated, base=…,
  scratch=…)`, `[cld] wrote /workspace/smoke-t1/CLAUDE.md`,
  `[cld] ticket 'smoke-t1' ready (1 repos)`.
- In the repo on the host: `jj bookmark list` shows `cld_ticket_smoke-t1`;
  `jj log -r 'description(glob:"cld anchor: cld_ticket_smoke-t1*")'` shows the
  scratch commit B as a child of the anchor base, description ending
  `mode=isolated`.
- `docker exec cld_ticket_smoke-t1 cat /workspace/smoke-t1/CLAUDE.md` — table
  row per repo (name, anchor, mode, backend) and the anchor-contract sentence.
- `jj log -r 'description(glob:"cld anchor: cld_ticket_smoke-t1*")' -T empty`
  in the repo on the host prints `true`: the ticket's scratch commit carries no
  files (no `AGENT_SCRATCH` env on the container: `docker inspect` shows none).
- Watchman snapshots: edit a file via
  `docker exec cld_ticket_smoke-t1 sh -c 'echo x >> /workspace/smoke-t1/<repo-a>/README.md'`,
  then within ~10 s the host-side `jj log -r cld_ticket_smoke-t1@` (workspace
  working copy) shows the edit.
- Single-session lock: `docker exec -d cld_ticket_smoke-t1 claude -p 'sleep'`
  then a second `docker exec cld_ticket_smoke-t1 claude -p hi` refuses with
  `a claude session is already live` naming pid + start time.

**Debug if it fails**

- No sentinel + exit: `docker logs` names the failing repo — the boot is
  fail-fast per repo by design.
- `Error: repo '<name>': no origin mount` → the manifest and the store `--mount`
  rows disagree; inspect `docker inspect --format '{{json .Mounts}}'`.
- `cld: repo '<name>': <reason> -- cannot mount its store safely` → the host
  preflight refused the repo before any container started; see §10.

### 8.2 Warm start (stop / start)

**Steps**

1. `docker exec cld_ticket_smoke-t1 sh -c 'touch /workspace/smoke-t1/<repo-a>/warm-marker'`
2. `docker stop cld_ticket_smoke-t1` — expect a fast, clean stop (TERM trap
   exits 0; **no** bookmark forgetting: `jj bookmark list` on the host still
   shows `cld_ticket_smoke-t1`).
3. `docker start cld_ticket_smoke-t1`

**Observe / confirm**

- Logs (second boot) show `[cld] warm restart: reusing existing workspace at
  /workspace/smoke-t1/<repo-a>` — not `first launch`, no new scratch commit
  (host `jj log` shows exactly one `cld anchor:` commit for the session).
- `warm-marker` still exists in the workspace (container layer persisted).
- The readiness sentinel `/tmp/cld-ticket-ready` in the container is gone
  right after `docker start` (the entrypoint removes the previous boot's
  stale copy first) and reappears only once the boot loop finishes — so the
  host-side readiness wait cannot return early on a warm start.
- CLAUDE.md was regenerated (mtime changed), same content.

### 8.3 Restart-recreate (rm + rerun) reattaches at bookmarks

**Steps**

1. Make and snapshot an edit (as in 8.1), note `jj log -r cld_ticket_smoke-t1`
   on the host.
2. `docker rm -f cld_ticket_smoke-t1`
3. `cld_ticket_run smoke-t1 <repo-a>` again (same ticket; with W4 this is
   `cld restart`, which reuses the labeled manifest).

**Observe / confirm**

- Logs show `[cld] reattaching workspace 'cld_ticket_smoke-t1'` — not first
  launch; the anchor line is absent but the recovered anchor governs (verify:
  no second `cld anchor:` commit appears).
- The bookmark still points where it did before the rm; the snapshotted edit
  is reachable from it.
- Venvs/caches are gone (expected: container layer died) — a bootstrap repo
  (8.6) reruns poetry install.

### 8.4 Multi-repo ticket

**Steps**

1. `cld shutdown`-equivalent cleanup of 8.1-8.3 first (host-side per repo:
   `jj workspace forget cld_ticket_smoke-t1 && jj bookmark forget
   cld_ticket_smoke-t1`; `docker rm -f cld_ticket_smoke-t1`).
2. `cld_ticket_run smoke-t2 <repo-a> <repo-b>`

**Observe / confirm**

- Progress lines `[cld] repo 1/2: …` and `[cld] repo 2/2: …`; ready line says
  `(2 repos)`.
- Both repos' stores gain a `cld_ticket_smoke-t2` bookmark + workspace; the
  ticket root holds both subdirs; CLAUDE.md lists two rows.
- Per-repo `ignore_gitignore`: give repo-a a `.cld/config.toml` with
  `ignore_gitignore = [".env"]` and a host `.env` before launch. The launch
  warns `ignore_gitignore: '.env' skipped: the host checkout is no longer
  mounted`, and no `.env` appears in either workspace.
- Kill one repo's mount assumption deliberately (register a bogus path and
  launch): the boot exits non-zero naming that repo, no sentinel.

### 8.5 Shared-anchor repo

**Steps**

1. `cld_ticket_run` variant with `anchor_mode="shared"` for one repo (pass
   `shared=["<repo-a>"]` to `resolve_manifest`; with W4:
   `cld start smoke-t3 <repo-a> --shared-anchor <repo-a>`).

**Observe / confirm**

- The anchor log line says `mode=shared` and `anchor=` equals `base=` (A
  itself, not the scratch hash).
- The scratch commit still exists with description `… mode=shared` (recovery
  channel), but pre-existing descendants of A count as editable.
- Recreate (8.3 steps) on the shared repo recovers A, not B: after rerun, the
  boot's recovered anchor equals the original base (check `docker exec …
  cat /workspace/smoke-t3/CLAUDE.md` anchor column).

### 8.6 Bootstrap key

**Steps**

1. Register a python repo with `cld repos add <name> <path> --bootstrap`
   (and, if its pyproject lives in a subdir, `pyproject_dir` in the repo's
   `.cld/config.toml`).
2. Launch a ticket with it.

**Observe / confirm**

- `docker inspect` env contains `CLD_REPO_BOOTSTRAP=<name>=<subdir>`.
- Logs show `[cld] repo '<name>': poetry install in <subdir>` and the boot
  takes correspondingly longer; afterwards
  `docker exec … sh -c 'cd /workspace/<slug>/<name>/<subdir> && poetry env info -p'`
  names a venv.
- A repo **without** the key shows no poetry line at all (v1's unconditional
  depth-3 scan is gone in ticket mode).
- Failure tolerance: point bootstrap at a repo whose `poetry install` fails
  (e.g. unreachable index) — boot logs `[WARN] poetry install failed for
  <name> (continuing)` and still reaches the ready sentinel.

## 9. Venv cache (`scripts/venv-cache.sh`)

Prereq: `runtests` and `graphqlserver` images rebuilt with their `build.sh`,
broker restarted, `VENV_CACHE_ROOT` unset in the broker config (default
`~/.cld/cache`). Use a python repo with a `poetry.lock`. The broker gives each
repo its own folder, `~/.cld/cache/repos/<name>-<hash>/`; below it is called
`<repo cache>`.

### 9.1 Miss, then hit

**Steps**

1. `rm -rf ~/.cld/cache`, then `cld broker run-tests -k <any>` from the master.
2. Run it again.

**Observe / confirm**

- First run output starts with `[venv-cache] miss, building: <key>`;
  `<repo cache>/venvs/<key>` exists afterwards.
- Second run shows `[venv-cache] hit: <key>` and installs only the project
  itself; it is clearly faster than the first.
- `cld broker graphql start` for the same repo also hits its own key (the image
  id differs from `runtests`, so it misses once first).
- No `cp: preserving times` lines anywhere in the output: before the restore
  fix every hit fell back to a full build after printing them.

### 9.1b Host working copy untouched

**Steps**

1. On the host, edit a tracked file in the repo's default workspace without
   running any jj command; note the latest operation with
   `jj -R <repo> --ignore-working-copy op log -n1`.
2. Run `cld broker run-tests -k <any>` from a container, then
   `cld broker graphql start` and `stop`.
3. Edit a file in the container during a run (rewrites the run's base).

**Observe / confirm**

- `jj -R <repo> --ignore-working-copy op log` shows no `snapshot working copy`
  operation from user `runtests` or `graphqlserver`; the host edit is still
  unsnapshotted.
- After each run, `jj workspace list` shows no `runtests-*` workspace, and
  `jj log -r 'heads(all())'` shows no new commit from those users -- also
  for the run whose base was rewritten in step 3.
- After step 3, `jj log` shows the container's change once, not as a
  divergent pair (`??` after the change id): the runner builds on the
  change's current commit, never on the hidden one the broker resolved.

### 9.2 Lock file change

**Steps**

1. Add a dependency in the workspace (`poetry add <pkg>`), then run tests.

**Observe / confirm**

- A new key is built; the old key's dir stays until evicted.
- The poetry download cache is reused: only the new package is downloaded.

### 9.3 Parallel runs

**Steps**

1. With an empty cache, start two `run-tests` at once (two task-agents, or two
   shells).

**Observe / confirm**

- One logs `miss, building`, the other waits and then logs
  `hit after waiting for another build`; both pass.
- No `.tmp-*` dirs remain in `<repo cache>/venvs`.

### 9.4 Disabled / rebuild / eviction

**Steps**

1. Set `VENV_CACHE_ROOT=` in the broker config, run tests.
2. Unset it again; run the image directly with `-v <repo cache>:/cache
   -e VENV_CACHE_IMAGE_ID=$(docker image inspect -f '{{.Id}}' runtests:latest)
   -e VENV_CACHE=rebuild` (see `runtests/README.md` for the rest of the args).
3. Same, with `-e VENV_CACHE_KEEP=1` after changing `poetry.lock`.

**Observe / confirm**

- Step 1: no `/cache` mount in `docker inspect`; logs `/cache not mounted,
  skipping`; tests still pass.
- Step 2: logs `miss, building` for an existing key and replaces it.
- Step 3: only one dir remains under `venvs/`.

### 9.5 Killed build recovers

**Steps**

1. With an empty cache, start `run-tests` and `docker rm -f` the runtests
   container during the `miss, building` phase.
2. Run tests again.

**Observe / confirm**

- The second run builds normally (the lock died with the container) and the
  stale `.tmp-*` dir is gone after it publishes.

## 10. Secrets containment host checks (CLD-001, `docs/design-secrets-containment.md`)

Run on the **host** (WSL2), once per deploy of the store-only mounts, on lide and
diskuze. Do the subsections in order: 10.1 to 10.4 are the deploy itself. Old
containers keep the full read-write checkout until they are recreated.

### 10.1 Host jj version

**Steps**

1. `jj --version`
2. `which -a jj`, with the `PATH` that `broker/broker.conf` sets.

**Observe / confirm**

- The version is 0.38 or newer (0.45.1 matches the images).
- No older `jj` comes earlier on the broker's `PATH`.

**Debug if it fails**

- `cld: repo '<name>': host jj '…' is older than 0.38 -- cannot mount its store
  safely` → upgrade the host jj. The broker refuses the same case with
  `denied: host jj '…' is older than 0.38`.

### 10.2 Deploy order: broker, then images

**Steps**

1. Upgrade cld on the host. Copy the new `broker/cld-broker.py` over the
   installed one (the path the sshd `ForceCommand` names), then
   `cld-brokerctl restart`. Until the runner images are rebuilt in step 3, the
   broker refuses `run-tests` and `graphql start` with exit 3.
2. `cld build`. It builds the base image `claude-base` first, then the derived
   images `claude-devcontainer` and `claude-run`.
3. `cd runtests && ./build.sh`, then `cd graphqlserver && ./build.sh`.

**Observe / confirm**

- `docker image inspect -f '{{index .Config.Labels "org.cld.boot-api"}}'
  claude-base:latest claude-devcontainer:latest claude-run:latest` prints `2`
  three times.
- `docker image inspect -f '{{index .Config.Labels "org.cld.jj-version"}}'
  runtests:latest graphqlserver:latest` prints 0.38 or newer twice.
- `docker run --rm --entrypoint jj runtests:latest --version`, and the same for
  `graphqlserver:latest`, show 0.45.1.
- After the first `run-tests`, `SECRETS_STAGE_DIR` (default
  `~/.cld/broker-secrets`) exists with mode 0700, and each copy in it is 0600.

**Debug if it fails**

- "cld: image '<image>' lacks the label org.cld.boot-api=2 -- rebuild the cld
  images with cld build" → an image is from before the change. Rerun `cld build`.
- `denied: image <image> has no label org.cld.jj-version of 0.38 or newer` →
  rerun that image's `build.sh`.

### 10.3 Prepare each repo's store

**Steps**

1. Start a ticket once on each repo: `cld start <ticket> <repo>`.

**Observe / confirm**

- The launch succeeds. The repo now has `.jj/repo/config-id`,
  `.jj/workspace-config-id`, `<git dir>/hooks` and `<git dir>/commondir`
  (holding `.`).
- Before this, the broker refuses the repo with `denied: <repo> store is not
  prepared -- start a ticket on it once`.

**Debug if it fails**

- `unmigrated jj config <file>: check it, then run jj once on the host` → read
  the file for aliases you did not set, then run any jj command on the host.
- `<git dir>/commondir exists: …` → check what it names, then delete it.
- `<git dir>/modules exists: git submodules are not supported yet` → the repo
  cannot be mounted until submodules are supported.
- `git_target points to …, outside this repo's git dirs` → fix `git_target`.

### 10.4 Restart existing tickets

**Steps**

1. List tickets and their mount label:
   `docker ps -a --filter label=org.cld.kind=ticket --format '{{.Names}} {{.Label "org.cld.origin-mount"}}'`
2. Before the restart, `cld start <old ticket>` on a stopped old ticket.
3. `cld restart <ticket>` for every ticket without `store-v1`. End every old
   task agent, or let it finish.

**Observe / confirm**

- Step 2 is refused: "'<container>' mounts the full host checkout; run
  cld restart <ticket>".
- After the restarts, every ticket shows `store-v1`, and the work at each
  ticket bookmark is intact.
- Container-layer state (installed packages, edits not yet snapshotted) is gone.

### 10.5 Nested read-only mounts on this docker

**Steps**

1. Record `docker version`.
2. In a new ticket container, `cd /workspace/origin/lide`, then run the commands
   below.

**Observe / confirm**

- `ls -A /workspace/origin/lide` shows only `.jj .git`.
  `find / -name .env 2>/dev/null` finds no lide or diskuze file.
- These fail with "Read-only file system":
  `touch .jj/repo/store/git_target`, `echo x > .jj/workspace-config-id`,
  `echo x > .git/hooks/pre-push`, `echo x > .git/commondir`,
  `git -C /workspace/origin/lide config core.sshCommand x`.
- These fail with "Device or resource busy": `mv .jj/repo /tmp/x`,
  `rm .jj/workspace-config-id`, `mv .jj /tmp/y`.
- `docker inspect <ticket> --format '{{json .Mounts}}'` lists eleven rows per jj
  repo, with `RW:false` on `working_copy`, both jj ids, `git_target`, and the
  git dir's `config`, `hooks` and `commondir`.
- `cd /workspace/origin/lide && jj st` fails. On the host,
  `jj -R <lide> --ignore-working-copy op log -n 3` shows no new operation.

**Debug if it fails**

- A write succeeds → this docker engine does not keep nested read-only binds.
  Stop using tickets on it and record the engine (Docker Desktop and the WSL
  engine can differ).

### 10.6 Planted legacy jj config is ignored

**Steps**

1. In the container, write `/workspace/origin/lide/.jj/workspace-config.toml`
   with `[revset-aliases]` `"trunk()" = "@"`.
2. On the host, `jj -R <lide> --ignore-working-copy log -r 'trunk()'`.
3. Delete the file.

**Observe / confirm**

- The output of step 2 is the same as before step 1.

### 10.7 Daemon volume filter sees bind-mount sources

The launcher finds old containers with `docker ps -a --filter volume=<repo>` and
refuses to launch while one exists. This needs the daemon to match bind-mount
sources.

**Steps**

1. `docker run -d --name cld-volfilter-probe -v <lide>:/x alpine sleep 600`
2. `docker ps -a --filter volume=<lide> --format '{{.Names}}'`
3. Repeat step 2 with a trailing slash, and with a symlinked spelling of the
   path if one exists.
4. `cld start volfilter-check lide`
5. `docker rm -f cld-volfilter-probe`, then `cld start volfilter-check lide`
   succeeds. `cld shutdown volfilter-check`.

**Observe / confirm**

- Step 2 lists `cld-volfilter-probe`. Record the result of step 3.
- Step 4 is refused: `container(s) cld-volfilter-probe mount the full checkout
  without the protected store mounts: restart each one …`.

**Debug if it fails**

- Step 2 lists nothing → this daemon does not match bind sources, so old
  containers are not detected. Record `docker version` and treat it as an open
  item.

### 10.8 Broker runs and secrets

**Steps**

1. `cld broker run-tests --repo lide <one test>` from a ticket.
2. `graphql start`, `query` and `endpoints`.
3. `docker inspect` of the running server.
4. On the host, make `api/.env` a symlink, then run `run-tests` again. Restore
   the file.

**Observe / confirm**

- Step 1 passes and sees a `.env` value.
- Step 3: `/secrets/.env` comes from the stage dir, not from the repo. `/repo`
  has the eleven store rows. `CapDrop` is `ALL` and `SecurityOpt` holds
  `no-new-privileges`.
- Step 4 exits 3 with `denied: secrets file <source>: …`. The message names the
  source, not the copy.

### 10.9 Venv cache per repo

**Steps**

1. Run `cld broker run-tests` once on each repo.
2. `ls ~/.cld/cache ~/.cld/cache/repos`

**Observe / confirm**

- The first run on each repo logs `[venv-cache] miss, building: <key>`. Expect
  this one rebuild per repo. The next run hits.
- Each repo has its own folder `repos/<name>-<hash>/`.
- The old folders directly below the cache root (`venvs/`, `poetry/`, `locks/`,
  `ssh/`) are no longer used. Delete them.

### 10.10 Workspace files and marketplaces

**Steps**

1. Set `ignore_gitignore = [".env", ".envrc"]` in a repo's `.cld/config.toml`
   and start a ticket on it.
2. Read the launch log for `plugin marketplace:` lines.

**Observe / confirm**

- Both `ignore_gitignore` entries are skipped with a warning, and nothing
  appears in the workspace.
- One `plugin marketplace: '…' skipped: …` line for each entry from a repo's own
  `.claude/settings.json`, and for each folder without a marker, inside
  `~/.claude`, the mailbox root or a repo, or with a symlink in its path.
- Real marketplaces still load in the ticket.

### 10.11 Task agent and `cld run`

**Steps**

1. Start a task agent and let it push its deliverable branch.
2. Run `cld run` to completion.

**Observe / confirm**

- Both work. `docker inspect` of each shows the `store-v1` label.

### 10.12 Host repo state, once per repo after the restarts

Old full-checkout containers could write all of these before this change.

**Steps**

1. `jj config list --repo` and `jj config list --workspace` in each jj repo.
2. `git -C <repo> config --list --show-origin`
3. `ls <git dir>/hooks`, and list the git dir. Compare it with a fresh `git init`.
4. `jj st` on the host.
5. `jj git push --dry-run` and `git status` in lide.

**Observe / confirm**

- Steps 1 and 2 show no alias, command or entry you did not set.
- Step 3 shows no hook and no file you do not recognise.
- Step 4 reports no stale working copy. If it does, inspect the target commit
  before you run `jj workspace update-stale`: an agent can move the host
  workspace's `@` in the store.
- Step 5 behaves as before.
