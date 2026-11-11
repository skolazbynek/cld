# cld host broker

The host-side glue that lets a `cld task-agent` or ticket
container trigger a fixed set of host-side actions -- run the target repo's
tests, enumerate cld containers and registered repos, launch task-agents,
drive a GraphQL server -- without ever seeing the secrets. The `task-agent`
launcher action's `start` reaches a repo mounted in the caller or registered on
the host (`cld repos add`); see `../docs/design-ticket-task-agents.md`.
`graphql`'s role gate, like `run-tests`', is prompt-based only (persona/skill
text), not mechanical (see `../docs/design-cld-broker.md` §16). Agents and
task-agents are instructed (persona prompts, not enforced here) to only run
`run-tests` or `graphql` with their master's explicit per-invocation
authorization.

A container asks over SSH; a dedicated, single-purpose `sshd` answers with a
forced command (`cld-broker.py`). It serves **any repo that has a running
agent, task-agent or ticket container** -- no whitelist: it resolves the target repo
from the calling container's `org.cld.repo-root` label, resolves which change
that session is on (reading the jj store, or the session branch of a git repo; never touching the working copy),
and for `run-tests` runs the standalone [`runtests`](../runtests/) container
against it, mounting only the repo's jj/git store (no checkout) and a private
copy of its `.env` into that ephemeral runner. Every host `jj` call goes through
`host_jj`, which first checks the repo. A repo is refused with exit 3, before any
host `jj` runs or any container starts, when a jj id (`.jj/repo/config-id`,
`.jj/workspace-config-id`) is missing (start a ticket on it once), when its git
dir holds a foreign `commondir` or a `modules/` folder, when `git_target` points
outside the repo, or when the host jj is older than 0.38.
`claude` only ever sees the streamed output. See `../docs/design-cld-broker.md`
for the full design.

```
container: cld broker run-tests -k login -x tests/
   └─ssh (restricted key)─▶ dedicated sshd ─ForceCommand▶ cld-broker.py
        ├─ validate: action=run-tests, session=a.<ticket>.<agent> or t.<ticket>
        ├─ REPO = docker inspect <session> -> org.cld.repo-root label
        ├─ REV  = jj -R <repo> --ignore-working-copy log -r "<session>@"   (workspace tip, store-reading only;
        │         git repo: the commit of refs/heads/<session>, committed work only)
        ├─ .env = <repo>/.env  (or under pyproject_dir from <repo>/.cld/config.toml)
        └─ docker run --rm runtests -e REVISION=REV <store rows> <.env copy> -- -k login -x tests/
   ◀───────────────── stdout / stderr / exit code ─────────────────┘
```

## Files

| File | Role |
|---|---|
| `cld-broker.py` | the `ForceCommand` target; the only thing the key can run |
| `broker.conf.sample` | broker-wide config (image, PATH, ssh-agent socket); copy to `/etc/cld/broker.conf`. Read as plain `KEY=VALUE` lines, never sourced: shell quoting works, `$VAR` expansion does not |
| `sshd_cld_broker.conf` | sample config for the dedicated `sshd` instance |
| `keygen.sh` | generate the broker keypair, host key, and `authorized_keys` |
| `cld-cld-brokerctl.sh` | operate the sshd: `start` / `restart` / `shutdown` / `status` / `logs`; `run` an action from the host |

## Operating

After first-time setup, symlink `cld-cld-brokerctl.sh` onto your PATH and drive the
daemon with it — it starts sshd detached, tracks it by PID file, and logs to a
file, so there are no sshd flags to remember:

```
cld-brokerctl start | restart | shutdown | status | logs [N] | graphql-sweep | run ...
```

`graphql-sweep` runs the broker's own sweep (`cld-broker.py graphql-sweep`), so it
reaches a repo's jj only through the broker's store checks.

It reads `$CLD_BROKER_DIR` (default `~/.cld/broker`). `restart` is the one to run
after editing the broker config or rebuilding the `runtests` image.

### Running an action from the host

For debugging, run any action directly, without sshd and without a container:

```
cld-brokerctl run [-C <repo>] [-r <revset>] [-s <session>] <action> [args...]
cld-brokerctl run run-tests -k login -x tests/
cld-brokerctl run -r main graphql start
```

It runs the installed `ForceCommand` from the sshd config, with that config's
`SetEnv` lines and an otherwise cleared environment, so `broker.conf` and its
`PATH` apply as they do for a container. The flags supply the `Caller` (see
"Adding an action") that labels supply on the ssh route:

| Flag | Caller member | Default |
|---|---|---|
| `-C` | `repo` | the repo containing the current directory |
| `-r` | `revision` | `@`, your working copy (jj snapshots it first, so uncommitted edits count) |
| `-s` | `name` | `cld_host_<repo dir name>` |

Everything after the action goes to it as given. An action's `--repo` selector
is refused: it picks a ticket container's mounted repo, so on the host use `-C`.
The exception is `task-agent start --repo <name>`, which resolves `<name>` in
the host registry.
A graphql server started this way has no container named after its session,
so the orphan cleanup reaps it on the next `graphql start` or `status` from any
caller.

## Setup (once, host-side)

Assumes the `runtests` image is built (`../runtests/build.sh`) and the docker
socket is **not** exposed to any container (that is what keeps the broker as
claude's only host channel). The broker refuses (exit 3) a runner or server image
whose label `org.cld.jj-version` is missing or older than 0.38: rebuild an image
built before that label with its `build.sh`.

1. **Keys.** `./keygen.sh /etc/cld` (or any dir), then note the printed
   `known_hosts` line.
2. **Config.** Copy `broker.conf.sample` to `/etc/cld/broker.conf` and
   set `RUNTESTS_IMAGE` + `PATH`. `PATH` must include both `docker` and `cld`
   (the `task-agent` action runs host-side `cld task-agent`). There
   is nothing per-repo to set -- the repo and its secrets path are resolved per
   request. Set `SSH_AUTH_SOCK` too if the agents and task-agents launched
   through the broker should be able to push (see the sample for why a
   login-shell agent's socket path will not do). `VENV_CACHE_ROOT` (default
   `~/.cld/cache`) holds the venvs reused across `run-tests` and `graphql`
   runs, one folder per repo below `repos/`; see `../runtests/README.md#venv-cache`.
3. **Broker script.** Install `cld-broker.py` at `/opt/cld/cld-broker.py`
   (path referenced by `ForceCommand`), `chmod +x`.
4. **sshd.** Edit `sshd_cld_broker.conf` (`AllowUsers`, `ListenAddress` for your
   bridge gateway, key paths), then launch the dedicated instance:
   ```
   /usr/sbin/sshd -f /etc/cld/sshd_cld_broker.conf -D
   ```
   It runs the forced command as `AllowUsers`, so that user must own the target
   repos, read their `.env`s, and have docker access.

## Verify (no container needed)

The repo is resolved from the calling container's label, so the smoke test needs
a container named like the session carrying that label (a real cld container, or
a throwaway):

```
REPO=/abs/path/to/some/repo
docker run -d --name a.t1.demo --label org.cld.repo-root="$REPO" alpine sleep 300
jj -R "$REPO" bookmark create a.t1.demo -r @

payload=$(printf '%s\0' -k some_test -x | base64 -w0)
ssh -i /etc/cld/broker_key \
    -o UserKnownHostsFile=<known_hosts> -o StrictHostKeyChecking=yes \
    -p 2222 <user>@172.17.0.1 -- "run-tests a.t1.demo $payload"

# rejected: anything but a defined action, or a malformed/unknown session
ssh ... -- "deploy a.t1.demo $payload"        # -> denied (bad action)
ssh ... -- "run-tests ../../etc/passwd $payload"    # -> denied (bad session)
ssh ... -- "run-tests a.t1.nope $payload"     # -> denied (no such container)

docker rm -f a.t1.demo; jj -R "$REPO" bookmark delete a.t1.demo
```

## Adding an action

The broker dispatches `$SSH_ORIGINAL_COMMAND` (`<action> <session> <base64-argv>`)
to a Python function named `action_<name>` (hyphens → underscores). To add one,
define a function in `cld-broker.py` — that's the whole change:

```python
def action_lint(caller: Caller, argv: list[str]) -> None:
    repo = caller.repo("")
    rev, secrets = resolve_test_context(caller, repo)
    image = _setting("LINT_IMAGE") or "runlint:latest"
    _exec(["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
           *store_container_args(repo, image), "-e", f"REVISION={rev}", image, *argv])
```

It receives the `Caller` the dispatcher built and the decoded argv as a list.
`Caller` is the seam between the two domains of the script: the caller source
answers who called and what they may reach, the actions consume the answers as
plain values. It has five members, all resolved lazily, so read-only actions
don't depend on inputs they don't use:

| Member | Meaning | `LabelCaller` (the sshd route) |
|---|---|---|
| `name` | session id; names the caller's own resources | the container name |
| `repo(selector)` | host repo to act on; `selector` is the `--repo` name | `org.cld.repo-root`, or `org.cld.repo.<name>` for a ticket |
| `revision(repo)` | caller's current commit, `None` if unresolvable | jj `<session>@` in that repo |
| `mounted_repo(selector)` | host repo mounted in the caller under that name, `None` if none | `org.cld.repo.<name>` for a ticket, `None` for a v1 kind |
| `ssh_agent()` | host ssh-agent socket its runs may use, `""` for none | the socket cld mounted into the caller (`cld/docker.py:stage_ssh_agent`) |

`run-tests` calls `resolve_test_context` for the revision and the secrets
`.env` / `PROJECT_SUBDIR`. Feeding actions from another source (e.g. inputs
given on a host CLI) means implementing `Caller` once; no action changes. The script uses the Python standard library only
(3.12+), so the host needs no venv. Call it from a container with `cld broker lint <args>`
(action name is the first argument; `run-tests` needs no special-casing). An
action name that has no matching function is denied, so enabling/disabling is
just defining/removing the function.

**Built-in actions:** `run-tests` (pytest in the `runtests` container),
`list-containers` (read-only cld-container enumeration for the messenger /
`cld task-agent status`), `list-repos` (read-only host registry for `cld repos`,
marking which entries the caller mounts), `task-agent` (`<op>` -- the `cld
task-agent` lifecycle verbs), `run` and `chain` (`start|status|cancel` -- ticket
callers start `cld run` / `cld chain run` on a commit inside their own tree, at most 2
live jobs at a time, and see or cancel only their own; see
`../docs/design-broker-run-chain.md`), and `graphql` (`<op> [args...]` -- start/stop/restart/status/
logs for the calling session's own GraphQL server, plus query/introspect/
endpoints against it or a credentialed alias; see
`../docs/impl-graphql-broker-plan.md` and `../docs/graphql-mcp.md`). The
`task-agent` action's `start` takes a leading `--repo <name>` and resolves it
through `resolve_task_agent_repo`: a repo mounted in the caller first, then the
host registry. Its other ops act on an agent by name, which carries its repo,
so they need none. `graphql`, like `run-tests`, stays on the caller's mounted
repos (`Caller.repo`).

`run-tests` and `graphql start` (and `restart`) resolve the session's revision first. On a jj
repo that is the session workspace's tip, `<session>@`, so uncommitted edits are part of the run.
On a git repo it is the commit of the branch `refs/heads/<session>` (read with host `git`
after the same store checks as the mount), and the runner checks it out through a detached
`git worktree`: **on git only committed work is tested** -- commit first, edits in the working
tree are not seen. A git session without that branch gets `cannot resolve revision` (exit 3).
`graphql`'s read-only ops (`status`/`logs`/`stop`/`endpoints`) do not need a revision at all.
The host route (`cld-brokerctl run`, `CliCaller`) takes a jj revset and still works on jj repos only.
`task-agent start` refuses a git repo (`denied: task-agents need a jj repo`, exit 3) before any
container starts, and `run` / `chain` still refuse one: task-agents and their reach check have no
git version.

`task-agent` is the only action that creates a container with a caller-chosen file
mounted inside it, so it also polices its argv: `--force` is denied (overriding a
reap-readiness refusal stays a human act), a caller-supplied `--parent` is denied and
the validated `$session` appended instead -- both in either spelling, `--opt` and
`--opt=value` -- and `start`'s persona positional must be a bare name (an `@ref`) -- a
path there would let a container read any host file you can. The positional check
knows which `start` options take a *separate* value (`-n`/`-p`/`--branch`/`-m`/`-r`/
`--peer`) rather than assuming every `-*` token consumes the next one, which would
let the token after an `--opt=value` slip by unchecked. A `--` token is refused, so
no token after it can reach the host as a bare file path.

## Security notes

- **Scoped, not dynamic.** The action set and `ForceCommand` are fixed host-side.
  The caller controls only the action (must map to a defined function), a
  validated session id, and the argv. The target repo is taken from the calling
  container's host-set label, not caller input -- so the caller can only reach a
  repo that already has an agent, task-agent or ticket container, never an arbitrary host path.
- **No injection.** argv arrives base64(NUL-joined) and is decoded into an argv
  array, never `eval`'d -- it can only become arguments to the action's command.
- **Blast radius.** A leaked broker key unlocks `run-tests`/`list-containers` for
  any repo that has a running agent, task-agent or ticket container (no
  per-container key isolation in this design) -- more surface than before this
  key reached agent/task-agent containers too. The `task-agent` launcher action
  reaches every repo in the host registry, which only the host operator edits.
  With
  `SSH_AUTH_SOCK` configured that includes launching an agent that holds your
  forwarded ssh-agent, so the key buys signing inside a container it spawns and
  not only `run-tests`. Leave it unset to keep every broker-launched agent
  structurally unable to push.
- **Network scope.** Bind the instance to the docker bridge gateway so only
  containers on the bridge can reach it.
- **Session name is not tied to the caller.** The broker trusts the session name
  the caller sends. It does not check that the request comes from that container.
  So a caller holding the key can act as another session: run its tests, or stop,
  restart, query or read the logs of its GraphQL server. Open item.
- **ssh-agent in runs.** `run-tests` and `graphql start` mount the caller's own
  forwarded ssh-agent (if it has one) so `git+ssh://` dependencies install.
  The code under test can then use the key for the whole run -- exactly what
  the caller container can already do, nothing more. On the host route
  (`cld-brokerctl run`) it is broker.conf's `SSH_AUTH_SOCK`.
