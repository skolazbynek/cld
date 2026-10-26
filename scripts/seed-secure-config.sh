#!/usr/bin/env bash
# seed-secure-config: give jj a per-repo config record for the store at $1, so no
# jj call rewrites the store's config ids. Shared by the runtests and graphqlserver
# images; run before the first jj call on the store.
#
# jj keeps per-repo config under its user config dir, keyed by the id in
# .jj/repo/config-id and .jj/workspace-config-id. With no metadata.binpb there,
# jj takes the repo as new and rewrites the id file by rename -- which fails with
# EBUSY on the read-only bind the broker mounts over it (docs/design-secrets-
# containment.md D3). The images' $HOME is always fresh, so an empty record is
# seeded: jj fills in its path there and leaves the store alone (jj
# lib/src/secure_config.rs, SecureConfig::handle_metadata_path).
set -euo pipefail

store=$1
config="${XDG_CONFIG_HOME:-$HOME/.config}/jj"

seed() {  # <kind dir> <id file>
    [ -f "$2" ] || return 0
    local id
    id=$(<"$2")
    # The id becomes a path; a malformed one is left for jj to reject.
    [[ $id =~ ^[0-9a-fA-F]{20}$ ]] || return 0
    mkdir -p "$config/$1/$id"
    touch "$config/$1/$id/metadata.binpb"
}

seed repos "$store/.jj/repo/config-id"
seed workspaces "$store/.jj/workspace-config-id"
