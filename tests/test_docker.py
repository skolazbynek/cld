"""Tests for pure helpers in cld.docker."""

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from cld.cli import _reap_task_agent, app
from cld.config import Config, _load_dotenv
from cld.docker import (
    _INSPECT_FORMAT,
    _JJ_IDS,
    ANCHOR_BLOCKING_KINDS,
    MAILBOX_MOUNT,
    ORIGIN_MOUNT_LABEL,
    ORIGIN_MOUNT_VALUE,
    StoreLayout,
    TaskAgentSpec,
    allocate_task_agent_name,
    assert_task_agent_capacity,
    build_container_args,
    build_ticket_container_args,
    container_has_store_mounts,
    docker_occupant_list,
    docker_task_agent_list,
    find_repo_root,
    in_master_container,
    parse_peers_env,
    prepare_store_mounts,
    require_boot_api,
    resolve_anchor_checked,
    stage_home_ro,
    stage_plugin_marketplaces,
    stage_broker,
    stage_ssh_agent,
    store_mount_args,
    ticket_anchor_resolver,
    ticket_repo_bootstrap,
    to_host_path,
    warn_skipped_workspace_files,
    refuse_unprotected_containers,
)
from cld.manifest import RepoManifestEntry, TicketManifest
from cld.registry import RepoEntry
from cld.run import launch_run
from cld.ticket import _bookmark_tip, forget_session_state, restart_ticket, start_ticket
from cld.vcs import get_backend
from cld.vcs.anchor import resolve_anchor
from cld.vcs.jj import COMMONDIR_PLACEHOLDER, JJ_ID_FILES, check_git_dir, check_jj_ids


def _ps(names: str, rc: int = 0):
    return type("R", (), {"returncode": rc, "stdout": names, "stderr": ""})()


class TestFindJjRoot:
    def test_finds_in_start_dir(self, tmp_path):
        (tmp_path / ".jj").mkdir()
        assert find_repo_root(tmp_path) == tmp_path

    def test_walks_up_from_nested(self, tmp_path):
        (tmp_path / ".jj").mkdir()
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert find_repo_root(nested) == tmp_path

    def test_workspace_origin_env_takes_priority(self, tmp_path, monkeypatch):
        origin = tmp_path / "origin"
        (origin / ".jj").mkdir(parents=True)
        elsewhere = tmp_path / "elsewhere"
        (elsewhere / ".jj").mkdir(parents=True)
        monkeypatch.setenv("WORKSPACE_ORIGIN", str(origin))
        assert find_repo_root(elsewhere) == origin

    def test_exits_when_not_found(self, tmp_path):
        with pytest.raises(SystemExit):
            find_repo_root(tmp_path)


class TestLoadDotenv:
    def test_loads_key_value(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FOO", raising=False)
        env = tmp_path / ".env"
        env.write_text("FOO=bar\n")
        _load_dotenv(env)
        assert os.environ["FOO"] == "bar"

    def test_ignores_comments_and_blanks(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BAZ", raising=False)
        env = tmp_path / ".env"
        env.write_text("# comment\n\n   \nBAZ=qux\n")
        _load_dotenv(env)
        assert os.environ["BAZ"] == "qux"

    def test_strips_whitespace(self, tmp_path, monkeypatch):
        monkeypatch.delenv("K", raising=False)
        env = tmp_path / ".env"
        env.write_text("  K  =  v  \n")
        _load_dotenv(env)
        assert os.environ["K"] == "v"

    def test_missing_file_is_noop(self, tmp_path):
        _load_dotenv(tmp_path / "nonexistent")


class TestToHostPath:
    def test_workspace_current_not_translated(self):
        # /workspace/current lives inside the container's ephemeral filesystem
        # and has no host equivalent; to_host_path leaves it alone.
        cfg = Config(host_project_dir="/host/proj")
        assert to_host_path("/workspace/current/file.py", cfg) == "/workspace/current/file.py"

    def test_translates_workspace_origin(self):
        cfg = Config(host_project_dir="/host/proj")
        assert to_host_path("/workspace/origin/.jj", cfg) == "/host/proj/.jj"

    def test_translates_home(self):
        from cld.docker import CONTAINER_HOME
        cfg = Config(host_home="/home/host")
        assert to_host_path(f"{CONTAINER_HOME}/.claude", cfg) == "/home/host/.claude"

    def test_no_env_no_translation(self):
        assert to_host_path("/anywhere/else", Config()) == "/anywhere/else"

    def test_non_matching_path_untouched(self):
        cfg = Config(host_project_dir="/host/proj")
        assert to_host_path("/unrelated/path", cfg) == "/unrelated/path"

    def test_path_map_translates_per_repo_prefixes(self):
        cfg = Config(path_map={
            "/workspace/origin/lide-api": "/host/projects/lide-api",
            "/workspace/lide-2600/lide-api": "/host/projects/lide-api",
            "/home/claude": "/host/home",
        })
        assert to_host_path("/workspace/origin/lide-api/src/a.py", cfg) == "/host/projects/lide-api/src/a.py"
        assert to_host_path("/workspace/lide-2600/lide-api/src/a.py", cfg) == "/host/projects/lide-api/src/a.py"
        assert to_host_path("/home/claude/.claude", cfg) == "/host/home/.claude"

    def test_path_map_exact_prefix_match(self):
        cfg = Config(path_map={"/workspace/origin/lide-api": "/host/lide-api"})
        assert to_host_path("/workspace/origin/lide-api", cfg) == "/host/lide-api"

    def test_path_map_longest_prefix_wins(self):
        cfg = Config(path_map={
            "/workspace/origin": "/host/wrong",
            "/workspace/origin/lide-api": "/host/lide-api",
        })
        assert to_host_path("/workspace/origin/lide-api/x", cfg) == "/host/lide-api/x"

    def test_path_map_prefix_is_a_path_boundary(self):
        # A sibling repo whose name extends another's must not be hijacked.
        cfg = Config(path_map={"/workspace/origin/lide": "/host/lide"})
        assert to_host_path("/workspace/origin/lide-api/x", cfg) == "/workspace/origin/lide-api/x"

    def test_path_map_wins_over_scalar_pair(self):
        cfg = Config(
            host_project_dir="/host/scalar",
            path_map={"/workspace/origin": "/host/mapped"},
        )
        assert to_host_path("/workspace/origin/x", cfg) == "/host/mapped/x"

    def test_scalar_fallback_when_map_misses(self):
        cfg = Config(
            host_project_dir="/host/proj",
            path_map={"/workspace/lide-2600/lide-api": "/host/lide-api"},
        )
        assert to_host_path("/workspace/origin/x", cfg) == "/host/proj/x"


class TestStageHomeRo:
    @pytest.fixture(autouse=True)
    def _home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))

    def test_missing_returns_empty(self):
        assert stage_home_ro(".missing", Config()) == []

    def test_existing_file(self, tmp_path):
        (tmp_path / ".gitconfig").write_text("x")
        args = stage_home_ro(".gitconfig", Config())
        assert args == [
            "--mount",
            f"type=bind,src={tmp_path.resolve()}/.gitconfig,dst=/tmp/host-config/.gitconfig,readonly",
        ]

    def test_existing_dir(self, tmp_path):
        (tmp_path / ".config" / "anthropic").mkdir(parents=True)
        args = stage_home_ro(".config/anthropic", Config())
        assert args[0] == "--mount"
        assert args[1].endswith(",dst=/tmp/host-config/.config/anthropic,readonly")

    def test_nested_rel_preserved(self, tmp_path):
        (tmp_path / ".local" / "state" / "nvim").mkdir(parents=True)
        args = stage_home_ro(".local/state/nvim", Config())
        assert args[1].endswith(",dst=/tmp/host-config/.local/state/nvim,readonly")

    def test_to_host_path_translation(self, tmp_path):
        (tmp_path / ".bashrc").write_text("x")
        cfg = Config(host_home="/host/home")
        # tmp_path stands in for $CONTAINER_HOME via HOME env; to_host_path
        # only rewrites paths starting with CONTAINER_HOME, which tmp_path does
        # not, so the host-translated string is just the resolved tmp path.
        args = stage_home_ro(".bashrc", cfg)
        assert args[1].startswith(f"type=bind,src={tmp_path.resolve()}/.bashrc,")

    @pytest.mark.parametrize(
        "rel", ["/etc", "../outside", "a/../../outside", ".config/../../x", "", "."],
        ids=["absolute", "dotdot", "dotdot-inside", "dotdot-nested", "empty", "home"],
    )
    def test_unsafe_path_skipped_even_if_it_exists(self, tmp_path, rel, caplog):
        (tmp_path.parent / "outside").mkdir(exist_ok=True)
        (tmp_path / "a").mkdir()
        assert stage_home_ro(rel, Config()) == []
        assert "not a relative path below $HOME without '..'" in caplog.text

    @pytest.mark.parametrize("link_at", ["last", "middle"])
    def test_symlink_in_any_component_skipped(self, tmp_path, caplog, link_at):
        real = tmp_path / "real"
        (real / "sub").mkdir(parents=True)
        (real / "sub" / "f").write_text("x")
        if link_at == "last":
            (tmp_path / "link").symlink_to(real / "sub" / "f")
            rel = "link"
        else:
            (tmp_path / "link").symlink_to(real)
            rel = "link/sub/f"
        assert stage_home_ro(rel, Config()) == []
        assert f"{tmp_path / 'link'} is a symlink" in caplog.text

    @pytest.mark.parametrize("where", ["registry", "launch"])
    def test_repo_overlap_skipped(self, tmp_path, caplog, where):
        repo = tmp_path / "projects" / "api"
        (repo / "sub").mkdir(parents=True)
        (repo / "sub" / ".env").write_text("SECRET=1")
        cfg = Config(repos={"api": RepoEntry(path=str(repo))}) if where == "registry" else Config()
        roots = [repo] if where == "launch" else []
        assert stage_home_ro("projects/api/sub/.env", cfg, roots) == []
        assert f"it overlaps the repo {repo}" in caplog.text

    def test_repo_holding_the_path_skipped(self, tmp_path):
        (tmp_path / ".config").mkdir()
        assert stage_home_ro(".config", Config(), [tmp_path / ".config" / "repo"]) == []

    def test_sibling_of_a_repo_is_fine(self, tmp_path):
        (tmp_path / "projects" / "api").mkdir(parents=True)
        (tmp_path / ".gitconfig").write_text("x")
        assert stage_home_ro(".gitconfig", Config(), [tmp_path / "projects" / "api"])

    def test_comma_in_path_skipped(self, tmp_path, caplog):
        (tmp_path / "a,b").write_text("x")
        assert stage_home_ro("a,b", Config()) == []
        assert "comma or a quote" in caplog.text


@pytest.fixture
def other_containers(monkeypatch):
    """The records ``docker_occupant_list`` reports: none; append to add."""
    records: list[dict] = []
    monkeypatch.setattr("cld.docker.docker_occupant_list", lambda: records)
    return records


def _ro_mount(path) -> list[str]:
    return ["--mount", f"type=bind,src={path},dst={path},readonly"]


def _make_marketplace(path: Path) -> Path:
    (path / ".claude-plugin").mkdir(parents=True)
    (path / ".claude-plugin/marketplace.json").write_text("{}")
    return path


@pytest.mark.usefixtures("other_containers")
class TestStagePluginMarketplaces:
    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".claude" / "plugins").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        return home

    @staticmethod
    def _registry(home, **sources):
        (home / ".claude/plugins/known_marketplaces.json").write_text(
            json.dumps({name: {"source": src} for name, src in sources.items()})
        )

    def test_directory_source(self, home, tmp_path):
        mp = _make_marketplace(tmp_path / "mp")
        self._registry(home, local={"source": "directory", "path": str(mp)})
        assert stage_plugin_marketplaces(Config(), []) == _ro_mount(mp)

    def test_file_source_mounts_parent(self, home, tmp_path):
        mp = tmp_path / "mp"
        mp.mkdir()
        (mp / "marketplace.json").write_text("{}")
        self._registry(home, local={"source": "file", "path": str(mp / "marketplace.json")})
        assert stage_plugin_marketplaces(Config(), []) == _ro_mount(mp)

    def test_tilde_expanded(self, home):
        _make_marketplace(home / "mp")
        self._registry(home, local={"source": "directory", "path": "~/mp"})
        assert stage_plugin_marketplaces(Config(), []) == _ro_mount(f"{home}/mp")

    def test_other_sources_ignored(self, home):
        self._registry(
            home,
            gh={"source": "github", "repo": "a/b"},
            git={"source": "git", "url": "https://x/y.git"},
            url={"source": "url", "url": "https://x/m.json"},
        )
        assert stage_plugin_marketplaces(Config(), []) == []

    def test_missing_path_skipped(self, home, tmp_path):
        self._registry(home, local={"source": "directory", "path": str(tmp_path / "nope")})
        assert stage_plugin_marketplaces(Config(), []) == []

    def test_dedupe_across_host_and_repo_settings(self, home, tmp_path):
        mp = _make_marketplace(tmp_path / "mp")
        src = {"source": {"source": "directory", "path": str(mp)}}
        self._registry(home, local={"source": "directory", "path": str(mp)})
        (home / ".claude/settings.json").write_text(json.dumps({"extraKnownMarketplaces": {"a": src}}))
        repo = tmp_path / "repo"
        (repo / ".claude").mkdir(parents=True)
        (repo / ".claude/settings.json").write_text(json.dumps({"extraKnownMarketplaces": {"b": src}}))
        (repo / ".claude/settings.local.json").write_text(json.dumps({"extraKnownMarketplaces": {"c": src}}))
        assert stage_plugin_marketplaces(Config(), [repo]) == _ro_mount(mp)

    def test_user_settings_source_and_sorted_order(self, home, tmp_path):
        a, b = _make_marketplace(tmp_path / "a"), _make_marketplace(tmp_path / "b")
        self._registry(home, bb={"source": "directory", "path": str(b)})
        (home / ".claude/settings.json").write_text(json.dumps(
            {"extraKnownMarketplaces": {"aa": {"source": {"source": "directory", "path": str(a)}}}}
        ))
        assert stage_plugin_marketplaces(Config(), []) == [
            *_ro_mount(a), *_ro_mount(b),
        ]

    def test_malformed_json_tolerated(self, home, tmp_path):
        (home / ".claude/plugins/known_marketplaces.json").write_text("{not json")
        (home / ".claude/settings.json").write_text("[]")
        repo = tmp_path / "repo"
        (repo / ".claude").mkdir(parents=True)
        (repo / ".claude/settings.json").write_text('{"extraKnownMarketplaces": "x"}')
        assert stage_plugin_marketplaces(Config(), [repo]) == []


@pytest.mark.usefixtures("other_containers")
class TestPluginMarketplaceFilter:
    """Design section 4.2: a marketplace folder is mounted only after the marker,
    overlap and symlink checks."""

    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        return home

    @pytest.fixture
    def repo(self, tmp_path):
        repo = tmp_path / "work/repo"
        repo.mkdir(parents=True)
        return repo

    @staticmethod
    def _settings(settings_file: Path, kind: str, path: Path) -> None:
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(json.dumps(
            {"extraKnownMarketplaces": {"x": {"source": {"source": kind, "path": str(path)}}}}
        ))

    def _stage(self, home, repo, kind, path, cfg=None):
        self._settings(home / ".claude/settings.json", kind, path)
        return stage_plugin_marketplaces(cfg or Config(mailbox_root=str(home / ".cld/mailboxes")), [repo])

    def _assert_skipped(self, caplog, path, reason):
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == [f"plugin marketplace: '{path}' skipped: {reason}"]

    def test_valid_marketplace_is_mounted_read_only(self, home, repo, tmp_path):
        mp = _make_marketplace(tmp_path / "mp")
        assert self._stage(home, repo, "directory", mp) == _ro_mount(mp)

    def test_valid_file_source_is_mounted_by_parent(self, home, repo, tmp_path):
        mp = tmp_path / "mp"
        mp.mkdir()
        (mp / "marketplace.json").write_text("{}")
        assert self._stage(home, repo, "file", mp / "marketplace.json") == _ro_mount(mp)

    def test_folder_without_marker_is_skipped(self, home, repo, tmp_path, caplog):
        mp = tmp_path / "mp"
        mp.mkdir()
        assert self._stage(home, repo, "directory", mp) == []
        self._assert_skipped(
            caplog, mp, f"marker {mp}/.claude-plugin/marketplace.json is missing or not a regular file"
        )

    def test_marker_that_is_a_directory_is_skipped(self, home, repo, tmp_path, caplog):
        mp = tmp_path / "mp"
        (mp / ".claude-plugin/marketplace.json").mkdir(parents=True)
        assert self._stage(home, repo, "directory", mp) == []
        self._assert_skipped(
            caplog, mp, f"marker {mp}/.claude-plugin/marketplace.json is missing or not a regular file"
        )

    def test_file_source_with_another_name_is_skipped(self, home, repo, tmp_path, caplog):
        mp = tmp_path / "mp"
        mp.mkdir()
        (mp / "other.json").write_text("{}")
        assert self._stage(home, repo, "file", mp / "other.json") == []
        self._assert_skipped(caplog, mp / "other.json", "a file source must be named marketplace.json")

    def test_missing_folder_is_skipped(self, home, repo, tmp_path, caplog):
        assert self._stage(home, repo, "directory", tmp_path / "nope") == []
        self._assert_skipped(caplog, tmp_path / "nope", "not found")

    def test_folder_inside_a_repo_is_skipped(self, home, repo, caplog):
        mp = _make_marketplace(repo / "tools/mp")
        assert self._stage(home, repo, "directory", mp) == []
        self._assert_skipped(caplog, mp, f"it overlaps the repo {repo}")

    def test_repo_itself_is_skipped(self, home, repo, caplog):
        _make_marketplace(repo)
        assert self._stage(home, repo, "directory", repo) == []
        self._assert_skipped(caplog, repo, f"it overlaps the repo {repo}")

    def test_folder_containing_a_repo_is_skipped(self, home, repo, tmp_path, caplog):
        mp = _make_marketplace(tmp_path / "work")
        assert self._stage(home, repo, "directory", mp) == []
        self._assert_skipped(caplog, mp, f"it overlaps the repo {repo}")

    def test_folder_overlapping_a_registry_repo_is_skipped(self, home, repo, tmp_path, caplog):
        registered = tmp_path / "other/registered"
        registered.mkdir(parents=True)
        mp = _make_marketplace(registered / "mp")
        cfg = Config(repos={"registered": RepoEntry(path=str(registered))})
        assert self._stage(home, repo, "directory", mp, cfg) == []
        self._assert_skipped(caplog, mp, f"it overlaps the repo {registered}")

    def test_home_itself_is_skipped(self, home, repo, caplog):
        _make_marketplace(home)
        assert self._stage(home, repo, "directory", home) == []
        self._assert_skipped(caplog, home, "it is $HOME or contains it")

    def test_folder_containing_home_is_skipped(self, home, repo, tmp_path, caplog):
        mp = _make_marketplace(tmp_path)
        assert self._stage(home, repo, "directory", mp) == []
        self._assert_skipped(caplog, mp, "it is $HOME or contains it")

    def test_folder_inside_home_is_mounted(self, home, repo):
        mp = _make_marketplace(home / "plugins/mp")
        assert self._stage(home, repo, "directory", mp) == _ro_mount(mp)

    def test_symlinked_folder_is_skipped(self, home, repo, tmp_path, caplog):
        _make_marketplace(tmp_path / "real")
        link = tmp_path / "link"
        link.symlink_to(tmp_path / "real")
        assert self._stage(home, repo, "directory", link) == []
        self._assert_skipped(caplog, link, f"{link} is a symlink")

    def test_symlink_into_a_repo_is_skipped(self, home, repo, tmp_path, caplog):
        mp = _make_marketplace(repo / "mp")
        link = tmp_path / "link"
        link.symlink_to(mp)
        assert self._stage(home, repo, "directory", link) == []
        self._assert_skipped(caplog, link, f"{link} is a symlink")

    def test_symlinked_marker_is_skipped(self, home, repo, tmp_path, caplog):
        mp = tmp_path / "mp"
        (mp / ".claude-plugin").mkdir(parents=True)
        (tmp_path / "real.json").write_text("{}")
        (mp / ".claude-plugin/marketplace.json").symlink_to(tmp_path / "real.json")
        assert self._stage(home, repo, "directory", mp) == []
        self._assert_skipped(caplog, mp, f"{mp}/.claude-plugin/marketplace.json is a symlink")

    def test_symlinked_marker_directory_is_skipped(self, home, repo, tmp_path, caplog):
        mp = tmp_path / "mp"
        mp.mkdir()
        _make_marketplace(tmp_path / "real")
        (mp / ".claude-plugin").symlink_to(tmp_path / "real/.claude-plugin")
        assert self._stage(home, repo, "directory", mp) == []
        self._assert_skipped(caplog, mp, f"{mp}/.claude-plugin is a symlink")

    def test_symlinked_file_source_is_skipped(self, home, repo, tmp_path, caplog):
        mp = tmp_path / "mp"
        mp.mkdir()
        (tmp_path / "real.json").write_text("{}")
        (mp / "marketplace.json").symlink_to(tmp_path / "real.json")
        assert self._stage(home, repo, "file", mp / "marketplace.json") == []
        self._assert_skipped(caplog, mp / "marketplace.json", f"{mp}/marketplace.json is a symlink")

    def test_file_source_in_a_symlinked_folder_is_skipped(self, home, repo, tmp_path, caplog):
        real = tmp_path / "real"
        real.mkdir()
        (real / "marketplace.json").write_text("{}")
        link = tmp_path / "link"
        link.symlink_to(real)
        assert self._stage(home, repo, "file", link / "marketplace.json") == []
        self._assert_skipped(caplog, link / "marketplace.json", f"{link} is a symlink")

    def test_path_from_repo_settings_is_never_mounted(self, home, repo, tmp_path, caplog):
        mp = _make_marketplace(tmp_path / "mp")
        settings = repo / ".claude/settings.json"
        self._settings(settings, "directory", mp)
        assert stage_plugin_marketplaces(Config(), [repo]) == []
        self._assert_skipped(caplog, mp, f"listed in {settings}, which the repo controls")

    def test_path_from_repo_local_settings_is_never_mounted(self, home, repo, tmp_path, caplog):
        mp = _make_marketplace(tmp_path / "mp")
        settings = repo / ".claude/settings.local.json"
        self._settings(settings, "directory", mp)
        assert stage_plugin_marketplaces(Config(), [repo]) == []
        self._assert_skipped(caplog, mp, f"listed in {settings}, which the repo controls")

    def test_user_settings_path_also_in_repo_settings_mounts_without_warning(
        self, home, repo, tmp_path, caplog
    ):
        mp = _make_marketplace(tmp_path / "mp")
        self._settings(repo / ".claude/settings.json", "directory", mp)
        assert self._stage(home, repo, "directory", mp) == _ro_mount(mp)
        assert not caplog.records

    def test_same_bad_path_warns_once(self, home, repo, tmp_path, caplog):
        mp = tmp_path / "mp"
        mp.mkdir()
        (home / ".claude/plugins").mkdir()
        (home / ".claude/plugins/known_marketplaces.json").write_text(
            json.dumps({"a": {"source": {"source": "directory", "path": str(mp)}}})
        )
        assert self._stage(home, repo, "directory", mp) == []
        assert len(caplog.records) == 1

    # Docker resolves the source again at every `docker run` and `docker start`, so a
    # folder an agent container can write could be swapped for a link after the check.

    @pytest.mark.parametrize(
        "where",
        [
            "claude-dir", "ticket-notes-dir", "mailbox-root", "custom-mailbox-root", "repo-git-dir",
            "other-container-repo", "registry-repo-git-dir",
        ],
    )
    def test_folder_an_agent_can_swap_is_skipped(self, home, repo, tmp_path, caplog, other_containers, where):
        cfg = Config(mailbox_root=str(home / ".cld/mailboxes"))
        match where:
            case "claude-dir":
                mp, writable = home / ".claude/plugins/mp", f"{home}/.claude, which agent containers write"
            case "ticket-notes-dir":
                mp, writable = home / ".ticket-notes/mp", f"{home}/.ticket-notes, which agent containers write"
            case "mailbox-root":
                mp, writable = home / ".cld/mailboxes/mp", f"{home}/.cld/mailboxes, which agent containers write"
            case "custom-mailbox-root":
                cfg = Config(mailbox_root=str(tmp_path / "mail"))
                mp, writable = tmp_path / "mail/mp", f"{tmp_path}/mail, which agent containers write"
            case "repo-git-dir":
                mp, writable = repo / ".git/mp", None
            case "other-container-repo":
                other = tmp_path / "other"
                other_containers.append({"repo_root": str(other)})
                mp, writable = other / ".jj/mp", None
            case "registry-repo-git-dir":
                registered = tmp_path / "registered"
                cfg = Config(repos={"registered": RepoEntry(path=str(registered))})
                mp, writable = registered / ".git/mp", None
        _make_marketplace(mp)
        assert self._stage(home, repo, "directory", mp, cfg) == []
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert warnings[0].startswith(f"plugin marketplace: '{mp}' skipped: it overlaps ")
        assert writable is None or warnings[0].endswith(writable)

    def test_folder_holding_the_mailbox_root_is_skipped(self, home, repo, caplog):
        mp = _make_marketplace(home / ".cld")
        assert self._stage(home, repo, "directory", mp) == []
        self._assert_skipped(caplog, mp, f"it overlaps {home}/.cld/mailboxes, which agent containers write")

    def test_folder_below_a_symlinked_directory_is_skipped(self, home, repo, tmp_path, caplog):
        _make_marketplace(tmp_path / "real/mp")
        (tmp_path / "link").symlink_to(tmp_path / "real")
        assert self._stage(home, repo, "directory", tmp_path / "link/mp") == []
        self._assert_skipped(
            caplog, tmp_path / "link/mp",
            f"a symlink is in its path (the real path is {tmp_path}/real/mp)",
        )

    def test_symlink_into_the_claude_dir_is_skipped(self, home, repo, tmp_path, caplog):
        mp = _make_marketplace(home / ".claude/mp")
        link = tmp_path / "link"
        link.symlink_to(mp)
        assert self._stage(home, repo, "directory", link) == []
        self._assert_skipped(caplog, link, f"{link} is a symlink")

    def test_relative_path_is_skipped(self, home, repo, tmp_path, monkeypatch, caplog):
        _make_marketplace(tmp_path / "mp")
        monkeypatch.chdir(tmp_path)
        assert self._stage(home, repo, "directory", Path("mp")) == []
        self._assert_skipped(caplog, Path("mp"), "not an absolute path")

    @pytest.mark.parametrize("char", [",", '"'], ids=["comma", "quote"])
    def test_path_that_would_split_the_mount_value_is_skipped(self, home, repo, tmp_path, caplog, char):
        mp = _make_marketplace(tmp_path / f"a{char}b")
        assert self._stage(home, repo, "directory", mp) == []
        self._assert_skipped(
            caplog, mp, f"'{mp}' contains a comma or a quote, which would split the --mount value",
        )

    def test_mount_is_a_read_only_bind_that_must_exist(self, home, repo, tmp_path):
        mp = _make_marketplace(tmp_path / "mp")
        assert self._stage(home, repo, "directory", mp) == [
            "--mount", f"type=bind,src={mp},dst={mp},readonly",
        ]

    def test_nested_launch_mounts_the_translated_host_path(self, home, repo, tmp_path):
        mp = _make_marketplace(tmp_path / "mp")
        cfg = Config(path_map={str(tmp_path): "/host/data"})
        assert self._stage(home, repo, "directory", mp, cfg) == _ro_mount("/host/data/mp")

    def test_docker_is_not_asked_without_a_candidate(self, home, repo, monkeypatch):
        def fail():
            raise AssertionError("docker consulted")

        monkeypatch.setattr("cld.docker.docker_occupant_list", fail)
        assert stage_plugin_marketplaces(Config(), [repo]) == []


class TestStageSshAgent:
    @pytest.fixture
    def sock(self, tmp_path):
        import socket
        path = tmp_path / "agent.sock"
        s = socket.socket(socket.AF_UNIX)
        s.bind(str(path))
        yield path
        s.close()

    def test_known_hosts_staged_with_agent(self, tmp_path, monkeypatch, sock):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".ssh").mkdir()
        (tmp_path / ".ssh" / "known_hosts").write_text("h")
        args = stage_ssh_agent(Config(ssh_auth_sock=str(sock)))
        assert f"{sock}:/run/host-ssh-agent.sock" in args
        assert any(a.endswith(":/tmp/host-config/.ssh/known_hosts:ro") for a in args)

    def test_no_known_hosts_on_host(self, tmp_path, monkeypatch, sock):
        monkeypatch.setenv("HOME", str(tmp_path))
        args = stage_ssh_agent(Config(ssh_auth_sock=str(sock)))
        assert f"{sock}:/run/host-ssh-agent.sock" in args
        assert not any("/tmp/host-config/.ssh/known_hosts" in a for a in args)

    def test_disabled_stages_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".ssh").mkdir()
        (tmp_path / ".ssh" / "known_hosts").write_text("h")
        assert stage_ssh_agent(Config(ssh_auth_sock="")) == []


class TestEnsureImageNested:
    def test_raises_inside_hub_container_no_daemon(self, monkeypatch):
        # A container has no docker daemon (socket removed), so ensure_image
        # must never be reached from one; if it is, it fails clearly rather
        # than touching docker. HUB_MODE is the flag it still checks -- see
        # in_master_container().
        monkeypatch.setenv("HUB_MODE", "1")
        import cld.docker as docker_mod
        from pathlib import Path
        from unittest.mock import patch
        with patch.object(docker_mod.subprocess, "run") as run_mock:
            with pytest.raises(RuntimeError, match="cannot be ensured from inside a master"):
                docker_mod.ensure_image(
                    "missing:img",
                    Path("/opt/cld/imgs/x/Dockerfile"),
                    Path("/opt/cld"),
                )
        run_mock.assert_not_called()  # never touches the (absent) daemon


class TestInMasterContainer:
    def test_true_when_hub_mode_set(self, monkeypatch):
        # No launcher sets HUB_MODE since the master role was removed; the flag
        # (and the broker dispatch keyed on it) stays until that rework.
        monkeypatch.setenv("HUB_MODE", "1")
        assert in_master_container() is True

    def test_false_when_only_agent_mode_set(self, monkeypatch):
        # Every persistent role gets AGENT_MODE; that must not read as a hub.
        monkeypatch.setenv("AGENT_MODE", "1")
        assert in_master_container() is False

    def test_false_when_unset(self):
        assert in_master_container() is False


class TestStageBroker:
    def test_no_key_is_noop(self):
        assert stage_broker(Config()) == []

    def test_missing_key_returns_empty(self, tmp_path):
        cfg = Config(broker_key=str(tmp_path / "nope"))
        assert stage_broker(cfg) == []

    def test_key_only_wires_gateway_and_endpoint(self, tmp_path):
        key = tmp_path / "broker_key"
        key.write_text("k")
        cfg = Config(broker_key=str(key), broker_endpoint="host.docker.internal:2222")
        args = stage_broker(cfg)
        assert args[:2] == ["--add-host", "host.docker.internal:host-gateway"]
        assert "-v" in args and f"{key}:/run/secrets/broker-key:ro" in args
        assert "-e" in args and "CLD_BROKER_ENDPOINT=host.docker.internal:2222" in args
        # No known_hosts mount when it isn't configured.
        assert not any("broker-known-hosts" in a for a in args)

    def test_known_hosts_mounted_when_present(self, tmp_path):
        key = tmp_path / "broker_key"; key.write_text("k")
        known = tmp_path / "known_hosts"; known.write_text("h")
        cfg = Config(broker_key=str(key), broker_known_hosts=str(known))
        args = stage_broker(cfg)
        assert f"{known}:/run/secrets/broker-known-hosts:ro" in args

    def test_endpoint_override_passed_through(self, tmp_path):
        key = tmp_path / "broker_key"; key.write_text("k")
        cfg = Config(broker_key=str(key), broker_endpoint="me@1.2.3.4:2200")
        args = stage_broker(cfg)
        assert "CLD_BROKER_ENDPOINT=me@1.2.3.4:2200" in args


class TestDockerKindList:
    """Enumeration is label-driven; `docker inspect` reads .Config.Labels."""

    def test_inspect_format_uses_config_labels(self):
        # Regression: a top-level .Labels field does not exist on a container and
        # fails the whole template, which would silently drop every container.
        assert ".Config.Labels" in _INSPECT_FORMAT
        assert "index .Labels" not in _INSPECT_FORMAT

    def test_records_carry_kind_parent_task(self):
        inspect = _ps("/home/u/repoA|a.repoA.add-oauth|task-agent|cld_master_repoA_ab12|add-oauth||\n")
        with patch("cld.docker.subprocess.run", side_effect=[_ps("a.repoA.add-oauth\n"), inspect]):
            assert docker_task_agent_list() == [{
                "name": "a.repoA.add-oauth",
                "repo_root": "/home/u/repoA",
                "session": "a.repoA.add-oauth",
                "kind": "task-agent",
                "parent": "cld_master_repoA_ab12",
                "task": "add-oauth",
                "anchor": "",
                "anchor_mode": "",
            }]

    def test_missing_labels_become_empty(self):
        with patch("cld.docker.subprocess.run", side_effect=[_ps("c1\n"), _ps("/repo|c1|agent|||\n")]):
            rec = docker_task_agent_list()[0]
        assert (rec["parent"], rec["task"]) == ("", "")

    def test_running_only_filters_docker_side(self):
        calls = []

        def spy(cmd, **_kwargs):
            calls.append(cmd)
            return _ps("")

        with patch("cld.docker.subprocess.run", side_effect=spy):
            docker_task_agent_list(running_only=True)
        assert "status=running" in calls[0]
        assert "label=org.cld.kind=task-agent" in calls[0]

    def test_no_status_filter_by_default(self):
        calls = []

        def spy(cmd, **_kwargs):
            calls.append(cmd)
            return _ps("")

        with patch("cld.docker.subprocess.run", side_effect=spy):
            docker_task_agent_list()
        assert "status=running" not in calls[0]

    def test_ps_failure_returns_empty(self):
        with patch("cld.docker.subprocess.run", return_value=_ps("", rc=1)):
            assert docker_task_agent_list() == []

    def test_inspect_failure_skips_container(self):
        with patch("cld.docker.subprocess.run", side_effect=[_ps("c1\nc2\n"), _ps("", rc=1), _ps("/r|c2|agent||\n")]):
            assert [c["name"] for c in docker_task_agent_list()] == ["c2"]


class TestAllocateTaskAgentName:
    def test_returns_base_when_free(self, tmp_path):
        with patch("cld.docker._docker_status", return_value="absent"):
            assert allocate_task_agent_name("lide-1", "task") == "a.lide-1.task"

    def test_no_ticket_is_host(self):
        with patch("cld.docker._docker_status", return_value="absent"):
            assert allocate_task_agent_name(None, "task") == "a._host.task"

    def test_invalid_slug_rejected(self):
        with pytest.raises(ValueError, match="invalid task slug"):
            allocate_task_agent_name("lide-1", "Bad Slug")

    def test_skips_taken_names(self, tmp_path):
        with patch("cld.docker._docker_status", side_effect=["running", "stopped", "absent"]):
            assert allocate_task_agent_name("lide-1", "task") == "a.lide-1.task-3"


class TestTaskAgentSpec:
    def test_peers_env_encoding(self):
        spec = TaskAgentSpec(slug="t", peers={"a.r.b": 5, "a.r.a": 15})
        assert spec.peers_env() == "a.r.a:15,a.r.b:5"

    def test_peers_env_empty(self):
        assert TaskAgentSpec(slug="t").peers_env() == ""


@pytest.mark.usefixtures("stub_store_preflight")
class TestBuildContainerArgsTaskAgent:
    """Task-agent role wiring. No daemon needed -- build_container_args only
    inspects the filesystem and cfg."""

    def _args(self, tmp_path, **kwargs):
        spec = TaskAgentSpec(
            slug="add-oauth",
            parent_master="cld_master_repoA_ab12",
            deliverable_branch="add-oauth-login",
            peers={"a.repoA.contract": 15},
            **kwargs,
        )
        return build_container_args(
            tmp_path, "a.repoA.add-oauth", Config(mailbox_root=str(tmp_path / "mb")),
            task_agent=spec,
        )

    def test_labels_and_name(self, tmp_path):
        args = self._args(tmp_path)
        assert "--name" in args and "a.repoA.add-oauth" in args
        assert "org.cld.kind=task-agent" in args
        assert "org.cld.task=add-oauth" in args
        assert "org.cld.parent-master=cld_master_repoA_ab12" in args
        assert f"org.cld.session=a.repoA.add-oauth" in args

    def test_agent_mode_plus_task_modifier(self, tmp_path):
        args = self._args(tmp_path)
        assert "AGENT_MODE=1" in args
        assert "TASK_AGENT_MODE=1" in args
        assert "HUB_MODE=1" not in args
        assert not any(a.startswith("org.cld.targets=") for a in args)

    def test_spawn_facts_in_env(self, tmp_path):
        args = self._args(tmp_path)
        assert "AGENT_DELIVERABLE_BRANCH=add-oauth-login" in args
        assert "AGENT_PEERS=a.repoA.contract:15" in args
        assert "AGENT_PARENT_MASTER=cld_master_repoA_ab12" in args
        assert "AGENT_TASK_SLUG=add-oauth" in args

    def test_turn_cap_propagated(self, tmp_path):
        """In-container Config.from_env() sees no host TOML, so the cap has to be passed."""
        args = build_container_args(
            tmp_path, "a.r.t",
            Config(mailbox_root=str(tmp_path / "mb"), agent_max_turns=200),
            task_agent=TaskAgentSpec(slug="t"),
        )
        assert "CLD_AGENT_MAX_TURNS=200" in args

    def test_budget_fallbacks_propagated(self, tmp_path):
        spec = TaskAgentSpec(slug="t")
        args = build_container_args(
            tmp_path, "a.r.t",
            Config(mailbox_root=str(tmp_path / "mb"), peer_absolute_limit=3, root_ask_limit=5),
            task_agent=spec,
        )
        assert "CLD_PEER_ABSOLUTE_LIMIT=3" in args
        assert "CLD_ROOT_ASK_LIMIT=5" in args

    def test_persistent_not_ephemeral(self, tmp_path):
        assert "--rm" not in self._args(tmp_path)

    def test_mailbox_mounted(self, tmp_path):
        args = self._args(tmp_path)
        assert any(a.endswith(f":{MAILBOX_MOUNT}:rw") for a in args)

    def test_broker_key_wired(self, tmp_path):
        """Task-agents get the broker too now -- gated by policy in their
        persona prompt (must ask master first), not by wiring."""
        key = tmp_path / "broker_key"
        key.write_text("k")
        args = build_container_args(
            tmp_path, "a.r.t",
            Config(mailbox_root=str(tmp_path / "mb"), broker_key=str(key)),
            task_agent=TaskAgentSpec(slug="t"),
        )
        assert any("broker-key" in a for a in args)


@pytest.mark.usefixtures("stub_store_preflight")
class TestBuildContainerArgsPluginMarketplaces:
    def test_mount_included(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".claude/plugins").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        mp = _make_marketplace(tmp_path / "mp")
        (home / ".claude/plugins/known_marketplaces.json").write_text(
            json.dumps({"local": {"source": {"source": "directory", "path": str(mp)}}})
        )
        repo = tmp_path / "repo"
        repo.mkdir()
        args = build_container_args(repo, "run.x", Config(mailbox_root=str(tmp_path / "mb")))
        assert _ro_mount(mp)[1] in _mount_values(args)


@pytest.mark.usefixtures("stub_store_preflight")
class TestBuildContainerArgsTicketNotes:
    def test_created_and_mounted_rw(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        repo = tmp_path / "repo"
        repo.mkdir()
        args = build_container_args(repo, "run.x", Config(mailbox_root=str(tmp_path / "mb")))
        assert (home / ".ticket-notes").is_dir()
        assert f"{home}/.ticket-notes:/home/claude/.ticket-notes:rw" in args


@pytest.mark.usefixtures("stub_store_preflight")
class TestBuildContainerArgsBrokerWiring:
    """Broker key reaches the one persistent v1 role left -- the task-agent.
    Access-time policy (master authorization) lives in its persona prompt, not
    in this wiring."""

    def _cfg(self, tmp_path):
        key = tmp_path / "broker_key"
        key.write_text("k")
        return Config(mailbox_root=str(tmp_path / "mb"), broker_key=str(key))

    def test_task_agent_role_gets_broker(self, tmp_path):
        args = build_container_args(
            tmp_path, "a.r.t", self._cfg(tmp_path), task_agent=TaskAgentSpec(slug="t"),
        )
        assert any("broker-key" in a for a in args)

    def test_run_role_gets_no_broker(self, tmp_path):
        """`cld run` (no persistent role) never gets the broker -- it's a
        one-shot, unattended container."""
        args = build_container_args(tmp_path, "run.x", self._cfg(tmp_path))
        assert not any("broker-key" in a for a in args)

    def test_run_devcontainer_not_named(self, tmp_path):
        """Only the persistent roles get a name; `cld run` stays anonymous."""
        args = build_container_args(tmp_path, "run.x", self._cfg(tmp_path))
        assert "--name" not in args


def _tasks(*specs):
    """Fake docker_task_agent_list records: (name, parent, repo_root, task)."""
    return [
        {"name": n, "parent": p, "repo_root": r, "task": t, "session": n, "kind": "task-agent"}
        for n, p, r, t in specs
    ]


class TestAssertTaskAgentCapacity:
    def test_under_cap_passes(self):
        with patch("cld.docker.docker_task_agent_list", return_value=_tasks(("a", "m1", "/r", "t"))):
            assert_task_agent_capacity(Config(max_task_agents=2), "m1")

    def test_at_cap_raises_naming_agents(self):
        running = _tasks(("a", "m1", "/r", "task-a"), ("b", "m1", "/r", "task-b"))
        with patch("cld.docker.docker_task_agent_list", return_value=running):
            with pytest.raises(RuntimeError, match="task-agent cap reached") as e:
                assert_task_agent_capacity(Config(max_task_agents=2), "m1")
        assert "task-a" in str(e.value) and "b (task-b)" in str(e.value)

    def test_other_masters_do_not_count(self):
        running = _tasks(("a", "m2", "/r", "t"), ("b", "m2", "/r", "t"))
        with patch("cld.docker.docker_task_agent_list", return_value=running):
            assert_task_agent_capacity(Config(max_task_agents=2), "m1")

    def test_host_launched_group_counted_on_its_own(self):
        running = _tasks(("a", "", "/r", "t"), ("b", "", "/r", "t"))
        with patch("cld.docker.docker_task_agent_list", return_value=running) as m:
            with pytest.raises(RuntimeError, match="host-launched agents"):
                assert_task_agent_capacity(Config(max_task_agents=2), "")
        assert m.call_args.kwargs == {"running_only": True}


def _occupants(*specs, kind="task-agent"):
    """Fake docker_occupant_list records: (name, repo_root, anchor_base, mode).
    Paths are resolved like the real lister resolves them at record-build time."""
    return [
        {
            "name": n, "repo_root": str(Path(r).resolve()), "anchor_base": a,
            "session": n, "mode": m, "kind": kind,
        }
        for n, r, a, m in specs
    ]


class TestResolveAnchorChecked:
    """Overlap check against a real jj repo; the occupant list is faked."""

    def _commits(self, jj_repo):
        base = resolve_anchor(jj_repo, "@-")
        jj_repo.run(["new", base])
        (jj_repo.repo_root / "live.txt").write_text("live\n")
        jj_repo.run(["commit", "-m", "live agent anchor"])
        live_anchor = resolve_anchor(jj_repo, "@-")
        (jj_repo.repo_root / "more.txt").write_text("more\n")
        jj_repo.run(["commit", "-m", "live agent work"])
        inside = resolve_anchor(jj_repo, "@-")
        return base, live_anchor, inside

    def _fleet(self, tmp_path, jj_repo, anchor, name="a.r.live", anchor_mode="isolated", kind="task-agent"):
        cfg = Config(mailbox_root=str(tmp_path / "mb"))
        records = _occupants((name, str(jj_repo.repo_root), anchor, anchor_mode), kind=kind)
        return cfg, records

    def test_shared_base_passes_with_live_sibling(self, tmp_path, jj_repo):
        base, live_anchor, _ = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, live_anchor)
        with patch("cld.docker.docker_occupant_list", return_value=records):
            assert resolve_anchor_checked(cfg, jj_repo.repo_root, base) == base

    def test_inside_live_stack_refused_naming_owner(self, tmp_path, jj_repo):
        _, live_anchor, inside = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, live_anchor)
        with patch("cld.docker.docker_occupant_list", return_value=records):
            with pytest.raises(RuntimeError, match="inside the live reach") as e:
                resolve_anchor_checked(cfg, jj_repo.repo_root, inside)
        assert "a.r.live" in str(e.value)

    def test_equal_to_live_anchor_refused(self, tmp_path, jj_repo):
        _, live_anchor, _ = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, live_anchor)
        with patch("cld.docker.docker_occupant_list", return_value=records):
            with pytest.raises(RuntimeError, match="inside the live reach"):
                resolve_anchor_checked(cfg, jj_repo.repo_root, live_anchor)

    def test_other_repo_agents_ignored(self, tmp_path, jj_repo):
        _, live_anchor, inside = self._commits(jj_repo)
        elsewhere = _occupants(("a.r.live", "/some/other/repo", live_anchor, "isolated"))
        cfg = Config(mailbox_root=str(tmp_path / "mb"))
        with patch("cld.docker.docker_occupant_list", return_value=elsewhere):
            assert resolve_anchor_checked(cfg, jj_repo.repo_root, inside) == inside

    def test_symlinked_repo_path_does_not_bypass_check(self, tmp_path, jj_repo):
        """The caller's repo path is realpath-normalized before matching records."""
        _, live_anchor, inside = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, live_anchor)
        link = tmp_path / "repo-link"
        link.symlink_to(jj_repo.repo_root)
        with patch("cld.docker.docker_occupant_list", return_value=records):
            with pytest.raises(RuntimeError, match="inside the live reach"):
                resolve_anchor_checked(cfg, link, inside)

    def test_missing_anchor_label_ignored(self, tmp_path, jj_repo):
        _, _, inside = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, "")
        with patch("cld.docker.docker_occupant_list", return_value=records):
            assert resolve_anchor_checked(cfg, jj_repo.repo_root, inside) == inside

    def test_no_live_agents_passes(self, tmp_path, jj_repo):
        _, _, inside = self._commits(jj_repo)
        with patch("cld.docker.docker_occupant_list", return_value=[]):
            cfg = Config(mailbox_root=str(tmp_path / "mb"))
            assert resolve_anchor_checked(cfg, jj_repo.repo_root, inside) == inside

    def test_unverifiable_occupant_blocks_headless_caller(self, tmp_path, jj_repo):
        # Fail-closed: an anchor the store cannot resolve means the check
        # cannot rule the overlap out -- refuse rather than silently pass.
        _, _, inside = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, "dead" * 10)
        with patch("cld.docker.docker_occupant_list", return_value=records):
            with pytest.raises(RuntimeError, match="could not verify"):
                resolve_anchor_checked(cfg, jj_repo.repo_root, inside, caller_kind="task-agent")

    def test_unverifiable_occupant_warns_ticket_caller(self, tmp_path, jj_repo, caplog):
        _, _, inside = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, "dead" * 10)
        with caplog.at_level("WARNING"), patch("cld.docker.docker_occupant_list", return_value=records):
            assert resolve_anchor_checked(
                cfg, jj_repo.repo_root, inside, caller_kind="ticket",
            ) == inside
        assert "could not verify" in caplog.text

    def _broken_ticket_record(self):
        """The unverifiable record docker_occupant_list builds for a ticket
        with an unreadable manifest: no repo paths, so it must count against
        EVERY repo checked."""
        return {
            "name": "t.broken", "repo_root": "", "anchor_base": "",
            "session": "t.broken", "mode": "isolated", "kind": "ticket",
            "manifest_error": "no label",
        }

    def test_unreadable_ticket_manifest_blocks_headless_caller(self, tmp_path, jj_repo):
        _, _, inside = self._commits(jj_repo)
        cfg = Config(mailbox_root=str(tmp_path / "mb"))
        with patch("cld.docker.docker_occupant_list", return_value=[self._broken_ticket_record()]):
            with pytest.raises(RuntimeError, match="could not verify") as e:
                resolve_anchor_checked(cfg, jj_repo.repo_root, inside, caller_kind="task-agent")
        assert "unreadable manifest" in str(e.value)
        assert "t.broken" in str(e.value)

    def test_unreadable_ticket_manifest_warns_ticket_caller(self, tmp_path, jj_repo, caplog):
        _, _, inside = self._commits(jj_repo)
        cfg = Config(mailbox_root=str(tmp_path / "mb"))
        with caplog.at_level("WARNING"), \
             patch("cld.docker.docker_occupant_list", return_value=[self._broken_ticket_record()]):
            assert resolve_anchor_checked(
                cfg, jj_repo.repo_root, inside, caller_kind="ticket",
            ) == inside
        assert "unreadable manifest" in caplog.text
        assert "t.broken" in caplog.text

    def test_git_backend_skips_check(self, tmp_path, git_repo):
        head = git_repo.resolve_revision("HEAD")
        cfg = Config(mailbox_root=str(tmp_path / "mb"))
        with patch("cld.docker.docker_occupant_list") as m:
            assert resolve_anchor_checked(cfg, git_repo.repo_root, head) == head
        m.assert_not_called()

    def test_isolated_sibling_of_live_base_passes(self, tmp_path, jj_repo):
        """Two isolated agents off the same base don't collide (default mode)."""
        base, live_anchor, _ = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, live_anchor, anchor_mode="isolated")
        with patch("cld.docker.docker_occupant_list", return_value=records):
            assert resolve_anchor_checked(cfg, jj_repo.repo_root, base, "isolated") == base

    def test_exclude_session_drops_own_ticket_record(self, tmp_path, jj_repo):
        """A ticket reanchoring itself must not be blocked or warned about its
        own live reach -- exclude_session drops its own occupant record."""
        _, live_anchor, inside = self._commits(jj_repo)
        cfg, records = self._fleet(
            tmp_path, jj_repo, live_anchor, name="t.self", kind="ticket",
        )
        with patch("cld.docker.docker_occupant_list", return_value=records):
            assert resolve_anchor_checked(
                cfg, jj_repo.repo_root, inside, caller_kind="ticket",
                exclude_session="t.self",
            ) == inside

    def test_exclude_session_leaves_other_occupants_blocking(self, tmp_path, jj_repo):
        """exclude_session only drops the matching record -- a task-agent's
        live reach still blocks."""
        _, live_anchor, inside = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, live_anchor, kind="task-agent")
        with patch("cld.docker.docker_occupant_list", return_value=records):
            with pytest.raises(RuntimeError, match="inside the live reach"):
                resolve_anchor_checked(
                    cfg, jj_repo.repo_root, inside, caller_kind="ticket",
                    exclude_session="t.self",
                )

    def test_exclude_session_leaves_run_container_blocking(self, tmp_path, jj_repo):
        _, live_anchor, inside = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, live_anchor, kind="run")
        with patch("cld.docker.docker_occupant_list", return_value=records):
            with pytest.raises(RuntimeError, match="inside the live reach"):
                resolve_anchor_checked(
                    cfg, jj_repo.repo_root, inside, caller_kind="ticket",
                    exclude_session="t.self",
                )

    def test_shared_refused_when_live_occupant_inside_tree(self, tmp_path, jj_repo):
        """A shared anchor claiming a tree with a live occupant already inside it is refused."""
        base, live_anchor, _ = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, live_anchor, anchor_mode="isolated")
        with patch("cld.docker.docker_occupant_list", return_value=records):
            with pytest.raises(RuntimeError, match="already inside"):
                resolve_anchor_checked(cfg, jj_repo.repo_root, base, "shared")

    def test_shared_passes_with_no_live_occupant(self, tmp_path, jj_repo):
        base, _, _ = self._commits(jj_repo)
        with patch("cld.docker.docker_occupant_list", return_value=[]):
            cfg = Config(mailbox_root=str(tmp_path / "mb"))
            assert resolve_anchor_checked(cfg, jj_repo.repo_root, base, "shared") == base

    def test_live_task_agent_still_blocks_nested_spawn(self, tmp_path, jj_repo):
        """A live task-agent's own tree still refuses another spawn on top of it."""
        _, live_anchor, inside = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, live_anchor, name="cld_task_r_a", kind="task-agent")
        with patch("cld.docker.docker_occupant_list", return_value=records):
            with pytest.raises(RuntimeError, match="inside the live reach"):
                resolve_anchor_checked(cfg, jj_repo.repo_root, inside)

    def test_live_run_still_blocks(self, tmp_path, jj_repo):
        _, live_anchor, inside = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, live_anchor, name="run.r", kind="run")
        with patch("cld.docker.docker_occupant_list", return_value=records):
            with pytest.raises(RuntimeError, match="inside the live reach"):
                resolve_anchor_checked(cfg, jj_repo.repo_root, inside)


class TestEffectiveAnchorDerivation:
    """The occupant's editable boundary is scratch commit B, derived from the
    store at check time; the labeled base A is only the boot-window fallback."""

    SESSION = "a.r.live"

    def _staged(self, jj_repo):
        """base A, its staged scratch child B (session-marked), and B's child,
        with the session bookmark on B as the entrypoint leaves it."""
        base = resolve_anchor(jj_repo, "@-")
        jj_repo.run(["new", base])
        (jj_repo.repo_root / ".cld-run").mkdir()
        (jj_repo.repo_root / ".cld-run" / "anchor.json").write_text("{}\n")
        jj_repo.run(["commit", "-m", f"cld anchor: {self.SESSION} mode=isolated"])
        scratch = resolve_anchor(jj_repo, "@-")
        (jj_repo.repo_root / "work.txt").write_text("work\n")
        jj_repo.run(["commit", "-m", "live agent work"])
        inside_b = resolve_anchor(jj_repo, "@-")
        jj_repo.run(["bookmark", "create", self.SESSION, "-r", scratch])
        return base, scratch, inside_b

    def _fleet(self, tmp_path, jj_repo, base, kind="task-agent"):
        cfg = Config(mailbox_root=str(tmp_path / "mb"))
        records = _occupants((self.SESSION, str(jj_repo.repo_root), base, "isolated"), kind=kind)
        return cfg, records

    def test_base_itself_passes_once_scratch_is_staged(self, tmp_path, jj_repo):
        """The v1 over-block: labeling A blocked anchoring on A itself even
        though the occupant's reach starts at B. Derivation fixes it."""
        base, _, _ = self._staged(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, base)
        with patch("cld.docker.docker_occupant_list", return_value=records):
            assert resolve_anchor_checked(cfg, jj_repo.repo_root, base) == base

    def test_descendant_of_scratch_still_refused(self, tmp_path, jj_repo):
        base, _, inside_b = self._staged(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, base)
        with patch("cld.docker.docker_occupant_list", return_value=records):
            with pytest.raises(RuntimeError, match="inside the live reach") as e:
                resolve_anchor_checked(cfg, jj_repo.repo_root, inside_b)
        assert "effective anchor" in str(e.value)

    def test_scratch_commit_itself_refused(self, tmp_path, jj_repo):
        base, scratch, _ = self._staged(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, base)
        with patch("cld.docker.docker_occupant_list", return_value=records):
            with pytest.raises(RuntimeError, match="inside the live reach"):
                resolve_anchor_checked(cfg, jj_repo.repo_root, scratch)

    def test_scratch_rebased_off_base_on_host_is_still_found(self, tmp_path, jj_repo):
        """The host may rebase B off the labeled base A; B is found through the
        session bookmark, so A stays outside the occupant's reach."""
        base, scratch, inside_b = self._staged(jj_repo)
        jj_repo.run(["rebase", "-s", scratch, "-o", f"parents({base})"])
        cfg, records = self._fleet(tmp_path, jj_repo, base)
        with patch("cld.docker.docker_occupant_list", return_value=records):
            assert resolve_anchor_checked(cfg, jj_repo.repo_root, base) == base
            with pytest.raises(RuntimeError, match="inside the live reach"):
                resolve_anchor_checked(cfg, jj_repo.repo_root, inside_b)

    def test_other_sessions_scratch_is_not_mine(self, tmp_path, jj_repo):
        """The derivation is session-scoped: a sibling's scratch child of the
        same base must not be mistaken for this occupant's boundary."""
        base, scratch, _ = self._staged(jj_repo)
        records = _occupants((("a.r.other"), str(jj_repo.repo_root), base, "isolated"))
        cfg = Config(mailbox_root=str(tmp_path / "mb"))
        # The other session has no scratch commit -> fallback to base A, whose
        # reach covers our session's scratch commit.
        with patch("cld.docker.docker_occupant_list", return_value=records):
            with pytest.raises(RuntimeError, match="inside the live reach"):
                resolve_anchor_checked(cfg, jj_repo.repo_root, scratch)

    def test_sibling_prefix_session_scratch_is_not_mine(self, tmp_path, jj_repo):
        """The description glob ends in ' *' (space before the wildcard), so
        session x must not match the scratch of session x2 staged off the same
        base -- a prefix collision would swap in the sibling's boundary."""
        base = resolve_anchor(jj_repo, "@-")
        jj_repo.run(["new", base])
        (jj_repo.repo_root / ".cld-run").mkdir()
        (jj_repo.repo_root / ".cld-run" / "anchor.json").write_text("{}\n")
        jj_repo.run(["commit", "-m", f"cld anchor: {self.SESSION}2 mode=isolated"])
        # Bookmarked so the sibling's scratch sits in SESSION's searched ancestry.
        jj_repo.run(["bookmark", "create", self.SESSION, "-r", "@-"])
        cfg, records = self._fleet(tmp_path, jj_repo, base)
        # SESSION itself has no scratch commit -> its effective anchor must
        # fall back to base A, whose reach covers A itself.
        with patch("cld.docker.docker_occupant_list", return_value=records):
            with pytest.raises(RuntimeError, match="inside the live reach"):
                resolve_anchor_checked(cfg, jj_repo.repo_root, base)

    def test_shared_occupant_keeps_base_reach(self, tmp_path, jj_repo):
        """A shared-mode occupant's effective anchor is the base itself."""
        base, _, _ = self._staged(jj_repo)
        records = _occupants((self.SESSION, str(jj_repo.repo_root), base, "shared"))
        cfg = Config(mailbox_root=str(tmp_path / "mb"))
        with patch("cld.docker.docker_occupant_list", return_value=records):
            with pytest.raises(RuntimeError, match="inside the live reach"):
                resolve_anchor_checked(cfg, jj_repo.repo_root, base)


class TestOverlapKindMatrix:
    """Outcome per (caller kind, occupant kind): ticket vs ticket warns and
    proceeds, everything else involving an occupant blocks."""

    def _commits(self, jj_repo):
        base = resolve_anchor(jj_repo, "@-")
        jj_repo.run(["new", base])
        (jj_repo.repo_root / "live.txt").write_text("live\n")
        jj_repo.run(["commit", "-m", "occupant anchor"])
        occupant_anchor = resolve_anchor(jj_repo, "@-")
        (jj_repo.repo_root / "more.txt").write_text("more\n")
        jj_repo.run(["commit", "-m", "occupant work"])
        inside = resolve_anchor(jj_repo, "@-")
        return occupant_anchor, inside

    def _fleet(self, tmp_path, jj_repo, anchor, kind, name="cld_occupant"):
        cfg = Config(mailbox_root=str(tmp_path / "mb"))
        records = _occupants((name, str(jj_repo.repo_root), anchor, "isolated"), kind=kind)
        return cfg, records

    def test_ticket_vs_ticket_warns_and_proceeds(self, tmp_path, jj_repo, caplog):
        occupant_anchor, inside = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, occupant_anchor, "ticket", name="t.other")
        with caplog.at_level("WARNING"), patch("cld.docker.docker_occupant_list", return_value=records):
            assert resolve_anchor_checked(
                cfg, jj_repo.repo_root, inside, caller_kind="ticket",
            ) == inside
        assert "t.other" in caplog.text
        assert inside[:12] in caplog.text

    def test_ticket_vs_headless_blocks(self, tmp_path, jj_repo):
        occupant_anchor, inside = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, occupant_anchor, "task-agent")
        with patch("cld.docker.docker_occupant_list", return_value=records):
            with pytest.raises(RuntimeError, match="inside the live reach"):
                resolve_anchor_checked(cfg, jj_repo.repo_root, inside, caller_kind="ticket")

    @pytest.mark.parametrize("caller", ["task-agent", "run"])
    def test_headless_vs_ticket_blocks(self, tmp_path, jj_repo, caller):
        occupant_anchor, inside = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, occupant_anchor, "ticket")
        with patch("cld.docker.docker_occupant_list", return_value=records):
            with pytest.raises(RuntimeError, match="inside the live reach"):
                resolve_anchor_checked(cfg, jj_repo.repo_root, inside, caller_kind=caller)

    def test_headless_vs_headless_blocks(self, tmp_path, jj_repo):
        occupant_anchor, inside = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, occupant_anchor, "task-agent")
        with patch("cld.docker.docker_occupant_list", return_value=records):
            with pytest.raises(RuntimeError, match="inside the live reach"):
                resolve_anchor_checked(cfg, jj_repo.repo_root, inside, caller_kind="task-agent")

    def test_legacy_standing_agent_occupant_still_blocks(self, tmp_path, jj_repo):
        """Nothing launches the removed standing `agent` role any more, but a
        container left over from an older image still owns its tree -- "agent"
        stays in ANCHOR_BLOCKING_KINDS so it keeps refusing an overlap."""
        assert "agent" in ANCHOR_BLOCKING_KINDS
        occupant_anchor, inside = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, occupant_anchor, "agent")
        with patch("cld.docker.docker_occupant_list", return_value=records):
            with pytest.raises(RuntimeError, match="inside the live reach"):
                resolve_anchor_checked(cfg, jj_repo.repo_root, inside, caller_kind="task-agent")

    def test_default_caller_kind_vs_ticket_blocks(self, tmp_path, jj_repo):
        """Only `ticket` gets warn strength -- the default caller kind blocks."""
        occupant_anchor, inside = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, occupant_anchor, "ticket")
        with patch("cld.docker.docker_occupant_list", return_value=records):
            with pytest.raises(RuntimeError, match="inside the live reach"):
                resolve_anchor_checked(cfg, jj_repo.repo_root, inside)

    def test_shared_ticket_vs_ticket_still_warns(self, tmp_path, jj_repo, caplog):
        base = resolve_anchor(jj_repo, "@-")
        occupant_anchor, _ = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, occupant_anchor, "ticket", name="t.other")
        with caplog.at_level("WARNING"), patch("cld.docker.docker_occupant_list", return_value=records):
            assert resolve_anchor_checked(
                cfg, jj_repo.repo_root, base, "shared", caller_kind="ticket",
            ) == base
        assert "shared anchor" in caplog.text


class TestDockerOccupantList:
    """Record building: label parsing, kind and run-state filtering, ticket
    manifest expansion, realpath normalization."""

    def _v1(self, rows, inspects):
        return patch("cld.docker._docker_names_with_state", side_effect=[rows, []]), \
            patch("cld.docker.subprocess.run", side_effect=inspects)

    def test_stopped_agent_included_stopped_run_excluded(self):
        rows = [("run.x", "exited"), ("a.t1.y", "exited")]
        inspects = [
            _ps("/r|aaa|isolated|run|run.x\n"),
            _ps("/r|bbb|isolated|agent|a.t1.y\n"),
        ]
        names_patch, run_patch = self._v1(rows, inspects)
        with names_patch, run_patch:
            records = docker_occupant_list()
        assert [r["name"] for r in records] == ["a.t1.y"]
        assert records[0]["anchor_base"] == "bbb"
        assert records[0]["session"] == "a.t1.y"

    def test_running_run_included(self):
        names_patch, run_patch = self._v1(
            [("run.x", "running")], [_ps("/r|aaa|isolated|run|run.x\n")],
        )
        with names_patch, run_patch:
            assert [r["kind"] for r in docker_occupant_list()] == ["run"]

    def test_non_blocking_kind_never_occupies(self):
        # A ticket carries no org.cld.anchor label (it occupies via its
        # manifest instead), so a kind outside ANCHOR_BLOCKING_KINDS found by
        # the label scan is dropped.
        names_patch, run_patch = self._v1(
            [("cld_x", "running")], [_ps("/r|aaa|isolated|ticket|cld_x\n")],
        )
        with names_patch, run_patch:
            assert docker_occupant_list() == []

    def test_ticket_manifest_expands_per_repo_with_normalized_paths(self):
        manifest = TicketManifest(ticket="lide-2600", repos=(
            RepoManifestEntry(name="lide-api", path="/host/repos/../repos/lide-api",
                              anchor_base="a" * 40),
            RepoManifestEntry(name="diskuze-api", path="/host/repos/diskuze-api",
                              anchor_base="b" * 40, anchor_mode="shared"),
        ))
        with patch("cld.docker._docker_names_with_state",
                   side_effect=[[], [("t.lide-2600", "exited")]]), \
             patch("cld.docker.read_manifest", return_value=manifest):
            records = docker_occupant_list()
        assert len(records) == 2
        assert all(r["kind"] == "ticket" for r in records)
        assert all(r["name"] == r["session"] == "t.lide-2600" for r in records)
        assert records[0]["repo_root"] == "/host/repos/lide-api"
        assert (records[1]["anchor_base"], records[1]["mode"]) == ("b" * 40, "shared")

    def test_unreadable_manifest_becomes_unverifiable_record(self):
        """A ticket whose manifest cannot be read still occupies its (now
        unknown) trees: fail-closed means a record the check must trip over,
        not a warn-and-skip that hides the ticket entirely."""
        with patch("cld.docker._docker_names_with_state",
                   side_effect=[[], [("t.broken", "running")]]), \
             patch("cld.docker.read_manifest", side_effect=RuntimeError("no label")):
            [record] = docker_occupant_list()
        assert record["name"] == record["session"] == "t.broken"
        assert record["kind"] == "ticket"
        assert record["repo_root"] == "" and record["anchor_base"] == ""
        assert "no label" in record["manifest_error"]

    def test_lists_stopped_containers_docker_side(self):
        calls = []

        def spy(cmd, **_kwargs):
            calls.append(cmd)
            return _ps("")

        with patch("cld.docker.subprocess.run", side_effect=spy):
            docker_occupant_list()
        assert all("-a" in cmd for cmd in calls)
        assert not any("status=running" in arg for cmd in calls for arg in cmd)


@pytest.mark.usefixtures("stub_store_preflight")
class TestTicketAnchorResolver:
    """The resolve_manifest hook: ticket caller semantics and mode passthrough."""

    def _occupied(self, jj_repo, kind):
        base = resolve_anchor(jj_repo, "@-")
        jj_repo.run(["new", base])
        (jj_repo.repo_root / "live.txt").write_text("live\n")
        jj_repo.run(["commit", "-m", "occupant anchor"])
        occupant_anchor = resolve_anchor(jj_repo, "@-")
        return base, occupant_anchor, _occupants(
            ("cld_occupant", str(jj_repo.repo_root), occupant_anchor, "isolated"), kind=kind,
        )

    def test_warns_on_ticket_occupant_and_resolves(self, tmp_path, jj_repo, caplog):
        _, occupant_anchor, records = self._occupied(jj_repo, "ticket")
        cfg = Config(mailbox_root=str(tmp_path / "mb"))
        resolver = ticket_anchor_resolver(cfg)
        with caplog.at_level("WARNING"), patch("cld.docker.docker_occupant_list", return_value=records):
            assert resolver(str(jj_repo.repo_root), occupant_anchor, "isolated") == occupant_anchor
        assert "cld_occupant" in caplog.text

    def test_blocks_on_headless_occupant(self, tmp_path, jj_repo):
        _, occupant_anchor, records = self._occupied(jj_repo, "task-agent")
        cfg = Config(mailbox_root=str(tmp_path / "mb"))
        resolver = ticket_anchor_resolver(cfg)
        with patch("cld.docker.docker_occupant_list", return_value=records):
            with pytest.raises(RuntimeError, match="inside the live reach"):
                resolver(str(jj_repo.repo_root), occupant_anchor, "isolated")

    def test_shared_mode_passes_through_to_the_check(self, tmp_path, jj_repo):
        """--shared-anchor for a repo must trigger the whole-tree claim check."""
        base, _, records = self._occupied(jj_repo, "task-agent")
        cfg = Config(mailbox_root=str(tmp_path / "mb"))
        resolver = ticket_anchor_resolver(cfg)
        with patch("cld.docker.docker_occupant_list", return_value=records):
            # isolated: sibling off the same base is fine
            assert resolver(str(jj_repo.repo_root), base, "isolated") == base
            # shared: claiming the occupied tree is refused
            with pytest.raises(RuntimeError, match="already inside"):
                resolver(str(jj_repo.repo_root), base, "shared")

    def test_exclude_session_forwarded_to_the_check(self, tmp_path, jj_repo):
        """A reanchoring ticket passes its own container name through so the
        check drops its own occupant record -- without exclusion this record
        (task-agent kind) would block even a ticket caller."""
        _, occupant_anchor, records = self._occupied(jj_repo, "task-agent")
        cfg = Config(mailbox_root=str(tmp_path / "mb"))
        resolver = ticket_anchor_resolver(cfg, exclude_session="cld_occupant")
        with patch("cld.docker.docker_occupant_list", return_value=records):
            assert resolver(str(jj_repo.repo_root), occupant_anchor, "isolated") == occupant_anchor


class TestParsePeersEnv:
    def test_round_trip(self):
        peers = {"a.r.a": 15, "a.r.b": 5}
        assert parse_peers_env(TaskAgentSpec(slug="t", peers=peers).peers_env()) == peers

    def test_empty_is_empty_dict(self):
        assert parse_peers_env("") == {}

    def test_ignores_blank_segments(self):
        assert parse_peers_env("a:1,,b:2,") == {"a": 1, "b": 2}

    @pytest.mark.parametrize("value", ["nocolon", "a:", ":5", "a:x", "a:1.5"])
    def test_malformed_raises(self, value):
        with pytest.raises(ValueError, match="malformed peer spec"):
            parse_peers_env(value)


def _env_value(args, key):
    """The value of `-e KEY=...` in a docker arg list; None when absent."""
    for flag, value in zip(args, args[1:]):
        if flag == "-e" and value.startswith(f"{key}="):
            return value.removeprefix(f"{key}=")
    return None


@pytest.mark.usefixtures("stub_store_preflight")
class TestBuildTicketContainerArgs:
    """Ticket launcher args: N origin mounts, manifest labels, prefix-map env.
    No daemon needed -- the builder only inspects the filesystem and cfg."""

    def _manifest(self, *repos):
        return TicketManifest(ticket="lide-2600", repos=tuple(
            RepoManifestEntry(name=name, path=path, anchor_base="a" * 40)
            for name, path in repos
        ))

    def _setup(self, tmp_path, monkeypatch, **cfg_kwargs):
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        repo_a = tmp_path / "lide-api"
        repo_b = tmp_path / "diskuze-api"
        repo_a.mkdir()
        repo_b.mkdir()
        manifest = self._manifest(("lide-api", str(repo_a)), ("diskuze-api", str(repo_b)))
        cfg = Config(mailbox_root=str(tmp_path / "mb"), **cfg_kwargs)
        return manifest, cfg

    def test_plugin_marketplace_mounts(self, tmp_path, monkeypatch):
        manifest, cfg = self._setup(tmp_path, monkeypatch)
        mp = _make_marketplace(tmp_path / "mp")
        (tmp_path / "home/.claude/settings.json").write_text(json.dumps(
            {"extraKnownMarketplaces": {"x": {"source": {"source": "directory", "path": str(mp)}}}}
        ))
        args = build_ticket_container_args(manifest, cfg)
        assert _ro_mount(mp)[1] in _mount_values(args)

    def test_ticket_notes_created_and_mounted_rw(self, tmp_path, monkeypatch):
        manifest, cfg = self._setup(tmp_path, monkeypatch)
        args = build_ticket_container_args(manifest, cfg)
        assert (tmp_path / "home/.ticket-notes").is_dir()
        assert f"{tmp_path}/home/.ticket-notes:/home/claude/.ticket-notes:rw" in args

    def test_name_and_no_workdir(self, tmp_path, monkeypatch):
        manifest, cfg = self._setup(tmp_path, monkeypatch)
        args = build_ticket_container_args(manifest, cfg)
        assert args[:2] == ["--name", "t.lide-2600"]
        # No -w: the daemon would pre-create the ticket root as root:root,
        # breaking the entrypoint's mkdir; `docker exec -w` sets it post-boot.
        assert "-w" not in args

    def test_per_repo_store_mounts_only(self, tmp_path, monkeypatch):
        manifest, cfg = self._setup(tmp_path, monkeypatch)
        args = build_ticket_container_args(manifest, cfg)
        for name in ("lide-api", "diskuze-api"):
            assert not _checkout_mounts(args, f"/workspace/origin/{name}")
            assert (
                f"type=bind,src={tmp_path}/{name}/.git,dst=/workspace/origin/{name}/.git"
                in _mount_values(args)
            )

    def test_manifest_label_round_trips(self, tmp_path, monkeypatch):
        manifest, cfg = self._setup(tmp_path, monkeypatch)
        args = build_ticket_container_args(manifest, cfg)
        labeled = next(
            a.removeprefix("org.cld.manifest=") for a in args
            if a.startswith("org.cld.manifest=")
        )
        assert TicketManifest.from_json(labeled) == manifest

    def test_flat_labels_and_identity_labels(self, tmp_path, monkeypatch):
        manifest, cfg = self._setup(tmp_path, monkeypatch)
        args = build_ticket_container_args(manifest, cfg)
        assert "org.cld.kind=ticket" in args
        assert "org.cld.ticket=lide-2600" in args
        assert "org.cld.session=t.lide-2600" in args
        assert f"org.cld.repo.lide-api={tmp_path}/lide-api" in args
        assert f"org.cld.repo.diskuze-api={tmp_path}/diskuze-api" in args

    def test_no_anchor_labels(self, tmp_path, monkeypatch):
        """Anchors ride only in the manifest; B is derived from the jj store."""
        manifest, cfg = self._setup(tmp_path, monkeypatch)
        args = build_ticket_container_args(manifest, cfg)
        assert not any(a.startswith("org.cld.anchor") for a in args)

    def test_ticket_mode_and_session_env(self, tmp_path, monkeypatch):
        manifest, cfg = self._setup(tmp_path, monkeypatch)
        args = build_ticket_container_args(manifest, cfg)
        assert "TICKET_MODE=1" in args
        assert _env_value(args, "SESSION_NAME") == "t.lide-2600"
        assert _env_value(args, "CLD_TICKET_MANIFEST") == manifest.to_json()

    def test_path_map_covers_both_prefixes_and_home(self, tmp_path, monkeypatch):
        manifest, cfg = self._setup(tmp_path, monkeypatch)
        args = build_ticket_container_args(manifest, cfg)
        path_map = json.loads(_env_value(args, "CLD_PATH_MAP"))
        assert path_map["/workspace/origin/lide-api"] == f"{tmp_path}/lide-api"
        assert path_map["/workspace/lide-2600/lide-api"] == f"{tmp_path}/lide-api"
        assert path_map["/home/claude"] == f"{tmp_path}/home"

    def test_no_scalar_host_path_envs(self, tmp_path, monkeypatch):
        """The prefix map replaces CLD_HOST_PROJECT_DIR/CLD_HOST_HOME for the
        ticket kind only; v1 kinds keep the scalar pair."""
        manifest, cfg = self._setup(tmp_path, monkeypatch)
        args = build_ticket_container_args(manifest, cfg)
        assert _env_value(args, "CLD_HOST_PROJECT_DIR") is None
        assert _env_value(args, "CLD_HOST_HOME") is None

    def test_persistent_not_ephemeral(self, tmp_path, monkeypatch):
        manifest, cfg = self._setup(tmp_path, monkeypatch)
        args = build_ticket_container_args(manifest, cfg)
        assert "--rm" not in args and "-it" not in args

    def test_ignore_gitignore_passes_nothing_and_warns(self, tmp_path, monkeypatch, caplog):
        manifest, cfg = self._setup(tmp_path, monkeypatch)
        cld_dir = tmp_path / "lide-api" / ".cld"
        cld_dir.mkdir()
        (cld_dir / "config.toml").write_text('ignore_gitignore = [".env", "local.py"]\n')
        with caplog.at_level(logging.WARNING, logger="cld.docker"):
            args = build_ticket_container_args(manifest, cfg)
        assert _env_value(args, "CLD_REPO_FILES") is None
        assert _skip_warnings(caplog) == [
            f"ignore_gitignore: '{name}' skipped: the host checkout is no longer mounted"
            for name in (".env", "local.py")
        ]

    def test_no_warning_when_nothing_to_link(self, tmp_path, monkeypatch, caplog):
        manifest, cfg = self._setup(tmp_path, monkeypatch)
        with caplog.at_level(logging.WARNING, logger="cld.docker"):
            build_ticket_container_args(manifest, cfg)
        assert not _skip_warnings(caplog)

    def test_bootstrap_env_from_registry(self, tmp_path, monkeypatch):
        manifest, cfg = self._setup(
            tmp_path, monkeypatch,
            repos={"lide-api": RepoEntry(path=str(tmp_path / "lide-api"), bootstrap=True)},
        )
        args = build_ticket_container_args(manifest, cfg)
        assert _env_value(args, "CLD_REPO_BOOTSTRAP") == "lide-api=."

    def test_no_bootstrap_env_when_nothing_opted_in(self, tmp_path, monkeypatch):
        manifest, cfg = self._setup(tmp_path, monkeypatch)
        args = build_ticket_container_args(manifest, cfg)
        assert _env_value(args, "CLD_REPO_BOOTSTRAP") is None

    def test_mailbox_mounted(self, tmp_path, monkeypatch):
        manifest, cfg = self._setup(tmp_path, monkeypatch)
        args = build_ticket_container_args(manifest, cfg)
        assert any(a.endswith(f":{MAILBOX_MOUNT}:rw") for a in args)

    def test_broker_key_wired(self, tmp_path, monkeypatch):
        key = tmp_path / "broker_key"
        key.write_text("k")
        manifest, cfg = self._setup(tmp_path, monkeypatch, broker_key=str(key))
        args = build_ticket_container_args(manifest, cfg)
        assert any("broker-key" in a for a in args)


class TestIgnoreGitignoreSkipped:
    """``ignore_gitignore`` is off while the host checkout is not mounted: each
    entry warns once per launch and nothing reaches the container."""

    def test_one_warning_per_file(self, caplog):
        with caplog.at_level(logging.WARNING, logger="cld.docker"):
            warn_skipped_workspace_files((".env", ".envrc"))
        assert [r.getMessage() for r in caplog.records] == [
            "ignore_gitignore: '.env' skipped: the host checkout is no longer mounted",
            "ignore_gitignore: '.envrc' skipped: the host checkout is no longer mounted",
        ]

    @pytest.mark.usefixtures("stub_store_preflight")
    @pytest.mark.parametrize("task_agent", [TaskAgentSpec(slug="t"), None], ids=["task-agent", "run"])
    def test_container_builder_passes_no_files(self, tmp_path, monkeypatch, caplog, task_agent):
        (tmp_path / "home/.claude").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        cfg = Config(mailbox_root=str(tmp_path / "mb"), ignore_gitignore=(".env", "local.py"))
        with caplog.at_level(logging.WARNING, logger="cld.docker"):
            args = build_container_args(tmp_path, "cld_x", cfg, task_agent=task_agent)
        assert _env_value(args, "WORKSPACE_FILES") is None
        assert _skip_warnings(caplog) == [
            f"ignore_gitignore: '{name}' skipped: the host checkout is no longer mounted"
            for name in (".env", "local.py")
        ]

    @pytest.mark.usefixtures("stub_store_preflight")
    def test_container_builder_is_quiet_without_entries(self, tmp_path, monkeypatch, caplog):
        (tmp_path / "home/.claude").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        with caplog.at_level(logging.WARNING, logger="cld.docker"):
            build_container_args(tmp_path, "cld_x", Config(mailbox_root=str(tmp_path / "mb")))
        assert not _skip_warnings(caplog)


class TestTicketRepoBootstrap:
    def _manifest(self, tmp_path, *names):
        return TicketManifest(ticket="t", repos=tuple(
            RepoManifestEntry(name=name, path=str(tmp_path / name), anchor_base="h")
            for name in names
        ))

    def test_registry_optin_with_pyproject_dir(self, tmp_path):
        cld_dir = tmp_path / "a" / ".cld"
        cld_dir.mkdir(parents=True)
        (cld_dir / "config.toml").write_text('pyproject_dir = "api"\n')
        (tmp_path / "b").mkdir()
        manifest = self._manifest(tmp_path, "a", "b")
        cfg = Config(repos={
            "a": RepoEntry(path=str(tmp_path / "a"), bootstrap=True),
            "b": RepoEntry(path=str(tmp_path / "b"), bootstrap=True),
        })
        assert ticket_repo_bootstrap(manifest, cfg) == "a=api;b=."

    @pytest.mark.parametrize(
        "value", ["/etc", "../x", "a/../../x", "a;b=c", "a=b"],
        ids=["absolute", "dotdot", "dotdot-nested", "semicolon", "equals"],
    )
    def test_unsafe_pyproject_dir_skips_the_repo(self, tmp_path, caplog, value):
        cld_dir = tmp_path / "a" / ".cld"
        cld_dir.mkdir(parents=True)
        (cld_dir / "config.toml").write_text(f'pyproject_dir = "{value}"\n')
        manifest = self._manifest(tmp_path, "a")
        cfg = Config(repos={"a": RepoEntry(path=str(tmp_path / "a"), bootstrap=True)})
        assert ticket_repo_bootstrap(manifest, cfg) == ""
        assert "pyproject_dir" in caplog.text and "skipped" in caplog.text

    def test_non_string_pyproject_dir_skips_the_repo(self, tmp_path):
        cld_dir = tmp_path / "a" / ".cld"
        cld_dir.mkdir(parents=True)
        (cld_dir / "config.toml").write_text("pyproject_dir = 3\n")
        cfg = Config(repos={"a": RepoEntry(path=str(tmp_path / "a"), bootstrap=True)})
        assert ticket_repo_bootstrap(self._manifest(tmp_path, "a"), cfg) == ""

    def test_default_off_and_adhoc_repos_omitted(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        manifest = self._manifest(tmp_path, "a", "b")
        # a: registered without bootstrap; b: ad-hoc (no registry entry).
        cfg = Config(repos={"a": RepoEntry(path=str(tmp_path / "a"))})
        assert ticket_repo_bootstrap(manifest, cfg) == ""


def _real_jj_has_secure_config() -> bool:
    if shutil.which("jj") is None:
        return False
    out = subprocess.run(["jj", "--version"], capture_output=True, text=True).stdout
    found = re.search(r"(\d+)\.(\d+)", out)
    return bool(found) and (int(found[1]), int(found[2])) >= (0, 38)


needs_jj_38 = pytest.mark.skipif(
    not _real_jj_has_secure_config(), reason="needs jj 0.38+ (secure config)",
)

_INIT_COMMANDS = {
    "colocated": ["jj", "git", "init", "--colocate"],
    "internal": ["jj", "git", "init", "--no-colocate"],
    "git-only": ["git", "init"],
}
_GIT_DIRS = {"colocated": ".git", "internal": ".jj/repo/store/git", "git-only": ".git"}


def _expected_rows(layout_id):
    """``(path, readonly)`` of the design's mount set (section 3.1), written out by hand."""
    git = _GIT_DIRS[layout_id]
    rows = [(git, False), (f"{git}/config", True), (f"{git}/hooks", True), (f"{git}/commondir", True)]
    if layout_id != "git-only":
        rows += [
            (".jj", False), (".jj/working_copy", True),
            (".jj/repo", False), (".jj/repo/config-id", True),
            (".jj/repo/store", False), (".jj/repo/store/git_target", True),
            (".jj/workspace-config-id", True),
        ]
    return rows


def _expected_mounts(layout_id, host, root):
    return sorted(
        f"type=bind,src={host}/{rel},dst={root}/{rel}" + (",readonly" if readonly else "")
        for rel, readonly in _expected_rows(layout_id)
    )


def _mount_values(args):
    return [value for flag, value in zip(args, args[1:]) if flag == "--mount"]


def _skip_warnings(caplog):
    return [m for m in (r.getMessage() for r in caplog.records) if m.startswith("ignore_gitignore:")]


def _checkout_mounts(args, root):
    """Mounts that put a host checkout at *root*: any ``-v`` onto it, or a ``--mount`` with it as target."""
    volumes = [v for f, v in zip(args, args[1:]) if f == "-v" and v.split(":")[1:2] == [root]]
    mounts = [m for m in _mount_values(args) if m.split("dst=")[1].split(",")[0] == root]
    return volumes + mounts


@pytest.fixture
def jj_home(tmp_path, monkeypatch):
    """Keep every jj the tests run away from the real user config dir."""
    home = tmp_path / "jj-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / "state"))
    monkeypatch.delenv("JJ_CONFIG", raising=False)
    # With no config record in HOME, jj run inside a workspace on a read-only store
    # (the broker runner, a ticket container) rewrites that store's config ids and fails.
    monkeypatch.chdir(tmp_path)


@pytest.fixture(params=list(_INIT_COMMANDS))
def bare_repo(request, tmp_path, jj_home):
    """A fresh repo of each layout, no jj ids yet: (layout id, path). Skips without the tool."""
    tool = _INIT_COMMANDS[request.param][0]
    if shutil.which(tool) is None:
        pytest.skip(f"{tool} not installed")
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(_INIT_COMMANDS[request.param], cwd=path, check=True, capture_output=True)
    return request.param, path


@pytest.fixture
def fake_jj(tmp_path, monkeypatch):
    """A ``jj`` first on PATH that logs its arguments and reports a version.

    Returns ``set_version, calls``; ``calls()`` lists the argument lines seen.
    """
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    log_file = tmp_path / "fake-jj.log"
    version_file = tmp_path / "fake-jj.version"
    script = bin_dir / "jj"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{log_file}"\n'
        f'[ "$1" = "--version" ] && echo "jj $(cat "{version_file}")"\n'
        "exit 0\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    def set_version(version):
        version_file.write_text(version)

    def calls():
        return log_file.read_text().splitlines() if log_file.exists() else []

    set_version("0.45.1")
    return set_version, calls


@pytest.fixture
def store_repo(bare_repo, fake_jj):
    """A repo of each layout the preflight accepts without running jj: a fake
    jj 0.45 first on PATH and both ids already present."""
    layout_id, repo = bare_repo
    if layout_id != "git-only":
        (repo / ".jj/repo/config-id").write_text("id")
        (repo / ".jj/workspace-config-id").write_text("id")
    return layout_id, repo


class TestStoreMounts:
    def test_rows_per_layout(self, store_repo):
        layout_id, repo = store_repo
        layout = prepare_store_mounts(repo)
        args = store_mount_args(layout, str(repo), "/workspace/origin/api")
        assert sorted(_mount_values(args)) == _expected_mounts(
            layout_id, repo, "/workspace/origin/api",
        )

    def test_only_mount_flags_never_v(self, store_repo):
        _, repo = store_repo
        args = store_mount_args(prepare_store_mounts(repo), str(repo), "/repo")
        assert set(args[::2]) == {"--mount"}

    def test_parent_is_mounted_before_child(self, store_repo):
        """Docker sorts by depth anyway; the rows must name each read-only
        path's parent as a read-write mount so the parent cannot be renamed."""
        _, repo = store_repo
        args = store_mount_args(prepare_store_mounts(repo), str(repo), "/repo")
        rw = {v.split("dst=")[1] for v in _mount_values(args) if not v.endswith(",readonly")}
        for value in _mount_values(args):
            if value.endswith(",readonly"):
                dst = value.split("dst=")[1].removesuffix(",readonly")
                assert os.path.dirname(dst) in rw

    @pytest.fixture(params=["ticket", "task-agent", "run"])
    def build(self, request, store_repo, tmp_path, stub_launch_checks):
        """Run one builder on the store repo: ``(layout id, repo, origin root, args)``."""
        layout_id, repo = store_repo
        (tmp_path / "jj-home/.claude").mkdir()
        cfg = Config(mailbox_root=str(tmp_path / "mb"))
        if request.param == "ticket":
            manifest = TicketManifest(ticket="t", repos=(
                RepoManifestEntry(name="api", path=str(repo), anchor_base="a" * 40),
            ))
            return layout_id, repo, "/workspace/origin/api", build_ticket_container_args(manifest, cfg)
        task_agent = TaskAgentSpec(slug="t") if request.param == "task-agent" else None
        args = build_container_args(repo, "cld_x", cfg, task_agent=task_agent)
        return layout_id, repo, "/workspace/origin", args

    def test_no_builder_emits_a_checkout_mount(self, build):
        _, _, root, args = build
        assert not _checkout_mounts(args, root)

    def test_every_builder_emits_the_store_rows_and_label(self, build):
        layout_id, repo, root, args = build
        assert sorted(_mount_values(args)) == _expected_mounts(layout_id, repo, root)
        assert f"{ORIGIN_MOUNT_LABEL}=store-v1" in args

    def test_label_value(self):
        assert ORIGIN_MOUNT_VALUE == "store-v1"

    @pytest.mark.parametrize("char", [",", '"'], ids=["comma", "quote"])
    def test_mount_separator_in_host_path_refused(self, tmp_path, monkeypatch, stub_launch_checks, char):
        (tmp_path / "jj-home").mkdir()
        (tmp_path / "jj-home/.claude").mkdir()
        monkeypatch.setenv("HOME", str(tmp_path / "jj-home"))
        repo = tmp_path / f"a{char}b"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        with pytest.raises(SystemExit, match="comma or a quote"):
            build_container_args(repo, "cld_x", Config(mailbox_root=str(tmp_path / "mb")))

    def test_mount_separator_in_a_repo_name_refused(self, tmp_path, monkeypatch, stub_launch_checks):
        (tmp_path / ".claude").mkdir()
        monkeypatch.setenv("HOME", str(tmp_path))
        manifest = TicketManifest(ticket="t", repos=(
            RepoManifestEntry(name="a,b", path=str(tmp_path), anchor_base="a" * 40),
        ))
        with pytest.raises(SystemExit, match="comma or a quote"):
            build_ticket_container_args(manifest, Config(mailbox_root=str(tmp_path / "mb")))


class TestLaunchWiring:
    """Image label refusal and the refusal while unprotected siblings run."""

    @pytest.mark.parametrize("label", ["", "1", "3"], ids=["missing", "old", "other"])
    def test_image_without_boot_api_2_refused(self, label):
        with patch("cld.docker._image_label", return_value=label) as read:
            with pytest.raises(SystemExit, match="rebuild the cld images"):
                require_boot_api("claude-devcontainer:latest")
        assert read.call_args.args == ("claude-devcontainer:latest", "org.cld.boot-api")

    def test_image_with_boot_api_2_passes(self):
        with patch("cld.docker._image_label", return_value="2"):
            require_boot_api("claude-devcontainer:latest")

    def test_builders_refuse_an_old_image(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "cld.docker.prepare_store_mounts", lambda repo, name="": StoreLayout(False, ".git"),
        )
        (tmp_path / ".claude").mkdir()
        monkeypatch.setenv("HOME", str(tmp_path))
        manifest = TicketManifest(ticket="t", repos=(
            RepoManifestEntry(name="api", path=str(tmp_path), anchor_base="a" * 40),
        ))
        cfg = Config(mailbox_root=str(tmp_path / "mb"))
        with patch("cld.docker._image_label", return_value=""):
            with pytest.raises(SystemExit, match="rebuild the cld images"):
                build_ticket_container_args(manifest, cfg)
            with pytest.raises(SystemExit, match="rebuild the cld images"):
                build_container_args(tmp_path, "cld_x", cfg)

    def test_builders_refuse_a_stale_base_image(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "cld.docker.prepare_store_mounts", lambda repo, name="": StoreLayout(False, ".git"),
        )
        (tmp_path / ".claude").mkdir()
        monkeypatch.setenv("HOME", str(tmp_path))
        manifest = TicketManifest(ticket="t", repos=(
            RepoManifestEntry(name="api", path=str(tmp_path), anchor_base="a" * 40),
        ))
        cfg = Config(mailbox_root=str(tmp_path / "mb"))
        labels = {"claude-base:latest": "", "claude-devcontainer:latest": "2", "claude-run:latest": "2"}
        with patch("cld.docker._image_label", side_effect=lambda image, label: labels[image]):
            with pytest.raises(SystemExit, match="image 'claude-base:latest' lacks"):
                build_ticket_container_args(manifest, cfg)
            with pytest.raises(SystemExit, match="image 'claude-base:latest' lacks"):
                build_container_args(tmp_path, "cld_x", cfg)

    def test_refusal_names_containers_without_the_label(self):
        listing = f"t.old\t\nt.new\t{ORIGIN_MOUNT_VALUE}\nrunner\t\npins\tstore-pins-v1\n"
        with patch("cld.docker.subprocess.run", return_value=_ps(listing)) as run, \
             pytest.raises(SystemExit) as refused:
            refuse_unprotected_containers("api", "/home/u/api")
        assert "volume=/home/u/api" in run.call_args.args[0]
        message = str(refused.value)
        assert message.startswith("cld: repo 'api': container(s) t.old, runner, pins mount")
        assert "t.new" not in message
        assert "cld restart <ticket>" in message

    @pytest.mark.parametrize(
        ("label", "has_store_mounts"),
        [("", False), ("store-pins-v1", False), ("store-v2", False), ("store-v1", True)],
        ids=["unlabelled", "pins-v1", "other", "store-v1"],
    )
    def test_only_the_store_v1_label_counts(self, label, has_store_mounts):
        with patch("cld.docker._image_label", return_value=label) as read:
            assert container_has_store_mounts("t.t") is has_store_mounts
        assert read.call_args.args == ("t.t", "org.cld.origin-mount")

    def test_no_refusal_when_every_container_has_the_label(self):
        with patch("cld.docker.subprocess.run", return_value=_ps(f"a\t{ORIGIN_MOUNT_VALUE}\n")):
            refuse_unprotected_containers("api", "/home/u/api")

    def test_no_refusal_without_containers(self):
        with patch("cld.docker.subprocess.run", return_value=_ps("")):
            refuse_unprotected_containers("api", "/home/u/api")

    def test_docker_failure_refuses(self):
        with patch("cld.docker.subprocess.run", return_value=_ps("", rc=1)), \
             pytest.raises(SystemExit, match="docker ps failed"):
            refuse_unprotected_containers("api", "/home/u/api")

    def test_builders_refuse_before_the_preflight_touches_the_repo(self, tmp_path, monkeypatch, stub_launch_checks):
        def refuse(name, host_repo):
            raise SystemExit("old container")

        def preflight(host_repo, name=""):
            raise AssertionError("the preflight ran")

        monkeypatch.setattr("cld.docker.refuse_unprotected_containers", refuse)
        monkeypatch.setattr("cld.docker.prepare_store_mounts", preflight)
        (tmp_path / ".claude").mkdir()
        monkeypatch.setenv("HOME", str(tmp_path))
        manifest = TicketManifest(ticket="t", repos=(
            RepoManifestEntry(name="api", path=str(tmp_path), anchor_base="a" * 40),
        ))
        cfg = Config(mailbox_root=str(tmp_path / "mb"))
        with pytest.raises(SystemExit, match="old container"):
            build_ticket_container_args(manifest, cfg)
        with pytest.raises(SystemExit, match="old container"):
            build_container_args(tmp_path, "cld_x", cfg)


def _refusal(repo, name="api"):
    with pytest.raises(SystemExit) as refused:
        prepare_store_mounts(repo, name)
    return str(refused.value)


class TestStorePreflight:
    def test_layout_detected(self, store_repo):
        layout_id, repo = store_repo
        layout = prepare_store_mounts(repo)
        assert layout == StoreLayout(jj=layout_id != "git-only", git_dir=_GIT_DIRS[layout_id])

    @needs_jj_38
    def test_both_ids_created_with_real_jj(self, bare_repo):
        layout_id, repo = bare_repo
        if layout_id == "git-only":
            pytest.skip("no jj ids in a git-only repo")
        assert not (repo / ".jj/repo/config-id").exists()
        prepare_store_mounts(repo)
        assert (repo / ".jj/repo/config-id").is_file()
        assert (repo / ".jj/workspace-config-id").is_file()

    @needs_jj_38
    def test_existing_ids_are_kept(self, bare_repo):
        layout_id, repo = bare_repo
        if layout_id == "git-only":
            pytest.skip("no jj ids in a git-only repo")
        prepare_store_mounts(repo)
        before = [(repo / rel).read_bytes() for rel in (".jj/repo/config-id", ".jj/workspace-config-id")]
        prepare_store_mounts(repo)
        after = [(repo / rel).read_bytes() for rel in (".jj/repo/config-id", ".jj/workspace-config-id")]
        assert before == after

    def test_missing_hooks_created_empty(self, store_repo):
        layout_id, repo = store_repo
        hooks = repo / _GIT_DIRS[layout_id] / "hooks"
        shutil.rmtree(hooks, ignore_errors=True)
        prepare_store_mounts(repo)
        assert hooks.is_dir() and list(hooks.iterdir()) == []
        assert hooks.stat().st_mode & 0o777 == 0o755

    def test_missing_commondir_placeholder_created(self, store_repo):
        layout_id, repo = store_repo
        commondir = repo / _GIT_DIRS[layout_id] / "commondir"
        assert not commondir.exists()
        prepare_store_mounts(repo)
        assert commondir.read_text() == COMMONDIR_PLACEHOLDER
        assert check_git_dir(repo, _GIT_DIRS[layout_id]) is None

    def test_existing_commondir_placeholder_kept(self, store_repo):
        layout_id, repo = store_repo
        prepare_store_mounts(repo)
        prepare_store_mounts(repo)
        assert (repo / _GIT_DIRS[layout_id] / "commondir").read_text() == COMMONDIR_PLACEHOLDER

    @pytest.mark.parametrize("kind", ["foreign", "empty", "padded", "symlink", "dir"])
    def test_foreign_commondir_refused_before_any_jj_runs(self, bare_repo, fake_jj, tmp_path, kind):
        """With a planted commondir, even ``jj config path`` would load an agent-written config."""
        layout_id, repo = bare_repo
        commondir = repo / _GIT_DIRS[layout_id] / "commondir"
        match kind:
            case "foreign":
                commondir.write_text("../../elsewhere")
            case "empty":
                commondir.write_text("")
            case "padded":
                commondir.write_text(COMMONDIR_PLACEHOLDER + "\n")
            case "symlink":
                (tmp_path / "target").write_text(COMMONDIR_PLACEHOLDER)
                commondir.symlink_to(tmp_path / "target")
            case "dir":
                commondir.mkdir()
        message = _refusal(repo)
        assert f"{_GIT_DIRS[layout_id]}/commondir exists: git and jj would load their config and hooks" in message
        _, calls = fake_jj
        assert calls() in ([], ["--version"])
        assert not (repo / ".jj/repo/config-id").exists()

    def test_submodules_refused(self, store_repo):
        layout_id, repo = store_repo
        (repo / _GIT_DIRS[layout_id] / "modules/lib").mkdir(parents=True)
        message = _refusal(repo)
        assert f"{_GIT_DIRS[layout_id]}/modules exists: git submodules are not supported yet" in message

    def test_refusal_format_names_the_repo(self, store_repo):
        layout_id, repo = store_repo
        (repo / _GIT_DIRS[layout_id] / "config").unlink()
        message = _refusal(repo, "lide-api")
        assert message.startswith("cld: repo 'lide-api': ")
        assert message.endswith(" -- cannot mount its store safely")

    def test_git_file_refused(self, tmp_path):
        (tmp_path / ".git").write_text("gitdir: /elsewhere\n")
        assert "git worktree" in _refusal(tmp_path)

    def test_neither_jj_nor_git_refused(self, tmp_path):
        assert "neither a .jj nor a .git directory" in _refusal(tmp_path)

    def test_jj_repo_file_refused(self, tmp_path):
        (tmp_path / ".jj").mkdir()
        (tmp_path / ".jj/repo").write_text("/elsewhere/.jj/repo")
        assert "secondary jj workspace" in _refusal(tmp_path)

    @pytest.mark.parametrize("version", ["0.37.9", "0.9.0"])
    def test_old_jj_refused(self, store_repo, fake_jj, version):
        layout_id, repo = store_repo
        if layout_id == "git-only":
            pytest.skip("jj is not needed for a git-only repo")
        set_version, calls = fake_jj
        set_version(version)
        assert "older than 0.38" in _refusal(repo)
        assert calls() == ["--version"]

    def test_jj_38_and_newer_pass(self, store_repo, fake_jj):
        layout_id, repo = store_repo
        if layout_id == "git-only":
            pytest.skip("jj is not needed for a git-only repo")
        set_version, _ = fake_jj
        for version in ("0.38.0", "1.2.0"):
            set_version(version)
            prepare_store_mounts(repo)

    def test_missing_jj_refused(self, store_repo, tmp_path, monkeypatch):
        layout_id, repo = store_repo
        if layout_id == "git-only":
            pytest.skip("jj is not needed for a git-only repo")
        monkeypatch.setenv("PATH", str(tmp_path))
        assert "jj is not installed" in _refusal(repo)

    def test_foreign_git_target_refused(self, store_repo, tmp_path, fake_jj):
        layout_id, repo = store_repo
        if layout_id == "git-only":
            pytest.skip("no git_target in a git-only repo")
        foreign = tmp_path / "foreign.git"
        foreign.mkdir()
        (repo / ".jj/repo/store/git_target").write_text(str(foreign))
        assert "git_target" in _refusal(repo)
        _, calls = fake_jj
        assert calls() == ["--version"]

    def test_relative_git_target_escape_refused(self, store_repo):
        layout_id, repo = store_repo
        if layout_id == "git-only":
            pytest.skip("no git_target in a git-only repo")
        (repo / ".jj/repo/store/git_target").write_text("../../../../elsewhere/.git")
        assert "git_target" in _refusal(repo)

    @pytest.mark.parametrize("name", ["config", "hooks"])
    def test_symlinked_git_source_refused(self, store_repo, tmp_path, name):
        layout_id, repo = store_repo
        target = repo / _GIT_DIRS[layout_id] / name
        elsewhere = tmp_path / "elsewhere"
        if name == "hooks":
            shutil.rmtree(target, ignore_errors=True)
            elsewhere.mkdir()
        else:
            target.unlink()
            elsewhere.write_text("x")
        target.symlink_to(elsewhere)
        assert f"{_GIT_DIRS[layout_id]}/{name} is a symlink" in _refusal(repo)

    @pytest.mark.parametrize("rel", [".jj/working_copy", ".jj/repo/store/git_target"])
    def test_symlinked_jj_source_refused(self, store_repo, tmp_path, rel):
        layout_id, repo = store_repo
        if layout_id == "git-only":
            pytest.skip("no jj store in a git-only repo")
        target = repo / rel
        elsewhere = tmp_path / "elsewhere"
        if target.is_dir():
            shutil.move(target, elsewhere)
        else:
            elsewhere.write_text(target.read_text())
            target.unlink()
        target.symlink_to(elsewhere)
        assert f"{rel} is a symlink" in _refusal(repo)

    def test_symlinked_id_refused_before_any_jj_runs(self, bare_repo, tmp_path, fake_jj):
        layout_id, repo = bare_repo
        if layout_id == "git-only":
            pytest.skip("no jj ids in a git-only repo")
        elsewhere = tmp_path / "elsewhere-id"
        elsewhere.write_text("x")
        (repo / ".jj/repo/config-id").symlink_to(elsewhere)
        assert ".jj/repo/config-id is a symlink" in _refusal(repo)
        _, calls = fake_jj
        assert calls() == ["--version"]

    def test_dir_where_file_expected_refused(self, store_repo):
        layout_id, repo = store_repo
        config = repo / _GIT_DIRS[layout_id] / "config"
        config.unlink()
        config.mkdir()
        assert "config is a dir, expected a file" in _refusal(repo)

    def test_file_where_dir_expected_refused(self, store_repo):
        layout_id, repo = store_repo
        hooks = repo / _GIT_DIRS[layout_id] / "hooks"
        shutil.rmtree(hooks, ignore_errors=True)
        hooks.write_text("x")
        assert "hooks is a file, expected a dir" in _refusal(repo)

    @pytest.mark.parametrize(
        "legacy", [".jj/repo/config.toml", ".jj/workspace-config.toml"],
        ids=["repo", "workspace"],
    )
    def test_id_missing_next_to_legacy_file_refused_with_no_jj_run(
        self, bare_repo, fake_jj, legacy,
    ):
        """With both ids absent, the first id must not be created by jj either:
        that jj call would migrate the other id's planted legacy file."""
        layout_id, repo = bare_repo
        if layout_id == "git-only":
            pytest.skip("no jj ids in a git-only repo")
        (repo / legacy).write_text('[revset-aliases]\n"trunk()" = "@"\n')
        message = _refusal(repo)
        assert f"unmigrated jj config {legacy}: check it, then run jj once on the host" in message
        _, calls = fake_jj
        assert calls() == ["--version"]
        assert not (repo / ".jj/repo/config-id").exists()
        assert not (repo / ".jj/workspace-config-id").exists()

    def test_legacy_file_with_its_id_present_is_fine(self, store_repo):
        layout_id, repo = store_repo
        if layout_id == "git-only":
            pytest.skip("no jj ids in a git-only repo")
        prepare_store_mounts(repo)
        (repo / ".jj/repo/config.toml").write_text("x = 1\n")
        prepare_store_mounts(repo)

    def test_failed_id_creation_refused(self, bare_repo, fake_jj):
        layout_id, repo = bare_repo
        if layout_id == "git-only":
            pytest.skip("no jj ids in a git-only repo")
        assert "jj did not create .jj/repo/config-id" in _refusal(repo)
        _, calls = fake_jj
        assert calls()[1].endswith("--ignore-working-copy config path --repo")


@needs_jj_38
class TestJjConfigPins:
    """Guards assumptions A1 and A10 of the design on jj upgrades."""

    _ALIAS = '[revset-aliases]\n"trunk()" = "@"\n'

    def _trunk(self, repo):
        out = subprocess.run(
            ["jj", "-R", str(repo), "--ignore-working-copy", "log", "-r", "trunk()",
             "--no-graph", "-T", "commit_id"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()

    @pytest.fixture
    def prepared(self, tmp_path, jj_home):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["jj", "git", "init"], cwd=repo, check=True, capture_output=True)
        prepare_store_mounts(repo)
        return repo

    @pytest.mark.parametrize(
        "legacy", [".jj/repo/config.toml", ".jj/workspace-config.toml"], ids=["repo", "workspace"],
    )
    def test_planted_legacy_config_is_ignored_while_ids_exist(self, prepared, legacy):
        before = self._trunk(prepared)
        (prepared / legacy).write_text(self._ALIAS)
        assert self._trunk(prepared) == before

    @pytest.mark.parametrize(
        "legacy,id_file",
        [(".jj/repo/config.toml", ".jj/repo/config-id"),
         (".jj/workspace-config.toml", ".jj/workspace-config-id")],
        ids=["repo", "workspace"],
    )
    def test_planted_legacy_config_counts_once_its_id_is_gone(self, prepared, legacy, id_file):
        before = self._trunk(prepared)
        (prepared / legacy).write_text(self._ALIAS)
        (prepared / id_file).unlink()
        assert self._trunk(prepared) != before

    def test_preflight_refuses_what_jj_would_migrate(self, prepared):
        (prepared / ".jj/workspace-config.toml").write_text(self._ALIAS)
        (prepared / ".jj/workspace-config-id").unlink()
        assert "unmigrated jj config .jj/workspace-config.toml" in _refusal(prepared)


def _tool(args, cwd, **kwargs):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True, **kwargs).stdout


@needs_jj_38
@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
class TestCommondirPlaceholderWithRealTools:
    """The ``commondir`` row mounts a placeholder file ("."), which git and jj
    must treat like no file at all (verified on jj 0.45.1 and git 2.47). The
    read-only mount itself needs docker; this checks the file it covers."""

    @pytest.fixture
    def prepared(self, bare_repo, tmp_path, monkeypatch):
        for var, value in (("GIT_AUTHOR_NAME", "t"), ("GIT_COMMITTER_NAME", "t"),
                           ("GIT_AUTHOR_EMAIL", "t@t"), ("GIT_COMMITTER_EMAIL", "t@t"),
                           ("JJ_USER", "t"), ("JJ_EMAIL", "t@t")):
            monkeypatch.setenv(var, value)
        layout_id, repo = bare_repo
        remote = tmp_path / "remote.git"
        _tool(["git", "init", "--bare", str(remote)], tmp_path)
        prepare_store_mounts(repo)
        return layout_id, repo, remote

    def _git(self, layout_id, repo, *args):
        git_dir = repo / _GIT_DIRS[layout_id]
        return _tool(["git", f"--git-dir={git_dir}", *args], repo)

    def test_the_placeholder_is_in_the_git_dir(self, prepared):
        layout_id, repo, _ = prepared
        assert (repo / _GIT_DIRS[layout_id] / "commondir").read_text() == COMMONDIR_PLACEHOLDER

    def test_git_works(self, prepared):
        layout_id, repo, remote = prepared
        if layout_id != "git-only":
            pytest.skip("a jj repo is driven through jj")
        _tool(["git", "commit", "--allow-empty", "-m", "seed"], repo)
        _tool(["git", "remote", "add", "origin", str(remote)], repo)
        _tool(["git", "push", "origin", "HEAD"], repo)
        _tool(["git", "fetch", "origin"], repo)
        _tool(["git", "worktree", "add", str(repo.parent / "wt"), "-b", "wt"], repo)
        assert str(repo.parent / "wt") in _tool(["git", "worktree", "list"], repo)
        _tool(["git", "worktree", "remove", str(repo.parent / "wt")], repo)
        _tool(["git", "worktree", "prune"], repo)
        _tool(["git", "gc", "--quiet"], repo)
        _tool(["git", "fsck"], repo)
        assert _tool(["git", "status", "--porcelain"], repo) == ""

    def test_jj_works(self, prepared):
        layout_id, repo, remote = prepared
        if layout_id == "git-only":
            pytest.skip("a git-only repo is driven through git")
        (repo / "f.txt").write_text("x")
        _tool(["jj", "commit", "-m", "seed"], repo)
        _tool(["jj", "bookmark", "create", "main", "-r", "@-"], repo)
        _tool(["jj", "git", "remote", "add", "origin", str(remote)], repo)
        _tool(["jj", "git", "push", "--bookmark", "main"], repo)
        _tool(["jj", "git", "fetch"], repo)
        _tool(["jj", "workspace", "add", str(repo.parent / "ws")], repo)
        _tool(["jj", "workspace", "forget", "ws"], repo)
        self._git(layout_id, repo, "fsck")
        self._git(layout_id, repo, "gc", "--quiet")
        assert "seed" in self._git(layout_id, repo, "log", "--all", "--format=%s")
        assert "seed" in _tool(["git", f"--git-dir={remote}", "log", "--all", "--format=%s"], repo)

    def test_the_placeholder_survives_the_tools(self, prepared):
        layout_id, repo, _ = prepared
        if layout_id == "git-only":
            _tool(["git", "commit", "--allow-empty", "-m", "seed"], repo)
        else:
            _tool(["jj", "new", "-m", "next"], repo)
        assert check_git_dir(repo, _GIT_DIRS[layout_id]) is None
        assert (repo / _GIT_DIRS[layout_id] / "commondir").read_text() == COMMONDIR_PLACEHOLDER

    def test_a_planted_commondir_redirects_git_config(self, prepared):
        """What the block is for: git reads config from the folder commondir names."""
        layout_id, repo, _ = prepared
        git_dir = repo / _GIT_DIRS[layout_id]
        evil = repo.parent / "evil-common"
        shutil.copytree(git_dir, evil)
        with (evil / "config").open("a") as f:
            f.write("[cld]\n\tplanted = yes\n")
        (git_dir / "commondir").write_text(os.path.relpath(evil, git_dir))
        result = subprocess.run(
            ["git", f"--git-dir={git_dir}", "config", "--get", "cld.planted"],
            cwd=repo, capture_output=True, text=True,
        )
        assert result.stdout.strip() == "yes"
        assert "commondir exists" in check_git_dir(repo, _GIT_DIRS[layout_id])
        assert "commondir exists" in _refusal(repo)


class TestPreflightBeforeHostJj:
    """Every launch flow runs the store preflight before any host jj call on the
    repo: any jj call migrates a planted legacy config into the host jj config.
    The repo has one, next to a missing id; the fake jj logs each call, so
    ``--version`` (run away from the repo) is the only one allowed."""

    REFUSED = "unmigrated jj config .jj/repo/config.toml"

    @pytest.fixture
    def planted(self, tmp_path, jj_home, fake_jj, monkeypatch):
        repo = tmp_path / "api"
        (repo / ".jj/repo/store").mkdir(parents=True)
        (repo / ".jj/repo/store/git_target").write_text("../../../.git")
        (repo / ".git").mkdir()
        (repo / ".jj/repo/config.toml").write_text('[revset-aliases]\n"trunk()" = "@"\n')
        monkeypatch.chdir(repo)
        return repo, fake_jj[1]

    def _manifest(self, repo):
        return TicketManifest(ticket="t", repos=(
            RepoManifestEntry(name="api", path=str(repo), anchor_base="a" * 40),
        ))

    def test_cld_run(self, planted):
        _, calls = planted
        with patch("cld.run.require_docker"), patch("cld.run.in_master_container", return_value=False), \
             patch("cld.run.ensure_image"), pytest.raises(SystemExit, match=self.REFUSED):
            launch_run(Config(), "do the thing")
        assert calls() == ["--version"]

    def test_task_agent_start(self, planted):
        _, calls = planted
        with patch("cld.cli.require_docker"):
            result = CliRunner().invoke(app, ["task-agent", "start", "-p", "x", "-n", "t"])
        assert result.exit_code == 1
        assert self.REFUSED in result.output
        assert calls() == ["--version"]

    @pytest.mark.parametrize("flags", [[], ["--no-detach"]], ids=["detached", "foreground"])
    def test_chain_run(self, planted, tmp_path, flags):
        _, calls = planted
        chain = tmp_path / "c.yaml"
        chain.write_text("name: c\nsteps: []\n")
        result = CliRunner().invoke(app, ["chain", "run", str(chain), "-p", "x", *flags])
        assert result.exit_code == 1
        assert self.REFUSED in result.output
        assert calls() == ["--version"]

    def test_ticket_start(self, planted):
        repo, calls = planted
        with patch("cld.ticket._docker_status", return_value="absent"), \
             patch("cld.ticket._launch") as launch, pytest.raises(SystemExit, match=self.REFUSED):
            start_ticket(Config(), "t", [str(repo)], [])
        launch.assert_not_called()
        assert calls() == ["--version"]

    @pytest.mark.parametrize("reanchor", [[], ["api@feature-x"]], ids=["plain", "reanchor"])
    def test_ticket_restart_refuses_before_the_container_is_touched(self, planted, reanchor):
        repo, calls = planted
        with patch("cld.ticket._docker_status", return_value="running"), \
             patch("cld.ticket.read_manifest", return_value=self._manifest(repo)), \
             patch("cld.ticket._stop_and_remove") as stop, \
             patch("cld.ticket._launch") as launch, \
             pytest.raises(SystemExit, match=self.REFUSED):
            restart_ticket(Config(), "t", reanchor)
        stop.assert_not_called()
        launch.assert_not_called()
        assert calls() == ["--version"]


class TestCheckJjIds:
    """The cld-side lstat check: no creation, no jj."""

    @pytest.fixture
    def repo(self, tmp_path):
        repo = tmp_path / "api"
        (repo / ".jj/repo").mkdir(parents=True)
        for rel in JJ_ID_FILES:
            (repo / rel).write_text("id")
        return repo

    def test_ids_match_the_preflight_list(self):
        assert JJ_ID_FILES == tuple(rel for rel, _legacy, _flag in _JJ_IDS)

    def test_both_ids_present(self, repo):
        assert check_jj_ids(repo) is None

    def test_repo_without_jj_has_nothing_to_check(self, tmp_path):
        (tmp_path / ".git").mkdir()
        assert check_jj_ids(tmp_path) is None

    @pytest.mark.parametrize("rel", JJ_ID_FILES)
    def test_missing_id(self, repo, rel):
        (repo / rel).unlink()
        assert check_jj_ids(repo) == f"{rel} is missing"
        assert not (repo / rel).exists()

    @pytest.mark.parametrize("rel", JJ_ID_FILES)
    @pytest.mark.parametrize("kind", ["symlink", "dir"])
    def test_id_that_is_not_a_regular_file(self, repo, rel, kind):
        (repo / rel).unlink()
        if kind == "symlink":
            (repo / rel).symlink_to("/etc/hostname")
        else:
            (repo / rel).mkdir()
        assert check_jj_ids(repo) == f"{rel} is not a regular file"


class TestCheckJjIdsGitDir:
    """``check_jj_ids`` also refuses what ``check_git_dir`` refuses, in either git dir."""

    @pytest.fixture(params=[".git", ".jj/repo/store/git"])
    def repo(self, request, tmp_path):
        repo = tmp_path / "api"
        (repo / ".jj/repo").mkdir(parents=True)
        (repo / request.param).mkdir(parents=True, exist_ok=True)
        for rel in JJ_ID_FILES:
            (repo / rel).write_text("id")
        return repo, request.param

    def test_clean_store(self, repo):
        assert check_jj_ids(repo[0]) is None

    def test_placeholder_is_fine(self, repo):
        path, git_dir = repo
        (path / git_dir / "commondir").write_text(COMMONDIR_PLACEHOLDER)
        assert check_jj_ids(path) is None

    def test_foreign_commondir(self, repo):
        path, git_dir = repo
        (path / git_dir / "commondir").write_text("../evil")
        assert check_jj_ids(path).startswith(f"{git_dir}/commondir exists")

    def test_modules(self, repo):
        path, git_dir = repo
        (path / git_dir / "modules").mkdir()
        assert check_jj_ids(path) == f"{git_dir}/modules exists: git submodules are not supported yet"


class TestCldHostJjNeedsIds:
    """Teardown, the reap of a task agent, the roster detail and secondary-workspace
    detection run no host jj on a repo whose ids are missing or not regular files: any
    jj call would migrate a planted legacy config into the host jj config. The fake jj
    logs each call, so a refused repo leaves the log empty."""

    SESSION = "t.t"

    @pytest.fixture
    def repo(self, tmp_path, jj_home, fake_jj):
        repo = tmp_path / "api"
        (repo / ".jj/repo/store").mkdir(parents=True)
        (repo / ".jj/repo/store/git_target").write_text("../../../.git")
        (repo / ".git").mkdir()
        (repo / ".jj/repo/config.toml").write_text('[revset-aliases]\n"trunk()" = "@"\n')
        return repo

    @pytest.fixture
    def calls(self, fake_jj):
        return fake_jj[1]

    def _prepare(self, repo):
        for rel in JJ_ID_FILES:
            (repo / rel).write_text("id")

    @pytest.fixture(params=["missing", "symlink"])
    def broken(self, request, repo):
        self._prepare(repo)
        (repo / JJ_ID_FILES[0]).unlink()
        if request.param == "symlink":
            (repo / JJ_ID_FILES[0]).symlink_to("/etc/hostname")
        return repo

    def test_forget_session_state_skips_with_a_warning(self, broken, calls, caplog):
        forget_session_state(str(broken), self.SESSION)
        assert calls() == []
        assert f"Not cleaning up session state for {self.SESSION}" in caplog.text
        assert f"jj bookmark forget {self.SESSION}" in caplog.text

    def test_forget_session_state_runs_jj_with_both_ids(self, repo, calls):
        self._prepare(repo)
        forget_session_state(str(repo), self.SESSION)
        assert [c for c in calls() if " forget " in f" {c} "] == [
            f"bookmark forget {self.SESSION}", f"workspace forget {self.SESSION}",
        ]

    def test_bookmark_tip_skips_with_a_warning(self, broken, calls, caplog):
        assert _bookmark_tip(str(broken), self.SESSION) == "-"
        assert calls() == []
        assert "No bookmark lookup" in caplog.text

    def test_bookmark_tip_runs_jj_with_both_ids(self, repo, calls):
        self._prepare(repo)
        _bookmark_tip(str(repo), self.SESSION)
        assert any(" log " in f" {c} " for c in calls())

    def test_foreign_commondir_stops_teardown_and_lookup(self, repo, calls, caplog):
        self._prepare(repo)
        (repo / ".git/commondir").write_text("../evil")
        forget_session_state(str(repo), self.SESSION)
        assert _bookmark_tip(str(repo), self.SESSION) == "-"
        assert calls() == []
        assert ".git/commondir exists" in caplog.text

    def test_reap_task_agent_runs_no_jj(self, broken, calls, tmp_path):
        with patch("cld.cli._task_agent_repo_root", return_value=str(broken)), \
             patch("cld.cli._stop_and_remove_container"), \
             patch("cld.cli.mailbox.archive_mailbox") as archive:
            _reap_task_agent(Config(mailbox_root=str(tmp_path / "mb")), self.SESSION, parent="", force=True)
        assert calls() == []
        archive.assert_called_once()

    @pytest.fixture
    def secondary(self, repo, tmp_path):
        workspace = tmp_path / "ws"
        (workspace / ".jj").mkdir(parents=True)
        (workspace / ".jj/repo").write_text(str(repo / ".jj/repo"))
        return workspace

    def test_secondary_workspace_lookup_waits_for_the_ids(self, broken, secondary, calls):
        backend = get_backend(secondary)
        assert backend.repo_root == broken
        assert backend.workspace_revision == ""
        assert calls() == []

    def test_secondary_workspace_lookup_runs_with_both_ids(self, repo, secondary, calls):
        self._prepare(repo)
        get_backend(secondary)
        assert any(" workspace list " in f" {c} " for c in calls())
