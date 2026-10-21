# Repo-owned broker profile

**Status:** design, not implemented. **Date:** 2026-10-21. **Ticket:** CLD-001 (gap 3).
**Prerequisite:** `docs/design-secrets-containment.md` (build step 1); section 5 depends on it.
**Related:** `docs/design-cld-broker.md`; siblings `docs/design-broker-test-jobs.md` and
`docs/design-broker-identities.md`. This doc owns the schema, the shared accessors, the client
changes and the build order of all three (section 10). Code cites: working copy of 2026-10-21.

## 1. Problem

How a repo runs its tests and its server lives on the host only, outside the repo and the
tool. Evidence: ticket notes `~/.claude/ego/tickets/CLD-001/gaps.md` (gap 3), `live-tests.md`.

- diskuze has no `graphql_command`; nobody can tell if that is on purpose.
- pytest runs from `pyproject_dir`, so repo-relative paths fail with exit 4. Agents also
  pass container paths such as `/workspace/<ticket>/lide/api/tests`.
- Both AGENTS.md files forbid `integration` and `json` in one run; the skill's "whole
  suite" example does exactly that (106 min). lide wants `-n 5 --dist=loadscope`; the
  skill says `-n logical`. The runner is UTC; 4 diskuze unit tests assume local time.
- A quoted-value regex reads the config (`broker/cld-broker.py:240-246`), so
  `graphql_port = 8000` is silently ignored. The agent cannot see the effective config.

Chosen direction: a versioned profile in each project repo, read only from a reviewed
revision, which host config may override, shown by a read-only `cld broker config`.
**Out of scope:** runner dependency parity with the host (`live-tests.md:33-36`: diskuze's
`--workers` and pytest-freezegun are not in `poetry.lock`; host Python 3.13.16, runner 3.13.5).

## 2. Decisions

| # | Decision | Rejected alternatives |
|---|---|---|
| D1 | **File `.cld-broker.toml` at the repo root.** | **`.cld/broker.toml`:** `.cld/` is the gitignored host-local dir (`cld/config.py:129-135`). A versioned file there needs `!` rules and mixes reviewed and unreviewed files. **`[tool.cld]` in `pyproject.toml`:** that file sits in the project dir the profile itself names. **`cld.toml`:** reads like cld's own app config. |
| D2 | **TOML via `tomllib`.** It also replaces `cld_conf_get` for the host file. | **YAML:** needs PyYAML; the broker is stdlib only. **JSON:** no comments. Host Python is 3.13.16; `tomllib` exists since 3.11, and the broker already imports it (`:55`). |
| D3 | **The reviewed revision is the fork point of the caller's revision and `trunk()`.** The broker reads the file from the jj store (4.2). | **Caller's revision:** no review; an agent's half-done edit changes its own rules. **Latest `trunk()`:** an older branch gets suites that do not match its code. **Ticket `anchor_base`:** ticket kind only, and task-agent anchors may be agent-chosen (`-r` passes through, `:606-607`). **Fetch the remote into a broker clone:** network and credentials on every call. **Content-hash approval (direnv style):** an extra operator step that the MR review already covers. |
| D4 | **Four layers:** defaults < repo profile < host checkout `.cld/config.toml` < operator entry in `~/.config/cld/config.toml`. `broker.conf` stays broker-wide; `.env` holds only secrets. | **Host checkout file only:** unversioned, and agent-writable until containment ships. **Env-var overrides:** sshd builds a fresh environment, and operators already use `broker.conf`. |
| D5 | **Trust by boundary.** Profile keys act only inside a runner container. Command keys come from the reviewed revision or from operator layers; the operator can turn repo commands off (5). | **Commands host-only:** safe, but setup stays per machine. Kept as a one-switch fallback (5.3). |
| D6 | **The broker enforces "one suite per run" when the profile asks** (2.1, 6.2). | **Skill text only:** see 2.1. |
| D7 | **The broker always passes `PYTEST_ADDOPTS`, `POETRY_INSTALL_ARGS` and `TZ`.** Defaults equal today's image values. Both runner images gain `tzdata`. | **Implicit image defaults:** `config` could not show the real value. **Mount host zoneinfo:** couples the standalone images to the host (design-cld-broker.md, decision 6). |
| D8 | **`[server] enabled = false` plus a `note` declares "no server".** The port is a TOML integer. | **A missing table means none:** cannot tell "not set up" from "never". |
| D9 | **`cld broker config` is a new read-only broker action** that prints every key with its source. | **Read it in the client:** the container cannot see host layers or resolve the reviewed revision. |
| D10 | **Bad input falls back to lower layers, with warnings.** A bad file is dropped whole; a bad key is dropped alone. | **Refuse runs:** one typo in a reviewed file would block every agent until a new merge. |
| D11 | **The code goes in a new section of `broker/cld-broker.py`.** | **Sibling module:** the script runs directly and tests load it by path; both would need `sys.path` care. |
| D12 | **One server per repo in version 1.** No `[server.variants]`. | **Variants now (lide internal API):** a second command, port, label set and hook table, plus a `--variant` on every graphql op. It does not close the Gap 2 evidence (lide public `me.user`); diskuze can, with no schema change (6.3). |

### 2.1 Rule enforcement: through suites, not only skill text

The broker enforces the structural part of a repo's test rules through suites; AGENTS.md and
the skill keep the semantic part, such as marker combinations. With `tests.one_suite_per_run
= true`, a run touches at most one suite; a run with no selection touches all, so it is
refused unless `default_suite` names one. Why: the skill's own example broke the rule, and
agents copy examples. Escape: the operator sets `one_suite_per_run = false` in layer 3.

## 3. Schema

The file is `.cld-broker.toml` at the repo root, with `version = 1`. Host layers use the
same keys under a `[broker]` table (4.1). Unknown keys get a warning and are ignored.
`list` means a list of strings; `map` a string-to-string table; "rel path" relative and
normalised, with no `..` and no leading `/`. Every key is final for version 1.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `version` | int | required | Only `1` is accepted. |
| `project.dir` | rel path | `"."` | Dir with `pyproject.toml` and `.env`; pytest's cwd. Relative in every layer. Replaces `pyproject_dir`. |
| `runner.timezone` | str | `""` (UTC) | IANA zone, passed as `TZ` to both runners. |
| `runner.env` | map | `{}` | Plain non-secret variables for both runners. Reserved names refused (5.1). |
| `runner.install_args` | list | `["--all-extras", "--all-groups"]` | Becomes `POETRY_INSTALL_ARGS`. |
| `tests.addopts` | list | `["--tb=short", "--disable-warnings", "-q", "--maxfail=30"]` | Becomes `PYTEST_ADDOPTS`. |
| `tests.one_suite_per_run` | bool | `false` | **Rule key**, layers 1 and 3 only (5.1). Refuses runs that touch more than one suite (6.2). |
| `tests.default_suite` | str | `""` | **Rule key.** Suite for a run with no `--suite` and no selection. |
| `tests.max_runtime` | int | unset | Seconds; capped by `TESTS_MAX_RUNTIME` in `broker.conf`. |
| `tests.suites.<name>.paths` | list of rel paths | `[]` | Selection, relative to the suite's dir. Empty: a suite selected by its `args` only. Every `tests.suites` key is a **rule key**. |
| `tests.suites.<name>.args` | list | `[]` | pytest args placed before the caller's. |
| `tests.suites.<name>.dir` | rel path | `project.dir` | pytest cwd and `.env` location for this suite. |
| `tests.suites.<name>.description` | str | `""` | Shown by `config` and in refusals. |
| `tests.suites.<name>.max_runtime` | int | `tests.max_runtime` | Seconds, same cap. |
| `server.enabled` | bool | `true` | `false`: the repo declares no live server. |
| `server.note` | str | `""` | Shown when the server is disabled or not configured. |
| `server.command` | str | `""` | **Command key.** Shell string run by `bash -c` in the server container. |
| `server.port` | int | `8000` | Port inside the container, 1-65535. |
| `server.health_path` | str | `"/graphql"` | Must start with `/`. |
| `identities.hook` | str | `""` | **Command key.** Shell string run in the server container. Empty: identities off. |
| `identities.hook_timeout` | int | `20` | Seconds; the broker caps it at 60. |
| `identities.roles` | map | `{}` | Role name to user string. |
| `secrets.env_file` | abs path | unset | Layer 3 only. The `.env` to use instead of `<project dir>/.env`, for one outside the repo. |
| `trust.repo_profile` | bool | `true` | Layer 3 only. `false` ignores `.cld-broker.toml`. |
| `trust.repo_commands` | bool | `true` | Layer 3 only. `false`: command keys only from layer 3. |
| `trust.reviewed` | str | `"trunk()"` | Layer 3 only. Revset of the reviewed line. |

Suite and role names match `[a-z0-9][a-z0-9_-]*`; `runner.env` names match `[A-Z_][A-Z0-9_]*`.
Log retention and run caps stay in `broker.conf` (test-jobs 5.1): host disk is the
operator's. A `[server.variants]` table gets the warning `not supported in version 1` (D12).

## 4. Loading and precedence

### 4.1 Layers, lowest first

| # | Layer | Where | Who can write it | Keys |
|---|---|---|---|---|
| 0 | Defaults | broker code | a cld release | all |
| 1 | Repo profile | `.cld-broker.toml` at the reviewed revision | merged MRs | all but `trust.*`, `secrets.*` |
| 2 | Host checkout | `<repo>/.cld/config.toml`: `[broker]`, with legacy flat keys below it | operator; also the agent before containment, and a committed copy after it (5.2) | all but `trust.*`, `secrets.*` and rule keys (5.1) |
| 3 | Operator | `[repos.<name>.broker]` in `~/.config/cld/config.toml` | operator only; not mounted | all |

- **Merging:** for scalars and lists the higher layer wins. Maps (`runner.env`,
  `identities.roles`) merge per entry. A higher layer replaces a suite whole.
- **Legacy keys** in layer 2: `pyproject_dir` maps to `project.dir`, `graphql_command` to
  `server.command`, `graphql_port` (string or integer) to `server.port`, and
  `graphql_health_path` to `server.health_path`. In the same file, `[broker]` wins. An
  absolute `pyproject_dir` is dropped with the hint `use secrets.env_file in the operator
  config`: the runners `cd` into a relative dir only (`runtests/entrypoint.sh:77`).
- **Layer 3** is the `[repos.<name>]` entry whose `path` has the repo's realpath. The
  broker already reads this file (`host_registry`, `:253-262`). Unregistered repos have none.
- **Not layers:** `broker.conf` keeps images, timeouts, output caps, the URL allowlist and
  the test-jobs caps; a profile value with a cap there is clamped to it. `.env` holds
  secrets and the `CLD_GRAPHQL_*` aliases only.

### 4.2 Reading the repo profile

For each request that needs the profile, on the host:

1. Store check (containment 5 and A10): `.jj/repo/config-id` and `.jj/workspace-config-id` are
   regular files, else layer 1 is dropped (`store not prepared`) and no jj runs. `C` is the
   caller's revision (`Caller.revision`); it may be unknown.
2. `T` is `trust.reviewed`, by default `trunk()`. jj resolves `trunk()` from repo config
   (jj writes it there on clone) or to the default remote's main bookmark, else `root()`.
3. `R` = `jj -R <repo> --ignore-working-copy log --no-graph -n1 -T commit_id -r X`, where
   `X` is `latest(heads(::(T) & ::C))`, or `latest(T)` when `C` is unknown. This is
   `fork_point(T | C)` written out, so it works on any jj version.
4. If `R` is the root commit, the status is `no reviewed line` and layer 1 is empty.
5. Probe: `jj ... file list -r R 'root-file:".cld-broker.toml"'`. Empty output: `absent`.
6. Read: `jj ... file show -r R 'root-file:".cld-broker.toml"'`, then `tomllib.loads`.

On the host route, the new `cld-brokerctl run -P <revset>` pins `R` (`CliCaller` only),
so the operator can try an unmerged profile. Git-backed repos skip layer 1. Cost: three
jj calls of tens of milliseconds, against about 1.5 s per broker call. No cache.

### 4.3 Errors

| Problem | Effect |
|---|---|
| Layer unreadable, TOML error, `version` missing or not `1` | The whole layer is dropped. |
| Bad type or value, or a broken path rule | That key is dropped. |
| Unknown key; `trust.*` / `secrets.*` outside layer 3; a rule key in layer 2 | Ignored. |
| jj fails for a reason other than a missing file | Layer 1 dropped; status `error: <first stderr line>`. |

Each case gives a warning. `run-tests`, `tests start` and `graphql start` print up to five
to stderr as `[cld-broker] profile: ...`, then `see: cld broker config`.

## 5. Trust

### 5.1 The rule

Code under test already runs in the runner with the repo `.env`, from the agent's own
revision, and a test can print its environment. A profile command in the same container,
with the same mounts, adds no reach. Danger starts only where a key could change that
boundary, or run something on the host outside docker. Hence:

1. **Runner boundary.** Every key acts only inside `runtests` or `graphqlserver`; the
   identity hook runs there too (identities D1). No key sets the image, docker flags,
   mounts, network, host env pass-through, output caps or the URL allowlist.
   - **1a. Output channel.** A command's output reaches the broker only through a reply its
     design validates and size-caps (identities 4.1). A future key that feeds the host
     must define the same before it ships.
2. **Paths stay in the repo.** Rel paths are checked at parse time, in every layer. The
   broker reads and mounts a `.env` only through the containment design's resolver. The
   only absolute path is `secrets.env_file`, from layer 3, through the same resolver.
3. **Command keys** (`server.command`, `identities.hook`) come from layer 1 only at the
   reviewed revision, never from the caller's revision, or from layers 2 and 3. **Rule keys**
   (`tests.one_suite_per_run`, `tests.default_suite`, all of `tests.suites`) come from layers
   1 and 3 only. `trust.repo_commands = false` in layer 3 drops command keys from layers 1 and 2.
4. **Nothing from the profile runs as a plain host process.** A future host-process hook
   (identities D4, deferred) may come from layer 3 only.
5. **Trust and secrets keys live only in layer 3**, which no container can write.
6. **No secrets in the profile.** `runner.env` values show in `docker inspect`. `config`
   output goes through the shared masker (identities 5).

Reserved `runner.env` names: `TZ`, `HOME`, `USER`, `LOGNAME`, `PORT`, `REVISION`,
`PROJECT_SUBDIR`, `SECRETS_FILE`, `JJ_CONFIG`, `SSH_AUTH_SOCK`, `VIRTUAL_ENV`,
`POETRY_CACHE_DIR`, `POETRY_INSTALL_ARGS`, `PYTEST_ADDOPTS`, `OUTPUT_MAX_BYTES`, and names
starting with `GQL_`, `VENV_CACHE` or `CLD_`. Runner control variables use the `CLD_` prefix
(`CLD_RT_*`, `CLD_HOOK_*`); the entrypoints save them before they source `.env` and restore
them after, so `.env` cannot change them. A `.env` may still override other names, as today.

### 5.2 Guarantees before and after containment

The containment contract: the agent container can neither read the host repo `.env` nor
write the host `<repo>/.cld/config.toml`, and the broker never follows a symlinked secrets
file. The build order (section 10) ships containment first. In both phases rule 1 caps the
harm, and rule keys are reviewed or the operator's: layer 2 cannot set them (rule 3).

**Before containment ships** (today's mounts, `cld/docker.py:605-608`): layer 2 is
agent-writable, so command keys set there are not reviewed and outrank layer 1. The agent can
read the host `.env`, so masking `config` hides nothing from it. The `.env` symlink escape and
the jj config forgery of `trunk()` (containment section 1) stay open.

**After containment ships:** no container writes layer 2, but a committed `.cld/config.toml`
lands in the host checkout when the operator checks out an agent commit, and can set command
keys; containment open question 4 owns that path. jj config is pinned (4.2 step 1). Every
`.env` read goes through the containment resolver.

**Remaining limit, both phases:** the store's bookmarks stay writable, so an agent with `git`
could move `main@origin` and with it `trunk()`. The broker does not pin `trunk()` with
`--config`: jj writes that alias into repo config on clone, so a pin breaks the normal case
and leaves the bookmark move open. An operator may set `trust.reviewed` to `<bookmark>@<remote>`.

### 5.3 If commands must stay host-only (open question 1)

Flip the default of `trust.repo_commands` to `false`. Command keys then come only from
layer 3: `graphql_command` moves to `[repos.<name>.broker.server]`, and each identity hook
needs a layer 3 entry. Repo profiles keep everything else. `config` lists the dropped repo
values as `ignored: command keys are host-only here`.

## 6. Interfaces

### 6.1 `cld broker config`

```
cld broker config [--repo <name>] [--json] [--as-profile]
```

Read-only; starts no runner. The client snapshots first (6.5). Exit 0 when it prints, 3
when the repo cannot be resolved. Profile errors are output, not an exit code. Sample:

```
profile  .cld-broker.toml at 3f9c2a1b7e0d  (fork point of trunk() and your revision)
project.dir = "api"                                     # repo
server.command = "poetry run ..."                       # host .cld/config.toml (graphql_command)
- your revision changes .cld-broker.toml; it takes effect after review and merge
```

- Every effective key with its value and source (`default`, `repo`, `host
  .cld/config.toml` with the legacy name, or `operator`), then the warnings.
- `--json`: `{repo, profile: {path, revision, status, error}, trust, effective, sources,
  warnings}`. `--as-profile`: the effective settings, minus `trust.*` and `secrets.*`, as a
  ready `.cld-broker.toml` (the migration aid). The "your revision changes" warning compares
  the file at `C` and at `R`. On the host, `cld-brokerctl run [-P <revset>] config` works too.

### 6.2 Broker options and `plan_test_run`

```
cld broker run-tests [--repo N] [--suite N] [--lf[=ID]] [--max-runtime S] [--] [pytest args...]
```

**Options parser.** `parse_broker_options(argv, allowed) -> (BrokerOptions(repo, suite, lf,
max_runtime), rest)` is the only broker-option parser; it replaces `parse_repo_arg`
(`broker/cld-broker.py:530-540`) in every action. Broker options lead, in any order among
themselves. The first other token, or `--`, ends them; that `--` is dropped. Forms:
`--opt value` or `--opt=value`; `--lf` is only `--lf` or `--lf=<id>`. `allowed` is all four
for `run-tests` and `tests start`, and `--repo` only for `graphql` and `config`, which parse
their own flags after it. For `run-tests` and `tests start`, a broker option name later in
`rest` is refused, exit 2; so are pytest's own `--lf` and `--last-failed` (test-jobs 3.5).

**Planning.** `plan_test_run(profile, suite, argv, extra_selections=())` works in seven
steps. `extra_selections` are node ids from `--lf` (test-jobs 3.5), treated exactly like
the caller's selections.

1. **Split** argv into options and selections. Tokens starting with `-` are options.
   These take a separate value: `-k -m -p -c -o -n -W -r --maxfail --tb --deselect
   --ignore --ignore-glob --rootdir --confcutdir --basetemp --durations --dist --junitxml
   --report-log --log-level --import-mode --capture --color --override-ini`. Other options
   need `--opt=value`; otherwise their value looks like a selection, and the refusal names
   it. The rest, plus `extra_selections`, are selections; for `path::node` the path counts.
2. **Normalise:** strip a leading container prefix, `/workspace/current/` or
   `/workspace/<ticket>/<repo name>/`; refuse any other absolute path. Then strip a leading
   `<d>/`, where `d` is `project.dir` or a suite `dir` (longest match), so `api/tests/unit`
   becomes `tests/unit`. This removes the exit-4 failure for broker runs.
3. **Touch:** a selection touches suite S when it lies inside one of S's paths, or one of
   them lies inside it. A suite with no paths is touched only by name.
4. **Suite:** with `--suite S`, S must exist and every selection must lie inside its
   paths. Without it, a single touched suite is used; with no selection, `default_suite`.
5. **Rule:** with `one_suite_per_run`, refuse a run that touches two or more suites, or
   has no suite and no selection.
6. **Build** the `TestPlan`. Argv: suite `args`, then caller options, then selections (or
   the suite's paths if there are none).
7. **Mark** on stderr:
   `[cld-broker] profile 3f9c2a1b (reviewed) | suite integration (inferred) | pytest cwd api`.

`TestPlan` fields: `suite` (name or `""`); `suite_source` (`explicit`, `inferred`,
`default` or `none`); `argv`; `env` (`PYTEST_ADDOPTS` as `shlex.join` of `addopts`,
`POETRY_INSTALL_ARGS`, `TZ` if set, then `runner.env`; callers may add `CLD_*` names, never
remove one); `project_dir` (the suite's `dir`, else `project.dir`; it becomes
`PROJECT_SUBDIR`); `env_file` (host path of the `.env`: `secrets.env_file` if set, else
`<repo>/<project_dir>/.env`; callers pass it to the containment resolver and mount only what
it returns); `max_runtime` (the suite's, else `tests.max_runtime`, else `TESTS_MAX_RUNTIME`;
never above `TESTS_MAX_RUNTIME`). The synchronous `run_tests` path and the job path
(test-jobs 3.1) both build their `docker run` from a `TestPlan`; neither assembles its own.

| Exit | Refusal |
|---|---|
| 3 | `denied: unknown suite 'x' -- suites: unit, integration, json (cld broker config)` |
| 3 | `denied: this repo runs one suite per call -- pass --suite <name> or paths inside one suite: unit (tests/unit), ...` |
| 3 | `denied: the selection touches suites integration and json -- run them in separate calls` |
| 3 | `denied: 'tests/json/x.py' is outside suite 'unit' (tests/unit)` |
| 3 | `denied: '/home/x/y.py' is outside the repo -- pass a path relative to the repo` |
| 2 | `denied: put broker options before the pytest arguments` |

### 6.3 graphql

- `start` and `restart` read `project.dir`, `runner.*`, `server.*` and `identities.*`.
- `server.enabled = false`: exit 3, `denied: <repo> declares no live server: <note>`.
- No command: exit 3, `denied: no server command for <repo> -- set [server] command in
  .cld-broker.toml (or the host .cld/config.toml)`.
- `start` labels the container `org.cld.gql-port`, `org.cld.gql-health-path`,
  `org.cld.gql-profile-rev` and `org.cld.gql-identity`. The last is compact JSON
  `{"hook", "timeout", "roles", "source"}` from `identity_settings`; none of it is secret.
- `status`, `query local`, `logs` and every identity op read the labels, not the current
  profile, because trunk can move while a server runs. Unlabelled (older) containers fall
  back to the profile for port and health path; identity ops refuse them (identities 4.4).
  A trust or hook change takes effect at the next `start`.

**Gap 2 in version 1.** The mechanism ships in this repo. diskuze: Gap 2 closes after two
follow-ups outside this repo: a diskuze profile with a real `server.command` (needs ticket
question 3 answered "yes"), then the diskuze hook. lide: not closed in version 1; its hook
waits on ticket question 2, and until then the static `CLD_GRAPHQL_AUTH_LOCAL` and
`CLD_GRAPHQL_COOKIE_LOCAL` stay the only way. The lide internal API is not in version 1.

### 6.4 Accessors for the sibling designs

- `load_profile(caller, repo) -> Profile`: values, sources, warnings, `R`, status, repo
  name; once per request. `Profile.get("dotted.key")` gives the value after the trust filter.
- `parse_broker_options(argv, allowed)` and `plan_test_run(profile, suite, argv,
  extra_selections=()) -> TestPlan` (6.2). Test jobs uses only these two for suites.
- `graphql_settings(profile) -> (enabled, note, command, port, health_path, env_file)`.
- `identity_settings(profile) -> IdentitySettings(hook, timeout, roles, source)`.
  `source` is `unset`, `repo`, `host`, `operator` or `dropped` (dropped by
  `trust.repo_commands = false`). `timeout` is already capped at 60.

### 6.5 Client changes (owned here; siblings add cases)

- **Snapshot predicate.** `_needs_snapshot(action, args)` replaces the `_REVISION_ACTIONS`
  tuple (`cld/broker.py:35`, `:114-115`). It skips a leading `--repo <name>` or
  `--repo=<name>`, then decides: `run-tests` yes; `graphql` yes for every op, as today;
  `config` yes; `tests` only when the op is `start`; anything else no.
- **`--` survives the CLI.** The `broker` command (`cld/cli_container.py:280`) adds
  `"allow_interspersed_args": False` to its context settings. Today click drops `--`:
  `cld broker run-tests -- -k x` reaches the broker as `-k x`. The MCP path keeps it.

## 7. Compatibility and migration

**Unchanged:** a repo with no profile and no `[broker]` table behaves as today; the
explicit env values equal the image defaults, so existing dispatch tests keep their
`docker run` tails. The wire format and old clients are unchanged. On an old broker,
`--suite` fails in pytest (exit 4) and `config` is an unknown action. Servers started
before the upgrade keep working (6.3).

**Changed:** unquoted legacy values are honoured. An absolute `pyproject_dir` in layer 2 is
dropped; an `.env` outside the repo needs `secrets.env_file` in layer 3. `broker` joins
`_TOML_KEYS` (`cld/config.py`) and `_ENTRY_KEYS` (`cld/registry.py`); legacy keys stay known.

**Ticket bootstrap.** `ticket_repo_bootstrap` (`cld/docker.py:547-565`) reads `pyproject_dir`
for the dir of the first-boot `poetry install`. New order: layer 3 `project.dir`, layer 2
`[broker] project.dir`, legacy `pyproject_dir`, `project.dir` from `.cld-broker.toml` in the
host checkout's working copy, then `.`. The working copy is not reviewed; that is fine, as
the value only picks a dir for an install inside the agent's own container. cld opens the
file with `O_NOFOLLOW`, needs a regular file under 64 KiB, and checks the value as a rel
path. Without this, step 4 below would make lide bootstrap in `.` instead of `api/`.

**Operator steps, per repo:** (1) upgrade cld, rebuild `runtests` and `graphqlserver`,
restart the broker; (2) `cld broker config --repo <r>` shows the legacy keys as
`host .cld/config.toml`; (3) `--as-profile` gives a starting file: add suites and the
timezone, and commit `.cld-broker.toml` through a normal MR; (4) after the merge, `config`
warns that legacy keys shadow the profile: delete them, keep real host differences in
`[broker]`, and put rule keys, trust settings and `secrets.env_file` in layer 3. cld never
rewrites host config files. Once both example repos have moved, legacy keys can become an error.

## 8. Test plan

### 8.1 Automated, run with `cld broker run-tests`

- **`tests/test_broker_profile.py`** (new; loads the script by path, calls functions
  in-process): `TestParseProfile`; `TestPathRules` (rel `project.dir` in every layer,
  `secrets.env_file` layer 3 only and absolute); `TestMergeLayers`; `TestLegacyKeys`
  (quoted and unquoted port, `[broker]` beats flat keys, absolute `pyproject_dir` dropped);
  `TestTrustFilter` (layer 2 rule keys dropped); `TestBrokerOptions` (any order, both forms,
  `--`, later names refused, `allowed` per action); `TestClassifyPytestArgv`;
  `TestPlanTestRun` (explicit, inferred and default suites, `extra_selections` checked
  inside the suite, container prefixes, every 6.2 refusal, argv, env, `env_file`,
  `max_runtime`); `TestIdentitySettings` (each `source`); `TestRenderConfig` (sources,
  warnings, masking, `--json`, `--as-profile`).
- **`TestReviewedRevision`** (same file) runs real `jj` on a temp repo, skipped without jj,
  as `tests/test_runner_entrypoints_sh.py` does: fork point with and without `C`, root
  commit, `root-file:` probe of an absent and a present file, jj error, `-P`, a missing id.
- **`test_broker_script.py`:** the fake `jj` also answers `file list` and `file show`.
  `TestDispatchRunTests` gains suite cases (`-e PYTEST_ADDOPTS=`, `-e TZ=`, suite args).
  New `TestDispatchConfig`. Graphql start tests cover `enabled = false` and the four labels.
- **Others:** `test_config.py`, `test_registry.py`: no warning for `broker` tables.
  `test_broker.py`: the `_needs_snapshot` table. `test_cli_container.py`: `cld broker
  run-tests -- -k x` and `graphql query --as-user u -- local q` keep `--`.
  `test_docker.py`: the bootstrap dir order, and a symlinked `.cld-broker.toml`.

### 8.2 Manual host checks

1. `jj -R <repo> --ignore-working-copy file list -r 'trunk()' 'root-file:".cld-broker.toml"'`
   prints nothing and exits 0 before the merge, and prints the path after it.
2. `docker run --rm --entrypoint date -e TZ=Europe/Prague runtests:latest` prints CET or CEST.
3. In a ticket container, `cld broker config --repo lide` shows the fork-point profile; an
   edit to `.cld-broker.toml` in the workspace adds the review warning.
4. `run-tests --repo diskuze` with no args is refused within seconds. `run-tests --repo
   lide api/tests/unit -x`, and the same with the `/workspace/...` path, run with no exit 4.
5. `run-tests --repo diskuze tests/unit/test_cursor.py` infers `unit`; no timezone failures.
6. `graphql start` for a repo with `enabled = false` shows "declares no live server". With
   `trust.repo_commands = false` in layer 3, the repo `server.command` is dropped. A host
   with only legacy keys behaves as before; `config` shows the legacy sources.
7. lide ticket container after operator step 4: first boot installs in `api/`.

## 9. Implementation packages (names, not order: see section 10)

| # | Package | Exit criterion |
|---|---|---|
| 1 | **Profile model and layers.** Pure functions: parse, validate, legacy mapping, merge, trust filter (rule keys too), path rules. | the parse, path, merge, legacy and trust classes pass |
| 2 | **Reader and wiring.** `load_profile` with the 4.2 store check, `-P`, `env_file` into the containment resolver, `secrets.env_file`, `resolve_graphql_config` on the profile, `broker` in `cld/config.py` and `cld/registry.py`, `ticket_repo_bootstrap` (7). This package deletes `cld_conf_get`; containment only reroutes its read. | `TestReviewedRevision`, `test_broker_script.py` and the `test_docker.py` cases pass; no caller of `cld_conf_get` is left |
| 3 | **`config` action and client.** `action_config` in three formats, `_needs_snapshot`, the click fix, help text at `cld/cli_container.py:284`. | `TestRenderConfig`, `TestDispatchConfig`, the `test_broker.py` and `test_cli_container.py` cases pass |
| 4 | **Options and run-tests planning.** `parse_broker_options` in every action, `plan_test_run`, the synchronous `run_tests` built from a `TestPlan`, the marker line. | `TestBrokerOptions`, `TestClassifyPytestArgv`, `TestPlanTestRun`, the suite cases of `TestDispatchRunTests` pass |
| 5 | **graphql settings.** `enabled` and `note`, the integer port, the four labels and their read-back, runner env and `TZ`, `identity_settings`. | the graphql tests pass |
| 6 | **Runner images.** `tzdata` in both Dockerfiles, in the shared rebuild. | manual check 2 passes |
| 7 | **Docs and skills.** `broker-run-tests`: `--suite`, `cld broker config`, no hard-coded `-n`, the typo fix. `broker-graphql-server`: a "declares no live server" row, its denial row at `SKILL.md:76`. Legacy key names in `README.md:389`, `docs/quick-start-graphql-mcp.md:40-58`, `MANUAL_TESTS.md:326-335`, `broker/README.md:31`, `graphqlserver/README.md:63`. The config line in `CLAUDE.md`, a pointer in `broker/broker.conf.sample`. | no doc tells agents to edit host config or pass `-n logical`, or presents `pyproject_dir` or `graphql_command` as the main way |
| 8 | **Profiles in diskuze and lide.** User-owned MRs outside this repo, then the operator steps (7). | `config` for both shows `status loaded` and no legacy sources |

## 10. Build order across the designs

Prefixes: P = this doc, J = test jobs, I = identities. J and I depend on P and on steps
1 to 3, not on each other. Each step ends with its packages' exit criteria.

1. **Secrets containment** (`docs/design-secrets-containment.md`): origin mount, `.env` symlink
   escape, resolver. Its own order: the broker, then cld with the cld boot-image rebuild.
2. **Runner images (`runtests`, `graphqlserver`), one rebuild.** P 6 (`tzdata`). J 1: results
   mode, `CLD_RT_*` saved around `.env`, one deadline over the whole body, the log watchdog,
   the reportlog wheel, `runtests.api=2`, the full-entrypoint test harness. I 2:
   `identity-hook.sh` (absolute `timeout` and `bash`, `SECRETS_FILE` default), `CLD_IDENTITY_*`
   unset in both entrypoints, `graphqlserver.api=2`. The old broker never sets the new variables.
3. **Shared masker.** I 1: session, alias and `.env` literals on every output path.
4. **Profile model.** P 1.
5. **Profile reader and wiring.** P 2.
6. **Options and run-tests planning.** P 4.
7. **Graphql settings.** P 5.
8. **`config` action and client.** P 3.
9. **Test jobs**, parallel with step 10: J 2 to J 7. J 5 creates the `--sweep` entry.
10. **Identities**, parallel with step 9: I 3 to I 6. Whichever of J 5 and I 4 lands
    second wires `sweep_identity_cache` into the `--sweep` entry.
11. **Docs and skills, one pass.** P 7, J 8, I 7, one `docs/design-cld-broker.md`
    amendment, `broker/broker.conf.sample`.
12. **Host verification.** P 8.2, J 9.2, I 11.2. Then J 9 removes the synchronous path.
13. **Outside this repo.** P 8. Then the diskuze server command and hook (after ticket
    question 3), then the lide hook (after ticket question 2). No test-account password
    goes into a file a container can read before step 1 ships.

## 11. Assumptions and open questions

**Assumptions:** ticket question 1 is answered "commands from the reviewed revision" (5.3
covers the other answer). Single-user host (`docs/design-cld-broker.md` section 9); the
default branch is protected on GitLab, so `trunk()` holds only merged, reviewed work.
`debian:stable-slim` may lack `tzdata` (unverified). The host jj supports `root-file:`
filesets (`TestReviewedRevision` checks it). pytest node ids are relative to the suite's
`dir`, which holds the pytest config. diskuze and lide facts come from the ticket code map.

**Open questions:**

1. Ticket question 1: may the repo name commands at all? `trust.repo_commands` follows it.
2. Are `integration` and `json` directories or markers in diskuze and lide? The examples
   assume directories. Markers need `args = ["-m", "json"]` and give no path inference.
3. Should layer 3 be able to drop a repo suite? Version 1 can only replace one.

## Appendix: example profile (illustration only, facts unverified)

diskuze. lide differs in its `-n` suite args (section 1) and its server command, moved from the host.

```toml
version = 1
project.dir = "api"
runner.timezone = "Europe/Prague"   # tests/unit/test_cursor.py expects local time
[tests]
one_suite_per_run = true            # AGENTS.md: never integration + json in one run
default_suite = "unit"
suites.unit.paths = ["tests/unit"]
suites.integration = { paths = ["tests/integration"], description = "Real MariaDB. Slow." }
suites.json.paths = ["tests/json"]
[server]
enabled = false                     # until the follow-up (6.3) sets command and [identities]
note = "No local server set up yet (CLD-001 ticket question 3)."
```
