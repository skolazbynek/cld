"""Shared anchor-change helpers.

Two primitives that every cld subcommand uses to satisfy the contract
described in docs/design-anchor-change.md:

- ``resolve_anchor``     pin a (possibly symbolic) revision to a change id
                         (jj) or commit hash (git).
- ``assert_descendant``  verify a revision lies in the anchor's descendants.

Anchor staging (creating a child commit B of the resolved anchor A containing
`.cld-run/*`) lives in :mod:`cld.vcs.scratch`. The container ephemeral
workspace layout means no host-side workspace directory is ever created; jj
bookmarks + watchman snapshots against the origin's `.jj/repo/store` provide
persistence across `docker rm && docker run`.
"""

from cld.log import get_logger
from cld.vcs import VcsBackend

log = get_logger(__name__)


def resolve_anchor(vcs: VcsBackend, revision: str) -> str:
    """Pin the anchor revision: its change id on jj, a commit hash on git.

    The change id survives a host-side rebase of the anchor, which rewrites
    its commit hash. Git has no change ids. Empty ``revision`` defaults to
    ``@`` (jj) or ``HEAD`` (git), respecting ``vcs.workspace_revision`` when
    invoked from a secondary workspace.
    """
    if not revision:
        revision = vcs.workspace_revision or ("@" if vcs.name == "jj" else "HEAD")
    if vcs.name != "jj":
        return vcs.resolve_revision(revision)
    result = vcs.run(["log", "-r", revision, "--no-graph", "-T", "change_id", "-n", "1"])
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"Failed to resolve revision '{revision}': {result.stderr.strip()}")
    return result.stdout.strip()


def assert_descendant(vcs: VcsBackend, anchor_hash: str, candidate: str) -> None:
    """Raise RuntimeError if ``candidate`` does not have ``anchor_hash`` in its ancestry."""
    if vcs.name == "jj":
        result = vcs.run([
            "log", "-r", f"ancestors({candidate}) & {anchor_hash}",
            "--no-graph", "-T", "commit_id", "-n", "1",
        ])
        if result.returncode != 0 or not result.stdout.strip():
            raise RuntimeError(
                f"anchor_violation: {candidate} is not a descendant of {anchor_hash}"
            )
    else:
        result = vcs.run(["merge-base", "--is-ancestor", anchor_hash, candidate])
        if result.returncode != 0:
            raise RuntimeError(
                f"anchor_violation: {candidate} is not a descendant of {anchor_hash}"
            )


def find_anchor_commit(vcs: VcsBackend, session: str) -> str:
    """Find the change id of a ticket's own scratch commit S: the commit at or below the
    *session* bookmark (which sits on the anchor) carrying its own ``cld anchor: <session> mode=<mode>`` staging
    description (the same revset ``cld_recover_anchor`` and ``_effective_anchor``
    use to find it -- see ``imgs/claude-devcontainer/vcs-lib.sh`` and
    ``cld/docker.py``). jj-only.

    Raises RuntimeError if zero or more than one commit matches -- either
    means the caller cannot safely identify which commit is S.
    """
    revset = (
        f"heads(descendants({session}) & description(glob:'cld anchor: {session} *'))"
    )
    result = vcs.run(["log", "-r", revset, "--no-graph", "-T", 'change_id ++ "\\n"'])
    hashes = (
        [line for line in result.stdout.splitlines() if line.strip()]
        if result.returncode == 0
        else []
    )
    if len(hashes) != 1:
        raise RuntimeError(
            f"expected exactly one anchor commit for session {session!r}, "
            f"found {len(hashes)}: {hashes}"
        )
    return hashes[0]


def _op_log_head(vcs: VcsBackend) -> str:
    result = vcs.run(["op", "log", "--no-graph", "-T", "id", "-n", "1"])
    if result.returncode != 0:
        raise RuntimeError(f"could not read the jj op log: {(result.stderr or '').strip()}")
    return result.stdout.strip()


def _ops_since(vcs: VcsBackend, before_op: str) -> list[str]:
    """Every op id created since *before_op*, newest first (matching jj's own
    op log order), by walking the log until *before_op* is reached."""
    result = vcs.run(["op", "log", "--no-graph", "-T", 'id ++ "\\n"'])
    if result.returncode != 0:
        raise RuntimeError(f"could not read the jj op log: {(result.stderr or '').strip()}")
    new_ops = []
    for op_id in result.stdout.splitlines():
        op_id = op_id.strip()
        if not op_id or op_id == before_op:
            break
        new_ops.append(op_id)
    return new_ops


def _conflicted_change_ids(vcs: VcsBackend) -> set[str]:
    result = vcs.run(["log", "-r", "conflicts()", "--no-graph", "-T", 'change_id ++ "\\n"'])
    if result.returncode != 0:
        raise RuntimeError(f"could not list conflicted changes: {(result.stderr or '').strip()}")
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def revert_ops(vcs: VcsBackend, op_ids: list[str]) -> None:
    """``jj op revert`` every id in *op_ids*, newest first (the order they are
    already recorded in by ``move_anchor_commit``)."""
    for op_id in op_ids:
        result = vcs.run(["op", "revert", op_id])
        if result.returncode != 0:
            raise RuntimeError(
                f"jj op revert {op_id} failed: {(result.stderr or '').strip()}"
            )


def move_anchor_commit(
    vcs: VcsBackend, anchor: str, target: str, mode: str, session: str,
) -> list[str]:
    """Move a ticket's scratch commit S onto *target* (design section 3). Both
    are change ids, so S keeps its id across the move:

    - isolated: ``jj rebase -r S -o target`` -- only S moves; S's own old
      children stay behind on S's old parent.
    - shared: ``jj rebase -r S -A target`` -- S is inserted after target;
      target's old children move onto the rebased S.

    Then the bookmark rule: the session bookmark must sit on the new anchor --
    S' when isolated, *target* when shared. It follows S' by itself when
    isolated; otherwise it is force-moved there (``--allow-backwards``), as
    without this a reattach can no longer find S.

    Already-in-place rule: if S's current parent already is *target*, no
    jj command runs at all (this is what makes undo idempotent).

    Returns the op ids this call created, newest first, so a caller that
    fails on a later repo can revert them with ``revert_ops``. Raises
    RuntimeError -- after reverting its own ops -- on a jj command failure or
    on a new conflict appearing that was not there before.
    """
    current_parent = vcs.run(
        ["log", "-r", f"parents({anchor})", "--no-graph", "-T", "change_id", "-n", "1"],
    ).stdout.strip()
    if current_parent == target:
        return []

    ops: list[str] = []
    before_conflicts = _conflicted_change_ids(vcs)

    def _fail(message: str) -> None:
        revert_ops(vcs, ops)
        raise RuntimeError(message)

    op_before = _op_log_head(vcs)
    rebase_flag = "-o" if mode != "shared" else "-A"
    result = vcs.run(["rebase", "-r", anchor, rebase_flag, target])
    ops = _ops_since(vcs, op_before) + ops
    if result.returncode != 0:
        _fail(f"jj rebase -r {anchor} {rebase_flag} {target} failed: {(result.stderr or '').strip()}")

    new_conflicts = _conflicted_change_ids(vcs) - before_conflicts
    if new_conflicts:
        _fail(f"reanchor introduced new conflicts: {', '.join(sorted(new_conflicts))}")

    new_anchor = target if mode == "shared" else anchor
    in_place = vcs.run(
        ["log", "-r", f"{session} & {new_anchor}", "--no-graph", "-T", "commit_id", "-n", "1"],
    )
    if in_place.returncode != 0:
        _fail(f"could not check the session bookmark's position: {(in_place.stderr or '').strip()}")
    if not in_place.stdout.strip():
        op_before = _op_log_head(vcs)
        bm_result = vcs.run(["bookmark", "set", session, "-r", new_anchor, "--allow-backwards"])
        ops = _ops_since(vcs, op_before) + ops
        if bm_result.returncode != 0:
            _fail(f"jj bookmark set {session} -r {new_anchor} failed: {(bm_result.stderr or '').strip()}")

    return ops
