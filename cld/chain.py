"""In-memory representation of a parsed chain file."""

from __future__ import annotations

import logging
import re
import time
import yaml
from dataclasses import dataclass, replace
from pathlib import Path
from string import Template
from typing import TYPE_CHECKING, Iterator

from cld import names
from cld.run import launch_run
from cld.agent_runtime import wait_for_agent, read_agent_cost, format_duration
from cld.config import Config
from cld.docker import find_repo_root
from cld.log import get_logger
from cld.prompts import compose_brief, resolve_prompt_arg
from cld.vcs import get_backend
from cld.vcs.anchor import assert_descendant, resolve_anchor

if TYPE_CHECKING:
    from cld.chain_state import StateWriter

log = get_logger(__name__)


@dataclass(frozen=True)
class ChainStep:
    name: str
    prompts: tuple[str, ...] = ()
    model: str = ""
    prompt: str = ""
    output: str = ""
    inputs: tuple[str, ...] = ()
    timeout: int = 0


@dataclass(frozen=True)
class ParallelGroup:
    siblings: tuple[ChainStep, ...]


@dataclass(frozen=True)
class ChainDefaults:
    model: str = "sonnet"
    timeout: int = 0


@dataclass(frozen=True)
class Chain:
    name: str
    description: str
    defaults: ChainDefaults
    steps: tuple[ChainStep | ParallelGroup, ...]


def is_parallel(item) -> bool:
    return isinstance(item, ParallelGroup)


def iter_steps(chain: Chain) -> Iterator[ChainStep]:
    for s in chain.steps:
        if isinstance(s, ParallelGroup):
            yield from s.siblings
        else:
            yield s


def load_chain(path: Path) -> Chain:
    """Parse a YAML chain file into a Chain dataclass.

    Performs only structural parsing; semantic validation is in `validate_chain`.
    """
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ValueError(f"Failed to parse {path}: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level must be a YAML mapping")
    return _build_chain(data, path)


def _build_chain(data: dict, path: Path) -> Chain:
    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"{path}: missing required string 'name'")
    description = data.get("description", "") or ""
    defaults_raw = data.get("defaults", {}) or {}
    defaults = ChainDefaults(
        model=defaults_raw.get("model", "sonnet") or "sonnet",
        timeout=int(defaults_raw.get("timeout", 0) or 0),
    )
    steps_raw = data.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise ValueError(f"{path}: 'steps' must be a non-empty list")
    steps = tuple(_build_step_or_group(s, path) for s in steps_raw)
    return Chain(name=name, description=description, defaults=defaults, steps=steps)


def _build_step_or_group(raw, path):
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: each step must be a mapping")
    if "parallel" in raw:
        if set(raw.keys()) - {"parallel"}:
            raise ValueError(f"{path}: parallel step cannot have sibling keys")
        siblings_raw = raw["parallel"]
        if not isinstance(siblings_raw, list) or not siblings_raw:
            raise ValueError(f"{path}: parallel block must be a non-empty list")
        return ParallelGroup(
            siblings=tuple(_build_step(s, path) for s in siblings_raw)
        )
    return _build_step(raw, path)


def _build_step(raw, path) -> ChainStep:
    if "parallel" in raw:
        raise ValueError(f"{path}: nested 'parallel' is not allowed")
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"{path}: step missing required 'name'")
    if "persona" in raw:
        raise ValueError(
            f"{path}: step '{name}' uses 'persona'; it is now 'prompts', an ordered list "
            "of refs (personas and task files interchangeable), e.g. "
            "prompts: [\"@personas/architect\"]. See docs/design-prompt-chaining.md"
        )
    prompts = raw.get("prompts", []) or []
    if isinstance(prompts, str):
        raise ValueError(f"{path}: step '{name}': 'prompts' must be a list of refs, not a string")
    prompt = str(raw.get("prompt", "") or "")
    if not prompts and not prompt:
        raise ValueError(f"{path}: step '{name}' needs at least one of 'prompts' or 'prompt'")
    inputs = raw.get("inputs", []) or []
    return ChainStep(
        name=name,
        prompts=tuple(prompts),
        model=str(raw.get("model", "") or ""),
        prompt=str(raw.get("prompt", "") or ""),
        output=str(raw.get("output", "") or ""),
        inputs=tuple(inputs),
        timeout=int(raw.get("timeout", 0) or 0),
    )


_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def validate_chain(chain: Chain, repo_root: Path, cld_root: Path) -> None:
    """Validate a parsed Chain. Raises ValueError on the first problem.

    Checks:
    - Chain name matches [a-zA-Z0-9_-]+.
    - All step names match the same regex and are unique within the chain.
    - Every prompt ref resolves to a file (uses resolve_prompt_arg).
    - `inputs:` reference only step names that appear EARLIER in the chain
      (no forward references, no parallel siblings referencing each other).
    """
    if not _NAME_RE.match(chain.name):
        raise ValueError(f"chain name '{chain.name}' must match {_NAME_RE.pattern}")

    seen_names: set[str] = set()
    declared_so_far: set[str] = set()

    for item in chain.steps:
        if isinstance(item, ParallelGroup):
            group_names: set[str] = set()
            for s in item.siblings:
                _validate_step_shape(s, seen_names, repo_root, cld_root, declared_so_far)
                if any(i in {sib.name for sib in item.siblings if sib is not s} for i in s.inputs):
                    raise ValueError(
                        f"step '{s.name}': parallel siblings cannot list each other in inputs"
                    )
                seen_names.add(s.name)
                group_names.add(s.name)
            declared_so_far |= group_names
        else:
            _validate_step_shape(item, seen_names, repo_root, cld_root, declared_so_far)
            seen_names.add(item.name)
            declared_so_far.add(item.name)


def _validate_step_shape(s: ChainStep, seen_names, repo_root, cld_root, declared_so_far):
    if not _NAME_RE.match(s.name):
        raise ValueError(f"step name '{s.name}' must match {_NAME_RE.pattern}")
    if s.name in seen_names:
        raise ValueError(f"duplicate step name '{s.name}'")
    for ref in s.prompts:
        try:
            resolve_prompt_arg(ref, repo_root, cld_root)
        except (FileNotFoundError, ValueError) as e:
            raise ValueError(f"step '{s.name}': {e}") from e
    for ref in s.inputs:
        if ref not in declared_so_far:
            raise ValueError(
                f"step '{s.name}' inputs references unknown or future step '{ref}'"
            )


EMBED_THRESHOLD_BYTES = 10_000  # > this → stage as mounted file
CHAIN_OUTPUT_DIR = "chain-outputs"  # relative to repo root (NOT under .cld/, which is gitignored)


def chain_output_path(chain_name: str, step: ChainStep) -> str:
    """Relative path the step's agent will write to."""
    filename = step.output or f"{step.name}.md"
    return f"{CHAIN_OUTPUT_DIR}/{chain_name}/{filename}"


def compose_task(
    *,
    chain: Chain,
    step: ChainStep,
    initial_task: str,
    prior_outputs: list[tuple[str, str]],
    repo_root: Path,
    cld_root: Path,
) -> str:
    """Compose this step's brief: its refs, then its prompt, then what the chain adds.

    The refs come first (they carry the role, as the persona system prompt used to) and
    are composed by ``cld.prompts.compose_brief``; the chain's own sections -- initial
    task, prior step outputs, the output-path footer -- follow. Returned as text: the
    launcher ships it in the anchor scratch, so there is no host-side staging.
    """
    sections = []

    refs = [path for path, _ in (resolve_prompt_arg(r, repo_root, cld_root) for r in step.prompts)]
    if refs:
        sections.append(compose_brief(refs).strip())

    if step.prompt:
        sections.append(step.prompt.strip())

    if initial_task:
        sections.append("# Initial Task\n\n" + initial_task.strip())

    if prior_outputs:
        header = "# Previous Step Output" if len(prior_outputs) == 1 \
                 else "# Previous Step Outputs (parallel)"
        parts = [header]
        for src, body in prior_outputs:
            if len(prior_outputs) > 1:
                parts.append(f"\n### {src}\n")
            parts.append(body.strip() or "_(no text output)_")
        sections.append("\n".join(parts))

    footer_tpl = (cld_root / "cld/prompts/chain-step-footer.md").read_text()
    output_path = chain_output_path(chain.name, step)
    footer = Template(footer_tpl).safe_substitute(OUTPUT_PATH=output_path)
    sections.append(footer)

    return "\n\n".join(sections) + "\n"


def apply_name_override(chain: Chain, override: str) -> Chain:
    """Return a copy of *chain* whose `name` is replaced by *override*.

    Used to honour `cld chain run -n <name>`: the user-provided name takes
    over chain.name so it flows into branches, sessions, workspaces, state
    dirs, output paths and report banners uniformly.
    """
    if not override:
        raise ValueError("a chain name is required: pass -n/--name <chain>")
    if not _NAME_RE.match(override):
        raise ValueError(
            f"chain name override '{override}' must match {_NAME_RE.pattern}"
        )
    return replace(chain, name=override)


def chain_branch(chain: Chain) -> str:
    return names.chain_id(chain.name)


def step_session(chain: Chain, step: ChainStep) -> str:
    """Session/branch name for an individual step's agent."""
    return names.step_id(chain.name, step.name)


def _anchor_ref(vcs, anchor_commit: str) -> str:
    """Return a descent-check reference for *anchor_commit* that survives WC rewrites.

    For jj, return ``change_id(<A's change_id>)`` -- a revset expression that
    selects every commit belonging to A's change (current visible + hidden +
    divergent siblings). This is stable across working-copy snapshots that
    rewrite A's commit_id and robust against transient divergence: since jj
    auto-rebases descendants when a change is rewritten, a step tip anchored
    on any commit of A's change ends up with some commit of A's change in its
    ancestry. For git, HEAD identity is already commit-based.
    """
    if vcs.name != "jj":
        return anchor_commit
    result = vcs.run(["log", "-r", anchor_commit, "--no-graph", "-T", "change_id", "-n", "1"])
    if result.returncode == 0 and result.stdout.strip():
        return f"change_id({result.stdout.strip()})"
    return anchor_commit


def initialise_chain_branch(chain: Chain, vcs, anchor_hash: str) -> str:
    """Create the persistent chain branch as a bookmark pointing at the anchor.

    The chain "branch" is a jj bookmark that tracks the current chain tip; it
    starts pointing at *anchor_hash* (B) and gets advanced per successful step.
    Individual step agents run in their own containers with per-step anchors
    layered on top of the chain branch's current tip.
    """
    branch = chain_branch(chain)
    vcs.set_branch(branch, anchor_hash)
    return branch


def advance_chain_branch(
    chain: Chain,
    vcs,
    successful_session: str,
    transient_sessions: list[str],
    anchor_hash: str,
) -> None:
    """Advance the chain branch to a step's tip, then delete the transients.

    successful_session: the session whose tip becomes the new chain head
      (for parallel groups, this is the first sibling).
    transient_sessions: all per-step session branches to clean up afterwards
      (includes successful_session itself).
    """
    branch = chain_branch(chain)
    assert_descendant(vcs, anchor_hash, successful_session)
    vcs.set_branch(branch, successful_session)
    for s in transient_sessions:
        try:
            vcs.delete_branch(s)
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass


@dataclass(frozen=True)
class StepResult:
    step_name: str
    session: str
    status: str            # "success", "failed", "timeout", "no_changes", "unknown"
    output_text: str       # raw contents of declared output file (may be "")
    failure_md: str        # contents of AGENT-FAILURE.md if present, else ""
    summary: dict
    cost_usd: float        # from result.json, 0.0 if not present
    duration_seconds: float


def execute_step(
    *,
    cfg: Config,
    vcs,
    chain: Chain,
    step: ChainStep,
    session_name: str,
    initial_task: str,
    prior_outputs: list[tuple[str, str]],
    repo_root: Path,
    cld_root: Path,
) -> StepResult:
    """Launch one agent for one step, wait for completion, return its result."""
    brief = compose_task(
        chain=chain, step=step,
        initial_task=initial_task,
        prior_outputs=prior_outputs,
        repo_root=repo_root, cld_root=cld_root,
    )
    if log.isEnabledFor(logging.DEBUG):
        log.debug("composed brief: %s", brief[:500])
    model = step.model or chain.defaults.model or cfg.chain_default_model or ""
    revision = chain_branch(chain)
    start = time.monotonic()
    launch_run(
        cfg, brief,
        model=model,
        revision=revision,
        session_name=session_name,
        quiet=True,
    )
    summary = wait_for_agent(session_name, vcs, cfg)
    output_path = chain_output_path(chain.name, step)
    output_text = vcs.file_show(session_name, output_path) or ""
    failure_md = vcs.file_show(
        session_name, f"agent-output-{session_name}/AGENT-FAILURE.md"
    ) or ""
    cost = read_agent_cost(session_name, vcs) or 0.0
    return StepResult(
        step_name=step.name,
        session=session_name,
        status=summary.get("status", "unknown"),
        output_text=output_text,
        failure_md=failure_md,
        summary=summary,
        cost_usd=cost,
        duration_seconds=time.monotonic() - start,
    )


@dataclass(frozen=True)
class ChainResult:
    chain_name: str
    chain_branch: str
    steps: tuple[StepResult, ...]
    success: bool
    total_cost_usd: float
    total_duration_seconds: float
    failure_reason: str    # "" when success
    anchor_hash: str = ""


def chain_state_dir(repo_root: Path, chain_name: str) -> Path:
    return repo_root / ".cld" / "chains" / names.chain_id(chain_name)


def run_chain(
    cfg: Config,
    chain_file: Path,
    *,
    initial_task: str = "",
    revision: str = "",
    name_suffix: str = "",
    state_writer: "StateWriter | None" = None,
    anchor_hash: str = "",
) -> ChainResult:
    # Chain orchestration writes host-side scratch into the target repo's
    # working copy and stages a chain-level anchor there. Neither is possible
    # from inside a master container (own repo lives in a jj-secondary
    # workspace; sibling repos aren't visible to master at all). Block until
    # the chain-runner-in-peer refactor lands. See
    # docs/design-master-sibling-launch.md.
    from cld.docker import in_master_container
    if in_master_container():
        raise RuntimeError(
            "`cld chain run` is not yet supported from inside a master container. "
            "Run chains from the host, or hand bounded pieces of work to "
            "`cld task-agent start`."
        )

    repo_root = find_repo_root()
    cld_root = Path(__file__).resolve().parent.parent
    vcs = get_backend()

    chain = load_chain(chain_file)
    validate_chain(chain, repo_root, cld_root)
    chain = apply_name_override(chain, name_suffix)
    log.info("Chain '%s' starting with %d step(s)", chain.name, len(chain.steps))
    log.debug("run_chain: name=%s file=%s steps=%d", chain.name, chain_file, len(chain.steps))

    anchor = anchor_hash or resolve_anchor(vcs, revision)
    # Chain-level anchor == A. Each step's peer container stages its own child
    # commit B on top of the current chain-branch tip inside its ephemeral
    # workspace; assert_descendant(vcs, A, step_tip) still holds because every
    # per-step B descends from A.
    # For jj, resolve A's change_id and use it (not the commit_id) as the
    # descendant-check anchor: change_id is stable across working-copy rewrites
    # (e.g. host-side snapshot churn) whereas the commit_id may
    # become hidden and lookup as-an-ancestor fails.
    anchor_ref = _anchor_ref(vcs, anchor)
    initialise_chain_branch(chain, vcs, anchor)
    log.info("Chain anchor: %s", anchor[:12])

    if state_writer is not None:
        state_writer.set_anchor(anchor)

    initial_text = initial_task

    results: list[StepResult] = []
    total_start = time.monotonic()
    failure_reason = ""

    try:
        for i, item in enumerate(chain.steps):
            if isinstance(item, ParallelGroup):
                log.info("Parallel group %d: launching %d siblings", i, len(item.siblings))
                log.debug("parallel group (%d siblings) launching", len(item.siblings))
                if state_writer is not None:
                    sibling_names = ",".join(s.name for s in item.siblings)
                    sibling_sessions = [step_session(chain, s) for s in item.siblings]
                    state_writer.mark_step_start(i, "parallel", sibling_names, sibling_sessions)
                prior_outputs = _gather_prior_outputs(chain, item, results, i)
                step_initial = initial_text or None
                group_results = _run_parallel(
                    cfg=cfg, vcs=vcs, chain=chain, group=item,
                    prior_outputs=prior_outputs,
                    initial_task=step_initial,
                    repo_root=repo_root, cld_root=cld_root,
                )
                results.extend(group_results)
                failures = [r for r in group_results if r.status not in ("success", "no_changes", "unknown")]
                if state_writer is not None:
                    group_status = "failed" if failures else "success"
                    group_cost = sum(r.cost_usd for r in group_results)
                    group_dur = sum(r.duration_seconds for r in group_results)
                    state_writer.mark_step_done(sibling_names, group_status, group_dur, group_cost)
                if failures:
                    failure_reason = (
                        f"parallel group {i} had {len(failures)} failed siblings: "
                        + ", ".join(r.step_name for r in failures)
                    )
                    break
                first_success = next(
                    (r for r in group_results if r.status in ("success", "no_changes", "unknown")),
                    group_results[0],
                )
                advance_chain_branch(
                    chain, vcs,
                    successful_session=first_success.session,
                    transient_sessions=[r.session for r in group_results],
                    anchor_hash=anchor_ref,
                )
                continue
            step = item
            session = step_session(chain, step)
            model_eff = step.model or chain.defaults.model or cfg.chain_default_model or ""
            log.info(
                "Step %d/%d '%s' launching (prompts=%s, model=%s)",
                i + 1, len(chain.steps), step.name, ",".join(step.prompts) or "-", model_eff,
            )
            log.debug(
                "step %d/%d '%s' launching (prompts=%s, model=%s, revision=%s, session=%s)",
                i + 1, len(chain.steps), step.name, ",".join(step.prompts) or "-", model_eff,
                chain_branch(chain), session,
            )
            if state_writer is not None:
                state_writer.mark_step_start(i, "step", step.name, [session])
            prior_outputs = _gather_prior_outputs(chain, step, results, i)
            step_initial = initial_text or None

            result = execute_step(
                cfg=cfg, vcs=vcs, chain=chain, step=step,
                session_name=session,
                initial_task=step_initial,
                prior_outputs=prior_outputs,
                repo_root=repo_root,
                cld_root=cld_root,
            )
            results.append(result)
            log.info(
                "Step '%s' done: status=%s, cost=$%.4f, duration=%.1fs",
                step.name, result.status, result.cost_usd, result.duration_seconds,
            )
            log.debug(
                "step '%s' completed status=%s cost=$%.4f duration=%.1fs output_bytes=%d",
                step.name, result.status, result.cost_usd, result.duration_seconds,
                len(result.output_text),
            )
            if state_writer is not None:
                state_writer.mark_step_done(
                    step.name, result.status,
                    result.duration_seconds, result.cost_usd,
                )

            _TERMINAL_FAILURES = {"failed", "commit_failed", "timeout"}
            if result.status in _TERMINAL_FAILURES:
                first_line = (result.failure_md.splitlines() or [""])[0][:120]
                failure_reason = f"step '{step.name}' {result.status}"
                if first_line:
                    failure_reason += f": {first_line}"
                log.error("Step '%s' failed: %s", step.name, result.status)
                if first_line:
                    log.error("%s", first_line)
                break
            if result.status not in {"success", "no_changes", "unknown"}:
                log.warning(
                    "Step '%s' has unrecognised status %r; treating as success",
                    step.name, result.status,
                )

            advance_chain_branch(
                chain, vcs, successful_session=session,
                transient_sessions=[session],
                anchor_hash=anchor_ref,
            )
            log.debug("chain branch advanced to %s", session)
    except KeyboardInterrupt:
        failure_reason = "interrupted"

    total_cost = sum(r.cost_usd for r in results)
    total_dur = time.monotonic() - total_start
    _OK_STATUSES = {"success", "no_changes", "unknown"}
    success = not failure_reason and all(r.status in _OK_STATUSES for r in results)
    return ChainResult(
        chain_name=chain.name,
        chain_branch=chain_branch(chain),
        steps=tuple(results),
        success=success,
        total_cost_usd=total_cost,
        total_duration_seconds=total_dur,
        failure_reason=failure_reason,
        anchor_hash=anchor,
    )


def print_chain_report(result: ChainResult, vcs) -> None:
    print()
    print("=" * 60)
    header = "Chain completed" if result.success else "Chain stopped"
    print(f"{header}: {result.chain_name} "
          f"({len(result.steps)} step{'s' if len(result.steps) != 1 else ''})")
    if result.failure_reason:
        print(f"Reason: {result.failure_reason}")
    print("=" * 60)
    print()

    name_w = max((len(s.step_name) for s in result.steps), default=8)
    sess_w = max((len(s.session) for s in result.steps), default=12)
    for s in result.steps:
        glyph = "✓" if s.status == "success" else "✗"
        print(
            f"  {glyph} {s.step_name:<{name_w}}  "
            f"{s.session:<{sess_w}}  "
            f"{format_duration(s.duration_seconds):>7}  "
            f"${s.cost_usd:>6.4f}  "
            f"status={s.status}"
            f"  output_bytes={len(s.output_text)}"
        )
        if s.failure_md:
            for line in s.failure_md.strip().splitlines()[:5]:
                print(f"      | {line}")
            if len(s.failure_md.splitlines()) > 5:
                print("      | ... (see branch for full failure)")

    print()
    print(f"  Anchor:   {result.anchor_hash[:12]}")
    print(f"  Branch:   {result.chain_branch}")
    print(f"  Total:    {format_duration(result.total_duration_seconds)}, "
          f"${result.total_cost_usd:.4f}")
    if vcs.name == "jj":
        print(f"  Inspect:  jj log -r '{result.anchor_hash}..{result.chain_branch}'")
        print(f"  Diff:     jj diff --from {result.anchor_hash} --to {result.chain_branch}")
        print(f"  Merge:    jj squash --from {result.chain_branch}")
    else:
        print(f"  Inspect:  git log {result.anchor_hash}..{result.chain_branch}")
        print(f"  Diff:     git diff {result.anchor_hash}..{result.chain_branch}")
        print(f"  Merge:    git merge {result.chain_branch}")
    print()



def _run_parallel(
    *,
    cfg: Config,
    vcs,
    chain: Chain,
    group: ParallelGroup,
    prior_outputs: list[tuple[str, str]],
    initial_task: str | None,
    repo_root: Path,
    cld_root: Path,
) -> list[StepResult]:
    """Launch all siblings (serialized launch), wait concurrently, return results."""
    sessions: list[tuple[ChainStep, str]] = []
    for sibling in group.siblings:
        session = step_session(chain, sibling)
        brief = compose_task(
            chain=chain, step=sibling,
            initial_task=initial_task,
            prior_outputs=prior_outputs,
            repo_root=repo_root, cld_root=cld_root,
        )
        model = sibling.model or chain.defaults.model or cfg.chain_default_model or ""
        log.debug("parallel sibling '%s' launching session=%s", sibling.name, session)
        launch_run(
            cfg, brief,
            model=model,
            revision=chain_branch(chain),
            session_name=session,
            quiet=True,
        )
        sessions.append((sibling, session))

    results: list[StepResult] = []
    for sibling, session in sessions:
        start = time.monotonic()
        summary = wait_for_agent(session, vcs, cfg)
        output_path = chain_output_path(chain.name, sibling)
        output_text = vcs.file_show(session, output_path) or ""
        failure_md = vcs.file_show(
            session, f"agent-output-{session}/AGENT-FAILURE.md"
        ) or ""
        cost = read_agent_cost(session, vcs) or 0.0
        results.append(StepResult(
            step_name=sibling.name,
            session=session,
            status=summary.get("status", "unknown"),
            output_text=output_text,
            summary=summary,
            cost_usd=cost,
            duration_seconds=time.monotonic() - start,
            failure_md=failure_md,
        ))
    return results


def _gather_prior_outputs(
    chain: Chain,
    step: ChainStep | ParallelGroup,
    results: list[StepResult],
    current_idx: int,
) -> list[tuple[str, str]]:
    """Resolve prior outputs for *step* (or the current *group*).

    - If step.inputs is set, use those step names (must exist in `results`).
    - Else, use the immediately previous chain entry. If it was a parallel
      group (T-017), concatenate all its siblings' outputs.
    - ParallelGroup has no inputs field; falls through to the default path.
    """
    inputs = getattr(step, "inputs", ())
    if inputs:
        return [
            (r.step_name, r.output_text)
            for r in results if r.step_name in inputs
        ]
    if current_idx == 0:
        return []
    prev_item = chain.steps[current_idx - 1]
    if isinstance(prev_item, ParallelGroup):
        sib_names = {s.name for s in prev_item.siblings}
        return [(r.step_name, r.output_text)
                for r in results if r.step_name in sib_names]
    # Previous was a single step.
    return [(results[-1].step_name, results[-1].output_text)]
