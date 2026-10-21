"""Application configuration. All runtime tunables live here.

Each Typer command (and MCP tool) constructs ``Config.from_env()`` once at
entry and passes it explicitly down the call chain.

Static structural constants (image-internal paths like CONTAINER_HOME,
mount layouts) stay as module constants in their owning files -- they're
not user-tunable and are coupled to Dockerfile/shell-script invariants.
"""

import json
import logging
import os
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from cld.log import _CldFormatter, _LazyStderrHandler
from cld.registry import RepoEntry, parse_repos

_log = logging.getLogger("cld.config")
if not _log.handlers:
    _h = _LazyStderrHandler()
    _h.setFormatter(_CldFormatter(use_color=False))
    _log.addHandler(_h)
    _log.propagate = False


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    return int(val) if val else default


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name, "").lower()
    if not val:
        return default
    return val in ("1", "true", "yes", "on")


def _parse_path_map(raw: str) -> dict[str, str]:
    """Parse ``CLD_PATH_MAP`` (a JSON object set by the cld launcher).

    Config loading must not die on a malformed value: warn and fall back to no
    map -- path translation then uses the scalar host_project_dir/host_home pair.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as e:
        _log.warning("CLD_PATH_MAP is not valid JSON (%s); ignoring it", e)
        return {}
    if not isinstance(parsed, dict):
        _log.warning(
            "CLD_PATH_MAP must be a JSON object, got %s; ignoring it",
            type(parsed).__name__,
        )
        return {}
    return parsed


_TOML_KEYS = {
    "base_image",
    "devcontainer_image",
    "run_image",
    "agent_timeout",
    "poll_interval",
    "debug",
    "home_mounts_always",
    "home_mounts_devcontainer",
    "ssl_certs_path",
    "chain_max_parallel",
    "chain_default_model",
    "log_level",
    "log_color",
    "ignore_gitignore",
    "ssh_auth_sock",
    "mailbox_root",
    "agent_max_turns",
    "agent_kickoff_persona",
    "max_task_agents",
    "peer_absolute_limit",
    "root_ask_limit",
    "broker_key",
    "broker_endpoint",
    "broker_known_hosts",
    "otel_endpoint",
    # Not a Config field -- no Python code reads this. Recognized here only so
    # _load_toml() doesn't warn "unknown key" on repos that set it; the value
    # is read directly out of .cld/config.toml by cld-broker.py's own parser
    # (PROJECT_SUBDIR for the cld broker / runtests container).
    "pyproject_dir",
    # Same story as pyproject_dir above, but for the `graphql` broker action's
    # server lifecycle (cld-broker.py's resolve_graphql_config): the shell
    # command that starts the server, the port it binds inside its container,
    # and the HTTP path the readiness probe/queries hit.
    "graphql_command",
    "graphql_port",
    "graphql_health_path",
}


# Keys only the user config may set: a repo's .cld/config.toml can be an agent
# commit, and these name host paths that get mounted into containers.
_USER_ONLY_KEYS = {"home_mounts_always", "home_mounts_devcontainer"}


_DEFAULT_CONFIG_TEMPLATE = Path(__file__).parent / "config.default.toml"


def _user_config_path() -> Path:
    return Path.home() / ".config" / "cld" / "config.toml"


def _default_mailbox_root() -> str:
    return str(Path.home() / ".cld" / "mailboxes")


def _ensure_user_config(path: Path) -> None:
    """Copy the default template to ``path`` if it does not exist yet."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_DEFAULT_CONFIG_TEMPLATE, path)
    _log.warning("created default config at %s", path)


def _find_project_config(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` (or cwd) looking for ``.cld/config.toml``.

    Lives under the gitignored ``.cld/`` dir so it's host-local by default,
    not committed. Independent of VCS detection so config can be discovered
    before a backend is required (and so a missing VCS does not abort startup).
    """
    cur = (start or Path.cwd()).resolve()
    for d in (cur, *cur.parents):
        candidate = d / ".cld" / "config.toml"
        if candidate.is_file():
            return candidate
    return None


def _load_toml(path: Path, user_layer: bool = False) -> dict:
    """Read a TOML file, warn on parse errors or unknown keys; return known keys only.

    ``user_layer`` marks the one file allowed to define ``[repos.*]``
    tables and ``_USER_ONLY_KEYS``: the user config (PRODUCT_DESIGN.md section 4
    puts the registry solely there). Every other layer -- a repo's
    ``.cld/config.toml`` in particular -- has them ignored with a warning, so a
    checked-out repo cannot inject registry entries or host mounts.
    """
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        _log.warning("failed to read %s: %s", path, e)
        return {}
    renamed = {k: k.removeprefix("host_") for k in data if k.startswith("host_broker_")}
    if renamed:
        pairs = ", ".join(f"'{old}' -> '{new}'" for old, new in sorted(renamed.items()))
        raise RuntimeError(
            f"{path}: the broker config keys lost their 'host_' prefix ({pairs}). "
            "Rename them; the values are still valid as-is. Ignoring them would leave "
            "the broker silently off, which breaks every task-agent launch."
        )
    unknown = set(data) - _TOML_KEYS - {"repos"}
    for key in sorted(unknown):
        _log.warning("unknown key '%s' in %s", key, path)
    known = {k: v for k, v in data.items() if k in _TOML_KEYS}
    if not user_layer:
        for key in sorted(_USER_ONLY_KEYS & known.keys()):
            _log.warning("'%s' in %s is ignored: it is read only from the user config", key, path)
            del known[key]
    repos = data.get("repos")
    if repos is not None:
        if not user_layer:
            _log.warning(
                "'repos' in %s is ignored: the registry lives only in the user "
                "config (~/.config/cld/config.toml)", path,
            )
        elif isinstance(repos, dict):
            known["repos"] = repos
        else:
            _log.warning("'repos' in %s must be a table of [repos.<name>] tables", path)
    return known


def _load_dotenv(path: Path | None = None) -> None:
    """Read a .env file and inject its variables into ``os.environ``.

    Limitations: does not handle quoted values, ``export`` prefix, or escape
    sequences. Values are split on the first ``=`` and stripped of surrounding
    whitespace only.
    """
    dotenv = path or Path.cwd() / ".env"
    if not dotenv.is_file():
        return
    for line in dotenv.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        if key:
            os.environ[key.strip()] = value.strip()


def _resolve_ssh_auth_sock(layered: dict) -> str | None:
    """Tri-state resolution: env takes precedence over TOML; absent -> None."""
    if "CLD_SSH_AUTH_SOCK" in os.environ:
        return os.environ["CLD_SSH_AUTH_SOCK"]
    if "ssh_auth_sock" in layered:
        return layered["ssh_auth_sock"]
    return None


@dataclass(frozen=True)
class Config:
    """All runtime-tunable settings.

    Field defaults apply when the corresponding ``CLD_*`` env var is unset.
    Construct via ``Config.from_env()`` at command entry; tests construct
    directly with kwargs.
    """

    # Docker image names
    base_image: str = "claude-base:latest"
    devcontainer_image: str = "claude-devcontainer:latest"
    run_image: str = "claude-run:latest"

    # SSL CA certificates path on the host (dir or file).
    # Empty = auto-detect: /etc/ssl/certs (Linux) then /etc/ssl/cert.pem (macOS).
    # Set explicitly to use a custom CA bundle; leave empty to skip if neither found.
    ssl_certs_path: str = ""

    # RO $HOME paths staged into every container (relative to $HOME)
    home_mounts_always: tuple[str, ...] = (
        ".claude.json",
        ".config/anthropic",
        ".config/claude",
        ".config/jj",
    )
    # Additional RO $HOME paths staged only for devcontainer
    home_mounts_devcontainer: tuple[str, ...] = (
        ".gitconfig",
        ".bashrc",
        ".config/nvim",
        ".local/state/nvim",
        ".cache/nvim",
    )

    # Named repo registry for ticket containers: [repos.<name>] tables in the
    # user config (PRODUCT_DESIGN.md section 4). Managed with `cld repos`.
    repos: dict[str, RepoEntry] = field(default_factory=dict)

    # Set by the host launcher when running inside a container, so Python
    # code (e.g. nested `cld` invocations) can translate container-side
    # paths back to host paths for sibling -v mounts. Empty on the host.
    host_project_dir: str = ""
    host_home: str = ""

    # Ticket containers get the per-repo prefix map instead of the scalar
    # pair: CLD_PATH_MAP is a JSON object {container prefix: host prefix},
    # longest prefix wins (design-ticket-containers.md section 6.4). Empty on
    # the host and in v1 kinds.
    path_map: dict[str, str] = field(default_factory=dict)

    # Agent-wait tunables (used by chain orchestrator)
    agent_timeout: int = 1800
    poll_interval: int = 30

    # Chain orchestration tunables
    chain_max_parallel: int = 4
    chain_default_model: str = ""

    # Workspace setup: gitignored files to symlink into workspace
    ignore_gitignore: tuple[str, ...] = ()

    # SSH agent forwarding for devcontainer sessions.
    # None (unset) = auto-detect from $SSH_AUTH_SOCK on the host.
    # "" (explicitly empty) = disable forwarding entirely.
    # "/path/to/socket" = use that host socket path explicitly.
    ssh_auth_sock: str | None = None

    # Inter-container agent messaging (mailboxes + the headless supervisor,
    # cld/messenger/agent_loop.py). agent_kickoff_persona only feeds the
    # supervisor's non-task kickoff -- vestigial since the standing `cld agent`
    # role was removed; a task-agent composes its kickoff from the task brief.
    mailbox_root: str = _default_mailbox_root()
    agent_max_turns: int = 120
    agent_kickoff_persona: str = "agent"

    # Task-scoped agents (see docs/design-task-agents.md). max_task_agents caps
    # running task-agents per master; peer_absolute_limit is the hop budget a
    # peer edge gets when its `--peer <name>[:<hops>]` spec omits one;
    # root_ask_limit bounds the asks outstanding under one unanswered question.
    max_task_agents: int = 5
    peer_absolute_limit: int = 10
    root_ask_limit: int = 5

    # The cld broker: if broker_key is set, task-agent/ticket containers
    # mount the restricted private key and get a `cld broker` wrapper that ships
    # pytest args to a host-side SSH broker running the `runtests` container.
    # Empty = off. Agents and task-agents are instructed (via their personas)
    # to only invoke it with explicit per-run authorization from their master.
    # See docs/design-cld-broker.md.
    broker_key: str = ""
    broker_endpoint: str = "host.docker.internal:2222"
    broker_known_hosts: str = ""

    # Host-side OpenTelemetry collector (see otel/): if set, agent/task-agent/
    # ticket containers point Claude Code's
    # own OTEL metrics export at it, tagged with this session's name as the
    # standard `service.name` resource attribute. Empty = off. The collector
    # itself is a plain, cld-independent OTel Collector -- see otel/README.md.
    otel_endpoint: str = ""

    # Diagnostics
    debug: bool = False
    log_level: str = "INFO"
    log_color: str = "auto"

    @classmethod
    def from_env(
        cls,
        dotenv: Path | None = None,
        user_config: Path | None = None,
        project_config: Path | None = None,
    ) -> "Config":
        """Build a ``Config`` layering: defaults < user TOML < project TOML < .env < CLD_* env."""
        _load_dotenv(dotenv)
        layered: dict = {}
        up = user_config if user_config is not None else _user_config_path()
        _ensure_user_config(up)
        if up.is_file():
            layered.update(_load_toml(up, user_layer=True))
        pp = project_config if project_config is not None else _find_project_config()
        if pp and pp.is_file():
            layered.update(_load_toml(pp))
        return cls(
            base_image=_env_str("CLD_BASE_IMAGE", layered.get("base_image", "claude-base:latest")),
            devcontainer_image=_env_str("CLD_DEVCONTAINER_IMAGE", layered.get("devcontainer_image", "claude-devcontainer:latest")),
            run_image=_env_str("CLD_RUN_IMAGE", layered.get("run_image", "claude-run:latest")),
            ssl_certs_path=_env_str("CLD_SSL_CERTS_PATH", layered.get("ssl_certs_path", "")),
            host_project_dir=_env_str("CLD_HOST_PROJECT_DIR"),
            host_home=_env_str("CLD_HOST_HOME"),
            path_map=_parse_path_map(_env_str("CLD_PATH_MAP")),
            agent_timeout=_env_int("CLD_AGENT_TIMEOUT", int(layered.get("agent_timeout", 1800))),
            poll_interval=_env_int("CLD_POLL_INTERVAL", int(layered.get("poll_interval", 30))),
            debug=_env_bool("CLD_DEBUG", bool(layered.get("debug", False))),
            log_level=_env_str("CLD_LOG_LEVEL", layered.get("log_level", "INFO")),
            log_color=_env_str("CLD_LOG_COLOR", layered.get("log_color", "auto")),
            home_mounts_always=tuple(layered.get("home_mounts_always", (
                ".claude.json", ".config/anthropic", ".config/claude", ".config/jj",
            ))),
            home_mounts_devcontainer=tuple(layered.get("home_mounts_devcontainer", (
                ".gitconfig", ".bashrc", ".config/nvim", ".local/state/nvim", ".cache/nvim",
            ))),
            repos=parse_repos(layered.get("repos", {})),
            chain_max_parallel=_env_int("CLD_CHAIN_MAX_PARALLEL", int(layered.get("chain_max_parallel", 4))),
            chain_default_model=_env_str("CLD_CHAIN_DEFAULT_MODEL", layered.get("chain_default_model", "")),
            ignore_gitignore=tuple(layered.get("ignore_gitignore", ())),
            ssh_auth_sock=_resolve_ssh_auth_sock(layered),
            mailbox_root=_env_str("CLD_MAILBOX_ROOT", layered.get("mailbox_root", _default_mailbox_root())),
            agent_max_turns=_env_int("CLD_AGENT_MAX_TURNS", int(layered.get("agent_max_turns", 120))),
            agent_kickoff_persona=_env_str("CLD_AGENT_KICKOFF_PERSONA", layered.get("agent_kickoff_persona", "agent")),
            max_task_agents=_env_int("CLD_MAX_TASK_AGENTS", int(layered.get("max_task_agents", 4))),
            peer_absolute_limit=_env_int("CLD_PEER_ABSOLUTE_LIMIT", int(layered.get("peer_absolute_limit", 10))),
            root_ask_limit=_env_int("CLD_ROOT_ASK_LIMIT", int(layered.get("root_ask_limit", 3))),
            broker_key=_env_str("CLD_BROKER_KEY", layered.get("broker_key", "")),
            broker_endpoint=_env_str("CLD_BROKER_ENDPOINT", layered.get("broker_endpoint", "host.docker.internal:2222")),
            broker_known_hosts=_env_str("CLD_BROKER_KNOWN_HOSTS", layered.get("broker_known_hosts", "")),
            otel_endpoint=_env_str("CLD_OTEL_ENDPOINT", layered.get("otel_endpoint", "")),
        )
