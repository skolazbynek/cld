---
name: broker-run-tests
description: >
  Run the repo's pytest suite through the cld broker (`cld broker run-tests`)
  from inside a cld ticket or task-agent container. The broker runs it on the
  host, with the repo's real secrets (DB/Redis credentials etc.), against
  your latest edits. Invoke whenever you need to run tests, run pytest, or
  check whether tests pass. A task-agent needs its parent's explicit go-ahead
  for the run first.
user-invocable: true
---

# Run tests through the cld broker

`cld broker run-tests` checks out your workspace's current state into a fresh
workspace on the host, installs the repo's dependencies, sources the repo's
host `.env` and runs pytest there. Only the output comes back. This container
never holds the secrets, so a database-backed suite run in-container tests
nothing real.

**Task-agent:** run it only when your parent asked for this run. Ask with
`send()` and wait otherwise.

## Run

```bash
cld broker run-tests [--repo <name>] [pytest args...]
```

```bash
cld broker run-tests                               # whole suite
cld broker run-tests -x tests/unit/test_foo.py     # one file, stop at first failure
cld broker run-tests --repo api -k login tests/    # multi-repo ticket
```

- Set the Bash tool's `timeout` to `600000` for this command, or use
  `run_in_background` for a whole suite. The default two minutes is often too
  short; a killed call loses the output.
- `--repo <name>` comes first, and only when the ticket mounts more than one
  repo. `cld repos` lists the names.
- Everything else goes to pytest unchanged. Defaults are
  `--tb=short --disable-warnings -q --maxfail=30`; your own flags override
  them. Do not add `-q`: twice-quiet pytest drops the pass/fail count line.
- Add `-n logical` to run the tests in parallel. Use this for bigger chunks of long testslike integration.
- The exit code is pytest's: 0 passed, 1 failures, 2 interrupted, 4 usage
  error, 5 no tests collected.

## What is tested

- Your workspace tip, uncommitted edits included: the client snapshots the
  working copy right before the call. No commit is needed.
- **Git repo: committed work only.** A git workspace is not snapshotted. The
  broker tests the tip of your session branch (`t.<ticket>`), so `git add` and
  `git commit` before every run. Edits in the working tree are not seen.
- Only files under version control. Ignored files (local fixtures, caches,
  your workspace's `.env` link) do not exist in the test checkout.
- Runs are independent: several can run at the same time.

## Reading the output

- `[runtests] ...` lines are progress markers. The rest is the install log
  followed by pytest's own output, capped to the last 64 KiB.
- The first run after a dependency change installs every package (minutes).
  Later runs reuse a cached environment and install only the project.
- `[runtests] WARN: secrets file /secrets/.env not found` means the repo has
  no `.env` on the host. Tests needing a database or other services will fail
  on configuration. Tell the user.
- An install failure ends the run before pytest; the error is at the end.
- `git+ssh://` dependencies (e.g. pinned to GitLab) install with your
  container's own forwarded ssh-agent. Errors from that step are in the table
  below.

## Broker-level failures

| Message | What to do |
|---|---|
| `the cld broker is not configured for this container` | Not wired for this container. Tell the user and stop. Do not fall back to in-container runs for tests that need secrets. |
| `denied: ... mounts N repos -- pass --repo <name>` | Add `--repo <name>` as the first argument. |
| `denied: repo '<x>' is not mounted` | Use a name from `cld repos`. |
| `denied: --repo is only for ticket containers` | Drop `--repo`; a task-agent has one repo. |
| `cannot resolve revision for workspace ...` | This workspace is gone, or (git repo) the session branch `t.<ticket>` does not exist. Tell the user. |
| `no cld container for session ...` | Host-side state problem. Tell the user. |
| ssh `Connection refused` / `timed out` | The broker is not running on the host. Ask the user to run `cld-brokerctl start`. |
| ssh `Host key verification failed` | The pinned broker host key does not match. Tell the user. |
| `No such image: runtests:latest` / `Unable to find image` | The image is not built on the host. Ask the user to run `runtests/build.sh`. |
| `No user exists for uid N`, or `ssh: not found` while installing a git dependency | The host image predates ssh support. Ask the user to rebuild it (`runtests/build.sh`). |
| `Permission denied (publickey)` while installing a git dependency | This container has no ssh-agent, or its key cannot read that repo. Check `ssh-add -l` here; tell the user. |
| `REMOTE HOST IDENTIFICATION HAS CHANGED` / `Host key verification failed` | The git host's key differs from the one learned earlier. Possibly an attack: tell the user, do not work around it. |

Never hand-build an `ssh` call to the broker, and never try to fix the broker
from inside the container: it is host infrastructure the user operates.
