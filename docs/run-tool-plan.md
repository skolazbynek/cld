# Run-tool plan: broker `run-tests` and the GraphQL server

Ticket CLD-001 (internal). Date: 2026-10-21.

The plan covers two broker features:

- **run-tests**: `cld broker run-tests`. It runs a project's pytest suite on the host with real secrets.
- **graphql server**: the `graphql-tester` MCP tools and `cld broker graphql`. They run the project's GraphQL server on the host and send queries to it.

The plan has three parts: three gaps, three small improvements and three bigger design directions. Each point compares at least two alternatives.

Method:

- Code reading of the cld repo.
- Live runs from the `broker-analysis` ticket container against two real projects: diskuze (Flask + Graphene) and lide (FastAPI + Strawberry under `api/`).
- Web research on precedents.

File references are paths in the cld repo unless they name another repo.

## Evidence from live runs

| What | Result |
|---|---|
| diskuze `--collect-only -q` | 27-33 s wall time. 2181 tests. Output cut to the last 64 KiB of 263 KiB. |
| lide `api/tests/unit -x` | Exit 4: `file or directory not found`. pytest runs from `api/`. |
| lide `tests/unit -x -n 4` | 46 s wall time. 551 passed in 22.8 s, so about 23 s is fixed overhead. |
| diskuze full suite | 106 min. 29 failed, 2149 passed. Only two progress lines until the end, so nothing could show whether it was slow or stuck, and nothing could stop it. Output was 1.18 MB, cut to 64 KiB: only 4 of 29 tracebacks survived. The run mixes `integration` and `json`, which diskuze's AGENTS.md forbids. |
| Container vs host | 4 `test_cursor` unit tests fail with a 2-hour shift: the runner uses UTC, the host likely Prague time. Other failures hit external services (SBox, Profil, SMS gate, Redis scan); untriaged. |
| Speed vs host (one diskuze file) | Per-test times are equal (about 17 s setup and 10 s teardown per test class). Fixed broker overhead is about 28 s per call. The host is faster only through parallel runs: `--workers` comes from pytest-parallel, which is in the host venv but not in `poetry.lock`, so the runner cannot use it. |
| Interrupting the client | The host run continued to the end (about 2 min). No cancel exists. |
| graphql start, then restart after an edit | Works. Restart takes 31 s. `stale: true` is reported correctly. |
| `introspect` | List types show as `[?]`. `CommentConnection.edges` has no element type. |
| A query that needs a signed-in user | `you need to be logged in`. No credentials are configured for either repo. |
| `get_server_logs` | About 5 JSON lines of about 700 B per request. A regex filter keeps only the first line of a traceback. |
| A startup error | Shown well, but after 9 identical `Connection refused` lines. |
| Query latency | About 1.5 s through the broker. About 34 ms direct to the same server. |
| Direct port access | The server port answers straight from the container, bypassing the broker. |
| diskuze server | Not usable: no `graphql_command` in the host config. |

## Part 1: Gaps

### Gap 1: Test runs are blind, all-or-nothing and cannot be stopped

**What happens:**

- **Output arrives only at the end.** Only the `[runtests]` progress markers come live. The runner buffers all install and pytest output (`runtests/entrypoint.sh:88-110`).
- **Long runs can't finish in one call.** The agent's foreground command limit is 10 minutes. A full diskuze run takes longer, so a foreground call is killed and every line of output is lost.
- **Output is cut.** Only the last 64 KiB comes back. The notice says "raise OUTPUT_MAX_BYTES", but the caller has no way to set it (`broker/cld-broker.py:457-464` passes no such variable). Only the operator can, through the host repo `.env`.
- **Interrupting does not stop the run.** sshd sends no hangup signal to a command without a terminal, and the client sends no input, so the host run never learns the caller left.
- **Nothing can find or clean up a run.** Runner containers have no name, label or time limit (`broker/cld-broker.py:457`), so nothing can list, stop or sweep them.

**Why it matters:** an agent cannot run the full suite safely, cannot see progress and cannot cancel a wrong run. A wrong run keeps load on the shared dev databases.

| Way to close it | Pros | Cons |
|---|---|---|
| Stream output live over ssh (`tee` instead of a buffer) | Small change. Progress becomes visible. | Floods the agent's context. Still dies with the client call. Does not fix cancel or leaks. |
| Skill text only: always run in the background, run smaller selections | No code. | Lost output, leaks and missing cancel stay. |
| **Host-side job model** (Design direction 1) | Fixes lost output, cancel, leaks and the summary together. | The biggest change of the three. |

**Decision:** the job model. Until then, the skill should say plainly that full suites must run in the background.

### Gap 2: The live server cannot act as a signed-in user

**What happens:** a query that needs a user fails with "you need to be logged in". The only identity option for the local server is one fixed set of secrets (`CLD_GRAPHQL_AUTH_LOCAL` and/or `CLD_GRAPHQL_COOKIE_LOCAL`) in the host `.env` or `broker.conf`. Nobody has set it, and a cookie expires. Lide's public API checks the session against a remote login service, so no offline value works for long.

**Why it matters:** most GraphQL changes touch permissions, mutations or "my" data. Agents can only check anonymous paths, which are the least risky ones.

| Way to close it | Pros | Cons |
|---|---|---|
| Static secrets set by the operator (current) | Exists today. | One identity per target. Manual refresh. Unused in practice. |
| A dev-only impersonation header added to each app | Simple for agents to use. | Changes product code. A bypass could reach production. Needs work in every repo. |
| **Broker-side credential hook** (Design direction 2) | The broker stays generic and the repo owns its auth logic. Any user, no product change for diskuze. | Needs a hook per repo. Lide's public API still needs a test account or a bypass. |

**Decision:** the credential hook.

### Gap 3: Project setup and repo rules live outside the repo and outside the tool

**What happens:**

- **diskuze cannot start a server.** Its host config has no `graphql_command`.
- **Paths don't match what the agent sees.** pytest runs from the project subfolder (`pyproject_dir`), so lide paths written from the repo root fail.
- **Repo rules are ignored.** Both repos' AGENTS.md forbid running `integration` and `json` tests in one call, because they share databases. The skill's "whole suite" example does exactly that.
- **Repo advice is not applied.** Lide's test README recommends parallel runs with `-n 5 --dist=loadscope` (`lide: api/tests/README.md:66-70`). The skill suggests a generic `-n logical` instead.
- **The runner's environment differs from the host's.** It uses UTC, which breaks 4 diskuze unit tests that pass on the host timezone. Nothing in the config can set the timezone or other host-like settings.
- **The config is hidden and fragile.** It sits only in the host checkout's `.cld/config.toml`. It is not versioned and is read by a regex that ignores unquoted values. The agent cannot see the effective config and is told to "tell the user".

**Why it matters:** each new project or machine is set up by hand. Mistakes show up as confusing failures in the middle of an agent's task.

| Way to close it | Pros | Cons |
|---|---|---|
| Keep host-only config, add a read-only `config` / `doctor` action | Cheap. The trust model stays simple. | Still per machine, unversioned and outside review. |
| Detect framework and layout automatically | No config. | Fragile: Flask vs FastAPI, project subfolders, several apps per repo. |
| **Repo-owned profile file, trusted only from a reviewed revision** (Design direction 3) | Versioned, reviewed and shared. Repo rules become enforceable. | Needs a trust rule for commands run with secrets. |

**Decision:** the repo-owned profile. A read-only `config` action ships with it.

**Candidates not chosen as top gaps:**

- Query latency (1.5 s): fine at agent pace.
- Direct port access: a security issue rather than a usability one. It is an open question for you.
- One server per multi-repo ticket: no need seen yet.

## Part 2: Small improvements

### Small improvement 1: Full-depth schema introspection

**Problem:** the broker's introspection query nests type wrappers only 3 levels deep for fields and 2 for arguments (`broker/cld-broker.py:870`). `[CommentSortEnum!]!` therefore shows as `[?]!`. graphql-js and graphql-core use 9 levels.

**Prototype (done):** the same query with 8 levels, sent through `cld broker graphql query` and formatted by the existing `_format_type_ref` (`cld/mcp/graphql.py:183`). Result: zero unknown types, and `edges: [CommentNodeEdge!]!`.

| Alternative | Pros | Cons |
|---|---|---|
| **Deepen the broker's query to 9 levels** | One-line change. No new dependency. The formatter already handles any depth. | A bigger response (100 KB for lide). |
| Read the repo's `schema.graphql` | No server needed. | Not the code under test. It can lag behind edits. |
| Render the schema as text (SDL) in the MCP with graphql-core | Compact for agents. | Adds a new dependency to the container image. |

**Decision:** deepen the query.

- Also let `describe_type` return formatted type strings (for example `[CommentNodeEdge!]!`) instead of raw nested JSON.
- Check the size cap: the 100 KB answer came back whole, so the host's `GRAPHQL_OUTPUT_MAX_BYTES` differs from the 64 KiB default. If the default applies, leave descriptions out of the bulk query.

**Cost:** under an hour, plus a test in `tests/test_broker_script.py`.

### Small improvement 2: Accept test paths written from the repo root

**Problem:** for lide, `run-tests api/tests/unit` fails with exit 4 because pytest runs from `api/`. Agents see paths from the repo root.

| Alternative | Pros | Cons |
|---|---|---|
| **The runner strips a leading `<pyproject_dir>/` from path arguments and prints the pytest working directory** | About 15 lines in `runtests/entrypoint.sh`. Both path forms work. | The rewrite must leave `-k`/`-m` values alone, handle `path::test` ids and `--ignore`/`--deselect` paths, and skip an absolute `pyproject_dir`. |
| Run pytest from the repo root with `-c api/pyproject.toml` | No rewriting. | Changes rootdir, conftest discovery and `pythonpath`. Risky for both repos. |
| Skill text only | Free. | Agents keep making the mistake; this analysis made it too. |

**Decision:** strip the prefix and print the working directory. Same change set:

- Fix the skill typo ("testslike").
- Replace the misleading "raise OUTPUT_MAX_BYTES" notice.

**Cost:** about two hours, including a shell test in `tests/test_runner_entrypoints_sh.py`.

### Small improvement 3: Return the request's own server logs with a failing query

**Problem:**

- Logs are noisy: one lide request writes about 5 JSON lines of about 700 B, plus health-probe lines.
- `filter_pattern` matches single lines, so it keeps only the `Traceback` header.
- The error's `request_id` came back empty, so the agent cannot tie a failure to its log lines.
- Startup errors begin with repeated `Connection refused` probe lines.

| Alternative | Pros | Cons |
|---|---|---|
| **The broker notes the time before and after each request. On GraphQL errors or a non-2xx status, it adds that window's log lines (masked, capped) to the result** | One call. Whole tracebacks. No regex. | Concurrent requests to the same server share the window, which is rare for one agent. |
| Add `since` / `request_id` options to `get_server_logs` | Flexible. | Two calls. The request id is not always returned. |
| Only drop probe lines and merge repeated `Connection refused` lines | Tiny. | Does not tie logs to the failing request. |

**Decision:** the first option, and also do the third (drop probe noise).

**Cost:** about half a day in `broker/cld-broker.py` (`do_graphql_query`, the readiness loop) and `cld/mcp/graphql.py`, plus tests.

**Candidates not chosen:**

- Quiet install log: helps only on cache misses.
- ssh connect and keep-alive timeouts on the client: real, but rare.
- A schema cache per repo in the MCP: matters only with several servers.

## Part 3: Bigger design directions

### Design direction 1: Test runs as host-side jobs

**Shape:**

- `cld broker run-tests` starts a job and returns a run id. The runner container gets a name and a label (`cld_rt_<session>_<id>`) and runs with `--init`.
- The runner writes a full log and a `pytest --report-log` file (JSON lines, flushed per test) to `~/.cld/runs/<id>/` on the host.
- New verbs:
  - `status`
  - `log [--follow]`
  - `result`: counts, failed test ids and short tracebacks
  - `cancel`
  - `list`
- The broker enforces a maximum runtime and sweeps old runs by age.
- The stored failure list allows `--lf` (rerun last failures), which throwaway containers cannot do today.
- Plain `run-tests` stays as a shortcut: start, follow progress, print the result.

| Alternative | Pros | Cons |
|---|---|---|
| Keep synchronous ssh, add streaming and a watchdog that stops the run when the client leaves | Smaller. Keeps one call. | Still bound by the agent's 10-minute command limit. No reattach, no stored result. |
| **Job model behind CLI verbs** | Survives client timeouts. Cancel, summary and sweep all fit naturally. Same pattern as the server lifecycle. | More state on the host (run directories, cleanup). |
| An MCP test server using MCP Tasks (`tasks/get`, `tasks/cancel`) | Native async for agents. | The spec is experimental (2025-11-25). Claude Code support for it is unverified. |

**Decision:** the job model behind CLI verbs. An MCP layer can be added later as a thin wrapper.

**Precedents:**

- Bazel's remote execution (`WaitExecution` lets a client reattach to a running job).
- `gh run watch`.
- Docker's guidance on stopping containers when the client goes away: name, label, `docker kill` and a sweep (moby issue 48566).

**Facts:**

- `pytest-reportlog` 1.0.0 is maintained by pytest-dev.
- It should be installed into the run's environment by the runner, not added to each project.

**Cost:** several days. It touches `broker/cld-broker.py`, `runtests/entrypoint.sh`, `cld/cli_container.py`, the skill and `broker/cld-brokerctl.sh` (sweep).

### Design direction 2: Broker-side identities for the live server

**Shape:**

- `query` and `introspect` take `as_user` (a user id or a role name).
- The repo config names a credential hook: a command the broker runs on the host with the repo's `.env`.
- The hook receives the requested identity and prints `{"headers": {...}, "expires": <time>}`. This follows AWS `credential_process` and the kubectl exec credential plugin.
- The broker caches the headers until they expire and attaches them to the request. It never returns them to the container.
- Roles (for example `regular`, `blocked`, `verified`) map to user ids in config. A role the repo's token cannot express (diskuze `admin`) needs its own hook branch.

**Per repo:**

- **diskuze:** the hook mints a `Service-Authorization: Bearer <JWT>` token, and the middleware already accepts it (`diskuze: api/api/middleware/user.py:64-110`). No product change. The token must meet these rules:
  - HS256, `iss` set to `lide`, `sub` set to the user id.
  - Signed with `JWT_SERVICE_SECRETS__lide`. This is a config default that `.env` can override (`diskuze: api/api/config.py:329`).
  - `iat` no older than 5 minutes (`JWT_SERVICE_TOKEN_EXPIRES`), so the hook's `expires` must be at most 5 minutes out.
  - Limits: a service token never grants admin (admin needs the Flask admin JWT path, `user.py:268-271`), and the request counts as Lide traffic, so some permissions may differ from a cookie user.
- **lide internal API:** `Authorization: Bearer $INTERNAL_GRAPHQL_SECURE_TOKEN` plus an optional integer `X-User-Id`. It is a separate app (`manage.py --api INTERNAL`) with only the `comments` query. The broker runs one server command per session, so it cannot serve the public and internal APIs at the same time.
- **lide public API:** checks a real login session with the remote login service. It needs a test account whose session the hook can renew, or a dev-only bypass. Open question.

| Alternative | Pros | Cons |
|---|---|---|
| Static secrets per alias (current) | Exists. | One identity each. Manual refresh. Nobody uses it. |
| A dev-only impersonation header in each app | Easy to call. | Product code change. A bypass risk if config leaks to production. |
| A pool of test accounts with real logins | Real auth path. | Needs accounts and renewal. Fits inside a hook rather than replacing it. |
| **Credential hook** | Generic broker. Repo-specific logic lives in the repo. Supports any user. | One hook per repo. Hook output must be masked and never logged. |

**Decision:** the credential hook. For lide's public API, the hook can use test accounts.

**Cost:** two to three days for the broker side, plus one small hook per repo.

### Design direction 3: Repo-owned broker profile

**Shape:** a versioned `.cld/broker.toml` in each project repo declares:

- the project folder
- named test suites, for example `unit`, `integration`, `json`, each run separately (this encodes the AGENTS.md rule)
- default pytest arguments (for example lide's `-n 5 --dist=loadscope`)
- server command, port and health path
- the credential hook and roles

How it is used:

- The broker reads the profile from a reviewed revision (the anchor or the main branch), never from the agent's own edits.
- The host `.cld/config.toml` can override any key.
- `cld broker config` shows the effective profile.
- `run-tests --suite <name>` runs one suite. A "whole suite" run becomes "every suite, one after another".

| Alternative | Pros | Cons |
|---|---|---|
| Host-only config plus a `doctor` action | Simple trust model. | Per machine, unversioned, invisible in review. diskuze-style gaps remain. |
| Automatic detection | No config. | Fragile across frameworks and layouts. Cannot express repo rules. |
| **Repo-owned profile, trusted only from a reviewed revision** | Versioned and reviewed. Shared by all users. Enforces repo rules. | Needs the trust rule. Edits to the profile take effect only after review. |

**Decision:** the repo-owned profile. The precedent is GitHub Copilot's agent setup file, which is used only from the default branch. The common rule in hosted agents is that repo commands get secrets only from a reviewed revision.

**Cost:** two to three days, plus one profile per project.

### Considered, not selected

- **In-place hot reload of the server:** update the server's checkout and let uvicorn or werkzeug reload.
  - Gain: restart drops from 31 s to about 2 s.
  - Not selected because: partial-checkout states, a different reloader per framework, and watchers unreliable on WSL bind mounts.
  - Revisit after Design direction 1.
- **Running the server inside the agent container with a secrets proxy:** breaks the rule that secrets never enter the container.

## Suggested order

1. Small improvements 1 and 2. Each is under a day, removes a confusing failure, and needs no decisions.
2. Small improvement 3, together with removing the probe noise.
3. Design direction 3 (profile). Directions 1 and 2 need its suites and hook settings. Its first step, a read-only `cld broker config` over the existing host config, can ship alone.
4. Design direction 1 (jobs).
5. Design direction 2 (identities), after the questions below are answered.

## Open questions

These are also in the ticket notes (`~/.claude/ego/tickets/CLD-001/questions.md`).

1. **Trust:** may a checked-in profile name commands the broker runs with secrets, if only a reviewed revision is used?
2. **Identities:** which users or roles should agents act as? Is there a lide test account, or may we add a dev-only bypass?
3. **diskuze server:** is the missing `graphql_command` on purpose?
4. **Direct port access:** accept it for dev data, or fix it before the UX work?
5. **Repo rules:** should the broker enforce rules like "never run `integration` and `json` together", or should only the skill state them?
6. **Runner checkout:** runners mount the host repo read-write, and their jj commits appear under the agent's `@`. Accept, or switch to a read-only export of the revision?
