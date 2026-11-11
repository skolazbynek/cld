# GraphQL testing over the broker -- implementation plan

Move the GraphQL **server lifecycle** and **query execution** out of the
in-container `graphql-tester` MCP and into the host-side broker, so the server
runs from the real repo with the real `.env`, on the same revision the calling
container is sitting on. The container keeps a thin MCP that drives the
lifecycle and sends queries by naming a target, never by holding a credential.

The loop this enables: container edits code -> `restart_server` -> `query` ->
read the result -> edit again.

Prerequisite reading: `docs/design-cld-broker.md` (esp. §2 locked decisions,
§9 security model, §15 label gate), `broker/README.md` ("Adding an action"),
`runtests/entrypoint.sh` (the revision-materialization mechanism to copy).

---

## 0. Rulings already made -- do not re-litigate

| # | Decision |
|---|---|
| A | Resolve the session revision from the **workspace tip** (`${session}@`), not the bookmark. Fix `run-tests` the same way, as its own commit. |
| B | **Slow path only:** every `start`/`restart` is a fresh `docker run`, paying `poetry install`. The fast path (keep the container, re-materialize the workspace inside it) is documented as a next step, not built. |
| C | **`set_env` is deleted.** The server gets exactly the repo's `.env`; pointing that at a non-production database is the human's setup responsibility. Host-side output masking still applies (§6). |
| D | Role gate matches `run-tests`: **prompt-based, not mechanical.** Personas and the skill carry the restriction; the broker does not check `org.cld.kind`. |
| E | Queries go **through the broker**, which attaches credentials the container never sees. Raw URLs require a `broker.conf` host allowlist. |

---

## 1. Why the revision change is the load-bearing part (A)

`broker/cld-broker.sh:61` resolves `-r "$session"` -- a **bookmark**, set once
at first container launch (`imgs/claude-devcontainer/entrypoint-claude-devcontainer.sh:124`)
and never moved again.

Measured in a live `cld master` container (`SESSION_NAME=cld_master_cld_3e2891d8`),
reading the store with `jj --ignore-working-copy`:

| event | `${session}@` | bookmark `$session` | probe file present |
|---|---|---|---|
| baseline | `588cf1e4` | `a0344014` | -- |
| `touch .cld-snapshot-probe` | `05d7741b` | `a0344014` | yes |
| `rm .cld-snapshot-probe` | `16851288` | `a0344014` | no |

Watchman has a `jj-background-monitor` trigger running `jj debug snapshot`, so
snapshots reach the shared store with no jj command from the container. The
bookmark tracks `@` only until the container's **first** `jj commit`; after
that it is frozen forever. Since agents commit as they work, frozen is the
normal state -- `run-tests` has been testing one change behind the code the
caller just wrote.

`${session}@` is the workspace-scoped `@`, already the idiom at
`cld/vcs/detect.py:65`. Workspace name == bookmark name == container name ==
`SESSION_NAME`, verified via `jj workspace list`.

### 1.1 Changes

In `resolve_test_context` (`cld-broker.sh:60-70`), and in the new
`resolve_graphql_context`:

```bash
REV=$(jj -R "$REPO" --ignore-working-copy log --no-graph -n1 -r "${session}@" -T commit_id) \
    || { echo "cannot resolve revision for workspace $session" >&2; exit 3; }
```

Two edits, both deliberate:

- `-r "${session}@"` -- the live workspace tip.
- `--ignore-working-copy` -- without it, `jj -R "$REPO" log` snapshots the
  **host's default workspace** as a side effect, which contradicts locked
  decision 5 ("store-reading only", `docs/design-cld-broker.md:76`). It does
  not cost freshness: watchman already wrote the container's snapshot.

Ship the `run-tests` half as a **separate commit** so it can be reverted
independently of the graphql feature.

### 1.2 Known limits -- document, do not fix

- Residual watchman debounce (empirically < 6s). Forcing a snapshot would need
  `docker exec` into the session, reversing locked decision 5. Out of scope.
- `jj -R` was unconditional, so a git-backed repo exited 3. Superseded: both
  resolvers now read `refs/heads/<session>` on a git-only repo (committed work
  only; see `broker/README.md`). The host route (`cld-brokerctl run`) is still
  jj only.
- A missing workspace registration errors cleanly
  (`Workspace \`X\` doesn't have a working-copy commit`). Unreachable in
  practice -- the dispatcher already requires a live container carrying
  `org.cld.repo-root` (`cld-broker.sh:248-250`).

---

## 2. New `graphqlserver/` image

Mirror `runtests/` exactly: a standalone, **zero-cld-coupling** image
(locked decision 6, `docs/design-cld-broker.md:77`). Four files:
`Dockerfile`, `entrypoint.sh`, `build.sh`, `README.md`.

`Dockerfile`: copy `runtests/Dockerfile` -- same `debian:stable-slim`, same
pinned `JJ_VERSION` / `POETRY_VERSION`, same build deps, same baked CA certs
and `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE`. Add `curl` (the in-container
readiness probe is host-side, but `curl` is useful for debugging). Do **not**
import anything from `cld/`.

`build.sh`: copy `runtests/build.sh`, `IMAGE="${GRAPHQL_IMAGE:-graphqlserver:latest}"`.

### 2.1 `entrypoint.sh` contract

Environment in:

| var | default | meaning |
|---|---|---|
| `REVISION` | `@` | revset to serve |
| `PROJECT_SUBDIR` | `.` | dir holding `pyproject.toml` |
| `SECRETS_FILE` | `/secrets/.env` | sourced into the server's env |
| `GQL_WORKSPACE` | `gql-$HOSTNAME` | **deterministic** jj workspace name |
| `GQL_COMMAND` | *(required)* | shell command that starts the server |
| `GQL_PORT` | `8000` | port the server binds **inside** the container |
| `POETRY_INSTALL_ARGS` | `--all-extras --all-groups` | as runtests |

Body, following `runtests/entrypoint.sh` line for line:

1. `set -euo pipefail`; defaults via `: "${VAR:=...}"`.
2. `export HOME="${HOME:-/tmp}"`, `USER`, `LOGNAME` -- the `--user <hostuid>`
   has no `/etc/passwd` entry (`runtests/entrypoint.sh:33-37`).
3. `JJ_CONFIG=/tmp/jj-config.toml` with a synthetic identity
   (`runtests/entrypoint.sh:39-41`) -- jj needs an author for the
   workspace working-copy commit.
4. `cd /repo`; `jj workspace add --name "$GQL_WORKSPACE" -r "$REVISION" "$HOME/gql-workspace"`.
   Workspace dir under `$HOME` because `--user` cannot mkdir at `/`
   (`runtests/entrypoint.sh:47-49`).
5. `trap 'jj workspace forget "$GQL_WORKSPACE" || true' EXIT` -- best effort
   only. It will **not** fire on `docker rm -f`, which is why §4.4 makes the
   broker forget explicitly.
6. `cd "$HOME/gql-workspace/$PROJECT_SUBDIR"`.
7. Source `$SECRETS_FILE` with `set -a; . "$SECRETS_FILE"; set +a`, warning to
   stderr if absent.
8. `poetry install --no-interaction $POETRY_INSTALL_ARGS` -- to **stderr**, not
   stdout, so install noise stays out of the deliverable stream and shows up in
   `docker logs`.
9. `export PORT="$GQL_PORT"` (and `GQL_PORT`), then
   `exec bash -c "$GQL_COMMAND"`. Server must bind `0.0.0.0:$GQL_PORT` --
   `127.0.0.1` inside the container is unreachable from the host. State this in
   `graphqlserver/README.md`; it is the single most likely misconfiguration.

**`GQL_WORKSPACE` must be deterministic, not `$HOSTNAME`-derived**, because the
broker needs to name it to forget it after a `docker rm -f`. The broker sets it
to `gql-<session>`.

---

## 3. Per-repo config

Three keys read out of the target repo's `.cld/config.toml` by the broker's own
`cld_conf_get` (`cld-broker.sh:47-51`):

| key | default | notes |
|---|---|---|
| `graphql_command` | *(none -- required)* | server start command |
| `graphql_port` | `"8000"` | port the server binds inside the container |
| `graphql_health_path` | `"/graphql"` | path the readiness probe POSTs to |

**`cld_conf_get` parses double-quoted strings only.** `graphql_port = 8000`
silently yields nothing; it must be `graphql_port = "8000"`. Say so in the
sample and the docs.

If `graphql_command` is unset, `graphql start` must fail with a specific
message naming the file and key -- no guessed default. (The current MCP's
`poetry run python manage.py` default is a lide-api-shaped guess and is being
removed, not carried over.)

Register all three in `_TOML_KEYS` (`cld/config.py:44-84`) **only** to silence
`_load_toml`'s "unknown key" warning -- no `Config` dataclass field, no
`from_env()` line, exactly as `pyproject_dir` is handled at `cld/config.py:79-84`.
Extend that comment to cover them.

---

## 4. Broker action `graphql`

One new function `action_graphql()` in `cld-broker.sh`, placed in the actions
region (before dispatch at line 233). The dispatcher resolves it by name --
nothing else in the script needs editing (`cld-broker.sh:31-36, 238-240`).

Ops: `start | stop | restart | status | logs | query | introspect | endpoints`.
Validate `$op` against that fixed set and `exit 2` on anything else, matching
`action_agent`'s pattern (`cld-broker.sh:142-145`).

### 4.1 Naming and labels

- Server container: `cld_gql_<session>`.
- jj workspace: `gql-<session>`.
- Labels for the sweep: `org.cld.gql-session=<session>`, `org.cld.gql-repo=<REPO>`.

One server per session. `start` when one is already running returns the
existing endpoint rather than erroring -- it makes the MCP idempotent.

### 4.2 Port allocation -- let Docker do it

Do **not** probe for a free port and do not derive one from a hash. Publish
with an empty host port and read it back:

```bash
docker run -d --name "cld_gql_$session" \
    --label "org.cld.gql-session=$session" \
    --label "org.cld.gql-repo=$REPO" \
    --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -v "$REPO:/repo" "${secret_args[@]}" \
    -e "REVISION=$REV" -e "PROJECT_SUBDIR=$PROJECT_SUBDIR" \
    -e "GQL_WORKSPACE=gql-$session" -e "GQL_COMMAND=$GQL_COMMAND" \
    -e "GQL_PORT=$GQL_PORT" \
    -p "${GRAPHQL_BIND}:0:${GQL_PORT}" \
    "$GRAPHQL_IMAGE"
host_port=$(docker port "cld_gql_$session" "$GQL_PORT/tcp" | head -n1 | sed 's/.*://')
```

Race-free, and no port range to own.

`GRAPHQL_BIND` defaults to the bridge gateway, mirroring the sshd's
bridge-only posture (`broker/sshd_cld_broker.conf:6-9`):

```bash
: "${GRAPHQL_BIND:=$(docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || echo 172.17.0.1)}"
```

Binding `0.0.0.0` would expose a real-credentials server to the LAN. Do not
default to it.

Note `-v "$REPO:/repo"` is **read-write**, as `run-tests` already does
(`cld-broker.sh:77`) -- `jj workspace add` writes store objects. The server
serves from its own workspace and never touches the caller's `@` or bookmarks.

### 4.3 Readiness probe -- and the bug it replaces

The current MCP probes with `query { hello }` (`cld/mcp/graphql.py:63`) and
accepts only a response with no `errors`. Against any real schema that field
does not exist, so the probe **can never succeed** and `start_server` always
raises after its timeout. The same function also busy-loops: `time.sleep(0.3)`
sits only in the `except` branch, so once the server answers with `errors` it
spins at full CPU until the deadline.

Replace with `{__typename}`, which is valid at the root of every GraphQL
schema. Probe host-side after `docker run -d`:

```bash
POST http://$GRAPHQL_BIND:$host_port$GQL_HEALTH_PATH
     {"query":"{__typename}"}
```

Ready when HTTP 200 and the body has no top-level `errors`. Poll with a real
sleep between attempts. `GRAPHQL_START_TIMEOUT` defaults to **300s** -- the
budget must cover `poetry install`, which dominates (§0 ruling B).

On timeout: emit the last ~50 log lines to stderr, then tear down exactly as
`stop` does (§4.4), and `exit 3`. Never leave a half-started container.

### 4.4 Teardown -- the orphan problem

`docker rm -f` sends SIGKILL, so the entrypoint's `trap EXIT` does not run and
the jj workspace registration **leaks** into the user's store. This is the same
failure the devcontainer entrypoint pre-empts at
`entrypoint-claude-devcontainer.sh:74`.

`stop` must therefore:

1. `docker stop -t 10` (gives the trap a chance), then `docker rm -f`.
2. `jj -R "$REPO" --ignore-working-copy workspace forget "gql-$session" || true`
   -- unconditionally, because step 1 usually kills the trap.

`restart` = `stop` then `start`. Because B is the slow path, restart re-resolves
`REV` and pays a fresh `poetry install`; that is the intended behaviour and the
docs must set that expectation.

### 4.5 Orphan sweep

Nothing reaps what a broker action spawns: `cld-brokerctl.sh:41-47` manages
only the sshd, and no host-side hook fires when a container is reaped. So
`action_graphql` sweeps at the top of every `start` and `status`:

```bash
sweep_gql_orphans() {
    local c s r
    for c in $(docker ps -a --filter label=org.cld.gql-session --format '{{.Names}}'); do
        s=$(docker inspect "$c" --format '{{index .Config.Labels "org.cld.gql-session"}}' 2>/dev/null) || continue
        docker inspect "$s" >/dev/null 2>&1 && continue   # owning session alive -- keep
        r=$(docker inspect "$c" --format '{{index .Config.Labels "org.cld.gql-repo"}}' 2>/dev/null) || true
        docker rm -f "$c" >/dev/null 2>&1 || true
        [ -n "$r" ] && jj -R "$r" --ignore-working-copy workspace forget "gql-$s" >/dev/null 2>&1 || true
    done
}
```

Never fatal -- a sweep failure must not block the caller's own `start`.

Also add `graphql-sweep` to `cld-brokerctl.sh` as an explicit verb, so an
operator can reap after a host reboot without starting a session. Do **not**
wire it into `restart`/`shutdown`: bouncing the sshd should not kill a live
session's server.

### 4.6 `logs`

`docker logs --tail N` on the server container, `N` from argv (default 50, cap
it). Pipe through the byte cap and the masker (§6). This is the separate
channel that replaces the in-process ring buffer -- `run-tests`' streaming
idiom (`exec` + one synchronous child, `cld-broker.sh:54-55`) cannot work for a
detached container, and this is the only reason `action_graphql` does not
`exec`.

### 4.7 `status`

One tab-separated line, parsed client-side like
`_parse_container_line` (`cld/broker.py:88-106`):

```
state<TAB>port<TAB>endpoint<TAB>revision<TAB>container
```

`state` in `running | starting | exited | not_started`. `start` prints the same
line on success, so the client has one parser.

---

## 5. `query` / `introspect` / `endpoints` (E)

Argv: `query <target> <query-string> [variables-json]`.

### 5.1 Target resolution

| form | resolves to | auth attached |
|---|---|---|
| `local` | this session's server, from `docker port` | none |
| `^[A-Za-z0-9_-]+$` | alias looked up in the repo's `.env` | yes |
| `https?://…` | the URL verbatim | **none** |
| anything else | `exit 2` denied | -- |

Aliases come from the repo's own `.env` -- the same per-repo secrets file
`run-tests` already mounts, found by the same `SECRETS_ENV_FILE` logic
(`cld-broker.sh:63-69`). No new secret store, no new broker config:

```
CLD_GRAPHQL_URL_<ALIAS>      = https://dev.example/graphql
CLD_GRAPHQL_AUTH_<ALIAS>     = Bearer eyJ...        # full Authorization value
CLD_GRAPHQL_COOKIE_<ALIAS>   = session=...          # full Cookie value
```

Lookup: uppercase the alias, `-` to `_`, then source `.env` **in a subshell**
and print via indirect expansion, so the broker's own environment is never
polluted:

```bash
lookup_alias() {
    local uc; uc=$(printf '%s' "$1" | tr '[:lower:]-' '[:upper:]_')
    ( set -a; [ -f "$SECRETS_ENV_FILE" ] && . "$SECRETS_ENV_FILE"; set +a
      local u="CLD_GRAPHQL_URL_$uc" a="CLD_GRAPHQL_AUTH_$uc" c="CLD_GRAPHQL_COOKIE_$uc"
      printf '%s\n%s\n%s\n' "${!u-}" "${!a-}" "${!c-}" )
}
```

An unknown alias is a clear error naming the expected variable, **without**
echoing any value.

### 5.2 The raw-URL allowlist -- why it exists

If the broker will curl any URL the container names, the container gains the
host as an **egress channel and SSRF pivot**: it could reach an internal
service it cannot itself route to. It cannot leak a credential that way (raw
URLs get no auth), but the channel is new and does not exist today.

So raw URLs are gated on a `broker.conf` allowlist of **hostnames**:

```bash
# GRAPHQL_URL_ALLOWLIST="dev.example.internal api.staging.example"
```

Empty (the default) means **no raw URLs at all** -- aliases only. Match the
parsed host exactly against the list; no substring, prefix or wildcard
matching, and reject any URL whose scheme is not `http`/`https`. Denials say
which knob to set.

### 5.3 Execution

`curl -sS -X POST -H 'Content-Type: application/json'`, plus
`-H "Authorization: …"` / `-H "Cookie: …"` when the alias supplies them. Pass
the body via `--data-binary @-` on stdin, never as an argv element -- a query
string on the command line would be visible in the host's process table.

Build the JSON body with `python3 -c` or `jq` rather than string-concatenating,
so a query containing quotes cannot corrupt it. Whichever is used must be added
to `broker.conf`'s `PATH` note.

`--max-time` from `GRAPHQL_QUERY_TIMEOUT` (default 30s). Response goes through
the cap and the masker (§6). Non-2xx: emit the status and the (masked, capped)
body, `exit 1`.

`introspect <target>` is `query` with the introspection document, kept
broker-side so the container does not need to ship it. `endpoints` prints alias
**names only**, one per line, discovered by scanning `.env` for
`CLD_GRAPHQL_URL_*` -- never any value.

---

## 6. Output capping and masking

Two host-side filters on everything `logs`, `query` and `introspect` return.
Nothing but `runtests` bounds broker output today, and there is no rotation
anywhere in this repo.

**Cap.** `GRAPHQL_OUTPUT_MAX_BYTES`, default `65536`, mirroring
`runtests/entrypoint.sh:72-80`. For `logs` keep the **tail** (recent lines
matter); for `query` keep the **head** (the response starts with `data`) and
append a truncation notice on stderr.

**Mask.** A server holding real DB credentials can echo a DSN or a token in an
error body. Masking client-side would be too late to matter, so it happens in
the broker. Add `mask_output()` to `cld-broker.sh` as a `sed -E` mirroring
`cld/log.py:178-181`:

```
(?i)(TOKEN|KEY|SECRET|PASSWORD)=[^\s,'"]+      ->  \1=<redacted>
/run/secrets/[\w./-]+                          ->  /run/secrets/<redacted>
```

`cld/log.py`'s current patterns **miss credentials embedded in a URL**, which is
the most likely shape here (`mysql://user:pass@host`). Add a third pattern for
`scheme://user:pass@host` in **both** places, replacing the userinfo with
`<redacted>`, and cross-reference each site in a comment so the duplication is
visible. The duplication is deliberate: the broker must not depend on `cld`
being importable by the host's `python3`.

---

## 7. Container-side client

### 7.1 `cld/broker.py`

Add one function, following `broker_agent_op` (`cld/broker.py:164-172`):

```python
def graphql_op(op: str, *args: str, capture: bool = True) -> subprocess.CompletedProcess:
```

`capture=True` by default -- every caller is the MCP, which must return a value
rather than stream. Keep the parameter so the CLI can stream.

No change to `cld/cli_container.py` is needed: `broker()` already forwards any
action verbatim (`cld/cli_container.py:328-347`), so `cld broker graphql start`
works as soon as the host function exists. Update only the `help=` string at
line 332 to name `graphql`.

### 7.2 `cld/mcp/graphql.py`

Becomes thin. Delete:

- `ServerState.proc` / `.port` / `.env` / `.command` / `.workdir` /
  `.log_buffer`, `running`, `endpoint`, `kill`
- `_log_reader`, `_health_check`, `_gql_request`, `_resolve_endpoint`,
  `_start_server`
- the `set_env` tool (ruling C)
- imports `os`, `signal`, `subprocess`, `time`, `deque`, `Thread`, `urllib.*`

Keep: `_INTROSPECTION_QUERY`, `_format_type_ref`, `_summarize_schema`,
`describe_type`, the `graphql://schema` resource. `ServerState` shrinks to a
single field, `cached_schema`, still held in the lifespan.

Tools after the change -- all delegating to `graphql_op`:

| tool | args | broker op |
|---|---|---|
| `start_server` | -- | `graphql start` |
| `stop_server` | -- | `graphql stop` |
| `restart_server` | -- | `graphql restart` |
| `server_status` | -- | `graphql status` |
| `get_server_logs` | `tail=50`, `filter_pattern=""` | `graphql logs <tail>` |
| `list_endpoints` | -- | `graphql endpoints` |
| `introspect` | `target="local"` | `graphql introspect <target>` |
| `describe_type` | `type_name` | *(local, cached schema)* |
| `query` | `query`, `variables=None`, `target="local"` | `graphql query …` |

`endpoint: str = ""` becomes `target: str = "local"` on `query` and
`introspect`. Docstrings must explain the three target forms and that an alias
is configured in the repo's `.env`, since that string is the model's only
documentation.

`filter_pattern` stays a **client-side** regex over the returned lines (keep
the existing `re.error` handling at `cld/mcp/graphql.py:227-232`) -- do not push
a caller-supplied regex into the broker.

Also: drop the `ctx: Context` parameter from every tool that no longer needs
`await ctx.info(...)`. It exists today only for the in-process state and it is
what makes these tools awkward to call from a test. Route the schema cache
through a module-level accessor so a test can monkeypatch it the way
`tests/test_messenger_mcp.py:21-25` patches `_mailbox_root`.

Add `setup_logging(Config.from_env(), force_stderr=True)` under `__main__`,
matching `cld/mcp/messenger.py:182-183`. `import cld` works because
`PYTHONPATH=/opt/cld` is set at `imgs/claude-base/Dockerfile.claude-base:120`.

No change to `imgs/claude-devcontainer/container-init.sh` -- the module path is
unchanged. Note in the docs that the MCP still only appears in a container when
the **host's** `~/.claude.json` already has a `graphql-tester` entry
(`container-init.sh:129`), and that editing `cld/mcp/graphql.py` needs
`cld build` + a container restart because `cld/` is `COPY`'d into the image.

---

## 8. Tests

Follow the existing shapes; do not invent new harness.

**`tests/test_broker.py`** -- add `TestGraphqlOp` next to `TestBrokerAgentOp`
(line 125). Reuse the `configured` fixture (15-21) and `_argv_of` (24-27).
Assert: wire prefix `"graphql cld_master_cld_ab12 "`, the decoded argv, and
`capture_output is True`. Include a case proving a query string containing
spaces, quotes and a `;` survives as **one** argv element, mirroring
`test_argv_never_becomes_a_command` (line ~110).

**`tests/test_graphql_mcp.py`** (new) -- model on `tests/test_messenger_mcp.py`:
import the tool functions and call them directly, with `graphql_op` patched.
Cover: argv built per tool; `status` line parsing incl. malformed lines;
`filter_pattern` filtering and the invalid-regex path; `describe_type` raising
without a cached schema; `introspect` populating the cache; and unit tests for
`_format_type_ref` / `_summarize_schema`, which are pure and currently untested
(the module has **zero** tests today).

**Verify each new test can actually fail.** Break the thing it guards, watch it
go red, restore. A test that cannot fail is worse than no test. Note in the
final report which ones you did this for.

Shell-side (`cld-broker.sh`, `graphqlserver/entrypoint.sh`) has no test harness
in this repo and is verified manually -- add cases to `MANUAL_TESTS.md`
following the existing style: start/query/restart-after-edit/stop, an orphan
sweep, an unknown alias, a denied raw URL, and a missing `graphql_command`.

---

## 9. Docs

| file | change |
|---|---|
| `docs/graphql-mcp.md` | rewrite: broker-backed lifecycle, target forms, alias setup in `.env`, the `cld build` note |
| `graphqlserver/README.md` | new; mirror `runtests/README.md`, incl. the **bind `0.0.0.0`** requirement |
| `broker/README.md` | `graphql` row in the action table; the "Adding an action" section stays accurate; note the git-repo exit-3 limit |
| `broker/broker.conf.sample` | `GRAPHQL_IMAGE`, `GRAPHQL_BIND`, `GRAPHQL_URL_ALLOWLIST`, `GRAPHQL_START_TIMEOUT`; add `curl` (+ `jq`/`python3`) to the `PATH` comment |
| `broker/cld-brokerctl.sh` | `graphql-sweep` verb + usage line |
| `docs/design-cld-broker.md` | new §16: the graphql action, the five rulings, and the revision-semantics change with its blast radius |
| `README.md` | architecture map (212-219) currently omits the graphql MCP entirely -- add it plus `graphqlserver/` |
| `MANUAL_TESTS.md` | the smoke tests from §8 |
| `prompts/personas/agent.md:15`, `prompts/personas/task-agent.md:79-86` | the authorization sentence names `run-tests`; make it cover `graphql` too (ruling D) |
| `.claude/skills/broker-run-tests/SKILL.md` | Step 0's gate applies to `graphql` as well -- one added sentence |

---

## 10. Commits (jj, one concern each)

1. `broker: resolve the session revision from the workspace tip` -- §1 only,
   both resolvers, independently revertable.
2. `graphqlserver: image serving a jj revision with the repo secrets` -- §2.
3. `broker: graphql action for server lifecycle and credentialed queries` --
   §3-§6, incl. `brokerctl graphql-sweep`.
4. `graphql-tester: delegate lifecycle and queries to the broker` -- §7 + §8.
5. `docs: graphql testing over the broker` -- §9.

---

## 11. Out of scope -- record, do not build

- **Fast restart path** (ruling B): keep the container alive, `jj workspace
  forget` + `add` at the new revision, signal the server. Needs a supervisor
  inside the image. This is the obvious follow-up once the slow path works,
  since `poetry install` dominates every restart.
- **Forcing a watchman snapshot** before resolving `REV` (§1.2) -- reverses
  locked decision 5.
- **A mechanical role gate** on `graphql` (ruling D).
- ~~**Git-backed repo support** in either resolver (§1.2).~~ Done for the
  session route, on committed work only; the host route stays jj only.
- **Mutation blocking / a scratch-DB requirement** (ruling C): the human's
  `.env` is the control point.
