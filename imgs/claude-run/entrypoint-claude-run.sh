#!/bin/bash
set -e
source /workspace/container-init.sh
source /workspace/vcs-lib.sh

AGENT_NAME="${SESSION_NAME:?SESSION_NAME must be set}"
BRIEF_FILE="/workspace/current/.cld-run/brief.md"

log() {
    local msg="[$(date +'%Y-%m-%d %H:%M:%S')] $*"
    echo "$msg"
    [ -n "$LOG_FILE" ] && echo "$msg" >> "$LOG_FILE"
}

log_error() {
    local msg="[$(date +'%Y-%m-%d %H:%M:%S')] ERROR: $*"
    echo "$msg" >&2
    [ -n "$LOG_FILE" ] && echo "$msg" >> "$LOG_FILE"
}

# --- Validate inputs ---

# Stage host configs before any VCS operation; jj working-copy changes need
# user.email/user.name from ~/.config/jj.
copy_host_configs

# Anchor: base revision arrives as AGENT_REVISION_HINT (a change id the host
# resolved from its own jj view).
# Scratch commit B (child of anchor A, carrying `.cld-run/*`) is created
# INSIDE /workspace/current by `python3 -m cld.vcs.scratch` -- the origin
# working copy is never touched.
if [ -z "${AGENT_SCRATCH:-}" ]; then
    echo "Error: AGENT_SCRATCH is required" >&2
    exit 1
fi

BOOKMARK="$AGENT_NAME"

cd "$WORKSPACE_ORIGIN"
BASE_REV="${AGENT_REVISION_HINT:-@}"
if ! A_HASH=$(cld_store_jj "$WORKSPACE_ORIGIN" log --no-graph -n 1 -r "$BASE_REV" -T change_id 2>/dev/null); then
    echo "Error: could not resolve AGENT_REVISION_HINT='$BASE_REV'" >&2
    exit 1
fi
echo "[cld] base=${A_HASH:0:12}"
cld_add_workspace "$WORKSPACE_ORIGIN" "$BOOKMARK" "$A_HASH" /workspace/current
if ! B_HASH=$(cd /workspace/current && python3 -m cld.vcs.scratch); then
    echo "Error: peer-side anchor staging failed" >&2
    exit 1
fi
# isolated (default): AGENT_ANCHOR_HASH is B, so only B's own descendants are
# editable. shared: AGENT_ANCHOR_HASH is A itself, so any pre-existing
# descendant of A (not just of B) is in the container's editable tree -- see
# docs/design-ticket-containers.md section 3.
AGENT_ANCHOR_MODE="${AGENT_ANCHOR_MODE:-isolated}"
if [ "$AGENT_ANCHOR_MODE" = "shared" ]; then
    AGENT_ANCHOR_HASH="$A_HASH"
else
    AGENT_ANCHOR_HASH="$B_HASH"
fi
export AGENT_ANCHOR_HASH
echo "[cld] anchor=${AGENT_ANCHOR_HASH:0:12} (mode=$AGENT_ANCHOR_MODE, base=${A_HASH:0:12}, scratch=${B_HASH:0:12})"
(cd /workspace/current && jj bookmark set "$BOOKMARK" -r "$AGENT_ANCHOR_HASH" --allow-backwards)

: > "$CLD_ANCHORS_FILE"
if ! cld_guard_anchor current /workspace/current "$AGENT_ANCHOR_HASH" \
        "$AGENT_ANCHOR_MODE" jj; then
    echo "Error: anchor guard setup failed" >&2
    exit 1
fi

(cd /workspace/current && \
    jj config set --workspace fsmonitor.backend watchman && \
    jj config set --workspace fsmonitor.watchman.register-snapshot-trigger true && \
    jj status >/dev/null)

cd /workspace/current
VCS_TYPE="jj"

OUTPUT_DIR="$WORKSPACE_CURRENT/agent-output-$AGENT_NAME"
LOG_FILE="$OUTPUT_DIR/agent.log"
RESULT_FILE="$OUTPUT_DIR/result.json"
SUMMARY_FILE="$OUTPUT_DIR/summary.json"
mkdir -p "$OUTPUT_DIR"

# The brief only exists once the workspace is staged, so this check lives here rather
# than up with the other input validation.
if [ ! -s "$BRIEF_FILE" ]; then
    echo "Error: no brief at $BRIEF_FILE -- the launcher composes it into the anchor scratch" >&2
    exit 1
fi

log "Agent $AGENT_NAME started (VCS: $VCS_TYPE, anchor: ${AGENT_ANCHOR_HASH:0:12})"

# --- Configure MCP servers ---

build_claude_config

# --- Execute Claude ---

INSTRUCTIONS=$(cat "$BRIEF_FILE")

if [ "$VCS_TYPE" = "jj" ]; then
    VCS_NOTE="Your working directory is isolated in a jujutsu workspace. All changes will be committed as a single change when you're done."
else
    VCS_NOTE="Your working directory is isolated in a git worktree. All changes will be committed when you're done."
fi

# Image-owned frame; the user's layer is the brief, appended below. The header stays:
# it separates the frame from the brief, unlike the inter-ref glue the ordered-blocks
# composition dropped (docs/design-prompt-chaining.md).
SYSTEM_PROMPT_FILE="/opt/cld/run-system-prompt.md"
SYSTEM_PROMPT="$(cat "$SYSTEM_PROMPT_FILE")

$VCS_NOTE

TASK INSTRUCTIONS:
$INSTRUCTIONS"

START_TIME=$(date +%s)
log "Executing Claude..."

(while sleep 30; do log "Still running... $(($(date +%s) - START_TIME))s elapsed"; done) &
PROGRESS_PID=$!

AGENT_MODEL="${AGENT_MODEL:-sonnet}"
log "Using model: $AGENT_MODEL"

# No `--add-dir /opt/cld` here, unlike the devcontainer: the base image's baked skills
# are all agent surfaces (broker, mailbox, task-agent fleet) and a one-shot run
# has neither a mailbox mount nor a broker key, so loading them would only offer tools
# that cannot work.
if claude -p "$SYSTEM_PROMPT" \
    --model "$AGENT_MODEL" \
    --dangerously-skip-permissions \
    --output-format json > "$RESULT_FILE" 2>&1; then
    CLAUDE_EXIT=0
    log "Claude completed successfully"
else
    CLAUDE_EXIT=$?
    log_error "Claude failed with exit code: $CLAUDE_EXIT"
fi

kill $PROGRESS_PID 2>/dev/null || true

DURATION=$(($(date +%s) - START_TIME))
log "Duration: ${DURATION}s"

# --- Commit changes ---

CHANGED_FILES=""
FILE_COUNT=0
COMMIT_HASH="none"
TASK_STATUS="unknown"

if vcs_has_changes; then
    FILE_COUNT=$(vcs_diff_file_count)
    CHANGED_FILES=$(vcs_diff_file_names)
    log "Files modified: $FILE_COUNT ($CHANGED_FILES)"

    if [ "${AGENT_COMMIT_MSG_LLM:-0}" = "1" ]; then
        if [ "$VCS_TYPE" = "jj" ]; then
            DESCRIBE_PROMPT="Look at the current jj diff (run jj diff --stat and jj diff). Write a single short sentence (under 72 chars) describing what was done. Output ONLY the description, nothing else."
        else
            DESCRIBE_PROMPT="Look at the current git diff (run git diff --stat and git diff). Write a single short sentence (under 72 chars) describing what was done. Output ONLY the description, nothing else."
        fi
        COMMIT_MSG=$(claude -p "$DESCRIBE_PROMPT" \
            --model "$AGENT_MODEL" \
            --dangerously-skip-permissions 2>/dev/null | head -1)
        COMMIT_MSG="${COMMIT_MSG:-agent $AGENT_NAME: task}"
    else
        STAT_SUMMARY=$(vcs_diff_stat 2>/dev/null | tail -1)
        INS=$(echo "$STAT_SUMMARY" | sed -n 's/.*[^0-9]\([0-9]*\) insertion.*/\1/p')
        DEL=$(echo "$STAT_SUMMARY" | sed -n 's/.*[^0-9]\([0-9]*\) deletion.*/\1/p')
        INS="${INS:-0}"
        DEL="${DEL:-0}"
        COMMIT_MSG="agent $AGENT_NAME: ${FILE_COUNT} files, +${INS}/-${DEL}"
    fi
    log "Commit message: $COMMIT_MSG"

    if ! vcs_assert_descendant "$AGENT_ANCHOR_HASH" "@"; then
        log_error "Pre-commit anchor check failed; @ is not a descendant of $AGENT_ANCHOR_HASH"
        printf 'anchor_violation: @ is not a descendant of %s\n' "$AGENT_ANCHOR_HASH" \
            > "$WORKSPACE_CURRENT/AGENT-FAILURE.md"
        TASK_STATUS="anchor_violation"
        exit 4
    fi

    if ! vcs_commit "$COMMIT_MSG" 2>&1 | tee -a "$LOG_FILE"; then
        log_error "Failed to commit changes"
        TASK_STATUS="commit_failed"
        exit 3
    fi

    vcs_update_branch_after_commit "$AGENT_NAME" 2>&1 | tee -a "$LOG_FILE"
    COMMIT_HASH=$(vcs_log_commit_id "$AGENT_NAME" 2>&1 | head -n1)
    log "Committed to branch $AGENT_NAME ($COMMIT_HASH)"
    TASK_STATUS="success"
else
    log "No changes detected"
    TASK_STATUS="no_changes"
fi

FAILURE_JSON_STR="null"
if [ -f "$WORKSPACE_CURRENT/AGENT-FAILURE.md" ]; then
    log "AGENT-FAILURE.md detected - task failed"
    FAILURE_JSON_STR=$(jq -Rs . < "$WORKSPACE_CURRENT/AGENT-FAILURE.md")
    cp "$WORKSPACE_CURRENT/AGENT-FAILURE.md" "$OUTPUT_DIR/AGENT-FAILURE.md" 2>/dev/null || true
    [ "$TASK_STATUS" = "success" ] && TASK_STATUS="failed"
fi

cat > "$SUMMARY_FILE" <<EOF
{
  "status": "$TASK_STATUS",
  "agent_name": "$AGENT_NAME",
  "branch": "$AGENT_NAME",
  "commit_hash": "$COMMIT_HASH",
  "anchor_hash": "$AGENT_ANCHOR_HASH",
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "duration_seconds": $DURATION,
  "claude_exit_code": $CLAUDE_EXIT,
  "brief_file": "$BRIEF_FILE",
  "vcs_type": "$VCS_TYPE",
  "failure": $FAILURE_JSON_STR,
  "changes": {
    "files_modified": $FILE_COUNT,
    "changed_files": "$CHANGED_FILES"
  },
  "output": {
    "log_file": "$LOG_FILE",
    "result_file": "$RESULT_FILE",
    "summary_file": "$SUMMARY_FILE"
  }
}
EOF

log "Summary written to $SUMMARY_FILE"

if [ "$TASK_STATUS" = "success" ] || [ "$TASK_STATUS" = "failed" ]; then
    if vcs_assert_descendant "$AGENT_ANCHOR_HASH" "$AGENT_NAME"; then
        vcs_squash_into_parent 2>&1 | tee -a "$LOG_FILE" || \
            log_error "Failed to include summary in commit (non-fatal)"
    else
        log_error "Post-commit anchor check failed; skipping summary squash"
    fi
fi

log "Agent execution complete"
