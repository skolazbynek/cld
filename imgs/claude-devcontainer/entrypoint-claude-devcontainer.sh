#!/bin/bash
source /workspace/container-init.sh
source /workspace/vcs-lib.sh

copy_host_configs
ensure_own_mailbox
MAILBOX_OK=$?

BOOKMARK="${SESSION_NAME:?SESSION_NAME must be set}"

# Ticket container (v2): per-repo boot loop over the launch manifest, then its
# own after-loop steps and PID-1 idle. It never reaches the v1 single-repo
# flow below (docs/design-ticket-containers.md section 4).
if [ -n "${TICKET_MODE:-}" ]; then
    # The container layer survives `docker stop`, so a warm start (`docker
    # start`) still sees the previous boot's sentinel. Clear it before any
    # per-repo work, or the host-side readiness wait returns early.
    rm -f /tmp/cld-ticket-ready
    : > "$CLD_ANCHORS_FILE"
    # /tmp/bin survives too, and it is first on PATH (container-init.sh), so
    # the previous boot's generated wrapper shadows `which claude` on a warm
    # start: the regenerated wrapper would exec the stale wrapper's own path,
    # an infinite self-exec loop. Drop it so this boot resolves the real
    # binary fresh. The claude wrapper is rewritten below.
    rm -f /tmp/bin/*
    : "${CLD_TICKET_MANIFEST:?CLD_TICKET_MANIFEST must be set in TICKET_MODE}"
    if ! command -v jq &>/dev/null; then
        echo "Error: TICKET_MODE requires jq in the image" >&2
        exit 1
    fi
    TICKET=$(printf '%s' "$CLD_TICKET_MANIFEST" | jq -r '.ticket')
    TICKET_ROOT="/workspace/${TICKET}"
    N_REPOS=$(printf '%s' "$CLD_TICKET_MANIFEST" | jq '.repos | length')
    mkdir -p "$TICKET_ROOT"
    TICKET_ROWS=""
    TICKET_ADD_DIRS=""
    TICKET_TRUST_DIRS=("$TICKET_ROOT")
    REPO_INDEX=0
    export CLD_TICKET_ANCHOR_ENV_FILE="/tmp/cld-ticket-anchors.sh"
    : > "$CLD_TICKET_ANCHOR_ENV_FILE"

    # Workspaces live inside the container's ephemeral filesystem under the
    # ticket root. jj stores everything into each origin's .jj/repo/store via
    # its RW bind mount, so bookmarks and (watchman-driven) snapshots persist
    # across `docker rm && docker run` even though the workspace dirs do not.
    # Per-repo boot failure is fatal for the whole boot (no sentinel): a
    # ticket with a silently missing repo is worse than a failed start.
    while IFS= read -r _repo_json; do
        REPO_INDEX=$((REPO_INDEX + 1))
        REPO_NAME=$(jq -r '.name' <<<"$_repo_json")
        REPO_BASE=$(jq -r '.anchor_base' <<<"$_repo_json")
        REPO_MODE=$(jq -r '.anchor_mode' <<<"$_repo_json")
        REPO_ORIGIN="/workspace/origin/${REPO_NAME}"
        REPO_WORKSPACE="${TICKET_ROOT}/${REPO_NAME}"
        echo "[cld] repo ${REPO_INDEX}/${N_REPOS}: ${REPO_NAME} (base=${REPO_BASE:0:12}, mode=${REPO_MODE})"
        if [ ! -d "$REPO_ORIGIN" ]; then
            echo "Error: repo '${REPO_NAME}': no origin mount at ${REPO_ORIGIN}" >&2
            exit 1
        fi
        REPO_BACKEND=$(cld_detect_backend "$REPO_ORIGIN")
        case "$REPO_BACKEND" in
            jj)
                if ! cld_boot_workspace "$REPO_ORIGIN" "$REPO_WORKSPACE" \
                        "$BOOKMARK" "$REPO_BASE" "$REPO_MODE" default; then
                    echo "Error: repo '${REPO_NAME}': boot failed" >&2
                    exit 1
                fi
                cld_enable_watchman "$REPO_WORKSPACE"
                ;;
            git)
                if ! cld_boot_worktree "$REPO_ORIGIN" "$REPO_WORKSPACE" \
                        "$BOOKMARK" "$REPO_BASE" "$REPO_MODE"; then
                    echo "Error: repo '${REPO_NAME}': boot failed" >&2
                    exit 1
                fi
                ;;
            *)
                echo "Error: repo '${REPO_NAME}': no supported VCS at ${REPO_ORIGIN} (expected .jj/ or .git)" >&2
                exit 1
                ;;
        esac
        # Optional per-repo bootstrap, opted in via the registry `bootstrap`
        # key and resolved host-side into CLD_REPO_BOOTSTRAP (<name>=<pyproject
        # subdir>;...). Replaces v1's unconditional depth-3 poetry scan, which
        # does not scale to N repos.
        BOOTSTRAP_DIR=$(cld_repo_kv_get "${CLD_REPO_BOOTSTRAP:-}" "$REPO_NAME")
        if [ -n "$BOOTSTRAP_DIR" ] && command -v poetry &>/dev/null; then
            echo "[cld] repo '${REPO_NAME}': poetry install in ${BOOTSTRAP_DIR}"
            (cd "$REPO_WORKSPACE/$BOOTSTRAP_DIR" && \
                poetry install --no-interaction -q >/dev/null 2>&1) || \
                echo "[WARN] poetry install failed for ${REPO_NAME} (continuing)"
        fi
        REPO_ANCHOR="${CLD_BOOT_ANCHOR:-$REPO_BASE}"
        cld_record_anchor_env "$REPO_NAME" "$REPO_ANCHOR"
        if ! cld_guard_anchor "$REPO_NAME" "$REPO_WORKSPACE" "$REPO_ANCHOR" \
                "$REPO_MODE" "$REPO_BACKEND"; then
            echo "Error: repo '${REPO_NAME}': anchor guard setup failed" >&2
            exit 1
        fi
        cld_set_push_bookmark_template "$REPO_WORKSPACE" "$REPO_BACKEND"
        TICKET_ROWS="${TICKET_ROWS}| ${REPO_NAME} | ${REPO_ANCHOR:0:12} | ${REPO_MODE} | ${REPO_BACKEND} |
"
        # Sessions start at the ticket root, above every repo, where claude
        # loads a repo's .claude/skills/ only once it touches a file there;
        # --add-dir loads them at startup.
        TICKET_ADD_DIRS="${TICKET_ADD_DIRS} --add-dir $(printf '%q' "$REPO_WORKSPACE")"
        TICKET_TRUST_DIRS+=("$REPO_WORKSPACE")
    done < <(printf '%s' "$CLD_TICKET_MANIFEST" | jq -c '.repos[]')

    write_ticket_claude_md "$TICKET_ROOT" "$TICKET" "$TICKET_ROWS"
    build_claude_config "${TICKET_TRUST_DIRS[@]}"

    # Claude wrapper: permission-skip and baked skills stay in-container; the
    # model comes per-invocation from `cld claude -- --model ...`, never baked
    # (design 4.3). The flock is the single-session refusal (design 4.5): one
    # live harness session per ticket container (POC); the lock file carries
    # pid + start time so the refusal can name the holder.
    CLAUDE_BIN=$(which claude)
    cat > /tmp/bin/claude <<EOF
#!/bin/bash
# Per-repo *_ANCHOR vars: this wrapper is reached via \`docker exec\`, which
# does not inherit env the entrypoint exported at runtime, so hand them over
# by sourcing the file cld_record_anchor_env wrote at boot.
[ -f "$CLD_TICKET_ANCHOR_ENV_FILE" ] && . "$CLD_TICKET_ANCHOR_ENV_FILE"
exec 9>>/tmp/cld-session.lock
if ! flock -n 9; then
    echo "Error: a claude session is already live in this ticket container:" >&2
    sed 's/^/  /' /tmp/cld-session.lock >&2
    echo "One session per ticket (POC) -- wait for it to end, or use it." >&2
    exit 1
fi
printf 'pid=%s start=%s\n' "\$\$" "\$(date -Is)" > /tmp/cld-session.lock
exec $CLAUDE_BIN --dangerously-skip-permissions --add-dir /opt/cld${TICKET_ADD_DIRS} "\$@"
EOF
    chmod +x /tmp/bin/claude

    # /tmp (not /run, which is root-owned 755) is writable by the non-root
    # container user.
    touch /tmp/cld-ticket-ready
    echo "[cld] ticket '${TICKET}' ready (${N_REPOS} repos)"

    # PID 1 idles; harness sessions arrive via `docker exec` from the host.
    # TERM (docker stop) tears nothing down: stop is the *pause* verb, and all
    # bookmark/workspace forgetting lives host-side in `cld shutdown`
    # (design 4.3).
    trap 'exit 0' TERM INT
    sleep infinity &
    wait $!
    exit 0
fi

cd "$WORKSPACE_ORIGIN"

# v1 single-repo boot: with the bare, master and standing-agent roles gone, the
# task-agent is the only kind that still takes this path (`cld run` has its own
# image and entrypoint, imgs/claude-run). The three-branch workspace logic (warm
# restart / reattach / first launch) lives in cld_boot_workspace (vcs-lib.sh),
# shared with the ticket loop above. Base revision comes from
# AGENT_REVISION_HINT, a change id the host resolved from its own jj view.
if ! cld_boot_workspace "$WORKSPACE_ORIGIN" /workspace/current "$BOOKMARK" \
        "${AGENT_REVISION_HINT:-@}" "${AGENT_ANCHOR_MODE:-isolated}" env; then
    exit 1
fi
AGENT_ANCHOR_HASH="$CLD_BOOT_ANCHOR"
export AGENT_ANCHOR_HASH

: > "$CLD_ANCHORS_FILE"
if ! cld_guard_anchor current /workspace/current "$AGENT_ANCHOR_HASH" \
        "${AGENT_ANCHOR_MODE:-isolated}" jj; then
    echo "Error: anchor guard setup failed" >&2
    exit 1
fi
cld_set_push_bookmark_template /workspace/current jj

cld_enable_watchman /workspace/current

cd /workspace/current

# The deliverable branch is a *second*, durable bookmark that survives
# teardown (the session bookmark does not). Created at the anchor so it
# has a base to exist at; from then on only the agent moves it, by squashing its
# work into it on wrap-up. Create-if-absent, so a restart or reattach never
# rewinds a branch that already advanced.
if [ -n "${TASK_AGENT_MODE:-}" ] && [ -n "${AGENT_DELIVERABLE_BRANCH:-}" ]; then
    if [ -z "${AGENT_ANCHOR_HASH:-}" ]; then
        echo "[WARN] no anchor recovered -- skipping deliverable bookmark '$AGENT_DELIVERABLE_BRANCH'" >&2
    elif jj bookmark list -T 'name ++ "\n"' | grep -qx "$AGENT_DELIVERABLE_BRANCH"; then
        echo "[cld] deliverable bookmark '$AGENT_DELIVERABLE_BRANCH' already exists -- leaving it alone"
    elif jj bookmark create "$AGENT_DELIVERABLE_BRANCH" -r "$AGENT_ANCHOR_HASH"; then
        echo "[cld] deliverable bookmark '$AGENT_DELIVERABLE_BRANCH' created at ${AGENT_ANCHOR_HASH:0:12}"
    else
        echo "[WARN] could not create deliverable bookmark '$AGENT_DELIVERABLE_BRANCH'" >&2
    fi
    # Wrap-up may push this branch to the remote; a remote missing from the
    # host's known_hosts would fail host-key verification.
    seed_known_hosts
fi

build_claude_config

if command -v poetry &>/dev/null; then
    while IFS= read -r pyproject; do
        project_dir=$(dirname "$pyproject")
        echo "[INFO] Installing poetry packages in $project_dir"
        (cd "$project_dir" && poetry install --no-interaction -q >/dev/null 2>&1) || \
            echo "[WARN] poetry install failed in $project_dir (continuing)"
    done < <(find "$WORKSPACE_CURRENT" -maxdepth 3 -name pyproject.toml \
        -not -path '*/.*' -not -path '*/node_modules/*' -not -path '*/.venv/*' 2>/dev/null)
fi

CLAUDE_BIN=$(which claude)
# --add-dir /opt/cld surfaces the baked-in .claude/skills/ (messenger-*,
# task-agent-*) regardless of which repo is mounted at /workspace/origin;
# settings.json's permissions.additionalDirectories grants file access only
# and does not trigger skill auto-loading, so this must be a CLI flag.
CLAUDE_EXTRA_ARGS="--dangerously-skip-permissions --add-dir /opt/cld"
if [ -n "${AGENT_MODEL:-}" ]; then
    CLAUDE_EXTRA_ARGS="$CLAUDE_EXTRA_ARGS --model $AGENT_MODEL"
fi
printf '#!/bin/bash\nexec %s %s "$@"\n' "$CLAUDE_BIN" "$CLAUDE_EXTRA_ARGS" > /tmp/bin/claude
chmod +x /tmp/bin/claude

# AGENT_MODE is the headless mailbox-supervisor branch; the task-agent role is
# its only producer (cld/docker.py:build_container_args), and TASK_AGENT_MODE
# always rides along with it.
if [ -n "${AGENT_MODE:-}" ]; then
    if [ "$MAILBOX_OK" -ne 0 ]; then
        echo "Error: a task-agent cannot start without its mailbox (see error above)" >&2
        exit 1
    fi
    touch /tmp/cld-agent-ready               # host readiness sentinel (/tmp is non-root writable)
    exec python3 -P -m cld.messenger.agent_loop
fi

# Every v1 launcher sets one of the modes above; reaching here means the
# container was started without one, which is a launcher bug, not a session.
echo "Error: no container mode set (expected TICKET_MODE or AGENT_MODE)" >&2
exit 1
