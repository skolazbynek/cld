"""Anchor guard hook: Bash parsing units and integration against a throwaway jj repo."""

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from cld.vcs.anchor_guard import _simple_commands, parse_jj

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOK = PROJECT_ROOT / "cld" / "vcs" / "anchor_guard.py"
FOREIGN = "/workspace/origin/cld-guard-test"

needs_jj = pytest.mark.skipif(shutil.which("jj") is None, reason="jj not installed")


# --- Parsing (no jj) ----------------------------------------------------------


def _jj_commands(command: str, cwd: str = "/start") -> list[tuple[list[str], str]]:
    return [
        (words[1:], directory)
        for words, directory in _simple_commands(command, cwd)
        if os.path.basename(words[0]) == "jj"
    ]


class TestSimpleCommands:
    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("jj log", [(["log"], "/start")]),
            ("/usr/bin/jj log", [(["log"], "/start")]),
            ("jj new a; jj new b", [(["new", "a"], "/start"), (["new", "b"], "/start")]),
            ("jj new a && jj new b || jj new c", [(["new", "a"], "/start"), (["new", "b"], "/start"), (["new", "c"], "/start")]),
            ("jj log | jj st", [(["log"], "/start"), (["st"], "/start")]),
            ("jj log\njj st", [(["log"], "/start"), (["st"], "/start")]),
            ("cd /tmp && jj log; jj st", [(["log"], "/tmp"), (["st"], "/tmp")]),
            ("cd /tmp\njj log", [(["log"], "/tmp")]),
            ("(cd /tmp; jj log); jj st", [(["log"], "/tmp"), (["st"], "/start")]),
            ("cd sub && jj log", [(["log"], "/start/sub")]),
            ("cd /tmp && cd .. && jj log", [(["log"], "/")]),
            ("FOO=1 BAR=2 jj log", [(["log"], "/start")]),
            ("bash -c 'cd /tmp && jj log'", [(["log"], "/tmp")]),
            ('sh -c "jj new a"', [(["new", "a"], "/start")]),
            ("bash -lc 'jj st'", [(["st"], "/start")]),
            ("jj log 2>/dev/null", [(["log"], "/start")]),
            ("jj log 2>&1 | head", [(["log"], "/start")]),
            ("jj log > out.txt", [(["log"], "/start")]),
            ("jj describe -m 'a; b && c'", [(["describe", "-m", "a; b && c"], "/start")]),
            ("(jj log)", [(["log"], "/start")]),
        ],
    )
    def test_jj_commands(self, command: str, expected: list[tuple[list[str], str]]) -> None:
        assert _jj_commands(command) == expected

    @pytest.mark.parametrize("command", ["echo jj new a", "git log | grep jj", "ls; pwd", "bash -c 'echo jj'", "echo 'jj new a'"])
    def test_non_jj_ignored(self, command: str) -> None:
        assert _jj_commands(command) == []


class TestParseJj:
    @pytest.mark.parametrize(
        ("args", "words", "options"),
        [
            (["-R", "/x", "log"], ["log"], [("-R", "/x")]),
            (["log", "-R/x"], ["log"], [("-R", "/x")]),
            (["log", "--repository=/x"], ["log"], [("--repository", "/x")]),
            (["log", "--repository", "/x"], ["log"], [("--repository", "/x")]),
            (["new", "-A", "abc"], ["new"], [("-A", "abc")]),
            (["new", "-Aabc"], ["new"], [("-A", "abc")]),
            (["new", "--insert-after=abc", "p"], ["new", "p"], [("--insert-after", "abc")]),
            (["--ignore-immutable", "log"], ["log"], [("--ignore-immutable", None)]),
            (["describe", "--ignore-immutable"], ["describe"], [("--ignore-immutable", None)]),
            (["--config", "a=b", "log"], ["log"], [("--config", "a=b")]),
            (["log", "--config-file=f.toml"], ["log"], [("--config-file", "f.toml")]),
            (["--at-op", "o1", "log"], ["log"], [("--at-op", "o1")]),
            (["--at-operation=o1", "log"], ["log"], [("--at-operation", "o1")]),
            (["--ignore-working-copy", "log"], ["log"], [("--ignore-working-copy", None)]),
            (["new", "-m", "msg", "parent"], ["new", "parent"], [("-m", "msg")]),
            (["bookmark", "set", "x", "-B"], ["bookmark", "set", "x"], [("-B", None)]),
            (["rebase", "-B", "x"], ["rebase"], [("-B", "x")]),
            (["log", "--", "-weird"], ["log", "-weird"], []),
        ],
    )
    def test_parse(self, args: list[str], words: list[str], options: list[tuple[str, str | None]]) -> None:
        call = parse_jj(args)
        assert (call.words, call.options) == (words, options)


# --- Integration --------------------------------------------------------------


@dataclass
class Repo:
    path: Path
    env: dict[str, str]
    registry: Path
    anchor: str
    base: str
    sibling: str
    mine: str

    def jj(self, *args: str) -> str:
        return subprocess.run(
            ["jj", *args], cwd=self.path, env=self.env, capture_output=True, text=True, check=True,
        ).stdout.strip()

    def hook(self, payload: dict, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-P", str(HOOK)],
            input=json.dumps(payload) if stdin is None else stdin,
            cwd=self.path, env=self.env, capture_output=True, text=True,
        )

    def decision(self, payload: dict) -> str | None:
        result = self.hook(payload)
        assert result.returncode == 0, result.stderr
        if not result.stdout:
            return None
        out = json.loads(result.stdout)
        if "decision" in out:
            assert out["decision"] == "block"
            return "block"
        assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        assert out["hookSpecificOutput"]["permissionDecisionReason"]
        return out["hookSpecificOutput"]["permissionDecision"]

    def bash(self, command: str, cwd: Path | None = None) -> str | None:
        return self.decision({
            "hook_event_name": "PreToolUse", "tool_name": "Bash",
            "cwd": str(cwd or self.path), "tool_input": {"command": command},
        })

    def file(self, path: str | Path, tool: str = "Write", key: str = "file_path") -> str | None:
        return self.decision({
            "hook_event_name": "PreToolUse", "tool_name": tool,
            "cwd": str(self.path), "tool_input": {key: str(path)},
        })

    def post(self) -> str | None:
        return self.decision({
            "hook_event_name": "PostToolUse", "tool_name": "Bash",
            "cwd": str(self.path), "tool_input": {"command": "true"},
        })

    def register(self, *records: dict[str, str]) -> None:
        self.registry.write_text("".join(json.dumps(r) + "\n" for r in records))


def _build_repo(tmp_path: Path) -> Repo:
    home = tmp_path / "home"
    home.mkdir()
    path = tmp_path.resolve() / "repo"
    path.mkdir()
    registry = tmp_path / "anchors.jsonl"
    env = {
        **{k: v for k, v in os.environ.items() if k not in ("XDG_CONFIG_HOME", "JJ_CONFIG")},
        "HOME": str(home),
        "JJ_USER": "Test",
        "JJ_EMAIL": "test@example.com",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "CLD_ANCHORS_FILE": str(registry),
    }
    r = Repo(path, env, registry, "", "", "", "")
    r.jj("git", "init", ".")
    r.jj("describe", "-m", "base")
    r.base = r.jj("log", "-r", "@", "--no-graph", "-T", "change_id")
    r.jj("new", "-m", "sibling")
    r.sibling = r.jj("log", "-r", "@", "--no-graph", "-T", "change_id")
    r.jj("new", "description(substring:base)", "-m", "anchor")
    r.anchor = r.jj("log", "-r", "@", "--no-graph", "-T", "change_id")
    r.jj("new", "-m", "mine")
    r.mine = r.jj("log", "-r", "@", "--no-graph", "-T", "change_id")
    r.jj(
        "config", "set", "--workspace", 'revset-aliases."immutable_heads()"',
        f"builtin_immutable_heads() | ~(change_id({r.anchor})+::)",
    )
    r.jj("bookmark", "create", "outside", "-r", r.sibling)
    r.jj("bookmark", "create", "inside", "-r", r.mine)
    r.register({"repo": "repo", "workspace": str(path), "anchor": r.anchor, "mode": "isolated", "backend": "jj"})
    return r


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return _build_repo(tmp_path)


@pytest.fixture(scope="module")
def shared_repo(tmp_path_factory: pytest.TempPathFactory) -> Repo:
    """For tests that only feed the hook and never change the repo."""
    return _build_repo(tmp_path_factory.mktemp("shared"))


@needs_jj
class TestBashRules:
    @pytest.mark.parametrize(
        "command",
        [
            "jj --ignore-immutable describe -m x",
            "jj describe --ignore-immutable -m x",
            "jj --config 'revset-aliases.\"immutable_heads()\"=\"none()\"' log",
            "jj log --config='revset-aliases.\"immutable_heads()\"=\"none()\"'",
            "jj config set --workspace ui.color never",
            "jj config set --user 'revset-aliases.\"x\"' 'none()'",
            "jj config unset --repo revset-aliases.x",
            "jj config edit --workspace",
            "jj workspace add ../other",
            "jj workspace forget",
            "jj workspace rename foo",
            f"jj -R {FOREIGN} describe -m x",
            f"jj --repository={FOREIGN} new",
            f"jj -R{FOREIGN} abandon",
            f"cd {FOREIGN} && jj new",
            "cd /workspace/origin/other; jj describe -m x",
            "jj new {sibling}",
            "jj new {base}",
            "jj new {anchor} {sibling}",
            "jj new -A {sibling}",
            "jj new -A{sibling}",
            "jj new --insert-after={sibling}",
            "jj new --after {sibling}",
            "jj rebase -r @ --after {sibling}",
            "jj new -o {sibling}",
            "jj new 'all()'",
            "jj new 'root()'",
            "jj rebase -r @ -o {sibling}",
            "jj rebase -r @ --onto {sibling}",
            "jj rebase -r @ -d {sibling}",
            "jj rebase -r @ -A {base}",
            "jj rebase -r @ -A '{anchor} | {sibling}'",
            "jj rebase -r @ -o '{sibling} | {base}'",
            "jj duplicate @ -o '{anchor} | {sibling}'",
            "jj duplicate @ -o {sibling}",
            "jj revert -r @ -A {sibling}",
            "jj split -r @ -o {sibling}",
            "jj squash -o {sibling}",
            "jj bookmark create foo -r {sibling}",
            "jj bookmark set foo -r {base}",
            "jj bookmark move inside --to {sibling}",
            "jj bookmark move --from {sibling} --to @",
            "jj bookmark move --from {sibling} -t @",
            "jj b s foo -r {sibling}",
            "jj bookmark delete outside",
            "jj bookmark forget outside",
            "jj bookmark rename outside renamed",
            "jj bookmark move outside --to @",
            "jj bookmark set outside -r @",
            "jj bookmark delete 'glob:out*'",
            "jj bookmark delete inside outside",
            "cd /tmp && jj -R {repo} new {sibling}",
            "echo hi; jj new {sibling}",
            "bash -c 'jj new {sibling}'",
            "FOO=1 jj new {sibling}",
            "jj --at-op @ -R {repo} new {sibling}",
            "jj new {sibling} # don't",
            "jj new {sibling} \\",
        ],
    )
    def test_deny(self, shared_repo: Repo, command: str) -> None:
        command = command.format(repo=shared_repo.path, **vars(shared_repo))
        assert shared_repo.bash(command) == "deny"

    def test_deny_reason_names_rule_and_fix(self, shared_repo: Repo) -> None:
        result = shared_repo.hook({
            "hook_event_name": "PreToolUse", "tool_name": "Bash",
            "cwd": str(shared_repo.path), "tool_input": {"command": f"jj new {shared_repo.sibling}"},
        })
        out = json.loads(result.stdout)
        assert result.returncode == 0
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert "destination" in reason
        assert shared_repo.anchor in reason

    @pytest.mark.parametrize(
        "command",
        [
            "jj log",
            "jj st",
            "jj new {anchor}",
            "jj new @",
            "jj new {mine}",
            "jj new",
            "jj new -m msg {mine}",
            "jj new -m 'parent {sibling}' {mine}",
            "jj new -A @",
            "jj new -A {anchor}",
            "jj new 'bad(('",
            "jj rebase -r @ -o {anchor}",
            "jj rebase -r @ -o {mine}",
            "jj rebase -r @ -B {sibling}",
            "jj rebase -b '{anchor}::' --onto '{anchor} | {sibling}'",
            "jj rebase -b @ -o '{mine} | {sibling}'",
            "jj describe -m x",
            "jj describe {sibling} -m x",
            "jj edit {mine}",
            "jj abandon @",
            "jj undo",
            "jj op log",
            "jj bookmark list",
            "jj bookmark create foo -r @",
            "jj bookmark create foo -r {anchor}",
            "jj bookmark set inside -r @",
            "jj bookmark set inside -B -r @",
            "jj bookmark move inside --to @",
            "jj bookmark delete inside",
            "jj bookmark rename inside renamed",
            "jj bookmark forget inside",
            "jj bookmark delete does-not-exist",
            "jj frobnicate",
            "jj config get ui.color",
            "jj config set --user ui.color never",
            "jj --ignore-working-copy log",
            "echo jj new {sibling}",
            "git status",
            "ls | grep jj",
            "jj -R {repo} log",
            f"jj -R {FOREIGN} log",
            f"jj -R {FOREIGN} op log",
            f"jj -R {FOREIGN} --help",
            f"cd {FOREIGN} && jj st",
            "jj -R /tmp/not-a-workspace describe -m x",
            "cd /tmp && jj new",
            "echo hi # don't",
            "cat <<'EOF'\nit's\nEOF",
            "echo foo \\",
            "jj describe -m \"$(cat <<'EOF'\nit's\nEOF\n)\"",
        ],
    )
    def test_allow(self, shared_repo: Repo, command: str) -> None:
        command = command.format(repo=shared_repo.path, **vars(shared_repo))
        assert shared_repo.bash(command) is None

    def test_cd_into_registered_subdir_uses_registry(self, shared_repo: Repo) -> None:
        (shared_repo.path / "sub").mkdir()
        assert shared_repo.bash(f"jj new {shared_repo.sibling}", cwd=shared_repo.path / "sub") == "deny"

    def test_config_file_with_revset_aliases(self, shared_repo: Repo) -> None:
        (shared_repo.path / "bad.toml").write_text('[revset-aliases]\n"immutable_heads()" = "none()"\n')
        (shared_repo.path / "ok.toml").write_text("[ui]\ncolor = 'never'\n")
        assert shared_repo.bash("jj --config-file bad.toml log") == "deny"
        assert shared_repo.bash("jj --config-file=ok.toml log") is None


@needs_jj
class TestFileRules:
    @pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
    def test_inside_tree_allowed(self, repo: Repo, tool: str) -> None:
        assert repo.file(repo.path / "a.txt", tool) is None

    def test_relative_path_uses_cwd(self, repo: Repo) -> None:
        assert repo.file("a.txt") is None

    def test_notebook_path(self, repo: Repo) -> None:
        repo.jj("new", repo.sibling)
        assert repo.file(repo.path / "n.ipynb", "NotebookEdit", "notebook_path") == "deny"

    @pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
    def test_working_copy_outside_tree_denied(self, repo: Repo, tool: str) -> None:
        repo.jj("new", repo.sibling)
        assert repo.file(repo.path / "a.txt", tool) == "deny"

    def test_working_copy_on_anchor_denied(self, repo: Repo) -> None:
        repo.jj("edit", "--ignore-immutable", repo.anchor)
        assert repo.file(repo.path / "a.txt") == "deny"

    def test_outside_workspace_allowed_even_when_drifted(self, repo: Repo, tmp_path: Path) -> None:
        repo.jj("new", repo.sibling)
        assert repo.file(tmp_path / "scratch.txt") is None
        assert repo.file(Path(repo.env["HOME"]) / ".claude" / "x.json") is None

    def test_jj_dir_denied(self, repo: Repo) -> None:
        assert repo.file(repo.path / ".jj" / "repo" / "store" / "x") == "deny"
        assert repo.file(repo.path / ".jj" / "working_copy" / "x", "Edit") == "deny"

    def test_workspace_config_dir_denied(self, repo: Repo) -> None:
        config = Path(repo.env["HOME"]) / ".config" / "jj" / "workspaces"
        assert config.is_dir()
        assert repo.file(config / "abc" / "config.toml") == "deny"

    def test_symlink_into_jj_dir_denied(self, repo: Repo) -> None:
        link = repo.path / "link"
        link.symlink_to(repo.path / ".jj")
        assert repo.file(link / "repo" / "x") == "deny"

    def test_other_tools_ignored(self, repo: Repo) -> None:
        repo.jj("new", repo.sibling)
        assert repo.decision({
            "hook_event_name": "PreToolUse", "tool_name": "Read",
            "cwd": str(repo.path), "tool_input": {"file_path": str(repo.path / "a.txt")},
        }) is None


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
class TestGitFileRule:
    @pytest.fixture
    def git_repo(self, repo: Repo, tmp_path: Path) -> tuple[Path, Callable[..., str]]:
        path = tmp_path.resolve() / "gitrepo"
        path.mkdir()

        def git(*args: str) -> str:
            return subprocess.run(
                ["git", "-C", str(path), *args], env=repo.env, capture_output=True, text=True, check=True,
            ).stdout.strip()

        git("init", "-q", "-b", "main")
        git("commit", "-q", "--allow-empty", "-m", "one")
        anchor = git("rev-parse", "HEAD")
        repo.register(
            {"repo": "g", "workspace": str(path), "anchor": anchor, "mode": "isolated", "backend": "git"},
        )
        return path, git

    def test_head_is_anchor_denied(self, repo: Repo, git_repo: tuple[Path, Callable[..., str]]) -> None:
        assert repo.file(git_repo[0] / "a.txt") == "deny"

    def test_descendant_allowed(self, repo: Repo, git_repo: tuple[Path, Callable[..., str]]) -> None:
        path, git = git_repo
        git("commit", "-q", "--allow-empty", "-m", "two")
        assert repo.file(path / "a.txt") is None

    def test_unrelated_head_denied(self, repo: Repo, git_repo: tuple[Path, Callable[..., str]]) -> None:
        path, git = git_repo
        git("checkout", "-q", "--orphan", "other")
        git("commit", "-q", "--allow-empty", "-m", "unrelated")
        assert repo.file(path / "a.txt") == "deny"


    def test_drift_silent_at_the_anchor(self, repo: Repo, git_repo: tuple[Path, Callable[..., str]]) -> None:
        """A git workspace starts at its anchor (no scratch commit): that is no drift."""
        assert repo.post() is None

    def test_drift_silent_on_a_descendant(self, repo: Repo, git_repo: tuple[Path, Callable[..., str]]) -> None:
        git_repo[1]("commit", "-q", "--allow-empty", "-m", "two")
        assert repo.post() is None

    def test_drift_blocks_on_an_unrelated_head(self, repo: Repo, git_repo: tuple[Path, Callable[..., str]]) -> None:
        path, git = git_repo
        anchor = git("rev-parse", "HEAD")
        git("checkout", "-q", "--orphan", "other")
        git("commit", "-q", "--allow-empty", "-m", "unrelated")
        result = repo.hook({
            "hook_event_name": "PostToolUse", "tool_name": "Bash",
            "cwd": str(path), "tool_input": {"command": "git checkout --orphan other"},
        })
        out = json.loads(result.stdout)
        assert out["decision"] == "block"
        assert "g: HEAD" in out["reason"] and anchor in out["reason"]

    def test_drift_blocks_when_the_anchor_no_longer_resolves(
        self, repo: Repo, git_repo: tuple[Path, Callable[..., str]],
    ) -> None:
        repo.register(
            {"repo": "g", "workspace": str(git_repo[0]), "anchor": "f" * 40, "mode": "isolated", "backend": "git"},
        )
        assert repo.post() == "block"


@needs_jj
class TestPostToolUse:
    def test_in_tree_silent(self, repo: Repo) -> None:
        assert repo.post() is None

    def test_drift_blocks(self, repo: Repo) -> None:
        repo.jj("new", repo.sibling)
        result = repo.hook({
            "hook_event_name": "PostToolUse", "tool_name": "Bash",
            "cwd": str(repo.path), "tool_input": {"command": "jj new"},
        })
        out = json.loads(result.stdout)
        assert result.returncode == 0
        assert out["decision"] == "block"
        assert "repo" in out["reason"] and repo.anchor in out["reason"]
        assert "jj new" in out["reason"]

    def test_missing_anchor_blocks(self, repo: Repo) -> None:
        repo.register(
            {"repo": "repo", "workspace": str(repo.path), "anchor": "k" * 32, "mode": "isolated", "backend": "jj"},
        )
        assert repo.post() == "block"

    def test_non_bash_tool_silent(self, repo: Repo) -> None:
        repo.jj("new", repo.sibling)
        assert repo.decision({
            "hook_event_name": "PostToolUse", "tool_name": "Write",
            "cwd": str(repo.path), "tool_input": {},
        }) is None


class TestFailureModes:
    def _run(self, tmp_path: Path, registry: Path, stdin: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-P", str(HOOK)],
            input=stdin, cwd=tmp_path, capture_output=True, text=True,
            env={**os.environ, "CLD_ANCHORS_FILE": str(registry)},
        )

    @pytest.mark.parametrize("event", ["PreToolUse", "PostToolUse"])
    def test_no_registry_silent(self, tmp_path: Path, event: str) -> None:
        payload = {"hook_event_name": event, "tool_name": "Bash", "cwd": str(tmp_path), "tool_input": {"command": "jj new"}}
        result = self._run(tmp_path, tmp_path / "missing.jsonl", json.dumps(payload))
        assert (result.returncode, result.stdout) == (0, "")

    def test_empty_registry_silent(self, tmp_path: Path) -> None:
        registry = tmp_path / "anchors.jsonl"
        registry.write_text("")
        result = self._run(tmp_path, registry, "{}")
        assert (result.returncode, result.stdout) == (0, "")

    def test_malformed_registry_fails_open(self, tmp_path: Path) -> None:
        registry = tmp_path / "anchors.jsonl"
        registry.write_text("{not json\n")
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(tmp_path), "tool_input": {"command": "jj new"}}
        result = self._run(tmp_path, registry, json.dumps(payload))
        assert result.returncode == 1
        assert result.stdout == ""
        assert result.stderr

    def test_malformed_stdin_fails_open(self, tmp_path: Path) -> None:
        registry = tmp_path / "anchors.jsonl"
        registry.write_text(json.dumps(
            {"repo": "r", "workspace": str(tmp_path), "anchor": "x", "mode": "isolated", "backend": "jj"},
        ) + "\n")
        result = self._run(tmp_path, registry, "not json")
        assert (result.returncode, result.stdout) == (1, "")

    @needs_jj
    def test_jj_error_fails_open(self, repo: Repo) -> None:
        repo.register(
            {"repo": "repo", "workspace": str(repo.path), "anchor": "!!bad", "mode": "isolated", "backend": "jj"},
        )
        result = repo.hook({
            "hook_event_name": "PreToolUse", "tool_name": "Write",
            "cwd": str(repo.path), "tool_input": {"file_path": str(repo.path / "a.txt")},
        })
        assert (result.returncode, result.stdout) == (1, "")

    @needs_jj
    def test_hook_latency(self, repo: Repo) -> None:
        start = time.monotonic()
        repo.bash(f"jj new {repo.sibling}")
        assert time.monotonic() - start < 5
