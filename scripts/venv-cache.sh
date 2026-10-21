#!/usr/bin/env bash
# venv-cache: restore a cached, dependencies-only venv into $VIRTUAL_ENV, or
# build and publish one on a miss. Shared by the runtests and graphqlserver
# images; the caller's own `poetry install` still runs afterwards -- it
# installs the project itself and repairs any drift, so a stale cache can cost
# time but never correctness.
#
# Run from the directory holding pyproject.toml. Never fails the caller: any
# cache error falls back to an empty venv that `poetry install` fills normally.
#
#   VIRTUAL_ENV          venv path, fixed per image (a venv only works at the
#                        absolute path it was built at, so it is copied back there)
#   VENV_CACHE_DIR       cache dir holding venvs/ poetry/ locks/ (default: /cache);
#                        used only when it is a mount point. The broker mounts a
#                        folder of its own per repo here, never one shared by repos:
#                        a restored venv runs with its repo's .env
#   VENV_CACHE           on | off | rebuild (default: on)
#   VENV_CACHE_KEEP      number of most recently used venvs kept (default: 10)
#   VENV_CACHE_IMAGE_ID  image id, part of the key -- system libs under compiled
#                        packages change with the image; the cache is skipped without it
#   POETRY_INSTALL_ARGS  install flags, part of the key
set -uo pipefail

: "${VENV_CACHE_DIR:=/cache}"
: "${VENV_CACHE:=on}"
: "${VENV_CACHE_KEEP:=10}"
: "${VENV_CACHE_IMAGE_ID:=}"
: "${POETRY_INSTALL_ARGS:=}"
# The base interpreter, not whatever `python3` resolves to once $VIRTUAL_ENV/bin
# (first on PATH) is populated.
PYTHON=/usr/bin/python3

venvs="$VENV_CACHE_DIR/venvs"
locks="$VENV_CACHE_DIR/locks"

warn() { echo "[venv-cache] $*" >&2; }

reset_venv() {
    find "$VIRTUAL_ENV" -mindepth 1 -delete && "$PYTHON" -m venv "$VIRTUAL_ENV"
}

cache_key() {
    {
        cat pyproject.toml poetry.lock
        echo "$POETRY_INSTALL_ARGS"
        readlink -f "$PYTHON"
        "$PYTHON" -VV
        poetry --version
        echo "$VENV_CACHE_IMAGE_ID"
    } | sha256sum | cut -c1-32
}

# Evict the least recently used venvs beyond VENV_CACHE_KEEP. Caller holds the
# build lock, so every .tmp-* dir is a leftover of a killed build.
evict() {
    rm -rf "$venvs"/.tmp-*
    (
        flock -x 9
        find "$venvs" -mindepth 1 -maxdepth 1 -type d -not -name '.*' -printf '%T@ %p\n' \
            | sort -rn | tail -n +"$((VENV_CACHE_KEEP + 1))" | cut -d' ' -f2- \
            | xargs -r rm -rf
    ) 9>"$locks/venvs.lock"
}

# Copy venvs/<key> into $VIRTUAL_ENV under a shared lock, so eviction cannot
# delete it mid-copy. The touch records the use for eviction. The entries are
# copied, not the dir itself: $VIRTUAL_ENV is root-owned in the images, so
# `cp -a dir/.` fails setting its timestamps and every hit became a miss.
restore() {
    (
        flock -s 9
        [ -d "$venvs/$1" ] || exit 1
        touch "$venvs/$1"
        find "$venvs/$1" -mindepth 1 -maxdepth 1 -exec cp -a -t "$VIRTUAL_ENV/" {} +
    ) 9>"$locks/venvs.lock"
}

# One build at a time across all keys: parallel writes to poetry's download
# cache are not known to be safe, and misses are rare.
build() {
    local key="$1" tmp="$venvs/.tmp-$1-${HOSTNAME:-$$}"
    (
        flock -x 8
        if [ "$VENV_CACHE" != rebuild ] && restore "$key"; then
            warn "hit after waiting for another build: $key"
            exit 0
        fi
        warn "miss, building: $key"
        reset_venv || exit 1
        # shellcheck disable=SC2086  -- intentional word-splitting into separate flags
        poetry install --no-interaction --no-root $POETRY_INSTALL_ARGS || exit 1
        cp -a "$VIRTUAL_ENV" "$tmp" || exit 1
        (
            flock -x 9
            rm -rf "${venvs:?}/$key" && mv "$tmp" "$venvs/$key" && touch "$venvs/$key"
        ) 9>"$locks/venvs.lock" || exit 1
        evict
    ) 8>"$locks/build.lock"
}

main() {
    [ "$VENV_CACHE" != off ] || { warn "disabled"; return 1; }
    mountpoint -q "$VENV_CACHE_DIR" || { warn "$VENV_CACHE_DIR not mounted, skipping"; return 1; }
    [ -n "$VENV_CACHE_IMAGE_ID" ] || { warn "VENV_CACHE_IMAGE_ID unset, skipping"; return 1; }
    [ -f poetry.lock ] || { warn "no poetry.lock, skipping"; return 1; }
    mkdir -p "$venvs" "$locks" || return 1
    local key
    key=$(cache_key) || return 1
    if [ "$VENV_CACHE" != rebuild ] && restore "$key"; then
        warn "hit: $key"
        return 0
    fi
    build "$key"
}

main || reset_venv
