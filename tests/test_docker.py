"""Tests for pure helpers in cld.docker."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from cld.config import Config, _load_dotenv
from cld.docker import (
    _INSPECT_FORMAT,
    ANCHOR_BLOCKING_KINDS,
    MAILBOX_MOUNT,
    TaskAgentSpec,
    allocate_task_agent_name,
    assert_task_agent_capacity,
    build_container_args,
    build_session_name,
    build_ticket_container_args,
    docker_occupant_list,
    docker_task_agent_list,
    find_repo_root,
    in_master_container,
    parse_peers_env,
    resolve_anchor_checked,
    stage_home_ro,
    stage_plugin_marketplaces,
    stage_broker,
    task_agent_container_name,
    ticket_anchor_resolver,
    ticket_container_name,
    ticket_repo_bootstrap,
    ticket_repo_files,
    ticket_slug,
    to_host_path,
)
from cld.manifest import RepoManifestEntry, TicketManifest
from cld.registry import RepoEntry
from cld.vcs.anchor import resolve_anchor


def _ps(names: str, rc: int = 0):
    return type("R", (), {"returncode": rc, "stdout": names, "stderr": ""})()


class TestBuildSessionName:
    def test_explicit_suffix(self):
        assert build_session_name("agent", "feature") == "agent_feature"

    def test_auto_suffix_is_hex(self):
        prefix, _, suffix = build_session_name("cld").partition("_")
        assert prefix == "cld"
        assert len(suffix) == 6 and all(c in "0123456789abcdef" for c in suffix)

    def test_auto_suffix_varies(self):
        # secrets.token_hex(3) -> 6 hex chars; collisions in 20 picks are astronomical
        assert len({build_session_name("x") for _ in range(20)}) > 1


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
    def test_missing_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert stage_home_ro(".missing", Config()) == []

    def test_existing_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".gitconfig").write_text("x")
        args = stage_home_ro(".gitconfig", Config())
        assert args[0] == "-v"
        assert args[1].endswith(":/tmp/host-config/.gitconfig:ro")
        assert str(tmp_path / ".gitconfig") in args[1]

    def test_existing_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".config" / "anthropic").mkdir(parents=True)
        args = stage_home_ro(".config/anthropic", Config())
        assert args[0] == "-v"
        assert args[1].endswith(":/tmp/host-config/.config/anthropic:ro")

    def test_nested_rel_preserved(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".local" / "state" / "nvim").mkdir(parents=True)
        args = stage_home_ro(".local/state/nvim", Config())
        assert args[1].endswith(":/tmp/host-config/.local/state/nvim:ro")

    def test_to_host_path_translation(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".bashrc").write_text("x")
        cfg = Config(host_home="/host/home")
        # tmp_path stands in for $CONTAINER_HOME via HOME env; to_host_path
        # only rewrites paths starting with CONTAINER_HOME, which tmp_path does
        # not, so the host-translated string is just the resolved tmp path.
        args = stage_home_ro(".bashrc", cfg)
        assert args[1].startswith(str(tmp_path.resolve()) + "/.bashrc:")


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
        mp = tmp_path / "mp"
        mp.mkdir()
        self._registry(home, local={"source": "directory", "path": str(mp)})
        assert stage_plugin_marketplaces(Config(), []) == ["-v", f"{mp}:{mp}:ro"]

    def test_file_source_mounts_parent(self, home, tmp_path):
        mp = tmp_path / "mp"
        mp.mkdir()
        (mp / "marketplace.json").write_text("{}")
        self._registry(home, local={"source": "file", "path": str(mp / "marketplace.json")})
        assert stage_plugin_marketplaces(Config(), []) == ["-v", f"{mp}:{mp}:ro"]

    def test_tilde_expanded(self, home):
        (home / "mp").mkdir()
        self._registry(home, local={"source": "directory", "path": "~/mp"})
        assert stage_plugin_marketplaces(Config(), []) == ["-v", f"{home}/mp:{home}/mp:ro"]

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
        mp = tmp_path / "mp"
        mp.mkdir()
        src = {"source": {"source": "directory", "path": str(mp)}}
        self._registry(home, local={"source": "directory", "path": str(mp)})
        (home / ".claude/settings.json").write_text(json.dumps({"extraKnownMarketplaces": {"a": src}}))
        repo = tmp_path / "repo"
        (repo / ".claude").mkdir(parents=True)
        (repo / ".claude/settings.json").write_text(json.dumps({"extraKnownMarketplaces": {"b": src}}))
        (repo / ".claude/settings.local.json").write_text(json.dumps({"extraKnownMarketplaces": {"c": src}}))
        assert stage_plugin_marketplaces(Config(), [repo]) == ["-v", f"{mp}:{mp}:ro"]

    def test_repo_settings_source_and_sorted_order(self, home, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        self._registry(home, bb={"source": "directory", "path": str(b)})
        repo = tmp_path / "repo"
        (repo / ".claude").mkdir(parents=True)
        (repo / ".claude/settings.local.json").write_text(json.dumps(
            {"extraKnownMarketplaces": {"aa": {"source": {"source": "directory", "path": str(a)}}}}
        ))
        assert stage_plugin_marketplaces(Config(), [repo]) == [
            "-v", f"{a}:{a}:ro", "-v", f"{b}:{b}:ro",
        ]

    def test_malformed_json_tolerated(self, home, tmp_path):
        (home / ".claude/plugins/known_marketplaces.json").write_text("{not json")
        (home / ".claude/settings.json").write_text("[]")
        repo = tmp_path / "repo"
        (repo / ".claude").mkdir(parents=True)
        (repo / ".claude/settings.json").write_text('{"extraKnownMarketplaces": "x"}')
        assert stage_plugin_marketplaces(Config(), [repo]) == []


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
        inspect = _ps("/home/u/repoA|cld_agent_repoA_add-oauth|task-agent|cld_master_repoA_ab12|add-oauth||\n")
        with patch("cld.docker.subprocess.run", side_effect=[_ps("cld_agent_repoA_add-oauth\n"), inspect]):
            assert docker_task_agent_list() == [{
                "name": "cld_agent_repoA_add-oauth",
                "repo_root": "/home/u/repoA",
                "session": "cld_agent_repoA_add-oauth",
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


class TestTaskAgentContainerName:
    def test_repo_and_slug(self, tmp_path):
        assert task_agent_container_name(tmp_path / "myrepo", "add-oauth") == "cld_agent_myrepo_add-oauth"

    def test_suffix_appended_from_two(self, tmp_path):
        repo = tmp_path / "myrepo"
        assert task_agent_container_name(repo, "x", 1) == "cld_agent_myrepo_x"
        assert task_agent_container_name(repo, "x", 2) == "cld_agent_myrepo_x-2"

    @pytest.mark.parametrize("slug", ["Add-OAuth", "add oauth", "-lead", "add_oauth", "", "add/oauth"])
    def test_invalid_slug_rejected(self, tmp_path, slug):
        with pytest.raises(ValueError, match="invalid task slug"):
            task_agent_container_name(tmp_path / "r", slug)


class TestAllocateTaskAgentName:
    def test_returns_base_when_free(self, tmp_path):
        with patch("cld.docker._docker_status", return_value="absent"):
            assert allocate_task_agent_name(tmp_path / "r", "task") == "cld_agent_r_task"

    def test_skips_taken_names(self, tmp_path):
        with patch("cld.docker._docker_status", side_effect=["running", "stopped", "absent"]):
            assert allocate_task_agent_name(tmp_path / "r", "task") == "cld_agent_r_task-3"


class TestTaskAgentSpec:
    def test_peers_env_encoding(self):
        spec = TaskAgentSpec(slug="t", peers={"cld_agent_r_b": 5, "cld_agent_r_a": 15})
        assert spec.peers_env() == "cld_agent_r_a:15,cld_agent_r_b:5"

    def test_peers_env_empty(self):
        assert TaskAgentSpec(slug="t").peers_env() == ""


class TestBuildContainerArgsTaskAgent:
    """Task-agent role wiring. No daemon needed -- build_container_args only
    inspects the filesystem and cfg."""

    def _args(self, tmp_path, **kwargs):
        spec = TaskAgentSpec(
            slug="add-oauth",
            parent_master="cld_master_repoA_ab12",
            deliverable_branch="add-oauth-login",
            peers={"cld_agent_repoA_contract": 15},
            **kwargs,
        )
        return build_container_args(
            tmp_path, "cld_agent_repoA_add-oauth", Config(mailbox_root=str(tmp_path / "mb")),
            task_agent=spec,
        )

    def test_labels_and_name(self, tmp_path):
        args = self._args(tmp_path)
        assert "--name" in args and "cld_agent_repoA_add-oauth" in args
        assert "org.cld.kind=task-agent" in args
        assert "org.cld.task=add-oauth" in args
        assert "org.cld.parent-master=cld_master_repoA_ab12" in args
        assert f"org.cld.session=cld_agent_repoA_add-oauth" in args

    def test_agent_mode_plus_task_modifier(self, tmp_path):
        args = self._args(tmp_path)
        assert "AGENT_MODE=1" in args
        assert "TASK_AGENT_MODE=1" in args
        assert "HUB_MODE=1" not in args
        assert not any(a.startswith("org.cld.targets=") for a in args)

    def test_spawn_facts_in_env(self, tmp_path):
        args = self._args(tmp_path)
        assert "AGENT_DELIVERABLE_BRANCH=add-oauth-login" in args
        assert "AGENT_PEERS=cld_agent_repoA_contract:15" in args
        assert "AGENT_PARENT_MASTER=cld_master_repoA_ab12" in args
        assert "AGENT_TASK_SLUG=add-oauth" in args

    def test_turn_cap_propagated(self, tmp_path):
        """In-container Config.from_env() sees no host TOML, so the cap has to be passed."""
        args = build_container_args(
            tmp_path, "cld_agent_r_t",
            Config(mailbox_root=str(tmp_path / "mb"), agent_max_turns=200),
            task_agent=TaskAgentSpec(slug="t"),
        )
        assert "CLD_AGENT_MAX_TURNS=200" in args

    def test_budget_fallbacks_propagated(self, tmp_path):
        spec = TaskAgentSpec(slug="t")
        args = build_container_args(
            tmp_path, "cld_agent_r_t",
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
            tmp_path, "cld_agent_r_t",
            Config(mailbox_root=str(tmp_path / "mb"), broker_key=str(key)),
            task_agent=TaskAgentSpec(slug="t"),
        )
        assert any("broker-key" in a for a in args)


class TestBuildContainerArgsPluginMarketplaces:
    def test_mount_included(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".claude/plugins").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        mp = tmp_path / "mp"
        mp.mkdir()
        (home / ".claude/plugins/known_marketplaces.json").write_text(
            json.dumps({"local": {"source": {"source": "directory", "path": str(mp)}}})
        )
        repo = tmp_path / "repo"
        repo.mkdir()
        args = build_container_args(repo, "cld_run_x", Config(mailbox_root=str(tmp_path / "mb")))
        assert f"{mp}:{mp}:ro" in args


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
            tmp_path, "cld_agent_r_t", self._cfg(tmp_path), task_agent=TaskAgentSpec(slug="t"),
        )
        assert any("broker-key" in a for a in args)

    def test_run_role_gets_no_broker(self, tmp_path):
        """`cld run` (no persistent role) never gets the broker -- it's a
        one-shot, unattended container."""
        args = build_container_args(tmp_path, "run_x", self._cfg(tmp_path))
        assert not any("broker-key" in a for a in args)

    def test_run_devcontainer_not_named(self, tmp_path):
        """Only the persistent roles get a name; `cld run` stays anonymous."""
        args = build_container_args(tmp_path, "run_x", self._cfg(tmp_path))
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

    def _fleet(self, tmp_path, jj_repo, anchor, name="cld_agent_r_live", anchor_mode="isolated", kind="task-agent"):
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
        assert "cld_agent_r_live" in str(e.value)

    def test_equal_to_live_anchor_refused(self, tmp_path, jj_repo):
        _, live_anchor, _ = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, live_anchor)
        with patch("cld.docker.docker_occupant_list", return_value=records):
            with pytest.raises(RuntimeError, match="inside the live reach"):
                resolve_anchor_checked(cfg, jj_repo.repo_root, live_anchor)

    def test_other_repo_agents_ignored(self, tmp_path, jj_repo):
        _, live_anchor, inside = self._commits(jj_repo)
        elsewhere = _occupants(("cld_agent_r_live", "/some/other/repo", live_anchor, "isolated"))
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
            "name": "cld_ticket_broken", "repo_root": "", "anchor_base": "",
            "session": "cld_ticket_broken", "mode": "isolated", "kind": "ticket",
            "manifest_error": "no label",
        }

    def test_unreadable_ticket_manifest_blocks_headless_caller(self, tmp_path, jj_repo):
        _, _, inside = self._commits(jj_repo)
        cfg = Config(mailbox_root=str(tmp_path / "mb"))
        with patch("cld.docker.docker_occupant_list", return_value=[self._broken_ticket_record()]):
            with pytest.raises(RuntimeError, match="could not verify") as e:
                resolve_anchor_checked(cfg, jj_repo.repo_root, inside, caller_kind="task-agent")
        assert "unreadable manifest" in str(e.value)
        assert "cld_ticket_broken" in str(e.value)

    def test_unreadable_ticket_manifest_warns_ticket_caller(self, tmp_path, jj_repo, caplog):
        _, _, inside = self._commits(jj_repo)
        cfg = Config(mailbox_root=str(tmp_path / "mb"))
        with caplog.at_level("WARNING"), \
             patch("cld.docker.docker_occupant_list", return_value=[self._broken_ticket_record()]):
            assert resolve_anchor_checked(
                cfg, jj_repo.repo_root, inside, caller_kind="ticket",
            ) == inside
        assert "unreadable manifest" in caplog.text
        assert "cld_ticket_broken" in caplog.text

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
            tmp_path, jj_repo, live_anchor, name="cld_ticket_self", kind="ticket",
        )
        with patch("cld.docker.docker_occupant_list", return_value=records):
            assert resolve_anchor_checked(
                cfg, jj_repo.repo_root, inside, caller_kind="ticket",
                exclude_session="cld_ticket_self",
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
                    exclude_session="cld_ticket_self",
                )

    def test_exclude_session_leaves_run_container_blocking(self, tmp_path, jj_repo):
        _, live_anchor, inside = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, live_anchor, kind="run")
        with patch("cld.docker.docker_occupant_list", return_value=records):
            with pytest.raises(RuntimeError, match="inside the live reach"):
                resolve_anchor_checked(
                    cfg, jj_repo.repo_root, inside, caller_kind="ticket",
                    exclude_session="cld_ticket_self",
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
        cfg, records = self._fleet(tmp_path, jj_repo, live_anchor, name="cld_run_r", kind="run")
        with patch("cld.docker.docker_occupant_list", return_value=records):
            with pytest.raises(RuntimeError, match="inside the live reach"):
                resolve_anchor_checked(cfg, jj_repo.repo_root, inside)


class TestEffectiveAnchorDerivation:
    """The occupant's editable boundary is scratch commit B, derived from the
    store at check time; the labeled base A is only the boot-window fallback."""

    SESSION = "cld_agent_r_live"

    def _staged(self, jj_repo):
        """base A, its staged scratch child B (session-marked), and B's child,
        bookmarked at the session as the entrypoint leaves it."""
        base = resolve_anchor(jj_repo, "@-")
        jj_repo.run(["new", base])
        (jj_repo.repo_root / ".cld-run").mkdir()
        (jj_repo.repo_root / ".cld-run" / "anchor.json").write_text("{}\n")
        jj_repo.run(["commit", "-m", f"cld anchor: {self.SESSION} mode=isolated"])
        scratch = resolve_anchor(jj_repo, "@-")
        (jj_repo.repo_root / "work.txt").write_text("work\n")
        jj_repo.run(["commit", "-m", "live agent work"])
        inside_b = resolve_anchor(jj_repo, "@-")
        jj_repo.run(["bookmark", "create", self.SESSION, "-r", inside_b])
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
        records = _occupants((("cld_agent_r_other"), str(jj_repo.repo_root), base, "isolated"))
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
        cfg, records = self._fleet(tmp_path, jj_repo, occupant_anchor, "ticket", name="cld_ticket_other")
        with caplog.at_level("WARNING"), patch("cld.docker.docker_occupant_list", return_value=records):
            assert resolve_anchor_checked(
                cfg, jj_repo.repo_root, inside, caller_kind="ticket",
            ) == inside
        assert "cld_ticket_other" in caplog.text
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
        cfg, records = self._fleet(tmp_path, jj_repo, occupant_anchor, "ticket", name="cld_ticket_other")
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
        rows = [("cld_run_x", "exited"), ("cld_agent_y", "exited")]
        inspects = [
            _ps("/r|aaa|isolated|run|cld_run_x\n"),
            _ps("/r|bbb|isolated|agent|cld_agent_y\n"),
        ]
        names_patch, run_patch = self._v1(rows, inspects)
        with names_patch, run_patch:
            records = docker_occupant_list()
        assert [r["name"] for r in records] == ["cld_agent_y"]
        assert records[0]["anchor_base"] == "bbb"
        assert records[0]["session"] == "cld_agent_y"

    def test_running_run_included(self):
        names_patch, run_patch = self._v1(
            [("cld_run_x", "running")], [_ps("/r|aaa|isolated|run|cld_run_x\n")],
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
                   side_effect=[[], [("cld_ticket_lide-2600", "exited")]]), \
             patch("cld.docker.read_manifest", return_value=manifest):
            records = docker_occupant_list()
        assert len(records) == 2
        assert all(r["kind"] == "ticket" for r in records)
        assert all(r["name"] == r["session"] == "cld_ticket_lide-2600" for r in records)
        assert records[0]["repo_root"] == "/host/repos/lide-api"
        assert (records[1]["anchor_base"], records[1]["mode"]) == ("b" * 40, "shared")

    def test_unreadable_manifest_becomes_unverifiable_record(self):
        """A ticket whose manifest cannot be read still occupies its (now
        unknown) trees: fail-closed means a record the check must trip over,
        not a warn-and-skip that hides the ticket entirely."""
        with patch("cld.docker._docker_names_with_state",
                   side_effect=[[], [("cld_ticket_broken", "running")]]), \
             patch("cld.docker.read_manifest", side_effect=RuntimeError("no label")):
            [record] = docker_occupant_list()
        assert record["name"] == record["session"] == "cld_ticket_broken"
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
        peers = {"cld_agent_r_a": 15, "cld_agent_r_b": 5}
        assert parse_peers_env(TaskAgentSpec(slug="t", peers=peers).peers_env()) == peers

    def test_empty_is_empty_dict(self):
        assert parse_peers_env("") == {}

    def test_ignores_blank_segments(self):
        assert parse_peers_env("a:1,,b:2,") == {"a": 1, "b": 2}

    @pytest.mark.parametrize("value", ["nocolon", "a:", ":5", "a:x", "a:1.5"])
    def test_malformed_raises(self, value):
        with pytest.raises(ValueError, match="malformed peer spec"):
            parse_peers_env(value)


class TestTicketSlug:
    def test_lowercases_and_keeps_kebab(self):
        assert ticket_slug("LIDE-2600") == "lide-2600"

    def test_free_form_sanitized(self):
        assert ticket_slug("My Ticket_v2!") == "my-ticket-v2"

    def test_leading_trailing_junk_stripped(self):
        assert ticket_slug("--x--") == "x"

    def test_idempotent_on_a_slug(self):
        assert ticket_slug("lide-2600") == "lide-2600"

    @pytest.mark.parametrize("ticket", ["", "___", "!!"])
    def test_empty_slug_rejected(self, ticket):
        with pytest.raises(ValueError, match="empty slug"):
            ticket_slug(ticket)

    def test_container_name(self):
        assert ticket_container_name("LIDE-2600") == "cld_ticket_lide-2600"


def _env_value(args, key):
    """The value of `-e KEY=...` in a docker arg list; None when absent."""
    for flag, value in zip(args, args[1:]):
        if flag == "-e" and value.startswith(f"{key}="):
            return value.removeprefix(f"{key}=")
    return None


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
        mp = tmp_path / "mp"
        mp.mkdir()
        (tmp_path / "diskuze-api/.claude").mkdir()
        (tmp_path / "diskuze-api/.claude/settings.json").write_text(json.dumps(
            {"extraKnownMarketplaces": {"x": {"source": {"source": "directory", "path": str(mp)}}}}
        ))
        args = build_ticket_container_args(manifest, cfg)
        assert f"{mp}:{mp}:ro" in args

    def test_name_and_no_workdir(self, tmp_path, monkeypatch):
        manifest, cfg = self._setup(tmp_path, monkeypatch)
        args = build_ticket_container_args(manifest, cfg)
        assert args[:2] == ["--name", "cld_ticket_lide-2600"]
        # No -w: the daemon would pre-create the ticket root as root:root,
        # breaking the entrypoint's mkdir; `docker exec -w` sets it post-boot.
        assert "-w" not in args

    def test_per_repo_rw_origin_mounts(self, tmp_path, monkeypatch):
        manifest, cfg = self._setup(tmp_path, monkeypatch)
        args = build_ticket_container_args(manifest, cfg)
        assert f"{tmp_path}/lide-api:/workspace/origin/lide-api" in args
        assert f"{tmp_path}/diskuze-api:/workspace/origin/diskuze-api" in args

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
        assert "org.cld.session=cld_ticket_lide-2600" in args
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
        assert _env_value(args, "SESSION_NAME") == "cld_ticket_lide-2600"
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

    def test_repo_files_env_from_repo_config(self, tmp_path, monkeypatch):
        manifest, cfg = self._setup(tmp_path, monkeypatch)
        cld_dir = tmp_path / "lide-api" / ".cld"
        cld_dir.mkdir()
        (cld_dir / "config.toml").write_text('ignore_gitignore = [".env", "local.py"]\n')
        args = build_ticket_container_args(manifest, cfg)
        assert _env_value(args, "CLD_REPO_FILES") == "lide-api=.env:local.py"

    def test_no_repo_files_env_when_nothing_to_link(self, tmp_path, monkeypatch):
        manifest, cfg = self._setup(tmp_path, monkeypatch)
        args = build_ticket_container_args(manifest, cfg)
        assert _env_value(args, "CLD_REPO_FILES") is None

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


class TestTicketRepoFiles:
    def test_multiple_repos_joined_with_semicolon(self, tmp_path):
        for name, files in (("a", '[".env"]'), ("b", '["x", "y"]')):
            cld_dir = tmp_path / name / ".cld"
            cld_dir.mkdir(parents=True)
            (cld_dir / "config.toml").write_text(f"ignore_gitignore = {files}\n")
        manifest = TicketManifest(ticket="t", repos=(
            RepoManifestEntry(name="a", path=str(tmp_path / "a"), anchor_base="h"),
            RepoManifestEntry(name="b", path=str(tmp_path / "b"), anchor_base="h"),
        ))
        assert ticket_repo_files(manifest) == "a=.env;b=x:y"

    def test_missing_config_omitted(self, tmp_path):
        (tmp_path / "a").mkdir()
        manifest = TicketManifest(ticket="t", repos=(
            RepoManifestEntry(name="a", path=str(tmp_path / "a"), anchor_base="h"),
        ))
        assert ticket_repo_files(manifest) == ""


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

    def test_default_off_and_adhoc_repos_omitted(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        manifest = self._manifest(tmp_path, "a", "b")
        # a: registered without bootstrap; b: ad-hoc (no registry entry).
        cfg = Config(repos={"a": RepoEntry(path=str(tmp_path / "a"))})
        assert ticket_repo_bootstrap(manifest, cfg) == ""
