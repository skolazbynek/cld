"""Container-side CLI: the `cld` the devcontainer image installs.

Only verbs that work inside a container, each wired straight to the seam it uses
-- the host broker for anything needing a docker daemon, the bind-mounted mailbox
tree for anything needing a conversation. Host-only verbs are hidden stubs that
say so instead of failing obscurely. See docs/design-cli-split.md.
"""

import os
import subprocess
import time
from pathlib import Path
from typing import Literal, Optional

import typer

from cld.cli_msg import handle_errors as _handle_errors, msg_app
from cld.config import Config
from cld.broker import (
    broker_available, broker_chain_op, broker_run_op, broker_task_agent_op, list_registered_repos, run_action,
)
from cld.log import get_logger, setup_logging
from cld.manifest import TicketManifest
from cld.prompts import list_prompt_items
from cld.task_agent import print_task_agent_transcript, resolve_task_agent

log = get_logger(__name__)

_ANY_ARGS = {"allow_extra_args": True, "ignore_unknown_options": True}

app = typer.Typer(context_settings=_ANY_ARGS)


def _host_only(verb: str) -> None:
    typer.echo(f"host-only: run `{verb}` on the host", err=True)
    raise typer.Exit(2)


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context):
    """cld inside a container: task-agents, mailbox messaging, the broker."""
    if ctx.invoked_subcommand is None:
        _host_only("cld")


@app.command("build", hidden=True, context_settings=_ANY_ARGS)
def build_stub(ctx: typer.Context):
    _host_only("cld build")


# Ticket lifecycle verbs (v2) are host-only: they drive the docker daemon.


@app.command("start", hidden=True, context_settings=_ANY_ARGS)
def start_stub(ctx: typer.Context):
    _host_only("cld start")


@app.command("claude", hidden=True, context_settings=_ANY_ARGS)
def claude_stub(ctx: typer.Context):
    _host_only("cld claude")


@app.command("shell", hidden=True, context_settings=_ANY_ARGS)
def shell_stub(ctx: typer.Context):
    _host_only("cld shell")


@app.command("stop", hidden=True, context_settings=_ANY_ARGS)
def stop_stub(ctx: typer.Context):
    _host_only("cld stop")


@app.command("restart", hidden=True, context_settings=_ANY_ARGS)
def restart_stub(ctx: typer.Context):
    _host_only("cld restart")


@app.command("shutdown", hidden=True, context_settings=_ANY_ARGS)
def shutdown_stub(ctx: typer.Context):
    _host_only("cld shutdown")


@app.command("status", hidden=True, context_settings=_ANY_ARGS)
def status_stub(ctx: typer.Context):
    _host_only("cld status")


@app.command("logs", hidden=True, context_settings=_ANY_ARGS)
def logs_stub(ctx: typer.Context):
    _host_only("cld logs")


# --- Broker dispatch ----------------------------------------------------------


def _dispatch_task_agent_to_broker(cfg: Config, op: str, extra_args: list[str]) -> None:
    """Delegate a `cld task-agent <op>` to the host broker.

    Spawning happens host-side for the repo `start`'s `--repo` names (a mounted one, or
    any in the host registry); the other ops need none, so cwd never matters. The broker
    stamps `--parent <this container>` on the way through and refuses `--force`, so a
    caller reaps only its own fleet and can never override a reap-readiness refusal
    (docs/design-task-agents.md §7).
    """
    if not broker_available():
        typer.echo(
            "Error: the host broker is not configured for this container, so `cld task-agent` "
            "cannot reach the host. Set `broker_key` (and `broker_known_hosts`) "
            "in cld config and restart this container. Reading the fleet still works without it: "
            "the messenger's fleet_digest()/read_mailbox() tools and `cld task-agent "
            "transcript` all read the mounted mailbox.",
            err=True,
        )
        raise typer.Exit(1)
    log.info("Delegating `cld task-agent %s` to host broker", op)
    raise typer.Exit(broker_task_agent_op(op, extra_args))


def _task_agent_start_argv(
    refs: list[str], name: str, prompt: str,
    branch: str, model: str, revision: str, peer: list[str],
    shared_anchor: bool = False, repo: str = "",
) -> list[str]:
    """Rebuild `start`'s argv for the broker, which re-parses it host-side.

    Paths are the one argument that cannot cross: `/workspace/current` is
    container-ephemeral and a sibling target is an empty placeholder, so a path that
    resolves here resolves to nothing (or to the wrong file) there. An `@ref` is
    forwarded verbatim precisely so the host resolves it against the *target* repo;
    a real path is read here -- reading its own files is exactly what this container is
    entitled to do -- and folded into the inline text. Typed order is only preserved
    among the folded files: host-side the positionals compose first and `-p` is
    appended last, so every local file lands after every `@ref` no matter where it was
    typed. The broker refuses a bare path for the same reason
    (docs/design-prompt-chaining.md §4). *repo* leads as `--repo <name>`, where the
    broker consumes it.
    """
    argv: list[str] = ["--repo", repo] if repo else []
    bodies: list[str] = []
    for ref in refs:
        if ref.startswith("@"):
            argv.append(ref)
            continue
        body = Path(ref).read_text().strip()
        if not body:
            raise ValueError(f"prompt file is empty: {ref}")
        bodies.append(body)
    inline = "\n\n".join([*bodies, prompt] if prompt else bodies)
    if name:
        argv += ["-n", name]
    if inline:
        argv += ["-p", inline]
    if branch:
        argv += ["--branch", branch]
    if model:
        argv += ["-m", model]
    if revision:
        argv += ["-r", revision]
    for spec in peer:
        argv += ["--peer", spec]
    if shared_anchor:
        argv += ["--shared-anchor"]
    return argv


# --- Task-scoped agents -------------------------------------------------------
task_agent_app = typer.Typer(
    help="Task-scoped headless agents: one per task, bounded lifespan (see docs/design-task-agents.md).",
)
app.add_typer(task_agent_app, name="task-agent")


@task_agent_app.command("start")
@_handle_errors
def task_agent_start(
    refs: Optional[list[str]] = typer.Argument(None, help="Prompt refs in order: @<ref> resolved host-side, or a path in this container (folded into -p)"),
    name: str = typer.Option("", "-n", "--name", help="Task slug, kebab-case (default: --branch)"),
    prompt: str = typer.Option("", "-p", "--prompt", help="Inline task description, appended after the refs"),
    branch: str = typer.Option("", "--branch", help="Deliverable branch name (default: the task slug)"),
    model: str = typer.Option("", "-m", "--model", help="Claude model (e.g. opus, sonnet)"),
    revision: str = typer.Option("", "-r", "--revision", help="Anchor revision (default: current change)"),
    peer: list[str] = typer.Option([], "--peer", help="A peer this agent may message: <container-name>[:<hops>]. Repeatable."),
    shared_anchor: bool = typer.Option(
        False, "--shared-anchor",
        help="Anchor directly on -r, sharing reach with its existing descendants "
        "(needs explicit human approval; default is an isolated sibling)",
    ),
    repo: str = typer.Option(
        "", "--repo",
        help="Repo to target: a name mounted in this ticket, or any registered on the host "
        "(`cld repos`). Omit when this container has exactly one repo.",
    ),
):
    """Spawn a task-scoped agent. Every start creates a new container (no start-or-attach)."""
    cfg = Config.from_env()
    setup_logging(cfg)
    _dispatch_task_agent_to_broker(cfg, "start", _task_agent_start_argv(
        refs or [], name, prompt, branch, model, revision, peer, shared_anchor, repo,
    ))


@task_agent_app.command("status")
@_handle_errors
def task_agent_status(
    name: Optional[str] = typer.Argument(None, help="Task slug or container name; omit for the roster"),
):
    """Roster of this master's task-agents, or one agent in detail."""
    cfg = Config.from_env()
    setup_logging(cfg)
    _dispatch_task_agent_to_broker(cfg, "status", [name] if name else [])


@task_agent_app.command("logs")
@_handle_errors
def task_agent_logs(
    name: str = typer.Argument(..., help="Task slug or container name"),
    tail: int = typer.Option(80, "-n", "--tail", help="Number of lines to show"),
):
    """Tail a task-agent's supervisor log (state + cost), NOT its conversation."""
    cfg = Config.from_env()
    setup_logging(cfg)
    _dispatch_task_agent_to_broker(cfg, "logs", [name, "-n", str(tail)])


@task_agent_app.command("shutdown")
@_handle_errors
def task_agent_shutdown(
    name: Optional[str] = typer.Argument(None, help="Task slug or container name"),
    all_: bool = typer.Option(False, "--all", help="Reap every task-agent in this master's fleet"),
    force: bool = typer.Option(False, "--force", help="Host-only; refused here"),
):
    """Stop and remove a task-agent, forget its session bookmark, archive its mailbox."""
    cfg = Config.from_env()
    setup_logging(cfg)
    if all_ == bool(name):
        typer.echo("Error: pass a task slug/container name, or --all -- not both", err=True)
        raise typer.Exit(1)
    if force:
        # The broker denies it too; refusing here gives the reason instead of an
        # opaque exit code.
        typer.echo(
            "Error: --force is host-only. A master cannot override a reap-readiness "
            "refusal -- a refusal means wrap-up has not finished (or a live peer still "
            "depends on this agent), so drive that to completion instead.",
            err=True,
        )
        raise typer.Exit(1)
    _dispatch_task_agent_to_broker(cfg, "shutdown", [name] if name else ["--all"])


@task_agent_app.command("transcript")
@_handle_errors
def task_agent_transcript(
    name: str = typer.Argument(..., help="Task slug or container name"),
):
    """Print the mailbox conversation: what the agent received and what it sent.

    Needs no host channel: the mailbox tree is bind-mounted, and the name resolver
    falls back to the mailbox view where docker is unavailable.
    """
    cfg = Config.from_env()
    setup_logging(cfg)
    print_task_agent_transcript(cfg, resolve_task_agent(cfg, name))


# --- Runs and chains ----------------------------------------------------------

# `wait` stays under the agent's 10-minute command limit.
_WAIT_MAX_SECONDS = 540
_WAIT_POLL_SECONDS = 15
_WAIT_TIMEOUT_EXIT = 124


def _job_op(
    kind: Literal["run", "chain"], op: str, *args: str, repo: str = "", capture: bool = False,
) -> subprocess.CompletedProcess:
    """Send `cld <kind> <op>` through the broker; the helper is picked late so tests can patch it."""
    if not broker_available():
        typer.echo(
            f"Error: the host broker is not configured for this container, so `cld {kind}` "
            "cannot reach the host. Set `broker_key` (and `broker_known_hosts`) "
            "in cld config and restart this container.",
            err=True,
        )
        raise typer.Exit(1)
    helper = broker_run_op if kind == "run" else broker_chain_op
    return helper(op, *args, repo=repo, capture=capture)


def _repo_workspace(repo: str) -> str:
    """This container's workspace of *repo*, where a revset means this ticket's own tree."""
    raw = os.environ.get("CLD_TICKET_MANIFEST")
    if not raw:
        raise ValueError("`cld run` and `cld chain` work only in a ticket container")
    manifest = TicketManifest.from_json(raw)
    names = [entry.name for entry in manifest.repos]
    if not repo and len(names) != 1:
        raise ValueError(f"this ticket mounts {len(names)} repos -- pass --repo <name> ({', '.join(names)})")
    if repo and repo not in names:
        raise ValueError(f"repo '{repo}' is not mounted here -- its base must lie in your own tree ({', '.join(names)})")
    return f"/workspace/{manifest.ticket}/{repo or names[0]}"


def _jj_change_ids(workspace: str, revset: str) -> list[str]:
    result = subprocess.run(
        ["jj", "-R", workspace, "log", "-r", revset, "--no-graph", "-T", 'change_id ++ "\\n"'],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise ValueError(f"cannot resolve revision '{revset}': {result.stderr.strip()}")
    return result.stdout.split()


def _resolve_base_change_id(workspace: str, revision: str) -> str:
    """Resolve *revision* in this workspace to the one change id the broker gets.

    The host would read an ``@``-style revset in its own repo, not in this ticket's
    workspace; a change id means the same commit on both sides. A working-copy commit
    is refused because it changes on every snapshot.
    """
    ids = _jj_change_ids(workspace, revision)
    if len(ids) != 1:
        raise ValueError(f"revision '{revision}' must resolve to exactly one change, got {len(ids)}")
    if _jj_change_ids(workspace, f"{ids[0]} & working_copies()"):
        raise ValueError(
            f"revision '{revision}' is a working-copy commit, which changes on every snapshot. "
            "Run `jj new` first and pass the parent change, or pick another committed change."
        )
    return ids[0]


def _job_start(
    kind: Literal["run", "chain"], refs: list[str], name: str, prompt: str, model: str, revision: str, repo: str,
) -> None:
    if kind == "chain" and not (refs and refs[0].startswith("@")):
        raise ValueError("`cld chain start` needs `@<chain-name>` as the first argument")
    change_id = _resolve_base_change_id(_repo_workspace(repo), revision)
    argv = _task_agent_start_argv(refs, name, prompt, "", model, change_id, [])
    raise typer.Exit(_job_op(kind, "start", *argv, repo=repo).returncode)


def _job_status(kind: Literal["run", "chain"], name: str | None) -> None:
    raise typer.Exit(_job_op(kind, "status", *([name] if name else [])).returncode)


def _job_cancel(kind: Literal["run", "chain"], name: str) -> None:
    raise typer.Exit(_job_op(kind, "cancel", name).returncode)


def _job_wait(kind: Literal["run", "chain"], name: str, timeout: int) -> None:
    """Poll `status <name>` until the state leaves `running`; exit 124 when *timeout* runs out."""
    deadline = time.monotonic() + timeout
    while True:
        result = _job_op(kind, "status", name, capture=True)
        if result.returncode != 0:
            typer.echo(result.stderr.strip() or result.stdout.strip(), err=True)
            raise typer.Exit(result.returncode)
        state = next((line.partition(":")[2].strip() for line in result.stdout.splitlines()
                      if line.startswith("state:")), None)
        if state is None:
            raise RuntimeError(f"the broker's status of '{name}' has no state line:\n{result.stdout}")
        if state != "running":
            typer.echo(result.stdout.rstrip())
            raise typer.Exit(0)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            typer.echo(result.stdout.rstrip())
            typer.echo(f"Timed out after {timeout}s: '{name}' is still running. Wait again.", err=True)
            raise typer.Exit(_WAIT_TIMEOUT_EXIT)
        time.sleep(min(_WAIT_POLL_SECONDS, remaining))


_REFS_HELP = "Prompt refs in order: @<ref> resolved host-side, or a path in this container (folded into -p)"
_REPO_HELP = "Repo to target: a name mounted in this ticket. Omit when it mounts exactly one repo."
_REVISION_HELP = "Base change in your own tree, committed (not the working copy). Resolved here to a change id."
_TIMEOUT_HELP = f"Seconds to wait, at most {_WAIT_MAX_SECONDS} (the default); exit {_WAIT_TIMEOUT_EXIT} on timeout"

run_app = typer.Typer(help="One-shot headless runs, launched on the host (see skill broker-run-chain).")
chain_app = typer.Typer(help="Declarative multi-step chains, launched on the host (see skill broker-run-chain).")
app.add_typer(run_app, name="run")
app.add_typer(chain_app, name="chain")


@run_app.command("start")
@_handle_errors
def run_start(
    refs: Optional[list[str]] = typer.Argument(None, help=_REFS_HELP),
    revision: str = typer.Option(..., "-r", "--revision", help=_REVISION_HELP),
    name: str = typer.Option("", "-n", "--name", help="Run name (default: random); the session is run.<name>"),
    model: str = typer.Option("", "-m", "--model", help="Claude model (e.g. opus, sonnet)"),
    prompt: str = typer.Option("", "-p", "--prompt", help="Inline task, appended after the refs"),
    repo: str = typer.Option("", "--repo", help=_REPO_HELP),
):
    """Start a run on top of -r. Prints `started: <name>`."""
    setup_logging(Config.from_env())
    _job_start("run", refs or [], name, prompt, model, revision, repo)


@run_app.command("status")
@_handle_errors
def run_status(name: Optional[str] = typer.Argument(None, help="Run name; omit for the list")):
    """List this ticket's runs, or show one in detail."""
    setup_logging(Config.from_env())
    _job_status("run", name)


@run_app.command("wait")
@_handle_errors
def run_wait(
    name: str = typer.Argument(..., help="Run name"),
    timeout: int = typer.Option(_WAIT_MAX_SECONDS, "--timeout", min=0, max=_WAIT_MAX_SECONDS, help=_TIMEOUT_HELP),
):
    """Wait until the run is no longer running, then print its detail."""
    setup_logging(Config.from_env())
    _job_wait("run", name, timeout)


@run_app.command("cancel")
@_handle_errors
def run_cancel(name: str = typer.Argument(..., help="Run name")):
    """Stop a live run. Work committed so far stays on its bookmark."""
    setup_logging(Config.from_env())
    _job_cancel("run", name)


@chain_app.command("start")
@_handle_errors
def chain_start(
    refs: Optional[list[str]] = typer.Argument(None, help="`@<chain-name>` first, then the initial brief. " + _REFS_HELP),
    revision: str = typer.Option(..., "-r", "--revision", help=_REVISION_HELP),
    name: str = typer.Option(..., "-n", "--name", help="Chain name (required, must be unused)"),
    model: str = typer.Option("", "-m", "--model", help="Claude model (e.g. opus, sonnet)"),
    prompt: str = typer.Option("", "-p", "--prompt", help="Inline brief, appended after the refs"),
    repo: str = typer.Option("", "--repo", help=_REPO_HELP),
):
    """Start a chain on top of -r. Prints `started: <name>`."""
    setup_logging(Config.from_env())
    _job_start("chain", refs or [], name, prompt, model, revision, repo)


@chain_app.command("status")
@_handle_errors
def chain_status(name: Optional[str] = typer.Argument(None, help="Chain name; omit for the list")):
    """List this ticket's chains, or show one in detail."""
    setup_logging(Config.from_env())
    _job_status("chain", name)


@chain_app.command("wait")
@_handle_errors
def chain_wait(
    name: str = typer.Argument(..., help="Chain name"),
    timeout: int = typer.Option(_WAIT_MAX_SECONDS, "--timeout", min=0, max=_WAIT_MAX_SECONDS, help=_TIMEOUT_HELP),
):
    """Wait until the chain is no longer running, then print its detail."""
    setup_logging(Config.from_env())
    _job_wait("chain", name, timeout)


@chain_app.command("cancel")
@_handle_errors
def chain_cancel(name: str = typer.Argument(..., help="Chain name")):
    """Stop a live chain. Work committed so far stays on its bookmark."""
    setup_logging(Config.from_env())
    _job_cancel("chain", name)


# --- Mailbox messaging --------------------------------------------------------
app.add_typer(msg_app, name="msg")


# --- The broker ---------------------------------------------------------------


@app.command(context_settings=_ANY_ARGS)
@_handle_errors
def broker(
    ctx: typer.Context,
    action: str = typer.Argument(..., help="Broker action: run-tests, list-containers, task-agent, graphql, run, chain"),
):
    """Run a host-side action through the cld broker (docs/design-cld-broker.md).

    Everything after the action is forwarded verbatim as that action's argv, e.g.
    `cld broker run-tests -k login -x tests/`. The broker decides what an action may
    do; this is only the client.
    """
    if not broker_available():
        typer.echo(
            "Error: the cld broker is not configured for this container. Set `broker_key` "
            "(and `broker_known_hosts`) in cld config and restart this container.",
            err=True,
        )
        raise typer.Exit(1)
    raise typer.Exit(run_action(action, *ctx.args).returncode)


# --- Config-only surfaces -----------------------------------------------------


@app.command()
@_handle_errors
def repos():
    """List the repos this container works against.

    Ticket container (v2): one line per mounted repo from the launch manifest
    -- name, origin path, workspace path, anchor, mode
    (docs/design-ticket-containers.md section 6.6). The manifest env is
    host-set at launch.

    v1 task-agent: the single repo the container was launched for,
    tagged 'own'. Its host path comes from the host-set CLD_HOST_PROJECT_DIR,
    not from in-container TOML -- `.cld/` is gitignored, so a re-read would
    usually find nothing and disagree with the host.

    With the broker reachable, a second block lists the host registry entries not
    mounted here -- any of them is a valid `task-agent start --repo`.
    """
    cfg = Config.from_env()
    setup_logging(cfg)
    raw_manifest = os.environ.get("CLD_TICKET_MANIFEST", "")
    if raw_manifest:
        manifest = TicketManifest.from_json(raw_manifest)
        for repo in manifest.repos:
            typer.echo(
                f"{repo.name}\t/workspace/origin/{repo.name}"
                f"\t/workspace/{manifest.ticket}/{repo.name}"
                f"\t{repo.anchor_base[:12]}\t{repo.anchor_mode}"
            )
    elif cfg.host_project_dir:
        typer.echo(f"{cfg.host_project_dir}\town")
    if broker_available() and (others := [(n, p) for n, p, mounted in list_registered_repos() if not mounted]):
        typer.echo("\nAlso registered on the host (not mounted here):")
        for name, path in others:
            typer.echo(f"{name}\t{path}")


@app.command()
@_handle_errors
def prompts():
    """List the prompt templates an @<name> argument accepts."""
    cfg = Config.from_env()
    setup_logging(cfg)
    prompts_dir = Path(__file__).resolve().parent.parent / "prompts"
    if not prompts_dir.exists():
        typer.echo("No prompts directory found.", err=True)
        raise typer.Exit(1)

    items = list_prompt_items(prompts_dir)
    if not items:
        typer.echo("No prompts found.")
        return
    width = max(len(name) for name, _ in items)
    for name, desc in items:
        typer.echo(f"  {name:<{width}}  {desc}")


if __name__ == "__main__":
    app()
