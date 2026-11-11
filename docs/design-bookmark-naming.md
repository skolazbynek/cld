# Naming convention

Status: implemented. A first version, to be revisited after use.

## Goal

Every object cld spawns (ticket, task-agent, run, chain, chain step) has one
**identifier**, built from the `<name>` it was spawned with. The identifier
names the session everywhere: docker container, jj workspace, session
bookmark, mailbox folder, broker caller id, output folders, telemetry. Other
bookmarks the object creates are the identifier plus more dotted parts.

Bookmarks the user creates on the host are never renamed, checked or deleted.

## Terms

- `<ticket>`: `ticket_slug()`, `[a-z0-9-]`, no leading or trailing dash.
- `<agent>`: the task slug, `[a-z0-9-]`, plus a `-N` suffix only when the same
  ticket already has a live agent of that slug (`allocate_task_agent_name`).
- `<chain>`, `<step>`, `<run>`: `[A-Za-z0-9_-]+`.
- `<word>`: a bookmark description, `[a-z0-9-]+`. Several may follow each
  other (`t.<ticket>.mr.migration`).
- No part ever contains a dot. The dot is the only separator.

## The names

| Object | Identifier = session bookmark | Other bookmarks |
|---|---|---|
| Ticket | `t.<ticket>` | MR: `t.<ticket>.mr` or `t.<ticket>.mr-<word>`. Other: `t.<ticket>.<word>[.<word>...]` |
| Task-agent of a ticket | `a.<ticket>.<agent>` | MR: `<id>.mr` (default) or `<id>.mr-<word>`. Other: `<id>.<word>[...]` |
| Task-agent, no ticket | `a._host.<agent>` | same as above |
| Run | `run.<run>` (default `<run>`: random hex) | none |
| Chain | `chain.<chain>` (`-n` required) | none (the bookmark is the chain's result) |
| Chain step | `chain.<chain>.<step>` | none |
| No owner (host automation) | -- | `internal.<word>` |

Notes:

- The session bookmark of a ticket or agent is static at its anchor. A run's
  or chain step's bookmark is moved onto its finished work. Both are simply the
  identifier, as today.
- No part is reserved.
- Every agent identifier has three parts. `_host` stands in for the ticket
  when there is none; ticket slugs never contain `_`, so it cannot be a real
  ticket.
- The ticket of a task-agent is read from its parent's identifier: `t.<t>`
  gives `<t>`; `a.<t>.<x>` (an agent started by an agent, which D14 in
  `docs/design-task-agents.md` forbids but the broker's task-agent route does
  not enforce) gives `<t>` too. No parent: `_host`.
- The parallel-group index in today's step names (`chain_<c>_<idx>_<step>`) is
  dropped: step names are unique across a chain (`_validate_step_shape`).
- Listing: `<id>.*` does not include `<id>` itself. One ticket's own
  bookmarks are `bookmarks(exact:"t.<t>") | bookmarks(glob:"t.<t>.*")`; its
  agents are `bookmarks(glob:"a.<t>.*")`. A bare `t.<t>*` would also match a
  ticket named `<t>-v2`.
- The messenger keeps accepting a bare `to=<ticket>` as a shortcut for
  `t.<ticket>`.
- Claude inside a container never uses `internal.`; it uses its owner's
  identifier.
- `jj git push --change` would create `push-<id>`. Ticket and task-agent
  containers set `templates.git_push_bookmark` in their jj workspace config
  (`cld_set_push_bookmark_template` in `vcs-lib.sh`; the value is a TOML
  literal string, as jj does not parse a bare one that starts with a quote) to
  `"<id>.mr-" ++ change_id.short()`.

## Chain names

Today the name defaults to the chain file's `name:` field (`@rdi` -> `rdi`),
so two tickets starting the same file collide on container names and on the
result bookmark. New rule:

- `-n <chain>` is required for `cld chain run` on the host and for
  `cld chain start` through the broker. The file's `name:` field no longer
  names anything.
- The name is in use, and the start is refused, when a chain of that name is
  running on the host (any repo, since container names are host-wide), or when
  the bookmark `chain.<chain>` already exists in the target repo (it holds an
  earlier result that would be overwritten). The error names which one.

## Constraints behind the names

- **git:** dots are allowed inside a ref name, but not `..`, a leading or
  trailing dot, or a trailing `.lock`. No `/` means no leaf-and-folder
  conflicts: `t.us` and `t.us.mr` coexist. Verified on jj 0.45.1: every form
  in the table was created and exported to git cleanly.
- **jj revsets:** dotted names work as bare symbols, including inside
  functions (`descendants(t.us)`) and with `@<remote>`. Verified. Workspace
  names with dots work too: `jj workspace add --name t.x-y` and
  `jj log -r 't.x-y@'` are covered by `TestDottedNames` in
  `tests/test_vcs_integration.py`.
- **docker:** names allow `[a-zA-Z0-9][a-zA-Z0-9_.-]*`, so identifiers fit.
  `docker ps --filter name=^<id>$` treats the value as a regex: every call
  must escape the dots, or `t.us` also matches `tXus`.
- **host-wide uniqueness:** docker, the mailbox root and `~/.cld/broker-jobs/`
  are shared by the whole host. The `t.` / `a.` / `run.` / `chain.` prefixes
  keep kinds apart and keep tickets from clashing with unrelated containers
  (a ticket named `postgres`) or the jj `default` workspace. The ticket (or
  `_host`) inside an agent identifier keeps two tickets' `review` agents apart.

## Where the identifier goes

| Place | Value |
|---|---|
| Docker container name, `org.cld.session` label, `SESSION_NAME` / `CONTAINER_NAME` env | `<id>` |
| Session bookmark; jj workspace name and `<workspace>@` revsets; git worktree branch | `<id>` |
| Mailbox folder, messenger address, `--peer`, mailbox meta (`parent`, peers) | `<id>` |
| Broker caller id, `~/.cld/broker-jobs/<id>/`, `gql-<id>` | `<id>` |
| Anchor marker `cld anchor: <id> mode=...` | `<id>` |
| git anchor pin, hidden ref (`names.anchor_ref`; copied in `vcs-lib.sh`) | `refs/cld/anchor/<id>` |
| git archive of the session branch tip at shutdown (`names.ended_ref`) | `refs/cld/ended/<id>/<UTC stamp>`; detached-HEAD work `.../<stamp>-detached` |
| Telemetry `service.name` | `<id>` (history restarts under the new name) |
| `agent-output-<id>/` in run and chain commits | `<id>` |
| `.cld/chains/<id>/` state folder, `chain-outputs/<chain>/` | chain `<id>` / `<chain>` |

Session bookmark, workspace name and container name stay one string, as
today, so the code that relies on that stays as it is. Only how the string is
built changes.

The two git refs sit outside `refs/heads` on purpose: no branch list shows
them and `git push --all` skips them. A git ticket's session branch `<id>` is
the worktree branch and moves with the work, so the anchor ref, not the
branch, finds the scratch commit again. The `ended` refs hold work that
shutdown would otherwise lose to gc; the id has no `/`, so it is a folder of
its own and never collides with another ticket's refs.

## No backwards compatibility

Old names (`cld_ticket_<t>`, `cld_agent_<repo>_<a>`, `run_<r>`,
`chain_<c>_[<idx>_]<step>`) are not read, renamed or migrated. Before
upgrading, the user reaps all task-agents and shuts down all tickets; old
bookmarks, mailbox folders and output folders are removed by hand.

## Implementation

See `docs/impl-naming-plan.md`.

## Open points

- Runs keep a random default name; they never collide, so they do not need a
  required `-n`.
- Enforcement is prompt-only; the anchor guard does not refuse names. Without
  a suffix, a session bookmark reads like a work branch; the guard already
  keeps agents from moving bookmarks outside their own tree.
