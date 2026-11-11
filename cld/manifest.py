"""Launch manifest for ticket containers (docs/design-ticket-containers.md section 2).

The manifest is the resolved repo set stamped into container labels at
``docker run``: the single source of truth for restart, status and broker
target validation. It carries durable identity facts only -- per-launch
ephemera (workspace-file lists, secrets paths) are recomputed from registry
and repo config on every recreate. Labels record the base anchor A plus mode
and session; the effective anchor B is derived from the jj store at check
time, never labeled.

This module must not import cld.config or cld.docker at runtime: cld.docker
imports from here.
"""

import json
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from cld.log import get_logger, log_subprocess
from cld.registry import RepoEntry, resolve_repo_specs
from cld.vcs import get_backend
from cld.vcs.anchor import default_anchor_revision, resolve_anchor

log = get_logger(__name__)

MANIFEST_VERSION = 1

KIND_LABEL = "org.cld.kind"
TICKET_LABEL = "org.cld.ticket"
SESSION_LABEL = "org.cld.session"
# Authoritative JSON manifest, read back via `docker inspect`.
MANIFEST_LABEL = "org.cld.manifest"
# Redundant flat per-repo labels for line-based readers (cld-broker.py reads
# labels without parsing JSON); must stay parseable by cld.registry's ticket_repo_mounts.
REPO_LABEL_PREFIX = "org.cld.repo."


@dataclass(frozen=True)
class RepoManifestEntry:
    """One mounted repo, as resolved at launch.

    ``rev_source`` (``arg`` | ``registry`` | ``trunk``) is provenance for
    status output only -- it never participates in identity comparisons.
    """

    name: str
    path: str
    anchor_base: str
    anchor_mode: str = "isolated"
    rev_source: str = "registry"

    def identity(self) -> tuple[str, str, str]:
        """The fields whose change makes a kept repo count as changed in a diff."""
        return (self.path, self.anchor_base, self.anchor_mode)


@dataclass(frozen=True)
class TicketManifest:
    """The resolved repo set of one ticket container. ``ticket`` is the slug."""

    ticket: str
    repos: tuple[RepoManifestEntry, ...]

    def to_json(self) -> str:
        return json.dumps({
            "v": MANIFEST_VERSION,
            "ticket": self.ticket,
            "repos": [
                {
                    "name": r.name,
                    "path": r.path,
                    "anchor_base": r.anchor_base,
                    "anchor_mode": r.anchor_mode,
                    "rev_source": r.rev_source,
                }
                for r in self.repos
            ],
        })

    @classmethod
    def from_json(cls, raw: str) -> "TicketManifest":
        """Decode a labeled manifest. Unknown keys are ignored (forward compat
        within v1); an unknown version is refused rather than misread."""
        data = json.loads(raw)
        version = data.get("v")
        if version != MANIFEST_VERSION:
            raise RuntimeError(
                f"unsupported manifest version {version!r} (this cld reads v{MANIFEST_VERSION}); "
                "upgrade cld to manage this ticket container"
            )
        return cls(
            ticket=data["ticket"],
            repos=tuple(
                RepoManifestEntry(
                    name=entry["name"],
                    path=entry["path"],
                    anchor_base=entry["anchor_base"],
                    anchor_mode=entry.get("anchor_mode", "isolated"),
                    rev_source=entry.get("rev_source", "registry"),
                )
                for entry in data["repos"]
            ),
        )


def manifest_labels(manifest: TicketManifest, session: str) -> dict[str, str]:
    """All ``org.cld.*`` labels a ticket container is stamped with.

    One writer for both encodings -- the authoritative JSON manifest and the
    redundant flat per-repo path labels -- so they cannot drift.
    """
    labels = {
        KIND_LABEL: "ticket",
        TICKET_LABEL: manifest.ticket,
        SESSION_LABEL: session,
        MANIFEST_LABEL: manifest.to_json(),
    }
    for repo in manifest.repos:
        labels[f"{REPO_LABEL_PREFIX}{repo.name}"] = repo.path
    return labels


def _split_rev(spec: str) -> tuple[str, str]:
    """Split a ``repo[@rev]`` launch spec.

    Rule: a spec that as a whole names an existing path on disk is a path with
    no rev override (so ``./dir@2x`` is not misread as ``./dir`` at rev
    ``2x``); otherwise it splits at the first ``@``, which lets a rev itself
    contain ``@`` (``repo@main@origin`` -> rev ``main@origin``).
    """
    if "@" in spec and Path(spec).expanduser().exists():
        return spec, ""
    base, sep, rev = spec.partition("@")
    if sep and not rev:
        raise RuntimeError(f"'{spec}': empty revision after '@'")
    return base, rev


def default_resolver(path: str, revision: str, mode: str) -> str:
    """Resolve *revision* to an anchor id (``resolve_anchor``) in the repo at *path*.

    *mode* (``isolated`` | ``shared``) is unused here; the overlap-checking
    resolver (``cld.docker.ticket_anchor_resolver``) needs it, so it is part
    of the resolver contract.
    """
    return resolve_anchor(get_backend(Path(path)), revision)


def resolve_manifest(
    ticket: str,
    specs: list[str],
    repos: dict[str, RepoEntry],
    *,
    shared: Iterable[str] = (),
    resolver: Callable[[str, str, str], str] = default_resolver,
) -> TicketManifest:
    """Resolve launch args + registry into a manifest.

    Each spec is a registry name or ad-hoc path with an optional ``@rev``
    anchor override; without one the registry ``default_rev`` applies, falling
    back to ``trunk()`` (jj) or the remote HEAD branch / ``HEAD`` (git). *shared* names the repos launched with
    ``--shared-anchor``. *resolver* pins each ``(path, revision, mode)`` to a
    change id (jj) --- the launch path passes the overlap-checking resolver here.
    """
    bare: list[str] = []
    overrides: list[str] = []
    for spec in specs:
        base, rev = _split_rev(spec)
        bare.append(base)
        overrides.append(rev)
    resolved = resolve_repo_specs(repos, bare)
    shared_set = set(shared)
    if unknown := shared_set - set(resolved):
        raise RuntimeError(
            f"--shared-anchor names no launched repo: {', '.join(sorted(unknown))} "
            f"(launching: {', '.join(resolved)})"
        )
    entries: list[RepoManifestEntry] = []
    for (name, entry), override in zip(resolved.items(), overrides, strict=True):
        # Expanded exactly like registry.tickets_referencing expands for its
        # comparison, so `repos rm` protection matches the labeled path.
        path = str(Path(entry.path).expanduser())
        if override:
            revision, source = override, "arg"
        elif entry.default_rev:
            revision, source = entry.default_rev, "registry"
        else:
            revision, source = default_anchor_revision(Path(path)), "trunk"
        mode = "shared" if name in shared_set else "isolated"
        entries.append(RepoManifestEntry(
            name=name,
            path=path,
            anchor_base=resolver(path, revision, mode),
            anchor_mode=mode,
            rev_source=source,
        ))
    return TicketManifest(ticket=ticket, repos=tuple(entries))


@dataclass(frozen=True)
class ManifestDiff:
    """Repo-set difference between a labeled manifest and a newly resolved one.

    ``changed`` pairs are (old, new) for kept names whose identity (path,
    anchor_base or anchor_mode) differs; a ``rev_source``-only difference is
    not a change.
    """

    added: tuple[RepoManifestEntry, ...]
    removed: tuple[RepoManifestEntry, ...]
    changed: tuple[tuple[RepoManifestEntry, RepoManifestEntry], ...]

    def __bool__(self) -> bool:
        return bool(self.added or self.removed or self.changed)


def diff_manifests(old: TicketManifest, new: TicketManifest) -> ManifestDiff:
    old_by = {r.name: r for r in old.repos}
    new_by = {r.name: r for r in new.repos}
    return ManifestDiff(
        added=tuple(r for r in new.repos if r.name not in old_by),
        removed=tuple(r for r in old.repos if r.name not in new_by),
        changed=tuple(
            (old_by[r.name], r)
            for r in new.repos
            if r.name in old_by and old_by[r.name].identity() != r.identity()
        ),
    )


def keep_attached_anchors(old: TicketManifest, new: TicketManifest) -> TicketManifest:
    """Carry each kept repo's recorded anchor forward from *old* into *new*.

    On a confirmed repo-set recreate a kept repo reattaches at its existing
    bookmark, so its editable stack still descends from the OLD anchor -- the
    launched manifest must record that reality, or the overlap check derives
    the effective anchor from the wrong base and the ticket's real stack goes
    invisible. The newly requested anchor only becomes real after
    ``cld shutdown`` + ``cld start`` (a fresh lifecycle). A kept name whose
    *path* changed points at a different store where the bookmark does not
    exist, so its new anchor takes effect at once and is not carried.
    """
    old_by = {r.name: r for r in old.repos}
    return replace(new, repos=tuple(
        replace(
            repo,
            anchor_base=old_by[repo.name].anchor_base,
            anchor_mode=old_by[repo.name].anchor_mode,
            rev_source=old_by[repo.name].rev_source,
        )
        if repo.name in old_by and old_by[repo.name].path == repo.path
        else repo
        for repo in new.repos
    ))


def read_manifest(container: str) -> TicketManifest:
    """Read the labeled manifest back off a (running or stopped) container."""
    cmd = ["docker", "inspect", "--format", "{{json .Config.Labels}}", container]
    result = subprocess.run(cmd, capture_output=True, text=True)
    log_subprocess(log, cmd, result)
    if result.returncode != 0:
        raise RuntimeError(
            f"cannot inspect container '{container}': {(result.stderr or '').strip()}"
        )
    labels = json.loads(result.stdout) or {}
    raw = labels.get(MANIFEST_LABEL, "")
    if not raw:
        raise RuntimeError(
            f"container '{container}' carries no {MANIFEST_LABEL} label -- "
            "not a ticket container"
        )
    return TicketManifest.from_json(raw)
