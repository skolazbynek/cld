"""Named repo registry for ticket containers (PRODUCT_DESIGN.md section 4).

Registry entries live as ``[repos.<name>]`` tables in the *user* config
(``~/.config/cld/config.toml``); ``Config.from_env`` parses them into
``Config.repos``. Writes go through tomlkit so a hand-edited config keeps
its comments, ordering and unrelated keys. tomllib stays the read path.

This module must not import cld.config or cld.docker at runtime:
cld.config imports ``RepoEntry``/``parse_repos`` from here, and cld.docker
imports cld.config.
"""

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import tomlkit
import typer

from cld.log import get_logger, log_subprocess
from cld.vcs.anchor import default_anchor_revision

log = get_logger(__name__)

# Same shape as docker._TASK_SLUG_RE (not importable here, see module docstring):
# names become subdir names under the ticket root and docker label keys.
_REPO_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

_ENTRY_KEYS = {"path", "default_rev", "bootstrap"}

_TICKET_KIND_LABEL = "org.cld.kind=ticket"
_REPO_LABEL_PREFIX = "org.cld.repo."


@dataclass(frozen=True)
class RepoEntry:
    """One registered repo: host path plus per-repo launch settings."""

    path: str
    default_rev: str = ""
    bootstrap: bool = False


def validate_repo_name(name: str) -> None:
    if not _REPO_NAME_RE.match(name):
        raise RuntimeError(
            f"invalid repo name {name!r}: expected kebab-case (lowercase letters, "
            "digits and dashes, starting with a letter or digit)"
        )


def parse_repos(raw: dict) -> dict[str, RepoEntry]:
    """Parse a raw ``repos`` table-of-tables into ``RepoEntry`` values.

    Invalid entries are warned about and skipped, never fatal: a broken
    registry entry should break launching *that* repo, not every cld
    invocation. Paths are not checked for existence here -- only at
    ``repos add`` time.
    """
    repos: dict[str, RepoEntry] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            log.warning("repo '%s': expected a [repos.%s] table, skipping", name, name)
            continue
        if not _REPO_NAME_RE.match(name):
            log.warning(
                "repo '%s': invalid name (kebab-case only -- it becomes the ticket "
                "subdir name), skipping", name,
            )
            continue
        path = entry.get("path", "")
        if not path:
            log.warning("repo '%s': missing 'path', skipping", name)
            continue
        for key in sorted(set(entry) - _ENTRY_KEYS):
            log.warning("repo '%s': unknown key '%s'", name, key)
        repos[name] = RepoEntry(
            path=str(path),
            default_rev=str(entry.get("default_rev", "")),
            bootstrap=bool(entry.get("bootstrap", False)),
        )
    return repos


def _write_atomic(config_path: Path, doc: tomlkit.TOMLDocument) -> None:
    fd, tmp = tempfile.mkstemp(dir=config_path.parent, prefix=".config.toml.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(tomlkit.dumps(doc))
        os.replace(tmp, config_path)
    except BaseException:
        os.unlink(tmp)
        raise


def add_repo(
    config_path: Path,
    name: str,
    path: str,
    default_rev: str = "",
    bootstrap: bool = False,
) -> None:
    """Append a ``[repos.<name>]`` table to the user config, preserving the rest."""
    validate_repo_name(name)
    if not Path(path).expanduser().is_dir():
        raise RuntimeError(f"path '{path}' is not an existing directory")
    doc = tomlkit.parse(config_path.read_text())
    repos = doc.get("repos")
    if repos is None:
        doc["repos"] = tomlkit.table(is_super_table=True)
        repos = doc["repos"]
    if name in repos:
        raise RuntimeError(
            f"repo '{name}' is already registered ({repos[name].get('path', '?')}); "
            f"`cld repos rm {name}` first to re-register it"
        )
    entry = tomlkit.table()
    entry["path"] = path
    if default_rev:
        entry["default_rev"] = default_rev
    if bootstrap:
        entry["bootstrap"] = True
    repos[name] = entry
    _write_atomic(config_path, doc)


def remove_repo(config_path: Path, name: str) -> None:
    """Delete the ``[repos.<name>]`` table from the user config."""
    doc = tomlkit.parse(config_path.read_text())
    repos = doc.get("repos")
    if repos is None or name not in repos:
        raise RuntimeError(f"repo '{name}' is not registered in {config_path}")
    del repos[name]
    _write_atomic(config_path, doc)


def ticket_repo_mounts() -> dict[str, dict[str, str]]:
    """Map each ticket container (running or stopped) to its mounted repos.

    Read from the per-repo ``org.cld.repo.<name>=<host path>`` labels; a
    docker failure reads as no tickets (there is nothing to orphan without
    a daemon).
    """
    ps = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"label={_TICKET_KIND_LABEL}", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    )
    log_subprocess(log, ["docker", "ps", "-a"], ps)
    if ps.returncode != 0:
        return {}
    mounts: dict[str, dict[str, str]] = {}
    for container in ps.stdout.split():
        inspect = subprocess.run(
            ["docker", "inspect", "--format", "{{json .Config.Labels}}", container],
            capture_output=True, text=True,
        )
        log_subprocess(log, ["docker", "inspect", container], inspect)
        if inspect.returncode != 0:
            continue
        labels = json.loads(inspect.stdout)
        mounts[container] = {
            label.removeprefix(_REPO_LABEL_PREFIX): host_path
            for label, host_path in labels.items()
            if label.startswith(_REPO_LABEL_PREFIX)
        }
    return mounts


def tickets_referencing(name: str, path: str) -> list[str]:
    """Ticket containers whose manifest mounts *name* at *path* (blocks `repos rm`)."""
    host_path = str(Path(path).expanduser())
    return sorted(
        container for container, repos in ticket_repo_mounts().items()
        if repos.get(name) == host_path
    )


def resolve_repo_specs(
    repos: dict[str, RepoEntry], specs: list[str],
) -> dict[str, RepoEntry]:
    """Resolve launch repo specs (registry names or ad-hoc paths) to named entries.

    An ad-hoc path's basename becomes the subdir name, so it must pass the
    same name validation, and a resolved-name collision is an error.
    """
    resolved: dict[str, RepoEntry] = {}
    origin: dict[str, str] = {}
    for spec in specs:
        if spec in repos:
            name, entry = spec, repos[spec]
        elif "/" in spec or spec.startswith(("~", ".")) or Path(spec).is_dir():
            adhoc = Path(spec).expanduser()
            name = adhoc.name
            validate_repo_name(name)
            entry = RepoEntry(path=str(adhoc))
        else:
            raise RuntimeError(
                f"'{spec}' is neither a registered repo name nor a path; "
                "`cld repos` lists the registry"
            )
        if name in resolved:
            raise RuntimeError(
                f"repo name '{name}' resolved twice (from '{origin[name]}' and "
                f"'{spec}'); names are the ticket subdir names, so they must be unique"
            )
        resolved[name] = entry
        origin[name] = spec
    return resolved


def parse_selection(raw: str, count: int) -> list[int]:
    """Parse the picker's comma-separated 1-based numbers; empty means abort."""
    raw = raw.strip()
    if not raw:
        return []
    picked: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        try:
            num = int(part)
        except ValueError:
            raise RuntimeError(f"'{part}' is not a number") from None
        if not 1 <= num <= count:
            raise RuntimeError(f"{num} is out of range (1-{count})")
        if num not in picked:
            picked.append(num)
    return picked


def pick_repos(repos: dict[str, RepoEntry]) -> list[tuple[str, RepoEntry, str]]:
    """Interactive registry picker: multi-select, then a per-repo anchor prompt.

    Returns ``(name, entry, revision)`` triples. The caller owns the TTY
    check -- non-interactive launches with no repos are its hard error.
    """
    if not repos:
        raise RuntimeError(
            "the repo registry is empty; `cld repos add <name> <path>` first, "
            "or pass repo paths explicitly"
        )
    names = sorted(repos)
    for i, name in enumerate(names, 1):
        typer.echo(f"  {i}. {name}  {repos[name].path}")
    raw = typer.prompt(
        "Repos to mount (comma-separated numbers, empty to abort)",
        default="", show_default=False,
    )
    picked = parse_selection(raw, len(names))
    if not picked:
        raise typer.Abort()
    selection: list[tuple[str, RepoEntry, str]] = []
    for num in picked:
        name = names[num - 1]
        entry = repos[name]
        revision = typer.prompt(
            f"{name} anchor",
            default=entry.default_rev or default_anchor_revision(Path(entry.path).expanduser()),
        )
        selection.append((name, entry, revision))
    return selection
