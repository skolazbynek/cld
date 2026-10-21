# cld

Run Claude Code in Docker containers with VCS workspace isolation. Supports **jujutsu (jj)** natively and **falls back to git** when jj is not installed. Each container gets its own isolated workspace (jj workspace or git worktree) and branch, so multiple agents can work on the same repo concurrently without conflicts.

The primary interactive workflow is the **ticket container** (v2): one container per ticket, mounting one or more registered repos, driven from the host shell via `cld claude <ticket>`. See [Ticket containers](#ticket-containers-v2).

## Prerequisites

- Docker
- A **jujutsu** or **git** repository (jj preferred; git used as fallback). Git submodules are not supported: a repo with a `modules/` folder in its git dir is refused.
- Host jj 0.38 or newer. cld refuses to mount a jj repo with an older host jj.
- Python 3.11+ with [Poetry](https://python-poetry.org/)
- Probably not Windows

## Setup

```bash
# Install with poetry
poetry install

# You can run from the poetry environment within CLD repo
poetry run cld --help

# To run from any directory, add `/.venv/bin/cld` to your PATH. I have `~/.local/bin/cld` symlink pointing there.

# Build images (one command builds all three: claude-base first, then claude-devcontainer and claude-run)
cld build [--no-cache]
```

The v1 verbs (`cld task-agent`, `cld run`, `cld chain`) must be run from within a VCS repository (jj or git) -- they operate on the cwd repo. The ticket verbs (`cld start`, `cld claude`, ...) run from anywhere: their repos come from the registry or from explicit paths.

## Usage

```bash
# Show information
cld --help

# Ticket container (v2): one container per ticket, N repos, host-driven
cld repos add lide-api ~/projects/lide-api    # register repos once
cld start LIDE-2600 lide-api                  # create the ticket sandbox
cld claude LIDE-2600                          # run Claude in it, from the host
cld claude LIDE-2600 -- --continue            # resume the ticket's last session
cld {stop | restart | shutdown | status | logs} <ticket>

# One-shot autonomous run (headless, --rm, commits to a branch)
cld run [refs...] [-n name] [-m model] [-r revision] [-p prompt]
cld run -p "Fix the auth bug in src/login.py"
cld run task.md -p "Focus on the database layer"

# Declarative multi-agent chain
cld chain run @review-implement task.md
cld chain run chains/parallel-review.yaml -p "Focus on auth code"
cld chain list
cld chain validate chains/my-chain.yaml
cld chain dry-run @review-implement

# --- v1 headless role (superseded by ticket containers; still works) ---

# Task-scoped headless Claude agent (one per task, mailbox-driven)
cld task-agent start [refs...] -n <slug> [-p prompt]
cld task-agent {status | logs | transcript | shutdown}
```

## Ticket containers (v2)

One container per ticket. The ticket (by convention a YouTrack id) is the unit of work; the container is its sandbox. You never shell in for normal work -- the cockpit is your host terminal, and `cld claude <ticket>` execs the Claude harness inside the container. The harness sees all the ticket's repos as subdirectories of one working set:

```
/workspace/<slug>/            harness cwd -- the "ticket root" (with a generated CLAUDE.md)
  <repo-a>/                   jj workspace (or git worktree) of repo-a
  <repo-b>/
/workspace/origin/<repo-a>/   the host repo's jj/git store only (.jj, .git), never the checkout
/workspace/origin/<repo-b>/
```

Full product spec: `PRODUCT_DESIGN.md`; implementation design: `docs/design-ticket-containers.md`.

### Repo registry

Repos are addressed by name from a registry in `~/.config/cld/config.toml`:

```bash
cld repos                                        # list registry + which tickets mount each repo
cld repos add lide-api ~/projects/lide-api [--default-rev R] [--bootstrap]
cld repos rm lide-api                            # refused while a ticket container mounts it
```

Each entry is a `[repos.<name>]` TOML table: `path`, optional `default_rev` (anchor offered at launch; empty means `trunk()`), and `bootstrap` (run `poetry install` in the repo's `pyproject_dir` on first boot). `cld repos add/rm` rewrites the config with tomlkit, preserving your comments and unrelated keys. Ad-hoc paths are also accepted at launch (the basename becomes the subdir name).

### Lifecycle

| Verb | Semantics |
|---|---|
| `cld start <ticket> [repo[@rev]...]` | Create-or-start. On a TTY with no repos: interactive picker over the registry. Each repo arg is a registry name or path, with an optional `@rev` anchor override. Against an existing ticket, a different repo set shows a diff, asks to confirm, and recreates the container. Bare `cld start` (no args at all) targets the cwd's repo directly -- ad hoc, regardless of registration -- under a ticket named after the repo directory. |
| `cld claude [<ticket>] [-- args...]` | The daily verb: exec the harness at the ticket root. Everything after `--` passes to claude (`--model`, `--continue`, `--resume`, `-p`). One live session per container (POC): a second `cld claude` is refused, naming the holder. |
| `cld shell [<ticket>]` | Escape hatch: interactive bash in the container, for debugging the sandbox itself. |
| `cld stop [<ticket>]` | Pause (`docker stop`). Workspaces stay in place; `cld start` on a stopped ticket is a warm start. |
| `cld restart [<ticket>] [--reanchor repo@rev]...` | Recreate the container from its persisted launch manifest (labels, read back before removal -- no argument drop). Workspaces reattach at their bookmarks; venvs and caches in the container layer are lost. `--reanchor` (repeatable, jj only) moves a repo's anchor commit onto a new revision first; the revision defaults to `@`, the repo's working copy on the host. |
| `cld shutdown [<ticket>] [--all]` | End of ticket: teardown, forget the ticket's bookmark and workspace in every mounted repo. Commits always survive in the repo stores. |
| `cld status [<ticket>]` | Roster (repos, state, live session) or one-ticket detail (per-repo anchors, modes, bookmark tips, session, uptime). |
| `cld logs [<ticket>] [-n N]` | Entrypoint/boot logs. |

Without a ticket, `claude`, `shell`, `stop`, `restart`, `shutdown` and `logs` target the ticket bare `cld start` creates: the one named after the cwd repo's directory. Bare `cld status` stays the roster.

Resume: transcripts land on the host under the per-ticket cwd slug, so `cld claude <ticket> -- --continue` resumes the ticket's latest conversation and `-- --resume` lists only that ticket's sessions. This works even after `cld shutdown` followed by a fresh `cld start` of the same ticket -- cld itself keeps no session state.

### Anchors per repo

At first launch, each repo's anchor revision resolves as: explicit `@rev` from the launch args, else the registry `default_rev`, else `trunk()`. In isolated mode (default), a scratch commit is staged as a child of the anchor and the ticket may edit only its descendants; `--shared-anchor <repo>` (repeatable, per repo) widens the editable tree to every descendant of the anchor itself. The anchor is pinned by its jj change ID (a commit hash on git), so a host-side rebase of the anchor keeps it the same anchor; no container ever rewrites, moves or rebases it. The contract is policy, enforced by prompt and convention, not mechanism.

When a new ticket's anchor lies inside another live ticket's editable tree in the same repo, cld **warns and proceeds** (stacked tickets are legitimate). Anchoring inside a headless container's tree (`task-agent`, `run`) still blocks, in both directions.

**Git-backed repos get weaker guarantees:** no scratch commit (the effective anchor is the base itself), no overlap check, no watchman snapshots -- worktree semantics only.

### Persistence

Commits, the op log, and the per-repo ticket bookmark live in each host repo's store and survive everything short of `shutdown` (which forgets the bookmark; commits remain). Uncommitted edits are watchman-snapshotted into the store. Workspace dirs, venvs and caches live in the container layer: they survive `stop`, are rebuilt (workspaces) or lost (venvs) on `restart`/recreate, and are gone after `shutdown`.

### Changes from v1 behavior

Verified deliberate differences from the v1 roles:

- **Default anchor is `default_rev` -> `trunk()`, not `@`.** A v2 launch happens from anywhere, so each repo's `@` is invisible and may be unrelated WIP. Stacked work uses an explicit `repo@rev`. (v1 kinds keep defaulting to `@`.)
- **Host-side `cld msg` prefers a single running ticket.** Identity resolution is: explicit `--ticket` flag or `CLD_TICKET` env, else the one running ticket container if exactly one exists, else the cwd repo's own host identity (the mailbox the v1 master used to own). cwd is deliberately not mapped to tickets -- several tickets can mount one repo.
- **Parallel same-base siblings need no placeholder commits.** The overlap check now derives each occupant's *effective* anchor (the scratch commit in isolated mode) from the jj store at check time, for v1 headless kinds too, so two isolated containers anchored on the same base no longer over-block each other.
- **A kept repo whose anchor changed on a repo-set change reattaches at its old bookmark.** `cld start <ticket> <new set>` recreates the container, but a kept repo's bookmark survives, and the boot's reattach branch wins over the new `anchor_base` -- the new anchor takes effect only after `cld shutdown` forgets the bookmark.
- **One GraphQL test server per ticket session, even multi-repo.** The broker's `graphql start` names the server container by session; a `start --repo b` while a server for repo a runs returns the running server's status rather than launching a second one. `graphql stop` tears down against the repo the server was *started* for (its own label), whatever `--repo` says.

### Migration from v1

1. **Reap any v1 task-agents first** (`cld task-agent shutdown --all`) -- bookmark/workspace hygiene in every repo store. (The `cld master` and standing `cld agent` roles are both gone; if such a container from an older cld is still around, remove it with `docker rm -f` and `jj bookmark forget` its session.)
2. **Register each repo you work on:** `cld repos add <name> <path>`.
3. Muscle memory:

| v1 | v2 |
|---|---|
| `cld master` (removed) | `cld start <ticket> <repo>` + `cld claude <ticket>` |
| `cld agent` (removed) | `cld task-agent start ... -n <slug>` for bounded headless work |
| in-container shell work | `cld claude <ticket>` (daily) / `cld shell <ticket>` (sandbox debugging) |
| `cld master shutdown` | `cld shutdown <ticket>` |
| `cld master status` / `logs` | `cld status [<ticket>]` / `cld logs <ticket>` |
| `-m model` / `-p prompt` at launch | per invocation: `cld claude <ticket> -- --model ... -p ...` |

`cld run`, `cld chain`, the broker and the messenger are unchanged; inside a ticket container the broker's `run-tests`/`graphql` take `--repo <name>` when several repos are mounted (with exactly one repo, `--repo` may be omitted).

## Agent workflow

Agent containers run detached and auto-remove on exit. Results are committed to the agent's branch as `agent-output-<session>/` containing `agent.log`, `result.json`, and `summary.json`.

## Chain workflow

`cld chain` runs a declarative sequence of named agents defined in a YAML file. Each step is an autonomous agent that receives the prior step's output as context. Steps can run in parallel (a `parallel:` group); the synthesiser step that follows sees a combined summary. Built-in chains live in `chains/` in the repo and in the installed package; reference them with `@name` shorthand.

```bash
# Run a built-in chain against a task file
cld chain run @review-implement task.md

# Run with an inline prompt instead of a task file
cld chain run @review-implement -p "Fix the N+1 query in user_repo.py"

# Run a local chain file
cld chain run chains/parallel-review.yaml task.md

# Inspect without running
cld chain list
cld chain validate @parallel-review
cld chain dry-run @review-implement
```

**Built-in chains:**

`chains/review-implement.yaml` — reviewer flags issues, implementer fixes them:

```yaml
name: review-implement
description: Reviewer flags issues, implementer fixes them.

defaults:
  model: sonnet

steps:
  - name: review
    prompts: ["@personas/reviewer"]

  - name: implement
    prompts: ["@personas/implementer"]
```

`chains/parallel-review.yaml` — two reviewers in parallel; synthesiser ranks findings:

```yaml
name: parallel-review
description: Two reviewers in parallel; synthesiser picks the most actionable.

defaults:
  model: sonnet

steps:
  - parallel:
      - name: generic
        prompts: ["@personas/reviewer"]
      - name: security
        prompts: ["@personas/security-reviewer"]

  - name: synthesise
    prompts: ["@personas/reviewer"]
    prompt: |
      Two prior reviewers produced findings. Combine them, deduplicate,
      and rank by severity. Drop anything that contradicts the user's
      original task.
```

**YAML field reference:**

| Field | Level | Description |
|---|---|---|
| `name` | chain, step | Identifier; used as branch name suffix |
| `description` | chain | Human-readable summary shown by `cld chain list` |
| `defaults` | chain | Default values applied to every step (`model`, `timeout`) |
| `steps` | chain | Ordered list of step or `parallel` group items |
| `parallel` | step item | List of steps to run concurrently |
| `persona` | step | Claude persona / system-prompt name |
| `model` | step, defaults | Claude model override for this step |
| `timeout` | step, defaults | Per-agent timeout in seconds (0 = inherit `CLD_AGENT_TIMEOUT`) |
| `prompt` | step | Extra instructions appended to the step's system prompt |
| `output` | step | Explicit output file path committed by this step |
| `inputs` | step | List of prior step names whose output this step receives |

**Limitations (PoC scope):** no loops, no conditionals. For parallel groups, code changes committed by non-first siblings are not visible to the next sequential step — only text output is forwarded.

Chain env vars (see *Configuration* below): `CLD_CHAIN_MAX_PARALLEL` caps concurrent siblings; `CLD_CHAIN_DEFAULT_MODEL` overrides the model for all steps.

## VCS Backend

The tool auto-detects the VCS backend:

1. If `.jj/` exists and `jj` is installed -- **jujutsu backend** (preferred)
2. If `.git/` exists and `git` is installed -- **git backend** (fallback)

This detection runs both on the host (CLI commands) and inside containers (entrypoints). The abstraction layer lives in `cld/vcs/` (Python) and `imgs/claude-devcontainer/vcs-lib.sh` (shell).

| Concept | jujutsu | git |
|---|---|---|
| Repository marker | `.jj/` | `.git/` |
| Workspace isolation | `jj workspace add` | `git worktree add` |
| Named ref | bookmark | branch |
| Current change | `@` | `HEAD` |
| Commit | `jj commit` (auto-tracks) | `git add -A && git commit` |
| Read file from revision | `jj file show -r <rev> <path>` | `git show <rev>:<path>` |
| Common ancestor | `fork_point(A \| B)` | `git merge-base A B` |

## Messenger

Lets any cld container (ticket or task-agent) send a message to any other and get a reply on its next turn, backed by a shared mailbox directory on the host -- no threads, no polling required from the user. Full design and mental model: `docs/design-agent-messaging.md`.

Ticket containers get a mailbox named `cld_ticket_<slug>` and can be addressed by the ticket slug; a slug that is also some repo's basename is an ambiguity error naming both. Host-side `cld msg` acts as: the `--ticket` flag (or `CLD_TICKET` env), else the single running ticket container if exactly one exists, else the cwd repo's own host identity.

```bash
# Register for host use (user-scoped, works from any directory)
claude mcp add -s user messenger -- /path/to/cld/scripts/mcp/run-messenger.sh

# Spawn a headless task-agent for the current repo
cld task-agent start @personas/implementer -n add-oauth -p "Add OAuth login."

# From any other container (a ticket or another agent), message it by container name
# (inside Claude): mcp__messenger__send(to="cld_agent_my-repo_add-oauth", subject="...", body="...", expects_reply=True)
```

**Tools:** `send(to, subject, body)`, `list_inbox(unread_only)`, `read_message(id)`, `archive(id)`, `list_agents(kind)`.

**Lifecycle:**
```bash
cld task-agent start ... -n <slug>   # spawn (every start is a new container)
cld task-agent status [<slug>]       # roster, or one agent's phase / session / cost
cld task-agent logs <slug> [-n N]    # tail its supervisor log
cld task-agent transcript <slug>     # the mailbox conversation (survives a reap)
cld task-agent shutdown <slug>       # stop + remove + cleanup
```

A task-agent has one persistent Claude session that survives across messages -- it remembers prior conversations with a given sender, so follow-ups like "for question a, RESTRICT" resolve without re-stating context. A message sent with `expects_reply` gets exactly one reply; if the agent's turn doesn't call `send()`, the supervisor synthesizes a fallback so a question is never left hanging. Without that flag the agent stays quiet by design -- an unconditional reply makes each acknowledgment oblige another one.

## Architecture

```
cld/                               Python package (CLI + shared logic)
  cli.py                           host typer app (all docker-daemon verbs)
  cli_container.py                 container typer app, shipped as `cld` in the image
  registry.py                      named repo registry (`cld repos`, tomlkit config writes, picker)
  manifest.py                      ticket launch manifest (schema, label codec, resolve, diff)
  ticket.py                        ticket container lifecycle (start/claude/stop/restart/shutdown/status/logs)
  task_agent.py                    task-agent helpers shared by both apps
  docker.py                        container arg building, image management, path translation
  run.py                           one-shot run launch logic (`cld run`)
  chain.py                         declarative multi-agent chain runner
  vcs/                             VCS abstraction layer
    base.py                        abstract VcsBackend interface
    jj.py                          jujutsu backend (preferred)
    git.py                         git backend (fallback)
    detect.py                      auto-detection logic
  mcp/messenger.py                 MCP server for the mailbox transport
  mcp/graphql.py                   MCP server for GraphQL testing -- thin client over the broker's `graphql` action
  messenger/mailbox.py             filesystem mailbox transport
  messenger/agent_loop.py          headless agent supervisor daemon (task-agents)

scripts/
  mcp/run-messenger.sh             venv wrapper for the messenger MCP server
  mcp/run-graphql.sh               venv wrapper for the graphql-tester MCP server

broker/                            host-side broker: sshd ForceCommand dispatching run-tests/list-containers/task-agent/graphql actions
graphqlserver/                     image serving a project's GraphQL server at a jj revision, driven by the broker's graphql action

imgs/
  claude-base/                     Common base image (debian, git, jj, docker cli, poetry, claude). No editor, no entrypoint.
    Dockerfile.claude-base
  claude-devcontainer/             Devcontainer image (FROM base, adds neovim + classic vim)
    container-init.sh              Shared init (MCP config merge) -- baked into base
    vcs-lib.sh                     Shell VCS abstraction (sourced by both entrypoints) -- baked into base
    entrypoint-claude-devcontainer.sh
  claude-run/                      One-shot run image (FROM base, adds run entrypoint + system prompt)
    entrypoint-claude-run.sh

prompts/                           Reusable task prompts for agents
```

**Image hierarchy:** `claude-base` is the parent of both `claude-devcontainer` and `claude-run` (siblings). Always build base first; `cld build` handles all three in order.

### Workspace isolation

Containers never mount the host checkout. They mount only the host repo's jj/git store at `/workspace/origin` (ticket containers: one store per repo at `/workspace/origin/<name>`), as the eleven-row mount set of `docs/design-secrets-containment.md` section 3.1. The store stays writable. The settings the host trusts stay read-only: the host default workspace's working-copy state, the jj config ids, `git_target`, and the git dir's `config`, `hooks` and `commondir`. So `.env` and other untracked host files never reach a container, and plain `jj` in origin fails. Before each launch a host preflight checks the store and refuses an unsafe one (host jj older than 0.38, a jj id missing next to a legacy config file, a `git_target` outside the repo, a foreign `commondir`, a git `modules/` folder, or another container that mounts the repo without the `org.cld.origin-mount=store-v1` label). The container's own entrypoint runs `jj workspace add` / `git worktree add` on boot, with `--ignore-working-copy` for every jj call in origin; the workspace directory lives in the container's own filesystem layer at `/workspace/current` (tickets: `/workspace/<slug>/<name>`), never on the host. The launcher refuses an image without the label `org.cld.boot-api=2` (rebuild with `cld build`). jj writes all store objects through the writable store rows, and watchman snapshots edits into the store autonomously, so work is durable and host-visible without a host-side workspace directory. The `-r` flag pins the anchor revision for v1 kinds (default: `@` for jj, `HEAD` for git; tickets default to the registry `default_rev`, falling back to `trunk()`). On shutdown, the session's bookmark and workspace registration are forgotten from the origin store host-side, by `cld shutdown` / `cld <role> shutdown`; committed work persists.

### Host file protection

Host `~/.claude.json` is mounted read-only. The entrypoint builds a container-local copy with MCP servers merged for the container's project path.

All RO `$HOME` mounts (claude/anthropic/jj configs, `~/.claude.json`, plus devcontainer-only `~/.gitconfig`, `~/.bashrc`, and the nvim dirs `~/.config/nvim` / `~/.local/state/nvim` / `~/.cache/nvim`) are staged read-only under `/tmp/host-config/<rel>` and copied into `$HOME` on startup by `copy_host_configs`. Changes made inside the container do not persist to the host. The agent image has no editor and skips the devcontainer-only entries.

Plugin marketplaces with a `directory` or `file` source are mounted read-only at their host path, with `--mount`, only when they come from the host `~/.claude` settings or plugin registry and the folder passes these checks: it holds `.claude-plugin/marketplace.json`; it is outside `~/.claude`, the mailbox root and every repo, and is not `$HOME` or one of its parents; and its literal path equals its real path (no symlink in it). Marketplaces listed in a repo's own `.claude/settings.json` are never mounted. Each skipped entry logs `plugin marketplace: '<path>' skipped: <reason>` on every launch.

### No docker socket in containers

No container mounts `/var/run/docker.sock` (it was equivalent to host root; see the security notes below). What needs in-container docker goes through the host broker over SSH:

- **Peer enumeration** (`list_agents`): the broker's `list-containers` action runs `docker ps` on the host and streams structured records back. Agents don't enumerate at all -- message replies address the sender by the full name carried in the message, delivered by filesystem, so agents need no host channel.
- **Running tests and the GraphQL server** (`run-tests`, `graphql`): host-side, against the repo the caller's own `org.cld.repo-root` label names, in a throwaway container the caller never sees. That container gets the same store-only mounts at `/repo` and a private 0600 copy of the repo's `.env`, never the file itself.

See `cld/broker.py` (the host-vs-broker seam) and `broker/cld-broker.py` (the actions). Path translation (`CLD_HOST_PROJECT_DIR`/`CLD_HOST_HOME`) still converts container paths to host paths; it is set unconditionally rather than riding along with the socket mount.

### Security model and known gaps

Containers run as host UID/GID with `--cap-drop=ALL`, `--security-opt=no-new-privileges`, and resource limits (2 CPU, 4GB RAM).

**Known gaps -- read carefully before shipping anything sensitive into a container:**

- **No outbound network firewall.** Once an agent is running, it can reach any host on the public internet and exfiltrate anything mounted in (`~/.claude` tokens, `~/.claude.json` MCP creds, `~/.config/*` creds). Anthropic's reference devcontainer ships an `init-firewall.sh` with default-deny outbound and a small allowlist; cld does not (yet) ship an equivalent.
- **No docker socket is mounted (was: host root).** Earlier versions mounted `/var/run/docker.sock` for peer enumeration, which let an agent run `docker run -v /:/host --privileged ...` and read or modify anything on the host -- bypassing every other control. The socket is gone; a container's only host channel is now the broker key (a fixed, non-eval action allowlist with label-validated targets -- see the host broker section). A leaked broker key grants that action set (run-tests, container enumeration, the GraphQL server lifecycle) for the labeled repos, which is strictly narrower than arbitrary host root but still privileged -- protect the key accordingly.
- **`~/.claude` is mounted rw.** A malicious agent can both read your OAuth tokens and overwrite session state.

## Configuration

Any command checks for `~/.config/cld/config.toml` and creates a default file if it doesn't exist. Adjust it as you need - especially what paths are mounted into the devcontainer. You can create a per-project overrides with `<repo_root>/.cld/config.toml`.

### Resolution order

Lowest → highest priority:

1. Dataclass defaults
2. User TOML — `~/.config/cld/config.toml`
3. Project TOML — `<repo_root>/.cld/config.toml` (walked up from cwd)
4. `.env` in cwd
5. `CLD_*` env vars

### TOML schema

Flat snake_case keys mirroring `Config` field names, valid in both `~/.config/cld/config.toml` (user-wide) and `<repo_root>/.cld/config.toml` (per-repo), except `home_mounts_always` and `home_mounts_devcontainer`, which only the user config may set. Unknown keys are warned about on stderr and ignored. Array-typed keys take a TOML array of strings. `host_project_dir` / `host_home` are container-internal and not exposed via TOML. The one table-typed key is the ticket repo registry, `[repos.<name>]` in the *user* config (see [Repo registry](#repo-registry)): per entry `path`, `default_rev`, `bootstrap`.

```toml
base_image = "claude-base:latest"
devcontainer_image = "claude-devcontainer:latest"
run_image = "claude-run:latest"
agent_timeout = 1800
poll_interval = 30
debug = false
```

Full set of keys:

| Key | Type | Default | Purpose |
|---|---|---|---|
| `base_image` | string | `"claude-base:latest"` | Common base Docker image |
| `devcontainer_image` | string | `"claude-devcontainer:latest"` | Devcontainer image (`cld task-agent`, ticket containers) |
| `run_image` | string | `"claude-run:latest"` | One-shot run image (`cld run`) |
| `pyproject_dir` | string | `"."` | Directory (relative to repo root) holding `pyproject.toml` and `.env`. Not a `Config` field -- read directly out of `.cld/config.toml` by `cld-broker.py` on the host, for the broker's `PROJECT_SUBDIR` and secrets path (see "Host-side test running" in `CLAUDE.md`) |
| `ssl_certs_path` | string | `""` | Opt-in override: host path (dir or PEM file) that **replaces** the baked CA bundle. Empty = use the baked bundle |
| `home_mounts_always` | array of strings | `[".claude.json", ".config/anthropic", ".config/claude", ".config/jj"]` | RO `$HOME` paths staged into every container. User config only (a repo's `.cld/config.toml` value is ignored with a warning). A path must be relative, with no `..`, no symlink in it and no overlap with a repo; others are skipped with a warning |
| `home_mounts_devcontainer` | array of strings | `[".gitconfig", ".bashrc", ".config/nvim", ".local/state/nvim", ".cache/nvim"]` | Additional RO `$HOME` paths staged only for interactive devcontainer sessions. Same rules as `home_mounts_always` |
| `repos.<name>` | TOML table | none | Ticket repo registry entry (`path`, `default_rev`, `bootstrap`); user config only, managed by `cld repos add/rm` |
| `ignore_gitignore` | array of strings | `[]` | Off. The host checkout is no longer mounted, so no file reaches the workspace: each listed file is skipped with a warning on every launch |
| `agent_timeout` | int (seconds) | `1800` | Chain orchestrator's per-agent wait timeout |
| `poll_interval` | int (seconds) | `30` | Chain orchestrator's docker-ps poll interval |
| `chain_max_parallel` | int | `4` | Max agents launched concurrently in a chain's parallel group |
| `chain_default_model` | string | `""` | Model override for chain agents; empty = each step's own default |
| `ssh_auth_sock` | string | unset (auto-detect) | SSH agent forwarding into every devcontainer-image launch (`task-agent`, tickets). Unset = auto-detect host `$SSH_AUTH_SOCK`; `""` = explicitly disable; a path = use that socket. Launches the broker makes on a container's behalf need `SSH_AUTH_SOCK` in `broker.conf` -- see `broker/README.md` |
| `mailbox_root` | string | `"~/.cld/mailboxes"` | Host root of the inter-container mailbox tree (bind-mounted RW into every task-agent and ticket container) |
| `agent_max_turns` | int | `120` | Per-message turn cap passed to the agent supervisor's `claude -p --max-turns` |
| `agent_kickoff_persona` | string | `"agent"` | Kickoff persona for the supervisor's non-task mode. Vestigial: the standing `cld agent` role that used it is gone, and every live launch is a task-agent, which composes its kickoff from the task brief instead |
| `broker_key` | string | `""` | Host path to the restricted broker **private** key. Setting this enables `cld broker <action>` inside a container |
| `broker_endpoint` | string | `"host.docker.internal:2222"` | Broker SSH endpoint, `[user@]host:port` (default login user `zet`) |
| `broker_known_hosts` | string | `""` | Host path to the pinned `known_hosts` for the broker; required for the client's strict host-key check |
| `log_level` | string | `"INFO"` | Root level for the `cld` logger hierarchy: `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `log_color` | string | `"auto"` | ANSI color in log output: `auto` (TTY-detect) / `always` / `never` |
| `debug` | bool | `false` | Diagnostics flag; back-compat alias for `log_level = "DEBUG"` when `log_level` is otherwise unset |

Every key above also has a `CLD_*` env var equivalent that overrides it (see below) except the array-typed ones (`home_mounts_always`, `home_mounts_devcontainer`, `ignore_gitignore`), the `repos` registry table, and `pyproject_dir` (not a `Config` field), which are TOML-only.

### `CLD_*` env vars (defaults shown)

| Variable | Default | Purpose |
|---|---|---|
| `CLD_BASE_IMAGE` | `claude-base:latest` | Common base Docker image |
| `CLD_DEVCONTAINER_IMAGE` | `claude-devcontainer:latest` | Devcontainer image |
| `CLD_RUN_IMAGE` | `claude-run:latest` | One-shot run image |
| `CLD_HOST_PROJECT_DIR` | `""` | Host repo root path; set by host launcher into containers for nested docker path translation |
| `CLD_HOST_HOME` | `""` | Host home directory (for path translation) |
| `CLD_AGENT_TIMEOUT` | `1800` | Chain's per-agent wait timeout (seconds) |
| `CLD_POLL_INTERVAL` | `30` | Chain's docker-ps poll interval (seconds) |
| `CLD_CHAIN_MAX_PARALLEL` | `4` | Max agents running concurrently in a parallel chain group |
| `CLD_CHAIN_DEFAULT_MODEL` | `""` | Model override for all chain steps; empty = use chain YAML default |
| `CLD_LOG_LEVEL` | `INFO` | Root level for the `cld` logger hierarchy (DEBUG/INFO/WARNING/ERROR) |
| `CLD_LOG_COLOR` | `auto` | ANSI color in log output: `auto` / `always` / `never` |
| `CLD_DEBUG` | `false` | Diagnostics flag. Back-compat alias: truthy ⇒ `CLD_LOG_LEVEL=DEBUG` |

### Logging

`cld` writes diagnostic output via the stdlib `logging` module. All log records go to **stderr**; stdout is reserved for user-facing deliverable output (final reports, list rows, prompts).

| Env var | Default | Values |
|---|---|---|
| `CLD_LOG_LEVEL` | `INFO` | DEBUG / INFO / WARNING / ERROR |
| `CLD_LOG_COLOR` | `auto` | auto / always / never |
| `CLD_DEBUG` | `false` | Back-compat alias: truthy ⇒ `CLD_LOG_LEVEL=DEBUG` |

The same keys can be set in TOML: `log_level`, `log_color`.

At DEBUG, every subprocess invocation (Docker, jj, git) and every VCS operation is logged with full command and exit code. At INFO (default), only major lifecycle events appear (agent starts/stops, image builds, chain steps).

## Development

```bash
poetry install

# Unit tests (no docker, no network)
poetry run pytest -m "not integration and not docker and not e2e"

# Integration tests
poetry run pytest -m integration

# Tests that need Docker
poetry run pytest -m docker

# End-to-end tests (slow, real containers)
poetry run pytest -m e2e
```

Test markers are declared in `pyproject.toml`. The `tests/conftest.py` detects when running inside the devcontainer via `CLD_HOST_PROJECT_DIR` to translate paths.
