# Secrets containment: no host checkout and no symlinked secrets in agent reach

**Status:** built, with the deviations in section 13. **Date:** 2026-10-21. **Ticket:** CLD-001.
**Prerequisite for:** `docs/design-broker-profile.md`, `docs/design-broker-test-jobs.md` (uses
section 6), `docs/design-broker-identities.md` (uses section 7). Ships first in their build order.
**Related:** `docs/design-ticket-containers.md` §4 and §6, `docs/design-cld-broker.md` §9,
`docs/design-anchor-guard.md`. Code cites are against the working copy of 2026-10-21.

## 1. Problem

The broker promises "secrets must never be readable by `claude`" (`docs/design-cld-broker.md:55`,
`:280-282`). Today that is false. Evidence (review findings 1 and 2, re-verified):

- **Full checkout, read-write.** Ticket containers mount each repo's host checkout at
  `/workspace/origin/<repo>` (`cld/docker.py:605-608`); task-agent and `cld run` containers mount
  one at `/workspace/origin` (`:459-461`). The agent can read `<repo>/.env` and write
  `<repo>/.cld/config.toml`. Nothing masks either.
- **`.env` linked in on request.** `link_workspace_files` symlinks listed files from origin into
  the workspace (`container-init.sh:48-72`), fed by `ignore_gitignore` (`cld/docker.py:500-504`,
  `:530-544`). The README's own example is `.env`.
- **Runners get the checkout too.** `run_tests` and `do_graphql_start` mount `<repo>:/repo`
  read-write (`broker/cld-broker.py:460`, `:1003`). Code the agent wrote runs there.
- **The broker follows symlinks.** `resolve_secrets_env_file` builds `<repo>/<subdir>/.env` and
  accepts an absolute `pyproject_dir` (`:401-407`). `_secret_mount` checks it with
  `os.path.isfile`, which follows symlinks; docker then resolves the path again at mount time
  (`:423-424`). A planted `.env` symlink puts any host file the operator can read at
  `/secrets/.env`. `lookup_alias` and `do_graphql_endpoints` read the same path (`:779-785`, `:879-886`).

Found while designing this (same root cause: agent-writable state that the host trusts):

- **Host code from VCS metadata.** `.git/config` and `.git/hooks` are on the read-write mount. A
  planted `core.sshCommand` or `pre-push` hook runs on the host at the next `jj git push`.
- **jj config from the store.** A planted `.jj/workspace-config.toml` with a `trunk()` alias
  counts once `.jj/workspace-config-id` is deleted, and forges the reviewed revision (7.3, A10).
- **Marketplace mounts.** `~/.claude` is read-write (`cld/docker.py:622`).
  `stage_plugin_marketplaces` mounts every directory named in `~/.claude/settings.json` into the
  next container (`:1266-1307`). An entry naming a repo directory brings its `.env` along.
- **Boot snapshots the host checkout.** Boot runs plain `jj` in origin (`vcs-lib.sh:107-134`,
  `entrypoint-claude-run.sh:39-46`, `agent_loop.py:414-419`), against `design-cld-broker.md` §9.

## 2. Decisions

| # | Decision | Rejected alternatives |
|---|---|---|
| D1 | **Origin holds VCS metadata only.** Containers mount `.jj` and the git dir, never the working tree (3.1). An allowlist: unknown files stay out by default. | Mask `.env` and `.cld/` over a read-only checkout: a denylist misses `api/.env`, `.env.local` and other gitignored secrets; each mask target must exist on the host, or docker creates it there; the store still needs write access. No origin mount (clone in, push back via the broker): loses the shared store that keeps snapshots across recreate, shows agent commits on the host live, and lets the broker resolve `<session>@`. |
| D2 | **The host default workspace's state is mounted read-only**, and every boot `jj` call in origin passes `--ignore-working-copy`. A forgotten flag, or an agent `jj st` in origin, then fails instead of snapshotting "every file deleted" into the operator's `@`. | No `.jj/working_copy`: jj has no bare mode (jj issue #10058), so boot needs a workspace to run in. The flag alone: one miss loses host work. |
| D3 | **Host-trusted VCS config is read-only:** the git dir's `config` and `hooks`, jj's `config-id`, `workspace-config-id` and `git_target`. The parent of each is its own mount root. With an id missing, cld and the broker run no jj on the repo. | Leave them writable: host code runs on the next push, and a planted jj config forges `trunk()`. One read-only bind deep in a read-write mount: Linux refuses to rename a mountpoint (EBUSY) but not its parent, so the agent renames the parent and plants a fresh copy. Create a missing id at preflight: jj then migrates a planted legacy file into host config (A10). |
| D4 | **Runner and server containers mount the same set at `/repo`.** | Keep the checkout: agent code in the runner writes `.cld/config.toml` and plants symlinks. |
| D5 | **Secrets resolver:** open `.env` once with no symlink anywhere below the repo, require a regular file, copy it into a broker-owned 0600 file, mount only the copy (6). | `realpath` check, then mount: docker resolves the path again at start (race; same class as snug #284, inspect_ai #563). `docker create` + `docker cp -` + `start`: no copy on disk, but three docker calls and a rewrite of the exec path test-jobs replaces; kept as a fallback. `--env-file`: the CLI follows symlinks, parses unlike `source`, shows values in `docker inspect`. `/proc/self/fd/N` source: the daemon resolves it in its own context. |
| D6 | **`ignore_gitignore` stays, as copies.** Not built (13). Listed files are checked, bound read-only into `/tmp`, copied into the workspace at boot. `.env` and `.env.*` are refused. | Drop it: it has non-secret uses. Keep symlinks: they need the checkout. Host staging copies: more host state, no gain once only the operator writes the checkout. |
| D7 | **Marketplace mounts need a marker** and must not overlap a repo or `$HOME`. | Leave it: a `~/.claude` edit mounts any directory. An operator allowlist: new config for every user. |
| D8 | **Old containers must be recreated.** Warm start with the old mount is refused with a `cld restart` hint. | Silent recreate: drops container-layer state without consent. Wait: one old container per repo voids D3 for all. |

Precedent (ticket `research.md`): Codex cloud (setup-phase secrets), Devin (build-only secrets)
and Claude Code cloud (proxy-injected secrets) keep secrets out of the agent's sandbox. So here.

## 3. Mechanism: the store mount

### 3.1 Mount set

`H` is the host repo path. `R` is `/workspace/origin/<name>` (ticket), `/workspace/origin` (task
agent, `cld run`) or `/repo` (runner, server). `G` is the git dir: `H/.git` for colocated jj and
git-only repos, `H/.jj/repo/store/git` for jj with an internal git store.

| # | Source | Target | Mode | For |
|---|---|---|---|---|
| 1 | `H/.jj` | `R/.jj` | rw | jj only; mount root for rows 2 and 10 |
| 2 | `H/.jj/working_copy` | `R/.jj/working_copy` | ro | D2 |
| 3 | `H/.jj/repo` | `R/.jj/repo` | rw | mount root for row 4 |
| 4 | `H/.jj/repo/config-id` | same under `R` | ro | blocks a planted legacy repo config (7.2) |
| 5 | `H/.jj/repo/store` | same under `R` | rw | mount root for row 6 |
| 6 | `H/.jj/repo/store/git_target` | same under `R` | ro | git-backed jj; blocks a redirect to an agent-made git dir |
| 7 | `G` | `R/.git` or `R/.jj/repo/store/git` | rw | objects, refs, worktrees |
| 8 | `G/config` | same under `R` | ro | `core.sshCommand`, `core.fsmonitor`, credential helpers |
| 9 | `G/hooks` | same under `R` | ro | `pre-push`, `post-checkout` |
| 10 | `H/.jj/workspace-config-id` | same under `R` | ro | blocks a planted legacy workspace config (7.2) |
| 11 | `G/commondir` | same under `R` | ro | placeholder holding `.`, created by the preflight; git and jj read config and hooks from the folder it names (13) |

Every row is `--mount type=bind,src=…,dst=…[,readonly]`, so a missing source fails instead of
being created on the host, as `-v` would do. Docker mounts parents first (moby `SortMounts`). cld
containers run with `--cap-drop=ALL`, so the agent cannot remount. Inside the container each
protected path and its parent is a mountpoint, so neither can be renamed or unlinked. Every
container that mounts a store uses this set (3.4 and section 5).

### 3.2 Host preflight

`prepare_store_mounts(host_repo) -> StoreLayout` in `cld/docker.py`, run by the launcher per repo:

1. `.jj` dir: jj. Else `.git` dir: git. A `.git` file (git worktree) or a `.jj/repo` file
   (secondary jj workspace): refuse. Both are broken in containers today too.
2. jj: host `jj --version` must be 0.38+ (repo and workspace config moved out of the repo,
   "secure config"). Then per id, `.jj/repo/config-id` and `.jj/workspace-config-id`: present,
   keep it. Missing next to its legacy file (`.jj/repo/config.toml`, `.jj/workspace-config.toml`):
   refuse `unmigrated jj config <file>: check it, then run jj once on the host`, and run no jj, as
   any jj call would migrate a planted alias (A10). Missing alone: create it with `jj -R H
   --ignore-working-copy config path --repo` (or `--workspace`, A2); refuse if still missing.
3. `git_target`, resolved against `H/.jj/repo/store`, must be `H/.git` or `H/.jj/repo/store/git`.
4. `G/hooks` missing: create it empty (0755).
5. `lstat` every source. Refuse a symlink, or a dir where a file is expected, or the reverse.
6. Store config and `config.worktree`. With `extensions.worktreeConfig` on, host git (and host jj,
   through its git layer) also reads `G/config.worktree` and `G/worktrees/*/config.worktree`, which no
   row protects: a container could set `core.hooksPath` (a planted `reference-transaction` hook ran on
   host `update-ref`, `branch -D`), `core.fsmonitor` (fires on read commands such as `git status`) or
   `core.sshCommand` (runs on host `jj git fetch`) and have the host act on it. The same holds for a
   config that includes a file inside `G` (`include.path`, `includeIf.*.path`, relative paths resolve
   against `G`). The checks read the files, with no git or jj run (`cld/vcs/git.py:check_worktree_config`;
   the key is found in any letter case and with `\r` as whitespace, as git reads it):
   - git-only store: refuse when `.git/config` sets `extensions.worktreeConfig` (any value), includes a
     file inside `.git`, or when `.git/config.worktree` or any `.git/worktrees/*/config.worktree`
     exists (also with the extension off: it becomes active once the host turns the extension on), or
     when `.git/worktrees` is not a directory or cannot be read (`.git/worktrees (cannot be read: ...)`
     in the reason), or when `.git/shallow` exists (see step 7).
   - jj store: **warn only** (user decision), in the launcher preflight and as a `[cld-broker]` note
     on stderr in the broker; this includes an unreadable `worktrees` folder and a `shallow` file, which
     never crash a jj launch or make the broker refuse the store. jj has priority and such stores exist for sparse-checkout. The warning
     names the risk and the fix: `git config --unset extensions.worktreeConfig` once sparse-checkout is
     not needed. "No host git hook runs there" does not hold: host `git status` and `jj git fetch`
     act on `core.fsmonitor` and `core.sshCommand` there. The residual risk stays with the user.

   Host git also runs no hook on the ref changes of shutdown and `--reanchor`
   (`-c core.hooksPath=/dev/null`, defense in depth), and none of the host git calls of a git ticket
   (shutdown, the branch lookup) run on a store that fails this check.
7. Replace refs, grafts, commit-graph and shallow file. A container can write `refs/replace/*` and `.git/info/grafts`, which show
   host `commit-tree <target>^{tree}` (`--reanchor`) and `merge-base --is-ancestor` (the target
   refusal, the shutdown "already held" check) fake objects and parents. Every host git command cld
   issues on a git store runs with `GIT_NO_REPLACE_OBJECTS=1` and `GIT_GRAFT_FILE=/dev/null`
   (`GitBackend.run`, `STORE_ENV`; the broker's `host_git`). Both are needed: the first does not
   switch off the graft file. The same calls run with `-c core.commitGraph=false` (`STORE_GIT_FLAGS`):
   the container can also write `objects/info/commit-graph`, which git trusts for parents and root trees
   without reading the commits, so a forged file would answer `merge-base --is-ancestor` and
   `for-each-ref --contains` wrongly (shutdown could skip the detached archive, the `--reanchor` target
   refusal and the rebase hints could be fooled). A `<git dir>/shallow` file cuts history the same way,
   and `GIT_GRAFT_FILE` does not switch it off. There is no documented switch, so a git-only store with
   a `shallow` file is refused (preflight and the shutdown store check, like `config.worktree`) and a jj
   store gets a warning. A truly shallow host clone must be unshallowed (`git fetch --unshallow`) to be
   used with ticket containers. The include parser behind the step 6 check skips a leading byte-order
   mark and joins a backslash line continuation (also before a CRLF), as git does. Residual risk: the host user's own git commands in their checkout still
   honour a replace ref or graft a container wrote, as the store is shared. cld does not fix that.

Refusal: `cld: repo '<name>': <reason> -- cannot mount its store safely`, exit 1, no container.

### 3.3 Boot changes (devcontainer and `cld run` images)

- `vcs-lib.sh` gains `cld_store_jj <origin> <args>`: `jj --ignore-working-copy <args>` in origin.
  Every origin call uses it: workspace forget, bookmark list, base resolution, `cld_recover_anchor`.
- `cld_add_workspace <origin> <name> <rev> <ws>`: `workspace add` through `cld_store_jj`,
  tolerating only "must be able to update the working copy", then `(cd <ws> && jj new <rev>)`.
  This is the runner pattern, in production on jj 0.45.1 (`runtests/entrypoint.sh:68-76`). First
  launch and reattach both use it.
- `cld_boot_worktree` passes `--no-track` to `git worktree add -b`; tracking writes `G/config`.
- `entrypoint-claude-run.sh:39-46` uses both helpers. `_forget_session_bookmark` adds the flag.
- `link_workspace_files` copies from `/tmp/cld-repo-files/<name>/<file>` (4.1). Not built (13).
- The ticket CLAUDE.md gains: "`/workspace/origin/<repo>` holds only the shared jj/git store.
  Run jj and edit files in your workspace."
- Both images get `LABEL org.cld.boot-api=2`.

### 3.4 Launch wiring

- `build_ticket_container_args` and `build_container_args` replace the checkout mount with the
  3.1 set and add the label `org.cld.origin-mount=store-v1`. `CLD_PATH_MAP` is unchanged.
- The launcher refuses an image without `org.cld.boot-api=2` ("rebuild the cld images"): an old
  entrypoint runs plain jj in origin and fails on row 2.
- `_warm_start` refuses a container without the label: "this container mounts the full host
  checkout; run `cld restart <ticket>`". `start_ticket` on a running one warns.
- At launch, another container mounting the repo without the label is refused (13): it can swap sources.

## 4. Mechanism: other paths from the host into the agent container

### 4.1 `ignore_gitignore` (not built, 13)

`ticket_repo_files` and the v1 `WORKSPACE_FILES` path check each name host-side: relative, no
`..`, no empty part; basename not `.env` and not starting with `.env.`; opened part by part with
no symlink (the 6.2 walk); a regular file. Accepted: a read-only bind of `H/<file>` at
`/tmp/cld-repo-files/<name>/<file>`, and the name stays in `CLD_REPO_FILES`. Refused: a warning
`ignore_gitignore: '<file>' skipped: <reason>` on every launch. Docker resolves the source again at
each `docker start`; after this design only the operator writes the checkout, so that is accepted.

### 4.2 Plugin marketplaces and the rest

`stage_plugin_marketplaces` skips, with a warning, a path that is `$HOME` or its ancestor;
overlaps any registry or manifest repo path; or lacks a marker. The marker is a regular
`.claude-plugin/marketplace.json` for a `directory` source; a `file` source must be a regular
file named `marketplace.json`. The agent cannot create a marker where it cannot write, and what it
can write is mounted anyway. Remaining mounts: `home_mounts_always`, SSL certs, broker key, otel
(read-only, no repo files); mailbox (messages only); `~/.claude` read-write (open, 7.3).

## 5. Mechanism: runner and server mounts

`run_tests` (`broker/cld-broker.py:457-464`) and `do_graphql_start` (`:995-1011`) replace
`-v <repo>:/repo` with the 3.1 set at `R=/repo`, built by the broker's own
`store_mounts(repo, "/repo")` (it cannot import cld). It runs preflight steps 1, 3, 5 and 6, and only
the check in step 2: it creates nothing and never runs `jj config`, keeping its store-reading
posture. A missing id or `hooks` gives exit 3: `denied: <repo> store is not prepared -- start a
ticket on it once`. Every host `jj` call the broker makes on a repo (`:235`, `:344`, `:382`, and
the profile's reviewed-revision read) first checks with `lstat` that both ids are regular files,
and runs no jj without them (A10). Entrypoints and runner images are unchanged: they touch
`/repo` only with `--ignore-working-copy` (`runtests/entrypoint.sh:53-76`, `graphqlserver/entrypoint.sh:56-76`).

## 6. Mechanism: the secrets resolver

### 6.1 Interface (`broker/cld-broker.py`)

- `resolve_secrets(repo, env_file, *, operator_path=False) -> Secrets`. `env_file` is a host path;
  the profile's `TestPlan.env_file` and `graphql_settings(...).env_file` go here.
- `Secrets(source, project_subdir, staged)`. `staged` is the copy's path, or `None` when absent.
  `mount_args()` gives `["--mount", "type=bind,src=<staged>,dst=/secrets/.env,readonly"]` or `[]`.
  `values()` parses the bytes read at resolve time with `parse_env_file` rules.
- `resolve_secrets_env_file(repo)` stays for today's callers: `pyproject_dir`, then `resolve_secrets`.
- `read_repo_file(repo, rel, max_bytes) -> bytes | None`: the same walk, no copy. `cld_conf_get`
  (`:240-246`) keeps its regex but reads `.cld/config.toml` through it; profile package 2 deletes it.
- Refusals raise `SecretsRefused(reason)`; actions exit 3 with
  `denied: secrets file <source>: <reason>`. Messages name `source`, never the copy.

### 6.2 The walk

1. In-repo path: the part below `repo` must be relative, with no `..` and no empty part, so an
   absolute or `..` `pyproject_dir` is refused. Open `repo` with `O_DIRECTORY`: it comes from
   host-set labels, and symlinks above the repo are the operator's. With `operator_path=True`
   (profile layer 3 `secrets.env_file`) open `realpath(dirname)`; the walk is the basename only.
2. Each directory part: `os.open(part, O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC, dir_fd=…)`.
   `ELOOP` or `ENOTDIR`: refuse "symlink or non-directory at <part>". `ENOENT`: absent.
3. Last part: `O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC`; `O_NONBLOCK` keeps a FIFO from blocking.
   `fstat`: refuse anything but `S_ISREG`, and files over 1 MiB. Read all bytes from the fd.

### 6.3 The copy

- Stage dir: `SECRETS_STAGE_DIR` in `broker.conf`, default `~/.cld/broker-secrets`, created 0700.
  Each use checks with `lstat`: a real directory, owned by the broker user, no group or other
  bits, not inside the mailbox root, `VENV_CACHE_ROOT` or `TESTS_RUNS_ROOT`. Else exit 3.
- Name: first 16 hex of `sha256(source)` plus `.env`. Written via `mkstemp` (0600) in the stage
  dir, then `os.replace`. A running container keeps the old inode; concurrent runs are safe.
- Absent source: delete that copy, mount nothing, keep today's runner warning.
- The copy is replaced on each use, so the exec'd `run-tests` path needs no post-run cleanup.

Docker resolves only the copy's path, which no container can write. Callers that change:
`_secret_mount`, `lookup_alias`, `do_graphql_endpoints`, `resolve_target`'s message, and
`resolve_graphql_config` (through `read_repo_file`).

## 7. Security: what holds before and after

### 7.1 Contract status

| Contract point | Today | After this design |
|---|---|---|
| Agent container cannot read the host repo `.env` | False: origin, `ignore_gitignore`, marketplaces | Holds for every mount and file path into the container. Not against host code planted through `~/.claude` (7.3). |
| Agent container cannot write the host `<repo>/.cld/config.toml` | False: origin and runner mounts | Holds for direct writes from agent, runner and server containers. An agent commit that tracks the file lands on the host when the operator checks it out. Then the broker and cld's launcher both read it (7.3). |
| Broker never follows a symlinked secrets file | False | Holds for every read and mount of `.env`, and for `.cld/config.toml`. |

### 7.2 Closed

- Reads of any working-tree file of a mounted repo, every `.env` and untracked file included.
- Writes to the host working tree from agent, runner and server containers.
- Snapshots of the operator's working copy from containers.
- The `.env` symlink escape and the absolute `pyproject_dir` path.
- Host code via git `config` and hooks, or a redirected `git_target`.
- A planted `.jj/repo/config.toml` or `.jj/workspace-config.toml`. jj 0.38+ ignores each while
  its id exists (A1); rows 4 and 10 keep the ids there; no host jj runs without them (5).
- The `ignore_gitignore` and marketplace routes for `.env`.

### 7.3 Stays open

- **`~/.claude` is read-write.** The agent can add hooks and MCP servers to the host Claude Code
  settings; they run as the operator in the next host session and can read every `.env`. This is
  the largest remaining escape. It is the next containment ticket (open question 1).
- **Code under test sees `.env` by design** in the runner and server, and can print it. Masking
  (identities section 5) is best effort.
- **The operator runs agent code** when checking out and running agent commits on the host
  (tests, committed hook dirs, build scripts). That checkout also lands a tracked
  `.cld/config.toml`, read by the broker (profile layer 2) and by cld's launcher
  (`cld/config.py`): `ssl_certs_path` swaps its CA bundle, the image keys pick its image.
  The home mounts are user-config only (13). Open question 4.
- **Store integrity.** The agent can move bookmarks, rewrite the op log, move `main@origin`
  through colocated refs and leave the host workspace stale. This can still forge `trunk()` for
  the profile's reviewed revision (profile 5.2). jj config is out of reach (rows 4 and 10; user
  config is never mounted), except an alias planted earlier by a full-checkout container and
  already migrated into host config; the check in section 9 finds it.
- Submodules are refused (13). `.git/info` and `objects/info/alternates` stay writable.
  `config.worktree` stays writable too, so a git-only store that turns on `extensions.worktreeConfig`
  (or holds a `config.worktree`, or includes a file inside the git dir) is refused, and a jj store
  with any of these is warned about (3.2 step 6). Replace refs, grafts and a commit-graph a container
  writes are switched off for cld's own host git, not for the user's commands in their checkout, and a
  `shallow` file is refused on a git-only store (3.2 step 7).
- Tracked or once-committed `.env` files are in the store (A7).

## 8. What breaks or changes for users

- `/workspace/origin/<repo>` shows only `.jj` and `.git`. Untracked host files are gone from it.
  Plain `jj` there fails with a read-only error; work in the workspace.
- **`ignore_gitignore`:** every listed file is skipped with a warning (13).
- **Broker:** a symlink at or above `.env`, `.cld` or `.cld/config.toml` (below the repo) is refused
  with exit 3 by `run-tests`, `graphql start`, `query` and `endpoints`. An absolute or `..`
  `pyproject_dir` is refused; the profile's layer 3 `secrets.env_file` replaces it.
- Host jj 0.38+ is required. The launcher may create both jj ids (via jj) and `G/hooks`, and
  refuses a repo with an id missing next to its legacy jj config file.
- Existing tickets need `cld restart <ticket>`. Container-layer state (installed packages, edits
  not yet snapshotted) is lost; committed and snapshotted work stays.
- Marketplaces that fail the 4.2 checks (as built, 13), and all repo-settings ones, are not mounted.
- Git-backed tickets no longer set upstream tracking on the session branch.
- A host `git config` change reaches a running container only after a restart.

## 9. Compatibility and migration

- **Order:** the broker first (sections 5 and 6, then restart it), then cld and the images
  together (3 and 4). An old image with a new launcher is refused (3.4). A new image with an old
  launcher still gets the full checkout and works as today.
- Runner images must be rebuilt (13). Servers started before the broker upgrade keep the old mount until
  `graphql restart`. Running task agents keep it until they end; new launches get the new mounts.
- **Operator steps:** jj 0.38+ on the host; upgrade cld and restart the broker; rebuild the cld
  images; `cld restart` each ticket; turn a symlinked `.env` into a regular file (or use profile
  layer 3 once it ships); drop `.env` from `ignore_gitignore`. Once per jj repo, after the
  restarts: `jj config list --repo` and `jj config list --workspace` show no alias you did not set.
- **Docs:** `README.md` (layout block, `ignore_gitignore` row), `CLAUDE.md`, `PRODUCT_DESIGN.md`,
  `MANUAL_TESTS.md`, `docs/design-ticket-containers.md` §4.1 and §6.2-6.6,
  `docs/design-cld-broker.md` §9, `docs/design-anchor-guard.md` §8, `broker/broker.conf.sample`.
  The three broker designs are updated by their owner.

## 10. Test plan

### 10.1 Automated, through `cld broker run-tests`

Real-jj cases use the `jj_repo` fixture and skip without jj. Read-only binds are simulated with
`chmod`, so those cases skip as root.

- `tests/test_broker_script.py`: `TestSecretsResolver` (symlink as last part and as a middle dir;
  FIFO refused without blocking; dir and 2 MiB file refused; absolute and `..` refused; absent
  gives no mount and deletes a stale copy; copy equals the bytes, is 0600, replaced atomically;
  bad stage dir refused). `TestSecretMountArgs` (`run-tests` and `graphql start` mount only the
  copy, read-only). `TestAliasNoFollow` (`query <alias>`, `endpoints`; denial names the source).
  `TestConfigNoFollow`. `TestRunnerStoreMounts` (rows 1-11 per layout, no `<repo>:/repo`, exit 3
  without either id). `TestStoreMountParity` (broker and cld builders give the same set).
  `TestHostJjNeedsIds` (fake jj: no call at all when an id is missing or a symlink).
- `tests/test_docker.py`: `TestStoreMounts` and `TestStorePreflight` (colocated, internal git,
  git-only; refusals for a `.git` file, a `.jj/repo` file, jj < 0.38 via fake jj, a foreign
  `git_target`, a symlinked source, an id missing next to its legacy file with no jj run; `hooks`
  created; both ids created with real jj). `TestJjConfigPins` (real jj: a planted legacy repo or
  workspace config leaves `trunk()` unchanged while both ids exist, and changes it once the
  workspace id is deleted; guards A1 and A10 on jj upgrades).
  `TestTicketContainerArgs`, `TestBuildContainerArgs` (no checkout mount, label, file binds).
  `TestPluginMarketplaceFilter` (`$HOME`, repo dir, dir inside a repo, no marker; a real one mounts).
- `tests/test_container_init_sh.py`: `TestBootWithReadOnlyWorkingCopy`
  (real jj; origin holds only `.jj`, `working_copy` not writable; first launch and reattach
  succeed; the op log gains no snapshot of the default workspace). `TestPlainJjInOriginFails`
  (real jj; `jj st` fails and adds no operation; A3). `TestNoPlainJjInOrigin` (static scan of
  `vcs-lib.sh` and both entrypoints). `TestTicketClaudeMdOriginLine`.
- `tests/test_ticket.py`: `TestWarmStartRefusesOldMounts`, `TestOldImageRefused`,
  `TestOldContainerSameRepoWarns`. `tests/test_agent_loop.py`: forget passes the flag.

### 10.2 Manual, on the host (WSL2, lide and diskuze)

1. Upgrade the broker, rebuild images, `cld start` a ticket on lide and diskuze.
2. In the container: `ls -A /workspace/origin/lide` shows `.jj .git`; `find / -name .env` finds
   no lide or diskuze file.
3. `cd /workspace/origin/lide && jj st` fails; host `jj op log -n 3` shows no new operation.
4. `touch .jj/repo/store/git_target`, `echo x > .jj/workspace-config-id`, `echo x > .git/hooks/pre-push`
   and `git config core.sshCommand x` fail (read-only); `mv .jj/repo /tmp/x` and
   `rm .jj/workspace-config-id` fail (busy). This confirms nested read-only binds on this engine;
   record `docker version`.
5. In the container, write `.jj/workspace-config.toml` with `[revset-aliases]` `"trunk()" = "@"`.
   On the host, `jj -R <lide> --ignore-working-copy log -r 'trunk()'` is unchanged. Delete the file.
6. `cld broker run-tests --repo lide <one test>` passes and sees a `.env` value; `graphql start`,
   `query` and `endpoints` work.
7. On the host, make `api/.env` a symlink: `run-tests` exits 3 naming it. Restore it.
8. `ignore_gitignore = [".env", ".envrc"]`: both skipped with a warning (13).
9. A ticket created by the old cld: warm start is refused; `cld restart` recreates it; the work at
   its bookmark is intact.
10. A task agent starts and pushes its deliverable branch. `cld run` completes.
11. On the host, `jj git push --dry-run` and `git status` in lide behave as before.

## 11. Implementation packages, in dependency order

1. **Broker secrets resolver** (6). `broker/cld-broker.py`, `broker.conf.sample`. `cld_conf_get`
   stays, reading through `read_repo_file`; profile package 2 removes it. Exit:
   `TestSecretsResolver`, `TestSecretMountArgs`, `TestAliasNoFollow`, `TestConfigNoFollow` pass.
2. **Store mount builder and preflight** (3.1, 3.2). `cld/docker.py`. Exit: `TestStoreMounts`,
   `TestStorePreflight` and `TestJjConfigPins` pass for all three layouts and every refusal.
3. **Boot scripts and images** (3.3). `vcs-lib.sh`, `container-init.sh`, both entrypoints,
   `agent_loop.py`, Dockerfile labels. Exit: `TestBootWithReadOnlyWorkingCopy` passes.
4. **Launch wiring and `ignore_gitignore`** (3.4, 4.1). `cld/docker.py`, `cld/ticket.py`,
   `tests/conftest.py`; same release as package 3. Exit: no builder emits a checkout mount, and
   `TestWarmStartRefusesOldMounts` passes.
5. **Runner and server mounts, host jj id check** (5). `broker/cld-broker.py`; needs package 2
   for parity. Exit: `TestStoreMountParity`, `TestRunnerStoreMounts`, `TestHostJjNeedsIds` pass.
6. **Marketplace filter** (4.2). `cld/docker.py`. Exit: `TestPluginMarketplaceFilter` passes.
7. **Docs and host verification** (9, 10.2). Exit: every manual check passes on the host.

## 12. Assumptions and open questions

Assumptions (each has a test or manual check above):

- A1. jj 0.38+ reads neither `.jj/repo/config.toml` nor `.jj/workspace-config.toml` while the
  matching id exists. Reproduced on jj 0.45.1 for both; the secure-config design doc implies it.
- A2. `jj config path --repo` and `--workspace` create the missing id (reproduced on 0.45.1).
- A3. jj takes the working-copy lock (`.jj/working_copy/working_copy.lock`) before any snapshot
  write, so a read-only `working_copy` makes a plain command fail with no operation written.
- A4. Linux refuses rename or unlink of a mountpoint in the caller's namespace (EBUSY) and allows
  it for its parent (rename(2); LWN 570338). Hence the extra mount roots.
- A5. Nested binds work on the host engine. Docker Desktop 4.67 on macOS broke them
  (docker/desktop-feedback #279); 10.2 checks the WSL2 host.
- A6. No in-container code needs host working-tree files. Only the init script, the entrypoints,
  `agent_loop.py` and `cld/vcs/detect.py` touch origin; `detect.py` reads only the store.
- A7. `.env` files are gitignored and were never committed.
- A8. Container jj stays 0.45.x. jj 0.46 makes `workspace add` run `git worktree add --orphan` in
  colocated repos; re-check rows 7-9 before upgrading.
- A9. Real `file` marketplace sources are named `marketplace.json`.
- A10. With an id missing, any jj command migrates the legacy file into the user's jj config dir
  and writes a new id, so a planted alias becomes host config (reproduced on 0.45.1). jj plans to
  stop migrating about 12 releases after 0.38; re-check 3.2 step 2 then.

Open questions:

1. Not in this design: `~/.claude` read-write (7.3) defeats the first contract point through
   host code. It is the next containment ticket, ahead of the identities work.
2. Refuse host jj older than 0.38 (this design), or only warn?
3. `ignore_gitignore` is off (13). Bring back the copy mechanism of 4.1, or drop the key?
4. Should the broker and cld's launcher both ignore a `.cld/config.toml` that came from a commit,
   closing the checkout path for the second contract point? Owner: this design, as the launcher
   is in scope. The profile design adopts the answer for its layer 2 (profile 5.2).
5. Submodules are refused (13). Add read-only rows for each `modules/*` config and hooks dir?

## 13. As built

Deviations from the sections above. Host checks: `MANUAL_TESTS.md` section 10.

- **Row 11** (`G/commondir`, read-only, holds `.`) is new; the preflight creates it.
- **More refusals, all before any host jj:** a foreign `commondir`; a `G/modules` dir (submodules
  are not supported yet); on a git-only store, `extensions.worktreeConfig` in `.git/config`, a config include inside `.git`,
  a `config.worktree` file, an unreadable `worktrees` folder, or a `shallow` file (3.2 steps 6 and 7, also in the broker's `store_layout`, so `host_git` and the
  runner mounts refuse it; a jj store gets a warning instead); any container that mounts the repo without the `store-v1` label (a
  refusal, not the warning of 3.4). `claude-base` also carries `org.cld.boot-api=2`.
- **`ignore_gitignore` is off.** D6 and 4.1 were not built. Listed files are skipped with a
  warning, and `link_workspace_files` is removed.
- **Marketplaces (4.2):** the marker, outside `~/.claude`, the mailbox root, every repo and the
  parents of `$HOME`, and literal path equal to real path. Mounted read-only with `--mount`.
  Entries in a repo's `.claude/settings.json` are never mounted.
- **Home mounts:** user config only. Relative, no `..`, no symlink, no repo overlap.
- **Broker secrets (6.3):** copies older than 24 h are swept. The stage dir must also lie
  outside `~/.claude` and the registry repos.
- **Runner and server (5):** `--cap-drop=ALL` and `no-new-privileges`. Images without an
  `org.cld.jj-version` label of 0.38+ are refused. `host_jj` checks the ids, `commondir`,
  `modules/`, `git_target` and the jj version. Venv cache per repo, at
  `<VENV_CACHE_ROOT>/repos/<name>-<hash>/`. `graphql_health_path` and `graphql_port` are
  validated, failed requests print no URL, `task-agent` refuses `--`, and `cld-brokerctl.sh
  graphql-sweep` uses the broker's own sweep.
- **Runner and server jj config:** both entrypoints run `scripts/seed-secure-config.sh` before
  any jj call. With an empty `$HOME`, jj otherwise rewrites both ids by rename, which the
  read-only binds refuse with EBUSY.
- **Tests (10.1):** the 4.1 classes do not exist. The old-image and old-container cases are unit
  tests in `TestLaunchWiring` (`tests/test_docker.py`).
