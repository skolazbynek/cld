"""The one place that builds the identifier of every object cld spawns.

An identifier is one dotted string: it is the docker container name, the jj
workspace name, the session bookmark, the mailbox folder and the broker caller
id. See docs/design-bookmark-naming.md. No part ever contains a dot.

``broker/cld-broker.py`` is standalone and cannot import this module: the few
strings it builds are duplicated there, with a pointer to this file. So is
``imgs/claude-devcontainer/vcs-lib.sh`` (the anchor ref of a git ticket).
"""

import re

SEP = "."
HOST_TICKET = "_host"

_TASK_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def ticket_slug(ticket: str) -> str:
    """Sanitize a free-form ticket name into the slug that names the container,
    workspaces, bookmarks and mailbox (``LIDE-2600`` -> ``lide-2600``)."""
    slug = re.sub(r"[^a-z0-9]+", "-", ticket.lower()).strip("-")
    if not slug:
        raise ValueError(f"ticket name {ticket!r} sanitizes to an empty slug")
    return slug


def ticket_id(ticket: str) -> str:
    """Identifier of a ticket: ``t.<slug>``. Idempotent on an already-sanitized slug."""
    return f"t{SEP}{ticket_slug(ticket)}"


def agent_id(ticket: str | None, slug: str, suffix: int = 0) -> str:
    """Identifier of a task-agent: ``a.<ticket>.<slug>[-<suffix>]``.

    *ticket* is a ticket slug, None for an agent with no ticket (``_host``).
    *suffix* > 1 is the collision disambiguator.
    """
    if not _TASK_SLUG_RE.match(slug):
        raise ValueError(
            f"invalid task slug {slug!r}: expected kebab-case (lowercase letters, "
            "digits and dashes, starting with a letter or digit)"
        )
    base = SEP.join(("a", ticket or HOST_TICKET, slug))
    return f"{base}-{suffix}" if suffix > 1 else base


def run_id(name: str) -> str:
    return f"run{SEP}{name}"


def chain_id(name: str) -> str:
    return f"chain{SEP}{name}"


def step_id(chain: str, step: str) -> str:
    return SEP.join(("chain", chain, step))


def ticket_of(identifier: str) -> str | None:
    """The ticket slug an identifier belongs to: ``t.<t>`` and ``a.<t>.<x>`` give
    ``<t>``; ``a._host.<x>``, other kinds and ``""`` give None."""
    kind, _, rest = identifier.partition(SEP)
    if kind == "t" and rest:
        return rest
    if kind == "a":
        ticket = rest.partition(SEP)[0]
        if ticket and ticket != HOST_TICKET:
            return ticket
    return None


def check_part(part: str) -> None:
    """Raise ValueError when *part* cannot be (a piece of) a git ref name."""
    if (
        not part
        or "/" in part
        or SEP * 2 in part
        or part.startswith(SEP)
        or part.endswith(SEP)
        or part.endswith(".lock")
    ):
        raise ValueError(
            f"invalid name {part!r}: it must be non-empty, without '/' or '..', "
            "and must not start or end with a dot or end with '.lock'"
        )


def deliverable(agent_identifier: str, branch_arg: str = "") -> str:
    """The deliverable (MR) bookmark of a task-agent.

    Default ``<id>.mr``; a *branch_arg* without a dot is appended
    (``<id>.<branch_arg>``), one with a dot is used as is.
    """
    if not branch_arg:
        return f"{agent_identifier}{SEP}mr"
    check_part(branch_arg)
    return branch_arg if SEP in branch_arg else f"{agent_identifier}{SEP}{branch_arg}"


def anchor_ref(identifier: str) -> str:
    """Hidden git ref pinning a git ticket's scratch commit S. Outside ``refs/heads``
    on purpose: no branch list shows it and ``git push --all`` skips it."""
    return f"refs/cld/anchor/{identifier}"


def ended_ref(identifier: str, stamp: str) -> str:
    """Where shutdown archives the tip of a git ticket's session branch before
    deleting the branch. *stamp* is a UTC ``YYYYmmddTHHMMSSZ`` string; the
    detached HEAD of the worktree is archived under the same stamp plus
    ``-detached``."""
    return f"refs/cld/ended/{identifier}/{stamp}"


def docker_name_filter(identifier: str) -> str:
    """``docker ps --filter`` value matching exactly *identifier*: docker takes
    the value as a regex, so the dots must be escaped."""
    return "name=^" + re.escape(identifier) + "$"


def docker_prefix_filter(prefix: str) -> str:
    """``docker ps --filter`` value matching every name that starts with *prefix*
    (escaped like ``docker_name_filter``)."""
    return "name=^" + re.escape(prefix)
