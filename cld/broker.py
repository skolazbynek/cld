"""The cld broker seam: local docker on the host, SSH to the host broker in a container.

On the host, cld talks to the Docker daemon directly. Inside a container there is no
docker socket (removed by design -- see docs/design-cld-broker.md); the only host
channel is the broker: an sshd whose ForceCommand is `broker/cld-broker.py`, reached
here by building the ssh argv ourselves. This module centralizes that host-vs-broker
decision so call sites stay backend-agnostic.

The set of functions here is deliberately the entire surface reachable from inside a
container -- each maps 1:1 to an `action_<name>` in the broker script. Anything not
exposed here simply cannot be done from a container, by construction.
"""

import base64
import os
import subprocess
from pathlib import Path

from cld.docker import in_master_container
from cld.log import get_logger
from cld.manifest import TicketManifest

log = get_logger(__name__)

# Where stage_broker (cld/docker.py) mounts the restricted key and the pinned
# known_hosts. Their presence, with an endpoint, is the availability signal.
KEY_MOUNT = "/run/secrets/broker-key"
KNOWN_HOSTS_MOUNT = "/run/secrets/broker-known-hosts"

# Login user when `broker_endpoint` carries no `user@`.
_DEFAULT_USER = "zet"

# Actions that act on the caller's revision, which the broker reads from the
# jj store without snapshotting (broker/cld-broker.py:LabelCaller.revision).
_REVISION_ACTIONS = ("run-tests", "graphql", "run", "chain")


def broker_available() -> bool:
    """True when this container has an endpoint, the restricted key AND the known_hosts.

    All three, because ``run_action`` always ssh's with ``StrictHostKeyChecking=yes``
    against ``KNOWN_HOSTS_MOUNT``: without that mount the connection can only fail on
    the host-key check. ``stage_broker`` merely warns host-side when
    ``broker_known_hosts`` is unset or missing, so counting it here is what turns a raw
    ssh error into the callers' actionable "set broker_key (and broker_known_hosts)".
    """
    return (
        bool(os.environ.get("CLD_BROKER_ENDPOINT"))
        and Path(KEY_MOUNT).exists()
        and Path(KNOWN_HOSTS_MOUNT).exists()
    )


def _endpoint() -> tuple[str, str, str]:
    """Split ``CLD_BROKER_ENDPOINT`` ([user@]host:port) into (user, host, port)."""
    endpoint = os.environ.get("CLD_BROKER_ENDPOINT", "")
    user, sep, hostport = endpoint.rpartition("@")
    host, _, port = hostport.partition(":")
    return (user if sep else _DEFAULT_USER), host, (port or "2222")


# Bounds the pre-call snapshot: a stuck jj lock must not hang every broker call.
_SNAPSHOT_TIMEOUT = 30


def _workspaces() -> list[str]:
    """This container's repo workspaces: one per mounted repo for a ticket, else the v1 one."""
    if raw := os.environ.get("CLD_TICKET_MANIFEST"):
        manifest = TicketManifest.from_json(raw)
        return [f"/workspace/{manifest.ticket}/{repo.name}" for repo in manifest.repos]
    return ["/workspace/current"]


def snapshot_workspaces() -> None:
    """Snapshot every repo workspace, so the broker sees edits made a moment ago.

    Watchman snapshots on its own, but only after the edit settles. Best effort:
    a git repo, an unreadable manifest or a failed snapshot leaves the broker
    reading the last snapshot.
    """
    try:
        workspaces = _workspaces()
    except (ValueError, KeyError, RuntimeError) as e:
        log.warning("not snapshotting before the broker call: %s", e)
        return
    for path in workspaces:
        try:
            subprocess.run(["jj", "-R", path, "status"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=_SNAPSHOT_TIMEOUT)
        except subprocess.TimeoutExpired:
            log.warning("snapshot of %s timed out; the broker reads its last snapshot", path)
        except OSError:
            return


def run_action(action: str, *args: str, capture: bool = False) -> subprocess.CompletedProcess:
    """Run *action* on the host broker with *args*, over ssh.

    The wire format is `<action> <session> <base64-argv>`; the argv is
    base64(NUL-joined) so it can only ever become arguments to the action's command,
    never a host command (the broker never evals it). With ``capture=False``
    stdout/stderr are inherited so the broker's output streams straight to the user
    (lifecycle ops); with ``capture=True`` stdout is captured for parsing.
    """
    if not broker_available():
        log.warning(
            "the cld broker is not configured for this container, so '%s' cannot "
            "reach the host.", action,
        )
        return subprocess.CompletedProcess(
            [], 127, stdout="", stderr="the cld broker is not configured for this container",
        )

    if action in _REVISION_ACTIONS:
        snapshot_workspaces()
    user, host, port = _endpoint()
    payload = base64.b64encode(b"".join(a.encode() + b"\0" for a in args)).decode("ascii")
    session = os.environ.get("SESSION_NAME", "")
    cmd = [
        "ssh", "-i", KEY_MOUNT,
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS_MOUNT}",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "IdentitiesOnly=yes",
        "-p", port, f"{user}@{host}",
        "--", f"{action} {session} {payload}",
    ]
    log.debug("broker: %s %s (%d args)", action, session, len(args))
    # No action reads stdin, and inside the graphql MCP server stdin carries its
    # protocol messages: an inheriting ssh would swallow them.
    return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=capture, text=True)


def _parse_container_line(line: str) -> dict | None:
    """Parse one tab-separated `list-containers` broker line into a record.

    Line shape: ``name<TAB>kind<TAB>repo<TAB>raw-status`` (raw-status is the
    ``docker ps`` Status column, e.g. "Up 3 minutes"). Mirrors the mapping the
    local-docker path applies so both backends return identical records.
    """
    parts = line.split("\t")
    if len(parts) < 4:
        return None
    name, kind, repo, raw_status = parts[0], parts[1], parts[2], parts[3]
    if not name:
        return None
    return {
        "name": name,
        "kind": kind,
        "repo": repo,
        "status": "running" if raw_status.lower().startswith("up") else "stopped",
    }


def _list_via_local_docker(kind: str | None) -> list[dict]:
    """Enumerate cld containers with the local Docker daemon (host path)."""
    filters = ["--filter", f"label=org.cld.kind={kind}"] if kind else ["--filter", "label=org.cld.kind"]
    result = subprocess.run(
        ["docker", "ps", "-a", *filters, "--format", "{{.Names}}\t{{.Status}}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.warning("docker ps failed: %s", result.stderr.strip())
        return []

    containers = []
    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        name, _, status = line.partition("\t")
        inspect = subprocess.run(
            ["docker", "inspect", name, "--format",
             '{{index .Config.Labels "org.cld.kind"}}|{{index .Config.Labels "org.cld.repo-root"}}'],
            capture_output=True, text=True,
        )
        if inspect.returncode != 0:
            continue
        parts = inspect.stdout.strip().split("|", 1)
        containers.append({
            "name": name,
            "kind": parts[0] if parts else "",
            "repo": parts[1] if len(parts) > 1 else "",
            "status": "running" if status.lower().startswith("up") else "stopped",
        })
    return containers


def list_cld_containers(kind: str | None = None) -> list[dict]:
    """Enumerate cld containers, returning ``{name, kind, repo, status}`` records.

    Host: reads the local daemon. Inside a container: the broker's
    ``list-containers`` action. *kind* filters to one ``org.cld.kind`` label value:
    ``"agent"``, ``"task-agent"`` or ``"ticket"``.

    The in-container branch keys on ``in_master_container()``, whose env has no
    producer left; it stays until that dispatch is reworked (cld/docker.py).
    """
    if not in_master_container():
        return _list_via_local_docker(kind)

    result = run_action("list-containers", *([kind] if kind else []), capture=True)
    if result.returncode != 0:
        log.warning("broker list-containers failed: %s", (result.stderr or "").strip())
        return []
    records = []
    for line in result.stdout.strip().splitlines():
        rec = _parse_container_line(line)
        if rec:
            records.append(rec)
    return records


def graphql_op(
    op: str, *args: str, capture: bool = True, repo: str = "",
) -> subprocess.CompletedProcess:
    """Delegate a `graphql <op>` to the host broker.

    Backs every tool in cld/mcp/graphql.py: the broker owns the server's
    lifecycle and attaches credentials to queries the container never sees
    (docs/impl-graphql-broker-plan.md). *op* is one of
    start/stop/restart/status/logs/query/introspect/endpoints. *repo* names
    the target repo of a multi-repo ticket container, sent as a leading
    ``--repo <name>`` the broker validates against the caller's manifest
    labels (design-ticket-containers.md section 6.1); empty means the
    caller's single repo, which is all a v1 kind has. Defaults to
    ``capture=True`` (unlike ``broker_task_agent_op``, which streams to the
    user) because every caller here is an MCP tool that
    needs the broker's stdout back as a return value, not a human watching a
    terminal.
    """
    repo_args = ("--repo", repo) if repo else ()
    return run_action("graphql", *repo_args, op, *args, capture=capture)


def broker_task_agent_op(op: str, extra_args: list[str] | None = None) -> int:
    """Delegate a `cld task-agent <op>` to the host broker.

    The broker's task-agent action enforces the argv rules a container must not
    be able to bypass -- it denies ``--force`` and a caller-supplied
    ``--parent``, and stamps the calling session as the parent itself (see
    broker/cld-broker.py). ``start`` names its repo with a leading ``--repo
    <name>`` in *extra_args*, which the broker resolves against the caller's
    mounts, then the host registry; the other ops need no repo.
    """
    return run_action("task-agent", op, *(extra_args or [])).returncode


def _broker_job_op(
    action: str, op: str, args: tuple[str, ...], repo: str, capture: bool,
) -> subprocess.CompletedProcess:
    repo_args = ("--repo", repo) if repo else ()
    return run_action(action, *repo_args, op, *args, capture=capture)


def broker_run_op(
    op: str, *args: str, repo: str = "", capture: bool = False,
) -> subprocess.CompletedProcess:
    """Delegate a `cld run <op>` (start|status|cancel) to the host broker.

    *repo* names the target repo of a multi-repo ticket container as a leading
    ``--repo <name>``, before *op*, the way ``graphql_op`` sends it. Output streams to
    the user unless ``capture=True``, which ``wait`` needs to read the state.
    """
    return _broker_job_op("run", op, args, repo, capture)


def broker_chain_op(
    op: str, *args: str, repo: str = "", capture: bool = False,
) -> subprocess.CompletedProcess:
    """Delegate a `cld chain <op>` (start|status|cancel) to the host broker, like ``broker_run_op``."""
    return _broker_job_op("chain", op, args, repo, capture)


def list_registered_repos() -> list[tuple[str, str, bool]]:
    """The host repo registry as ``(name, path, mounted here)``, via the broker's ``list-repos``.

    Empty when the broker is unreachable or fails: the registry is a hint for
    picking ``task-agent start --repo``, never needed to work on the mounted repos.
    """
    result = run_action("list-repos", capture=True)
    if result.returncode != 0:
        log.warning("broker list-repos failed: %s", (result.stderr or "").strip())
        return []
    return [(name, path, mounted == "yes")
            for name, path, mounted in (line.split("\t") for line in result.stdout.splitlines())]
