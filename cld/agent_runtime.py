"""Shared agent-lifecycle helpers (wait, cost, formatting)."""

import calendar
import json
import subprocess
import time

from cld import names
from cld.config import Config
from cld.log import get_logger
from cld.vcs import VcsBackend

log = get_logger(__name__)


def wait_for_agent(session_name: str, vcs: VcsBackend, cfg: Config) -> dict:
    log.info(
        "Waiting for agent %s (timeout=%ds, poll=%ds)",
        session_name, cfg.agent_timeout, cfg.poll_interval,
    )
    start = time.monotonic()
    while time.monotonic() - start < cfg.agent_timeout:
        log.debug("polling %s (elapsed=%.0fs)", session_name, time.monotonic() - start)
        result = subprocess.run(
            ["docker", "ps", "--filter", names.docker_name_filter(session_name), "--format", "{{.Status}}"],
            capture_output=True, text=True,
        )
        if not result.stdout.strip():
            break
        time.sleep(cfg.poll_interval)
    else:
        log.warning(
            "Agent %s timed out after %ds; stopping container",
            session_name, cfg.agent_timeout,
        )
        subprocess.run(["docker", "stop", session_name], capture_output=True, text=True)
        return {"status": "timeout", "session_name": session_name}

    summary_raw = vcs.file_show(
        session_name, f"agent-output-{session_name}/summary.json",
    )
    if not summary_raw:
        log.warning("Agent %s: no summary.json found", session_name)
        return {"status": "unknown", "error": "No summary.json found"}
    try:
        summary = json.loads(summary_raw)
    except json.JSONDecodeError:
        log.warning("Agent %s: invalid summary.json", session_name)
        return {"status": "unknown", "error": "Invalid summary.json"}
    log.info("Agent %s completed: status=%s", session_name, summary.get("status"))
    return summary


def read_agent_cost(session: str, vcs: VcsBackend) -> float | None:
    raw = vcs.file_show(session, f"agent-output-{session}/result.json")
    if not raw:
        log.debug("read cost for %s: unavailable", session)
        return None
    try:
        data = json.loads(raw)
        cost = data.get("cost_usd")
        if cost is None:
            log.debug("read cost for %s: unavailable", session)
            return None
        value = float(cost)
        log.debug("read cost for %s: $%.4f", session, value)
        return value
    except (json.JSONDecodeError, ValueError):
        log.debug("read cost for %s: unavailable", session)
        return None


def format_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s"


def format_age(iso_ts: str) -> str:
    try:
        # Mailbox timestamps carry microseconds; chain state's don't.
        whole = iso_ts.split(".", 1)[0].rstrip("Z") + "Z"
        t = time.strptime(whole, "%Y-%m-%dT%H:%M:%SZ")
        then = calendar.timegm(t)
        secs = int(time.time()) - then
    except Exception:
        return iso_ts
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"
