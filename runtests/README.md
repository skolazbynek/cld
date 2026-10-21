# runtests

A standalone, single-job container: run a project's **pytest** suite against a
specific **jujutsu (jj)** revision in an isolated workspace, without disturbing
the origin repo's current change (`@`).

It has no dependency on any other tooling — it knows only a jj store, a revision,
a secrets file, and pytest arguments.

## How it works

The repo whose jj store holds the change is mounted at `/repo`. The broker mounts only
the jj/git store there (the eleven rows of `docs/design-secrets-containment.md` section 3.1,
with the settings and the default workspace's `working_copy` read-only), never the
checkout, so the entrypoint reads nothing from `/repo` but the store. The
entrypoint runs:

```
jj --ignore-working-copy workspace add --name runtests-<id> -r <rev> "$HOME/rt-workspace"
cd "$HOME/rt-workspace" && jj new <rev>
```

(`<rev>` is `$REVISION` resolved to a commit id first), which creates an
**independent** working copy under `$HOME` sharing only `/repo/.jj/repo/store`.
jj never moves the origin's default-workspace `@` or its bookmarks, so the change
under test is materialized without touching whatever the user is working on.
`--ignore-working-copy` keeps jj from snapshotting `/repo`'s
own working copy -- the user's checkout, possibly mid-edit -- which would race
their own jj commands into divergent changes. jj then refuses to check the new
workspace out, so `jj new` inside it does that. The workspace is `forget`-ten on
exit (even on crash), again without a snapshot, so the run's byproducts leave no
commit behind. It lives under `$HOME` (not `/`) so it is writable when the
container runs with `--user`.

Secrets (DB hosts/passwords etc.) are read from a mounted env file and sourced
into pytest's environment only.

## Interface

| Input | Via | Default | Meaning |
|---|---|---|---|
| jj store | `--mount` rows at `/repo` (the broker) or `-v <repo>:/repo` (standalone) | — | repo whose store holds the change; only `.jj` and the git dir are needed |
| `REVISION` | `-e` | `@` | revset to test (any jj revset); read without a snapshot, so `@` is `/repo`'s last snapshot |
| secrets | `--mount` of the broker's private 0600 copy (the broker) or `-v <file>:/secrets/.env:ro` (standalone) | — | env file, sourced into pytest env; the broker never mounts the repo's own `.env` |
| `SECRETS_FILE` | `-e` | `/secrets/.env` | override secrets path |
| `PROJECT_SUBDIR` | `-e` | `.` | dir holding `pyproject.toml` |
| ssh-agent | `-v <socket>:/run/ssh-agent.sock -e SSH_AUTH_SOCK=/run/ssh-agent.sock` | — | key for `git+ssh://` dependencies; see [git+ssh dependencies](#gitssh-dependencies) |
| `POETRY_INSTALL_ARGS` | `-e` | `--all-extras --all-groups` | `poetry install` flags (installs test deps however declared); set e.g. `--no-root` or `--with dev` to narrow |
| `PYTEST_ADDOPTS` | `-e` | `--tb=short --disable-warnings -q --maxfail=30` | default pytest opts; explicit argv still overrides them |
| `OUTPUT_MAX_BYTES` | `-e` | `65536` | cap on returned output; only the last N bytes (the summary) are emitted |
| venv cache | `-v <dir>:/cache` (rw) | — | the repo's own host cache folder holding `venvs/`, `poetry/`, `locks/`; see [Venv cache](#venv-cache) |
| `VENV_CACHE_IMAGE_ID` | `-e` | — | image id, part of the cache key; the cache is skipped without it |
| `VENV_CACHE` | `-e` | `on` | `on`, `off`, or `rebuild` (rebuild this key's venv) |
| `VENV_CACHE_KEEP` | `-e` | `10` | most recently used venvs kept in the cache |
| pytest args | container **argv** | none | passed straight to `pytest`, override `PYTEST_ADDOPTS` |

Requires **Poetry 2.x** (the image ships 2.5.1): it reads PEP 621 `[project]`
metadata, which Poetry 1.x does not.

The container exits with pytest's exit code.

## git+ssh dependencies

A `git+ssh://` dependency (e.g. a package pinned to a GitLab repo) is fetched
by git or poetry's dulwich, both of which run `ssh` from `PATH`. The image's
`/usr/local/bin/ssh` is a wrapper (`../scripts/ssh-wrapper.sh`) that:

- gives the `--user` uid a passwd entry through nss_wrapper -- without one,
  ssh exits `No user exists for uid N`;
- never prompts (`BatchMode=yes`);
- trusts a new host key on first use, and keeps learned keys in
  `/cache/ssh/known_hosts` when the venv cache is mounted, so a host key that
  later changes fails the run.

The key comes from an ssh-agent socket at `$SSH_AUTH_SOCK`; the broker mounts
the calling container's own agent. Without one, the fetch fails with
`Permission denied (publickey)`.

## Build

```
./build.sh                     # -> runtests:latest
RUNTESTS_IMAGE=runtests:dev ./build.sh
```

The image carries the label `org.cld.jj-version`, set from the `JJ_VERSION` build
argument. The broker refuses an image whose label is missing or older than 0.38.

`build.sh` passes `../scripts` as the extra build context `scripts` (for
`venv-cache.sh`), which needs BuildKit (the default builder since Docker 23).

## Venv cache

Before `poetry install`, `venv-cache.sh` seeds the venv at `/opt/venv` from a
host cache mounted at `/cache`. The broker mounts one folder per repo, `~/.cld/cache/repos/<repo name>-<hash of
its real path>`, never the cache root: code under test writes the folder, and a venv or a wheel in it is later
run or installed with the `.env` of whichever repo restores it, so repos must not share one. The same repo's
runs share it (the same `.env`). Everything below, the download cache and the learned ssh host keys included,
is per repo, and so is the `VENV_CACHE_KEEP` limit. A cache root from before this layout (`venvs/`, `poetry/`,
`locks/`, `ssh/` directly in it) is no longer used: delete it.

- The key hashes `pyproject.toml`, `poetry.lock`, `POETRY_INSTALL_ARGS`, the
  Python and Poetry versions and `VENV_CACHE_IMAGE_ID`.
- **Hit:** `venvs/<key>` is copied to `/opt/venv`. A venv only works at the
  absolute path it was built at, so every venv is built and used at `/opt/venv`.
- **Miss:** the dependencies (not the project) are installed into `/opt/venv`,
  which is then published to `venvs/<key>`. One build runs at a time across all
  keys; the least recently used venvs beyond `VENV_CACHE_KEEP` are evicted.
- `poetry install` still runs afterwards: it installs the project itself and
  repairs any drift, taking seconds when the venv already matches.
- Any cache error falls back to a normal install. Without the mount, a
  lock file or the image id, the cache is skipped.

Poetry's download cache lives at `/cache/poetry`, so a miss downloads only new
packages. It is split per repo with the rest: poetry installs a cached wheel as it
is, so a download cache shared between repos would be as open to poisoning as the
venvs.

## Run

```
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v /path/to/repo:/repo \
  -v /path/to/repo/.env:/secrets/.env:ro \
  -e REVISION=@ \
  runtests:latest -k login -x tests/unit
```

Run the whole suite by passing no pytest args. `--user "$(id -u):$(id -g)"`
keeps any jj store writes owned by the host user; `HOME=/tmp` because that uid
may not exist in the image's `/etc/passwd`. For the same reason the entrypoint
defaults `USER`/`LOGNAME` to `runtests` if unset -- tools that call
`getpwuid()` on a passwd-less uid (e.g. Python's `getpass.getuser()`) would
otherwise fail.

## Notes

- The image bakes internal CA certs into the system trust store so TLS to the
  databases and PyPI works without a host mount.
- Non-root `pyproject.toml`? Set `PROJECT_SUBDIR`.
- Reading the revision while another process (e.g. an interactive session)
  writes the same store concurrently is supported by jj's op-log; a
  store-reading caller may lag the latest edit by a snapshot interval.
