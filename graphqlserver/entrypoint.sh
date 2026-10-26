#!/usr/bin/env bash
# graphqlserver entrypoint: materialize a jj revision in an isolated workspace
# and exec a caller-supplied command that serves GraphQL over HTTP. Follows
# runtests/entrypoint.sh line for line -- see that file for the rationale
# behind each step; this one differs only where a long-lived server needs
# something a one-shot pytest run does not (a deterministic workspace name
# the broker can name later, and a port to publish).
#
#   REVISION             revset to serve (default: @). Resolved without snapshotting
#                        /repo's working copy, so `@` means its last snapshot.
#   PROJECT_SUBDIR       dir under the workspace holding pyproject.toml (default: .)
#   SECRETS_FILE         env file sourced into the server's environment (default: /secrets/.env)
#   GQL_WORKSPACE         deterministic jj workspace name (default: gql-$HOSTNAME) -- must be
#                         deterministic, not random, so the broker can `workspace forget` it
#                         by name after a `docker rm -f` that skips this script's own trap
#   GQL_COMMAND           shell command that starts the server (required, no guessed default)
#   GQL_PORT              port the server binds to *inside* the container (default: 8000)
#   POETRY_INSTALL_ARGS   flags for `poetry install` (default: --all-extras --all-groups)
#   VENV_CACHE*           venv cache controls, see venv-cache.sh (the cache is
#                         used only when the broker mounts it at /cache).
#
# Only the repo's jj/git store is bind-mounted at /repo -- no checkout files, and the
# settings and the default workspace's working_copy are read-only (docs/design-secrets-
# containment.md section 3.1); the isolated working copy is created under $HOME via
# `jj workspace add`, which never touches the store's default workspace @ or bookmarks.
# The only secrets source is the broker's private copy of .env, mounted at /secrets/.env.
set -euo pipefail

: "${REVISION:=@}"
: "${PROJECT_SUBDIR:=.}"
: "${SECRETS_FILE:=/secrets/.env}"
: "${GQL_WORKSPACE:=gql-${HOSTNAME:-$$}}"
: "${GQL_PORT:=8000}"
: "${POETRY_INSTALL_ARGS:=--all-extras --all-groups}"
if [ -z "${GQL_COMMAND:-}" ]; then
    echo "[graphqlserver] GQL_COMMAND is required -- no guessed server command" >&2
    exit 1
fi

export HOME="${HOME:-/tmp}"
# The host uid passed via --user has no /etc/passwd entry in this image, so
# any getpwuid() lookup (Python's getpass.getuser(), some git paths, etc.)
# would fail; USER/LOGNAME are what those tools check first, ahead of NSS.
export USER="${USER:-graphqlserver}"
export LOGNAME="${LOGNAME:-$USER}"

# jj needs an author identity to create the workspace working-copy commit.
export JJ_CONFIG=/tmp/jj-config.toml
printf '[user]\nname = "graphqlserver"\nemail = "graphqlserver@localhost"\n' > "$JJ_CONFIG"

cd /repo
# Before any jj call: jj would rewrite the read-only config ids otherwise.
seed-secure-config.sh /repo
# Workspace dir lives under $HOME: with `--user` (the broker path always uses
# it) the container user cannot mkdir at / (root-owned), but $HOME is writable.
work="$HOME/gql-workspace"
# Best effort only -- `docker rm -f` sends SIGKILL and this trap does not run,
# which is exactly why the broker's `stop`/`restart` forgets $GQL_WORKSPACE
# explicitly by its (deterministic) name rather than relying on this.
# --ignore-working-copy wherever /repo is touched, as in runtests: a plain jj
# command there would snapshot a checkout that is not mounted, and fails instead.
trap 'jj -R /repo --ignore-working-copy workspace forget "$GQL_WORKSPACE" >/dev/null 2>&1 || true' EXIT

rev=$(jj --ignore-working-copy log --no-graph -n1 -r "$REVISION" -T commit_id)
# The caller may have snapshotted again since REVISION was resolved. Building on
# its now-hidden old commit would make that commit visible again -- a divergent
# twin of the caller's change -- so take the change's current commit instead.
if change=$(jj --ignore-working-copy log --no-graph -r "$rev" -T 'if(hidden, change_id)') && [ -n "$change" ]; then
    rev=$(jj --ignore-working-copy log --no-graph -n1 -r "change_id($change)" -T commit_id)
    [ -n "$rev" ] || { echo "[graphqlserver] $REVISION was abandoned" >&2; exit 1; }
fi
# Without a snapshot jj creates the workspace but refuses to check it out;
# `jj new` inside it does the checkout. Any other failure is real.
err=$(jj --quiet --ignore-working-copy workspace add --name "$GQL_WORKSPACE" -r "$rev" "$work" 2>&1) ||
    case "$err" in
        *"must be able to update the working copy"*) ;;
        *) echo "$err" >&2; exit 1 ;;
    esac
cd "$work"
jj --quiet new "$rev"
cd "$work/$PROJECT_SUBDIR"

if [ -f "$SECRETS_FILE" ]; then
    set -a; . "$SECRETS_FILE"; set +a
    # The broker's own graphql credentials (cld-broker.py:lookup_alias): code
    # under test has no use for them, so keep them out of its environment.
    unset "${!CLD_GRAPHQL_@}"
else
    echo "[graphqlserver] WARN: secrets file $SECRETS_FILE not found; running without it" >&2
fi

# Install noise goes to stderr (docker logs), not stdout -- stdout is reserved
# for the server process this script execs into, which the broker's `logs`
# action reads back as the deliverable stream.
echo "[graphqlserver] installing dependencies (poetry install $POETRY_INSTALL_ARGS)..." >&2
# Seeds $VIRTUAL_ENV from the cache (never fails); the install below then only
# adds the project itself. See scripts/venv-cache.sh for VENV_CACHE*.
POETRY_INSTALL_ARGS="$POETRY_INSTALL_ARGS" venv-cache.sh >&2
# shellcheck disable=SC2086  -- intentional word-splitting into separate flags
poetry install --no-interaction $POETRY_INSTALL_ARGS >&2

export PORT="$GQL_PORT"
export GQL_PORT
echo "[graphqlserver] starting: $GQL_COMMAND (port $GQL_PORT)" >&2
# exec so the server's PID replaces this shell's -- signals (docker stop)
# reach it directly, and its exit code becomes the container's.
exec bash -c "$GQL_COMMAND"
