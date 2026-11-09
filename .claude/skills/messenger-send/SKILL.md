---
name: messenger-send
description: >
  Compose and send a cld messenger message to another container's mailbox.
  Drafts subject and body from the user's intent, then delivers atomically.
  Invoke when the user wants to send, message, ping, or write to another agent
  or master.
user-invocable: true
---

# Messenger: send message

Compose a message from the user's intent and deliver it.

## Step 1: Resolve the recipient

If the user did not name a recipient, list available targets:

```bash
cld msg agents
```

The recipient is the container `name` -- `t.<ticket>` for a ticket, `a.<ticket>.<agent>`
for a task-agent -- or a shortname: a bare `<ticket>` for `t.<ticket>`, or a repo basename.
An agent is preferred over a master when both exist for the same shortname.

**Task-agents and peers must be addressed by full container name** -- several
task-agents can share one repo, so a basename identifies nothing and the send is
refused as ambiguous.

## Step 2: Draft subject and body

From the user's intent, draft:

- **Subject** — one line, imperative, specific. No trailing period.
- **Body** — markdown. State the ask (or the update) and give the minimum context
  the recipient needs to act. Skip preamble and sign-offs; the transport already
  records who sent it.

Then decide whether this message obliges an answer, because the transport carries
that as a flag rather than inferring it from the body:

- `--expects-reply` — only for a question the sender cannot proceed without. It is
  what obliges the recipient to reply; without it the recipient is told to send
  nothing back, which is the intended behavior for updates and hand-offs.
- `--answers <id>` — set whenever this message answers one that came in, even if it
  also asks something new.

Confirm the draft with the user before sending unless they asked to send
immediately.

## Step 3: Deliver

Write the body to a tmp file (multi-line safe) and invoke the sender:

```bash
BODY_FILE="$(mktemp -t messenger-send-body.XXXXXX.md)"
cat > "$BODY_FILE" <<'EOF'
<the drafted body verbatim>
EOF
cld msg send --to <recipient> --subject "<subject>" --body-file "$BODY_FILE" \
    [--expects-reply] [--answers <id>]
rm -f "$BODY_FILE"
```

Report the returned id to the user.

## Hop budget (agent-to-agent only)

A message between two task-agents counts against that edge's absolute hop budget, and
the output reports your position: `sent: <id>  a -> b  (hop 3/10)`. Converge as it
nears the limit -- land the exchange rather than letting it drift.

Once the budget is spent the send is **refused** (exit 1) and nothing is delivered, in
either direction, ever again on that edge. Do not retry it and do not work around it:
tell your master, whose channel is never budgeted, and let it decide what happens next.
Messages to a master are never counted.

## Ask budget (agent-to-agent only)

A peer edge also bounds how many questions may be open on it at once, reported as
`(open asks 2/3)`. Only `--expects-reply` sends count, and the counter clears when the
question the exchange started from is answered.

Past the limit, **the ask is refused but the edge stays open** -- an answer or a plain
update still delivers. So the two ways out are to answer the open question with a
stated assumption, or to ask the master to rule. Never re-send the same question.
