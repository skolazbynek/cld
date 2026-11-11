"""Tests for broker/cld-broker.py, the host-side sshd ForceCommand, as opposed
to the container-side client (cld/broker.py, covered in test_broker.py). The
script is loaded straight from its file -- it is not part of the `cld`
package -- and docker/jj are stubbed on PATH, so each test drives the real
function against a faked host.
"""

import base64
import errno
import http.server
import importlib.util
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import NamedTuple

import pytest

from cld import docker as cld_docker
from cld.vcs import jj as cld_jj

BROKER_PY = Path(__file__).resolve().parent.parent / "broker" / "cld-broker.py"

_spec = importlib.util.spec_from_file_location("cld_broker", BROKER_PY)
broker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(broker)


@pytest.fixture(autouse=True)
def conf(monkeypatch):
    settings = {}
    monkeypatch.setattr(broker, "CONF", settings)
    return settings


@pytest.fixture(autouse=True)
def runner_image_jj(monkeypatch):
    """Runner and server images label a new enough jj. Tests of other labels still reach the stubbed docker."""
    real_label = broker._label
    monkeypatch.setattr(
        broker, "_label",
        lambda container, key: "0.45.1" if key == broker._JJ_VERSION_LABEL else real_label(container, key),
    )


@pytest.fixture(autouse=True)
def host_jj_version(monkeypatch):
    """The host jj is new enough. Tests of the version check itself put _real_host_jj_version back."""
    monkeypatch.setattr(broker, "_host_jj_version", lambda: "jj 0.45.1")


_real_host_jj_version = broker._host_jj_version
# First lines of a fake jj run by a full-script test: the broker asks `jj --version` once per process.
_FAKE_JJ_VERSION = '[ "${1:-}" != --version ] || { echo "jj 0.45.1"; exit 0; }\n'


def _fakebin(tmp_path, monkeypatch, **scripts: str) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name, body in scripts.items():
        (bindir / name).write_text("#!/usr/bin/env bash\n" + body)
        (bindir / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return bindir


def _exit_code(excinfo) -> int:
    return excinfo.value.code


def _secrets_at(path: Path) -> "broker.Secrets":
    """The Secrets a resolve of *path* gives, for tests that stub the resolver."""
    return broker.Secrets(str(path), ".", None, path.read_bytes() if path.exists() else None)


def _make_store(path: Path, layout: str = "colocated") -> Path:
    """*path* holding exactly the files the store mount rows name, as a jj repo
    (`colocated` git dir or `internal` one) or a git-only repo. Nothing runs jj."""
    git = {"colocated": ".git", "internal": ".jj/repo/store/git", "git-only": ".git"}[layout]
    (path / git / "hooks").mkdir(parents=True)
    (path / git / "config").write_text("")
    (path / git / "commondir").write_text(".")
    if layout != "git-only":
        (path / ".jj/working_copy").mkdir(parents=True)
        (path / ".jj/repo/store").mkdir(parents=True, exist_ok=True)
        (path / ".jj/repo/config-id").write_text("id")
        (path / ".jj/workspace-config-id").write_text("id")
        (path / ".jj/repo/store/git_target").write_text("../../../.git" if layout == "colocated" else "git")
    return path


class TestParseRepoArg:
    """parse_repo_arg consumes a LEADING --repo <name> / --repo=<name> -- and
    only a leading one: a --repo later in the argv belongs to the action's own
    command (e.g. pytest)."""

    @pytest.mark.parametrize(
        "argv,name,rest",
        [
            (["--repo", "lide-api", "-k", "login"], "lide-api", ["-k", "login"]),
            (["--repo=lide-api", "status"], "lide-api", ["status"]),
            (["-k", "login"], "", ["-k", "login"]),
            (["-k", "x", "--repo", "y"], "", ["-k", "x", "--repo", "y"]),
        ],
        ids=["separate-token", "equals-form", "absent", "non-leading-untouched"],
    )
    def test_parse(self, argv, name, rest):
        assert broker.parse_repo_arg(argv) == (name, rest)

    @pytest.mark.parametrize("argv", [["--repo"], ["--repo="]], ids=["bare", "empty-equals"])
    def test_missing_name_denied(self, argv, capsys):
        with pytest.raises(SystemExit) as excinfo:
            broker.parse_repo_arg(argv)
        assert _exit_code(excinfo) == 2
        assert "denied" in capsys.readouterr().err


class TestLabelCallerRepo:
    """LabelCaller.repo maps a --repo name to a repo via the caller's
    host-set flat org.cld.repo.<name> labels: ticket callers get per-name
    validation plus the single-repo default; v1 callers keep their
    repo-root and refuse --repo (design section 6.1)."""

    @pytest.fixture(autouse=True)
    def fakebin(self, tmp_path, monkeypatch):
        return _fakebin(tmp_path, monkeypatch, docker="""
cmd="$1"; shift
[ "$cmd" = inspect ] || exit 1
printf '%s' "${FAKE_LABELS:-}"
""")

    @pytest.fixture
    def repos(self, tmp_path):
        for name in ("lide-api", "diskuze-api"):
            (tmp_path / name / ".jj").mkdir(parents=True)
        return tmp_path

    def _resolve(self, monkeypatch, labels: str, kind: str, name: str, repo_preset: str = "/preset") -> str:
        monkeypatch.setenv("FAKE_LABELS", labels)
        return broker.LabelCaller("t.x", kind, repo_preset).repo(name)

    def _labels(self, repos) -> str:
        return (
            "org.cld.kind=ticket\n"
            f"org.cld.repo.lide-api={repos}/lide-api\n"
            f"org.cld.repo.diskuze-api={repos}/diskuze-api\n"
        )

    def test_named_repo_resolves_to_its_labeled_path(self, monkeypatch, repos):
        assert self._resolve(monkeypatch, self._labels(repos), "ticket", "lide-api") == f"{repos}/lide-api"

    def test_unknown_repo_denied_listing_mounted_names(self, monkeypatch, repos, capsys):
        with pytest.raises(SystemExit) as excinfo:
            self._resolve(monkeypatch, self._labels(repos), "ticket", "nope")
        assert _exit_code(excinfo) == 3
        err = capsys.readouterr().err
        assert "not mounted" in err
        assert "lide-api" in err and "diskuze-api" in err

    def test_multi_repo_without_name_denied(self, monkeypatch, repos, capsys):
        with pytest.raises(SystemExit) as excinfo:
            self._resolve(monkeypatch, self._labels(repos), "ticket", "")
        assert _exit_code(excinfo) == 3
        assert "pass --repo" in capsys.readouterr().err

    def test_single_repo_defaults_without_name(self, monkeypatch, repos):
        labels = f"org.cld.repo.lide-api={repos}/lide-api\n"
        assert self._resolve(monkeypatch, labels, "ticket", "") == f"{repos}/lide-api"

    def test_labeled_path_must_be_a_repo(self, monkeypatch, tmp_path, capsys):
        (tmp_path / "bare").mkdir()
        with pytest.raises(SystemExit) as excinfo:
            self._resolve(monkeypatch, f"org.cld.repo.bare={tmp_path}/bare\n", "ticket", "bare")
        assert _exit_code(excinfo) == 3
        assert "is not a repo" in capsys.readouterr().err

    def test_v1_kind_refuses_repo_flag(self, monkeypatch, repos, capsys):
        with pytest.raises(SystemExit) as excinfo:
            self._resolve(monkeypatch, self._labels(repos), "task-agent", "lide-api")
        assert _exit_code(excinfo) == 2
        assert "only for ticket containers" in capsys.readouterr().err

    def test_v1_kind_keeps_dispatcher_repo(self, monkeypatch, repos):
        assert self._resolve(monkeypatch, "", "task-agent", "", repo_preset="/v1/repo") == "/v1/repo"


class TestLabelCallerMountedRepo:
    """LabelCaller.mounted_repo is repo() without the refusals: a ticket's labeled
    path for a name, None for anything else -- a v1 kind mounts nothing by name."""

    @pytest.fixture(autouse=True)
    def fakebin(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_LABELS", "org.cld.kind=ticket\norg.cld.repo.lide-api=/h/lide-api\n")
        return _fakebin(tmp_path, monkeypatch, docker='printf "%s" "$FAKE_LABELS"\n')

    @pytest.mark.parametrize(
        "kind,selector,expected",
        [
            ("ticket", "lide-api", "/h/lide-api"),
            ("ticket", "nope", None),
            ("ticket", "kind", None),
            ("task-agent", "lide-api", None),
        ],
        ids=["mounted", "unknown", "non-repo-label", "v1-kind"],
    )
    def test_mounted_repo(self, kind, selector, expected):
        assert broker.LabelCaller("t.x", kind, "/root").mounted_repo(selector) == expected


def _write_registry(home: Path, body: str) -> None:
    (home / ".config" / "cld").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "cld" / "config.toml").write_text(body)


class TestHostRegistry:
    """host_registry reads the [repos.<name>] tables of the operator's user config
    with a real TOML parser; anything unreadable is an empty registry, not a crash."""

    @pytest.fixture(autouse=True)
    def home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        return tmp_path

    def test_entries(self, home):
        _write_registry(home, '''
base_image = "claude-base:latest"  # unrelated key

[repos.lide-api]
path = "/srv/lide api"  # a comment
default_rev = "main"
bootstrap = true

[repos.home-rel]
path = "~/deps"

[repos.no-path]
default_rev = "main"
''')
        assert broker.host_registry() == {"lide-api": "/srv/lide api", "home-rel": f"{home}/deps"}

    @pytest.mark.parametrize("body", [None, "repos = [", 'repos = "x"'],
                             ids=["absent", "malformed", "not-a-table"])
    def test_unreadable_is_empty(self, home, body):
        if body is not None:
            _write_registry(home, body)
        assert broker.host_registry() == {}


class TestResolveTaskAgentRepo:
    """task-agent start's --repo resolves mount-first, then the host registry --
    unlike Caller.repo, which stays mounted-only for run-tests/graphql."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        for name in ("mounted", "registered"):
            (tmp_path / name / ".jj").mkdir(parents=True)
        (tmp_path / "bare").mkdir()
        _write_registry(tmp_path, f'''
[repos.registered]
path = "{tmp_path}/registered"
[repos.bare]
path = "{tmp_path}/bare"
[repos.mounted]
path = "/registry/loses"
''')
        monkeypatch.setenv("FAKE_LABELS", f"org.cld.repo.mounted={tmp_path}/mounted\n")
        _fakebin(tmp_path, monkeypatch, docker='printf "%s" "$FAKE_LABELS"\n')

    def _resolve(self, selector: str, kind: str = "ticket") -> str:
        return broker.resolve_task_agent_repo(broker.LabelCaller("t.x", kind, "/v1/root"), selector)

    @pytest.mark.parametrize(
        "selector,expected",
        [("mounted", "mounted"), ("registered", "registered"), ("", "mounted")],
        ids=["mount-wins", "registry-fallback", "single-mount-default"],
    )
    def test_resolves(self, tmp_path, selector, expected):
        assert self._resolve(selector) == f"{tmp_path}/{expected}"

    def test_v1_kind_reaches_the_registry(self, tmp_path):
        assert self._resolve("registered", kind="task-agent") == f"{tmp_path}/registered"

    def test_neither_mounted_nor_registered_denied(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            self._resolve("nope")
        assert _exit_code(excinfo) == 3
        err = capsys.readouterr().err
        assert "not one of t.x's mounted repos" in err
        assert "cld repos add nope" in err

    def test_registered_path_must_be_a_repo(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            self._resolve("bare")
        assert _exit_code(excinfo) == 3
        assert "is not a repo" in capsys.readouterr().err

    def test_several_mounts_without_name_denied_listing_them(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("FAKE_LABELS", f"org.cld.repo.a={tmp_path}/mounted\norg.cld.repo.b={tmp_path}/mounted\n")
        with pytest.raises(SystemExit) as excinfo:
            self._resolve("")
        assert _exit_code(excinfo) == 3
        assert "pass --repo <name> (mounted: a b)" in capsys.readouterr().err


class TestDispatchTaskAgent:
    """Full-script sshd route for the task-agent and list-repos actions from a
    ticket caller: start reaches a mounted or registry-only repo, and the other
    ops run with no repo at all -- they used to be refused for every ticket."""

    @pytest.fixture(autouse=True)
    def fakebin(self, tmp_path, monkeypatch):
        return _fakebin(tmp_path, monkeypatch, docker="""
cmd="$1"; shift
[ "$cmd" = inspect ] || exit 1
case "$*" in
  *org.cld.kind*) printf 'ticket\\n' ;;
  *range*)        printf '%s' "${FAKE_LABELS:-}" ;;
esac
""", cld="""
printf 'CWD %s\\n' "$PWD"
printf '%s\\n' "$@"
""")

    @pytest.fixture
    def home(self, tmp_path):
        for name in ("mounted", "registered"):
            (tmp_path / name / ".jj").mkdir(parents=True)
        _write_registry(tmp_path, f'''
[repos.registered]
path = "{tmp_path}/registered"
[repos.mounted]
path = "{tmp_path}/mounted"
''')
        return tmp_path

    def _dispatch(self, home: Path, action: str, argv: list) -> subprocess.CompletedProcess:
        payload = base64.b64encode(b"".join(a.encode() + b"\0" for a in argv)).decode()
        env = dict(os.environ, CLD_BROKER_CONF="/nonexistent", HOME=str(home),
                   SSH_ORIGINAL_COMMAND=f"{action} t.x {payload}",
                   FAKE_LABELS=f"org.cld.repo.mounted={home}/mounted\norg.cld.repo.other={home}/mounted\n")
        return subprocess.run([sys.executable, str(BROKER_PY)], capture_output=True, text=True, env=env, cwd=home)

    @pytest.mark.parametrize("name", ["mounted", "registered"])
    def test_start_runs_in_the_selected_repo(self, home, name):
        result = self._dispatch(home, "task-agent", ["start", "--repo", name, "@task", "-n", "s"])
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [
            f"CWD {home}/{name}", "task-agent", "start", "--parent", "t.x", "@task", "-n", "s"]

    def test_start_on_a_git_repo_is_denied_before_cld_runs(self, home):
        (home / "gitrepo" / ".git").mkdir(parents=True)
        _write_registry(home, f"""
[repos.gitrepo]
path = "{home}/gitrepo"
""")
        result = self._dispatch(home, "task-agent", ["start", "--repo", "gitrepo", "@task", "-n", "s"])
        assert result.returncode == 3
        assert "task-agents need a jj repo" in result.stderr
        assert result.stdout == ""

    def test_start_without_repo_on_several_mounts_denied(self, home):
        result = self._dispatch(home, "task-agent", ["start", "@task", "-n", "s"])
        assert result.returncode == 3
        assert "pass --repo" in result.stderr

    def test_start_still_refuses_a_bare_path(self, home):
        result = self._dispatch(home, "task-agent", ["start", "--repo", "mounted", "/etc/passwd"])
        assert result.returncode == 2
        assert "must be an @ref" in result.stderr

    @pytest.mark.parametrize("arg", ["--force", "--parent=cld_other"])
    def test_spoofing_still_denied(self, home, arg):
        result = self._dispatch(home, "task-agent", ["shutdown", "slug", arg])
        assert result.returncode == 2
        assert "denied" in result.stderr

    @pytest.mark.parametrize(
        "argv,forwarded",
        [
            (["status"], ["status", "--parent", "t.x"]),
            (["logs", "slug", "-n", "5"], ["logs", "slug", "-n", "5"]),
            (["shutdown", "--all"], ["shutdown", "--parent", "t.x", "--all"]),
        ],
        ids=["status", "logs", "shutdown"],
    )
    def test_other_ops_need_no_repo(self, home, argv, forwarded):
        result = self._dispatch(home, "task-agent", argv)
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [f"CWD {home}", "task-agent", *forwarded]

    def test_list_repos_marks_the_callers_mounts(self, home):
        result = self._dispatch(home, "list-repos", [])
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [
            f"registered\t{home}/registered\tno", f"mounted\t{home}/mounted\tyes"]

    def test_list_repos_empty_registry(self, tmp_path):
        result = self._dispatch(tmp_path, "list-repos", [])
        assert (result.returncode, result.stdout) == (0, "")


class TestDispatchRunTests:
    """Full-script dispatch: SSH_ORIGINAL_COMMAND in, `docker run` argv out --
    proves the dispatcher's kind branch, LabelCaller.repo and the --repo
    argv consumption compose for both ticket and v1 callers."""

    @pytest.fixture(autouse=True)
    def fakebin(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECRETS_STAGE_DIR", str(tmp_path / "stage"))
        return _fakebin(tmp_path, monkeypatch, docker="""
cmd="$1"; shift
case "$cmd" in
  inspect)
    fmt=""
    while [ $# -gt 0 ]; do
      case "$1" in --format) fmt="$2"; shift 2 ;; *) shift ;; esac
    done
    case "$fmt" in
      *org.cld.jj-version*) printf '%s\\n' "${FAKE_JJ_VERSION-0.45.1}" ;;
      *org.cld.kind*)      printf '%s\\n' "${FAKE_KIND:-}" ;;
      *org.cld.repo-root*) printf '%s\\n' "${FAKE_REPO_ROOT:-}" ;;
      *.Mounts*)           printf '%s' "${FAKE_AGENT:-}" ;;
      *range*)             printf '%s' "${FAKE_LABELS:-}" ;;
    esac
    ;;
  image) echo sha256:img123 ;;
  run) echo "DOCKER_RUN $*" ;;
esac
""", jj=_FAKE_JJ_VERSION + "echo rev123\n")

    @pytest.fixture
    def repos(self, tmp_path):
        for name in ("lide-api", "diskuze-api"):
            _make_store(tmp_path / name)
        return tmp_path

    def _dispatch(self, session: str, argv: list, env_overrides: dict) -> subprocess.CompletedProcess:
        payload = base64.b64encode(b"".join(a.encode() + b"\0" for a in argv)).decode()
        env = dict(os.environ)
        env["CLD_BROKER_CONF"] = "/nonexistent"
        env["SSH_ORIGINAL_COMMAND"] = f"run-tests {session} {payload}"
        env["VENV_CACHE_ROOT"] = ""
        env.update(env_overrides)
        return subprocess.run([sys.executable, str(BROKER_PY)], capture_output=True, text=True, env=env)

    def test_ticket_caller_with_repo_flag(self, repos):
        labels = (
            f"org.cld.repo.lide-api={repos}/lide-api\n"
            f"org.cld.repo.diskuze-api={repos}/diskuze-api\n"
        )
        result = self._dispatch(
            "t.x", ["--repo", "diskuze-api", "-k", "login"],
            {"FAKE_KIND": "ticket", "FAKE_LABELS": labels},
        )
        assert result.returncode == 0, result.stderr
        assert f"--mount type=bind,src={repos}/diskuze-api/.jj,dst=/repo/.jj " in result.stdout
        assert f"{repos}/diskuze-api:/repo" not in result.stdout
        assert "-e REVISION=rev123" in result.stdout
        assert result.stdout.rstrip().endswith("runtests:latest -k login")
        assert "--repo" not in result.stdout

    def test_ticket_caller_multi_repo_without_flag_denied(self, repos):
        labels = (
            f"org.cld.repo.lide-api={repos}/lide-api\n"
            f"org.cld.repo.diskuze-api={repos}/diskuze-api\n"
        )
        result = self._dispatch(
            "t.x", ["-k", "login"],
            {"FAKE_KIND": "ticket", "FAKE_LABELS": labels},
        )
        assert result.returncode == 3
        assert "pass --repo" in result.stderr

    def test_v1_caller_unchanged(self, repos):
        result = self._dispatch(
            "a.t1.fix", ["-k", "login"],
            {"FAKE_KIND": "task-agent", "FAKE_REPO_ROOT": f"{repos}/lide-api"},
        )
        assert result.returncode == 0, result.stderr
        assert f"--mount type=bind,src={repos}/lide-api/.jj,dst=/repo/.jj " in result.stdout
        assert f"{repos}/lide-api:/repo" not in result.stdout
        assert result.stdout.rstrip().endswith("runtests:latest -k login")

    @pytest.mark.parametrize("version", ["0.37.0", ""], ids=["old-jj", "no-label"])
    def test_runner_image_with_an_old_jj_denied(self, repos, version):
        result = self._dispatch(
            "a.t1.fix", ["-k", "login"],
            {"FAKE_KIND": "task-agent", "FAKE_REPO_ROOT": f"{repos}/lide-api", "FAKE_JJ_VERSION": version},
        )
        assert result.returncode == 3
        assert "org.cld.jj-version" in result.stderr
        assert "DOCKER_RUN" not in result.stdout

    def test_venv_cache_wired(self, repos, tmp_path):
        cache = tmp_path / "cache"
        result = self._dispatch(
            "a.t1.fix", ["-k", "login"],
            {"FAKE_KIND": "task-agent", "FAKE_REPO_ROOT": f"{repos}/lide-api",
             "VENV_CACHE_ROOT": str(cache)},
        )
        assert result.returncode == 0, result.stderr
        own = broker.venv_cache_dir(str(cache), str(repos / "lide-api"))
        assert f"-v {own}:/cache" in result.stdout
        assert f"-v {cache}:/cache" not in result.stdout
        assert "-e VENV_CACHE_IMAGE_ID=sha256:img123" in result.stdout
        assert all((Path(own) / d).is_dir() for d in ("venvs", "poetry", "locks"))
        assert not any((cache / d).exists() for d in ("venvs", "poetry", "locks"))

    def test_venv_cache_disabled(self, repos):
        result = self._dispatch(
            "a.t1.fix", ["-k", "login"],
            {"FAKE_KIND": "task-agent", "FAKE_REPO_ROOT": f"{repos}/lide-api"},
        )
        assert result.returncode == 0, result.stderr
        assert ":/cache" not in result.stdout
        assert "VENV_CACHE_IMAGE_ID" not in result.stdout

    def test_callers_agent_mounted(self, repos, tmp_path):
        """The run gets the ssh-agent socket already mounted into the caller."""
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(str(tmp_path / "agent.sock"))
            result = self._dispatch(
                "a.t1.fix", ["-k", "login"],
                {"FAKE_KIND": "task-agent", "FAKE_REPO_ROOT": f"{repos}/lide-api",
                 "FAKE_AGENT": str(tmp_path / "agent.sock")},
            )
        assert result.returncode == 0, result.stderr
        assert f"-v {tmp_path}/agent.sock:/run/ssh-agent.sock -e SSH_AUTH_SOCK=/run/ssh-agent.sock" in result.stdout

    @pytest.mark.parametrize("agent,warned", [("", False), ("/gone/agent.sock", True)],
                             ids=["caller-has-none", "dead-socket"])
    def test_no_usable_agent_runs_keyless(self, repos, agent, warned):
        result = self._dispatch(
            "a.t1.fix", ["-k", "login"],
            {"FAKE_KIND": "task-agent", "FAKE_REPO_ROOT": f"{repos}/lide-api", "FAKE_AGENT": agent},
        )
        assert result.returncode == 0, result.stderr
        assert "SSH_AUTH_SOCK" not in result.stdout
        assert ("not a live socket" in result.stderr) == warned

    def test_v1_caller_repo_flag_denied(self, repos):
        result = self._dispatch(
            "a.t1.fix", ["--repo", "lide-api", "-k", "login"],
            {"FAKE_KIND": "task-agent", "FAKE_REPO_ROOT": f"{repos}/lide-api"},
        )
        assert result.returncode == 2
        assert "only for ticket containers" in result.stderr

    @pytest.mark.parametrize(
        "command,message",
        [
            ("Deploy a.t1.x", "bad action"),
            ("deploy a.t1.x", "unknown action 'deploy'"),
            ("run-tests ../../etc/passwd", "bad session id"),
            ("run-tests t..x", "bad session id"),
        ],
        ids=["bad-action", "unknown-action", "bad-session", "double-dot-session"],
    )
    def test_malformed_command_denied(self, command, message):
        result = self._dispatch("", [], {"SSH_ORIGINAL_COMMAND": command})
        assert result.returncode == 2
        assert message in result.stderr


class TestSessionFormat:
    @pytest.mark.parametrize("session", ["t.x", "a._host.y-2", "a.lide-1.fix", "run.ab12cd", "cld_host_repo"])
    def test_accepted(self, session):
        assert broker._session_ok(session)

    @pytest.mark.parametrize("session", ["t..x", "t/x", ".t", "", "../x", "t x", "-t"],
                             ids=["double-dot", "slash", "leading-dot", "empty", "parent-dir", "space", "leading-dash"])
    def test_refused(self, session):
        assert not broker._session_ok(session)


class TestDispatchCli:
    """Full-script host route (`cld-brokerctl run`): arguments on the script's own
    command line select CliCaller, so the caller's repo, revision, session and
    targets come from flags and cwd, with no container or docker label read."""

    @pytest.fixture(autouse=True)
    def fakebin(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECRETS_STAGE_DIR", str(tmp_path / "stage"))
        return _fakebin(tmp_path, monkeypatch, docker="""
case "$1" in
  inspect) printf '%s\\n' "${FAKE_JJ_VERSION-0.45.1}" ;;
  run) printf 'DOCKER_RUN\\n'; printf '%s\\n' "$@" ;;
esac
""", jj=_FAKE_JJ_VERSION + """
printf '%s\\n' "$*" >> "$FAKE_JJ_LOG"
echo rev123
""", cld="""
printf '%s\\n' "$@"
""")

    @pytest.fixture
    def repo(self, tmp_path):
        _make_store(tmp_path / "my.repo")
        (tmp_path / "my.repo" / "sub").mkdir()
        return tmp_path / "my.repo"

    def _run(self, tmp_path, cwd: Path, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ, CLD_BROKER_CONF="/nonexistent", FAKE_JJ_LOG=str(tmp_path / "jj.log"),
                   VENV_CACHE_ROOT="")
        env.pop("SSH_ORIGINAL_COMMAND", None)
        return subprocess.run([sys.executable, str(BROKER_PY), *args], cwd=cwd,
                              capture_output=True, text=True, env=env)

    def test_run_tests_defaults_from_cwd(self, tmp_path, repo):
        result = self._run(tmp_path, repo / "sub", "run-tests", "-k", "two words", "-r", "x")
        assert result.returncode == 0, result.stderr
        lines = result.stdout.splitlines()
        assert f"type=bind,src={repo}/.jj,dst=/repo/.jj" in lines
        assert f"{repo}:/repo" not in lines
        assert "REVISION=rev123" in lines
        assert lines[-5:] == ["runtests:latest", "-k", "two words", "-r", "x"]
        assert (tmp_path / "jj.log").read_text().splitlines() == [
            f"-R {repo} log --no-graph -n1 -r @ -T commit_id"]

    def test_revset_and_repo_flags(self, tmp_path, repo):
        result = self._run(tmp_path, tmp_path, "-C", str(repo), "-r", "main", "run-tests")
        assert result.returncode == 0, result.stderr
        assert "-r main" in (tmp_path / "jj.log").read_text()

    def test_ticket_repo_selector_denied(self, tmp_path, repo):
        result = self._run(tmp_path, repo, "run-tests", "--repo", "lide-api")
        assert result.returncode == 2
        assert "pass -C <path>" in result.stderr

    def test_no_repo_above_cwd(self, tmp_path):
        result = self._run(tmp_path, tmp_path, "list-containers")
        assert result.returncode == 3
        assert "no repo at or above" in result.stderr

    @pytest.mark.parametrize(
        "flags,parent",
        [([], "cld_host_my-repo"), (["-s", "cld_mine"], "cld_mine")],
        ids=["default-session-sanitized", "explicit-session"],
    )
    def test_session_becomes_task_agent_parent(self, tmp_path, repo, flags, parent):
        result = self._run(tmp_path, repo, *flags, "task-agent", "status")
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == ["task-agent", "status", "--parent", parent]

    def test_start_reaches_a_registered_repo(self, tmp_path, repo, monkeypatch):
        (tmp_path / "other" / ".jj").mkdir(parents=True)
        _write_registry(tmp_path, f'[repos.other]\npath = "{tmp_path}/other"\n')
        monkeypatch.setenv("HOME", str(tmp_path))
        result = self._run(tmp_path, repo, "task-agent", "start", "--repo", "other", "@task")
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines()[:5] == ["task-agent", "start", "--parent", "cld_host_my-repo", "@task"]

    def test_bad_session_denied(self, tmp_path, repo):
        result = self._run(tmp_path, repo, "-s", "../x", "list-containers")
        assert result.returncode == 2
        assert "bad session id" in result.stderr


class TestParseEnvFile:
    """broker.conf and the repo .env are read, never sourced: shell quoting is
    honored, but nothing is expanded or executed."""

    @pytest.mark.parametrize(
        "line,expected",
        [
            ("A=plain", {"A": "plain"}),
            ("export A=exported", {"A": "exported"}),
            ('A="two words"', {"A": "two words"}),
            ("A='single $HOME'", {"A": "single $HOME"}),
            ('A="$HOME"', {"A": "$HOME"}),
            ("A=value # comment", {"A": "value"}),
            ("A=pass#word", {"A": "pass#word"}),
            ("# A=commented", {}),
            ("echo hi", {}),
            ("A='unterminated", {}),
        ],
        ids=["plain", "export", "double-quoted", "single-quoted", "no-expansion",
             "trailing-comment", "hash-inside-value", "comment-line", "command", "bad-quoting"],
    )
    def test_line(self, tmp_path, line, expected):
        path = tmp_path / ".env"
        path.write_text(line + "\n")
        assert broker.parse_env_file(str(path)) == expected

    def test_conf_updates_only_exported_variables(self, tmp_path, monkeypatch, conf):
        path = tmp_path / "broker.conf"
        path.write_text("PATH=/conf/bin\nRUNTESTS_IMAGE=custom:1\n")
        monkeypatch.setenv("CLD_BROKER_CONF", str(path))
        monkeypatch.setenv("PATH", "/env/bin")
        monkeypatch.delenv("RUNTESTS_IMAGE", raising=False)
        broker.load_conf()
        assert os.environ["PATH"] == "/conf/bin"
        assert "RUNTESTS_IMAGE" not in os.environ
        assert broker._setting("RUNTESTS_IMAGE") == "custom:1"
        assert conf["PATH"] == "/conf/bin"


class TestCheckUrlAllowlisted:
    """C1: userinfo (user:pass@) must be stripped before the port, or
    'allowed.example:80@evil.com' passes the allowlist while the request
    actually connects to evil.com."""

    @pytest.fixture(autouse=True)
    def allowlist(self, conf):
        conf["GRAPHQL_URL_ALLOWLIST"] = "allowed.example"

    @pytest.mark.parametrize(
        "url,allowed",
        [
            ("http://allowed.example/graphql", True),
            ("http://evil.com/graphql", False),
            ("http://evilallowed.example/graphql", False),
            ("http://allowed.example:80@evil.com/", False),
            ("http://169.254.169.254/latest/meta-data/@allowed.example/", False),
            ("http:///graphql", False),
        ],
    )
    def test_allowlist_decision(self, url, allowed, capsys):
        if allowed:
            broker.check_url_allowlisted(url)
            return
        with pytest.raises(SystemExit) as excinfo:
            broker.check_url_allowlisted(url)
        assert _exit_code(excinfo) != 0
        assert "denied" in capsys.readouterr().err

    def test_userinfo_bypass_resolves_to_real_host_not_allowlisted_prefix(self, capsys):
        """Regression pin for the exact C1 bug: prove the denial names the real
        connect target (evil.com), not the allowlisted-looking prefix."""
        with pytest.raises(SystemExit):
            broker.check_url_allowlisted("http://allowed.example:80@evil.com/")
        err = capsys.readouterr().err
        assert "evil.com" in err
        assert "allowed.example" not in err.split("not in")[0]


class TestMaskOutput:
    @pytest.mark.parametrize(
        "raw,masked",
        [
            (b"DB_PASSWORD=hunter2, next", b"DB_PASSWORD=<redacted>, next"),
            (b"see /run/secrets/db.pw now", b"see /run/secrets/<redacted> now"),
            (b"mysql://root:pw@db:3306/x", b"mysql://<redacted>@db:3306/x"),
            (b'{"Token": "abc"}', b'{"Token": "<redacted>"}'),
            (b'{"token":\n"abc"}', b'{"token":\n"abc"}'),
            (b"\xff token=x\n", b"\xff token=<redacted>\n"),
        ],
        ids=["kv", "secrets-path", "url-userinfo", "json", "json-across-lines-untouched", "invalid-utf8"],
    )
    def test_mask(self, raw, masked):
        assert broker.mask_output(raw) == masked


class TestCapOutput:
    @pytest.mark.parametrize(
        "keep,expected",
        [("head", b"abc"), ("tail", b"def")],
    )
    def test_truncates_and_reports_on_stderr(self, keep, expected, capsysbinary):
        broker.cap_output(b"abcdef\n\n", 3, keep)
        out, err = capsysbinary.readouterr()
        assert out == expected
        assert b"truncated to 3 of 6 bytes" in err

    def test_under_limit_drops_trailing_newlines_only(self, capsysbinary):
        broker.cap_output(b"a\nb\n\n", 100, "head")
        out, err = capsysbinary.readouterr()
        assert out == b"a\nb"
        assert err == b""


class TestGraphqlEndpoints:
    """M2: do_graphql_endpoints must return cleanly (no output) for a secrets
    file with zero CLD_GRAPHQL_URL_* aliases, or none at all."""

    def _endpoints(self, monkeypatch, secrets: Path, capsys) -> str:
        monkeypatch.setattr(broker, "resolve_secrets_env_file", lambda repo, stage: _secrets_at(secrets))
        broker.do_graphql_endpoints(broker.Scope("a.t1.test", "/repo"))
        return capsys.readouterr().out

    def test_empty_aliases_returns_cleanly(self, tmp_path, monkeypatch, capsys):
        secrets = tmp_path / ".env"
        secrets.write_text("SOME_OTHER_VAR=1\n")
        assert self._endpoints(monkeypatch, secrets, capsys) == ""

    def test_missing_secrets_file_returns_cleanly(self, tmp_path, monkeypatch, capsys):
        assert self._endpoints(monkeypatch, tmp_path / "does-not-exist.env", capsys) == ""

    def test_lists_configured_aliases(self, tmp_path, monkeypatch, capsys):
        secrets = tmp_path / ".env"
        secrets.write_text(
            "CLD_GRAPHQL_URL_DEV=http://dev.internal/graphql\n"
            "CLD_GRAPHQL_URL_STAGING_EU=http://staging.internal/graphql\n"
        )
        assert set(self._endpoints(monkeypatch, secrets, capsys).split()) == {"dev", "staging-eu"}


class TestResolveTargetAlias:
    def test_alias_url_and_credentials_come_from_the_env_file(self, tmp_path, monkeypatch):
        secrets = tmp_path / ".env"
        secrets.write_text(
            "CLD_GRAPHQL_URL_STAGING_EU=https://staging/graphql\n"
            "CLD_GRAPHQL_AUTH_STAGING_EU='Bearer abc'\n"
            'CLD_GRAPHQL_COOKIE_STAGING_EU="session=1; x=2"\n'
        )
        monkeypatch.setattr(broker, "resolve_secrets_env_file", lambda repo, stage: _secrets_at(secrets))
        ctx = broker.Scope("a.t1.test", "/repo")
        assert broker.resolve_target(ctx, "staging-eu") == ("https://staging/graphql", "Bearer abc", "session=1; x=2")

    def test_unknown_alias_denied(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(broker, "resolve_secrets_env_file",
                            lambda repo, stage: _secrets_at(tmp_path / ".env"))
        with pytest.raises(SystemExit) as excinfo:
            broker.resolve_target(broker.Scope("a.t1.test", "/repo"), "nope")
        assert _exit_code(excinfo) == 3
        assert "CLD_GRAPHQL_URL_NOPE" in capsys.readouterr().err


class TestResolveTargetLocal:
    """`local` is the session's own server, found via `docker port`; the
    operator's CLD_GRAPHQL_AUTH_LOCAL / CLD_GRAPHQL_COOKIE_LOCAL from the
    repo's .env ride along, so a signed-in query works without the container
    ever holding the credential."""

    @pytest.fixture(autouse=True)
    def fakebin(self, tmp_path, monkeypatch):
        monkeypatch.setattr(broker, "resolve_graphql_config", lambda repo: broker.GraphqlConfig("serve", "8000", "/graphql"))
        return _fakebin(tmp_path, monkeypatch, docker='[ "$1" = port ] && echo "172.17.0.1:32768"\n')

    def _resolve(self, monkeypatch, secrets: Path) -> tuple[str, str, str]:
        monkeypatch.setattr(broker, "resolve_secrets_env_file", lambda repo, stage: _secrets_at(secrets))
        return broker.resolve_target(broker.Scope("a.t1.test", "/repo"), "local")

    def test_credentials_come_from_the_env_file(self, tmp_path, monkeypatch):
        secrets = tmp_path / ".env"
        secrets.write_text("CLD_GRAPHQL_AUTH_LOCAL='Bearer abc'\nCLD_GRAPHQL_COOKIE_LOCAL=sid=1\n")
        assert self._resolve(monkeypatch, secrets) == ("http://172.17.0.1:32768/graphql", "Bearer abc", "sid=1")

    def test_anonymous_without_credentials(self, tmp_path, monkeypatch):
        assert self._resolve(monkeypatch, tmp_path / "missing.env") == ("http://172.17.0.1:32768/graphql", "", "")


class TestGraphqlStatusStale:
    """M5: do_graphql_status's 6th field (stale) compares the container's own
    served revision (REVISION env) against the session's current jj tip, as
    LabelCaller.revision resolves it.
    The stubbed `docker port` returns nothing, keeping the server in the
    'starting' state -- still enough to exercise the stale computation, which
    happens before the port/probe branching."""

    @pytest.fixture(autouse=True)
    def fakebin(self, tmp_path, monkeypatch):
        _make_store(tmp_path)
        return _fakebin(tmp_path, monkeypatch, docker="""
case "$1" in
  ps) exit 0 ;;
  inspect)
    shift; shift
    fmt=""
    while [ $# -gt 0 ]; do
      case "$1" in --format) fmt="$2"; shift 2 ;; *) shift ;; esac
    done
    if [ -z "$fmt" ]; then
      [ "${FAKE_CONTAINER_EXISTS:-1}" = 1 ] && exit 0 || exit 1
    fi
    case "$fmt" in
      '{{.State.Running}}') echo "${FAKE_RUNNING:-true}" ;;
      '{{range .Config.Env}}{{println .}}{{end}}') printf 'REVISION=%s\\n' "${FAKE_REV:-}" ;;
    esac
    ;;
  port) exit 0 ;;
esac
""", jj="""
[ "${FAKE_TIP_FAILS:-0}" = 1 ] && exit 1
echo "${FAKE_TIP:-}"
""")

    def _status(self, monkeypatch, tmp_path, capsys, **env) -> list:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        tip = broker.LabelCaller("a.t1.test", "task-agent", str(tmp_path)).revision(str(tmp_path))
        broker.do_graphql_status(broker.Scope("a.t1.test", str(tmp_path)), tip)
        return capsys.readouterr().out.rstrip("\n").split("\t")

    def test_matching_revision_is_not_stale(self, monkeypatch, tmp_path, capsys):
        fields = self._status(monkeypatch, tmp_path, capsys, FAKE_REV="abc123", FAKE_TIP="abc123")
        assert fields[0] == "starting"
        assert fields[3] == "abc123"
        assert fields[5] == "false"

    def test_mismatched_revision_is_stale(self, monkeypatch, tmp_path, capsys):
        fields = self._status(monkeypatch, tmp_path, capsys, FAKE_REV="abc123", FAKE_TIP="def456")
        assert fields[0] == "starting"
        assert fields[5] == "true"

    def test_unresolvable_tip_leaves_stale_empty(self, monkeypatch, tmp_path, capsys):
        fields = self._status(monkeypatch, tmp_path, capsys, FAKE_REV="abc123", FAKE_TIP_FAILS="1")
        assert fields[0] == "starting"
        assert fields[5] == ""

    def test_not_started_stale_empty(self, monkeypatch, tmp_path, capsys):
        fields = self._status(monkeypatch, tmp_path, capsys, FAKE_CONTAINER_EXISTS="0")
        assert fields == ["not_started", "", "", "", "", ""]

    def test_exited(self, monkeypatch, tmp_path, capsys):
        fields = self._status(monkeypatch, tmp_path, capsys, FAKE_RUNNING="false")
        assert fields == ["exited", "", "", "", "cld_gql_a.t1.test", ""]


class TestGraphqlStartRepoMismatch:
    """do_graphql_start's idempotence is per repo: a server already running
    for repo A answering `graphql start --repo B` must refuse and name A --
    not hand a multi-repo ticket the wrong repo's endpoint. Same-repo (and
    pre-label, empty-label) reuse stays idempotent."""

    @pytest.fixture(autouse=True)
    def fakebin(self, tmp_path, monkeypatch):
        monkeypatch.setattr(broker, "sweep_gql_orphans", lambda: None)
        monkeypatch.setattr(broker, "resolve_graphql_context", lambda repo: (
            broker.Secrets("/none/.env", ".", None, None), broker.GraphqlConfig("serve", "8000", "/graphql")))
        monkeypatch.setattr(broker, "do_graphql_status", lambda scope, tip: print("STATUS_CALLED"))
        return _fakebin(tmp_path, monkeypatch, docker="""
[ "$1" = inspect ] || exit 1
shift
fmt=""
while [ $# -gt 0 ]; do
  case "$1" in --format) fmt="$2"; shift 2 ;; *) shift ;; esac
done
case "$fmt" in
  '') exit 0 ;;
  '{{.State.Running}}') echo true ;;
  *gql-repo*) printf '%s\\n' "${FAKE_GQL_REPO:-}" ;;
esac
""")

    def _start(self, monkeypatch, repo: str, gql_repo: str) -> None:
        monkeypatch.setenv("FAKE_GQL_REPO", gql_repo)
        broker.do_graphql_start(broker.Scope("t.x", repo), "rev", "")

    def test_cross_repo_reuse_refused_naming_the_running_repo(self, monkeypatch, capsys):
        with pytest.raises(SystemExit) as excinfo:
            self._start(monkeypatch, repo="/repos/b", gql_repo="/repos/a")
        assert _exit_code(excinfo) == 3
        out, err = capsys.readouterr()
        assert "denied" in err
        assert "/repos/a" in err
        assert "graphql stop" in err
        assert "STATUS_CALLED" not in out

    def test_same_repo_reuse_stays_idempotent(self, monkeypatch, capsys):
        self._start(monkeypatch, repo="/repos/a", gql_repo="/repos/a")
        assert "STATUS_CALLED" in capsys.readouterr().out

    def test_unlabeled_server_stays_idempotent(self, monkeypatch, capsys):
        # v1 / pre-label containers carry no org.cld.gql-repo label.
        self._start(monkeypatch, repo="/repos/a", gql_repo="")
        assert "STATUS_CALLED" in capsys.readouterr().out


class TestGraphqlLogs:
    """`logs` with no server is a clear broker message, not docker's raw
    "No such container" error."""

    def _logs(self, tmp_path, monkeypatch, exists: bool) -> None:
        _fakebin(tmp_path, monkeypatch, docker=f"""
case "$1" in
  inspect) exit {0 if exists else 1} ;;
  logs) echo "server line" ;;
esac
""")
        broker.action_graphql(broker.CliCaller("a.t1.test", "/repo", "@"), ["logs", "5"])

    def test_no_server(self, tmp_path, monkeypatch, capsys):
        with pytest.raises(SystemExit) as excinfo:
            self._logs(tmp_path, monkeypatch, exists=False)
        assert _exit_code(excinfo) == 3
        out, err = capsys.readouterr()
        assert "no graphql server for a.t1.test" in err
        assert out == ""

    def test_running_server(self, tmp_path, monkeypatch, capsysbinary):
        with pytest.raises(SystemExit) as excinfo:
            self._logs(tmp_path, monkeypatch, exists=True)
        assert _exit_code(excinfo) == 0
        assert capsysbinary.readouterr().out == b"server line"


class TestGraphqlQuery:
    """do_graphql_query builds the body with json.dumps and returns the
    response body followed by a newline; a non-2xx status exits 1."""

    @pytest.fixture
    def sent(self, monkeypatch):
        sent = {}

        def fake_post(url, body, headers, timeout):
            sent.update(url=url, body=body, headers=headers)
            return sent.get("status", 200), body

        monkeypatch.setattr(broker, "resolve_target", lambda scope, target: ("http://stub/graphql", "Bearer t", ""))
        monkeypatch.setattr(broker, "http_post", fake_post)
        return sent

    def _query(self, query="{ __typename }", variables=""):
        broker.do_graphql_query(broker.Scope("a.t1.test", "/repo"), "local", query, variables)

    def test_body_reaches_the_request_and_response_is_returned(self, sent, capsys):
        self._query()
        out = capsys.readouterr().out
        assert out.endswith("\n")
        assert json.loads(out) == {"query": "{ __typename }", "variables": {}}
        assert sent["headers"] == {"Content-Type": "application/json", "Authorization": "Bearer t"}

    def test_variables_are_merged_into_the_body(self, sent, capsys):
        self._query("query T($n: String!) { __type(name: $n) { name } }", '{"n": "Query"}')
        assert json.loads(capsys.readouterr().out)["variables"] == {"n": "Query"}

    def test_malformed_variables_denied(self, sent, capsys):
        with pytest.raises(SystemExit) as excinfo:
            self._query(variables="{nope")
        assert _exit_code(excinfo) == 2
        assert "malformed variables JSON" in capsys.readouterr().err

    def test_non_2xx_exits_1_after_printing_the_body(self, sent, capsys):
        sent["status"] = 502
        with pytest.raises(SystemExit) as excinfo:
            self._query()
        assert _exit_code(excinfo) == 1
        out, err = capsys.readouterr()
        assert json.loads(out)["query"] == "{ __typename }"
        assert "HTTP 502" in err


class TestHttpPost:
    """http_post keeps curl -sS's contract: any HTTP status is a response,
    redirects are not followed, URL userinfo becomes basic auth, and only a
    transport failure returns None."""

    @pytest.fixture
    def server(self):
        seen = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                seen.update(path=self.path, body=body, auth=self.headers.get("Authorization"))
                status = {"/redirect": 302, "/fail": 500}.get(self.path, 200)
                self.send_response(status)
                if status == 302:
                    self.send_header("Location", "/elsewhere")
                self.end_headers()
                self.wfile.write(b"reply:" + body)

            def log_message(self, *args):
                pass

        httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        yield f"127.0.0.1:{httpd.server_address[1]}", seen
        httpd.shutdown()

    @pytest.mark.parametrize(
        "path,status",
        [("/graphql", 200), ("/redirect", 302), ("/fail", 500)],
        ids=["ok", "redirect-not-followed", "error-status"],
    )
    def test_status_and_body(self, server, path, status):
        address, seen = server
        assert broker.http_post(f"http://{address}{path}", b"{}", {}, 5) == (status, b"reply:{}")
        assert seen["path"] == path

    def test_userinfo_becomes_basic_auth(self, server):
        address, seen = server
        broker.http_post(f"http://user:p%40ss@{address}/graphql", b"{}", {}, 5)
        assert seen["auth"] == "Basic " + base64.b64encode(b"user:p@ss").decode()

    def test_transport_failure_is_none(self, capsys):
        assert broker.http_post("http://127.0.0.1:1/graphql", b"{}", {}, 5) is None
        assert "graphql request failed" in capsys.readouterr().err


class TestDecodeArgv:
    @pytest.mark.parametrize(
        "raw,argv",
        [(b"", []), (b"a\0b\0", ["a", "b"]), (b"a\0\0b", ["a", "", "b"])],
        ids=["empty", "nul-terminated", "empty-arg-kept"],
    )
    def test_decode(self, raw, argv):
        assert broker.decode_argv(base64.b64encode(raw).decode()) == argv

    def test_bad_payload_denied(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            broker.decode_argv("not base64!")
        assert _exit_code(excinfo) == 2


@pytest.fixture
def stage(tmp_path, monkeypatch, conf) -> Path:
    """A valid secrets stage dir, with HOME away from the operator's real config."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    conf["SECRETS_STAGE_DIR"] = str(tmp_path / "stage")
    conf["VENV_CACHE_ROOT"] = ""
    return tmp_path / "stage"


@pytest.fixture
def repo(tmp_path) -> Path:
    return _make_store(tmp_path / "repo")


@pytest.fixture
def docker_log(tmp_path, monkeypatch) -> Path:
    """Fake docker for `graphql start`: the argv of its `docker run` lands in the returned file."""
    log = tmp_path / "docker.log"
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    monkeypatch.setattr(broker, "sweep_gql_orphans", lambda: None)
    monkeypatch.setattr(broker, "do_graphql_status", lambda scope, tip: None)
    monkeypatch.setattr(broker, "_probe", lambda url, timeout: True)
    monkeypatch.setattr(broker, "_venv_cache_args", lambda repo, image: [])
    broker.CONF["GRAPHQL_BIND"] = "127.0.0.1"
    _fakebin(tmp_path, monkeypatch, docker="""
case "$1" in
  run) printf '%s\\n' "$@" > "$FAKE_DOCKER_LOG" ;;
  port) echo 127.0.0.1:4321 ;;
  inspect) [ "$2" != "--format" ] && [ "$3" != "--format" ] && exit 1; echo true ;;
esac
""")
    return log


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _refused(callable_, *args, **kwargs) -> str:
    with pytest.raises(broker.SecretsRefused) as excinfo:
        callable_(*args, **kwargs)
    return str(excinfo.value)


class TestSecretsResolver:
    @pytest.fixture(autouse=True)
    def _stage(self, stage):
        return stage

    def _resolve(self, repo: Path, rel: str = ".env", **kwargs) -> "broker.Secrets":
        return broker.resolve_secrets(str(repo), f"{repo}/{rel}", **kwargs)

    def test_copy_is_private_and_equals_the_bytes(self, repo, stage):
        (repo / ".env").write_bytes(b"A=1\nB='two words'\n")
        secrets = self._resolve(repo)
        assert secrets.source == f"{repo}/.env"
        assert secrets.project_subdir == "."
        assert Path(secrets.staged).parent == stage
        assert Path(secrets.staged).read_bytes() == b"A=1\nB='two words'\n"
        assert _mode(Path(secrets.staged)) == 0o600
        assert _mode(stage) == 0o700
        assert secrets.values() == {"A": "1", "B": "two words"}

    def test_subdir_env_gives_project_subdir(self, repo):
        (repo / "api").mkdir()
        (repo / "api" / ".env").write_text("A=1\n")
        secrets = self._resolve(repo, "api/.env")
        assert secrets.project_subdir == "api"
        assert Path(secrets.staged).read_text() == "A=1\n"

    def test_values_are_those_read_at_resolve_time(self, repo):
        (repo / ".env").write_text("A=before\n")
        secrets = self._resolve(repo)
        (repo / ".env").write_text("A=after\n")
        assert secrets.values() == {"A": "before"}

    @pytest.mark.parametrize("layout,part", [("last", ".env"), ("middle", "api")], ids=["last-part", "middle-dir"])
    def test_symlink_below_the_repo_refused(self, repo, tmp_path, layout, part):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / ".env").write_text("STOLEN=1\n")
        if layout == "last":
            (repo / ".env").symlink_to(outside / ".env")
            rel = ".env"
        else:
            (repo / "api").symlink_to(outside)
            rel = "api/.env"
        reason = _refused(self._resolve, repo, rel)
        assert "symlink" in reason
        assert part in reason

    def test_symlink_to_a_file_inside_the_repo_refused(self, repo):
        (repo / "real.env").write_text("A=1\n")
        (repo / ".env").symlink_to(repo / "real.env")
        assert "symlink" in _refused(self._resolve, repo)

    def test_fifo_refused_without_blocking(self, repo):
        os.mkfifo(repo / ".env")
        result = []
        thread = threading.Thread(target=lambda: result.append(_refused(self._resolve, repo)), daemon=True)
        thread.start()
        thread.join(5)
        assert not thread.is_alive(), "opening a FIFO blocked the resolver"
        assert "not a regular file" in result[0]

    def test_directory_refused(self, repo):
        (repo / ".env").mkdir()
        assert "not a regular file" in _refused(self._resolve, repo)

    def test_oversize_file_refused(self, repo):
        (repo / ".env").write_bytes(b"A=" + b"x" * (2 << 20))
        assert "larger than" in _refused(self._resolve, repo)

    @pytest.mark.parametrize("rel", ["../x/.env", "api/../../x/.env", "/abs/.env", "api//.env"],
                             ids=["dotdot", "nested-dotdot", "absolute", "empty-part"])
    def test_non_relative_path_refused(self, repo, rel):
        assert "relative" in _refused(broker.resolve_secrets, str(repo), f"{repo}/{rel}")

    def test_path_outside_the_repo_refused(self, repo, tmp_path):
        assert "outside the repo" in _refused(broker.resolve_secrets, str(repo), str(tmp_path / ".env"))

    def test_absent_gives_no_mount_and_deletes_a_stale_copy(self, repo, stage):
        (repo / ".env").write_text("A=1\n")
        stale = Path(self._resolve(repo).staged)
        (repo / ".env").unlink()
        secrets = self._resolve(repo)
        assert secrets.staged is None
        assert secrets.mount_args() == []
        assert not stale.exists()

    def test_absent_directory_below_the_repo_is_absent(self, repo):
        assert self._resolve(repo, "nope/.env").staged is None

    def test_copy_is_replaced_atomically(self, repo):
        (repo / ".env").write_text("A=old\n")
        first = self._resolve(repo).staged
        with open(first, "rb") as running:
            (repo / ".env").write_text("A=new\n")
            assert self._resolve(repo).staged == first
            assert running.read() == b"A=old\n"
        assert Path(first).read_bytes() == b"A=new\n"
        assert _mode(Path(first)) == 0o600
        assert [path.name for path in Path(first).parent.iterdir()] == [Path(first).name]

    def test_copy_of_another_file_has_another_name(self, repo):
        (repo / "api").mkdir()
        (repo / ".env").write_text("A=1\n")
        (repo / "api" / ".env").write_text("A=2\n")
        assert self._resolve(repo).staged != self._resolve(repo, "api/.env").staged

    def test_stage_false_reads_without_copying(self, repo, stage):
        (repo / ".env").write_text("A=1\n")
        secrets = self._resolve(repo, stage=False)
        assert secrets.staged is None
        assert secrets.values() == {"A": "1"}
        assert not stage.exists()

    def test_operator_path_follows_symlinks_above_but_not_the_basename(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        (real / "my.env").write_text("A=1\n")
        (tmp_path / "linkdir").symlink_to(real)
        (real / "evil.env").symlink_to(real / "my.env")
        assert broker.resolve_secrets("/unused", str(tmp_path / "linkdir" / "my.env"), operator_path=True).values() == {"A": "1"}
        assert "symlink" in _refused(broker.resolve_secrets, "/unused", str(real / "evil.env"), operator_path=True)

    def test_old_copies_are_swept(self, repo, stage):
        (repo / ".env").write_text("A=1\n")
        self._resolve(repo)
        old = stage / "0123456789abcdef.env"
        leftover = stage / ".stage-crash123"
        bystander = stage / "notes.txt"
        for path in (old, leftover, bystander):
            path.write_text("A=1\n")
            os.utime(path, (1, 1))
        fresh = Path(self._resolve(repo).staged)
        assert fresh.exists()
        assert not old.exists()
        assert not leftover.exists()
        assert bystander.exists()

    @pytest.mark.parametrize("break_stage", ["open-mode", "symlink", "regular-file", "inside-venv-cache", "inside-runs"],
                             ids=["open-mode", "symlink", "regular-file", "inside-venv-cache", "inside-runs"])
    def test_bad_stage_dir_refused(self, repo, stage, tmp_path, conf, break_stage):
        (repo / ".env").write_text("A=1\n")
        match break_stage:
            case "open-mode":
                stage.mkdir()
                stage.chmod(0o755)
            case "symlink":
                (tmp_path / "elsewhere").mkdir(mode=0o700)
                stage.symlink_to(tmp_path / "elsewhere")
            case "regular-file":
                stage.write_text("")
            case "inside-venv-cache":
                conf["VENV_CACHE_ROOT"] = str(tmp_path)
            case "inside-runs":
                conf["TESTS_RUNS_ROOT"] = str(tmp_path)
        assert "stage directory" in _refused(self._resolve, repo)

    def test_stage_dir_inside_claude_dir_refused(self, repo, tmp_path, conf):
        conf["SECRETS_STAGE_DIR"] = str(tmp_path / "home/.claude/stage")
        (repo / ".env").write_text("A=1\n")
        assert "stage directory" in _refused(self._resolve, repo)

    def test_stage_dir_inside_a_registry_repo_refused(self, repo, tmp_path, conf):
        registered = tmp_path / "registered"
        registered.mkdir()
        (tmp_path / "home/.config/cld").mkdir(parents=True)
        (tmp_path / "home/.config/cld/config.toml").write_text(f'[repos.x]\npath = "{registered}"\n')
        conf["SECRETS_STAGE_DIR"] = str(registered / "stage")
        (repo / ".env").write_text("A=1\n")
        assert "stage directory" in _refused(self._resolve, repo)

    @pytest.mark.parametrize("char", [",", '"'], ids=["comma", "quote"])
    def test_stage_dir_with_a_mount_separator_refused(self, repo, tmp_path, conf, char):
        conf["SECRETS_STAGE_DIR"] = str(tmp_path / f"a{char}b")
        (repo / ".env").write_text("A=1\n")
        assert "comma or a quote" in _refused(self._resolve, repo)

    def test_root_that_is_not_a_directory_is_refused(self, tmp_path):
        (tmp_path / "file").write_text("")
        assert "cannot read" in _refused(broker.read_repo_file, str(tmp_path / "file"), ".env")

    def test_read_error_is_refused(self, repo, monkeypatch):
        (repo / ".env").write_text("A=1\n")

        def fail(fd):
            raise OSError(errno.EIO, "Input/output error")

        monkeypatch.setattr(os, "fstat", fail)
        assert "cannot read .env: Input/output error" in _refused(broker.read_repo_file, str(repo), ".env")

    def test_stage_dir_inside_the_mailbox_root_refused(self, repo, tmp_path, conf):
        conf["CLD_MAILBOX_ROOT"] = str(tmp_path)
        (repo / ".env").write_text("A=1\n")
        assert "stage directory" in _refused(self._resolve, repo)

    def _exit3(self, repo, capsys, pyproject_dir: str) -> str:
        (repo / ".cld").mkdir(exist_ok=True)
        (repo / ".cld" / "config.toml").write_text(f'pyproject_dir = "{pyproject_dir}"\n')
        with pytest.raises(SystemExit) as excinfo:
            broker.resolve_secrets_env_file(str(repo))
        assert _exit_code(excinfo) == 3
        return capsys.readouterr().err

    @pytest.mark.parametrize("pyproject_dir", ["{repo}/api", "/etc", "../x", "api/../../x"],
                             ids=["absolute-inside-repo", "absolute-outside", "dotdot", "nested-dotdot"])
    def test_pyproject_dir_refused_naming_the_source(self, repo, capsys, pyproject_dir):
        pyproject_dir = pyproject_dir.format(repo=repo)
        (repo / "api").mkdir()
        (repo / "api" / ".env").write_text("A=1\n")
        err = self._exit3(repo, capsys, pyproject_dir)
        assert err.startswith(f"denied: secrets file {repo}/{pyproject_dir}/.env: ")

    def test_symlinked_pyproject_dir_folder_refused(self, repo, tmp_path, capsys):
        (tmp_path / "outside").mkdir()
        (tmp_path / "outside" / ".env").write_text("A=1\n")
        (repo / "api").symlink_to(tmp_path / "outside")
        err = self._exit3(repo, capsys, "api")
        assert f"denied: secrets file {repo}/api/.env: symlink or non-directory at api" in err

    def test_resolve_secrets_env_file_follows_pyproject_dir(self, repo):
        (repo / ".cld").mkdir()
        (repo / ".cld" / "config.toml").write_text('pyproject_dir = "api"\n')
        (repo / "api").mkdir()
        (repo / "api" / ".env").write_text("A=1\n")
        secrets = broker.resolve_secrets_env_file(str(repo))
        assert (secrets.source, secrets.project_subdir) == (f"{repo}/api/.env", "api")

    def test_denial_never_names_the_copy(self, repo, stage, capsys):
        (repo / ".env").symlink_to(repo / "real")
        with pytest.raises(SystemExit):
            broker.resolve_secrets_env_file(str(repo))
        assert str(stage) not in capsys.readouterr().err


class TestSecretMountArgs:
    @pytest.fixture(autouse=True)
    def _stage(self, stage):
        return stage

    def test_mount_is_the_copy_read_only(self):
        secrets = broker.Secrets("/r/.env", ".", "/stage/abc.env", b"")
        assert secrets.mount_args() == ["--mount", "type=bind,src=/stage/abc.env,dst=/secrets/.env,readonly"]

    def test_nothing_to_mount_when_absent(self):
        assert broker.Secrets("/r/.env", ".", None, None).mount_args() == []

    def _run_tests(self, repo: Path, monkeypatch, pytest_argv: list[str]) -> list[str]:
        """The `docker run` argv of run_tests, with the exec stubbed."""
        calls = []
        monkeypatch.setattr(broker, "_exec", calls.append)
        monkeypatch.setattr(broker, "_venv_cache_args", lambda repo, image: [])
        broker.run_tests(str(repo), "rev", broker.resolve_secrets_env_file(str(repo)), "", pytest_argv)
        return calls[0]

    def test_run_tests_mounts_only_the_copy(self, repo, stage, monkeypatch):
        (repo / ".env").write_text("A=1\n")
        argv = self._run_tests(repo, monkeypatch, ["-k", "x"])
        mount = next(spec for spec in _mount_specs(argv) if "dst=/secrets/.env" in spec)
        copy = mount.split(",")[1].removeprefix("src=")
        assert mount == f"type=bind,src={copy},dst=/secrets/.env,readonly"
        assert Path(copy).parent == stage
        assert _mode(Path(copy)) == 0o600
        assert not any(str(repo / ".env") in arg for arg in argv)

    def test_run_tests_without_env_mounts_nothing(self, repo, monkeypatch):
        argv = self._run_tests(repo, monkeypatch, [])
        assert "/secrets/.env" not in " ".join(argv)

    def test_run_tests_dispatch_refuses_a_symlinked_env(self, repo, tmp_path, monkeypatch, capsys):
        (tmp_path / "outside.env").write_text("A=1\n")
        (repo / ".env").symlink_to(tmp_path / "outside.env")
        monkeypatch.setattr(broker, "_exec", lambda argv: pytest.fail("docker run reached"))
        monkeypatch.setattr(broker.CliCaller, "revision", lambda self, repo: "rev")
        caller = broker.CliCaller("cld_cli", str(repo), "@")
        with pytest.raises(SystemExit) as excinfo:
            broker.action_run_tests(caller, [])
        assert _exit_code(excinfo) == 3
        assert f"denied: secrets file {repo}/.env: symlink at .env" in capsys.readouterr().err

    def test_graphql_start_mounts_only_the_copy(self, repo, stage, docker_log):
        (repo / ".cld").mkdir()
        (repo / ".cld" / "config.toml").write_text('graphql_command = "serve"\n')
        (repo / ".env").write_text("A=1\n")
        broker.do_graphql_start(broker.Scope("t.x", str(repo)), "rev", "")
        args = docker_log.read_text().splitlines()
        mount = next(spec for spec in _mount_specs(args) if "dst=/secrets/.env" in spec)
        copy = Path(mount.split(",")[1].removeprefix("src="))
        assert mount == f"type=bind,src={copy},dst=/secrets/.env,readonly"
        assert copy.parent == stage
        assert _mode(copy) == 0o600
        assert not any(str(repo / ".env") in arg for arg in args)

    def test_graphql_start_refuses_a_symlinked_env_before_docker_run(self, repo, tmp_path, docker_log, capsys):
        (repo / ".cld").mkdir()
        (repo / ".cld" / "config.toml").write_text('graphql_command = "serve"\n')
        (tmp_path / "outside.env").write_text("A=1\n")
        (repo / ".env").symlink_to(tmp_path / "outside.env")
        with pytest.raises(SystemExit) as excinfo:
            broker.do_graphql_start(broker.Scope("t.x", str(repo)), "rev", "")
        assert _exit_code(excinfo) == 3
        assert "denied: secrets file" in capsys.readouterr().err
        assert not docker_log.exists()


class TestAliasNoFollow:
    @pytest.fixture(autouse=True)
    def _stage(self, stage):
        return stage

    @pytest.fixture
    def scope(self, repo) -> "broker.Scope":
        return broker.Scope("a.t1.test", str(repo))

    @pytest.fixture
    def symlinked_env(self, repo, tmp_path) -> Path:
        (tmp_path / "outside.env").write_text("CLD_GRAPHQL_URL_DEV=http://stolen/graphql\n")
        (repo / ".env").symlink_to(tmp_path / "outside.env")
        return repo / ".env"

    def test_alias_query_reads_a_regular_env_without_staging_it(self, repo, scope, stage):
        (repo / ".env").write_text("CLD_GRAPHQL_URL_DEV=https://dev/graphql\nCLD_GRAPHQL_AUTH_DEV='Bearer abc'\n")
        assert broker.resolve_target(scope, "dev") == ("https://dev/graphql", "Bearer abc", "")
        assert not stage.exists()

    @pytest.mark.parametrize("target", ["dev", "local"], ids=["alias", "local"])
    def test_symlinked_env_denied_naming_the_source(self, tmp_path, scope, symlinked_env, monkeypatch, capsys, target):
        monkeypatch.setattr(broker, "resolve_graphql_config", lambda repo: broker.GraphqlConfig("serve", "8000", "/graphql"))
        _fakebin(tmp_path, monkeypatch, docker='[ "$1" = port ] && echo "172.17.0.1:32768"\n')
        with pytest.raises(SystemExit) as excinfo:
            broker.resolve_target(scope, target)
        assert _exit_code(excinfo) == 3
        assert f"denied: secrets file {symlinked_env}: symlink at .env" in capsys.readouterr().err

    def test_endpoints_denied_for_a_symlinked_env(self, scope, symlinked_env, capsys):
        with pytest.raises(SystemExit) as excinfo:
            broker.do_graphql_endpoints(scope)
        assert _exit_code(excinfo) == 3
        captured = capsys.readouterr()
        assert captured.out == ""
        assert f"denied: secrets file {symlinked_env}: symlink at .env" in captured.err

    def test_endpoints_lists_aliases_of_a_regular_env(self, repo, scope, capsys):
        (repo / ".env").write_text("CLD_GRAPHQL_URL_STAGING_EU=https://s/graphql\n")
        broker.do_graphql_endpoints(scope)
        assert capsys.readouterr().out == "staging-eu\n"

    def test_endpoints_denied_for_a_symlinked_pyproject_dir(self, repo, scope, tmp_path, capsys):
        (tmp_path / "outside").mkdir()
        (tmp_path / "outside" / ".env").write_text("CLD_GRAPHQL_URL_DEV=http://stolen/graphql\n")
        (repo / "api").symlink_to(tmp_path / "outside")
        (repo / ".cld").mkdir()
        (repo / ".cld" / "config.toml").write_text('pyproject_dir = "api"\n')
        with pytest.raises(SystemExit) as excinfo:
            broker.do_graphql_endpoints(scope)
        assert _exit_code(excinfo) == 3
        assert f"denied: secrets file {repo}/api/.env: " in capsys.readouterr().err

    def test_fifo_env_denied_without_blocking(self, repo, scope):
        os.mkfifo(repo / ".env")
        outcome = []

        def run() -> None:
            try:
                broker.resolve_target(scope, "dev")
            except SystemExit as e:
                outcome.append(e.code)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(5)
        assert outcome == [3]


class TestConfigNoFollow:
    @pytest.fixture(autouse=True)
    def _stage(self, stage):
        return stage

    def _denied(self, capsys, callable_, *args) -> str:
        with pytest.raises(SystemExit) as excinfo:
            callable_(*args)
        assert _exit_code(excinfo) == 3
        return capsys.readouterr().err

    def test_reads_a_quoted_key(self, repo):
        (repo / ".cld").mkdir()
        (repo / ".cld" / "config.toml").write_text('graphql_command = "serve"\n  graphql_port = "9000"\nplain = 1\n')
        assert broker.cld_conf_get(str(repo), "graphql_command") == "serve"
        assert broker.cld_conf_get(str(repo), "graphql_port") == "9000"
        assert broker.cld_conf_get(str(repo), "plain") == ""

    def test_absent_file_or_dir_gives_empty(self, repo):
        assert broker.cld_conf_get(str(repo), "graphql_command") == ""
        (repo / ".cld").mkdir()
        assert broker.cld_conf_get(str(repo), "graphql_command") == ""

    def test_graphql_config_reads_through_the_resolver(self, repo):
        (repo / ".cld").mkdir()
        (repo / ".cld" / "config.toml").write_text('graphql_command = "serve"\ngraphql_health_path = "/h"\n')
        assert broker.resolve_graphql_config(str(repo)) == broker.GraphqlConfig("serve", "8000", "/h")

    @pytest.mark.parametrize("layout", ["symlinked-dir", "symlinked-file", "fifo", "directory"])
    def test_refused_config_denied_naming_it(self, repo, tmp_path, capsys, layout):
        (tmp_path / "outside").mkdir()
        (tmp_path / "outside" / "config.toml").write_text('graphql_command = "evil"\npyproject_dir = "x"\n')
        match layout:
            case "symlinked-dir":
                (repo / ".cld").symlink_to(tmp_path / "outside")
            case "symlinked-file":
                (repo / ".cld").mkdir()
                (repo / ".cld" / "config.toml").symlink_to(tmp_path / "outside" / "config.toml")
            case "fifo":
                (repo / ".cld").mkdir()
                os.mkfifo(repo / ".cld" / "config.toml")
            case "directory":
                (repo / ".cld" / "config.toml").mkdir(parents=True)
        for call in (broker.cld_conf_get, broker.resolve_secrets_env_file):
            args = (str(repo), "graphql_command") if call is broker.cld_conf_get else (str(repo),)
            assert f"denied: config file {repo}/.cld/config.toml: " in self._denied(capsys, call, *args)
        assert "denied: config file" in self._denied(capsys, broker.resolve_graphql_config, str(repo))


# What each layout's mount set must be, written out here rather than derived from either
# builder (design section 3.1): path -> read-only.
_EXPECTED_ROWS = {
    "colocated": {".jj": False, ".jj/working_copy": True, ".jj/repo": False, ".jj/repo/config-id": True,
                  ".jj/repo/store": False, ".jj/repo/store/git_target": True, ".git": False,
                  ".git/config": True, ".git/hooks": True, ".git/commondir": True, ".jj/workspace-config-id": True},
    "internal": {".jj": False, ".jj/working_copy": True, ".jj/repo": False, ".jj/repo/config-id": True,
                 ".jj/repo/store": False, ".jj/repo/store/git_target": True, ".jj/repo/store/git": False,
                 ".jj/repo/store/git/config": True, ".jj/repo/store/git/hooks": True,
                 ".jj/repo/store/git/commondir": True, ".jj/workspace-config-id": True},
    "git-only": {".git": False, ".git/config": True, ".git/hooks": True, ".git/commondir": True},
}


def _mount_specs(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == "--mount"]


def _parse_mounts(specs: list[str], host_repo: Path, root: str) -> dict[tuple[str, str], bool]:
    """{(source relative to the repo, target relative to the root): read-only} of --mount specs."""
    mounts = {}
    for spec in specs:
        kind, src, dst, *flags = spec.split(",")
        assert kind == "type=bind" and src.startswith("src=") and dst.startswith("dst=")
        assert flags in ([], ["readonly"])
        mounts[(os.path.relpath(src.removeprefix("src="), host_repo),
                os.path.relpath(dst.removeprefix("dst="), root))] = bool(flags)
    return mounts


def _break_secondary_workspace(repo: Path) -> None:
    shutil.rmtree(repo / ".jj/repo")
    (repo / ".jj/repo").write_text("../../main/.jj/repo")


def _break_git_worktree(repo: Path) -> None:
    shutil.rmtree(repo / ".git")
    (repo / ".git").write_text("gitdir: /elsewhere")


def _break_git_target(repo: Path) -> None:
    (repo / ".jj/repo/store/git_target").write_text("/elsewhere/.git")


def _break_symlinked_source(repo: Path) -> None:
    (repo / ".git/config").unlink()
    (repo / ".git/config").symlink_to("/etc/hostname")


def _break_legacy_config_without_id(repo: Path) -> None:
    (repo / ".jj/workspace-config-id").unlink()
    (repo / ".jj/workspace-config.toml").write_text('[revset-aliases]\n"trunk()" = "@"\n')


def _break_no_store(repo: Path) -> None:
    shutil.rmtree(repo / ".jj")
    shutil.rmtree(repo / ".git")


def _git_dir(repo: Path) -> Path:
    return repo / ".git" if (repo / ".git" / "hooks").is_dir() else repo / ".jj/repo/store/git"


def _break_commondir_redirect(repo: Path) -> None:
    (_git_dir(repo) / "commondir").write_text("../elsewhere")


def _break_commondir_newline(repo: Path) -> None:
    (_git_dir(repo) / "commondir").write_text(".\n")


def _break_commondir_symlink(repo: Path) -> None:
    (_git_dir(repo) / "commondir").unlink()
    (_git_dir(repo) / "commondir").symlink_to(".")


def _break_commondir_directory(repo: Path) -> None:
    (_git_dir(repo) / "commondir").unlink()
    (_git_dir(repo) / "commondir").mkdir()


def _break_modules(repo: Path) -> None:
    (_git_dir(repo) / "modules").mkdir()


def _break_modules_file(repo: Path) -> None:
    (_git_dir(repo) / "modules").write_text("")


class TestStoreMountParity:
    """The broker cannot import cld, so its mount rows are a copy of cld/docker.py's: every
    layout must give the same sources, targets and read-only flags."""

    LAYOUTS = [("colocated", cld_docker.StoreLayout(jj=True, git_dir=".git")),
               ("internal", cld_docker.StoreLayout(jj=True, git_dir=".jj/repo/store/git")),
               ("git-only", cld_docker.StoreLayout(jj=False, git_dir=".git"))]

    @pytest.mark.parametrize("name,cld_layout", LAYOUTS, ids=[name for name, _ in LAYOUTS])
    @pytest.mark.parametrize("root", ["/repo", "/workspace/origin/api"])
    def test_mount_args_match_the_launchers(self, tmp_path, name, cld_layout, root):
        repo = _make_store(tmp_path / "repo", name)
        ours = _parse_mounts(_mount_specs(broker.store_mounts(str(repo), root)), repo, root)
        theirs = _parse_mounts(_mount_specs(cld_docker.store_mount_args(cld_layout, str(repo), root)), repo, root)
        assert ours == theirs
        assert len(ours) == len(_EXPECTED_ROWS[name])

    @pytest.mark.parametrize("name,cld_layout", LAYOUTS, ids=[name for name, _ in LAYOUTS])
    def test_rows_and_layout_match_the_launchers(self, tmp_path, name, cld_layout):
        layout = broker.store_layout(str(_make_store(tmp_path / "repo", name)))
        assert tuple(layout) == (cld_layout.jj, cld_layout.git_dir)
        assert sorted(broker.store_rows(layout)) == sorted(cld_docker._store_rows(cld_layout))

    def test_label_matches_the_launchers(self):
        assert broker._ORIGIN_MOUNT_LABEL == f"{cld_docker.ORIGIN_MOUNT_LABEL}={cld_docker.ORIGIN_MOUNT_VALUE}"

    def test_jj_ids_match_the_launchers(self):
        assert broker._JJ_IDS == tuple((rel, legacy) for rel, legacy, _flag in cld_docker._JJ_IDS)

    def test_git_dir_rules_match_the_launchers(self):
        assert broker._GIT_DIRS == cld_jj.GIT_DIRS
        assert broker._COMMONDIR_PLACEHOLDER == cld_jj.COMMONDIR_PLACEHOLDER
        assert broker._JJ_MIN_VERSION == cld_docker._JJ_MIN_VERSION

    GIT_DIR_DAMAGE = [
        ("colocated", _break_commondir_redirect), ("internal", _break_commondir_redirect),
        ("git-only", _break_commondir_redirect), ("colocated", _break_commondir_newline),
        ("colocated", _break_commondir_symlink), ("git-only", _break_commondir_directory),
        ("colocated", _break_modules), ("internal", _break_modules), ("git-only", _break_modules),
        ("colocated", _break_modules_file),
    ]
    GIT_DIR_IDS = ["commondir-redirect", "commondir-redirect-internal", "commondir-redirect-git-only",
                   "commondir-with-newline", "commondir-symlink", "commondir-directory", "modules",
                   "modules-internal", "modules-git-only", "modules-file"]

    @pytest.mark.parametrize(
        "layout,damage",
        [("colocated", _break_secondary_workspace), ("git-only", _break_git_worktree),
         ("colocated", _break_git_target), ("colocated", _break_symlinked_source),
         ("colocated", _break_legacy_config_without_id), ("colocated", _break_no_store), *GIT_DIR_DAMAGE],
        ids=["secondary-workspace", "git-worktree", "foreign-git-target", "symlinked-source",
             "legacy-config-without-id", "no-store", *GIT_DIR_IDS],
    )
    def test_refuses_what_the_launcher_refuses(self, tmp_path, monkeypatch, layout, damage):
        repo = _make_store(tmp_path / "repo", layout)
        damage(repo)
        _fakebin(tmp_path, monkeypatch, jj='echo "jj 0.45.1"\n')
        with pytest.raises(SystemExit):
            cld_docker.prepare_store_mounts(repo)
        with pytest.raises(broker.StoreRefused):
            broker.store_mounts(str(repo), "/repo")

    @pytest.mark.parametrize(
        "layout,damage",
        [case for case in GIT_DIR_DAMAGE if case[0] != "git-only"] + [("colocated", _break_legacy_config_without_id)],
        ids=[i for case, i in zip(GIT_DIR_DAMAGE, GIT_DIR_IDS) if case[0] != "git-only"] + ["legacy-config-without-id"],
    )
    def test_host_jj_guard_refuses_what_the_launchers_does(self, tmp_path, layout, damage):
        """The launcher's guard before any host jj call (cld/vcs/jj.py:check_jj_ids) and the broker's agree."""
        repo = _make_store(tmp_path / "repo", layout)
        damage(repo)
        assert cld_jj.check_jj_ids(repo) is not None
        with pytest.raises(broker.StoreRefused):
            broker.check_jj_ids(str(repo))

    @pytest.mark.parametrize("layout", ["colocated", "internal", "git-only"])
    def test_both_accept_the_prepared_store(self, tmp_path, monkeypatch, layout):
        repo = _make_store(tmp_path / "repo", layout)
        _fakebin(tmp_path, monkeypatch, jj='echo "jj 0.45.1"\n')
        cld_docker.prepare_store_mounts(repo)
        broker.store_mounts(str(repo), "/repo")
        assert cld_jj.check_jj_ids(repo) is None
        if layout != "git-only":
            broker.check_jj_ids(str(repo))

    def test_the_unused_git_dir_is_guarded_too(self, tmp_path):
        """A colocated repo's internal git dir is not mounted, but host jj could still use it."""
        repo = _make_store(tmp_path / "repo")
        (repo / ".jj/repo/store/git").mkdir()
        (repo / ".jj/repo/store/git/commondir").write_text("../elsewhere")
        assert cld_jj.check_jj_ids(repo) is not None
        with pytest.raises(broker.StoreRefused, match="commondir exists"):
            broker.check_jj_ids(str(repo))


class TestRunnerStoreMounts:
    """run-tests and graphql start mount the store rows at /repo and never the checkout."""

    @pytest.fixture(autouse=True)
    def _stage(self, stage):
        return stage

    @pytest.fixture
    def run_tests_argv(self, monkeypatch):
        def run(repo: Path) -> list[str]:
            calls = []
            monkeypatch.setattr(broker, "_exec", calls.append)
            monkeypatch.setattr(broker, "_venv_cache_args", lambda repo, image: [])
            broker.run_tests(str(repo), "rev", broker.Secrets(f"{repo}/.env", ".", None, None), "", [])
            return calls[0]
        return run

    @pytest.fixture
    def graphql_start_argv(self, docker_log):
        def start(repo: Path) -> list[str]:
            (repo / ".cld").mkdir(exist_ok=True)
            (repo / ".cld" / "config.toml").write_text('graphql_command = "serve"\n')
            broker.do_graphql_start(broker.Scope("t.x", str(repo)), "rev", "")
            return docker_log.read_text().splitlines()
        return start

    def _assert_store_only(self, argv: list[str], repo: Path, layout: str) -> None:
        specs = [spec for spec in _mount_specs(argv) if "dst=/repo" in spec]
        assert _parse_mounts(specs, repo, "/repo") == {(rel, rel): readonly
                                                       for rel, readonly in _EXPECTED_ROWS[layout].items()}
        assert "-v" not in argv
        assert not any(arg.startswith(f"{repo}:") for arg in argv)
        assert "org.cld.origin-mount=store-v1" in [argv[i + 1] for i, arg in enumerate(argv) if arg == "--label"]
        assert "--cap-drop=ALL" in argv
        assert "--security-opt=no-new-privileges" in argv

    @pytest.mark.parametrize("layout", list(_EXPECTED_ROWS))
    def test_run_tests_mounts_the_rows_per_layout(self, tmp_path, run_tests_argv, layout):
        repo = _make_store(tmp_path / "layout-repo", layout)
        self._assert_store_only(run_tests_argv(repo), repo, layout)

    @pytest.mark.parametrize("layout", list(_EXPECTED_ROWS))
    def test_graphql_start_mounts_the_rows_per_layout(self, tmp_path, graphql_start_argv, layout):
        repo = _make_store(tmp_path / "layout-repo", layout)
        self._assert_store_only(graphql_start_argv(repo), repo, layout)

    def test_the_secrets_copy_is_mounted_beside_the_rows(self, repo, monkeypatch):
        (repo / ".env").write_text("A=1\n")
        calls = []
        monkeypatch.setattr(broker, "_exec", calls.append)
        monkeypatch.setattr(broker, "_venv_cache_args", lambda repo, image: [])
        broker.run_tests(str(repo), "rev", broker.resolve_secrets_env_file(str(repo)), "", [])
        specs = _mount_specs(calls[0])
        assert len(specs) == 12
        assert sum("dst=/secrets/.env,readonly" in spec for spec in specs) == 1

    @pytest.mark.parametrize("missing", [".jj/repo/config-id", ".jj/workspace-config-id", ".git/hooks", ".git/commondir"],
                             ids=["repo-id", "workspace-id", "hooks", "commondir"])
    def test_unprepared_store_exits_3_without_running_docker(self, repo, run_tests_argv, graphql_start_argv,
                                                             docker_log, capsys, missing):
        target = repo / missing
        shutil.rmtree(target) if target.is_dir() else target.unlink()
        for start in (run_tests_argv, graphql_start_argv):
            with pytest.raises(SystemExit) as excinfo:
                start(repo)
            assert _exit_code(excinfo) == 3
            assert f"denied: {repo} store is not prepared -- start a ticket on it once" in capsys.readouterr().err
        assert not docker_log.exists()

    def test_symlinked_source_exits_3(self, repo, run_tests_argv, capsys):
        _break_symlinked_source(repo)
        with pytest.raises(SystemExit) as excinfo:
            run_tests_argv(repo)
        assert _exit_code(excinfo) == 3
        assert ".git/config is a symlink, expected a file -- cannot mount its store safely" in capsys.readouterr().err

    @pytest.mark.parametrize("label", [None, "", "0.37.0", "0.9.1", "garbage"],
                             ids=["no-image", "no-label", "0.37", "0.9", "unparsable"])
    def test_image_without_a_jj_of_0_38_exits_3_without_running_docker(
        self, repo, run_tests_argv, graphql_start_argv, docker_log, monkeypatch, capsys, label,
    ):
        monkeypatch.setattr(broker, "_label", lambda container, key: label)
        for start in (run_tests_argv, graphql_start_argv):
            with pytest.raises(SystemExit) as excinfo:
                start(repo)
            assert _exit_code(excinfo) == 3
            err = capsys.readouterr().err
            assert "org.cld.jj-version" in err and "rebuild it with its build.sh" in err
        assert not docker_log.exists()

    @pytest.mark.parametrize("label", ["0.38.0", "0.45.1", "0.100.0", "1.0.0"])
    def test_image_with_a_jj_of_0_38_or_newer_is_accepted(self, repo, run_tests_argv, monkeypatch, label):
        asked = []
        monkeypatch.setattr(broker, "_label", lambda container, key: asked.append((container, key)) or label)
        assert "--cap-drop=ALL" in run_tests_argv(repo)
        assert asked == [(broker._setting("RUNTESTS_IMAGE"), "org.cld.jj-version")]

    @pytest.mark.parametrize("char", [",", '"'], ids=["comma", "quote"])
    def test_repo_path_with_a_mount_separator_exits_3(self, tmp_path, run_tests_argv, capsys, char):
        repo = _make_store(tmp_path / f"a{char}b")
        with pytest.raises(SystemExit) as excinfo:
            run_tests_argv(repo)
        assert _exit_code(excinfo) == 3
        assert "comma or a quote" in capsys.readouterr().err

    def test_dispatch_mounts_no_checkout_file(self, repo, monkeypatch):
        (repo / ".env").write_text("A=1\n")
        (repo / "checkout-file.txt").write_text("x")
        calls = []
        monkeypatch.setattr(broker, "_exec", calls.append)
        monkeypatch.setattr(broker, "_venv_cache_args", lambda repo, image: [])
        monkeypatch.setattr(broker.CliCaller, "revision", lambda self, repo: "rev")
        broker.action_run_tests(broker.CliCaller("cld_cli", str(repo), "@"), ["-k", "x"])
        assert not any(str(repo / ".env") in arg or str(repo / "checkout-file.txt") in arg for arg in calls[0])
        assert calls[0][-2:] == ["-k", "x"]


def _drop_id(repo: Path, rel: str) -> None:
    (repo / rel).unlink()


def _symlink_id(repo: Path, rel: str) -> None:
    (repo / rel).unlink()
    (repo / rel).symlink_to("/etc/hostname")


def _legacy_next_to_missing_id(repo: Path, rel: str) -> None:
    (repo / rel).unlink()
    (repo / dict(broker._JJ_IDS)[rel]).write_text("")


class TestHostJjNeedsIds:
    """Host jj runs on a repo only with both jj ids present as regular files: without one,
    any jj call would migrate a planted legacy config into the host's jj config (design A10)."""

    @pytest.fixture
    def jj_log(self, tmp_path, monkeypatch) -> Path:
        """Fake jj and docker that record every call; the docker log only holds `docker run`."""
        monkeypatch.setenv("FAKE_JJ_LOG", str(tmp_path / "jj.log"))
        monkeypatch.setenv("FAKE_DOCKER_LOG", str(tmp_path / "docker.log"))
        _fakebin(tmp_path, monkeypatch, jj='printf "%s\\n" "$*" >> "$FAKE_JJ_LOG"\necho rev123\n', docker="""
case "$1" in
  run) echo run >> "$FAKE_DOCKER_LOG" ;;
  ps) echo cld_gql_cld_gone ;;
  inspect) [ "$#" -gt 2 ] || exit 1 ;;
esac
""")
        return tmp_path / "jj.log"

    BROKEN = pytest.mark.parametrize(
        "damage,rel,message",
        [(_drop_id, ".jj/repo/config-id", "store is not prepared -- start a ticket on it once"),
         (_drop_id, ".jj/workspace-config-id", "store is not prepared -- start a ticket on it once"),
         (_symlink_id, ".jj/repo/config-id", ".jj/repo/config-id is a symlink, expected a file"),
         (_symlink_id, ".jj/workspace-config-id", ".jj/workspace-config-id is a symlink, expected a file"),
         (_legacy_next_to_missing_id, ".jj/repo/config-id", "unmigrated jj config .jj/repo/config.toml"),
         (_legacy_next_to_missing_id, ".jj/workspace-config-id", "unmigrated jj config .jj/workspace-config.toml")],
        ids=["repo-id-missing", "workspace-id-missing", "repo-id-symlink", "workspace-id-symlink",
             "repo-legacy-config", "workspace-legacy-config"],
    )

    @BROKEN
    @pytest.mark.parametrize("call", ["label-revision", "cli-revision", "run-tests", "graphql-start"])
    def test_denied_with_no_jj_run(self, repo, jj_log, tmp_path, capsys, damage, rel, message, call):
        damage(repo, rel)
        label_caller = broker.LabelCaller("a.t1.x", "task-agent", str(repo))
        cli_caller = broker.CliCaller("cld_cli", str(repo), "@")
        action = {
            "label-revision": lambda: label_caller.revision(str(repo)),
            "cli-revision": lambda: cli_caller.revision(str(repo)),
            "run-tests": lambda: broker.action_run_tests(cli_caller, []),
            "graphql-start": lambda: broker.action_graphql(cli_caller, ["start"]),
        }[call]
        with pytest.raises(SystemExit) as excinfo:
            action()
        assert _exit_code(excinfo) == 3
        assert message in capsys.readouterr().err
        assert not jj_log.exists()
        assert not (tmp_path / "docker.log").exists()

    @BROKEN
    def test_forget_warns_and_runs_no_jj(self, repo, jj_log, capsys, damage, rel, message):
        damage(repo, rel)
        broker._forget_workspace(str(repo), "gql-x")
        err = capsys.readouterr().err
        assert "workspace gql-x not forgotten: denied: " in err
        assert message in err
        assert not jj_log.exists()

    @BROKEN
    def test_stop_and_the_orphan_sweep_run_no_jj(self, repo, jj_log, monkeypatch, damage, rel, message):
        damage(repo, rel)
        monkeypatch.setattr(broker, "_label", lambda container, key: "cld_gone" if "session" in key else str(repo))
        broker.do_graphql_stop(broker.Scope("t.x", str(repo)))
        broker.sweep_gql_orphans()
        assert not jj_log.exists()

    def test_prepared_store_runs_jj_read_only_for_a_container_caller(self, repo, jj_log):
        assert broker.LabelCaller("a.t1.x", "task-agent", str(repo)).revision(str(repo)) == "rev123"
        assert jj_log.read_text().splitlines() == [
            f"-R {repo} --ignore-working-copy log --no-graph -n1 -r a.t1.x@ -T commit_id"]

    def test_prepared_store_snapshots_for_the_host_operator(self, repo, jj_log):
        assert broker.CliCaller("cld_cli", str(repo), "main").revision(str(repo)) == "rev123"
        assert jj_log.read_text().splitlines() == [f"-R {repo} log --no-graph -n1 -r main -T commit_id"]

    def test_forget_runs_without_snapshot(self, repo, jj_log):
        broker._forget_workspace(str(repo), "gql-x")
        assert jj_log.read_text().splitlines() == [f"-R {repo} --ignore-working-copy workspace forget gql-x"]

    def test_git_only_repo_runs_no_jj(self, tmp_path, jj_log):
        repo = _make_store(tmp_path / "plain", "git-only")
        assert broker.LabelCaller("a.t1.x", "task-agent", str(repo)).revision(str(repo)) is None
        broker._forget_workspace(str(repo), "gql-x")
        assert not jj_log.exists()

    def test_every_host_jj_call_goes_through_the_guard(self):
        """Static: the script names the `jj` binary only in host_jj and in its version probe."""
        lines = BROKER_PY.read_text().splitlines()
        hits = [i for i, line in enumerate(lines) if '"jj"' in line and not line.lstrip().startswith("#")]
        assert [next(line for line in reversed(lines[:i]) if line.startswith("def ")).split("(")[0]
                for i in hits] == ["def _host_jj_version", "def host_jj"]
        start = next(i for i, line in enumerate(lines) if line.startswith("def host_jj("))
        body = "\n".join(lines[start:hits[1] + 1])
        assert body.index("    check_jj_ids(repo)") < body.index("    require_host_jj()") < body.index('_out("jj"')

    def test_the_operator_script_runs_no_jj_of_its_own(self):
        """cld-brokerctl.sh reaches the broker's guarded sweep instead of a bash copy with its own jj call."""
        script = (BROKER_PY.parent / "cld-brokerctl.sh").read_text()
        code = [line for line in script.splitlines() if not line.lstrip().startswith("#")]
        assert not any(re.search(r"\bjj\b", line) for line in code)
        assert "graphql-sweep() { run graphql-sweep; }" in code


class TestGitTicketRevision:
    """A git-only repo has no workspace tip: run-tests and graphql start get the session branch's
    commit (committed work only), through host git after the store checks of the runner mount."""

    SESSION = "t.x"

    @pytest.fixture
    def git_store(self, tmp_path) -> Path:
        """A real git repo: the session branch on the first commit, the checked-out branch one commit later."""
        repo = tmp_path / "git-repo"
        repo.mkdir()
        git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@localhost"]
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "first"], check=True)
        subprocess.run([*git, "branch", self.SESSION], check=True)
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "second"], check=True)
        (repo / ".git" / "hooks").mkdir(exist_ok=True)
        (repo / ".git" / "commondir").write_text(".")
        return repo

    def _rev(self, repo: Path, ref: str) -> str:
        return subprocess.run(["git", "-C", str(repo), "rev-parse", ref], capture_output=True, text=True,
                              check=True).stdout.strip()

    def test_the_label_caller_resolves_the_session_branch_not_head(self, git_store):
        tip = broker.LabelCaller(self.SESSION, "ticket", "").revision(str(git_store))
        assert tip == self._rev(git_store, self.SESSION)
        assert tip != self._rev(git_store, "HEAD")

    def test_a_missing_session_branch_is_unresolved(self, git_store):
        assert broker.LabelCaller("t.other", "ticket", "").revision(str(git_store)) is None

    def test_a_refused_store_is_a_denial_and_runs_no_git(self, git_store, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("FAKE_GIT_LOG", str(tmp_path / "git.log"))
        _fakebin(tmp_path, monkeypatch, git='echo "$*" >> "$FAKE_GIT_LOG"\n')
        _break_commondir_redirect(git_store)
        with pytest.raises(SystemExit) as excinfo:
            broker.LabelCaller(self.SESSION, "ticket", "").revision(str(git_store))
        assert _exit_code(excinfo) == 3
        assert "commondir exists" in capsys.readouterr().err
        assert not (tmp_path / "git.log").exists()

    def test_a_jj_repo_never_asks_git(self, repo, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_GIT_LOG", str(tmp_path / "git.log"))
        _fakebin(tmp_path, monkeypatch, git='echo "$*" >> "$FAKE_GIT_LOG"\n', jj="echo rev123\n")
        assert broker.LabelCaller("a.t1.x", "task-agent", str(repo)).revision(str(repo)) == "rev123"
        assert not (tmp_path / "git.log").exists()

    def test_run_tests_passes_the_branch_tip_as_the_revision(self, git_store, stage, monkeypatch):
        launched = []
        monkeypatch.setattr(broker, "_exec", launched.append)
        broker.action_run_tests(broker.LabelCaller(self.SESSION, "task-agent", str(git_store)), [])
        argv = launched[0]
        assert f"REVISION={self._rev(git_store, self.SESSION)}" in argv
        assert f"type=bind,src={git_store}/.git,dst=/repo/.git" in " ".join(argv)

    def test_graphql_start_resolves_the_branch_tip(self, git_store):
        caller = broker.LabelCaller(self.SESSION, "ticket", "")
        assert broker.resolve_revision(caller, str(git_store)) == self._rev(git_store, self.SESSION)

    def test_host_git_is_named_only_in_host_git(self):
        """Static: the script runs the `git` binary only through host_git, after the store checks."""
        lines = BROKER_PY.read_text().splitlines()
        hits = [i for i, line in enumerate(lines) if '"git"' in line and not line.lstrip().startswith("#")]
        assert [next(line for line in reversed(lines[:i]) if line.startswith("def ")).split("(")[0]
                for i in hits] == ["def host_git"]
        start = next(i for i, line in enumerate(lines) if line.startswith("def host_git("))
        body = "\n".join(lines[start:hits[0] + 1])
        assert body.index("store_layout(repo)") < body.index('_out("git"')


class TestVenvCachePerRepo:
    """Code under test writes the venv cache, and a venv or a wheel in it is run or installed later
    with the `.env` of whichever repo restores it. A repo therefore gets a folder of its own below
    the cache root, and that folder is all its runs mount."""

    @pytest.fixture(autouse=True)
    def _docker(self, monkeypatch):
        monkeypatch.setattr(broker, "_out_strict", lambda *argv, **kwargs: "sha256:img")

    def _mounted(self, cache: Path, repo: Path) -> str:
        broker.CONF["VENV_CACHE_ROOT"] = str(cache)
        args = broker._venv_cache_args(str(repo), "img")
        return args[args.index("-v") + 1].removesuffix(":/cache")

    def test_two_repos_never_share_a_folder(self, tmp_path):
        (tmp_path / "x" / "api").mkdir(parents=True)
        (tmp_path / "y" / "api").mkdir(parents=True)
        first, second = (self._mounted(tmp_path / "cache", tmp_path / d / "api") for d in "xy")
        assert first != second
        assert not Path(first).is_relative_to(second) and not Path(second).is_relative_to(first)

    def test_the_root_itself_is_never_mounted(self, tmp_path):
        mounted = Path(self._mounted(tmp_path / "cache", tmp_path))
        assert mounted.parent == tmp_path / "cache" / "repos"
        assert not (tmp_path / "cache" / "venvs").exists()

    def test_a_repo_keeps_its_folder_across_runs_and_symlinks(self, tmp_path):
        (tmp_path / "api").mkdir()
        (tmp_path / "link").symlink_to(tmp_path / "api")
        assert self._mounted(tmp_path / "cache", tmp_path / "api") == self._mounted(tmp_path / "cache", tmp_path / "link")

    def test_each_folder_holds_the_dirs_venv_cache_sh_expects(self, tmp_path):
        mounted = Path(self._mounted(tmp_path / "cache", tmp_path))
        assert sorted(entry.name for entry in mounted.iterdir()) == ["locks", "poetry", "venvs"]

    @pytest.mark.parametrize("name", ["we ird:na,me", "..x", "a'b\"c"], ids=["separators", "dots", "quotes"])
    def test_a_hostile_repo_name_stays_one_path_component(self, tmp_path, name):
        (tmp_path / name).mkdir()
        mounted = Path(self._mounted(tmp_path / "cache", tmp_path / name))
        assert mounted.parent == tmp_path / "cache" / "repos"
        assert re.fullmatch(r"[A-Za-z0-9_.-]*-[0-9a-f]{16}", mounted.name)

    def test_the_workers_still_get_the_image_id(self, tmp_path):
        broker.CONF["VENV_CACHE_ROOT"] = str(tmp_path / "cache")
        assert broker._venv_cache_args(str(tmp_path), "img")[2:] == ["-e", "VENV_CACHE_IMAGE_ID=sha256:img"]

    def test_disabled_cache_mounts_nothing(self, tmp_path):
        broker.CONF["VENV_CACHE_ROOT"] = ""
        assert broker._venv_cache_args(str(tmp_path), "img") == []


def _write_cld_config(repo: Path, body: str) -> None:
    (repo / ".cld").mkdir(exist_ok=True)
    (repo / ".cld" / "config.toml").write_text(body)


class TestGraphqlConfigChecks:
    """graphql_port and graphql_health_path are written by the agent and end up in a URL, `-p`,
    `docker port` and an env var. A health path that is not a plain path could move the URL to
    another host, where `local` would send the alias credentials."""

    @pytest.mark.parametrize("path", ["graphql", "//evil.example/graphql", "http://evil.example/x", "@evil.example",
                                      ":80/x", "evil.example/x", "?x=1"],
                             ids=["no-slash", "authority", "absolute-url", "userinfo", "port", "host", "query-only"])
    def test_health_path_must_be_a_path_from_the_root(self, repo, capsys, path):
        _write_cld_config(repo, f'graphql_health_path = "{path}"\n')
        with pytest.raises(SystemExit) as excinfo:
            broker.resolve_graphql_config(str(repo))
        assert _exit_code(excinfo) == 3
        assert "graphql_health_path" in capsys.readouterr().err

    @pytest.mark.parametrize("path", ["/graphql", "/", "/a/b?x=1", "/health/"])
    def test_plain_health_paths_are_accepted(self, repo, path):
        _write_cld_config(repo, f'graphql_health_path = "{path}"\n')
        assert broker.resolve_graphql_config(str(repo)).health_path == path

    @pytest.mark.parametrize("port", ["0", "65536", "-1", "80;x", "8000 -p 22:22", "8 0", "\u0668\u0660", "0x50", "1e3"],
                             ids=["zero", "too-big", "negative", "semicolon", "extra-flag", "space", "arabic-digits",
                                  "hex", "float"])
    def test_port_must_be_a_number_in_range(self, repo, capsys, port):
        _write_cld_config(repo, f'graphql_port = "{port}"\n')
        with pytest.raises(SystemExit) as excinfo:
            broker.resolve_graphql_config(str(repo))
        assert _exit_code(excinfo) == 3
        assert "graphql_port" in capsys.readouterr().err

    @pytest.mark.parametrize("port", ["1", "8000", "65535"])
    def test_ports_in_range_are_accepted(self, repo, port):
        _write_cld_config(repo, f'graphql_port = "{port}"\n')
        assert broker.resolve_graphql_config(str(repo)).port == port

    def test_a_bad_health_path_stops_local_before_any_request(self, repo, monkeypatch, capsys):
        _write_cld_config(repo, 'graphql_health_path = "//evil.example/x"\n')
        monkeypatch.setattr(broker, "http_post", lambda *args: pytest.fail("a request was sent"))
        with pytest.raises(SystemExit) as excinfo:
            broker.resolve_target(broker.Scope("a.t1.test", str(repo)), "local")
        assert _exit_code(excinfo) == 3

    @pytest.mark.parametrize(
        "path,url",
        [("/graphql", "http://127.0.0.1:9/graphql"), ("/h?a=1#frag", "http://127.0.0.1:9/h?a=1"),
         ("//evil.example/x", "http://127.0.0.1:9/x"), ("@evil.example/x", "http://127.0.0.1:9/@evil.example/x"),
         ("http://evil.example/x", "http://127.0.0.1:9/x")],
        ids=["plain", "query-kept-fragment-dropped", "authority-ignored", "userinfo-is-path", "url-ignored"],
    )
    def test_the_url_keeps_its_host_whatever_the_path_is(self, path, url):
        assert broker.local_url("127.0.0.1:9", path) == url


class TestGraphqlFailureMessages:
    """A failed request never prints the URL an alias resolved to: it can carry userinfo from the .env."""

    ALIAS_URL = "https://deploy:hunter2@api.example/graphql"

    def _query(self, monkeypatch, target: str, url: str) -> None:
        monkeypatch.setattr(broker, "resolve_target", lambda scope, t: (url, "", ""))
        broker.do_graphql_query(broker.Scope("a.t1.test", "/repo"), target, "{ __typename }")

    @pytest.mark.parametrize("target,shown", [("prod", "'prod'"), ("local", "'local'"),
                                              ("https://u:pw@dev.example:8443/g?x=1", "'https://dev.example'")],
                             ids=["alias", "local", "raw-url"])
    def test_connection_failure_names_the_target_without_userinfo(self, monkeypatch, capsys, target, shown):
        monkeypatch.setattr(broker, "http_post", lambda *args: None)
        with pytest.raises(SystemExit) as excinfo:
            self._query(monkeypatch, target, self.ALIAS_URL)
        assert _exit_code(excinfo) == 3
        err = capsys.readouterr().err
        assert f"graphql request to {shown} failed (connection error)" in err
        assert not any(secret in err for secret in ("hunter2", "deploy", "api.example", "pw@"))

    @pytest.mark.parametrize("url", ["https://deploy:hunter2@127.0.0.1:1/graphql",
                                     "https://deploy:hunter2@127.0.0.1:notaport/x", "gopher://deploy:hunter2@127.0.0.1/x"],
                             ids=["refused", "bad-port", "bad-scheme"])
    def test_transport_error_text_never_quotes_the_url(self, capsys, url):
        assert broker.http_post(url, b"{}", {}, 2) is None
        err = capsys.readouterr().err
        assert err.startswith("[cld-broker] graphql request failed: ")
        assert not any(secret in err for secret in ("hunter2", "deploy", "notaport", "gopher"))


class TestTaskAgentArgv:
    """`--` ends option parsing on the host, so a token after it would be a bare positional that
    skips the @ref check; the broker's own --parent has to come before the caller's tokens."""

    @pytest.fixture(autouse=True)
    def fakebin(self, tmp_path, monkeypatch):
        (tmp_path / "repo").mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(broker, "stage_agent_socket", lambda: None)
        monkeypatch.setattr(broker, "_exec", lambda argv: pytest.fail(f"exec {argv}"))
        return tmp_path

    def _denied(self, capsys, argv: list[str]) -> str:
        with pytest.raises(SystemExit) as excinfo:
            broker.action_task_agent(broker.CliCaller("cld_cli", "/nonexistent", "@"), argv)
        assert _exit_code(excinfo) == 2
        return capsys.readouterr().err

    @pytest.mark.parametrize(
        "argv",
        [["start", "@task", "--", "/etc/passwd"], ["start", "--", "/etc/passwd"], ["start", "-p", "--"],
         ["status", "--"], ["logs", "slug", "--"], ["shutdown", "--", "--all"], ["shutdown", "slug", "--", "--force"]],
        ids=["start-after-ref", "start-first", "start-as-option-value", "status", "logs", "shutdown-all",
             "shutdown-force-behind"],
    )
    def test_double_dash_denied_for_every_op(self, capsys, argv):
        assert "'--' is not accepted" in self._denied(capsys, argv)

    @pytest.mark.parametrize(
        "argv,forwarded",
        [(["start", "@task", "-n", "s"], ["start", "--parent", "cld_cli", "@task", "-n", "s"]),
         (["status", "slug"], ["status", "--parent", "cld_cli", "slug"]),
         (["shutdown", "--all"], ["shutdown", "--parent", "cld_cli", "--all"]),
         (["logs", "slug"], ["logs", "slug"])],
        ids=["start", "status", "shutdown", "logs-has-no-parent"],
    )
    def test_broker_parent_precedes_the_callers_tokens(self, tmp_path, monkeypatch, argv, forwarded):
        calls = []
        monkeypatch.setattr(broker, "_exec", calls.append)
        broker.action_task_agent(broker.CliCaller("cld_cli", str(tmp_path / "repo"), "@"), argv)
        assert calls == [["cld", "task-agent", *forwarded]]


class TestHostJjVersion:
    """Host jj gets the launcher's 0.38 minimum (cld/docker.py:_check_jj_version) before its first call:
    the broker PATH comes from broker.conf, and an older jj also reads the agent-writable repo configs."""

    @pytest.fixture(autouse=True)
    def jj(self, tmp_path, monkeypatch):
        """A fake jj that logs every call and reports FAKE_JJ_VERSION, with the real, cached check in place."""
        monkeypatch.setenv("FAKE_JJ_LOG", str(tmp_path / "jj.log"))
        monkeypatch.setenv("FAKE_JJ_VERSION", "jj 0.45.1")
        _fakebin(tmp_path, monkeypatch, jj="""
printf '%s %s\\n' "$PWD" "$*" >> "$FAKE_JJ_LOG"
if [ "$1" = --version ]; then echo "$FAKE_JJ_VERSION"; exit 0; fi
echo rev123
""")
        _real_host_jj_version.cache_clear()
        monkeypatch.setattr(broker, "_host_jj_version", _real_host_jj_version)
        yield tmp_path / "jj.log"
        _real_host_jj_version.cache_clear()

    def _calls(self, log: Path) -> list[str]:
        return log.read_text().splitlines() if log.exists() else []

    @pytest.mark.parametrize("version", ["jj 0.37.2", "jj 0.9.0", "jj 0.1.0-dev", "garbage", ""],
                             ids=["0.37", "0.9", "0.1", "unparsable", "silent"])
    def test_an_old_or_unreadable_jj_refuses_every_host_call(self, repo, jj, monkeypatch, version):
        monkeypatch.setenv("FAKE_JJ_VERSION", version)
        for snapshot in (False, True):
            with pytest.raises(broker.StoreRefused, match="older than 0.38"):
                broker.host_jj(str(repo), "log", snapshot=snapshot)
        assert [call.partition(" ")[2] for call in self._calls(jj)] == ["--version"]

    @pytest.mark.parametrize("version", ["jj 0.38.0", "jj 0.45.1-abc123", "jj 0.100.0", "jj 1.0.0"])
    def test_a_new_enough_jj_runs(self, repo, jj, monkeypatch, version):
        monkeypatch.setenv("FAKE_JJ_VERSION", version)
        assert broker.host_jj(str(repo), "log") == "rev123"

    def test_jj_missing_refuses(self, repo, tmp_path, monkeypatch):
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        (tmp_path / "empty").mkdir()
        with pytest.raises(broker.StoreRefused, match="'not found' is older than"):
            broker.host_jj(str(repo), "log")

    def test_asked_once_per_process_and_away_from_any_repo(self, repo, jj):
        for _ in range(3):
            broker.host_jj(str(repo), "log")
        calls = self._calls(jj)
        assert [call.partition(" ")[2] for call in calls].count("--version") == 1
        assert calls[0] == "/ --version"
        assert len(calls) == 4

    def test_a_repo_without_jj_asks_nothing(self, tmp_path, jj):
        assert broker.host_jj(str(_make_store(tmp_path / "plain", "git-only")), "log") is None
        assert self._calls(jj) == []

    def test_the_other_guards_come_first(self, repo, jj):
        (repo / ".jj/repo/config-id").unlink()
        with pytest.raises(broker.StoreRefused, match="not prepared"):
            broker.host_jj(str(repo), "log")
        assert self._calls(jj) == []

    def test_refusals_reach_the_callers_as_denials(self, repo, jj, monkeypatch, capsys):
        monkeypatch.setenv("FAKE_JJ_VERSION", "jj 0.30.0")
        with pytest.raises(SystemExit) as excinfo:
            broker.LabelCaller("a.t1.x", "task-agent", str(repo)).revision(str(repo))
        assert _exit_code(excinfo) == 3
        assert "denied: host jj 'jj 0.30.0' is older than 0.38" in capsys.readouterr().err
        broker._forget_workspace(str(repo), "gql-x")
        assert "workspace gql-x not forgotten: denied: host jj" in capsys.readouterr().err
        assert [call.partition(" ")[2] for call in self._calls(jj)] == ["--version"]


class TestHostJjStoreChecks:
    """check_jj_ids / host_jj refuse the git dir layouts the launcher refuses (TestStoreMountParity
    holds the two sides together), and a git_target that left the repo."""

    @pytest.fixture
    def jj_log(self, tmp_path, monkeypatch) -> Path:
        monkeypatch.setenv("FAKE_JJ_LOG", str(tmp_path / "jj.log"))
        _fakebin(tmp_path, monkeypatch, jj='printf "%s\\n" "$*" >> "$FAKE_JJ_LOG"\necho rev123\n')
        return tmp_path / "jj.log"

    @pytest.mark.parametrize("damage", [_break_commondir_redirect, _break_modules, _break_git_target],
                             ids=["commondir", "modules", "git-target"])
    def test_host_jj_runs_nothing_on_a_refused_store(self, repo, jj_log, damage):
        damage(repo)
        with pytest.raises(broker.StoreRefused):
            broker.host_jj(str(repo), "log")
        assert not jj_log.exists()

    def test_the_commondir_placeholder_is_accepted(self, repo, jj_log):
        assert (repo / ".git/commondir").read_text() == "."
        assert broker.host_jj(str(repo), "log") == "rev123"

    def test_runners_and_servers_are_refused_before_docker_run(self, repo, stage, docker_log):
        _break_commondir_redirect(repo)
        _write_cld_config(repo, 'graphql_command = "serve"\n')
        with pytest.raises(SystemExit) as excinfo:
            broker.do_graphql_start(broker.Scope("t.x", str(repo)), "rev", "")
        assert _exit_code(excinfo) == 3
        with pytest.raises(SystemExit) as excinfo:
            broker.run_tests(str(repo), "rev", broker.Secrets(f"{repo}/.env", ".", None, None), "", [])
        assert _exit_code(excinfo) == 3
        assert not docker_log.exists()

    def test_a_path_that_cannot_be_examined_is_a_refusal_not_a_traceback(self, repo):
        (repo / "plain-file").write_text("")
        with pytest.raises(broker.StoreRefused, match="cannot examine .*Not a directory"):
            broker.check_git_dir(str(repo / "plain-file"), ".git")


class TestGraphqlSweepRoute:
    """`cld-brokerctl.sh graphql-sweep` runs the broker's own sweep, so a repo whose store fails the
    host jj guard is not touched by it."""

    @pytest.fixture
    def run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_JJ_LOG", str(tmp_path / "jj.log"))
        monkeypatch.setenv("FAKE_DOCKER_LOG", str(tmp_path / "docker.log"))
        _fakebin(tmp_path, monkeypatch, jj=_FAKE_JJ_VERSION + 'printf "%s\\n" "$*" >> "$FAKE_JJ_LOG"\n', docker="""
case "$1" in
  ps) echo cld_gql_cld_gone ;;
  inspect)
    case "$*" in
      *org.cld.gql-session*) echo cld_gone ;;
      *org.cld.gql-repo*) echo "$FAKE_GQL_REPO" ;;
      *) exit 1 ;;
    esac ;;
  rm) echo "$*" >> "$FAKE_DOCKER_LOG" ;;
esac
""")

        def go(repo: Path) -> subprocess.CompletedProcess:
            env = dict(os.environ, CLD_BROKER_CONF="/nonexistent", FAKE_GQL_REPO=str(repo))
            env.pop("SSH_ORIGINAL_COMMAND", None)
            return subprocess.run([sys.executable, str(BROKER_PY), "graphql-sweep"], capture_output=True, text=True,
                                  env=env, cwd=tmp_path)
        return go

    def test_reaps_and_forgets_through_the_guarded_jj(self, tmp_path, repo, run):
        result = run(repo)
        assert result.returncode == 0, result.stderr
        assert "reaping orphaned graphql server cld_gql_cld_gone (session cld_gone gone)" in result.stdout
        assert (tmp_path / "docker.log").read_text() == "rm -f cld_gql_cld_gone\n"
        assert (tmp_path / "jj.log").read_text().splitlines() == [
            f"-R {repo} --ignore-working-copy workspace forget gql-cld_gone"]

    def test_a_refused_store_is_reaped_but_never_given_to_jj(self, tmp_path, repo, run):
        _break_commondir_redirect(repo)
        result = run(repo)
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "docker.log").exists()
        assert "workspace gql-cld_gone not forgotten: denied: " in result.stderr
        assert not (tmp_path / "jj.log").exists()

    def test_the_default_sweep_is_silent(self, monkeypatch, capsys):
        monkeypatch.setattr(broker, "_capture", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, b""))
        broker.sweep_gql_orphans()
        assert capsys.readouterr().out == ""


class TestGraphqlStartLeftover:
    """A stopped server left by an earlier start is cleaned up in the repo it was started for."""

    def test_the_workspace_is_forgotten_in_the_label_repo(self, tmp_path, monkeypatch):
        forgotten = []
        monkeypatch.setattr(broker, "sweep_gql_orphans", lambda: None)
        monkeypatch.setattr(broker, "resolve_graphql_context", lambda repo: (
            broker.Secrets("/none/.env", ".", None, None), broker.GraphqlConfig("serve", "8000", "/graphql")))
        monkeypatch.setattr(broker, "_forget_workspace", lambda repo, workspace: forgotten.append((repo, workspace)))
        monkeypatch.setattr(broker, "_label", lambda container, key: "/repos/a")
        monkeypatch.setattr(broker, "_rm_container", lambda name: None)
        monkeypatch.setattr(broker, "resolve_graphql_bind", lambda: "127.0.0.1")
        monkeypatch.setattr(broker, "_out_strict", lambda *argv, **kwargs: "false" if "{{.State.Running}}" in argv else "")
        _fakebin(tmp_path, monkeypatch, docker="exit 0\n")
        with pytest.raises(SystemExit):
            broker.do_graphql_start(broker.Scope("t.x", "/repos/b"), "rev", "")
        assert forgotten == [("/repos/a", "gql-t.x")]


# ---- run / chain actions (docs/design-broker-run-chain.md §5) ------------------

_JOB_DOCKER = r"""
cmd="$1"; shift
case "$cmd" in
  inspect)
    case "$*" in
      *org.cld.kind*)     printf 'ticket\n' ;;
      *org.cld.manifest*) printf '%s\n' "${FAKE_MANIFEST:-}" ;;
      *State.Running*)
        for last; do :; done
        [ -f "$FAKE_DOCKER_DIR/state-$last" ] || exit 1
        sleep "${FAKE_PROBE_SLEEP:-0}"
        cat "$FAKE_DOCKER_DIR/state-$last" ;;
      *range*)            printf '%s' "${FAKE_LABELS:-}" ;;
    esac ;;
  stop) printf '%s\n' "$*" >> "$FAKE_DOCKER_DIR/stops" ;;
esac
"""
_JOB_CLD = r"""
printf 'CWD %s\n' "$PWD"
printf '%s\n' "$@"
"""
TICKET = broker.LabelCaller("t.x", "ticket", "")
OTHER_TICKET = broker.LabelCaller("t.other", "ticket", "")
_HOUR = 3600


class _JobHost:
    """A faked host for the run / chain actions: docker (containers and their running state, the
    ticket's labels and manifest) and cld on PATH, a HOME for the launch records, one mounted repo."""

    def __init__(self, tmp_path: Path, monkeypatch, repo: Path):
        self.repo = repo
        self.docker = tmp_path / "docker-state"
        self.docker.mkdir()
        (tmp_path / "home").mkdir()
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("FAKE_DOCKER_DIR", str(self.docker))
        self.monkeypatch = monkeypatch
        _fakebin(tmp_path, monkeypatch, docker=_JOB_DOCKER, cld=_JOB_CLD)
        self.mount(repo)

    def mount(self, repo: Path, manifest: str = "") -> None:
        for caller in (TICKET, OTHER_TICKET):  # the module-level callers cache their labels
            caller.__dict__.pop("_mounts", None)
        self.monkeypatch.setenv("FAKE_LABELS", f"org.cld.kind=ticket\norg.cld.repo.lide={repo}\n")
        self.monkeypatch.setenv("FAKE_MANIFEST", manifest)

    def container(self, name: str, running: bool) -> None:
        (self.docker / f"state-{name}").write_text("true" if running else "false")

    def seed(self, name: str, kind: str = "run", age: int = 0, caller=TICKET, base: str = "baseid") -> "broker.Job":
        job = broker.Job(kind, name, str(self.repo), base, int(time.time()) - age)
        broker._job_dir(caller).mkdir(parents=True, exist_ok=True)
        broker._job_write(caller, job)
        return job

    def chain_state(self, name: str, **fields) -> None:
        state = {"status": "running", "pid": os.getpid(), "started_at": "2026-10-27T10:00:00Z", "finished_at": None,
                 "total_cost_usd": 0.0, "completed_steps": [], "failure_reason": "", **fields}
        path = self.repo / ".cld" / "chains" / f"chain.{name}" / "state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))

    def committed(self, files: dict[tuple[str, str], str]) -> None:
        """Stub `jj file show -r <revision> <path>` for the status probes."""
        def fake_host_jj(repo, *args, snapshot=False):
            return files.get((args[3], args[4])) if args[:3] == ("file", "show", "-r") else None
        self.monkeypatch.setattr(broker, "host_jj", fake_host_jj)


@pytest.fixture
def host(tmp_path, monkeypatch) -> _JobHost:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "repo" / ".jj").mkdir(parents=True)
    return _JobHost(tmp_path, monkeypatch, tmp_path / "repo")


@pytest.fixture
def execs(monkeypatch) -> list[list[str]]:
    """The argv the broker would exec; the call returns, so a test can look past it."""
    calls: list[list[str]] = []
    monkeypatch.setattr(broker, "_exec", calls.append)
    return calls


def _denied_with(action, caller, argv: list[str], capsys) -> tuple[int, str]:
    with pytest.raises(SystemExit) as excinfo:
        action(caller, argv)
    return _exit_code(excinfo), capsys.readouterr().err


class TestJobStartArgv:
    """The start argv is parsed into fields and the host command rebuilt from them: only what the
    broker checked crosses, with the option arity listed, in both spellings."""

    @pytest.fixture(autouse=True)
    def nothing_runs(self, monkeypatch):
        monkeypatch.setattr(broker, "_exec", lambda argv: pytest.fail(f"exec {argv}"))
        monkeypatch.setattr(broker, "resolve_job_base", lambda *args: pytest.fail("base checked"))

    @pytest.mark.parametrize("action,kind", [(broker.action_run, "run"), (broker.action_chain, "chain")])
    @pytest.mark.parametrize(
        "argv,message",
        [(["@a", "--", "/etc/passwd"], "'--' is not accepted"),
         (["@a", "-p", "--"], "'--' is not accepted"),
         (["@a", "--shared-anchor"], "--shared-anchor is not allowed"),
         (["@a", "--shared-anchor=1"], "--shared-anchor is not allowed"),
         (["@a", "--no-detach"], "--no-detach is not allowed"),
         (["@a", "--force"], "--force is not allowed"),
         (["@a", "--parent=cld_other"], "--parent is not allowed"),
         (["@a", "/etc/passwd"], "must be an @ref, not a path"),
         (["@a", "rel/path.md"], "must be an @ref, not a path"),
         (["@a", "-p", "@x", "/etc/passwd"], "must be an @ref, not a path"),
         (["@a", "--name=x", "/etc/passwd"], "must be an @ref, not a path"),
         (["@a", "@"], "must be an @ref, not a path"),
         (["@a", "-n", "a b"], "must match"),
         (["@a", "-n", "a/b"], "must match"),
         (["@a", "--name=a.b"], "must match"),
         (["@a", "-r"], "needs a value"),
         (["@a", "--model"], "needs a value"),
         (["@a", "-nfoo"], "option '-nfoo' is not accepted"),
         (["@a", "--other"], "option '--other' is not accepted"),
         (["@a", "-n", "x", "--name=y"], "given twice"),
         (["@a"], "-r <revision> is required"),
         (["@a", "-r", ""], "-r <revision> is required")],
        ids=["dash-dash", "dash-dash-as-value", "shared-anchor", "shared-anchor-eq", "no-detach", "force",
             "parent-eq", "abs-path", "rel-path", "path-after-option-value", "path-after-eq-option", "bare-at",
             "name-space", "name-slash", "name-dot-eq", "revision-no-value", "model-no-value",
             "attached-short-value", "unknown-option", "name-twice", "no-revision", "empty-revision"],
    )
    def test_denied(self, capsys, action, kind, argv, message):
        argv = argv if kind == "run" else ["@chain", *argv]
        code, err = _denied_with(action, TICKET, ["start", *argv], capsys)
        assert code == 2
        assert message in err

    @pytest.mark.parametrize("first", ["@a/b", "@../x", "@", "a", "-n", "@a b"], ids=["slash", "dotdot", "bare-at", "bare-name", "option", "space"])
    def test_chain_name_must_be_a_bare_at_name(self, capsys, first):
        argv = ["start", first, "-r", "x"] if first != "-n" else ["start", "-n", "x", "-r", "x"]
        code, err = _denied_with(broker.action_chain, TICKET, argv, capsys)
        assert code == 2
        assert "chain start needs @<name> first" in err

    def test_chain_without_a_name_denied(self, capsys):
        assert _denied_with(broker.action_chain, TICKET, ["start", "-r", "x"], capsys)[0] == 2

    @pytest.mark.parametrize(
        "argv",
        [["@a", "@b", "-n", "n1", "-m", "opus", "-p", "text", "-r", "R"],
         ["@a", "@b", "--name", "n1", "--model", "opus", "--prompt", "text", "--revision", "R"],
         ["@a", "@b", "--name=n1", "--model=opus", "--prompt=text", "--revision=R"],
         ["-r", "R", "@a", "-p", "text", "@b", "-m", "opus", "-n", "n1"]],
        ids=["short", "long-separate", "long-equals", "options-between-positionals"],
    )
    def test_every_spelling_parses_the_same(self, argv):
        assert broker.parse_job_start("run", argv) == broker.JobArgs(["@a", "@b"], "n1", "opus", "text", "R")

    def test_an_option_value_may_look_like_anything(self):
        parsed = broker.parse_job_start("run", ["-p", "fix cld/cli.py", "-m", "-odd", "-r", "@-"])
        assert (parsed.prompt, parsed.model, parsed.revision) == ("fix cld/cli.py", "-odd", "@-")

    @pytest.mark.parametrize("argv,message", [([], "needs an op"), (["--repo", "lide"], "needs an op"),
                                              (["wait", "x"], "bad run op 'wait'")])
    def test_op_is_required_and_listed(self, capsys, argv, message):
        code, err = _denied_with(broker.action_run, TICKET, argv, capsys)
        assert code == 2
        assert message in err


class TestJobCallerAndRepo:
    @pytest.mark.parametrize("action", [broker.action_run, broker.action_chain])
    @pytest.mark.parametrize("caller", [broker.LabelCaller("a.t1.x", "task-agent", "/r"),
                                        broker.LabelCaller("run.x", "run", "/r"),
                                        broker.CliCaller("cld_cli", "/r", "@")], ids=["task-agent", "run", "cli"])
    @pytest.mark.parametrize("op", ["start", "status", "cancel"])
    def test_only_a_ticket_may_call(self, capsys, action, caller, op):
        code, err = _denied_with(action, caller, [op, "@a", "-r", "x"], capsys)
        assert code == 3
        assert "only for ticket containers" in err

    def test_unmounted_repo_denied(self, host, capsys):
        code, err = _denied_with(broker.action_run, TICKET, ["--repo", "nope", "start", "@a", "-r", "x"], capsys)
        assert code == 3
        assert "not mounted" in err

    def test_git_repo_denied(self, host, tmp_path, capsys):
        git_repo = tmp_path / "gitrepo"
        (git_repo / ".git").mkdir(parents=True)
        host.mount(git_repo)
        code, err = _denied_with(broker.action_chain, TICKET, ["start", "@a", "-n", "c", "-r", "x"], capsys)
        assert code == 3
        assert "not jj-backed" in err

    def test_repo_selector_picks_the_mounted_repo(self, host, execs, monkeypatch):
        monkeypatch.setattr(broker, "resolve_job_base", lambda caller, repo, revision: "chg")
        broker.action_run(TICKET, ["--repo=lide", "start", "@a", "-n", "x", "-r", "R"])
        assert execs == [["cld", "run", "@a", "-n", "x", "-r", "chg"]]
        assert os.getcwd() == str(host.repo)


def _have_jj() -> bool:
    found = re.search(r"(\d+)\.(\d+)", subprocess.run(["jj", "--version"], capture_output=True, text=True).stdout) \
        if shutil.which("jj") else None
    return bool(found) and (int(found[1]), int(found[2])) >= (0, 38)


class _Tree(NamedTuple):
    """Change ids in a real jj repo. base A has two children, the sibling and the scratch commit S
    (bookmark t.x); inside is S's child, and the working copy sits on top of inside."""

    repo: Path
    base: str
    scratch: str
    inside: str
    sibling: str
    unrelated: str
    wc: str


@pytest.fixture
def tree(tmp_path, monkeypatch) -> _Tree:
    if not _have_jj():
        pytest.skip("needs jj 0.38+")
    config = tmp_path / "jj-config.toml"
    config.write_text('[user]\nname = "test"\nemail = "test@localhost"\n')
    monkeypatch.setenv("JJ_CONFIG", str(config))
    repo = tmp_path / "repo"

    def jj(*args: str) -> str:
        return subprocess.run(["jj", "-R", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()

    def change(revset: str) -> str:
        return jj("log", "--no-graph", "-r", revset, "-T", "change_id")

    subprocess.run(["jj", "git", "init", str(repo)], capture_output=True, check=True)
    for flag in ("--repo", "--workspace"):  # creates the config ids the broker insists on
        jj("config", "set", flag, "ui.color", "never")
    jj("commit", "-m", "base")
    base = change("@-")
    jj("new", base, "-m", "sibling")
    sibling = change("@")
    jj("new", "root()", "-m", "unrelated")
    unrelated = change("@")
    jj("new", base, "-m", "cld anchor: t.x mode=isolated")
    jj("bookmark", "create", "t.x", "-r", "@")
    scratch = change("@")
    jj("new", "-m", "inside")
    inside = change("@")
    jj("new")
    monkeypatch.chdir(tmp_path)
    return _Tree(repo, base, scratch, inside, sibling, unrelated, change("@"))


def _manifest(tree: _Tree, mode: str = "isolated") -> str:
    return json.dumps({"v": 1, "ticket": "x", "repos": [
        {"name": "lide", "path": str(tree.repo), "anchor_base": tree.base, "anchor_mode": mode, "rev_source": "arg"}]})


@pytest.fixture
def jj_host(tree, tmp_path, monkeypatch) -> _JobHost:
    host = _JobHost(tmp_path, monkeypatch, tree.repo)
    host.mount(tree.repo, _manifest(tree))
    return host


class TestJobBaseCheck:
    """Design §5.3 against a real jj repo: one commit, inside the caller's effective anchor tree,
    not a working copy; the host command gets the change id the broker checked."""

    def _start(self, argv_tail: list[str]) -> None:
        broker.action_run(TICKET, ["start", "@a", "-n", "x", "-r", *argv_tail])

    @pytest.mark.parametrize("pick", ["inside", "scratch"])
    def test_inside_the_isolated_tree_starts(self, tree, jj_host, execs, capsys, pick):
        self._start([getattr(tree, pick)])
        assert execs == [["cld", "run", "@a", "-n", "x", "-r", getattr(tree, pick)]]
        assert capsys.readouterr().out == "started: run.x\n"

    def test_a_revset_is_passed_on_as_its_change_id(self, tree, jj_host, execs):
        self._start(["@-"])  # the parent of the working copy
        assert execs == [["cld", "run", "@a", "-n", "x", "-r", tree.inside]]

    def test_a_commit_id_is_accepted_too(self, tree, jj_host, execs):
        commit = subprocess.run(["jj", "-R", str(tree.repo), "log", "--no-graph", "-r", tree.inside, "-T", "commit_id"],
                                capture_output=True, text=True, check=True).stdout
        self._start([commit])
        assert execs[0][-1] == tree.inside

    @pytest.mark.parametrize("pick", ["base", "sibling", "unrelated"])
    def test_outside_the_isolated_tree_denied(self, tree, jj_host, execs, capsys, pick):
        # The base itself is outside too: the isolated tree starts at the scratch commit above it.
        code, err = _denied_with(lambda caller, argv: self._start(argv), TICKET, [getattr(tree, pick)], capsys)
        assert code == 3
        assert "outside the editable tree" in err
        assert execs == []

    @pytest.mark.parametrize("pick,allowed", [("base", True), ("sibling", True), ("scratch", True), ("inside", True),
                                              ("unrelated", False)])
    def test_the_shared_tree_is_everything_below_the_base(self, tree, jj_host, execs, capsys, pick, allowed):
        jj_host.mount(tree.repo, _manifest(tree, "shared"))
        if allowed:
            self._start([getattr(tree, pick)])
            assert execs[0][-1] == getattr(tree, pick)
        else:
            assert _denied_with(lambda caller, argv: self._start(argv), TICKET, [getattr(tree, pick)], capsys)[0] == 3
            assert execs == []

    def test_a_working_copy_is_denied_even_inside_the_tree(self, tree, jj_host, execs, capsys):
        code, err = _denied_with(lambda caller, argv: self._start(argv), TICKET, [tree.wc], capsys)
        assert code == 3
        assert "working-copy commit" in err and "jj new" in err
        assert execs == []

    @pytest.mark.parametrize("revset", ["children(" + "{base}" + ")", "none()", "nosuchthing", "all()"],
                             ids=["several", "none", "unresolvable", "everything"])
    def test_not_exactly_one_commit_denied(self, tree, jj_host, execs, capsys, revset):
        code, err = _denied_with(lambda caller, argv: self._start(argv), TICKET,
                                 [revset.format(base=tree.base)], capsys)
        assert code == 3
        assert "exactly one commit" in err
        assert execs == []

    def test_a_bookmark_named_like_the_change_id_cannot_redirect_the_host(self, tree, jj_host, execs, capsys):
        subprocess.run(["jj", "-R", str(tree.repo), "bookmark", "create", tree.inside, "-r", tree.sibling],
                       capture_output=True, check=True)
        code, err = _denied_with(lambda caller, argv: self._start(argv), TICKET, [f"change_id({tree.inside})"], capsys)
        assert code == 3
        assert "shadowed by a bookmark" in err
        assert execs == []

    def test_the_scratch_commit_missing_denied(self, tree, jj_host, execs, capsys):
        subprocess.run(["jj", "-R", str(tree.repo), "bookmark", "delete", "t.x"], capture_output=True, check=True)
        code, err = _denied_with(lambda caller, argv: self._start(argv), TICKET, [tree.inside], capsys)
        assert code == 3
        assert "scratch commit" in err

    @pytest.mark.parametrize("manifest", ["", "not json", '{"repos": []}', '{"repos": [{"path": "/elsewhere"}]}'],
                             ids=["absent", "garbled", "no-repos", "other-repo"])
    def test_an_unreadable_manifest_denied(self, tree, jj_host, execs, capsys, manifest):
        jj_host.mount(tree.repo, manifest)
        code, err = _denied_with(lambda caller, argv: self._start(argv), TICKET, [tree.inside], capsys)
        assert code == 3
        assert "launch manifest" in err

    def test_the_effective_anchor_revset_matches_the_launcher(self):
        """cld/docker.py:_effective_anchor is the contract: the broker cannot import it, so it repeats it."""
        seen: list[list[str]] = []

        class Vcs:
            def run(self, args):
                seen.append(args)
                return subprocess.CompletedProcess(args, 0, "scratchid\n", "")

        occupant = {"name": "t.x", "session": "t.x", "mode": "isolated", "anchor_base": "baseid"}
        cld_docker._effective_anchor(Vcs(), occupant)
        assert seen[0][seen[0].index("-r") + 1] == broker.effective_anchor_revset("t.x")


class TestJobStartCapAndRecords:
    @pytest.fixture(autouse=True)
    def base_ok(self, monkeypatch):
        monkeypatch.setattr(broker, "resolve_job_base", lambda caller, repo, revision: "chg")

    def _run(self, *tail: str) -> None:
        broker.action_run(TICKET, ["start", "@a", "-r", "R", *tail])

    def _record_names(self, caller=TICKET) -> set[str]:
        return {path.name for path in broker._job_dir(caller).glob("*.json")}

    def test_the_record_and_its_folder_are_private(self, host, execs):
        self._run("-n", "x")
        folder = broker._job_dir(TICKET)
        assert _mode(folder) == 0o700
        assert folder == host.repo.parent / "home" / ".cld" / "broker-jobs" / "t.x"
        record = json.loads((folder / "run-run.x.json").read_text())
        assert record == {"kind": "run", "name": "run.x", "repo": str(host.repo), "base": "chg",
                          "started_at": record["started_at"]}
        assert abs(record["started_at"] - time.time()) < 60

    def test_run_gets_a_random_session_name_without_n(self, host, execs, capsys):
        self._run()
        started = capsys.readouterr().out.strip()
        assert re.fullmatch(r"started: run\.[0-9a-f]{6}", started)
        suffix = started.removeprefix("started: run.")
        assert execs == [["cld", "run", "@a", "-n", suffix, "-r", "chg"]]

    def test_run_host_command_with_every_option(self, host, execs):
        self._run("-n", "x", "-m", "opus", "-p", "do it")
        assert execs == [["cld", "run", "@a", "-n", "x", "-r", "chg", "-m", "opus", "-p", "do it"]]

    def test_chain_host_command_and_name(self, host, execs, capsys):
        broker.action_chain(TICKET, ["start", "@rdi", "@brief", "-n", "mine", "-r", "R", "-m", "opus", "-p", "t"])
        assert execs == [["cld", "chain", "run", "@rdi", "@brief", "-n", "mine", "-r", "chg", "-m", "opus", "-p", "t"]]
        assert capsys.readouterr().out == "started: mine\n"

    def test_chain_without_n_is_denied(self, host, execs, capsys):
        code, err = _denied_with(broker.action_chain, TICKET, ["start", "@rdi", "-r", "R"], capsys)
        assert code == 2
        assert "chain start needs -n" in err
        assert execs == []

    def test_the_host_command_runs_in_the_repo(self, host, execs):
        self._run("-n", "x")
        assert os.getcwd() == str(host.repo)

    def test_a_third_live_job_is_denied_naming_the_live_ones(self, host, execs, capsys):
        for name in ("run.a", "run.b"):
            host.seed(name)
            host.container(name, True)
        code, err = _denied_with(broker.action_run, TICKET, ["start", "@a", "-r", "R"], capsys)
        assert code == 3
        assert "2 live jobs (run.a, run.b)" in err
        assert execs == []

    def test_runs_and_chains_share_the_cap(self, host, execs, capsys):
        host.seed("run.a")
        host.container("run.a", True)
        host.seed("c1", kind="chain")
        host.chain_state("c1")
        assert _denied_with(broker.action_chain, TICKET, ["start", "@rdi", "-n", "c2", "-r", "R"], capsys)[0] == 3

    def test_a_finished_job_frees_its_slot(self, host, execs):
        host.seed("run.a")
        host.container("run.a", True)
        host.seed("run.b")
        host.container("run.b", False)
        self._run("-n", "c")
        assert len(execs) == 1

    def test_a_finished_chain_frees_its_slot(self, host, execs):
        host.seed("run.a")
        host.container("run.a", True)
        host.seed("c1", kind="chain")
        host.chain_state("c1", status="success")
        self._run("-n", "c")
        assert len(execs) == 1

    def test_a_chain_whose_runner_died_frees_its_slot(self, host, execs):
        dead = subprocess.Popen(["true"])
        dead.wait()
        host.seed("run.a")
        host.container("run.a", True)
        host.seed("c1", kind="chain")
        host.chain_state("c1", pid=dead.pid)
        self._run("-n", "c")
        assert len(execs) == 1

    def test_a_failed_launch_costs_no_slot_once_it_is_not_launching(self, host, execs):
        for name in ("run.a", "run.b"):
            host.seed(name, age=_HOUR)  # a record with no container ever
        self._run("-n", "c")
        assert len(execs) == 1

    def test_a_launching_job_counts_until_the_grace_ends(self, host, execs, capsys):
        host.seed("run.a")
        host.seed("run.b")  # no container yet, but just recorded
        assert _denied_with(broker.action_run, TICKET, ["start", "@a", "-r", "R"], capsys)[0] == 3

    def test_other_callers_jobs_do_not_count(self, host, execs):
        for name in ("run.a", "run.b"):
            host.seed(name, caller=OTHER_TICKET)
            host.container(name, True)
        self._run("-n", "c")
        assert len(execs) == 1

    @pytest.mark.parametrize("taken", ["record", "container"])
    def test_a_taken_run_name_denied(self, host, execs, capsys, taken):
        if taken == "record":
            host.seed("run.x")
        else:
            host.container("run.x", False)
        code, err = _denied_with(broker.action_run, TICKET, ["start", "@a", "-n", "x", "-r", "R"], capsys)
        assert code == 3
        assert "'run.x' already exists" in err

    def test_a_live_chain_name_denied(self, host, execs, capsys):
        host.chain_state("mine")
        code, err = _denied_with(broker.action_chain, TICKET, ["start", "@rdi", "-n", "mine", "-r", "R"], capsys)
        assert code == 3
        assert "'mine' already exists" in err

    def test_a_finished_records_name_can_be_used_again(self, host, execs):
        host.seed("run.x", age=_HOUR)
        self._run("-n", "x")
        assert len(execs) == 1

    def test_records_of_finished_jobs_older_than_seven_days_are_swept(self, host, execs):
        host.seed("run.old", age=8 * 24 * _HOUR)
        host.seed("run.kept", age=6 * 24 * _HOUR)
        host.seed("run.busy", age=8 * 24 * _HOUR)
        host.container("run.busy", True)
        self._run("-n", "c")
        assert self._record_names() == {"run-run.kept.json", "run-run.busy.json", "run-run.c.json"}

    def test_the_sweep_runs_on_status_too(self, host):
        host.seed("run.old", age=8 * 24 * _HOUR)
        host.committed({})
        broker.action_run(TICKET, ["status"])
        assert self._record_names() == set()

    def test_two_parallel_starts_cannot_both_pass_the_cap(self, host, monkeypatch):
        """One live job, cap 2: with the lock exactly one of two simultaneous starts gets the last slot.
        The probe of the live job sleeps, so both starts would count a single live job without it."""
        host.seed("run.a")
        host.container("run.a", True)
        monkeypatch.setenv("FAKE_PROBE_SLEEP", "0.6")
        launched: list[list[str]] = []
        monkeypatch.setattr(broker, "_exec", launched.append)
        monkeypatch.setattr(os, "chdir", lambda path: None)
        outcomes: list[object] = []

        def start() -> None:
            try:
                broker.action_run(TICKET, ["start", "@a", "-r", "R"])
                outcomes.append("started")
            except SystemExit as e:
                outcomes.append(e.code)

        threads = [threading.Thread(target=start) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(outcomes, key=str) == [3, "started"]
        assert len(launched) == 1


class TestJobStatus:
    @pytest.fixture(autouse=True)
    def repo_jj(self, host):
        host.committed({})

    def test_the_list_has_one_line_per_own_record_of_that_kind(self, host, capsys):
        host.seed("run.a", age=2 * _HOUR, base="b1")
        host.container("run.a", True)
        host.seed("run.b", age=_HOUR, base="b2")
        host.seed("c1", kind="chain", base="b3")
        host.chain_state("c1", status="failed")
        host.seed("run.theirs", caller=OTHER_TICKET)
        host.committed({("run.b", "agent-output-run.b/summary.json"): '{"status": "no_changes"}'})
        broker.action_run(TICKET, ["status"])
        assert capsys.readouterr().out.splitlines() == ["run.a\trun\trunning\tb1", "run.b\trun\tno_changes\tb2"]
        broker.action_chain(TICKET, ["status"])
        assert capsys.readouterr().out.splitlines() == ["c1\tchain\tfailed\tb3"]

    def test_an_empty_list_prints_nothing(self, host, capsys):
        broker.action_run(TICKET, ["status"])
        assert capsys.readouterr().out == ""

    def test_the_repo_selector_narrows_the_list(self, host, tmp_path, capsys):
        host.seed("run.a")
        elsewhere = broker.Job("run", "run.e", "/elsewhere", "b", int(time.time()))
        broker._job_write(TICKET, elsewhere)
        broker.action_run(TICKET, ["--repo", "lide", "status"])
        assert [line.split("\t")[0] for line in capsys.readouterr().out.splitlines()] == ["run.a"]

    @pytest.mark.parametrize(
        "container,summary,state",
        [(True, None, "running"), (False, '{"status": "success"}', "success"), (False, '{"status": "failed"}', "failed"),
         (False, '{"status": "anchor_violation"}', "anchor_violation"), (False, "garbage", "unknown"),
         (False, '{"duration_seconds": 3}', "unknown"), (False, None, "gone")],
        ids=["running", "success", "failed", "passed-through", "unreadable", "no-status", "gone"],
    )
    def test_run_state(self, host, capsys, container, summary, state):
        host.seed("run.a", age=_HOUR)
        if container:
            host.container("run.a", True)
        host.committed({} if summary is None else {("run.a", "agent-output-run.a/summary.json"): summary})
        broker.action_run(TICKET, ["status", "run.a"])
        assert capsys.readouterr().out.splitlines()[2] == f"state: {state}"

    def test_run_detail_block(self, host, capsys):
        host.seed("run.a", age=_HOUR, base="chg1")
        host.committed({
            ("run.a", "agent-output-run.a/summary.json"): '{"status": "failed", "duration_seconds": 125}',
            ("run.a", "agent-output-run.a/result.json"): '{"cost_usd": 0.5}',
            ("run.a", "agent-output-run.a/AGENT-FAILURE.md"): "first\n\nsecond\n" + "\n".join(f"l{n}" for n in range(9)),
        })
        broker.action_run(TICKET, ["status", "run.a"])
        lines = capsys.readouterr().out.splitlines()
        assert lines[:9] == ["name: run.a", "kind: run", "state: failed", "bookmark: run.a", "base: chg1",
                             "cost: 0.5000", "duration: 125s", "outputs: agent-output-run.a/", "failure: first"]
        assert lines[9:] == ["  second", "  l0", "  l1", "  l2"]

    def test_run_detail_while_nothing_is_committed_yet(self, host, capsys):
        host.seed("run.a")
        host.container("run.a", True)
        broker.action_run(TICKET, ["status", "run.a"])
        assert capsys.readouterr().out.splitlines() == ["name: run.a", "kind: run", "state: running",
                                                        "bookmark: run.a", "base: baseid"]

    def test_chain_states(self, host, capsys):
        dead = subprocess.Popen(["true"])
        dead.wait()
        for name, fields in (("alive", {}), ("dead", {"pid": dead.pid}), ("ok", {"status": "success"}),
                             ("cut", {"status": "interrupted"}), ("none", None)):
            host.seed(name, kind="chain", age=_HOUR)
            if fields is not None:
                host.chain_state(name, **fields)
        broker.action_chain(TICKET, ["status"])
        states = {line.split("\t")[0]: line.split("\t")[2] for line in capsys.readouterr().out.splitlines()}
        assert states == {"alive": "running", "dead": "stale", "ok": "success", "cut": "interrupted", "none": "gone"}

    def test_chain_detail_block(self, host, capsys):
        host.seed("c1", kind="chain", base="chg1")
        host.chain_state("c1", status="failed", finished_at="2026-10-27T10:02:05Z", total_cost_usd=1.25,
                         failure_reason="step 'b' failed",
                         completed_steps=[{"name": "a", "status": "success"}, {"name": "b", "status": "failed"}])
        broker.action_chain(TICKET, ["status", "c1"])
        assert capsys.readouterr().out.splitlines() == [
            "name: c1", "kind: chain", "state: failed", "bookmark: chain.c1", "base: chg1", "cost: 1.2500",
            "duration: 125s", "outputs: chain-outputs/c1/", "steps: a=success b=failed", "failure: step 'b' failed"]

    def test_output_is_capped(self, host, capsys):
        host.seed("c1", kind="chain")
        host.chain_state("c1", status="failed", failure_reason="x" * 5000)
        broker.action_chain(TICKET, ["status", "c1"])
        assert max(len(line) for line in capsys.readouterr().out.splitlines()) < 300

    @pytest.mark.parametrize("action,name", [(broker.action_run, "run.theirs"), (broker.action_run, "run.nope"),
                                             (broker.action_chain, "run.a")], ids=["other-callers", "unknown", "other-kind"])
    def test_only_own_records_of_that_kind(self, host, capsys, action, name):
        host.seed("run.a")
        host.seed("run.theirs", caller=OTHER_TICKET)
        code, err = _denied_with(action, TICKET, ["status", name], capsys)
        assert code == 3
        assert "started through the broker" in err

    def test_a_bad_name_or_extra_token_denied(self, host, capsys):
        assert _denied_with(broker.action_run, TICKET, ["status", "../x"], capsys)[0] == 2
        assert _denied_with(broker.action_run, TICKET, ["status", "a", "b"], capsys)[0] == 2


class TestJobCancel:
    def test_run_stops_its_container(self, host, execs):
        host.seed("run.a")
        host.container("run.a", True)
        broker.action_run(TICKET, ["cancel", "run.a"])
        assert execs == [["docker", "stop", "run.a"]]

    @pytest.mark.parametrize("fields", [{}, {"pid": 0}], ids=["alive", "launching-pid-0"])
    def test_chain_runs_the_host_cancel_in_its_repo(self, host, execs, fields):
        host.seed("c1", kind="chain")
        host.chain_state("c1", **fields)
        broker.action_chain(TICKET, ["cancel", "c1"])
        assert execs == [["cld", "chain", "cancel", "c1"]]
        assert os.getcwd() == str(host.repo)

    def test_a_chain_whose_runner_died_can_still_be_cancelled(self, host, execs):
        dead = subprocess.Popen(["true"])
        dead.wait()
        host.seed("c1", kind="chain", age=_HOUR)
        host.chain_state("c1", pid=dead.pid)
        broker.action_chain(TICKET, ["cancel", "c1"])
        assert execs == [["cld", "chain", "cancel", "c1"]]

    def test_a_finished_job_is_not_cancelled(self, host, execs, capsys):
        host.seed("run.a", age=_HOUR)
        host.container("run.a", False)
        host.committed({("run.a", "agent-output-run.a/summary.json"): '{"status": "success"}'})
        code, err = _denied_with(broker.action_run, TICKET, ["cancel", "run.a"], capsys)
        assert code == 3
        assert "not running (state: success)" in err
        assert execs == []

    @pytest.mark.parametrize("action,name", [(broker.action_run, "run.theirs"), (broker.action_run, "run.nope"),
                                             (broker.action_chain, "run.theirs")])
    def test_only_own_jobs(self, host, execs, capsys, action, name):
        host.seed("run.theirs", caller=OTHER_TICKET)
        host.container("run.theirs", True)
        assert _denied_with(action, TICKET, ["cancel", name], capsys)[0] == 3
        assert execs == []

    def test_exactly_one_name(self, host, capsys):
        assert _denied_with(broker.action_run, TICKET, ["cancel"], capsys)[0] == 2
        assert _denied_with(broker.action_run, TICKET, ["cancel", "a", "b"], capsys)[0] == 2


class TestJobDispatch:
    """The ssh route end to end, the way the container client calls it: a real jj repo, fake docker and cld."""

    def _dispatch(self, tmp_path: Path, action: str, argv: list[str]) -> subprocess.CompletedProcess:
        payload = base64.b64encode(b"".join(a.encode() + b"\0" for a in argv)).decode()
        env = dict(os.environ, CLD_BROKER_CONF="/nonexistent", SSH_ORIGINAL_COMMAND=f"{action} t.x {payload}")
        return subprocess.run([sys.executable, str(BROKER_PY)], capture_output=True, text=True, env=env, cwd=tmp_path)

    def test_start_then_status(self, tree, jj_host, tmp_path):
        result = self._dispatch(tmp_path, "run", ["--repo", "lide", "start", "@a", "-n", "x", "-r", tree.inside, "-p", "t"])
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [
            "started: run.x", f"CWD {tree.repo}", "run", "@a", "-n", "x", "-r", tree.inside, "-p", "t"]
        jj_host.container("run.x", True)
        status = self._dispatch(tmp_path, "run", ["--repo", "lide", "status"])
        assert status.stdout == f"run.x\trun\trunning\t{tree.inside}\n"

    def test_chain_start(self, tree, jj_host, tmp_path):
        result = self._dispatch(tmp_path, "chain", ["start", "@rdi", "@b", "-n", "mine", "-r", tree.inside])
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [
            "started: mine", f"CWD {tree.repo}", "chain", "run", "@rdi", "@b", "-n", "mine", "-r", tree.inside]

    def test_a_denied_start_prints_no_started_line(self, tree, jj_host, tmp_path):
        result = self._dispatch(tmp_path, "run", ["start", "@a", "-r", tree.sibling])
        assert result.returncode == 3
        assert result.stdout == ""
