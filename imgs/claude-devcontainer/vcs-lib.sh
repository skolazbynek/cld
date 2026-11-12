# Shell-level VCS abstraction. Source this file to get vcs_* functions.
#
# Detects whether the repository is jujutsu or git and provides a unified set
# of functions the devcontainer entrypoint uses (for both its ticket boot loop
# and the v1 task-agent flow) for workspace isolation, branching, committing,
# and cleanup.
#
# Usage: source /workspace/vcs-lib.sh
# Requires: WORKSPACE_ORIGIN to be set.

# --- Detection ---------------------------------------------------------------

VCS_TYPE=""

# Registry read by the anchor guard hooks (cld.vcs.anchor_guard).
CLD_ANCHORS_FILE="${CLD_ANCHORS_FILE:-/tmp/cld-anchors.jsonl}"

# --- Workspace isolation -----------------------------------------------------

cld_detect_backend() {
    # Echo the backend ("jj" or "git") of the repo at $1; echo nothing when
    # unsupported. Per-directory variant of detect_vcs for the ticket boot
    # loop, which handles N repos and cannot use the WORKSPACE_ORIGIN global.
    local dir="$1"
    if [ -d "$dir/.jj" ] && command -v jj &>/dev/null; then
        echo "jj"
    elif [ -e "$dir/.git" ] && command -v git &>/dev/null; then
        echo "git"
    fi
}

cld_store_jj() {
    # Run jj against the origin store without touching its working copy: the
    # origin's .jj/working_copy is mounted read-only, and a plain jj call there
    # would try to snapshot it (and fail). Args: $1=origin, rest=jj arguments.
    local origin="$1"
    shift
    (cd "$origin" && jj --ignore-working-copy "$@")
}

cld_add_workspace() {
    # Add jj workspace $2 at $4, based on revision $3, through the origin
    # store. Without a snapshot jj creates the workspace but refuses to check
    # it out; `jj new` inside it does the checkout. Any other failure is real.
    # Same pattern as runtests/entrypoint.sh. Args: $1=origin $2=name $3=rev
    # $4=workspace_dir.
    local origin="$1" name="$2" rev="$3" workspace="$4" err
    err=$(cld_store_jj "$origin" workspace add --name "$name" -r "$rev" "$workspace" 2>&1) ||
        case "$err" in
            *"must be able to update the working copy"*) ;;
            *) echo "$err" >&2; return 1 ;;
        esac
    (cd "$workspace" && jj new "$rev")
}

cld_recover_anchor() {
    # Recover AGENT_ANCHOR_HASH on a warm restart or bookmark reattach: find
    # the scratch commit at or below $bookmark (which always sits on the anchor:
    # the scratch commit itself when isolated, its parent when shared) carrying
    # our own 'cld anchor: <session> mode=<mode>' description (glob-matched -- `jj commit -m` appends a
    # trailing newline to single-line descriptions, so an exact match against
    # the bare text never hits; the space before the wildcard keeps session
    # x-1 from matching x-12's scratch), then read that scratch commit's own
    # description back to recover the mode it was staged with. isolated: the
    # scratch commit itself is the anchor. shared: its parent is (see
    # docs/design-ticket-containers.md section 3; mirrors the first-launch
    # branch of cld_boot_workspace and cld.vcs.scratch.stage_in_workspace).
    # Args: $1=bookmark $2=session. cwd must be inside the jj store.
    local bookmark="$1" session="$2" scratch_hash desc
    scratch_hash=$(cld_store_jj . log --no-graph -n 1 \
        -r "heads(descendants(${bookmark}) & description(glob:'cld anchor: ${session} *'))" \
        -T change_id 2>/dev/null || true)
    [ -n "$scratch_hash" ] || return 0
    desc=$(cld_store_jj . log --no-graph -n 1 -r "$scratch_hash" -T description 2>/dev/null || true)
    case "$desc" in
        *"mode=shared"*)
            cld_store_jj . log --no-graph -n 1 -r "parents($scratch_hash)" -T change_id 2>/dev/null || true
            ;;
        *)
            echo "$scratch_hash"
            ;;
    esac
}

# Three-branch jj workspace boot (warm restart / reattach / first launch),
# extracted from the v1 single-repo entrypoint flow so the ticket boot loop
# can run it once per repo. Sets:
#   CLD_BOOT_ANCHOR       effective anchor change id (empty when no scratch commit
#                         is recoverable, e.g. a git-era bookmark)
#   CLD_BOOT_FIRST_LAUNCH 1 on the first-launch branch, else 0
# Returns non-zero on any fatal boot error; the caller decides whether that
# kills the whole boot.
#
# Args: $1=origin_dir (the bind mount holding the jj store; its working-copy
#          record is read-only, so every call there goes through cld_store_jj)
#       $2=workspace_dir (container-layer path the workspace lives at)
#       $3=bookmark (session bookmark = workspace name)
#       $4=base_rev (revision to anchor on at first launch)
#       $5=mode (isolated|shared)
#       $6=scratch_source: "env" decodes AGENT_SCRATCH (v1 wire, required on
#          first launch); "default" stages the scratch commit empty
#          (`--default-payload`, ticket kind -- design 4.2).
#
# Invariant: bookmark $3 exists in the origin store <=> a live or
# restart-paused lifecycle owns this session. The bookmark is static: it sits
# on the effective anchor (CLD_BOOT_ANCHOR) and only a reanchor moves it, never
# the work. Restart preserves it (reattach resumes at the old workspace tip);
# shutdown forgets it so the next launch is a fresh lifecycle honoring the
# requested revision.
cld_boot_workspace() {
    local origin="$1" workspace="$2" bookmark="$3" base_rev="$4" mode="$5" scratch_source="$6"
    CLD_BOOT_ANCHOR=""
    CLD_BOOT_FIRST_LAUNCH=0

    if [ -e "$workspace/.jj" ]; then
        # Warm restart: `docker start` of a stopped container (not `docker rm
        # && docker run`). The ephemeral workspace dir and its jj workspace
        # registration persisted along with the container's writable layer, so
        # installed packages, history, and in-progress edits are all intact.
        # Do NOT forget + re-add -- `jj workspace add` refuses a non-empty dir
        # and would crash the boot. Reuse the workspace in place, reconcile a
        # possibly-stale working copy (a sibling workspace on the same origin
        # store may have advanced it), and recover the anchor.
        echo "[cld] warm restart: reusing existing workspace at $workspace"
        (cd "$workspace" && jj workspace update-stale 2>/dev/null || true)
        CLD_BOOT_ANCHOR=$(cd "$origin" && cld_recover_anchor "$bookmark" "$bookmark")
        return 0
    fi

    # Forget any workspace already registered under $bookmark before adding. A
    # prior shutdown forgets the bookmark but NOT the workspace, so on a fresh
    # first-launch the stale registration would make `jj workspace add --name`
    # fail with "Workspace named X already exists" and leave the workspace dir
    # empty. No-op when absent (first-ever launch). The workspace tip is read
    # first: the bookmark marks the anchor, so the work to resume is only found
    # through the workspace being forgotten.
    local tip
    tip=$(cld_store_jj "$origin" log --no-graph -n 1 -r "${bookmark}@" -T commit_id 2>/dev/null || true)
    cld_store_jj "$origin" workspace forget "$bookmark" 2>&1 || true

    if cld_store_jj "$origin" bookmark list -T 'name ++ "\n"' | grep -qx "$bookmark"; then
        echo "[cld] reattaching workspace '$bookmark'"
        if ! cld_add_workspace "$origin" "$bookmark" "${tip:-$bookmark}" "$workspace"; then
            echo "Error: jj workspace add failed (reattach)" >&2
            return 1
        fi
        CLD_BOOT_ANCHOR=$(cd "$origin" && cld_recover_anchor "$bookmark" "$bookmark")
        return 0
    fi

    # First launch. Scratch commit B (child of anchor A; empty, or carrying
    # `.cld-run/brief.md` when the launch composed a brief) is staged INSIDE the workspace by `python3 -m cld.vcs.scratch`, so the
    # origin working copy is never touched -- crucial for the common jj case
    # where the user's @ is A itself.
    if [ "$scratch_source" = "env" ] && [ -z "${AGENT_SCRATCH:-}" ]; then
        echo "Error: AGENT_SCRATCH is required on first launch" >&2
        return 1
    fi
    CLD_BOOT_FIRST_LAUNCH=1
    local a_hash b_hash
    if ! a_hash=$(cld_store_jj "$origin" log --no-graph -n 1 -r "$base_rev" -T change_id 2>/dev/null); then
        echo "Error: could not resolve base revision '$base_rev'" >&2
        return 1
    fi
    echo "[cld] first launch, base=${a_hash:0:12}"
    if ! cld_add_workspace "$origin" "$bookmark" "$a_hash" "$workspace"; then
        echo "Error: jj workspace add failed (first launch)" >&2
        return 1
    fi
    # -P: don't let `-m` put this freshly checked-out workspace's own `cld/`
    # package ahead of the installed one on sys.path (matters when booting cld
    # itself -- otherwise a stale in-repo cld.vcs.scratch runs unnoticed).
    local scratch_cmd=(python3 -P -m cld.vcs.scratch)
    [ "$scratch_source" = "default" ] && scratch_cmd+=(--default-payload)
    if ! b_hash=$(cd "$workspace" && \
            WORKSPACE_CURRENT="$workspace" AGENT_ANCHOR_MODE="$mode" "${scratch_cmd[@]}"); then
        echo "Error: anchor staging failed" >&2
        return 1
    fi
    # isolated (default): the anchor is B, so only B's own descendants are
    # editable. shared: the anchor is A itself, so any pre-existing descendant
    # of A (not just of B) is in the container's editable tree -- see
    # docs/design-ticket-containers.md section 3. cld_recover_anchor mirrors this choice on a
    # later restart/reattach.
    if [ "$mode" = "shared" ]; then
        CLD_BOOT_ANCHOR="$a_hash"
    else
        CLD_BOOT_ANCHOR="$b_hash"
    fi
    echo "[cld] anchor=${CLD_BOOT_ANCHOR:0:12} (mode=$mode, base=${a_hash:0:12}, scratch=${b_hash:0:12})"
    (cd "$workspace" && jj bookmark set "$bookmark" -r "$CLD_BOOT_ANCHOR" --allow-backwards)
    return 0
}

cld_recover_git_anchor() {
    # git counterpart of cld_recover_anchor: echo the effective anchor of a git
    # ticket, nothing when no scratch commit is pinned. The hidden ref
    # refs/cld/anchor/<session> points at the scratch commit S (cld.names.anchor_ref
    # builds the same string; this file cannot import it); S's own message says
    # which mode it was staged with. isolated: S is the anchor. shared: its
    # parent is. Args: $1=origin $2=session.
    local origin="$1" session="$2" s_hash desc
    s_hash=$(git -C "$origin" rev-parse --verify -q "refs/cld/anchor/${session}^{commit}" 2>/dev/null) || return 0
    desc=$(git -C "$origin" log -1 --format=%B "$s_hash" 2>/dev/null || true)
    case "$desc" in
        *"mode=shared"*) git -C "$origin" rev-parse --verify -q "${s_hash}^" 2>/dev/null || true ;;
        *) echo "$s_hash" ;;
    esac
}

cld_stale_worktree() {
    # Print "<HEAD> <branch>" of the registration for the worktree path $2 in the
    # origin store $1 (branch empty when detached); print nothing and return
    # non-zero when that path is not registered.
    local origin="$1" path head="" branch="" in_path=0 found=1 line
    path=$(realpath -m "$2")
    while IFS= read -r line; do
        case "$line" in
            "worktree "*)
                in_path=0
                if [ "${line#worktree }" = "$path" ]; then
                    in_path=1
                    found=0
                fi
                ;;
            "HEAD "*) if [ "$in_path" = 1 ]; then head="${line#HEAD }"; fi ;;
            "branch refs/heads/"*) if [ "$in_path" = 1 ]; then branch="${line#branch refs/heads/}"; fi ;;
        esac
    done < <(git -C "$origin" worktree list --porcelain)
    if [ "$found" = 0 ]; then
        echo "$head $branch"
    fi
    return "$found"
}

_cld_git_boot_anchor() {
    # Set CLD_BOOT_ANCHOR from the anchor ref of session $2, else keep the base
    # $3 with a warning. Args: $1=origin $2=session $3=base_rev.
    local anchor
    anchor=$(cld_recover_git_anchor "$1" "$2")
    if [ -n "$anchor" ]; then
        CLD_BOOT_ANCHOR="$anchor"
    else
        echo "[WARN] no anchor ref for '$2' (a ticket from before the scratch commit): anchoring on the base ${3:0:12}" >&2
        CLD_BOOT_ANCHOR="$3"
    fi
}

# git counterpart of cld_boot_workspace for ticket repos on a git backend.
# Same three branches over a git worktree whose branch is $bookmark. The
# branch moves with the work, so the anchor is pinned by the hidden ref
# refs/cld/anchor/$bookmark on the scratch commit S, staged like the jj one
# (cld.vcs.scratch; S's message names the mode). No watchman snapshots.
# Sets CLD_BOOT_ANCHOR (S isolated, S's parent shared; the base itself, with a
# warning, for a ticket booted before the scratch commit existed) and
# CLD_BOOT_FIRST_LAUNCH. Args as cld_boot_workspace: $1=origin $2=workspace
# $3=bookmark $4=base_rev $5=mode (default isolated).
#
# Worktrees are added locked (the reason names the session). The origin store
# is shared with other tickets whose paths do not exist in this container, so
# there is no blanket `git worktree prune`: only this session's own stale
# registration is dropped, after its HEAD and branch are read, so a reattach
# resumes where the lost worktree was. A detached HEAD holding commits nothing else
# holds is archived first (cld_archive_stale_head), so a failed add loses nothing.
cld_archive_stale_head() {
    # A stale registration is about to be dropped; a detached HEAD may be all that holds some
    # commits (the add below can fail, or resume something else). Keep them under
    # refs/cld/ended/<session>/<UTC stamp>-detached, the name shutdown uses (cld.names.ended_ref
    # builds the same string; this file cannot import it). Nothing is archived when the commit is
    # missing, already reachable from the session branch or the anchor ref, or already archived.
    # Args: $1=origin $2=session $3=head
    local origin="$1" session="$2" head="$3" held ref
    git -C "$origin" cat-file -e "${head}^{commit}" 2>/dev/null || return 0
    for held in "refs/heads/$session" "refs/cld/anchor/$session"; do
        if git -C "$origin" merge-base --is-ancestor "$head" "$held" 2>/dev/null; then
            return 0
        fi
    done
    if [ -n "$(git -C "$origin" for-each-ref --points-at "$head" "refs/cld/ended/$session/")" ]; then
        return 0
    fi
    ref="refs/cld/ended/$session/$(date -u +%Y%m%dT%H%M%SZ)-detached"
    if git -C "$origin" update-ref "$ref" "$head" ""; then
        echo "[cld] archived the old worktree HEAD ${head:0:12} as $ref"
    else
        echo "[WARN] could not archive the old worktree HEAD ${head:0:12}; keep it with: git update-ref <ref> $head" >&2
    fi
}

cld_boot_worktree() {
    local origin="$1" workspace="$2" bookmark="$3" base_rev="$4" mode="${5:-isolated}"
    local ref="refs/cld/anchor/$bookmark"
    CLD_BOOT_ANCHOR="$base_rev"
    CLD_BOOT_FIRST_LAUNCH=0

    if [ -e "$workspace/.git" ]; then
        echo "[cld] warm restart: reusing existing worktree at $workspace"
        _cld_git_boot_anchor "$origin" "$bookmark" "$base_rev"
        return 0
    fi

    local stale stale_head="" stale_branch="" have_ref=0 have_branch=0
    if stale=$(cld_stale_worktree "$origin" "$workspace"); then
        read -r stale_head stale_branch <<<"$stale"
        # Before the registration goes: a branch keeps its own commits, a detached HEAD does not.
        if [ -z "$stale_branch" ] && [ -n "$stale_head" ]; then
            cld_archive_stale_head "$origin" "$bookmark" "$stale_head"
        fi
        # `remove -f -f` also drops a locked registration whose directory is gone.
        git -C "$origin" worktree remove -f -f "$workspace" || true
    fi
    if git -C "$origin" rev-parse --verify -q "${ref}^{commit}" >/dev/null 2>&1; then
        have_ref=1
    fi
    if git -C "$origin" show-ref --verify --quiet "refs/heads/$bookmark"; then
        have_branch=1
    fi

    local lock=(--lock --reason "$bookmark")
    if [ "$have_ref" = 1 ] || [ "$have_branch" = 1 ]; then
        echo "[cld] reattaching worktree '$bookmark'"
        local add
        if [ -n "$stale_branch" ] && git -C "$origin" show-ref --verify --quiet "refs/heads/$stale_branch"; then
            add=("$workspace" "$stale_branch")
        elif [ -z "$stale_branch" ] && [ -n "$stale_head" ] &&
                git -C "$origin" cat-file -e "${stale_head}^{commit}" 2>/dev/null; then
            add=(--detach "$workspace" "$stale_head")
        elif [ "$have_branch" = 1 ]; then
            add=("$workspace" "$bookmark")
        else
            add=(--no-track -b "$bookmark" "$workspace" "$ref")
        fi
        if ! git -C "$origin" worktree add "${lock[@]}" "${add[@]}"; then
            echo "Error: git worktree add failed (reattach)" >&2
            return 1
        fi
        _cld_git_boot_anchor "$origin" "$bookmark" "$base_rev"
        return 0
    fi

    CLD_BOOT_FIRST_LAUNCH=1
    local a_hash s_hash
    if ! a_hash=$(git -C "$origin" rev-parse --verify -q "${base_rev}^{commit}"); then
        echo "Error: could not resolve base revision '$base_rev'" >&2
        return 1
    fi
    echo "[cld] first launch (git), base=${a_hash:0:12}"
    if ! git -C "$origin" worktree add "${lock[@]}" --no-track -b "$bookmark" "$workspace" "$a_hash"; then
        echo "Error: git worktree add failed (first launch)" >&2
        return 1
    fi
    # -P: as in cld_boot_workspace.
    if ! s_hash=$(cd "$workspace" && \
            WORKSPACE_CURRENT="$workspace" AGENT_ANCHOR_MODE="$mode" \
            python3 -P -m cld.vcs.scratch --default-payload); then
        echo "Error: anchor staging failed" >&2
        # Without the worktree and the branch the next boot is a clean first
        # launch again, not a warm restart that has no anchor.
        git -C "$origin" worktree remove -f -f "$workspace" || true
        git -C "$origin" branch -D "$bookmark" >/dev/null || true
        return 1
    fi
    if [ "$mode" = "shared" ]; then
        CLD_BOOT_ANCHOR="$a_hash"
    else
        CLD_BOOT_ANCHOR="$s_hash"
    fi
    echo "[cld] anchor=${CLD_BOOT_ANCHOR:0:12} (mode=$mode, base=${a_hash:0:12}, scratch=${s_hash:0:12})"
    return 0
}

cld_enable_watchman() {
    # Enable watchman auto-snapshot inside a jj workspace so background file
    # changes get snapshotted without a jj command. `register-snapshot-trigger`
    # fires under our cap-drop=ALL / no-new-privileges / non-root posture.
    local workspace="$1"
    (cd "$workspace" && \
        jj config set --workspace fsmonitor.backend watchman && \
        jj config set --workspace fsmonitor.watchman.register-snapshot-trigger true && \
        jj status >/dev/null)
}

cld_guard_anchor() {
    # Register a workspace with the anchor guard hooks and, for jj, make every
    # commit outside the anchor's descendants immutable in this workspace only
    # (workspace config lives in the container's home, so the host and sibling
    # containers sharing the store never see it; docs/design-anchor-guard.md
    # sections 3-4). An empty anchor is skipped: the revset would match nothing
    # and lock the whole repo. Returns non-zero when the jj config call fails;
    # the caller decides whether that kills the boot.
    # Args: $1=repo $2=workspace $3=anchor $4=mode $5=backend
    local repo="$1" workspace="$2" anchor="$3" mode="$4" backend="$5"
    if [ -z "$anchor" ]; then
        echo "[WARN] repo '${repo}': no anchor recorded, anchor guard skipped" >&2
        return 0
    fi
    jq -nc --arg repo "$repo" --arg workspace "$workspace" --arg anchor "$anchor" \
        --arg mode "$mode" --arg backend "$backend" \
        '{repo: $repo, workspace: $workspace, anchor: $anchor, mode: $mode, backend: $backend}' \
        >> "$CLD_ANCHORS_FILE"
    if [ "$backend" = "jj" ]; then
        (cd "$workspace" && \
            jj config set --workspace 'revset-aliases."immutable_heads()"' \
                "builtin_immutable_heads() | ~(change_id($anchor)+::)")
    fi
}

cld_set_push_bookmark_template() {
    # A bare `jj git push --change` would create `push-<id>`; name it
    # `<session>.mr-<id>` instead (docs/design-bookmark-naming.md). jj only; run
    # containers do not use this library. The value is a TOML literal string:
    # jj would not parse a bare one that starts with a quote.
    # Args: $1=workspace $2=backend
    local workspace="$1" backend="$2"
    [ "$backend" = "jj" ] || return 0
    (cd "$workspace" && \
        jj config set --workspace templates.git_push_bookmark \
            "'\"${SESSION_NAME}.mr-\" ++ change_id.short()'")
}

# --- Committing --------------------------------------------------------------

vcs_commit() {
    # Commit all changes with a message.
    # Args: $1=message
    local message="$1"

    if [ "$VCS_TYPE" = "jj" ]; then
        jj commit -m "$message" 2>&1
    else
        git add -A 2>&1
        git commit -m "$message" 2>&1
    fi
}

# --- Describe / amend message ------------------------------------------------

vcs_log_commit_id() {
    # Resolve a revision to a concrete commit hash.
    # Args: $1=revision
    local revision="$1"

    if [ "$VCS_TYPE" = "jj" ]; then
        jj log -r "$revision" --no-graph -T 'commit_id' 2>&1 | head -n1
    else
        git rev-parse "$revision" 2>&1
    fi
}

# --- Diff / change detection -------------------------------------------------

vcs_has_changes() {
    # Return 0 (true) if there are uncommitted changes, 1 otherwise.
    if [ "$VCS_TYPE" = "jj" ]; then
        jj diff --stat 2>&1 | grep -q .
    else
        [ -n "$(git status --porcelain 2>/dev/null)" ]
    fi
}

vcs_diff_stat() {
    # Print a stat summary of changes.
    if [ "$VCS_TYPE" = "jj" ]; then
        jj diff --stat --no-pager 2>/dev/null
    else
        git diff HEAD --stat 2>/dev/null
    fi
}

vcs_diff_file_count() {
    # Print the number of changed files.
    if [ "$VCS_TYPE" = "jj" ]; then
        jj diff --stat --no-pager 2>/dev/null | grep '|' | wc -l
    else
        git diff HEAD --stat 2>/dev/null | grep '|' | wc -l
    fi
}

vcs_diff_file_names() {
    # Print comma-separated list of changed file names.
    if [ "$VCS_TYPE" = "jj" ]; then
        jj diff --stat --no-pager 2>/dev/null | grep '|' | awk '{print $1}' | tr '\n' ', ' | sed 's/,$//'
    else
        git diff HEAD --stat 2>/dev/null | grep '|' | awk '{print $1}' | tr '\n' ', ' | sed 's/,$//'
    fi
}

# --- Squash ------------------------------------------------------------------

vcs_squash_into_parent() {
    # Squash the current change into its parent.
    if [ "$VCS_TYPE" = "jj" ]; then
        jj squash --from @ --into @- 2>&1
    else
        git add -A 2>&1
        git reset --soft HEAD~1 2>&1
        git commit --amend --no-edit 2>&1
    fi
}

# --- Anchor immutability guard ----------------------------------------------

vcs_assert_descendant() {
    # Exit 0 if REV descends from ANCHOR, non-zero otherwise.
    # Args: $1=anchor  $2=rev
    local anchor="$1" rev="$2"

    if [ "$VCS_TYPE" = "jj" ]; then
        local out
        out=$(jj log -r "ancestors($rev) & $anchor" --no-graph -T 'commit_id' -n 1 2>/dev/null)
        [ -n "$out" ]
    else
        git merge-base --is-ancestor "$anchor" "$rev" >/dev/null 2>&1
    fi
}

# --- After-commit branch update ---------------------------------------------

vcs_update_branch_after_commit() {
    # After committing, update the branch to point at the new commit.
    # In jj, @ moves forward after commit so the bookmark needs to be set to @-.
    # In git, the branch already tracks HEAD, so this is a no-op.
    # Args: $1=branch_name
    local branch_name="$1"

    if [ "$VCS_TYPE" = "jj" ]; then
        jj bookmark set "$branch_name" -r @- 2>&1
    fi
    # git: branch already points at HEAD after commit -- no action needed
}
