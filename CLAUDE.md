# CLAUDE.md

Docker container orchestration for Claude Code with jujutsu/git workspace isolation.

## Roles

- **cld task-agent** — bounded task-scoped with peer messaging
- **cld run** — one-shot autonomous headless agent

Prefers jujutsu; falls back to git. Git tickets get the scratch commit (pinned by `refs/cld/anchor/<id>`), the guard's file-write and drift checks, `--reanchor`, and broker tests/GraphQL on committed work. Still jj only: overlap check, watchman snapshots, commit immutability and git command parsing in the guard, task-agents, broker run/chain. See README *Git users*.

## Core Contracts

**Anchor + descendant tree:** Containers can only edit changes descending from anchor (`-r`, default `@`). Works in isolated `/workspace/current`; origin at `/workspace/origin` holds only the jj/git store (never the host checkout), with host-trusted settings read-only. See `docs/design-secrets-containment.md`.

**Persistence:** Sessions survive restart via jj bookmarks (git: the session branch). Watchman snapshots autonomously (jj only; on git only commits persist). Git shutdown archives the branch tip under `refs/cld/ended/<id>/...`.

**Config:** defaults → `~/.config/cld/config.toml` → repo `.cld/config.toml` → `.env` → `CLD_*` env vars. Key: `mailbox_root`, `agent_max_turns`, `max_task_agents`, `broker_*`.

**Naming:** every spawned object has one dotted identifier (container, jj workspace, session bookmark, mailbox, broker caller id): ticket `t.<ticket>`, task-agent `a.<ticket>.<agent>` (`a._host.<agent>` without a ticket), run `run.<run>`, chain `chain.<chain>` (`-n` required), chain step `chain.<chain>.<step>`. Agent MR default `<agent id>.mr`. Built in `cld/names.py`. See `docs/design-bookmark-naming.md`.

## Anchor Modes

- **Isolated (default):** Edit one branch only. No touching siblings.
- **Shared (`--shared-anchor`):** Edit entire subtree. Needs approval.

No overlap: live anchor trees checked host-wide (jj repos only).

## Subsystems

**Workspace files:** `ignore_gitignore` is off: listed files are skipped with a warning. No host checkout file reaches a container.

**Broker:** SSH ForceCommand for tests, container enumeration, task-agent lifecycle and the GraphQL server. Runner and server containers get the same store-only mounts at `/repo` and a private 0600 copy of `.env`, read without following symlinks. See `broker/README.md`.

**Broker run/chain:** a ticket container starts `cld run` / `cld chain` on a committed change in its own tree through the broker (`start|status|wait|cancel`, at most 2 live jobs). See skill `broker-run-chain`.

**Messenger:** Mailbox at `~/.cld/mailboxes`. `send()/list_inbox()/read_message()/archive()` via MCP. Hop budgets; explicit reply obligations. See `docs/design-agent-messaging.md`.

**Chain:** `cld chain <file.yaml>` runs declarative multi-step pipeline. Persistent accumulator; transient per-step branches.
