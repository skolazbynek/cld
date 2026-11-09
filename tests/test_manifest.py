"""Tests for the ticket launch manifest (cld.manifest)."""

import json
from unittest.mock import patch

import pytest

from cld.manifest import (
    MANIFEST_LABEL,
    REPO_LABEL_PREFIX,
    ManifestDiff,
    RepoManifestEntry,
    TicketManifest,
    diff_manifests,
    keep_attached_anchors,
    manifest_labels,
    read_manifest,
    resolve_manifest,
)
from cld.registry import RepoEntry, ticket_repo_mounts


def _entry(name, path="/host/repo", anchor="a" * 40, mode="isolated", source="registry"):
    return RepoManifestEntry(
        name=name, path=path, anchor_base=anchor, anchor_mode=mode, rev_source=source,
    )


def _manifest(*entries):
    return TicketManifest(ticket="lide-2600", repos=tuple(entries))


def _fake_resolver(path, revision, mode):
    return f"hash({revision})"


class TestJsonCodec:
    def test_round_trip(self):
        m = _manifest(_entry("lide-api"), _entry("diskuze-api", mode="shared", source="arg"))
        assert TicketManifest.from_json(m.to_json()) == m

    def test_unknown_keys_ignored_for_forward_compat(self):
        m = _manifest(_entry("lide-api"))
        raw = m.to_json().replace(
            '"ticket"', '"sessions": ["s1"], "ticket"',
        ).replace('"name"', '"future_field": 1, "name"')
        assert TicketManifest.from_json(raw) == m

    def test_unknown_version_refused(self):
        raw = _manifest(_entry("r")).to_json().replace('"v": 1', '"v": 2')
        with pytest.raises(RuntimeError, match="unsupported manifest version"):
            TicketManifest.from_json(raw)

    def test_optional_fields_default(self):
        raw = '{"v": 1, "ticket": "t", "repos": [{"name": "r", "path": "/p", "anchor_base": "h"}]}'
        m = TicketManifest.from_json(raw)
        assert m.repos[0].anchor_mode == "isolated"
        assert m.repos[0].rev_source == "registry"


class TestManifestLabels:
    def test_json_label_round_trips(self):
        m = _manifest(_entry("lide-api"), _entry("diskuze-api", path="/host/d"))
        labels = manifest_labels(m, "t.lide-2600")
        assert TicketManifest.from_json(labels[MANIFEST_LABEL]) == m

    def test_kind_ticket_session_labels(self):
        labels = manifest_labels(_manifest(_entry("r")), "t.lide-2600")
        assert labels["org.cld.kind"] == "ticket"
        assert labels["org.cld.ticket"] == "lide-2600"
        assert labels["org.cld.session"] == "t.lide-2600"

    def test_flat_repo_labels_carry_host_paths(self):
        m = _manifest(_entry("lide-api", path="/host/lide"), _entry("db", path="/host/db"))
        labels = manifest_labels(m, "s")
        assert labels[f"{REPO_LABEL_PREFIX}lide-api"] == "/host/lide"
        assert labels[f"{REPO_LABEL_PREFIX}db"] == "/host/db"

    def test_flat_labels_parse_through_registry_readers(self):
        """The redundant flat labels must stay readable by cld.registry's
        ticket_repo_mounts, which the broker-facing helpers key on."""
        m = _manifest(_entry("lide-api", path="/host/lide"))
        labels = manifest_labels(m, "t.lide-2600")
        ps = type("R", (), {"returncode": 0, "stdout": "t.lide-2600\n", "stderr": ""})()
        inspect = type("R", (), {"returncode": 0, "stdout": json.dumps(labels), "stderr": ""})()
        with patch("cld.registry.subprocess.run", side_effect=[ps, inspect]):
            assert ticket_repo_mounts() == {
                "t.lide-2600": {"lide-api": "/host/lide"},
            }


class TestResolveManifest:
    def _registry(self, **kwargs):
        return {"lide-api": RepoEntry(path="/host/lide-api", **kwargs)}

    def test_registry_name_with_default_rev(self):
        repos = self._registry(default_rev="main")
        m = resolve_manifest("t", ["lide-api"], repos, resolver=_fake_resolver)
        entry = m.repos[0]
        assert entry.name == "lide-api"
        assert entry.path == "/host/lide-api"
        assert entry.anchor_base == "hash(main)"
        assert entry.rev_source == "registry"
        assert entry.anchor_mode == "isolated"

    def test_missing_default_rev_falls_back_to_trunk(self):
        m = resolve_manifest("t", ["lide-api"], self._registry(), resolver=_fake_resolver)
        assert m.repos[0].anchor_base == "hash(trunk())"
        assert m.repos[0].rev_source == "trunk"

    def test_at_rev_override_wins_over_default_rev(self):
        repos = self._registry(default_rev="main")
        m = resolve_manifest("t", ["lide-api@xyz"], repos, resolver=_fake_resolver)
        assert m.repos[0].anchor_base == "hash(xyz)"
        assert m.repos[0].rev_source == "arg"

    def test_rev_may_itself_contain_at(self):
        m = resolve_manifest(
            "t", ["lide-api@main@origin"], self._registry(), resolver=_fake_resolver,
        )
        assert m.repos[0].anchor_base == "hash(main@origin)"

    def test_empty_rev_after_at_is_an_error(self):
        with pytest.raises(RuntimeError, match="empty revision"):
            resolve_manifest("t", ["lide-api@"], self._registry(), resolver=_fake_resolver)

    def test_adhoc_path_basename_becomes_the_name(self, tmp_path):
        repo = tmp_path / "my-repo"
        repo.mkdir()
        m = resolve_manifest("t", [str(repo)], {}, resolver=_fake_resolver)
        assert m.repos[0].name == "my-repo"
        assert m.repos[0].path == str(repo)
        assert m.repos[0].rev_source == "trunk"

    def test_path_spec_containing_at_is_a_path_not_a_rev(self, tmp_path):
        """A spec that as a whole names an existing path keeps its '@' -- only
        a non-path spec splits at the first '@' (the _split_rev rule)."""
        repo = tmp_path / "assets@2x" / "my-repo"
        repo.mkdir(parents=True)
        m = resolve_manifest("t", [str(repo)], {}, resolver=_fake_resolver)
        assert m.repos[0].name == "my-repo"
        assert m.repos[0].path == str(repo)
        assert m.repos[0].rev_source == "trunk"

    def test_shared_anchor_marks_only_named_repo(self, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        m = resolve_manifest(
            "t", ["lide-api", str(other)], self._registry(),
            shared={"lide-api"}, resolver=_fake_resolver,
        )
        assert m.repos[0].anchor_mode == "shared"
        assert m.repos[1].anchor_mode == "isolated"

    def test_shared_anchor_for_unlaunched_repo_is_an_error(self):
        with pytest.raises(RuntimeError, match="--shared-anchor names no launched repo"):
            resolve_manifest(
                "t", ["lide-api"], self._registry(),
                shared={"nope"}, resolver=_fake_resolver,
            )

    def test_resolver_receives_per_repo_mode(self, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        seen = {}

        def resolver(path, revision, mode):
            seen[path] = mode
            return "h"

        resolve_manifest(
            "t", ["lide-api", str(other)], self._registry(),
            shared={"lide-api"}, resolver=resolver,
        )
        assert seen == {"/host/lide-api": "shared", str(other): "isolated"}

    def test_registry_tilde_path_expanded(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        repos = {"r": RepoEntry(path="~/projects/r")}
        m = resolve_manifest("t", ["r"], repos, resolver=_fake_resolver)
        assert m.repos[0].path == str(tmp_path / "projects" / "r")

    def test_default_resolver_pins_a_real_jj_revision(self, jj_repo):
        base = jj_repo.run(["log", "-r", "@-", "--no-graph", "-T", "change_id"]).stdout.strip()
        repos = {"seed": RepoEntry(path=str(jj_repo.repo_root))}
        m = resolve_manifest("t", ["seed@@-"], repos)
        assert m.repos[0].anchor_base == base
        assert m.repos[0].rev_source == "arg"


class TestDiffManifests:
    def test_identical_is_empty_and_falsy(self):
        m = _manifest(_entry("a"), _entry("b"))
        diff = diff_manifests(m, m)
        assert diff == ManifestDiff(added=(), removed=(), changed=())
        assert not diff

    def test_added_and_removed(self):
        old = _manifest(_entry("a"), _entry("b"))
        new = _manifest(_entry("b"), _entry("c"))
        diff = diff_manifests(old, new)
        assert [r.name for r in diff.added] == ["c"]
        assert [r.name for r in diff.removed] == ["a"]
        assert diff.changed == ()
        assert diff

    @pytest.mark.parametrize("kwargs", [
        {"path": "/moved"},
        {"anchor": "b" * 40},
        {"mode": "shared"},
    ], ids=["path", "anchor_base", "anchor_mode"])
    def test_kept_name_with_changed_identity(self, kwargs):
        old = _manifest(_entry("a"))
        new = _manifest(_entry("a", **kwargs))
        diff = diff_manifests(old, new)
        assert diff.changed == ((old.repos[0], new.repos[0]),)
        assert diff.added == () and diff.removed == ()

    def test_rev_source_only_difference_is_not_a_change(self):
        # Provenance is informational: the same anchor reached via an explicit
        # arg instead of the registry must not force a recreate.
        diff = diff_manifests(
            _manifest(_entry("a", source="registry")),
            _manifest(_entry("a", source="arg")),
        )
        assert not diff


class TestKeepAttachedAnchors:
    """Repo-set recreate: kept repos reattach at their bookmarks, so the
    launched manifest must keep their old anchors (the new ones only become
    real after shutdown + start)."""

    def test_kept_repo_keeps_old_anchor_mode_and_source(self):
        old = _manifest(_entry("keep", anchor="b" * 40, source="trunk"))
        new = _manifest(
            _entry("keep", anchor="d" * 40, mode="shared", source="arg"),
            _entry("added", path="/host/new", anchor="e" * 40),
        )
        kept = keep_attached_anchors(old, new)
        assert kept.ticket == new.ticket
        by = {r.name: r for r in kept.repos}
        assert by["keep"] == _entry("keep", anchor="b" * 40, source="trunk")
        assert by["added"] == new.repos[1]

    def test_kept_name_with_changed_path_takes_the_new_anchor(self):
        """A path change points at a different store, where no bookmark exists
        to reattach -- the new anchor is real at once."""
        old = _manifest(_entry("r", path="/host/a", anchor="b" * 40))
        new = _manifest(_entry("r", path="/host/b", anchor="d" * 40, source="arg"))
        assert keep_attached_anchors(old, new) == new


class TestReadManifest:
    def _inspect(self, labels_json, rc=0, stderr=""):
        return type("R", (), {"returncode": rc, "stdout": labels_json, "stderr": stderr})()

    def test_reads_manifest_back_from_labels(self):
        m = _manifest(_entry("lide-api"))
        labels = manifest_labels(m, "t.lide-2600")
        with patch(
            "cld.manifest.subprocess.run", return_value=self._inspect(json.dumps(labels)),
        ):
            assert read_manifest("t.lide-2600") == m

    def test_missing_container_raises(self):
        with patch(
            "cld.manifest.subprocess.run",
            return_value=self._inspect("", rc=1, stderr="No such object"),
        ):
            with pytest.raises(RuntimeError, match="cannot inspect container"):
                read_manifest("t.gone")

    def test_non_ticket_container_raises(self):
        with patch(
            "cld.manifest.subprocess.run",
            return_value=self._inspect('{"org.cld.kind": "task-agent"}'),
        ):
            with pytest.raises(RuntimeError, match="no org.cld.manifest label"):
                read_manifest("a.x.t")
