"""Tests for the repo registry: TOML round-trip writes, parsing, spec resolution, picker."""

import tomllib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

from cld.cli import app
from cld.config import Config
from cld.registry import (
    RepoEntry,
    add_repo,
    parse_repos,
    parse_selection,
    pick_repos,
    remove_repo,
    resolve_repo_specs,
    ticket_repo_mounts,
    tickets_referencing,
)


runner = CliRunner()

_SEEDED = """\
# my hand-written header comment
base_image = "custom-base"  # trailing comment

# a comment between keys
agent_timeout = 99
"""


@pytest.fixture
def config_path(tmp_path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(_SEEDED)
    return p


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """HOME with an existing ~/projects/my-api, so tilde paths pass the add check."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "projects" / "my-api").mkdir(parents=True)
    return tmp_path


class TestAddRmRoundTrip:
    def test_add_writes_a_repos_table(self, config_path, home):
        add_repo(config_path, "my-api", "~/projects/my-api")
        data = tomllib.loads(config_path.read_text())
        assert data["repos"]["my-api"] == {"path": "~/projects/my-api"}

    def test_add_nonexistent_path_raises(self, config_path, tmp_path):
        with pytest.raises(RuntimeError, match="not an existing directory"):
            add_repo(config_path, "ghost", str(tmp_path / "missing"))
        assert "repos" not in tomllib.loads(config_path.read_text())

    def test_add_preserves_comments_and_unrelated_keys(self, config_path, home):
        add_repo(config_path, "my-api", "~/projects/my-api", default_rev="trunk()", bootstrap=True)
        text = config_path.read_text()
        assert "# my hand-written header comment" in text
        assert "# trailing comment" in text
        assert "# a comment between keys" in text
        data = tomllib.loads(text)
        assert data["base_image"] == "custom-base"
        assert data["agent_timeout"] == 99
        assert data["repos"]["my-api"] == {
            "path": "~/projects/my-api", "default_rev": "trunk()", "bootstrap": True,
        }

    def test_add_then_rm_restores_the_rest(self, config_path, tmp_path):
        x, y = tmp_path / "x", tmp_path / "y"
        x.mkdir()
        y.mkdir()
        before = config_path.read_text()
        add_repo(config_path, "a", str(x))
        add_repo(config_path, "b", str(y))
        remove_repo(config_path, "a")
        data = tomllib.loads(config_path.read_text())
        assert "a" not in data["repos"]
        assert data["repos"]["b"] == {"path": str(y)}
        remove_repo(config_path, "b")
        text = config_path.read_text()
        assert before in text
        assert "repos" not in tomllib.loads(text).get("repos", {})

    def test_add_duplicate_name_raises(self, config_path, tmp_path):
        x, y = tmp_path / "x", tmp_path / "y"
        x.mkdir()
        y.mkdir()
        add_repo(config_path, "a", str(x))
        with pytest.raises(RuntimeError, match="already registered"):
            add_repo(config_path, "a", str(y))

    @pytest.mark.parametrize("name", ["My-Api", "a_b", "-lead", "a b", ""])
    def test_add_invalid_name_raises(self, config_path, name):
        with pytest.raises(RuntimeError, match="invalid repo name"):
            add_repo(config_path, name, "/x")

    def test_rm_unknown_name_raises(self, config_path):
        with pytest.raises(RuntimeError, match="not registered"):
            remove_repo(config_path, "ghost")

    def test_load_parses_what_add_wrote(self, config_path, tmp_path, home):
        add_repo(config_path, "my-api", "~/projects/my-api", default_rev="main")
        cfg = Config.from_env(user_config=config_path, project_config=tmp_path / "missing")
        assert cfg.repos == {"my-api": RepoEntry(path="~/projects/my-api", default_rev="main")}


class TestParseRepos:
    def test_full_entry(self):
        raw = {"my-api": {
            "path": "/x", "default_rev": "main", "bootstrap": True,
        }}
        assert parse_repos(raw) == {"my-api": RepoEntry(
            path="/x", default_rev="main", bootstrap=True,
        )}

    def test_defaults(self):
        assert parse_repos({"a": {"path": "/x"}}) == {"a": RepoEntry(path="/x")}

    def test_invalid_name_skipped_with_warning(self, caplog):
        with caplog.at_level("WARNING"):
            assert parse_repos({"Bad_Name": {"path": "/x"}, "ok": {"path": "/y"}}) == {
                "ok": RepoEntry(path="/y"),
            }
        assert "invalid name" in caplog.text

    def test_missing_path_skipped_with_warning(self, caplog):
        with caplog.at_level("WARNING"):
            assert parse_repos({"a": {"default_rev": "main"}}) == {}
        assert "missing 'path'" in caplog.text

    def test_non_table_entry_skipped_with_warning(self, caplog):
        with caplog.at_level("WARNING"):
            assert parse_repos({"a": "/x"}) == {}
        assert "expected a [repos.a] table" in caplog.text

    def test_unknown_entry_key_warns_but_loads(self, caplog):
        with caplog.at_level("WARNING"):
            assert parse_repos({"a": {"path": "/x", "bogus": 1}}) == {"a": RepoEntry(path="/x")}
        assert "unknown key 'bogus'" in caplog.text


class TestResolveRepoSpecs:
    _REGISTRY = {"lide-api": RepoEntry(path="~/projects/lide-api", default_rev="main")}

    def test_registry_name(self):
        assert resolve_repo_specs(self._REGISTRY, ["lide-api"]) == self._REGISTRY

    def test_adhoc_path_basename_becomes_the_name(self, tmp_path):
        repo = tmp_path / "my-tool"
        repo.mkdir()
        resolved = resolve_repo_specs({}, [str(repo)])
        assert resolved == {"my-tool": RepoEntry(path=str(repo))}

    def test_adhoc_tilde_path_expands(self):
        resolved = resolve_repo_specs({}, ["~/projects/my-tool"])
        assert resolved["my-tool"].path == str(Path("~/projects/my-tool").expanduser())

    def test_registry_wins_over_a_samenamed_dir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "lide-api").mkdir()
        assert resolve_repo_specs(self._REGISTRY, ["lide-api"]) == self._REGISTRY

    def test_name_collision_is_an_error(self, tmp_path):
        repo = tmp_path / "lide-api"
        repo.mkdir()
        with pytest.raises(RuntimeError, match="resolved twice"):
            resolve_repo_specs(self._REGISTRY, ["lide-api", str(repo)])

    def test_unknown_bare_name_is_an_error(self):
        with pytest.raises(RuntimeError, match="neither a registered repo name nor a path"):
            resolve_repo_specs(self._REGISTRY, ["nope"])

    def test_adhoc_basename_must_be_a_valid_name(self, tmp_path):
        repo = tmp_path / "My_Repo"
        repo.mkdir()
        with pytest.raises(RuntimeError, match="invalid repo name"):
            resolve_repo_specs({}, [str(repo)])


class TestParseSelection:
    def test_numbers_and_spaces(self):
        assert parse_selection(" 1, 3 ", 3) == [1, 3]

    def test_duplicates_deduped_in_order(self):
        assert parse_selection("2,1,2", 3) == [2, 1]

    def test_empty_means_abort(self):
        assert parse_selection("   ", 3) == []

    def test_out_of_range_raises(self):
        with pytest.raises(RuntimeError, match="out of range"):
            parse_selection("4", 3)

    def test_non_number_raises(self):
        with pytest.raises(RuntimeError, match="not a number"):
            parse_selection("1,x", 3)


class TestPickRepos:
    _REGISTRY = {
        "a-repo": RepoEntry(path="/a", default_rev="main"),
        "b-repo": RepoEntry(path="/b"),
    }

    def test_selection_with_anchor_prompts(self):
        prompt = MagicMock(side_effect=["1,2", "main", "custom-rev"])
        with patch("cld.registry.typer.prompt", prompt):
            picked = pick_repos(self._REGISTRY)
        assert picked == [
            ("a-repo", self._REGISTRY["a-repo"], "main"),
            ("b-repo", self._REGISTRY["b-repo"], "custom-rev"),
        ]
        # default_rev is the offered default; trunk() where the entry has none
        anchor_defaults = [c.kwargs["default"] for c in prompt.call_args_list[1:]]
        assert anchor_defaults == ["main", "trunk()"]

    def test_git_repo_anchor_prompt_offers_head_not_trunk(self, git_repo):
        registry = {"g-repo": RepoEntry(path=str(git_repo.repo_root))}
        prompt = MagicMock(side_effect=["1", "HEAD"])
        with patch("cld.registry.typer.prompt", prompt):
            pick_repos(registry)
        assert prompt.call_args_list[1].kwargs["default"] == "HEAD"

    def test_empty_selection_aborts(self):
        with patch("cld.registry.typer.prompt", return_value=""):
            with pytest.raises(typer.Abort):
                pick_repos(self._REGISTRY)

    def test_empty_registry_is_an_error(self):
        with pytest.raises(RuntimeError, match="registry is empty"):
            pick_repos({})


class TestTicketRepoMounts:
    def _result(self, returncode=0, stdout=""):
        return MagicMock(returncode=returncode, stdout=stdout, stderr="")

    def test_collects_repo_labels_per_ticket(self):
        results = [
            self._result(stdout="t.lide-1\n"),
            self._result(stdout='{"org.cld.kind": "ticket", "org.cld.repo.lide-api": "/host/lide-api"}\n'),
        ]
        with patch("cld.registry.subprocess.run", side_effect=results):
            assert ticket_repo_mounts() == {"t.lide-1": {"lide-api": "/host/lide-api"}}

    def test_docker_failure_reads_as_no_tickets(self):
        with patch("cld.registry.subprocess.run", return_value=self._result(returncode=1)):
            assert ticket_repo_mounts() == {}

    def test_tickets_referencing_matches_name_and_path(self):
        mounts = {
            "t.one": {"lide-api": str(Path("~/p/lide-api").expanduser())},
            "t.two": {"lide-api": "/elsewhere/lide-api"},
        }
        with patch("cld.registry.ticket_repo_mounts", return_value=mounts):
            assert tickets_referencing("lide-api", "~/p/lide-api") == ["t.one"]


class TestReposCli:
    def _invoke(self, config_path, *argv):
        with patch("cld.cli._user_config_path", return_value=config_path), \
             patch("cld.config._user_config_path", return_value=config_path):
            return runner.invoke(app, ["repos", *argv])

    def test_add_then_list(self, config_path, tmp_path):
        repo = tmp_path / "my-api"
        repo.mkdir()
        with patch("cld.cli.ticket_repo_mounts", return_value={}):
            add = self._invoke(config_path, "add", "my-api", str(repo), "--default-rev", "main")
            listing = self._invoke(config_path)
        assert add.exit_code == 0, add.output
        assert "registered 'my-api'" in add.output
        assert listing.exit_code == 0, listing.output
        assert "my-api" in listing.output
        assert str(repo) in listing.output
        assert "main" in listing.output

    def test_add_nonexistent_path_errors(self, config_path, tmp_path):
        result = self._invoke(config_path, "add", "my-api", str(tmp_path / "missing"))
        assert result.exit_code == 1
        assert "not an existing directory" in result.output

    def test_list_empty_registry(self, config_path):
        result = self._invoke(config_path)
        assert result.exit_code == 0, result.output
        assert "No repos registered" in result.output

    def test_list_names_mounting_tickets(self, config_path, tmp_path):
        repo = tmp_path / "my-api"
        repo.mkdir()
        mounts = {"t.lide-1": {"my-api": str(repo)}}
        with patch("cld.cli.ticket_repo_mounts", return_value=mounts):
            self._invoke(config_path, "add", "my-api", str(repo))
            result = self._invoke(config_path)
        assert result.exit_code == 0, result.output
        assert "t.lide-1" in result.output

    def test_rm_removes(self, config_path, tmp_path):
        repo = tmp_path / "my-api"
        repo.mkdir()
        with patch("cld.cli.tickets_referencing", return_value=[]):
            self._invoke(config_path, "add", "my-api", str(repo))
            result = self._invoke(config_path, "rm", "my-api")
        assert result.exit_code == 0, result.output
        assert "repos" not in tomllib.loads(config_path.read_text())

    def test_rm_refused_while_a_ticket_mounts_it(self, config_path, tmp_path):
        repo = tmp_path / "my-api"
        repo.mkdir()
        with patch("cld.cli.tickets_referencing", return_value=["t.lide-1"]):
            self._invoke(config_path, "add", "my-api", str(repo))
            result = self._invoke(config_path, "rm", "my-api")
        assert result.exit_code == 1
        assert "t.lide-1" in result.output
        assert "my-api" in tomllib.loads(config_path.read_text())["repos"]

    def test_rm_unknown_name_errors(self, config_path):
        result = self._invoke(config_path, "rm", "ghost")
        assert result.exit_code == 1
        assert "not registered" in result.output

    def test_add_invalid_name_errors(self, config_path):
        result = self._invoke(config_path, "add", "Bad_Name", "/x")
        assert result.exit_code == 1
        assert "invalid repo name" in result.output
