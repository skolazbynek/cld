"""Tests for Config TOML layering and resolution order."""

from pathlib import Path

import pytest

from cld.config import Config, _find_project_config, _load_toml
from cld.registry import RepoEntry


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for var in (
        "CLD_BASE_IMAGE", "CLD_DEVCONTAINER_IMAGE", "CLD_AGENT_IMAGE",
        "CLD_AGENT_TIMEOUT", "CLD_POLL_INTERVAL", "CLD_DEBUG",
        "CLD_MAILBOX_ROOT", "CLD_AGENT_MAX_TURNS", "CLD_AGENT_KICKOFF_PERSONA",
        "CLD_MAX_TASK_AGENTS", "CLD_PEER_ABSOLUTE_LIMIT", "CLD_ROOT_ASK_LIMIT",
    ):
        monkeypatch.delenv(var, raising=False)


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


class TestTomlLayering:
    def test_user_only(self, tmp_path):
        user = _write(tmp_path / "user.toml", 'base_image = "u-base"\nagent_timeout = 99\n')
        cfg = Config.from_env(user_config=user, project_config=tmp_path / "missing")
        assert cfg.base_image == "u-base"
        assert cfg.agent_timeout == 99

    def test_project_only(self, tmp_path):
        proj = _write(tmp_path / ".cld/config.toml", 'base_image = "p-base"\n')
        cfg = Config.from_env(user_config=tmp_path / "missing", project_config=proj)
        assert cfg.base_image == "p-base"

    def test_project_overrides_user(self, tmp_path):
        user = _write(tmp_path / "user.toml", 'base_image = "u"\nrun_image = "u-run"\n')
        proj = _write(tmp_path / ".cld/config.toml", 'base_image = "p"\n')
        cfg = Config.from_env(user_config=user, project_config=proj)
        assert cfg.base_image == "p"
        assert cfg.run_image == "u-run"  # only project's keys override

    def test_env_overrides_toml(self, tmp_path, monkeypatch):
        proj = _write(tmp_path / ".cld/config.toml", 'base_image = "p"\n')
        monkeypatch.setenv("CLD_BASE_IMAGE", "env-base")
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=proj)
        assert cfg.base_image == "env-base"

    def test_dotenv_overrides_toml(self, tmp_path):
        proj = _write(tmp_path / ".cld/config.toml", 'base_image = "p"\n')
        dotenv = _write(tmp_path / ".env", "CLD_BASE_IMAGE=dotenv-base\n")
        cfg = Config.from_env(dotenv=dotenv, user_config=tmp_path / "u", project_config=proj)
        assert cfg.base_image == "dotenv-base"

    def test_missing_files_use_defaults(self, tmp_path):
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=tmp_path / "p")
        assert cfg.base_image == "claude-base:latest"
        assert cfg.agent_timeout == 1800
        assert cfg.debug is False

    def test_unknown_key_warns_but_loads(self, tmp_path, capsys):
        proj = _write(tmp_path / ".cld/config.toml", 'base_image = "p"\nbogus = 1\n')
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=proj)
        assert cfg.base_image == "p"
        err = capsys.readouterr().err
        assert "unknown key 'bogus'" in err

    def test_malformed_toml_does_not_crash(self, tmp_path, capsys):
        proj = _write(tmp_path / ".cld/config.toml", "this = is = not valid toml\n")
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=proj)
        assert cfg.base_image == "claude-base:latest"
        assert "failed to read" in capsys.readouterr().err

    def test_int_and_bool_types(self, tmp_path):
        proj = _write(tmp_path / ".cld/config.toml", "agent_timeout = 42\npoll_interval = 7\ndebug = true\n")
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=proj)
        assert cfg.agent_timeout == 42
        assert cfg.poll_interval == 7
        assert cfg.debug is True


class TestMessengerConfig:
    def test_defaults(self, tmp_path):
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=tmp_path / "p")
        assert cfg.mailbox_root.endswith(".cld/mailboxes")
        assert cfg.agent_max_turns == 120
        assert cfg.agent_kickoff_persona == "agent"

    def test_toml_overrides(self, tmp_path):
        proj = _write(
            tmp_path / ".cld/config.toml",
            'mailbox_root = "/custom/mailboxes"\nagent_max_turns = 10\nagent_kickoff_persona = "custom-persona"\n',
        )
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=proj)
        assert cfg.mailbox_root == "/custom/mailboxes"
        assert cfg.agent_max_turns == 10
        assert cfg.agent_kickoff_persona == "custom-persona"

    def test_env_overrides_toml(self, tmp_path, monkeypatch):
        proj = _write(tmp_path / ".cld/config.toml", 'mailbox_root = "/toml/mailboxes"\n')
        monkeypatch.setenv("CLD_MAILBOX_ROOT", "/env/mailboxes")
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=proj)
        assert cfg.mailbox_root == "/env/mailboxes"


class TestTaskAgentConfig:
    def test_defaults(self, tmp_path):
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=tmp_path / "p")
        assert cfg.max_task_agents == 4
        assert cfg.peer_absolute_limit == 10
        assert cfg.root_ask_limit == 3

    def test_toml_overrides_without_warning(self, tmp_path, capsys):
        proj = _write(tmp_path / ".cld/config.toml", "max_task_agents = 2\npeer_absolute_limit = 25\nroot_ask_limit = 5\n")
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=proj)
        assert cfg.max_task_agents == 2
        assert cfg.peer_absolute_limit == 25
        assert cfg.root_ask_limit == 5
        assert "unknown key" not in capsys.readouterr().err

    def test_env_overrides_toml(self, tmp_path, monkeypatch):
        proj = _write(tmp_path / ".cld/config.toml", "max_task_agents = 2\npeer_absolute_limit = 25\nroot_ask_limit = 5\n")
        monkeypatch.setenv("CLD_MAX_TASK_AGENTS", "8")
        monkeypatch.setenv("CLD_PEER_ABSOLUTE_LIMIT", "3")
        monkeypatch.setenv("CLD_ROOT_ASK_LIMIT", "2")
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=proj)
        assert cfg.max_task_agents == 8
        assert cfg.peer_absolute_limit == 3
        assert cfg.root_ask_limit == 2


class TestFindProjectConfig:
    def test_finds_in_start_dir(self, tmp_path):
        cfg = _write(tmp_path / ".cld/config.toml", "")
        assert _find_project_config(tmp_path) == cfg

    def test_walks_up_from_nested(self, tmp_path):
        cfg = _write(tmp_path / ".cld/config.toml", "")
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert _find_project_config(nested) == cfg

    def test_returns_none_if_absent(self, tmp_path):
        assert _find_project_config(tmp_path) is None


class TestLoadToml:
    def test_filters_unknown_keys(self, tmp_path):
        p = _write(tmp_path / "c.toml", 'base_image = "x"\nbogus = 1\n')
        assert _load_toml(p) == {"base_image": "x"}


class TestUserOnlyKeys:
    """``home_mounts_*`` name host paths that get mounted: only the user config may set them."""

    @pytest.mark.parametrize("key", ["home_mounts_always", "home_mounts_devcontainer"])
    def test_repo_layer_ignored_with_warning(self, tmp_path, capsys, key):
        proj = _write(tmp_path / ".cld/config.toml", f'{key} = ["/etc", "../x"]\nbase_image = "p"\n')
        user = _write(tmp_path / "user.toml", f'{key} = [".ok"]\n')
        cfg = Config.from_env(user_config=user, project_config=proj)
        assert getattr(cfg, key) == (".ok",)
        assert cfg.base_image == "p"
        assert f"'{key}' in {proj} is ignored: it is read only from the user config" in capsys.readouterr().err

    def test_user_layer_applies_and_repo_layer_cannot_override(self, tmp_path):
        user = _write(tmp_path / "user.toml", 'home_mounts_always = [".a"]\nhome_mounts_devcontainer = [".b"]\n')
        proj = _write(tmp_path / ".cld/config.toml", 'home_mounts_always = [".evil"]\n')
        cfg = Config.from_env(user_config=user, project_config=proj)
        assert cfg.home_mounts_always == (".a",)
        assert cfg.home_mounts_devcontainer == (".b",)

    def test_load_toml_drops_them_unless_user_layer(self, tmp_path):
        p = _write(tmp_path / "c.toml", 'home_mounts_always = [".a"]\nbase_image = "x"\n')
        assert _load_toml(p) == {"base_image": "x"}
        assert _load_toml(p, user_layer=True) == {"home_mounts_always": [".a"], "base_image": "x"}


class TestReposTable:
    def test_parsed_into_config(self, tmp_path):
        user = _write(
            tmp_path / "user.toml",
            '[repos.lide-api]\npath = "~/projects/lide-api"\ndefault_rev = "main"\n'
            '[repos.diskuze-api]\npath = "~/projects/diskuze-api"\nbootstrap = true\n',
        )
        cfg = Config.from_env(user_config=user, project_config=tmp_path / "missing")
        assert cfg.repos == {
            "lide-api": RepoEntry(path="~/projects/lide-api", default_rev="main"),
            "diskuze-api": RepoEntry(path="~/projects/diskuze-api", bootstrap=True),
        }

    def test_repos_table_does_not_warn_unknown(self, tmp_path, capsys):
        user = _write(tmp_path / "user.toml", '[repos.a]\npath = "/x"\n')
        Config.from_env(user_config=user, project_config=tmp_path / "missing")
        assert "unknown key" not in capsys.readouterr().err

    def test_non_table_repos_warns_and_is_dropped(self, tmp_path, capsys):
        user = _write(tmp_path / "user.toml", 'repos = "nope"\n')
        cfg = Config.from_env(user_config=user, project_config=tmp_path / "missing")
        assert cfg.repos == {}
        assert "table of [repos.<name>] tables" in capsys.readouterr().err

    def test_defaults_to_empty(self, tmp_path):
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=tmp_path / "p")
        assert cfg.repos == {}

    def test_repo_layer_repos_ignored_with_warning(self, tmp_path, capsys):
        """The registry lives solely in the user config (spec section 4): a
        checked-out repo's .cld/config.toml must not define entries."""
        proj = _write(tmp_path / ".cld/config.toml", '[repos.evil]\npath = "/x"\n')
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=proj)
        assert cfg.repos == {}
        assert "registry lives only in the user config" in capsys.readouterr().err

    def test_repo_layer_cannot_override_user_registry(self, tmp_path, capsys):
        user = _write(tmp_path / "user.toml", '[repos.a]\npath = "/real"\n')
        proj = _write(tmp_path / ".cld/config.toml", '[repos.a]\npath = "/evil"\n')
        cfg = Config.from_env(user_config=user, project_config=proj)
        assert cfg.repos == {"a": RepoEntry(path="/real")}
        assert "registry lives only in the user config" in capsys.readouterr().err


class TestPathMap:
    def test_parsed_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "CLD_PATH_MAP",
            '{"/workspace/origin/lide-api": "/host/lide-api", "/home/claude": "/host/home"}',
        )
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=tmp_path / "p")
        assert cfg.path_map == {
            "/workspace/origin/lide-api": "/host/lide-api",
            "/home/claude": "/host/home",
        }

    def test_defaults_to_empty(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLD_PATH_MAP", raising=False)
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=tmp_path / "p")
        assert cfg.path_map == {}

    def test_malformed_json_warns_and_falls_back_to_empty(self, tmp_path, monkeypatch, capsys):
        """Config loading must not die on a bad CLD_PATH_MAP; path translation
        then falls back to the scalar host_project_dir/host_home pair."""
        monkeypatch.setenv("CLD_PATH_MAP", "{not json")
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=tmp_path / "p")
        assert cfg.path_map == {}
        assert "CLD_PATH_MAP" in capsys.readouterr().err

    def test_non_object_json_warns_and_falls_back_to_empty(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("CLD_PATH_MAP", '["/a", "/b"]')
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=tmp_path / "p")
        assert cfg.path_map == {}
        assert "CLD_PATH_MAP" in capsys.readouterr().err


class TestRetiredMasterKeys:
    """`master_targets` (and the `master_extra_mounts_ro` it replaced) went with
    the master role: both are now plain unknown keys, warned about and dropped."""

    @pytest.mark.parametrize("key", ["master_targets", "master_extra_mounts_ro"])
    def test_ignored_with_a_warning(self, tmp_path, key, capsys):
        proj = _write(tmp_path / ".cld/config.toml", f'{key} = ["~/projects/foo"]\n')
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=proj)
        assert not hasattr(cfg, "master_targets")
        assert f"unknown key '{key}'" in capsys.readouterr().err
