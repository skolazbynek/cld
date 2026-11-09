"""One-shot Claude agent (`cld run`) launch logic."""

import secrets
import subprocess
import sys
from pathlib import Path

from cld import names
from cld.config import Config
from cld.docker import (
    anchor_env_args,
    base_extra_paths,
    build_container_args,
    ensure_image,
    find_target_repo,
    in_master_container,
    prepare_store_mounts,
    require_docker,
    run_extra_paths,
)
from cld.log import get_logger
from cld.vcs import get_backend
from cld.vcs.anchor import resolve_anchor

log = get_logger(__name__)


def launch_run(
    cfg: Config,
    brief: str,
    name: str = "",
    model: str = "",
    revision: str = "",
    session_name: str | None = None,
    quiet: bool = False,
    shared_anchor: bool = False,
    *,
    extra_env: dict[str, str] | None = None,
) -> dict:
    """Launch a one-shot autonomous Claude agent in a Docker container.

    *brief* is the already-composed prompt (see ``cld.prompts.compose_brief``); it
    ships inside the anchor scratch envelope and the entrypoint reads it from
    ``.cld-run/brief.md``.

    The container entrypoint stages scratch commit B inside its own ephemeral
    workspace after ``jj workspace add -r <A>``; ``anchor_env_args`` carries the
    resolved base revision + that envelope. The final ``AGENT_ANCHOR_HASH``
    (== A, B's parent) is computed peer-side and surfaces in the agent's
    ``summary.json``.
    """
    if in_master_container():
        log.error(
            "`cld run` is not supported from inside a container "
            "(no docker daemon). Run it on the host, or hand the work to a "
            "`cld task-agent`, whose lifecycle goes through the host broker."
        )
        sys.exit(1)
    require_docker()
    if not brief.strip():
        log.error("No prompt refs or inline prompt provided")
        sys.exit(1)

    repo_root = find_target_repo(cfg)
    # Before any host jj call on the repo (the anchor resolution below).
    prepare_store_mounts(repo_root)

    cld_root = Path(__file__).resolve().parent.parent
    ensure_image(
        cfg.run_image,
        cld_root / "imgs/claude-run/Dockerfile.claude-run",
        cld_root / "imgs/claude-run",
        extra_paths=run_extra_paths(cld_root),
        parent_image=(
            cfg.base_image,
            cld_root / "imgs/claude-base/Dockerfile.claude-base",
            cld_root,
            base_extra_paths(cld_root),
        ),
        quiet=quiet,
    )

    session = session_name or names.run_id(name or secrets.token_hex(3))
    mode = "shared" if shared_anchor else "isolated"
    # No overlap check: a run may start inside a live container's tree (a ticket
    # launches runs on its own work through the broker).
    anchor = resolve_anchor(get_backend(repo_root), revision)

    args = ["--name", session]
    args += build_container_args(repo_root, session, cfg, anchor_hash=anchor, anchor_mode=mode)
    # The brief travels in the anchor scratch envelope (-> .cld-run/brief.md); no
    # prompt mounts, no host temp file to outlive this detached launch.
    args += anchor_env_args(cfg, anchor, brief=brief, mode=mode)
    if model:
        args += ["-e", f"AGENT_MODEL={model}"]
    if extra_env:
        for k, v in extra_env.items():
            args += ["-e", f"{k}={v}"]

    if not quiet:
        log.info("Starting agent in background...")
        log.info("Brief: %d chars", len(brief))
        log.info(f"Repository: {repo_root}")
        print()

    container_id = subprocess.run(
        ["docker", "run", "--detach"] + args + [cfg.run_image],
        capture_output=True, text=True,
    )

    if container_id.returncode != 0:
        log.error(f"Failed to start container: {container_id.stderr.strip()}")
        sys.exit(1)

    cid = container_id.stdout.strip()

    if not quiet:
        print(f"Container ID: {cid}")
        print()
        print("========================================")
        print("Agent started successfully")
        print("========================================")
        print()
        print(f"Check if running:\n  docker ps --filter id={cid}")
        print(f"\nWait for completion:\n  docker wait {cid}")
        # The anchor commit is created inside the peer container's workspace,
        # so its hash is only known once the agent's summary.json is written.
        print(f"\nInspect on completion:\n  jj log -r {session}   # or: git log {session}")
        print()

    return {
        "container_id": cid,
        "session_name": session,
        "repo_root": str(repo_root),
    }
