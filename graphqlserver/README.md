# graphqlserver

A standalone, single-job container: run a project's **GraphQL server** against a
specific **jujutsu (jj)** revision in an isolated workspace, without disturbing
the origin repo's current change (`@`).

It has no dependency on any other tooling -- it knows only a jj store, a revision,
a secrets file, and a shell command that starts a server. It has no idea what
that server is (Django, Strawberry, anything that speaks HTTP).

## How it works

The repo whose jj store holds the change is mounted at `/repo`. The broker mounts only
the jj/git store there (the eleven rows of `docs/design-secrets-containment.md` section 3.1,
with the settings and the default workspace's `working_copy` read-only), never the
checkout, so the entrypoint reads nothing from `/repo` but the store. The
entrypoint runs:

```
jj --ignore-working-copy workspace add --name "$GQL_WORKSPACE" -r <rev> "$HOME/gql-workspace"
cd "$HOME/gql-workspace" && jj new <rev>
```

(`<rev>` is `$REVISION` resolved to a commit id first), which creates an
**independent** working copy under `$HOME` sharing only `/repo/.jj/repo/store`.
jj never moves the origin's default-workspace `@` or its bookmarks, so the change
under test is materialized without touching whatever the user is working on --
nor snapshotting it: see [runtests](../runtests/README.md#how-it-works) for why
and for the `jj new` step. Unlike `runtests`, this container is **long-lived** (a
server, not a one-shot command), so `$GQL_WORKSPACE` must be **deterministic**
rather than random: a `docker rm -f` sends SIGKILL, which skips this script's own `trap ...
EXIT`, so whatever manages this container's lifecycle (the broker) needs to name
the workspace itself to `jj workspace forget` it afterwards.

Secrets (DB hosts/passwords, API tokens, etc.) are read from a mounted env file
and sourced into the server's environment only.

## Interface

| Input | Via | Default | Meaning |
|---|---|---|---|
| jj store | `--mount` rows at `/repo` (the broker) or `-v <repo>:/repo` (standalone) | -- | repo whose store holds the change; only `.jj` and the git dir are needed |
| `REVISION` | `-e` | `@` | revset to serve (any jj revset); read without a snapshot, so `@` is `/repo`'s last snapshot |
| secrets | `--mount` of the broker's private 0600 copy (the broker) or `-v <file>:/secrets/.env:ro` (standalone) | -- | env file, sourced into the server's env; the broker never mounts the repo's own `.env` |
| `SECRETS_FILE` | `-e` | `/secrets/.env` | override secrets path |
| `PROJECT_SUBDIR` | `-e` | `.` | dir holding `pyproject.toml` |
| ssh-agent | `-v <socket>:/run/ssh-agent.sock -e SSH_AUTH_SOCK=/run/ssh-agent.sock` | — | key for `git+ssh://` dependencies; see [git+ssh dependencies](../runtests/README.md#gitssh-dependencies) |
| `GQL_WORKSPACE` | `-e` | `gql-$HOSTNAME` | **deterministic** jj workspace name -- set this explicitly if the caller needs to name it later |
| `GQL_COMMAND` | `-e` | *(required)* | shell command that starts the server; no guessed default |
| `GQL_PORT` | `-e` | `8000` | port the server must bind **inside** the container |
| `POETRY_INSTALL_ARGS` | `-e` | `--all-extras --all-groups` | `poetry install` flags |
| venv cache | `-v <dir>:/cache` (rw) | — | the repo's own host cache folder holding `venvs/`, `poetry/`, `locks/`; see [Venv cache](../runtests/README.md#venv-cache) |
| `VENV_CACHE_IMAGE_ID` | `-e` | — | image id, part of the cache key; the cache is skipped without it |
| `VENV_CACHE` | `-e` | `on` | `on`, `off`, or `rebuild` (rebuild this key's venv) |
| `VENV_CACHE_KEEP` | `-e` | `10` | most recently used venvs kept in the cache |

Requires **Poetry 2.x** (the image ships 2.5.1): it reads PEP 621 `[project]`
metadata, which Poetry 1.x does not.

## The one thing to get right: bind `0.0.0.0`, not `127.0.0.1`

`GQL_COMMAND` **must** bind the server to `0.0.0.0:$GQL_PORT`. Binding
`127.0.0.1` (a very common framework default) makes the server unreachable from
outside its own network namespace -- including from the host that published the
port. This is the single most likely misconfiguration when wiring a new repo's
`graphql_command`; if `graphql start` times out in its readiness probe with no
obvious error in the logs, check this first.

The container exits with the server process's exit code (the entrypoint `exec`s
into `$GQL_COMMAND`, so signals reach it directly).

## Build

```
./build.sh                        # -> graphqlserver:latest
GRAPHQL_IMAGE=graphqlserver:dev ./build.sh
```

The image carries the label `org.cld.jj-version`, set from the `JJ_VERSION` build
argument. The broker refuses an image whose label is missing or older than 0.38.

`build.sh` passes `../scripts` as the extra build context `scripts` (for
`venv-cache.sh`), which needs BuildKit (the default builder since Docker 23).

## Run

```
docker run -d --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v /path/to/repo:/repo \
  -v /path/to/repo/.env:/secrets/.env:ro \
  -e REVISION=@ \
  -e GQL_COMMAND='poetry run python manage.py runserver 0.0.0.0:$GQL_PORT' \
  -p 0:8000 \
  graphqlserver:latest
```

`--user "$(id -u):$(id -g)"` keeps any jj store writes owned by the host user;
`HOME=/tmp` because that uid may not exist in the image's `/etc/passwd`. For the
same reason the entrypoint defaults `USER`/`LOGNAME` to `graphqlserver` if unset.
`-p 0:8000` publishes an ephemeral host port -- read it back with `docker port`.

## Notes

- The image bakes internal CA certs into the system trust store so TLS to the
  databases and PyPI works without a host mount.
- Non-root `pyproject.toml`? Set `PROJECT_SUBDIR`.
- This image is normally driven by the `graphql` broker action
  (`../broker/cld-broker.py`), not run by hand -- see `../docs/graphql-mcp.md`.
- Reading the revision while another process (e.g. an interactive session)
  writes the same store concurrently is supported by jj's op-log; a
  store-reading caller may lag the latest edit by a snapshot interval.
