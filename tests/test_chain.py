"""Tests for chain orchestration: parse/validate, run_chain, detached launch, status.

Covers the main happy-path user experiences of `cld chain run` (detached default),
`cld chain status`, and the synchronous run_chain core.
"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from cld.chain import apply_name_override, chain_branch, load_chain, run_chain, validate_chain
from cld.chain_state import ChainState, _utcnow_iso, write_state
from cld.cli import _collect_chain_rows, app
from cld.config import Config
from cld.vcs.anchor import resolve_anchor


runner = CliRunner()

_CLD_ROOT = Path(__file__).resolve().parent.parent


def _write_chain(path: Path, name: str, steps: str) -> Path:
    path.write_text(f"name: {name}\ndefaults:\n  model: sonnet\nsteps:\n{steps}")
    return path


def _make_fake_agent_commit(vcs, revision, session_name, files):
    """Commit + branch named session_name on top of revision (mimics a real agent)."""
    sha = vcs.resolve_revision(revision)
    vcs.run(["new", sha] if vcs.name == "jj" else ["checkout", sha])
    for fname, content in files.items():
        target = vcs.repo_root / fname
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    vcs.commit(f"fake {session_name}")
    tip_alias = "@-" if vcs.name == "jj" else "HEAD"
    vcs.create_branch(session_name, tip_alias)


def _install_chain_mocks(monkeypatch, vcs, wait_results):
    la_mock = MagicMock()
    monkeypatch.setattr("cld.chain.launch_run", la_mock)
    monkeypatch.setattr("cld.chain.wait_for_agent", MagicMock(side_effect=list(wait_results)))
    monkeypatch.setattr("cld.chain.get_backend", lambda *_a, **_kw: vcs)
    monkeypatch.setattr("cld.chain.read_agent_cost", lambda *a, **k: 0.0)
    monkeypatch.chdir(vcs.repo_root)
    return la_mock


# --- parse + validate ---------------------------------------------------------


class TestLoadValidate:
    def test_single_step_chain_parses_and_validates(self, tmp_path):
        f = _write_chain(
            tmp_path / "c.yaml", "demo",
            "  - name: build\n    prompts: ['@personas/implementer']\n",
        )
        chain = load_chain(f)
        assert chain.name == "demo"
        assert len(chain.steps) == 1
        assert chain.steps[0].name == "build"
        # personas resolve from the real cld_root/prompts/personas.
        validate_chain(chain, tmp_path, _CLD_ROOT)

    def test_parallel_group_parses(self, tmp_path):
        f = _write_chain(
            tmp_path / "c.yaml", "demo",
            "  - parallel:\n"
            "    - name: a\n      prompts: ['@personas/implementer']\n"
            "    - name: b\n      prompts: ['@personas/reviewer']\n",
        )
        chain = load_chain(f)
        validate_chain(chain, tmp_path, _CLD_ROOT)
        assert len(chain.steps) == 1
        assert len(chain.steps[0].siblings) == 2


class TestNameOverride:
    def test_apply_name_override_replaces_name(self, tmp_path):
        f = _write_chain(
            tmp_path / "c.yaml", "demo",
            "  - name: build\n    prompts: ['@personas/implementer']\n",
        )
        chain = load_chain(f)
        overridden = apply_name_override(chain, "myrun")
        assert overridden.name == "myrun"
        assert chain_branch(overridden) == "chain_myrun"

    def test_apply_name_override_empty_is_noop(self, tmp_path):
        f = _write_chain(
            tmp_path / "c.yaml", "demo",
            "  - name: build\n    prompts: ['@personas/implementer']\n",
        )
        chain = load_chain(f)
        assert apply_name_override(chain, "").name == "demo"

    def test_apply_name_override_rejects_bad_chars(self, tmp_path):
        f = _write_chain(
            tmp_path / "c.yaml", "demo",
            "  - name: build\n    prompts: ['@personas/implementer']\n",
        )
        chain = load_chain(f)
        with pytest.raises(ValueError):
            apply_name_override(chain, "bad name!")

    def test_run_chain_name_suffix_overrides_branch(self, vcs_repo, monkeypatch):
        vcs = vcs_repo
        if vcs.name == "git":
            pytest.xfail("git backend anchor staging is out of scope for the container-ephemeral-workspace rewrite")
        la_mock = _install_chain_mocks(monkeypatch, vcs, [{"status": "success"}])
        f = _write_chain(
            vcs.repo_root / "c.yaml", "demo",
            "  - name: build\n    prompts: ['@personas/implementer']\n",
        )

        def fake(cfg, brief="", *, revision="", session_name=None, **kw):
            _make_fake_agent_commit(
                vcs, revision, session_name,
                {"chain-outputs/myrun/build.md": "X\n"},
            )
            return {"session_name": session_name}

        la_mock.side_effect = fake
        result = run_chain(Config(), f, initial_task="x", name_suffix="myrun")
        assert result.chain_name == "myrun"
        assert result.chain_branch == "chain_myrun"
        session_used = la_mock.call_args.kwargs["session_name"]
        assert session_used == "chain_myrun_build"


# --- run_chain core -----------------------------------------------------------


class TestRunChain:
    def test_single_step_success_advances_branch(self, vcs_repo, monkeypatch, tmp_path):
        vcs = vcs_repo
        if vcs.name == "git":
            pytest.xfail("git backend anchor staging is out of scope for the container-ephemeral-workspace rewrite")
        la_mock = _install_chain_mocks(monkeypatch, vcs, [{"status": "success"}])

        f = _write_chain(
            vcs.repo_root / "c.yaml", "t",
            "  - name: build\n    prompts: ['@personas/implementer']\n",
        )

        def fake(cfg, brief="", *, revision="", session_name=None, **kw):
            _make_fake_agent_commit(
                vcs, revision, session_name,
                {"chain-outputs/t/build.md": "BUILD_OUTPUT\n"},
            )
            return {"session_name": session_name}

        la_mock.side_effect = fake

        result = run_chain(Config(), f, initial_task="do it")

        assert result.success
        assert la_mock.call_count == 1
        branch = chain_branch(load_chain(f))
        assert result.chain_branch == branch
        if vcs.name == "git":
            # Pre-existing: git refuses `branch -f` on a worktree-checked-out
            # branch, so the accumulator can't advance to the step's tip.
            pytest.xfail("git backend cannot advance a worktree-checked-out branch")
        # On jj the accumulator advanced to the step's tip; the output lands there.
        assert vcs.file_show(branch, "chain-outputs/t/build.md") == "BUILD_OUTPUT\n"

    def test_prior_output_feeds_next_step(self, vcs_repo, monkeypatch):
        vcs = vcs_repo
        if vcs.name == "git":
            pytest.xfail("git backend anchor staging is out of scope for the container-ephemeral-workspace rewrite")
        la_mock = _install_chain_mocks(
            monkeypatch, vcs, [{"status": "success"}, {"status": "success"}],
        )

        f = _write_chain(
            vcs.repo_root / "c.yaml", "t",
            "  - name: first\n    prompts: ['@personas/implementer']\n"
            "  - name: second\n    prompts: ['@personas/reviewer']\n",
        )

        captured = {}

        def fake(cfg, brief="", *, revision="", session_name=None, **kw):
            files = {}
            if session_name.endswith("_first"):
                files = {"chain-outputs/t/first.md": "FIRST_OUTPUT\n"}
            elif session_name.endswith("_second"):
                captured["task"] = brief
                files = {"chain-outputs/t/second.md": "SECOND_OUTPUT\n"}
            _make_fake_agent_commit(vcs, revision, session_name, files)
            return {"session_name": session_name}

        la_mock.side_effect = fake

        result = run_chain(Config(), f, initial_task="do it")

        assert result.success
        assert la_mock.call_count == 2
        assert "FIRST_OUTPUT" in captured["task"]

    def test_step_failure_stops_and_reports(self, vcs_repo, monkeypatch):
        vcs = vcs_repo
        if vcs.name == "git":
            pytest.xfail("git backend anchor staging is out of scope for the container-ephemeral-workspace rewrite")
        la_mock = _install_chain_mocks(monkeypatch, vcs, [{"status": "failed"}])

        f = _write_chain(
            vcs.repo_root / "c.yaml", "t",
            "  - name: build\n    prompts: ['@personas/implementer']\n"
            "  - name: never\n    prompts: ['@personas/reviewer']\n",
        )

        def fake(cfg, brief="", *, revision="", session_name=None, **kw):
            _make_fake_agent_commit(vcs, revision, session_name, {"x.md": "partial\n"})
            return {"session_name": session_name}

        la_mock.side_effect = fake

        result = run_chain(Config(), f, initial_task="do it")

        assert not result.success
        assert la_mock.call_count == 1
        assert "build" in result.failure_reason


# --- detached launch (foreground anchor pinning) ------------------------------


class TestChainRunDetached:
    @pytest.fixture(autouse=True)
    def _no_store_preflight(self, monkeypatch):
        """The store preflight needs host jj 0.38+ and is covered in test_docker.py."""
        monkeypatch.setattr("cld.cli.prepare_store_mounts", lambda repo: None)

    def test_pins_anchor_and_records_running_state(self, vcs_repo, monkeypatch):
        vcs = vcs_repo
        monkeypatch.chdir(vcs.repo_root)

        f = _write_chain(
            vcs.repo_root / "c.yaml", "demo",
            "  - name: build\n    prompts: ['@personas/implementer']\n",
        )

        spawn = MagicMock(return_value=44321)
        monkeypatch.setattr("cld.cli._spawn_chain_runner", spawn)

        result = runner.invoke(app, ["chain", "run", str(f), "-p", "do stuff"])
        assert result.exit_code == 0, result.output

        # No real child ran; the parent must have pinned everything in foreground.
        spawn.assert_called_once()
        state_file = vcs.repo_root / ".cld" / "chains" / "chain_demo" / "state.json"
        state = ChainState.load(state_file)
        assert state.status == "running"
        assert state.anchor_hash  # resolved in the foreground, before child boot
        assert state.pid == 44321

    def test_runner_uses_pinned_anchor_and_finishes(self, vcs_repo, monkeypatch):
        """The background runner must consume the foreground-pinned anchor, not
        re-resolve it, and drive the chain to a terminal 'success' state."""
        vcs = vcs_repo
        if vcs.name == "git":
            pytest.xfail("git backend anchor staging is out of scope for the container-ephemeral-workspace rewrite")
        # Mirror production: .cld/ is gitignored, so the state dir is never part
        # of the tracked tree and survives the workspace ops run_chain performs.
        (vcs.repo_root / ".gitignore").write_text(".cld/\n")
        anchor = resolve_anchor(vcs, "")
        f = _write_chain(
            vcs.repo_root / "c.yaml", "demo",
            "  - name: build\n    prompts: ['@personas/implementer']\n",
        )
        state_dir = vcs.repo_root / ".cld" / "chains" / "chain_demo"
        state_dir.mkdir(parents=True)
        _seed_state(
            state_dir, "demo", status="running", pid=0,
            anchor_hash=anchor, chain_file=str(f),
            inputs={"inline_prompt": "go"},
        )

        la_mock = _install_chain_mocks(monkeypatch, vcs, [{"status": "success"}])

        def fake(cfg, brief="", *, revision="", session_name=None, **kw):
            _make_fake_agent_commit(
                vcs, revision, session_name,
                {"chain-outputs/demo/build.md": "OUT\n"},
            )
            return {"session_name": session_name}

        la_mock.side_effect = fake
        # Killer assertion: a child that re-resolves the anchor would blow up.
        monkeypatch.setattr(
            "cld.chain.resolve_anchor",
            MagicMock(side_effect=AssertionError("child re-resolved the anchor")),
        )

        result = runner.invoke(app, ["chain", "_chain-runner", str(state_dir)])
        assert result.exit_code == 0, result.output

        st = ChainState.load(state_dir / "state.json")
        assert st.status == "success"
        assert st.finished_at
        assert st.pid == os.getpid()
        assert st.completed_steps

    def test_refuses_when_already_running(self, vcs_repo, monkeypatch):
        vcs = vcs_repo
        monkeypatch.chdir(vcs.repo_root)
        f = _write_chain(
            vcs.repo_root / "c.yaml", "demo",
            "  - name: build\n    prompts: ['@personas/implementer']\n",
        )
        state_dir = vcs.repo_root / ".cld" / "chains" / "chain_demo"
        state_dir.mkdir(parents=True)
        _seed_state(state_dir, "demo", status="running", pid=os.getpid())

        spawn = MagicMock()
        monkeypatch.setattr("cld.cli._spawn_chain_runner", spawn)

        result = runner.invoke(app, ["chain", "run", str(f), "-p", "do stuff"])
        assert result.exit_code == 1
        assert "already running" in result.output
        spawn.assert_not_called()


# --- status -------------------------------------------------------------------


def _seed_state(
    state_dir: Path, name: str, *, status: str, pid: int,
    anchor_hash: str = "abc123", chain_file: str = "/x.yaml",
    inputs: dict | None = None,
) -> None:
    st = ChainState(
        schema_version=1, kind="chain", chain_name=name,
        chain_session=f"chain_{name}", chain_branch=f"chain_{name}",
        chain_file=chain_file, anchor_hash=anchor_hash, pid=pid,
        started_at=_utcnow_iso(), finished_at=None,
        log_file=str(state_dir / "chain.log"), status=status,
        total_steps=2, current_index=0, current_kind="step",
        current_step_name="build", current_step_sessions=[],
        completed_steps=[], total_cost_usd=1.25, failure_reason="",
        inputs=inputs or {},
    )
    write_state(state_dir / "state.json", st.to_dict())


class TestChainStatus:
    def _chains_dir(self, root, specs):
        cdir = root / ".cld" / "chains"
        for name, status, pid in specs:
            d = cdir / f"chain_{name}"
            d.mkdir(parents=True)
            _seed_state(d, name, status=status, pid=pid)
        return cdir

    def test_running_shown_terminal_hidden_by_default(self, tmp_path):
        cdir = self._chains_dir(tmp_path, [
            ("live", "running", os.getpid()),
            ("done", "success", 999999),
        ])
        rows = _collect_chain_rows(cdir, include_terminal=False)
        names = {r["name"] for r in rows}
        assert names == {"live"}

    def test_all_includes_terminal(self, tmp_path):
        cdir = self._chains_dir(tmp_path, [
            ("live", "running", os.getpid()),
            ("done", "success", 999999),
        ])
        rows = _collect_chain_rows(cdir, include_terminal=True)
        assert {r["name"] for r in rows} == {"live", "done"}

    def test_archived_prev_dir_ignored(self, tmp_path):
        cdir = tmp_path / ".cld" / "chains"
        live = cdir / "chain_demo"
        live.mkdir(parents=True)
        _seed_state(live, "demo", status="running", pid=os.getpid())
        prev = cdir / "chain_demo.prev"
        prev.mkdir(parents=True)
        _seed_state(prev, "demo", status="running", pid=999999)
        rows = _collect_chain_rows(cdir, include_terminal=True)
        assert len(rows) == 1
        assert rows[0]["pid"] == os.getpid()

    def test_dead_pid_marked_stale(self, tmp_path):
        # A pid that is (almost certainly) not alive and not owned by us.
        cdir = self._chains_dir(tmp_path, [("zombie", "running", 999999)])
        rows = _collect_chain_rows(cdir, include_terminal=False)
        assert rows[0]["status"] == "stale"

    def test_empty_prints_none(self, vcs_repo, monkeypatch):
        monkeypatch.chdir(vcs_repo.repo_root)
        result = runner.invoke(app, ["chain", "status"])
        assert result.exit_code == 0
        assert "No chains found" in result.output

    def test_json_output(self, tmp_path):
        cdir = self._chains_dir(tmp_path, [("live", "running", os.getpid())])
        rows = _collect_chain_rows(cdir, include_terminal=False)
        # Round-trips cleanly to JSON (the --json path).
        parsed = json.loads(json.dumps(rows))
        assert parsed[0]["name"] == "live"
        assert parsed[0]["cost_usd"] == 1.25
