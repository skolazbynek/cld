# Test runs as host-side jobs

> Ticket CLD-001, design direction 1. Closes Gap 1. Code cites are against the
> working copy of 2026-10-21. `docs/design-broker-profile.md` owns the config
> schema, suites, the broker-option parser, the client snapshot rule and the
> build order (its section 10). **Prerequisite:** `docs/design-secrets-containment.md`
> (section 6). Supersedes the "rejected for now" note in
> `docs/design-cld-broker.md` §14. **Status:** design, not implemented.

## 1. Problem

Evidence: ticket notes `CLD-001/gaps.md` (Gap 1) and `CLD-001/live-tests.md`.

- **Blind.** A full diskuze run took 106 min with two `[runtests]` lines until
  the end. Slow and stuck look the same from the container.
- **All-or-nothing output.** The entrypoint returns the last 64 KiB of a
  buffered log (`runtests/entrypoint.sh:88-110`): 1.18 MB kept 4 of 29
  tracebacks. Only the host operator can raise the cap.
- **Lost on interrupt.** The agent's foreground limit is 10 min, and its shell
  tool's default is 2 min. A killed call loses all output, and nothing can find
  the run again.
- **Cannot be stopped.** `run_tests` execs `docker run --rm` with no name,
  label or time limit (`broker/cld-broker.py:457-464`). sshd sends no SIGHUP
  on client loss and the broker reads no stdin, so the run goes on.

## 2. Decisions

| # | Decision | Rejected alternatives |
|---|---|---|
| D1 | **The runner container is the job.** `docker run -d`; no host daemon or supervisor. State is files plus docker labels. | Forked host supervisor per run: a second process kind to find and sweep. `systemd-run --user`: absent on some hosts (WSL). |
| D2 | **Run id `YYMMDD-HHMMSS-xxxx`** (UTC, 4 random hex), regex `[0-9]{6}-[0-9]{6}-[0-9a-f]{4}`. Verbs default to the caller's latest run. | UUID: long, unsorted. Per-session counter: needs a lock. |
| D3 | **Container `cld_rt_<session>_<id>`**, labels `org.cld.rt-session`, `org.cld.rt-id`, `org.cld.rt-repo`, `org.cld.rt-deadline`. Same pattern as `cld_gql_<session>`. Never `org.cld.kind`: `list-containers` and `LabelCaller.from_session` key on it. | `org.cld.kind=runtests`: runners would look like sessions. |
| D4 | **`--rm --init`.** tini at PID 1 reaps xdist and poetry zombies and lets `docker stop` end the run at once (a bash PID 1 ignores SIGTERM). Exit code comes from a file (D6). | Keeping exited containers for their exit code: they pile up and need their own sweep. |
| D5 | **Broker-owned and runner-owned state are split.** Only `<run>/out/` is mounted into the runner. | One shared dir: code under test could rewrite ownership or plant symlinks the broker follows. |
| D6 | **The runner writes** `events` (phases), `full.log`, `report.jsonl` (`pytest --report-log`), `exit`. | junit XML: written only at the end. `pytest-json-report`: dormant since 2022. Parsing terminal text: fragile. |
| D7 | **pytest-reportlog 1.0.0 is baked into the image as a wheel**, installed with `pip --no-index --no-deps` into the run's container-local venv copy, only if missing. | Network install per run: slow. `PYTHONPATH` + `-p`: double registration when the project has it. Changing project lock files: not ours. |
| D8 | **One GNU `timeout` wraps the whole run body** (checkout, install, pytest). Cancel = SIGINT to it, then hard stop. `timeout` relays SIGINT to its process group (poetry, pytest, xdist workers) and arms its SIGKILL follow-up. pytest takes its KeyboardInterrupt path: teardown runs, reportlog writes `SessionFinish`. | `docker kill -s INT`: tini forwards to bash only, and bash waits for its foreground child. Backgrounding pytest under bash: async commands start with SIGINT ignored. tini group mode: `timeout` has its own group. `timeout` around pytest only: a stuck `poetry install` has no bound. |
| D9 | **Max runtime is a deadline** fixed at start. The runner gives `timeout` the time left when the body starts (exit 124). The sweep is the backstop for a runner that never reached its body. | Broker watchdog: the broker process ends with the ssh call. `--stop-timeout`: only applies to a stop someone sends. |
| D10 | **The runner bounds its own log; the broker sweeps lazily.** An in-container watchdog stops a run whose output passes the cap, with no broker call needed. Every `tests`/`run-tests` call sweeps first; `cld-brokerctl sweep` runs it from the host. | cron or systemd timer: host setup we do not own. `--ulimit fsize`: per file, so it also hits large wheels in the venv cache and anything else the run writes. |
| D11 | **Runs survive a client disconnect.** Attached `run-tests` prints the id first; a killed client loses nothing. Bounded by max runtime, the log cap, active limit and orphan sweep. | Cancel on disconnect: throws the result away exactly when the tool limit hits. |
| D12 | **`--lf` is a broker option.** It feeds the stored failed node ids of an earlier run into the profile's `plan_test_run` as selections. | Persisted `.pytest_cache`: concurrent runs overwrite `lastfailed`, and pytest's "no failures, run all" default can start a 106-min run by surprise. Appending ids after the plan: the suite's paths would be added too, and the whole suite would rerun. |
| D13 | **New action `tests` with ops; `run-tests` stays as start + follow + result.** The client forwards argv; `--` survives once the profile's click fix lands (profile 6.5). | Verbs inside `run-tests`: ambiguous with pytest args. MCP Tasks: experimental; can wrap these verbs later. |
| D14 | **Progress is parsed from `report.jsonl`** on each call, plus an idle time (now minus the newest write to `report.jsonl` or `full.log`) that tells slow from stuck. | A custom pytest plugin for totals: more runner code; xdist controller hooks differ. |

## 3. Mechanism

### 3.1 Start (`tests start`, first half of `run-tests`)

1. Sweep (§7.2). Parse run options with the profile's `parse_broker_options`
   (all four allowed). Resolve repo and revision as today. `load_profile`. For
   `--lf`, pick the source run (§3.5), which gives the suite and the ids.
2. `plan = plan_test_run(profile, suite, rest, extra_selections=ids)`
   (profile 6.2). The run's max runtime is `plan.max_runtime`, lowered by
   `--max-runtime`; a higher value is refused (exit 2). Pass `plan.env_file` to
   the containment resolver for the secrets mount. Resolve the ssh agent as today.
3. Refuse an image without label `runtests.api=2` with "rebuild the runtests
   image (runtests/build.sh)". Attached `run-tests` instead falls back to the
   synchronous path with that text as a warning, until package 9 (§10).
4. Under the global `<root>/.lock` (flock), held only for this step: refuse at
   the session's active limit, listing ids. Create `<id>/` and `<id>/out/`
   (0700); write `meta.json` atomically.
5. `docker run -d --rm --init --name cld_rt_<session>_<id>`, the four labels,
   `--user uid:gid -e HOME=/tmp`, the repo, agent and venv-cache mounts as
   today, the mount the containment resolver returns, `-v <root>/<id>/out:/results`.
   Env: `REVISION`, `PROJECT_SUBDIR=<plan.project_dir>`, every `plan.env` entry,
   `CLD_RT_RESULTS_DIR=/results`, `CLD_RT_WORKSPACE=rt-<id>`,
   `CLD_RT_DEADLINE=<epoch>`, `CLD_RT_KILL_GRACE=<s>`,
   `CLD_RT_LOG_MAX_BYTES=<TESTS_LOG_MAX_BYTES>`. Argv = `plan.argv`.
6. Print the id. If `docker run` fails: finalize as `setup_failed`, exit 3.

### 3.2 Runner (`runtests/entrypoint.sh`, `Dockerfile`)

Without `CLD_RT_RESULTS_DIR` the entrypoint behaves exactly as today
(standalone use, `design-cld-broker.md` decision 6). With it set:

- **Control variables.** The entrypoint copies every `CLD_RT_*` value into
  shell locals at the top and reads only the locals. After sourcing `.env` it
  restores the `CLD_RT_*` values, so a project `.env` cannot change them.
- **Workspace** `$CLD_RT_WORKSPACE` (default stays `runtests-$HOSTNAME`), so
  the broker can forget it after a hard stop.
- **One deadline.** With `left = CLD_RT_DEADLINE - now` (at zero or below the
  body is skipped, code 124), the entrypoint runs itself again as the body:
  `sh -c 'echo $$ >/tmp/rt-timeout.pid; exec /usr/bin/timeout -s INT -k "$CLD_RT_KILL_GRACE" "$left" "$@"' rt "$0" "$@"`
  with `CLD_RT_BODY=1`. The body does checkout, `.env`, install, the reportlog
  step and pytest, each in the foreground. The outer run writes `exit`.
- **Events and log.** The body appends `<epoch> <phase> [detail]` to `events`:
  `checkout`, `install`, `pytest`, `done <rc>`. `full.log` replaces
  `/tmp/runtests.out` and is not copied to stdout. Last, the outer run writes
  the code to `exit` (tmp + rename).
- **Log watchdog.** Before the body, the outer run starts a background loop.
  Every 5 s it adds the sizes of `full.log` and `report.jsonl`. Past
  `CLD_RT_LOG_MAX_BYTES` it appends `log_limit` to `events`, sends SIGINT to
  the pid in `/tmp/rt-timeout.pid`, and ends. The outer run kills the loop
  when the body returns. Test code can kill the loop; the sweep is the backstop.
- **Reportlog.** After `poetry install`, if `import pytest_reportlog` fails in
  the venv: `pip install --no-index --no-deps /opt/rt-wheels/pytest_reportlog-*.whl`.
  A failure only warns; the digest then falls back to the log tail.
- **pytest** runs as `poetry run pytest "$@" --report-log="$CLD_RT_RESULTS_DIR/report.jsonl"`,
  appending to `full.log`.
- **Image.** `ARG REPORTLOG_VERSION=1.0.0`, `pip download --no-deps` into
  `/opt/rt-wheels`, `LABEL runtests.api=2`. The entrypoint also unsets
  `CLD_IDENTITY_*` after sourcing `.env` (identities 10).

### 3.3 State

From `final.json` if present; else `docker inspect` running, `out/exit`,
`out/events` and `cancel.json`. Rows are checked top-down.

| Condition | State | Exit of `result`/`wait`/`run-tests` |
|---|---|---|
| running, no `cancel.json` | `running` (phase from `events`) | 75 |
| running, `cancel.json` | `cancelling` | 75 |
| gone, `cancel.json` | `cancelled` | 130 |
| gone, `log_limit` in `events` | `cancelled` (reason `log_limit`) | 130 |
| gone, `exit` = 124 | `timed_out` | 124 |
| gone, `exit`, no `pytest` phase (checkout or install failed) | `setup_failed` | 125 |
| gone, `exit`, `pytest` phase | `finished` | pytest's code (0-5) |
| gone, no `exit` (OOM, manual `rm -f`, dockerd restart) | `lost` | 137 |

The first time a run is seen gone, the broker **finalizes** it under the run's
own lock `<id>/.lock`: forget workspace `rt-<id>` (idempotent), parse
`report.jsonl` once, write `final.json` (state, exit, finish time, counts,
failed ids) atomically. Later calls read only `final.json`. A second finalizer
waits on the run lock, then sees `final.json` and stops.

### 3.4 Progress and digest from `report.jsonl`

Read line by line. Skip invalid JSON (the last line may be half written),
lines over 1 MiB, unknown `$report_type` values and unknown keys.

- `TestReport`: `when=call` gives passed/failed/skipped; failed `setup` or
  `teardown` gives an error; skipped `setup` gives skipped; key `wasxfail`
  gives xfailed/xpassed. `CollectReport` failed: a collection error.
- Total: reportlog has none. Shown only if `full.log` has `collected N items`
  or xdist's `[N items]`; unverified under `-q`.
- Short traceback: `longrepr.reprcrash` (path, line, first 5 message lines)
  and the last `reprtraceback.reprentries[].data.lines` (up to 15). A string
  `longrepr` keeps its last 15 lines.

### 3.5 `--lf`

- `--lf` with `--suite S`: the caller's latest finalized run with the same repo
  and suite S. `--lf` alone: the caller's latest finalized run for the repo; its
  suite becomes the explicit suite. `--lf=<id>`: that run of the caller's; a
  `--suite` naming a different suite is refused (exit 3).
- Its failed and error ids from `final.json`, minus ids starting with `-`, go
  to `plan_test_run` as `extra_selections`. The plan normalises them, checks
  them against the suite, and adds no suite paths.
- Refused (exit 3) if that run had no failures or is not finalized. After a
  `--maxfail` stop only the seen failures rerun; `result` says so.
- pytest's own `--lf`/`--last-failed` in the pytest args is refused by the
  option parser with a hint: no pytest cache survives between runs.

### 3.6 Attached `run-tests`

Runs §3.1, prints `[runtests] run <id> started -- cld broker tests status <id>`,
then follows in the same broker process: a line per phase change, a
`FAILED <nodeid>` line per failure (first 50), a progress line every interval.
When the run leaves `running` it prints the digest and exits with the state's
code. A write error (client gone) ends only the follower (D11). Every line goes
through the shared masker. The old-image fallback (§3.1 step 3) runs the
synchronous path from the same `TestPlan`, unmasked as today; it refuses `--lf`.

## 4. Interfaces

### 4.1 CLI verbs (existing `<action> <session> <b64 argv>` wire)

```
cld broker run-tests [RUN-OPTS] [--] [pytest args]    attached: start, follow, digest
cld broker tests start [RUN-OPTS] [--] [pytest args]  detached: prints the run id
cld broker tests status [ID] [--json]
cld broker tests list [--json]
cld broker tests log [ID] [--tail N | --offset BYTES] [--follow]
cld broker tests result [ID] [--test NODEID] [--json]
cld broker tests wait [ID] [--timeout SECONDS]
cld broker tests cancel [ID | --all]
RUN-OPTS: --repo NAME  --suite NAME  --lf[=ID]  --max-runtime SECONDS
```

- RUN-OPTS must lead, in any order among themselves (profile 6.2).
- `ID` defaults to the caller's latest run; a bad or foreign id is "unknown
  run", exit 3. `--max-runtime` may only lower the cap (§5).
- `log`: last 200 lines by default. `--offset` returns bytes from the offset
  up to the output cap and prints `next offset: N` on stderr. `--follow`
  streams until the run ends, uncapped (for background use or Monitor).
- `result`: the digest; `--test` prints one test's full `longrepr`.
- `wait`: blocks until the run ends or `--timeout` (default 100 s, under the
  shell tool's 120 s default), then prints like `result`. The skill says: loop
  `wait`, or raise the tool timeout to 600000 ms and pass `--timeout 540`.
- `log`, `result`, the digest, `--follow` and attached output pass the shared
  masker (identities 5), with the run's `.env` values as literals.

### 4.2 Exit codes

| Verb | Codes |
|---|---|
| `start`, `status`, `list`, `log`, `cancel` | 0 ok; 2 bad request; 3 refused or unknown run |
| `result`, `wait`, `run-tests` | the run's code (§3.3); 75 still running; 2/3 denial before a run exists |

Denials keep today's 2/3, which already overlap pytest's 2/3; the `denied:`
text on stderr tells them apart.

### 4.3 Output formats

`status` (`--json` gives the same keys):

```
run 261021-143205-a3f9  running (pytest)  repo diskuze  suite unit  rev 1a2b3c4d5e6f
started 14:32:05Z  elapsed 12m04s  deadline in 1h47m  log 1.2 MB
412 done: 405 passed, 5 failed, 2 skipped, 0 errors (of 746)
last tests/integration/test_x.py::test_y  idle 4s
```

`list`: tab-separated, newest first:
`id  state  repo  suite  started  elapsed  passed/failed/errors`.

Digest (`result`, end of `run-tests`), within the output cap:

```
run 261021-143205-a3f9 finished, exit 1, 1h46m (repo diskuze, suite unit)
2149 passed, 29 failed, 3 skipped, 0 errors
FAILED tests/unit/test_cursor.py::test_shift [call]
  tests/unit/test_cursor.py:42: AssertionError: '10:40' != '12:40'
+12 more failures (ids only): <nodeid> ...
full log: cld broker tests log 261021-143205-a3f9
```

Tracebacks are dropped from the end first; ids stay while they fit. Without
`report.jsonl` the digest is the masked tail of `full.log`, as today.

### 4.4 Host state layout

```
<runs root>/                 default ~/.cld/runs, 0700
  .lock                      global flock: active-limit check and run dir creation only
  <id>/                      0700
    .lock                    run flock: finalize, cancel, retention delete
    meta.json                broker: id, session, caller_kind (label|host), repo,
                             repo_name, revision, suite, suite_source, argv, lf_source,
                             container, workspace, image_id, started, deadline
    cancel.json              broker: requested_at, reason (user|timeout|orphan|log_limit)
    final.json               broker: state, exit, finished, counts, failed ids, notes
    out/                     mounted rw at /results: events full.log report.jsonl exit
```

## 5. Config needs

### 5.1 Broker-wide (`broker.conf`, host operator)

All sizes are integer bytes and all times integer seconds, as `parse_env_file`
reads plain `KEY=VALUE` (`broker/cld-broker.py:140-154`).

| Setting | Meaning | Default |
|---|---|---|
| `TESTS_RUNS_ROOT` | host dir for run state | `~/.cld/runs` |
| `TESTS_MAX_RUNTIME` | hard cap on a run's wall time | `7200` |
| `TESTS_CANCEL_GRACE` | SIGINT-to-SIGKILL grace | `30` |
| `TESTS_MAX_ACTIVE` | active runs per session | `3` |
| `TESTS_KEEP_DAYS` | finished runs older than this are deleted | `7` |
| `TESTS_KEEP_BYTES` | total size of finished runs; oldest deleted first | `2147483648` |
| `TESTS_LOG_MAX_BYTES` | `full.log` plus `report.jsonl` above this stops the run | `268435456` |
| `TESTS_OUTPUT_MAX_BYTES` | cap on one `log`/`result`/digest reply | `65536` |
| `TESTS_PROGRESS_INTERVAL` | seconds between attached progress lines | `60` |
| `RUNTESTS_IMAGE` | existing; runner image | `runtests:latest` |
| `REPORTLOG_VERSION` | image build arg, not runtime config | `1.0.0` |

### 5.2 Per repo and per suite (profile)

This design takes suites only through the profile's `parse_broker_options` and
`plan_test_run` (profile 6.2, 6.4). Suite args and paths, the suite's dir, the
`.env` location, `addopts`, `install_args`, timezone, `runner.env`, the
one-suite rule and the suite's max runtime all arrive in the `TestPlan`. This
design adds only its `CLD_RT_*` variables to `TestPlan.env` and never builds
argv itself. Suite choice follows profile 6.2 step 4: `--suite`, else the one
suite the selections touch, else `default_suite` when there is no selection.
The suite name and its source go into `meta.json`, show in `status`/`list`,
and key `--lf`.

## 6. Security and trust

- **No new channel.** Same key, ForceCommand, session regex, label-based repo
  resolution, never-eval'd argv. Secrets stay in the runner.
- **Session scoping.** Every verb filters on `meta.json` `session` equal to the
  caller's session. Ids are regex-checked before any path is built. Runners
  carry no `org.cld.kind`, so a runner name used as a session is refused.
- **`out/` is untrusted.** Code under test writes it. The broker opens its
  files relative to a dir fd of `out/` with `O_RDONLY|O_NOFOLLOW|O_NONBLOCK`,
  then `fstat`s and requires `S_ISREG`, so a symlink, dir or FIFO is refused
  without blocking. It caps bytes read, parses JSON defensively and builds no
  path from content. It never reads `out/` while holding the global lock, so one
  session's files cannot stall another session's calls.
- **Fixed cancel command.** `docker exec` runs a constant
  `kill -INT "$(cat /tmp/rt-timeout.pid)"` as the container's user; a test
  that rewrites the pid file can only signal inside its own container.
- **`--lf` ids** come from runner output; they pass the profile's plan checks
  and become docker argv (no shell).
- **Longer reach (ticket question 6).** Detached runs outlive the client and
  still mount the host checkout read-write until the deadline. Workspaces
  `rt-<id>` and their `jj new` commit under the agent's `@` stay visible for
  the whole run.

**Before containment ships:** the agent can read the host `.env` through the
origin mount, so masking `.env` values in `log`/`result` hides nothing from it.
The secrets mount can follow a symlinked `.env` to any host file. Session
scoping, untrusted `out/` handling and the fixed cancel command hold.

**After containment ships:** the runner mounts only what the containment
resolver returns, and the agent cannot read the host `.env` directly. Masking
then stops accidental leaks of `.env` values into the agent's context. It
remains best effort: code under test reads the `.env` inside the runner and can
print it encoded. `full.log` at rest holds what tests print, for up to
`TESTS_KEEP_DAYS`, in a 0700 dir.

## 7. Failure, cancel and cleanup

### 7.1 Cancel

1. Write `cancel.json` (reason `user`).
2. If `/tmp/rt-timeout.pid` exists (any phase inside the body):
   `docker exec <c> sh -c 'kill -INT "$(cat /tmp/rt-timeout.pid)"'`, then poll
   `docker inspect` each second for up to `TESTS_CANCEL_GRACE` + 5 s. The
   in-runner `timeout -k` kills the body after the grace on its own.
3. Still running, or no pid file yet: `docker rm -f`. Then finalize.

`cancel --all` does this for every active run of the session.

### 7.2 Sweep

Top of every `tests`/`run-tests` call, and the unified host entry below. Never
fails the caller's request. Each run is handled under its own lock taken with
`LOCK_NB`; a busy run is skipped this time.

1. **Overdue:** `org.cld.rt-deadline` more than `2 × TESTS_CANCEL_GRACE` past:
   cancel, reason `timeout`.
2. **Log limit:** `full.log` plus `report.jsonl` over `TESTS_LOG_MAX_BYTES`
   (the watchdog was killed): cancel, `log_limit`.
3. **Orphans:** `caller_kind=label` runs whose session container is removed
   (a stopped one keeps its runs, as in `sweep_gql_orphans`): cancel,
   `orphan`. Host-route runs (`cld_host_*`) are exempt.
4. **Unfinalized:** no container and no `final.json` (also a broker killed
   mid-start): finalize.
5. **Retention:** delete finalized runs older than `TESTS_KEEP_DAYS`, then the
   oldest until under `TESTS_KEEP_BYTES`.
6. **Strays:** containers with label `org.cld.rt-id` and no run dir:
   `docker rm -f`, forget `rt-<id>`.

**Unified host entry (owned here).** `main()` in `cld-broker.py` gains a branch
before `main_cli`: `cld-broker.py --sweep` runs `sweep_gql_orphans`,
`sweep_identity_cache` (identities 3) and the test-run sweep, and prints a line
per reaped item. sshd cannot pass an argument, so only a host shell reaches it,
and it works after a reboot with no session. `cld-brokerctl sweep` calls it the
same env-cleared way as `run`; `graphql-sweep` stays as an alias.
The bash copy in `broker/cld-brokerctl.sh:50-65` goes.

## 8. Compatibility and migration

- **`cld broker run-tests` keeps its argv, the `--repo` rule and pytest's exit
  code.** Output changes: id line, progress, digest instead of a raw 64 KiB
  tail. New codes 124, 125 (was poetry's code), 130, 137.
- `parse_repo_arg` (`cld-broker.py:530`) is replaced by the profile's
  `parse_broker_options` (profile 6.2).
- **Client.** The profile's `_needs_snapshot` (profile 6.5) snapshots `tests`
  only for `start`, so a status poll does not pay about 1.5 s.
- **Image.** Without `runtests.api=2`, `tests start` is refused with a rebuild
  hint and attached `run-tests` falls back to the synchronous path. Package 9
  removes that path after the host checks; the old image is then refused too.
- **Docs:** amendment to `design-cld-broker.md` (§14 superseded),
  `broker.conf.sample`, `runtests/README.md`, and the `broker-run-tests`
  skill (`tests start` + `wait` for long runs, the `wait` timeout advice,
  `--suite`, `--lf`, exit codes).
- **Later:** an MCP layer maps 1:1 onto these verbs via `--json`.

## 9. Test plan

### 9.1 Automated (run with `cld broker run-tests`)

`tests/test_broker_script.py` (fake `docker`/`jj` on PATH, as today). Cases
that need a fake clock (`TestCancel` grace, deadlines, watchdog timing in the
broker) call functions in-process on the module loaded by path, with `time`
patched. Dispatch cases stay subprocess runs.

| Class | Covers |
|---|---|
| `TestRunOptions` | `--max-runtime` above the plan's cap refused; `--lf` and `--lf=ID` forms; the parser itself is the profile's |
| `TestTestsStart` | `-d --rm --init`, name, labels, `/results` mount, `CLD_RT_*` env, `TestPlan` argv and env passed through, resolver mount; `meta.json` 0700; old image, active limit, `docker run` failure |
| `TestRunState` | every §3.3 row, `log_limit` included; finalize runs once under the run lock and forgets `rt-<id>` |
| `TestReportlogDigest` | pass, fail, setup/teardown error, skip, xfail, xpass, collection error, half-written line, unknown type; trimming; cap keeps ids; masking; log-tail fallback |
| `TestUntrustedRunFiles` | symlinked, directory and FIFO `full.log` and `report.jsonl` (no hang); oversized line |
| `TestSessionScoping` | foreign id is unknown; default id is the caller's latest |
| `TestLastFailed` | source run choice with and without `--suite`; ids reach the plan as selections; no suite paths added; foreign id refused; no failures refused; `-` ids dropped |
| `TestCancel` | exec argv; `rm -f` after the grace; no pid file goes straight to `rm -f`; `--all` |
| `TestSweep` | each §7.2 step; busy run skipped; host exemption; failing docker is harmless; `--sweep` entry |
| `TestAttachedRunTests` | id line first, `FAILED` lines, digest, exit code, masking; broken stdout ends only the follower; old-image fallback |
| `TestLogVerb` | `--tail`, `--offset` paging, cap |
| `TestDispatchRunTests` (existing) | updated for the job path |

**Full entrypoint harness** (new, in `tests/test_runner_entrypoints_sh.py`; the
identities tests reuse it). The existing tests run only a cut fragment. The
harness copies a whole entrypoint, rewrites its fixed paths (`/repo`,
`/secrets/.env`, `/tmp/rt-timeout.pid`, `/opt/rt-wheels`, `/results`) to temp
paths, and puts stub `poetry`, `venv-cache.sh` and `pip` on `PATH`. bash, jj and
`timeout` are real. Cases: results mode writes `events`, `full.log`, `exit`;
`CLD_RT_WORKSPACE` used and forgotten; a `.env` that sets `CLD_RT_*` changes
nothing; without `CLD_RT_RESULTS_DIR` output equals today's; SIGINT via the pid
file stops a sleeping fake install and a sleeping fake pytest, and `exit` is
written; a past or short deadline gives 124 in either phase; the watchdog writes
`log_limit` and stops the run; the reportlog step is skipped, installs, or warns;
`CLD_IDENTITY_*` is unset.

`tests/test_broker.py`: `_needs_snapshot` is true for `tests start` and false
for `tests status`. `tests/test_cli_container.py`: `cld broker tests status
--json` reaches `run_action` verbatim.

### 9.2 Manual host checks

1. lide unit via `tests start`, plain and with `-n 4`: `status` phases and
   counts; `result` counts match pytest's summary line in `full.log`.
2. diskuze long run in the background: idle time grows on a slow class;
   `log --offset` pages the whole log; `result` lists all failures.
3. `cancel` in pytest: gone within the grace, DB teardown ran, no `rt-<id>`
   in `jj workspace list`, `SessionFinish` present. `cancel` in install: stops
   within the grace.
4. `--max-runtime 60` on a long suite: `timed_out`, 124.
5. Kill an attached `run-tests` client, and let the tool limit hit once: the
   run continues; `tests result` works later.
6. Remove the session container: next call cancels as `orphan`. Restart
   dockerd mid-run: `lost`; then `cld-brokerctl sweep`.
7. Old image: `tests start` refused, `run-tests` falls back with a warning. A
   test symlinking `/results/full.log` to a host file: `log` refuses it.
8. `--lf` after the diskuze run reruns exactly the failed ids, not the suite.
9. A test that prints without end with `TESTS_LOG_MAX_BYTES=1048576`: the run
   stops as `cancelled` (`log_limit`) with no broker call in between.
10. Claude Code: `run-tests` with `run_in_background`; Monitor on `tests log --follow`.

## 10. Implementation packages (dependency order)

The cross-design order is profile section 10. Package 1 is part of its step 2;
packages 2 to 7 are its step 9; package 8 is in step 11; package 9 is step 12.

| # | Package | Exit criterion |
|---|---|---|
| 1 | Runner results mode (entrypoint, Dockerfile, README), full entrypoint harness | the harness cases and existing `test_runner_entrypoints_sh.py` cases pass |
| 2 | Run store and `tests start`: ids, layout, locks, profile parser and plan, `--max-runtime`, image check, active limit, argv | `TestRunOptions` and `TestTestsStart` pass |
| 3 | State, finalize, `status`, `list`, digest parser, untrusted reads, session scoping | `TestRunState`, `TestReportlogDigest`, `TestUntrustedRunFiles`, `TestSessionScoping` pass |
| 4 | `log`, `result`, `wait` with the shared masker and caps | `TestLogVerb` and the `result`/`wait` cases pass |
| 5 | `cancel`, sweep, the `--sweep` entry, `cld-brokerctl sweep` and aliases | `TestCancel` and `TestSweep` pass |
| 6 | `--lf` through the plan | `TestLastFailed` passes |
| 7 | Attached `run-tests` on the job path; synchronous path kept as old-image fallback | `TestAttachedRunTests` and updated `TestDispatchRunTests` pass |
| 8 | Docs, skill, `broker.conf.sample` | the skill names `tests start` + `wait` and the timeout advice |
| 9 | Host verification, then remove the synchronous `_exec` and the fallback | every §9.2 check recorded in the ticket notes; tests pass after the removal |

## 11. Assumptions and open questions

Assumptions (all re-checked by §9.2):

- pytest calls `pytest_sessionfinish` on KeyboardInterrupt, so a cancelled run
  has `SessionFinish`. `poetry run` execs on Linux.
- GNU `timeout` relays a received SIGINT to its child and process group and
  arms `-k` (coreutils `src/timeout.c`); Debian slim ships it at `/usr/bin/timeout`.
  A SIGINT during `poetry install` ends the install.
- The project venv has pip (`python -m venv` in `venv-cache.sh`). An attached
  multi-hour ssh session is not cut by the broker sshd config.
- Claude Code's shell tool defaults to a 120 s timeout and allows up to 600 s.

Open questions:

1. Is 7200 s the right default cap? The diskuze full run took 106 min.
2. Should runs be cancelled when their session container stops, not only when
   it is removed? This design keeps them, like graphql servers.
3. Does the operator need an all-sessions view, or is
   `cld-brokerctl run -s <session> tests list` enough?
4. Are 7 days and 2 GiB right, given full logs may hold secrets?
5. Is 3 active runs per session right, given all runs share one dev DB host?
