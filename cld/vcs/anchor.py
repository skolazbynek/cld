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
persistence across `docker rm && docker run`. A git ticket gets the same
commit, pinned by the hidden ref ``refs/cld/anchor/<session>`` instead of a
bookmark; the ``--reanchor`` helpers below (``find_anchor_commit``,
``move_anchor_commit``, ``revert_ops``) handle both backends.
"""

from pathlib import Path

from cld.log import get_logger
from cld.names import anchor_ref
from cld.vcs import VcsBackend, get_backend
from cld.vcs.git import NO_HOOKS

log = get_logger(__name__)

JJ_DEFAULT_ANCHOR = "trunk()"


def default_anchor_revision(path: Path) -> str:
    """The anchor revision offered when a repo has no ``default_rev``.

    jj keeps ``trunk()``. git has no such revset: it gets the remote HEAD
    branch (``origin/<branch>``) when that resolves, else ``HEAD``. A path
    with no repo keeps ``trunk()``; the resolver reports the real error.
    """
    try:
        vcs = get_backend(path)
    except RuntimeError:
        return JJ_DEFAULT_ANCHOR
    if vcs.name == "jj":
        return JJ_DEFAULT_ANCHOR
    remote_head = vcs.run(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    branch = remote_head.stdout.strip()
    if remote_head.returncode == 0 and branch and vcs.run(
        ["rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}"],
    ).returncode == 0:
        return branch
    return "HEAD"


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


def assert_deliverable_free(vcs: VcsBackend, bookmark: str, anchor_hash: str) -> None:
    """Raise RuntimeError if the bookmark *bookmark* exists and points outside
    ``descendants(anchor_hash)``: a new task-agent would be handed a deliverable
    it can neither move nor own. A missing bookmark passes (the entrypoint
    creates it). jj-only, like the live-anchor overlap check.
    """
    if vcs.name != "jj":
        return
    listed = vcs.run(["bookmark", "list", "-T", 'name ++ "\\n"', f"exact:{bookmark}"])
    if listed.returncode != 0:
        raise RuntimeError(f"could not look up bookmark '{bookmark}': {listed.stderr.strip()}")
    if bookmark not in listed.stdout.splitlines():
        return
    outside = vcs.run([
        "log", "-r", f'bookmarks(exact:"{bookmark}") ~ descendants({anchor_hash})',
        "--no-graph", "-T", "commit_id", "-n", "1",
    ])
    if outside.returncode != 0:
        raise RuntimeError(f"could not check bookmark '{bookmark}': {outside.stderr.strip()}")
    if outside.stdout.strip():
        raise RuntimeError(
            f"the deliverable bookmark '{bookmark}' already exists outside the tree of "
            f"anchor {anchor_hash[:12]}. Pick another -n or --branch, or delete the bookmark."
        )


def _git_rev(vcs: VcsBackend, rev: str) -> str:
    """Commit hash *rev* resolves to, empty when it does not."""
    result = vcs.run(["rev-parse", "--verify", "-q", f"{rev}^{{commit}}"])
    return result.stdout.strip() if result.returncode == 0 else ""


def find_anchor_commit(vcs: VcsBackend, session: str) -> str:
    """Find a ticket's own scratch commit S: its change id on jj, its commit hash on git.

    jj: the commit at or below the *session* bookmark (which sits on the anchor) carrying its own
    ``cld anchor: <session> mode=<mode>`` staging description (the same revset ``cld_recover_anchor``
    and ``_effective_anchor`` use to find it -- see ``imgs/claude-devcontainer/vcs-lib.sh`` and
    ``cld/docker.py``). Raises RuntimeError if zero or more than one commit matches -- either
    means the caller cannot safely identify which commit is S.

    git: the commit ``refs/cld/anchor/<session>`` points at, which must carry that same
    description. Raises RuntimeError when the ref is missing (a ticket from before the scratch commit).
    """
    if vcs.name == "git":
        ref = anchor_ref(session)
        found = _git_rev(vcs, ref)
        if not found:
            raise RuntimeError(
                f"no anchor ref {ref} for session {session!r}; shut the ticket down and start it once to stage one"
            )
        if not vcs.get_description(found).startswith(f"cld anchor: {session} "):
            raise RuntimeError(f"{ref} points at {found[:12]}, which is not a scratch commit of {session!r}")
        return found
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
    already recorded in by ``move_anchor_commit``). On git an entry is
    ``<ref> <old> <new>`` and is undone by ``git update-ref <ref> <old> <new>``,
    which refuses when the ref has moved on meanwhile."""
    for op_id in op_ids:
        if vcs.name == "git":
            ref, old, new = op_id.split()
            result = vcs.run([*NO_HOOKS, "update-ref", ref, old, new])
            if result.returncode != 0:
                raise RuntimeError(f"git update-ref {ref} {old} {new} failed: {(result.stderr or '').strip()}")
            continue
        result = vcs.run(["op", "revert", op_id])
        if result.returncode != 0:
            raise RuntimeError(
                f"jj op revert {op_id} failed: {(result.stderr or '').strip()}"
            )


def _move_anchor_commit_git(vcs: VcsBackend, anchor: str, target: str, session: str) -> list[str]:
    target_commit = _git_rev(vcs, target)
    if not target_commit:
        raise RuntimeError(f"could not resolve the reanchor target {target!r}")
    # The new S would sit inside the tree of the old one, which no longer holds it.
    inside = vcs.run(["merge-base", "--is-ancestor", anchor, target_commit])
    if inside.returncode == 0:
        raise RuntimeError(
            f"the reanchor target {target!r} is the anchor commit {anchor[:12]} or builds on it; "
            "pick a commit outside the ticket's own tree"
        )
    if inside.returncode != 1:
        raise RuntimeError(
            f"could not compare the reanchor target {target!r} with the anchor commit: "
            f"{(inside.stderr or '').strip()}"
        )
    if _git_rev(vcs, f"{anchor}^") == target_commit:
        return []
    changed = vcs.run(["diff", "--quiet", f"{anchor}^", anchor])
    if changed.returncode != 0:
        raise RuntimeError(
            f"anchor commit {anchor[:12]} carries changes (a brief), which cannot be moved on git"
            if changed.returncode == 1
            else f"could not compare anchor commit {anchor[:12]} with its parent: {(changed.stderr or '').strip()}"
        )
    # Host-side commit-tree: no working tree is involved, and S' is a plain child of the target.
    # Host signing settings must not apply to a commit nobody there made.
    created = vcs.run([
        *NO_HOOKS, "-c", "commit.gpgsign=false", "commit-tree", f"{target_commit}^{{tree}}", "-p", target_commit, "-m", vcs.get_description(anchor),
    ])
    if created.returncode != 0:
        raise RuntimeError(f"git commit-tree failed: {(created.stderr or '').strip()}")
    new_anchor = created.stdout.strip()

    refs = [anchor_ref(session)]
    if _git_rev(vcs, f"refs/heads/{session}") == anchor:
        refs.append(f"refs/heads/{session}")
    ops: list[str] = []
    for ref in refs:
        result = vcs.run([*NO_HOOKS, "update-ref", ref, new_anchor, anchor])
        if result.returncode != 0:
            revert_ops(vcs, ops)
            raise RuntimeError(f"git update-ref {ref} failed: {(result.stderr or '').strip()}")
        ops.insert(0, f"{ref} {anchor} {new_anchor}")
    return ops


def move_anchor_commit(
    vcs: VcsBackend, anchor: str, target: str, mode: str, session: str,
) -> list[str]:
    """Move a ticket's scratch commit S onto *target* (design section 3). On jj
    both are change ids, so S keeps its id across the move:

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

    git (*mode* is not used): S is immutable, so a new S' with the same message is
    committed as a plain child of *target*, and the anchor ref is moved onto it
    with a compare-and-swap. The session branch follows only when it still points
    at S; work above S stays on the old S (the caller prints the ``git rebase
    --onto`` that carries it over). S must be empty, and unlike jj the other
    children of *target* are not moved. A *target* that is S or builds on S is refused: jj
    moves S into its own descendants by rewriting them, which git does not do to the agent's
    commits. Undo entries are ``<ref> <old> <new>``.

    Returns the op ids this call created, newest first, so a caller that
    fails on a later repo can revert them with ``revert_ops``. Raises
    RuntimeError -- after reverting its own ops -- on a jj command failure or
    on a new conflict appearing that was not there before.
    """
    if vcs.name == "git":
        return _move_anchor_commit_git(vcs, anchor, target, session)
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
