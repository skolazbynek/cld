# Broker-side identities for the live GraphQL server

**Status:** design, not built.
**Date:** 2026-10-21
**Ticket:** CLD-001, gap 2 ("the live server cannot act as a signed-in user").
**Prerequisite:** `docs/design-secrets-containment.md` (section 7 says what holds before and after it).
**Related:** `docs/impl-graphql-broker-plan.md`, `docs/design-cld-broker.md` §9 and §16,
`docs/design-broker-profile.md` (owns the config schema, the trust rule, the server start
labels and the build order of all three broker designs, its section 10).

## 1. Problem

Evidence (ticket notes `~/.claude/ego/tickets/CLD-001/gaps.md`, gap 2, and `live-tests.md`):

- lide `me.user` answered "you need to be logged in". Every query ran anonymous.
- No repo sets `CLD_GRAPHQL_*` aliases. `CLD_GRAPHQL_AUTH_LOCAL` gives one fixed identity,
  needs manual refresh, and nobody set it.
- Most GraphQL changes touch permissions or mutations. Those need a signed-in user.

The chosen direction is a credential hook: the repo names a command, the broker runs it
with the repo `.env`, the command prints headers and an expiry, the broker caches them and
attaches them to queries. Precedents: AWS `credential_process` (JSON on stdout, `Version: 1`,
optional ISO 8601 `Expiration`, non-zero exit is an error), the kubectl exec plugin
(`ExecCredential` on stdout, optional RFC 3339 `expirationTimestamp`, cached until expiry or
a 401), and Claude Code's MCP `headersHelper` (a JSON object of headers on stdout, 10 s limit,
re-run on 401/403). This document fixes the HOW. Not in scope: the config schema and the trust
rule (profile design), and the hooks themselves (built later in diskuze and lide; section 9).

## 2. Decisions

| # | Decision | Rejected alternatives |
|---|---|---|
| D1 | The hook runs **inside the session's server container** (`cld_gql_<session>`) via `docker exec`, through a wrapper in the `graphqlserver` image. | **Plain host process:** a new path that runs repo code on the host outside any sandbox; the host may lack the project's packages (PyJWT); the script would have to be copied out of the reviewed revision; `.env` would be parsed differently from the server's shell `source`. **A fresh container per mint:** about 25 s per mint, and diskuze tokens live 5 min. |
| D2 | Request goes in **environment variables**; the reply is **one JSON object on stdout**: `version`, `headers`, optional `expires_at` (RFC 3339), optional `subject`. | **JSON on stdin** (git helper style): no gain for a five-field request, and harder for shell hooks. **A single token** (kubectl `status.token`): diskuze needs `Service-Authorization`, lide needs `Cookie`. **`key=value` lines** (git): no clean place for an expiry and a header map. |
| D3 | One parameter, **`as_user`**. A configured role name selects that role; any other value is passed to the hook as a raw user, and the hook accepts or refuses it. | **Two parameters** (`as_role`, `as_user`): one more way to call it wrong. **Roles only:** diskuze can mint any user id, and testing a specific user is common. |
| D4 | Identities work **only with target `local`** in this version. | **Aliases too:** no server container to run the hook in, so it needs the host-process variant of D1 and its stricter trust. Deferred. |
| D5 | With `as_user`, the hook's headers **replace** the static `CLD_GRAPHQL_AUTH_LOCAL` / `COOKIE_LOCAL` values. Without `as_user`, nothing changes. | **Merge both:** diskuze checks several auth sources in a fixed order (`api/api/middleware/user.py:254-277`), so two credentials give an unclear identity. |
| D6 | **On-disk cache per session**, keyed by server container id and identity, mode 0600, re-mint a margin before expiry, deleted on `stop` and by the sweep. | **No cache:** a lide login per query, and roughly +0.5 s per query. **In-memory cache:** the broker is one process per request; there is no daemon. **Cache inside the server container:** one extra `docker exec` on every query. |
| D7 | **HTTP 401 drops the cached entry; no automatic retry.** A `refresh_identity` flag forces a re-mint. | **Retry once** (`headersHelper` style): a mutation could run twice. **Detect "not logged in" GraphQL errors:** the text is app-specific and arrives as HTTP 200. |
| D8 | **One shared masker for all broker output**, owned here (section 5): literal values (minted headers, static aliases, `.env` values) plus patterns, on every output path of all three designs. | **Pattern masking only:** a session cookie has no fixed shape. **A masker per design:** three strengths, and the test log path got patterns only. **Trust the server not to echo headers:** servers log request headers. |
| D9 | The hook timeout is enforced **inside the container** (`/usr/bin/timeout -k 2`), with a broker-side backstop. | **Broker-side kill only:** killing the `docker exec` client does not stop the process in the container (moby #35703 asks for an exec kill API; none exists). |
| D10 | **No negative cache.** A failed mint is retried on the next call. | Caching failures makes one bad run sticky for minutes. |
| D11 | **Hook settings and port come from the running server's start labels** (profile 6.3), not from the current profile. | **Fresh profile on each query:** trunk can move while a server runs; the hook code runs from the server's checkout, so a renamed hook module would fail or run the wrong code. |

D1 differs from the direction's wording "runs on the host". The container is on the host and
has the repo `.env`, so the intent holds. The gain: the hook runs in the same sandbox, with the
same secrets and the same code revision as the server, so it adds no new place where repo code
runs with host rights (section 7).

## 3. Mechanism

A `query` or `introspect` with `as_user` on target `local`:

1. Parse the leading flags (section 4.2). Deny `as_user` with any target but `local`.
2. Inspect `cld_gql_<session>`. No container: the existing "start one first" denial. Image
   label `graphqlserver.api` below `2`: deny with the rebuild hint. No `org.cld.gql-identity`
   label (started by an older broker): deny with "restart the server". Else read the hook,
   timeout, roles and `source` from that label, and the port and health path from
   `org.cld.gql-port` and `org.cld.gql-health-path`.
3. `source` is `unset`: deny "no identity hook configured". `dropped`: deny "disabled on
   this host" (section 4.4).
4. Resolve the identity: a role name maps to its configured user; any other value is a raw
   user with an empty role. Check the charset.
5. Find the server: `docker port` for the labelled port, and `docker inspect --format {{.Id}}`.
6. Cache lookup in `<cache dir>/<session>/<sha256(identity)>.json`. A hit needs the same
   container id and `now < expires_at - margin`, and no `--refresh-identity`.
7. On a miss, take an exclusive `flock` on `<sha256(identity)>.lock`, look again (another call
   may have minted), then run the hook (section 4.1), validate the reply, and write the entry
   atomically (temp file in the same directory, mode 0600, `os.replace`). Append each header
   value to the session mask list.
8. Send the request with `Content-Type` plus the hook headers only.
9. Mask the response with the shared masker (section 5), cap it, print it.
10. HTTP 401: delete the cache entry and exit 1 with the hint in section 4.4.

Cache entry fields: `identity`, `role`, `user`, `subject`, `container_id`, `minted_at`,
`expires_at`, `headers`.

**Cleanup.** `do_graphql_stop` deletes `<cache dir>/<session>/`. `sweep_identity_cache`
iterates `<cache dir>/*` and deletes every session dir that has no `cld_gql_<session>`
container, so a dir whose server is already gone is still found. It runs wherever
`sweep_gql_orphans` runs (`start`, `status`) and from the unified `cld-broker.py --sweep`
entry (test-jobs 7.2), which replaces the bash copy behind `cld-brokerctl graphql-sweep`.
`restart` runs `stop`, so a restart always re-mints. The container id check covers a server
replaced by `start` without `stop`. A changed hook or trust setting applies at the next `start`.

## 4. Interfaces

### 4.1 Hook protocol (version 1)

**Invocation.** The broker runs:

```
docker exec -e CLD_HOOK_VERSION=1 -e CLD_HOOK_IDENTITY=<name> -e CLD_HOOK_USER=<user>
            -e CLD_HOOK_ROLE=<role or empty> -e CLD_HOOK_SERVER_URL=http://127.0.0.1:<port><health path>
            -e CLD_HOOK_COMMAND=<hook command> -e CLD_HOOK_TIMEOUT=<seconds>
            cld_gql_<session> /usr/local/bin/identity-hook.sh
```

Port, health path, command and timeout come from the start labels. No value in that argv is
secret. The wrapper `graphqlserver/identity-hook.sh`:

1. Defaults `SECRETS_FILE` to `/secrets/.env`. The entrypoint sets it only for itself, so
   `docker exec` does not see it.
2. Saves the `CLD_HOOK_*` values.
3. `cd "$HOME/gql-workspace/$PROJECT_SUBDIR"` (the server's own checkout and revision).
4. Sources `$SECRETS_FILE` with `set -a`, as the entrypoint does, and unsets `CLD_GRAPHQL_*`.
   It keeps `CLD_IDENTITY_*` (hook secrets, section 9).
5. Restores the `CLD_HOOK_*` values, so the `.env` cannot overwrite them.
6. Puts `$VIRTUAL_ENV/bin` first on `PATH`, so the project's packages are importable.
7. `exec /usr/bin/timeout -k 2 "$CLD_HOOK_TIMEOUT" /bin/bash -c "$CLD_HOOK_COMMAND" </dev/null`.
   Both by absolute path: `/opt/venv` is world-writable (`graphqlserver/Dockerfile:54-55`), so
   agent code could plant a `timeout` or `bash` there and void D9. Stdin is empty.

**Reply on stdout, exit 0:**

```json
{"version": 1,
 "headers": {"Service-Authorization": "<value>"},
 "expires_at": "2026-10-21T10:05:00Z",
 "subject": "user 123456 (commenter)"}
```

| Field | Rule |
|---|---|
| `version` | Required. Must be `1`. |
| `headers` | Required. Non-empty object of string to string. Names are HTTP tokens. `Host`, `Content-Type`, `Content-Length`, `Transfer-Encoding`, `Connection` are refused. Values: printable, no CR, LF or NUL, at most 8 KiB. |
| `expires_at` | Optional RFC 3339 with an offset (`Z` allowed). Absent: `minted_at + max TTL`. Later than that: clamped to it. Not later than `now + margin`: invalid. |
| `subject` | Optional, non-secret, at most 200 characters. Shown by `identities`. Still masked. |

stdout over 16 KiB is invalid. Unknown fields are ignored. This reply is the only channel by
which hook output reaches the host (profile 5.1, rule 1a).

**Exit codes.** `0` with a valid reply: success. `77`: the identity is refused (unknown role or
user); the last stderr line is the reason. 77 is used because 2 is also bash's and argparse's
error code, so a broken hook would read as a refusal. `124` or `137` (from `timeout`): timed
out. Any other code: hook failure. The broker never echoes stdout. It returns the last 2 KiB
of stderr, masked (section 5).

**Backstop.** The broker runs `docker exec` with `Popen(start_new_session=True)` and waits
`timeout + 5` seconds; past that it kills the client group and reports a timeout.

### 4.2 Broker argv

`[--repo <name>]` leads, parsed by the profile's `parse_broker_options` (profile 6.2). Then
the op. The identity flags come after the op; `--` ends them. Old argv without flags is
unchanged. The container CLI keeps `--` once the profile's click fix lands (profile 6.5).

```
graphql query      [--as-user <identity>] [--refresh-identity] [--] <target> <query> [variables-json]
graphql introspect [--as-user <identity>] [--refresh-identity] [--] [target]
graphql identities
```

`identities` prints one tab-separated line per configured role, then one per cached raw user:
`identity<TAB>kind<TAB>user<TAB>state<TAB>expires_in<TAB>subject`, where `kind` is
`role|user` and `state` is `cached|expired|none`. Roles come from the server's label; with
no server running, from the current profile. It never prints a header value.

Identity charset: `[A-Za-z0-9][A-Za-z0-9_.@:-]{0,127}`.

### 4.3 MCP tools and CLI

| Tool | New parameters |
|---|---|
| `query` | `as_user: str = ""`, `refresh_identity: bool = False` |
| `introspect` | `as_user: str = ""`, `refresh_identity: bool = False` |
| `list_identities(repo="")` (new) | returns `[{identity, kind, user, state, expires_in, subject}]` |

The MCP sends the flags and `--` only when `as_user` is set, so a new container against an old
broker still works for anonymous calls. Docstrings must say: identities need a running local
server; `list_identities` shows the roles; a refused identity is the repo's hook decision.

CLI, same argv: `cld broker graphql query --as-user commenter local '{ me { id } }'` and
`cld broker graphql identities`. On the host: `cld-brokerctl run graphql ...`, the same way.

### 4.4 Error messages

| Exit | Message |
|---|---|
| 2 | `denied: --as-user works only with target local -- identities are minted for this session's own server` |
| 2 | `denied: bad identity '<x>' -- letters, digits and . _ @ : - only, up to 128, starting with a letter or digit` |
| 3 | `denied: no identity hook configured for <repo> -- the repo profile has no identity hook; ask the user` |
| 3 | `denied: the identity hook for <repo> is disabled on this host` |
| 3 | `denied: no local graphql server running for <session> -- start one first` (existing) |
| 3 | `denied: the graphql server image has no identity-hook.sh -- rebuild graphqlserver/build.sh, then restart_server` |
| 3 | `denied: this server was started by an older broker -- restart_server to use identities` |
| 2 | `identity '<x>' refused by the hook: <reason>` |
| 3 | `identity hook failed for '<x>' (exit <n>): <masked stderr tail>` |
| 3 | `identity hook timed out after <n>s for '<x>'` |
| 3 | `identity hook reply for '<x>' is invalid: <reason>` (reasons: not JSON, version, no headers, header '<name>' refused, header '<name>' has a bad value, expires_at unparsable, expires_at already past) |
| 1 | `graphql endpoint returned HTTP 401 as '<x>' -- the cached identity was dropped; retry once, then check the hook` |

Messages name headers, never values.

## 5. Masking (shared by all three broker designs)

**API.** `mask_output(data, literals=())` keeps today's patterns
(`broker/cld-broker.py:706-722`) and adds literals. `output_literals(session, env_file)`
collects the literals for one request:

- the session mask list `<cache dir>/<session>/masks` (every header value minted in the
  session, one per line, append-only, kept until `stop`);
- for each such value, its parts: split on whitespace, `;` and `,`, then take the text after
  the first `=`. Parts shorter than 8 characters are skipped;
- the static `CLD_GRAPHQL_AUTH_*` / `COOKIE_*` values (today not masked by value);
- every value of 8 or more characters in the request's `.env`, read through the containment
  resolver. Non-secret values that long, such as host names, are masked too; accepted.

Literals are replaced longest first, by one compiled alternation, line by line, with
`<redacted:identity>` for minted values and `<redacted:env>` for the rest. A new pattern,
`eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*`, becomes `<redacted:jwt>`.

**Every output path uses it:** graphql `query`, `introspect`, `logs`, the logs `start`
dumps on failure, hook stderr; `config` (profile 6.1); test-jobs `log`, `result`, the
digest, `log --follow` and attached `run-tests` (test-jobs 4.1). Any new output path must
call it. The one exception is the synchronous `run-tests` fallback, which streams from
`docker run` unmasked as today until test-jobs package 9 removes it.

Header values never appear in argv, in broker stderr or in the `identities` output.

## 6. Config needs

The profile design (`docs/design-broker-profile.md`, section 3) owns the names. This design
uses `identities.hook` (shell string run by `bash -c` in the server container, in the project
directory; empty means off), `identities.hook_timeout` (default 20, capped at 60) and
`identities.roles` (role name to user string, passed as `CLD_HOOK_USER`). The host may
override or disable the hook per profile 5.1. There is one hook per repo: version 1 has no
server variants (profile D12).

Host-wide, in `broker.conf`:

| Setting | Meaning | Default |
|---|---|---|
| `GRAPHQL_IDENTITY_CACHE_DIR` | Cache root; one subdirectory per session, mode 0700. | `~/.cld/identities` |
| `GRAPHQL_IDENTITY_REFRESH_MARGIN` | Seconds before `expires_at` at which the broker re-mints. | 30 |
| `GRAPHQL_IDENTITY_MAX_TTL` | Lifetime when the hook gives none, and the cap on any lifetime. | 3600 |

Hook secrets live in the repo `.env`, or in the file `secrets.env_file` names. diskuze:
`JWT_SERVICE_SECRETS__lide` (exists). lide: `CLD_IDENTITY_<ROLE>_LOGIN` and
`CLD_IDENTITY_<ROLE>_PASSWORD` (proposed names). No test-account password goes into any file
a container can read before containment ships.

## 7. Security and trust

- **Command string.** From the reviewed revision, or from operator layers; the operator may
  disable it (profile 5.1). The broker adds no other way to set it.
- **Where it runs.** In the server container: the same sandbox, the same `.env` and the same
  code revision as the server command. An agent can edit the hook script, exactly as it can
  edit the server. That is no new exposure: the server code already holds the signing secret.
  Nothing new runs on the host outside docker. Its output reaches the host only as the
  validated reply (4.1).
- **Impersonation reach.** The diskuze hook can mint any user id. That is fine on dev data only.
  The `.env` must point at a dev database, as `docs/design-cld-broker.md` §16 already requires.
- **Host disk.** Cache files hold header values in plain text, mode 0600 in 0700 directories
  under the operator's home. They live at most the max TTL and are removed on `stop` and sweep.
- **Process table.** Request variables and the hook command appear in `docker exec` argv; none
  is secret. Header values travel only through the stdout pipe.
- **Unchanged.** The server port is still reachable from other containers on the bridge
  (ticket question 4). Identities do not widen or close that.

**Before containment ships:** the agent can read the host `.env` through the origin mount.
The hook's secrets (diskuze's signing secret, any lide password) are exposed to it directly;
masking and "header values never come back" protect nothing it cannot read. So no
test-account password goes into the `.env` yet. Only diskuze's existing signing secret is used.

**After containment ships:** the agent cannot read the host `.env`. Header values never come
back on any broker output, and the shared masker replaces them where a server echoes them in
plain form. Agent-edited server or hook code in the server container still reads the
`.env` and receives the headers, and could return them encoded. Masking stops accidental
leaks only. This is accepted because the server already holds the secrets that mint them. It
requires that lide test accounts are test-only accounts with no production value (assumption).

## 8. Failure and expiry handling

| Case | Behaviour |
|---|---|
| Entry valid | Used; no hook run. |
| Within the margin of `expires_at` | Re-mint before the query. diskuze tokens (300 s) re-mint after about 270 s. |
| Server container id changed | Re-mint. |
| `refresh_identity` set | Re-mint, replace the entry. |
| HTTP 401 | Entry deleted; error with retry hint; no automatic retry. |
| HTTP 200 with "not logged in" errors | Response returned as is; the agent may call again with `refresh_identity`. |
| Hook exit 77, failure, timeout, invalid reply | Error per section 4.4; nothing cached. |
| Server `starting` (packages still installing) | The hook may fail on imports; the error says so. Retry after `server_status` shows `running`. |
| Two calls mint at once | The lock serialises them; the second reuses the first's entry. |
| Lock wait longer than timeout + 5 s | `identity hook timed out` for the waiting call. |
| `stop`, `restart`, sweep | Session cache directory deleted. |

## 9. What version 1 delivers, and per-repo hook sketches

**Version 1 delivers** the mechanism in this repo: hook protocol, wrapper, cache, masking,
ops and MCP tools. It closes Gap 2 in no repo on its own:

- **diskuze:** Gap 2 closes after two follow-ups outside this repo: a diskuze profile with a
  real `server.command` (ticket question 3 must be answered "yes"), then the diskuze hook
  below. Both are buildable now; no user answer blocks the hook itself.
- **lide:** not closed in version 1. The public API hook waits on ticket question 2. Until
  then `me.user` stays anonymous, and the static `CLD_GRAPHQL_AUTH_LOCAL` /
  `CLD_GRAPHQL_COOKIE_LOCAL` are the only option. The internal API is not in version 1.

Facts below come from the ticket code map. They are **unverified** from this repo.

**diskuze.** The hook mints a `Service-Authorization` JWT, HS256, with claims `iss=lide`,
`sub=<user id>`, `iat=now`, signed with `JWT_SERVICE_SECRETS__lide` from the `.env`. It can use
the project's own JWT package, since it runs in the project virtualenv. `CLD_HOOK_USER` must be
a numeric user id; anything else exits 77 with "user must be a numeric id". `expires_at` is
`iat + 300`. `subject` is `user <id>` plus the role. The token never grants admin, and the
server treats the request as Lide service traffic, so admin flows and any browser-only checks
cannot be tested this way. To verify before building: the header value format (bare token or
`Bearer` prefix) and the shape of the secret setting (`api/api/middleware/user.py:64-110`).

**lide public API (test-account login, the working assumption).** Each role maps to a test
account. The hook reads `CLD_IDENTITY_<ROLE>_LOGIN` and `_PASSWORD`, signs in to the SBox test
environment, and prints the session as a `Cookie` header. `expires_at` comes from the session
cookie's lifetime, or is left out (then the max TTL applies). Raw users are refused (exit 77),
because only configured accounts have passwords. Risks: a login per mint hits SBox; a shared
account's session may be ended by another login. Needs containment first (section 7).

**If a dev-only bypass is allowed instead.** lide gets a dev-only header (for example
`X-Dev-User-Id`) honoured only behind a setting that is off by default. The hook then prints
that header for any id, with no expiry, like diskuze. Gains: any user, no SBox dependency,
no passwords in `.env`. Costs: a product-code change, and a bypass that must never be enabled in
production; it needs a test in lide proving the default is off.

**lide internal API: not in version 1** (profile D12). It is a separate app with its own
server command, and its hook would print `Authorization: Bearer $INTERNAL_GRAPHQL_SECURE_TOKEN`
and `X-User-Id: <id>`. It does not address the Gap 2 evidence, which is the public API. A
later version may add server variants with their own hook.

## 10. Compatibility and migration

- Calls without `as_user` keep today's argv and behaviour, including `CLD_GRAPHQL_AUTH_LOCAL`
  and `CLD_GRAPHQL_COOKIE_LOCAL`. Nothing is removed. Docs and the skill recommend the hook
  and describe the static values as the fallback for repos without one.
- Order: update the host broker and rebuild `graphqlserver` (label `graphqlserver.api=2`),
  then restart running servers, then rebuild the container image (MCP). A server from the old
  image or an older broker gets the matching error in section 4.4.
- A new MCP against an old broker: anonymous calls work; `as_user` fails with
  `unknown graphql alias '--as-user'`. The skill names "update the host broker" for it.
- Both entrypoints, `graphqlserver/entrypoint.sh` and `runtests/entrypoint.sh`, unset
  `CLD_IDENTITY_*` after sourcing `.env`, so test-account passwords stay out of the server's
  and the tests' environment. The file itself stays readable in the container, as for
  `CLD_GRAPHQL_*`.

## 11. Test plan

### 11.1 Automated (`cld broker run-tests`)

Each new test must be seen failing once against a broken implementation.

`tests/test_broker_script.py` (loads the script, stubs `docker` on `PATH` with `_fakebin`):

- `TestParseGraphqlFlags`: flags after the op; `--` ends flags; no flags gives old argv;
  `--as-user` without a value denied; a leading `--repo` goes through the profile's parser.
- `TestResolveIdentity`: settings and port read from the labels, not the profile; role maps
  to user; unknown name is a raw user with empty role; bad charset denied; non-local target
  denied; `source` `unset` and `dropped` give their two messages; unlabelled server and old
  image label denied.
- `TestRunIdentityHook`: the `docker exec` argv carries only `CLD_HOOK_*` names and no secret;
  valid reply parsed; exit 77 maps to "refused"; exit 2 and other codes map to "failed" with
  masked stderr tail; stdout never echoed on invalid JSON; wrong version; refused header
  names; CR/LF value; past `expires_at`; missing `expires_at` gets the max TTL; long
  `expires_at` clamped; over 16 KiB invalid; backstop timeout kills the client.
- `TestIdentityCache`: hit skips the hook; margin forces re-mint; container id change re-mints;
  `--refresh-identity` re-mints; files 0600 and directory 0700; write is atomic; failures
  are not cached; `stop` deletes the session directory; the sweep deletes a dir whose server
  is already gone.
- `TestQueryWithIdentity`: headers are `Content-Type` plus hook headers only, even with
  `CLD_GRAPHQL_AUTH_LOCAL` set; without `as_user` headers are unchanged; 401 deletes the entry
  and exits 1.
- `TestMaskLiterals`: full values and their parts masked; short parts kept; `.env` values of
  8+ characters masked, shorter kept; longest first; JWT pattern; `logs` masks values from the
  session mask list; existing patterns unchanged.
- `TestGraphqlIdentities`: lists roles and cached raw users with state; never prints a value.

`tests/test_graphql_mcp.py`: `as_user` and `refresh_identity` become flags plus `--` before the
positionals for `query` and `introspect`; empty `as_user` sends exactly today's argv;
`list_identities` parses lines, including short lines.

`tests/test_runner_entrypoints_sh.py`, on the full entrypoint harness (test-jobs 9.1):
`identity-hook.sh` with a temp `HOME` and `VIRTUAL_ENV` and no `SECRETS_FILE` in its env
(the default is used, rewritten to a temp path): runs in the project directory; sees `.env`
values; `CLD_GRAPHQL_*` unset; `CLD_HOOK_*` survive a `.env` that sets them; a `timeout` and
`bash` planted in `$VIRTUAL_ENV/bin` are not used; a `sleep` hook is killed at the timeout
with exit 124. Both entrypoints unset `CLD_IDENTITY_*`.

### 11.2 Manual host checks (add to `MANUAL_TESTS.md`)

1. With a stub hook in a test repo's profile: `query as_user=<role>` returns data; the server
   logs show the header arrived; `get_server_logs` shows `<redacted:...>` in its place.
2. A second query within the lifetime does not run the hook (`identities` shows the same
   `expires_in`, counting down).
3. A hook giving 60 s with a 30 s margin re-mints after about 30 s.
4. A hook that sleeps longer than the timeout: error after the timeout, and `docker top
   cld_gql_<session>` shows no leftover hook process.
5. `ps -ef` on the host during a mint shows no header value.
6. `stop_server` removes `~/.cld/identities/<session>/`; removing the session container and
   then `cld-brokerctl sweep` removes it too.
7. A server from the old image gives the rebuild error; one started by the old broker gives
   the restart error.
8. Edit the hook name in the profile on trunk while a server runs: queries keep using the
   labelled hook until `restart`.
9. diskuze and lide, once their hooks exist: `me` returns the requested user.

## 12. Implementation packages (dependency order)

The cross-design order is profile section 10: package 1 is its step 3, package 2 is in
its step 2, packages 3 to 6 are its step 10, package 7 is in its step 11.

1. **Shared masker.** `mask_output(data, literals)`, `output_literals`, the JWT pattern, the
   session mask list reader, `.env` literals through the containment resolver. Wire it into
   graphql `query`, `introspect`, `logs` and the start log dump. Exit: `TestMaskLiterals`
   passes and all existing broker tests pass unchanged.
2. **Hook wrapper in the image.** `graphqlserver/identity-hook.sh`, the Dockerfile `COPY` and
   `LABEL graphqlserver.api=2`, the unset of `CLD_IDENTITY_*` in both entrypoints, README.
   Exit: the new shell tests pass.
3. **Hook runner and reply validation.** Reads the start labels, builds the `docker exec`
   call, enforces the backstop, validates the reply and maps exit codes. Needs profile
   package 5 (labels). Exit: `TestRunIdentityHook` and `TestResolveIdentity` pass.
4. **Cache.** Lookup, lock, atomic write, expiry and margin, container id check, cleanup in
   `stop`, `sweep_identity_cache` (called from the gql sweep and the `--sweep` entry).
   Exit: `TestIdentityCache` passes.
5. **Query, introspect and identities ops.** Flag parsing, header replacement, 401 handling,
   `identities` output, `broker.conf.sample` settings. Exit: `TestParseGraphqlFlags`,
   `TestQueryWithIdentity` and `TestGraphqlIdentities` pass.
6. **Container client.** MCP parameters, `list_identities`, CLI help text. Exit: the new
   `tests/test_graphql_mcp.py` cases pass.
7. **Docs and skill.** `docs/graphql-mcp.md`, the `broker-graphql-server` skill (identities,
   new error rows, what works in diskuze and lide today), a `docs/design-cld-broker.md`
   amendment, `MANUAL_TESTS.md`. Exit: manual checks 1 to 8 pass on the host.

Follow-ups outside this repo: the diskuze server command and hook, then the lide hook (after
ticket question 2 and containment).

## 13. Assumptions and open questions

Assumptions:

- The diskuze and lide facts in section 9 are correct (unverified here).
- The repo `.env` points the server at dev data, so impersonating any user is acceptable.
- lide test accounts exist and have no production value.
- The host `python3` is 3.11 or newer (the broker already needs `tomllib`), so
  `datetime.fromisoformat` reads RFC 3339 with `Z`.
- Container labels include the image's labels, so `docker inspect` of `cld_gql_<session>`
  shows `graphqlserver.api`.

Open questions:

1. lide public API: are there test accounts, or may lide add a dev-only bypass? (Ticket
   question 2.) Section 9 covers both.
2. Running the hook in the server container (D1) instead of as a plain host process: is that
   acceptable? It keeps identities to `local` until a host-process variant exists.
3. Does anyone need admin identities in diskuze? The service JWT never grants admin; an admin
   token would need its own secret and hook branch.
4. diskuze: the `Service-Authorization` value format, and the format of
   `JWT_SERVICE_SECRETS__lide`.
5. Are identities wanted for deployed aliases later? That needs the host-process hook and the
   stricter trust it brings.
