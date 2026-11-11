#!/usr/bin/env bash
# runtests entrypoint: materialize a jj revision in an isolated workspace and
# run pytest against it. All args are passed straight through to pytest.
#
#   REVISION            revset to test (default: @). Resolved without snapshotting
#                       /repo's working copy, so `@` means its last snapshot.
#   PROJECT_SUBDIR      dir under the workspace holding pyproject.toml (default: .)
#   SECRETS_FILE        env file sourced into pytest's environment (default: /secrets/.env)
#   POETRY_INSTALL_ARGS flags for `poetry install` (default: --all-extras --all-groups,
#                       so test deps get installed however they're declared -- poetry
#                       groups or PEP 621 extras). Override e.g. for --no-root.
#   PYTEST_ADDOPTS      default pytest opts (default: short tracebacks, quiet, no
#                       warnings, stop after 30 failures -- keeps output small).
#                       Explicit args passed to the container still override these.
#   OUTPUT_MAX_BYTES    cap on returned output; only the last N bytes (which hold
#                       pytest's summary) are emitted, with a truncation notice.
#   VENV_CACHE*         venv cache controls, see venv-cache.sh (the cache is
#                       used only when the broker mounts it at /cache).
#
# A git store (no .jj) is checked out the same way through a detached `git worktree`, at the
# commit REVISION names -- committed work only, the broker resolves the session's branch.
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
: "${POETRY_INSTALL_ARGS:=--all-extras --all-groups}"
# Bound pytest output by default. PYTEST_ADDOPTS is combined with the project's own
# config and the args passed to the container, with the latter winning -- so a caller
# can still ask for --tb=long or a different --maxfail.
: "${PYTEST_ADDOPTS:=--tb=short --disable-warnings -q --maxfail=30}"
export PYTEST_ADDOPTS
: "${OUTPUT_MAX_BYTES:=65536}"
export HOME="${HOME:-/tmp}"
# The host uid passed via --user has no /etc/passwd entry in this image, so
# any getpwuid() lookup (Python's getpass.getuser(), some git paths, etc.)
# would fail; USER/LOGNAME are what those tools check first, ahead of NSS.
export USER="${USER:-runtests}"
export LOGNAME="${LOGNAME:-$USER}"

# jj needs an author identity to create the workspace working-copy commit.
export JJ_CONFIG=/tmp/jj-config.toml
printf '[user]\nname = "runtests"\nemail = "runtests@localhost"\n' > "$JJ_CONFIG"

cd /repo
# Before any jj call: jj would rewrite the read-only config ids otherwise.
seed-secure-config.sh /repo
# Container hostname is a unique per-container id; keeps the workspace name
# collision-free across concurrent runs without a SIGPIPE-prone random pipe.
ws="runtests-${HOSTNAME:-$$}"
# Workspace dir lives under $HOME: with `--user` (the broker path always uses
# it) the container user cannot mkdir at / (root-owned), but $HOME is writable.
work="$HOME/rt-workspace"
if [ ! -d /repo/.jj ]; then
    # A git store: the broker resolved REVISION to a commit of the session branch, so only
    # committed work is tested. A detached worktree under $HOME (unique per run) checks it
    # out; --git-dir skips the ownership check on /repo, which the mount makes root-owned.
    work="$HOME/$ws"
    rev=$(git --git-dir=/repo/.git rev-parse --verify --quiet "$REVISION^{commit}") ||
        { echo "[runtests] cannot resolve $REVISION in the git store" >&2; exit 1; }
    trap 'git --git-dir=/repo/.git worktree remove --force "$work" >/dev/null 2>&1 || true' EXIT
    git --git-dir=/repo/.git worktree add --quiet --detach "$work" "$rev"
    cd "$work"
else
    # --ignore-working-copy wherever /repo is touched: a plain jj command there would
    # snapshot /repo's own working copy -- the host user's checkout, which is not mounted,
    # so the snapshot would record every file as deleted -- and its read-only working_copy
    # makes such a command fail instead.
    # Forgetting from inside the workspace would snapshot it instead, keeping the
    # run's byproducts as a stray commit (or failing if the run went stale).
    trap 'jj -R /repo --ignore-working-copy workspace forget "$ws" >/dev/null 2>&1 || true' EXIT

    rev=$(jj --ignore-working-copy log --no-graph -n1 -r "$REVISION" -T commit_id)
    # The caller may have snapshotted again since REVISION was resolved. Building on
    # its now-hidden old commit would make that commit visible again -- a divergent
    # twin of the caller's change -- so take the change's current commit instead.
    if change=$(jj --ignore-working-copy log --no-graph -r "$rev" -T 'if(hidden, change_id)') && [ -n "$change" ]; then
        rev=$(jj --ignore-working-copy log --no-graph -n1 -r "change_id($change)" -T commit_id)
        [ -n "$rev" ] || { echo "[runtests] $REVISION was abandoned" >&2; exit 1; }
    fi
    # Without a snapshot jj creates the workspace but refuses to check it out;
    # `jj new` inside it does the checkout. Any other failure is real.
    err=$(jj --quiet --ignore-working-copy workspace add --name "$ws" -r "$rev" "$work" 2>&1) ||
        case "$err" in
            *"must be able to update the working copy"*) ;;
            *) echo "$err" >&2; exit 1 ;;
        esac
    cd "$work"
    jj --quiet new "$rev"
fi
cd "$work/$PROJECT_SUBDIR"

if [ -f "$SECRETS_FILE" ]; then
    set -a; . "$SECRETS_FILE"; set +a
    # The broker's own graphql credentials (cld-broker.py:lookup_alias): code
    # under test has no use for them, so keep them out of its environment.
    unset "${!CLD_GRAPHQL_@}"
else
    echo "[runtests] WARN: secrets file $SECRETS_FILE not found; running without it" >&2
fi

# Buffer install + pytest into one log so we can cap what the caller sees. Markers
# go to stderr live; the log holds the deliverable. Install failure skips pytest.
log=/tmp/runtests.out
rc=0
echo "[runtests] installing dependencies (poetry install $POETRY_INSTALL_ARGS)..." >&2
# Seeds $VIRTUAL_ENV from the cache (never fails); the install below then only
# adds the project itself. See scripts/venv-cache.sh for VENV_CACHE*.
POETRY_INSTALL_ARGS="$POETRY_INSTALL_ARGS" venv-cache.sh >"$log" 2>&1
# shellcheck disable=SC2086  -- intentional word-splitting into separate flags
poetry install --no-interaction $POETRY_INSTALL_ARGS >>"$log" 2>&1 || rc=$?
if [ "$rc" -eq 0 ]; then
    echo "[runtests] running: pytest ${*:-<all tests>}  (PYTEST_ADDOPTS=$PYTEST_ADDOPTS)" >&2
    poetry run pytest "$@" >>"$log" 2>&1 || rc=$?
fi
# Return only the last OUTPUT_MAX_BYTES -- pytest's summary (and any install error)
# is at the end -- so a huge failing run can't flood the caller's context.
total=$(wc -c <"$log")
if [ "$total" -gt "$OUTPUT_MAX_BYTES" ]; then
    echo "[runtests] output truncated to last $OUTPUT_MAX_BYTES of $total bytes (raise OUTPUT_MAX_BYTES for more)" >&2
    tail -c "$OUTPUT_MAX_BYTES" "$log"
else
    cat "$log"
fi
exit "$rc"
