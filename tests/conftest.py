"""Shared pytest fixtures: env cleanup, VCS repos, docker helpers."""

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from cld.docker import StoreLayout
from cld.vcs.git import GitBackend
from cld.vcs.jj import JjBackend


_LEAKY_VARS = (
    "WORKSPACE_ORIGIN",
    "CLD_HOST_PROJECT_DIR",
    "CLD_HOST_HOME",
    "AGENT_MODE",
    # No launcher sets HUB_MODE since the master role was removed, but the
    # broker dispatch still reads it (cld/docker.py:in_master_container) and a
    # stale one in the developer's shell would flip that dispatch under tests.
    "HUB_MODE",
) + (
    "CLD_LOG_LEVEL",
    "CLD_LOG_COLOR",
    "CLD_DEBUG",
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = _PROJECT_ROOT / "tests" / "fixtures"

# CLD_HOST_PROJECT_DIR detection: E2E tests can run either on the host or inside
# the devcontainer. When running inside the devcontainer, Docker child containers
# receive volume mounts using *host* paths, but the container's own filesystem
# sees the repo at /workspace/origin. CLD_HOST_PROJECT_DIR is set by the host
# launcher when running inside a container to the original host-side project
# path so that we can translate /workspace/origin/* paths back to host paths
# before passing them to Docker. When unset we're running on the host directly
# and no translation is needed -- _PROJECT_ROOT is already the correct host path.
_HOST_PROJECT_DIR = os.environ.get("CLD_HOST_PROJECT_DIR", "")
_DOCKER_ROOT = Path("/workspace/origin") if _HOST_PROJECT_DIR else _PROJECT_ROOT
_E2E_REPO_BASE = _DOCKER_ROOT / ".test-repos"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in _LEAKY_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("JJ_EDITOR", "true")


@pytest.fixture(autouse=True)
def reset_cld_loggers():
    yield
    cld_log = logging.getLogger("cld")
    for h in list(cld_log.handlers):
        cld_log.removeHandler(h)
    cld_log.setLevel(logging.NOTSET)
    cld_log.propagate = True


@pytest.fixture
def stub_launch_checks(monkeypatch):
    """Skip the daemon-bound launch checks of the arg builders: the image
    labels, the refusal scan for other containers that mount the repo, and the list
    of repos other containers use (the marketplace filter)."""
    monkeypatch.setattr("cld.docker.require_boot_api", lambda *images: None)
    monkeypatch.setattr("cld.docker.docker_occupant_list", lambda: [])
    monkeypatch.setattr("cld.docker.refuse_unprotected_containers", lambda name, host_repo: None)


@pytest.fixture
def stub_store_preflight(monkeypatch, stub_launch_checks):
    """Builder tests on bare directories: report a git layout with no host checks."""
    monkeypatch.setattr(
        "cld.docker.prepare_store_mounts",
        lambda host_repo, name="": StoreLayout(jj=False, git_dir=".git"),
    )


# --- VCS repo fixtures (use tmp_path -- fast, no Docker visibility needed) ----


def _init_jj(path):
    subprocess.run(["jj", "git", "init"], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("seed\n")
    subprocess.run(
        ["jj", "commit", "-m", "seed commit"],
        cwd=path, check=True, capture_output=True,
    )
    return JjBackend(path)


def _init_git(path):
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=path, check=True, capture_output=True,
    )
    (path / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "seed commit"],
        cwd=path, check=True, capture_output=True,
    )
    return GitBackend(path)


@pytest.fixture
def jj_repo(tmp_path):
    """Init a jj repo with a seeded commit, return JjBackend."""
    return _init_jj(tmp_path)


@pytest.fixture
def git_repo(tmp_path):
    """Init a git repo with a seeded commit, return GitBackend."""
    return _init_git(tmp_path)


@pytest.fixture(params=["jj", "git"])
def vcs_repo(request, tmp_path):
    """Parametrized fixture yielding both backend types in separate directories."""
    path = tmp_path / request.param
    path.mkdir()
    if request.param == "jj":
        return _init_jj(path)
    return _init_git(path)


# --- Host-visible repo fixtures (for E2E container tests) ---------------------


def _to_host_path(container_path):
    """Translate container path under /workspace/origin to host path."""
    if _HOST_PROJECT_DIR and str(container_path).startswith("/workspace/origin"):
        return _HOST_PROJECT_DIR + str(container_path)[len("/workspace/origin"):]
    return str(container_path)


@pytest.fixture
def e2e_jj_repo():
    """Init a jj repo under a host-visible path for E2E Docker tests."""
    _E2E_REPO_BASE.mkdir(parents=True, exist_ok=True)
    import random
    name = f"jj_{random.randint(10000, 99999)}"
    path = _E2E_REPO_BASE / name
    path.mkdir()
    backend = _init_jj(path)
    yield backend
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def e2e_git_repo():
    """Init a git repo under a host-visible path for E2E Docker tests."""
    _E2E_REPO_BASE.mkdir(parents=True, exist_ok=True)
    import random
    name = f"git_{random.randint(10000, 99999)}"
    path = _E2E_REPO_BASE / name
    path.mkdir()
    backend = _init_git(path)
    yield backend
    shutil.rmtree(path, ignore_errors=True)


def _add_feature_branch_jj(path, backend):
    """Add trunk + feature branches to an existing jj repo."""
    subprocess.run(
        ["jj", "bookmark", "create", "trunk", "-r", "@-"],
        cwd=path, check=True, capture_output=True,
    )
    (path / "main.py").write_text("def main():\n    print('hello')\n")
    (path / "new_feature.py").write_text("def feature():\n    return 42\n")
    subprocess.run(
        ["jj", "commit", "-m", "add feature"],
        cwd=path, check=True, capture_output=True,
    )
    subprocess.run(
        ["jj", "bookmark", "create", "feature", "-r", "@-"],
        cwd=path, check=True, capture_output=True,
    )
    return backend


def _add_feature_branch_git(path, backend):
    """Add trunk + feature branches to an existing git repo."""
    subprocess.run(
        ["git", "branch", "trunk"],
        cwd=path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "-b", "feature"],
        cwd=path, check=True, capture_output=True,
    )
    (path / "main.py").write_text("def main():\n    print('hello')\n")
    (path / "new_feature.py").write_text("def feature():\n    return 42\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add feature"],
        cwd=path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "trunk"],
        cwd=path, check=True, capture_output=True,
    )
    return backend


@pytest.fixture(params=["jj", "git"])
def e2e_repo_with_branches(request):
    """Host-visible repo with trunk + feature branches for E2E Docker tests."""
    _E2E_REPO_BASE.mkdir(parents=True, exist_ok=True)
    import random
    vcs_type = request.param
    name = f"{vcs_type}_review_{random.randint(10000, 99999)}"
    path = _E2E_REPO_BASE / name
    path.mkdir()
    if vcs_type == "jj":
        backend = _init_jj(path)
        _add_feature_branch_jj(path, backend)
    else:
        backend = _init_git(path)
        _add_feature_branch_git(path, backend)
    yield backend
    shutil.rmtree(path, ignore_errors=True)


# --- Stub fixtures ------------------------------------------------------------


_STUB_DIR_BASE = _DOCKER_ROOT / "tests" / "fixtures"


@pytest.fixture
def claude_stub():
    """Host-visible directory containing a claude stub that creates a file."""
    return _STUB_DIR_BASE / "stub-default"


@pytest.fixture
def claude_stub_noop():
    """Host-visible directory containing a claude stub that makes no changes."""
    return _STUB_DIR_BASE / "stub-noop"


# --- Docker helpers -----------------------------------------------------------


def docker_available() -> bool:
    """Check whether the Docker daemon is reachable."""
    try:
        r = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def image_exists(image: str) -> bool:
    r = subprocess.run(
        ["docker", "images", "-q", image], capture_output=True, text=True,
    )
    return bool(r.stdout.strip())


skip_no_docker = pytest.mark.skipif(
    not docker_available(), reason="Docker daemon not available",
)

skip_no_agent_image = pytest.mark.skipif(
    not docker_available() or not image_exists("claude-agent:latest"),
    reason="claude-agent:latest image not built",
)

skip_no_devcontainer_image = pytest.mark.skipif(
    not docker_available() or not image_exists("claude-devcontainer:latest"),
    reason="claude-devcontainer:latest image not built",
)



def run_agent_container(
    repo_path, session_name, task_content, stub_dir,
    *, env=None, timeout=120, vcs_type="jj",
):
    """Run an agent container with a claude stub against a real repo.

    stub_dir is a directory containing a `claude` executable.
    Both paths are translated to host-visible paths via _to_host_path.
    Returns (exit_code, summary_dict_or_None, stdout, stderr).
    """
    task_file = repo_path / ".test-task.md"
    task_file.write_text(task_content)

    host_repo = _to_host_path(str(repo_path))
    host_stub_dir = _to_host_path(str(stub_dir))

    uid_gid = f"{os.getuid()}:{os.getgid()}"

    cmd = [
        "docker", "run", "--rm",
        "--name", session_name,
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--cpus=1.0",
        "--memory=2g",
        "--user", uid_gid,
        "-e", "HOME=/home/claude",
        "-e", "JJ_EDITOR=true",
        "-e", "GIT_EDITOR=true",
        "-e", f"SESSION_NAME={session_name}",
        "-e", "INSTRUCTION_FILE=/workspace/origin/.test-task.md",
        "-v", f"{host_repo}:/workspace/origin",
        "-v", f"{host_stub_dir}:/tmp/bin",
        "-w", "/workspace/current",
    ]
    if env:
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
    cmd.append("claude-agent:latest")

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    if vcs_type == "jj":
        backend = JjBackend(repo_path)
    else:
        backend = GitBackend(repo_path)

    summary_raw = backend.file_show(
        session_name, f"agent-output-{session_name}/summary.json",
    )
    summary = None
    if summary_raw:
        try:
            summary = json.loads(summary_raw)
        except json.JSONDecodeError:
            pass

    return result.returncode, summary, result.stdout, result.stderr
