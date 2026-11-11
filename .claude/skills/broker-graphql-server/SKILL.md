---
name: broker-graphql-server
description: >
  Run the repo's GraphQL server live through the cld broker and query it,
  from inside a cld ticket or task-agent container: start it at your latest
  edits, introspect, run queries and mutations, restart after changes, read
  its logs. Uses the `graphql-tester` MCP tools (or `cld broker graphql`).
  Invoke whenever you need to confirm a GraphQL change works for real -- a
  schema, resolver, permission or query change. A task-agent needs its
  parent's explicit go-ahead first.
user-invocable: true
---

# Live GraphQL server through the cld broker

The broker runs the repo's GraphQL server on the host, from a fresh checkout
of your workspace, with the repo's real `.env`. You drive it with the
`graphql-tester` MCP tools (`mcp__graphql-tester__*`; load them with
ToolSearch if they are deferred). Credentials never enter this container.

**Task-agent:** use it only when your parent asked for it. Ask with `send()`
and wait otherwise.

## The loop

1. `start_server` -- starts the server at your workspace tip, uncommitted
   edits included, and waits until it answers. On a git repo it serves the
   tip of your session branch (`t.<ticket>`): commit first, working-tree edits
   are not seen. The first start after a
   dependency change installs every package (minutes); later starts reuse a
   cached environment. Calling it again while it runs only returns status.
2. `introspect` -- fetches and caches the schema, returns a summary.
   `describe_type` then gives one type's details from the cache.
3. `query` -- runs a query or mutation, with optional `variables`.
4. Edit code (git repo: commit it), then `restart_server`. The server serves the code as it was
   at start: `server_status` reports `stale: true` once your workspace has
   moved on.
5. `get_server_logs` (optional `filter_pattern` regex) for tracebacks.
6. `stop_server` when done. One server per container; it keeps running
   until stopped.

If a tool call times out or returns nothing, call `server_status`: the
start keeps going on the host.

In a ticket that mounts more than one repo, pass `repo="<name>"` to every
tool except `describe_type`. `cld repos` lists the names.

## Targets and credentials

`query` and `introspect` take `target`:

- `"local"` (default) -- your own server. Requests carry no login unless the
  user set `CLD_GRAPHQL_AUTH_LOCAL` (an `Authorization` value) or
  `CLD_GRAPHQL_COOKIE_LOCAL` in the repo's host `.env`; the broker then
  attaches them. If a query fails because it needs a signed-in user, tell the
  user -- do not try to obtain credentials yourself.
- an alias from `list_endpoints` (e.g. `"dev"`) -- a deployed server the user
  configured, with its credentials attached by the broker.
- a raw `http(s)://` URL -- only if the user allowlisted its host; never
  carries credentials.

## Without the MCP tools

The same operations exist on the command line; `--repo <name>` goes first:

```bash
cld broker graphql start | status | restart | stop | logs [N] | endpoints
cld broker graphql introspect [target]
cld broker graphql query <target> '<query>' ['<variables json>']
```

Give these the same long Bash `timeout` (`600000`) as test runs.

## Failures

| Message | What to do |
|---|---|
| `graphql_command is not set in <path>/.cld/config.toml` | The repo is not set up for a server. It is the user's host configuration: ask them to add it. Do not edit it yourself. |
| `exited before becoming healthy -- last logs:` | Read the logs in the error. An import or startup error from your change: fix it and start again. A `/secrets/.env: line N: ...: command not found` line means an unquoted value in the host `.env`. Anything else: tell the user. |
| `did not become healthy within Ns` | Usually the server command binds `127.0.0.1` or the health path is wrong -- host configuration. Tell the user. |
| `already running for repo <path>, not <path>` | Another repo's server is running. `stop_server` (any mounted `repo` name stops it), then start again. |
| `no local graphql server running` / `no graphql server for ...` | `start_server` first. |
| `unknown graphql alias` / `not in GRAPHQL_URL_ALLOWLIST` | Use a name from `list_endpoints`. |
| `response was truncated` | Ask for fewer fields or items. |
| `graphql endpoint returned HTTP <n>` | The response body follows in the error; it usually names the problem. |
| `No such image: graphqlserver:latest` / `Unable to find image` | The image is not built on the host. Ask the user to run `graphqlserver/build.sh`. |
| A git dependency fails to install (ssh errors in the start error) | Same causes and answers as in the `broker-run-tests` skill's table. |
| `the cld broker is not configured for this container`, ssh errors | As in the `broker-run-tests` skill: host side, tell the user. |

Reference: `docs/graphql-mcp.md` in the cld repo.
