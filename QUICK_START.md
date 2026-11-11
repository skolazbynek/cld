# Quick start

## Once

```bash
poetry install
cld build
```

# Ticket containers (v2)

Work a ticket interactively: one container, your repos mounted, Claude driven
from the host shell. Spec: `PRODUCT_DESIGN.md`; design:
`docs/design-ticket-containers.md`.

## 1. Register your repos (once)

```bash
cld repos add lide-api ~/projects/lide-api
cld repos add diskuze-api ~/projects/diskuze-api
cld repos          # list
```

## 2. Start the ticket

```bash
cld start LIDE-2600 lide-api diskuze-api    # or bare `cld start LIDE-2600` for a picker
```

Each repo anchors on its registry `default_rev` (fallback `trunk()`; on a git
repo the remote HEAD branch, else `HEAD`); override per repo with
`lide-api@<rev>`. A git repo works too, with differences (commit before
broker tests, no snapshots): see README *Git users*.

## 3. Work

```bash
cld claude LIDE-2600                        # the daily verb: Claude at the ticket root
cld claude LIDE-2600 -- --continue          # resume the ticket's last conversation
cld claude LIDE-2600 -- --model opus        # anything after -- goes to claude
cld status LIDE-2600                        # anchors, bookmark tips, live session
cld restart LIDE-2600 --reanchor lide-api@main  # move the anchor onto a new revision
```

## 4. Pause / finish

```bash
cld stop LIDE-2600         # pause; `cld start LIDE-2600` warm-starts it
cld shutdown LIDE-2600     # end of ticket; commits survive in every repo store
```

**Coming from v1?** `cld repos add` each repo you work on. If a standing
`cld agent` container from an older cld is still around, remove it with
`docker rm -f` and `jj bookmark forget` its session -- the role is gone. Verb
map and behavior changes: README, "Ticket containers (v2)".

# Quick start: task-agents

Hand one bounded task to one agent, on the host. Design and details:
`docs/design-task-agents.md`. jj repos only: a git repo is refused.

## 1. Spawn

```bash
cd ~/projects/myrepo
cld task-agent start implementer -n add-oauth -p "Add OAuth login to the web app. Tests must pass."
```

- `implementer` — the role persona (`cld prompts` lists them).
- `-n add-oauth` — the task slug: it ends the agent's identifier `a._host.add-oauth`
  (container, mailbox, session bookmark); the deliverable branch is `a._host.add-oauth.mr`.

It starts working immediately; your task is its first turn.

## 2. Watch

```bash
cld task-agent status                  # phase, messages, cost
cld task-agent transcript add-oauth    # the conversation
```

## 3. Tell it to wrap up

```bash
echo "Wrap up: squash your work into a._host.add-oauth.mr and report what landed." > /tmp/msg.md
python -m cld.messenger.send --to a._host.add-oauth \
    --subject "wrap up" --body-file /tmp/msg.md --expects-reply

python -m cld.messenger.inbox          # its reply lands here
```

`--expects-reply` is what obliges an answer. Without it the agent does the work and
stays quiet -- which is what you want for instructions you won't act on, and is why
two agents no longer thank each other in a loop.

## 4. Take the work, then reap

```bash
jj log -r a._host.add-oauth.mr         # the branch survives teardown
cld task-agent shutdown add-oauth
```
