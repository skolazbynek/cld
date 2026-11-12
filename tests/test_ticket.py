"""Tests for the ticket container lifecycle (cld/ticket.py).

Docker is faked at the subprocess seam (the test_docker.py style) or one
level up (patching the ticket module's own helpers), so nothing here needs
a daemon.
"""

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer

from cld.config import Config
from cld.manifest import RepoManifestEntry, TicketManifest, manifest_labels
from cld.registry import RepoEntry
from cld.ticket import (
    _bookmark_tip,
    _echo_new_log_lines,
    _parse_reanchor,
    RebaseHint,
    _rebase_hints,
    _session_state,
    exec_claude,
    exec_shell,
    forget_session_state,
    list_tickets,
    print_ticket_detail,
    _print_manifest_diff,
    print_ticket_logs,
    print_ticket_roster,
    ready_timeout,
    restart_ticket,
    shutdown_all_tickets,
    shutdown_ticket,
    start_ticket,
    stop_ticket,
    wait_ticket_ready,
)
from cld.vcs.anchor import move_anchor_commit
from cld.vcs.git import NO_HOOKS
from cld.vcs.scratch import stage_in_workspace


def _res(rc=0, out="", err=""):
    return type("R", (), {"returncode": rc, "stdout": out, "stderr": err})()


def _manifest(ticket="lide-2600", *repos):
    repos = repos or (("lide-api", "/home/u/lide-api"), ("diskuze-api", "/home/u/diskuze-api"))
    return TicketManifest(ticket=ticket, repos=tuple(
        RepoManifestEntry(name=name, path=path, anchor_base="a" * 40)
        for name, path in repos
    ))


class TestReadyTimeout:
    def test_scales_with_repo_count(self):
        """60 + 45 x n (design section 4.4)."""
        assert ready_timeout(1) == 105
        assert ready_timeout(3) == 195


class TestWaitTicketReady:
    def test_true_once_sentinel_appears(self):
        probes = iter([_res(1), _res(1), _res(0)])
        with patch("cld.ticket.subprocess.run", side_effect=lambda *a, **k: next(probes)), \
             patch("cld.ticket.time.sleep"):
            assert wait_ticket_ready("c", 1) is True

    def test_false_on_timeout(self):
        # Fake clock: deadline at 0+30, first loop check already past it.
        clock = iter([0, 0, 31])
        with patch("cld.ticket.subprocess.run", return_value=_res(1)), \
             patch("cld.ticket.time.sleep"), \
             patch("cld.ticket.time.time", side_effect=lambda: next(clock)):
            assert wait_ticket_ready("c", 1, timeout=30) is False

    def test_zero_timeout_means_the_scaled_default(self):
        """timeout=0 falls back to ready_timeout(n) -- the launch path relies on it."""
        clock = iter([0, 0, 100, 100, 106, 106])
        with patch("cld.ticket.subprocess.run", return_value=_res(1)), \
             patch("cld.ticket.time.sleep"), \
             patch("cld.ticket.time.time", side_effect=lambda: next(clock)):
            # n_repos=1 -> 105 s: alive at t=100, expired at t=106.
            assert wait_ticket_ready("c", 1, timeout=0) is False

    def test_echoes_only_new_log_lines(self, capsys):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:2] == ["docker", "logs"]:
                return _res(0, out="line1\nline2\n")
            return _res(1)  # sentinel never appears

        clock = iter(range(0, 100, 6))  # 6 s per loop turn -> echo every other turn
        with patch("cld.ticket.subprocess.run", side_effect=fake_run), \
             patch("cld.ticket.time.sleep"), \
             patch("cld.ticket.time.time", side_effect=lambda: next(clock)):
            assert wait_ticket_ready("c", 1, timeout=30) is False
        out = capsys.readouterr().out
        # Repeated echo rounds must not reprint already-shown lines.
        assert out.count("line1") == 1
        assert out.count("line2") == 1

    def test_echo_helper_returns_new_count(self, capsys):
        with patch("cld.ticket.subprocess.run", return_value=_res(0, out="a\nb\nc\n")):
            assert _echo_new_log_lines("c", 1) == 3
        assert capsys.readouterr().out == "  [boot] b\n  [boot] c\n"


class TestStartTicket:
    """Arg -> manifest -> launch plumbing. The anchor resolver is faked so
    resolve_manifest runs for real against the registry."""

    def _cfg(self, tmp_path):
        repo = tmp_path / "lide-api"
        repo.mkdir()
        return Config(repos={
            "lide-api": RepoEntry(path=str(repo), default_rev="trunk()"),
        })

    def _resolver(self, calls=None):
        def resolve(path, revision, mode):
            if calls is not None:
                calls.append((path, revision, mode))
            return "b" * 40
        return lambda cfg: resolve

    def test_absent_specs_resolve_into_launched_manifest(self, tmp_path):
        cfg = self._cfg(tmp_path)
        calls = []
        with patch("cld.ticket._docker_status", return_value="absent"), \
             patch("cld.ticket.ticket_anchor_resolver", self._resolver(calls)), \
             patch("cld.ticket._launch") as launch:
            start_ticket(cfg, "LIDE-2600", ["lide-api@feature-x"], [])
        manifest = launch.call_args.args[1]
        assert manifest.ticket == "lide-2600"
        [repo] = manifest.repos
        assert repo.name == "lide-api"
        assert repo.anchor_base == "b" * 40
        assert repo.rev_source == "arg"
        # The @rev override reached the (overlap-checking) resolver verbatim.
        assert calls == [(str(tmp_path / "lide-api"), "feature-x", "isolated")]

    def test_shared_anchor_flag_reaches_the_manifest(self, tmp_path):
        cfg = self._cfg(tmp_path)
        with patch("cld.ticket._docker_status", return_value="absent"), \
             patch("cld.ticket.ticket_anchor_resolver", self._resolver()), \
             patch("cld.ticket._launch") as launch:
            start_ticket(cfg, "lide-2600", ["lide-api"], ["lide-api"])
        [repo] = launch.call_args.args[1].repos
        assert repo.anchor_mode == "shared"
        assert repo.rev_source == "registry"

    def test_no_repos_no_tty_is_a_hard_error(self, tmp_path):
        cfg = self._cfg(tmp_path)
        stdin = MagicMock()
        stdin.isatty.return_value = False
        with patch("cld.ticket._docker_status", return_value="absent"), \
             patch("cld.ticket.sys.stdin", stdin), \
             patch("cld.ticket._launch") as launch, \
             pytest.raises(RuntimeError, match="picker needs a TTY"):
            start_ticket(cfg, "lide-2600", [], [])
        launch.assert_not_called()

    def test_no_repos_on_tty_uses_the_picker(self, tmp_path):
        cfg = self._cfg(tmp_path)
        stdin = MagicMock()
        stdin.isatty.return_value = True
        selection = [("lide-api", cfg.repos["lide-api"], "xyz123")]
        with patch("cld.ticket._docker_status", return_value="absent"), \
             patch("cld.ticket.sys.stdin", stdin), \
             patch("cld.ticket.pick_repos", return_value=selection) as picker, \
             patch("cld.ticket.ticket_anchor_resolver", self._resolver(calls := [])), \
             patch("cld.ticket._launch") as launch:
            start_ticket(cfg, "lide-2600", [], [])
        picker.assert_called_once_with(cfg.repos)
        assert calls == [(str(tmp_path / "lide-api"), "xyz123", "isolated")]
        assert launch.call_args.args[1].repos[0].name == "lide-api"

    def test_running_without_specs_reports_and_launches_nothing(self, tmp_path, capsys):
        with patch("cld.ticket._docker_status", return_value="running"), \
             patch("cld.ticket.container_has_store_mounts", return_value=True), \
             patch("cld.ticket._launch") as launch, \
             patch("cld.ticket.subprocess.run") as run:
            start_ticket(self._cfg(tmp_path), "lide-2600", [], [])
        launch.assert_not_called()
        run.assert_not_called()
        assert "already running" in capsys.readouterr().out

    def test_stopped_without_specs_warm_starts(self, tmp_path, capsys):
        with patch("cld.ticket._docker_status", return_value="stopped"), \
             patch("cld.ticket.read_manifest", return_value=_manifest()), \
             patch("cld.ticket.container_has_store_mounts", return_value=True), \
             patch("cld.ticket.subprocess.run", return_value=_res()) as run, \
             patch("cld.ticket.wait_ticket_ready", return_value=True) as wait, \
             patch("cld.ticket._launch") as launch:
            start_ticket(self._cfg(tmp_path), "lide-2600", [], [])
        launch.assert_not_called()
        assert run.call_args.args[0] == ["docker", "start", "t.lide-2600"]
        # Warm-start wait is scaled by the labeled repo count, not a flat 60 s.
        assert wait.call_args.args == ("t.lide-2600", 2)
        assert "warm" in capsys.readouterr().out


class TestWarmStartRefusesOldMounts:
    """A container made before the store mounts existed carries no label; the
    protected paths are not mounted in it, so it must be recreated."""

    def _cfg(self, tmp_path):
        return Config(repos={"lide-api": RepoEntry(path=str(tmp_path), default_rev="trunk()")})

    def test_stopped_container_without_label_is_refused_and_not_started(self, tmp_path):
        with patch("cld.ticket._docker_status", return_value="stopped"), \
             patch("cld.ticket.read_manifest", return_value=_manifest()), \
             patch("cld.ticket.container_has_store_mounts", return_value=False), \
             patch("cld.ticket.subprocess.run") as run, \
             patch("cld.ticket._launch") as launch, \
             pytest.raises(RuntimeError, match="cld restart lide-2600") as refused:
            start_ticket(self._cfg(tmp_path), "LIDE-2600", [], [])
        assert "full host checkout" in str(refused.value)
        run.assert_not_called()
        launch.assert_not_called()

    @pytest.mark.parametrize("label", ["", "store-pins-v1"], ids=["unlabelled", "pins-v1"])
    def test_only_the_store_v1_label_warm_starts(self, tmp_path, label):
        """The label is read for real: an unlabelled container and the never-deployed
        store-pins-v1 one both still mount the checkout."""
        with patch("cld.ticket._docker_status", return_value="stopped"), \
             patch("cld.ticket.read_manifest", return_value=_manifest()), \
             patch("cld.docker._image_label", return_value=label), \
             patch("cld.ticket.subprocess.run") as run, \
             patch("cld.ticket.wait_ticket_ready", return_value=True), \
             pytest.raises(RuntimeError, match="mounts the full host checkout; run `cld restart lide-2600`"):
            start_ticket(self._cfg(tmp_path), "lide-2600", [], [])
        run.assert_not_called()

    def test_store_v1_label_warm_starts(self, tmp_path):
        with patch("cld.ticket._docker_status", return_value="stopped"), \
             patch("cld.ticket.read_manifest", return_value=_manifest()), \
             patch("cld.docker._image_label", return_value="store-v1"), \
             patch("cld.ticket.subprocess.run", return_value=_res()) as run, \
             patch("cld.ticket.wait_ticket_ready", return_value=True):
            start_ticket(self._cfg(tmp_path), "lide-2600", [], [])
        assert run.call_args.args[0] == ["docker", "start", "t.lide-2600"]

    def test_label_is_read_off_the_container(self, tmp_path):
        with patch("cld.ticket._docker_status", return_value="stopped"), \
             patch("cld.ticket.read_manifest", return_value=_manifest()), \
             patch("cld.ticket.container_has_store_mounts", return_value=False) as has, \
             patch("cld.ticket.subprocess.run"), \
             pytest.raises(RuntimeError):
            start_ticket(self._cfg(tmp_path), "lide-2600", [], [])
        has.assert_called_once_with("t.lide-2600")

    def test_running_container_without_label_warns_and_stays_running(self, tmp_path, caplog):
        with patch("cld.ticket._docker_status", return_value="running"), \
             patch("cld.ticket.container_has_store_mounts", return_value=False), \
             patch("cld.ticket.subprocess.run") as run, \
             caplog.at_level("WARNING", logger="cld.ticket"):
            start_ticket(self._cfg(tmp_path), "lide-2600", [], [])
        assert "cld restart lide-2600" in caplog.text
        run.assert_not_called()

    def test_running_container_with_label_does_not_warn(self, tmp_path, caplog):
        with patch("cld.ticket._docker_status", return_value="running"), \
             patch("cld.ticket.container_has_store_mounts", return_value=True), \
             caplog.at_level("WARNING", logger="cld.ticket"):
            start_ticket(self._cfg(tmp_path), "lide-2600", [], [])
        assert not caplog.text


class TestStartRepoSetChange:
    """Explicit new set against an existing ticket: diff, confirm, recreate
    (design section 2.3)."""

    def _cfg(self, tmp_path):
        keep = tmp_path / "lide-api"
        keep.mkdir()
        return Config(repos={"lide-api": RepoEntry(path=str(keep))})

    def _old(self, tmp_path):
        return TicketManifest(ticket="lide-2600", repos=(
            RepoManifestEntry(name="lide-api", path=str(tmp_path / "lide-api"),
                              anchor_base="b" * 40, rev_source="trunk"),
            RepoManifestEntry(name="leaving", path="/home/u/leaving", anchor_base="c" * 40),
        ))

    def _start(self, tmp_path, confirm):
        cfg = self._cfg(tmp_path)
        mocks = {}
        with patch("cld.ticket._docker_status", return_value="running"), \
             patch("cld.ticket.read_manifest", return_value=self._old(tmp_path)), \
             patch("cld.ticket.ticket_anchor_resolver", lambda cfg: lambda p, r, m: "b" * 40), \
             patch("cld.ticket.typer.confirm", return_value=confirm) as mocks["confirm"], \
             patch("cld.ticket._stop_and_remove") as mocks["stop"], \
             patch("cld.ticket.forget_session_state") as mocks["forget"], \
             patch("cld.ticket._launch") as mocks["launch"]:
            start_ticket(cfg, "lide-2600", ["lide-api"], [])
        return mocks

    def test_confirmed_change_tears_down_leaving_repos_and_relaunches(self, tmp_path, capsys):
        mocks = self._start(tmp_path, confirm=True)
        mocks["stop"].assert_called_once_with("t.lide-2600")
        # Only the repo leaving the set is torn down; the kept one reattaches.
        mocks["forget"].assert_called_once_with("/home/u/leaving", "t.lide-2600")
        new = mocks["launch"].call_args.args[1]
        assert [r.name for r in new.repos] == ["lide-api"]
        out = capsys.readouterr().out
        assert "- leaving" in out

    def test_declined_change_tears_nothing_down(self, tmp_path):
        with pytest.raises(typer.Abort):
            mocks = self._start(tmp_path, confirm=False)
            del mocks

    def test_declined_change_side_effects(self, tmp_path):
        cfg = self._cfg(tmp_path)
        with patch("cld.ticket._docker_status", return_value="running"), \
             patch("cld.ticket.read_manifest", return_value=self._old(tmp_path)), \
             patch("cld.ticket.ticket_anchor_resolver", lambda cfg: lambda p, r, m: "b" * 40), \
             patch("cld.ticket.typer.confirm", return_value=False), \
             patch("cld.ticket._stop_and_remove") as stop, \
             patch("cld.ticket.forget_session_state") as forget, \
             patch("cld.ticket._launch") as launch:
            with pytest.raises(typer.Abort):
                start_ticket(cfg, "lide-2600", ["lide-api"], [])
        stop.assert_not_called()
        forget.assert_not_called()
        launch.assert_not_called()

    def test_kept_repo_anchor_change_says_it_takes_effect_after_shutdown(self, tmp_path, capsys):
        """A kept repo reattaches at its existing bookmark, so the diff line
        for its changed anchor must say the change only lands after
        shutdown + start -- not imply the new anchor applies now."""
        cfg = self._cfg(tmp_path)
        with patch("cld.ticket._docker_status", return_value="running"), \
             patch("cld.ticket.read_manifest", return_value=self._old(tmp_path)), \
             patch("cld.ticket.ticket_anchor_resolver", lambda cfg: lambda p, r, m: "d" * 40), \
             patch("cld.ticket.typer.confirm", return_value=True), \
             patch("cld.ticket._stop_and_remove"), \
             patch("cld.ticket.forget_session_state"), \
             patch("cld.ticket._launch"):
            start_ticket(cfg, "lide-2600", ["lide-api"], [])
        out = capsys.readouterr().out
        assert f"~ lide-api  anchor {'b' * 12} -> {'d' * 12}" in out
        assert "takes effect only after shutdown + start" in out
        assert "reattaches at the existing bookmark" in out
        assert "cld restart <ticket> --reanchor lide-api@<rev>" in out

    def test_kept_repo_launches_with_its_old_anchor(self, tmp_path):
        """A kept repo reattaches at its existing bookmark (a child of the OLD
        base), so the labels stamped on the recreated container must keep the
        old anchor_base and mode -- recording the newly resolved ones would
        make the overlap check probe the wrong base and `cld status` lie."""
        cfg = self._cfg(tmp_path)
        with patch("cld.ticket._docker_status", return_value="running"), \
             patch("cld.ticket.read_manifest", return_value=self._old(tmp_path)), \
             patch("cld.ticket.ticket_anchor_resolver", lambda cfg: lambda p, r, m: "d" * 40), \
             patch("cld.ticket.typer.confirm", return_value=True), \
             patch("cld.ticket._stop_and_remove"), \
             patch("cld.ticket.forget_session_state"), \
             patch("cld.ticket._launch") as launch:
            start_ticket(cfg, "lide-2600", ["lide-api"], ["lide-api"])
        [repo] = launch.call_args.args[1].repos
        assert repo.anchor_base == "b" * 40
        assert repo.anchor_mode == "isolated"
        assert repo.rev_source == "trunk"

    def test_identical_resolved_set_skips_the_recreate(self, tmp_path, capsys):
        """Same specs re-given = plain create-or-start, no diff prompt."""
        cfg = self._cfg(tmp_path)
        old = TicketManifest(ticket="lide-2600", repos=(
            RepoManifestEntry(name="lide-api", path=str(tmp_path / "lide-api"),
                              anchor_base="b" * 40, rev_source="trunk"),
        ))
        with patch("cld.ticket._docker_status", return_value="running"), \
             patch("cld.ticket.read_manifest", return_value=old), \
             patch("cld.ticket.container_has_store_mounts", return_value=True), \
             patch("cld.ticket.ticket_anchor_resolver", lambda cfg: lambda p, r, m: "b" * 40), \
             patch("cld.ticket.typer.confirm") as confirm, \
             patch("cld.ticket._launch") as launch:
            start_ticket(cfg, "lide-2600", ["lide-api"], [])
        confirm.assert_not_called()
        launch.assert_not_called()
        assert "already running" in capsys.readouterr().out


@pytest.fixture
def stub_restart_preflight():
    """Restart on manifests of fake repo paths: skip the host preflight."""
    with patch("cld.ticket.prepare_store_mounts") as preflight:
        yield preflight


@pytest.mark.usefixtures("stub_restart_preflight")
class TestRestartTicket:
    def test_relaunches_from_labels_read_before_removal(self):
        """The manifest survives the recreate byte-for-byte: read from the real
        label encoding, and read BEFORE stop/rm (labels die with `docker rm`)."""
        manifest = _manifest()
        labels = manifest_labels(manifest, "t.lide-2600")
        order: list[str] = []
        cfg = Config()

        def fake_read(container):
            order.append("read")
            return TicketManifest.from_json(labels["org.cld.manifest"])

        with patch("cld.ticket._docker_status", return_value="stopped"), \
             patch("cld.ticket.read_manifest", side_effect=fake_read), \
             patch("cld.ticket._stop_and_remove", side_effect=lambda c: order.append("rm")), \
             patch("cld.ticket._launch", side_effect=lambda c, m: order.append("launch")) as launch:
            restart_ticket(cfg, "LIDE-2600")
        assert order == ["read", "rm", "launch"]
        assert launch.call_args.args[1] == manifest

    def test_absent_container_errors(self):
        with patch("cld.ticket._docker_status", return_value="absent"), \
             pytest.raises(RuntimeError, match="no ticket container"):
            restart_ticket(Config(), "lide-2600")


@pytest.mark.usefixtures("stub_restart_preflight")
class TestReanchorRestart:
    """``cld restart --reanchor`` (anchor-move design section 5)."""

    def _manifest(self, *, mode="isolated"):
        return TicketManifest(ticket="lide-2600", repos=(
            RepoManifestEntry(
                name="lide-api", path="/home/u/lide-api",
                anchor_base="a" * 40, anchor_mode=mode, rev_source="registry",
            ),
            RepoManifestEntry(
                name="diskuze-api", path="/home/u/diskuze-api",
                anchor_base="c" * 40, rev_source="registry",
            ),
        ))

    def _jj_backend(self, name="jj"):
        backend = MagicMock()
        backend.name = name
        return backend

    def _patches(self, manifest, *, status="running", session="none",
                 target="d" * 40, s_hash="s" * 40, resolver_calls=None,
                 move_side_effect=None, backend=None):
        backend = backend or self._jj_backend()
        resolver_calls = resolver_calls if resolver_calls is not None else []

        def fake_resolver(cfg, exclude_session=""):
            def resolve(path, revision, mode):
                resolver_calls.append((path, revision, mode, exclude_session))
                return target
            return resolve

        move = MagicMock(return_value=["op2", "op1"])
        if move_side_effect is not None:
            move.side_effect = move_side_effect

        return dict(
            docker_status=patch("cld.ticket._docker_status", return_value=status),
            read_manifest=patch("cld.ticket.read_manifest", return_value=manifest),
            session_state=patch("cld.ticket._session_state", return_value=session),
            get_backend=patch("cld.ticket.get_backend", return_value=backend),
            find_anchor=patch("cld.ticket.find_anchor_commit", return_value=s_hash),
            resolver=patch("cld.ticket.ticket_anchor_resolver", fake_resolver),
            move=patch("cld.ticket.move_anchor_commit", move),
            revert=patch("cld.ticket.revert_ops"),
            subprocess_run=patch("cld.ticket.subprocess.run", return_value=_res()),
            launch=patch("cld.ticket._launch"),
            warm_start=patch("cld.ticket._warm_start"),
        ), backend, move

    def test_no_reanchor_keeps_the_plain_order(self):
        manifest = self._manifest()
        order: list[str] = []
        with patch("cld.ticket._docker_status", return_value="stopped"), \
             patch("cld.ticket.read_manifest", return_value=manifest), \
             patch("cld.ticket._stop_and_remove", side_effect=lambda c: order.append("rm")), \
             patch("cld.ticket._launch", side_effect=lambda c, m: order.append("launch")):
            restart_ticket(Config(), "lide-2600")
        assert order == ["rm", "launch"]

    def test_running_ticket_stops_moves_removes_and_launches(self):
        manifest = self._manifest()
        calls: list[tuple] = []
        patches, backend, move = self._patches(manifest, resolver_calls=(rc := []))
        with patches["docker_status"], patches["read_manifest"], patches["session_state"], \
             patches["get_backend"], patches["find_anchor"], patches["resolver"], \
             patches["move"], patches["revert"], \
             patch("cld.ticket.subprocess.run", side_effect=lambda *a, **k: (calls.append(a[0]), _res())[1]), \
             patches["launch"] as launch, patches["warm_start"] as warm:
            restart_ticket(Config(), "lide-2600", ["lide-api@feature-x"])
        assert calls == [["docker", "stop", "t.lide-2600"], ["docker", "rm", "t.lide-2600"]]
        warm.assert_not_called()
        new_manifest = launch.call_args.args[1]
        by_name = {r.name: r for r in new_manifest.repos}
        assert by_name["lide-api"].anchor_base == "d" * 40
        assert by_name["lide-api"].rev_source == "arg"
        # Untouched repo keeps its old anchor.
        assert by_name["diskuze-api"].anchor_base == "c" * 40
        assert rc == [("/home/u/lide-api", "feature-x", "isolated", "t.lide-2600")]

    def test_stopped_ticket_moves_without_docker_stop_or_start(self):
        manifest = self._manifest()
        calls: list[tuple] = []
        patches, backend, move = self._patches(manifest, status="stopped")
        with patches["docker_status"], patches["read_manifest"], patches["session_state"], \
             patches["get_backend"], patches["find_anchor"], patches["resolver"], \
             patches["move"], patches["revert"], \
             patch("cld.ticket.subprocess.run", side_effect=lambda *a, **k: (calls.append(a[0]), _res())[1]), \
             patches["launch"], patches["warm_start"] as warm:
            restart_ticket(Config(), "lide-2600", ["lide-api@feature-x"])
        assert ["docker", "stop", "t.lide-2600"] not in calls
        assert calls == [["docker", "rm", "t.lide-2600"]]
        warm.assert_not_called()

    def test_live_session_is_refused(self):
        manifest = self._manifest()
        patches, backend, move = self._patches(manifest, session="live")
        with patches["docker_status"], patches["read_manifest"], patches["session_state"], \
             patches["get_backend"], patches["find_anchor"], patches["resolver"], \
             patches["move"] as move_mock, patches["subprocess_run"] as run, \
             patches["launch"] as launch, \
             pytest.raises(RuntimeError, match="live claude session"):
            restart_ticket(Config(), "lide-2600", ["lide-api@feature-x"])
        move_mock.assert_not_called()
        run.assert_not_called()
        launch.assert_not_called()

    def test_git_repo_goes_through_the_same_helpers(self):
        manifest = self._manifest()
        patches, backend, move = self._patches(manifest, backend=self._jj_backend("git"))
        with patches["docker_status"], patches["read_manifest"], patches["session_state"], \
             patches["get_backend"], patches["find_anchor"] as find, patches["resolver"], \
             patches["move"], patches["revert"], patches["subprocess_run"], \
             patches["launch"] as launch, patches["warm_start"]:
            restart_ticket(Config(), "lide-2600", ["lide-api@feature-x"])
        find.assert_called_once_with(backend, "t.lide-2600")
        move.assert_called_once_with(backend, "s" * 40, "d" * 40, "isolated", "t.lide-2600")
        assert launch.call_args.args[1].repos[0].anchor_base == "d" * 40

    def test_git_repo_without_an_anchor_ref_stops_nothing(self):
        manifest = self._manifest()
        patches, backend, move = self._patches(manifest, backend=self._jj_backend("git"))
        with patches["docker_status"], patches["read_manifest"], patches["session_state"], \
             patches["get_backend"], patch("cld.ticket.find_anchor_commit", side_effect=RuntimeError("no anchor ref")), \
             patches["resolver"], patches["move"] as move_mock, patches["subprocess_run"] as run, \
             pytest.raises(RuntimeError, match="no anchor ref"):
            restart_ticket(Config(), "lide-2600", ["lide-api@feature-x"])
        move_mock.assert_not_called()
        run.assert_not_called()

    def test_git_second_repo_failing_reverts_the_first_ref_entries(self):
        manifest = self._manifest()
        entries = ["refs/heads/t.lide-2600 " + "s" * 40 + " " + "n" * 40, "refs/cld/anchor/t.lide-2600 " + "s" * 40 + " " + "n" * 40]

        def move_side_effect(backend, s_hash, target, mode, session):
            if target == "d" * 40:
                return entries
            raise RuntimeError("git update-ref failed")

        def fake_resolver(cfg, exclude_session=""):
            def resolve(path, revision, mode):
                return "d" * 40 if "lide-api" in path else "e" * 40
            return resolve

        with patch("cld.ticket._docker_status", return_value="running"), \
             patch("cld.ticket.read_manifest", return_value=manifest), \
             patch("cld.ticket._session_state", return_value="none"), \
             patch("cld.ticket.get_backend", return_value=self._jj_backend("git")), \
             patch("cld.ticket.find_anchor_commit", return_value="s" * 40), \
             patch("cld.ticket.ticket_anchor_resolver", fake_resolver), \
             patch("cld.ticket.move_anchor_commit", side_effect=move_side_effect), \
             patch("cld.ticket.revert_ops") as revert, \
             patch("cld.ticket.subprocess.run", return_value=_res()), \
             patch("cld.ticket._launch") as launch, \
             patch("cld.ticket._warm_start") as warm, \
             pytest.raises(RuntimeError, match="reanchor failed"):
            restart_ticket(Config(), "lide-2600", ["lide-api@feature-x", "diskuze-api@feature-y"])
        revert.assert_called_once_with(revert.call_args.args[0], entries)
        warm.assert_called_once()
        launch.assert_not_called()

    def test_hint_for_a_branch_that_kept_its_work_is_printed(self, capsys):
        manifest = self._manifest()
        patches, backend, move = self._patches(manifest, backend=self._jj_backend("git"))
        with patches["docker_status"], patches["read_manifest"], patches["session_state"], \
             patches["get_backend"], patches["find_anchor"], patches["resolver"], \
             patches["move"], patches["revert"], patches["subprocess_run"], \
             patch("cld.ticket._rebase_hints", return_value=[
                 RebaseHint("git rebase --onto N S t.lide-2600", "branch t.lide-2600", True),
                 RebaseHint("git rebase --onto N S", "the detached HEAD", True),
                 RebaseHint("git rebase --onto N S t.lide-2600.mr", "branch t.lide-2600.mr", False),
             ]) as hints, \
             patches["launch"], patches["warm_start"]:
            restart_ticket(Config(), "lide-2600", ["lide-api@feature-x"])
        out = capsys.readouterr().out
        assert hints.call_args.args[1:] == ("t.lide-2600", "s" * 40, "/workspace/lide-2600/lide-api")
        assert "lide-api: branch t.lide-2600 keeps its work on the old anchor commit" in out
        assert "in the ticket worktree (`cld shell lide-2600`, then cd to lide-api): git rebase --onto N S t.lide-2600\n" in out
        assert "lide-api: the detached HEAD keeps its work" in out
        assert "in the repo, where the branch is checked out (if anywhere): git rebase --onto N S t.lide-2600.mr" in out

    def test_no_hint_line_for_jj(self, capsys):
        manifest = self._manifest()
        patches, backend, move = self._patches(manifest)
        with patches["docker_status"], patches["read_manifest"], patches["session_state"], \
             patches["get_backend"], patches["find_anchor"], patches["resolver"], \
             patches["move"], patches["revert"], patches["subprocess_run"], \
             patches["launch"], patches["warm_start"]:
            restart_ticket(Config(), "lide-2600", ["lide-api@feature-x"])
        assert "keeps its work" not in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("spec", "expect"),
        [
            ("lide-api", {"lide-api": "@"}),
            ("lide-api@", {"lide-api": "@"}),
            ("lide-api@main", {"lide-api": "main"}),
        ],
        ids=["bare-repo", "empty-rev", "explicit-rev"],
    )
    def test_revision_defaults_to_the_host_working_copy(self, spec, expect):
        assert _parse_reanchor(self._manifest(), [spec]) == expect

    def test_unknown_repo_is_a_parse_error(self):
        manifest = self._manifest()
        with patch("cld.ticket._docker_status", return_value="running"), \
             patch("cld.ticket.read_manifest", return_value=manifest), \
             pytest.raises(RuntimeError, match="not REPO@REV"):
            restart_ticket(Config(), "lide-2600", ["nope@feature-x"])

    def test_bare_revision_on_two_repo_ticket_is_a_parse_error(self):
        manifest = self._manifest()
        with patch("cld.ticket._docker_status", return_value="running"), \
             patch("cld.ticket.read_manifest", return_value=manifest), \
             pytest.raises(RuntimeError, match="not REPO@REV"):
            restart_ticket(Config(), "lide-2600", ["feature-x"])

    def test_repo_named_twice_is_a_parse_error(self):
        manifest = self._manifest()
        with patch("cld.ticket._docker_status", return_value="running"), \
             patch("cld.ticket.read_manifest", return_value=manifest), \
             pytest.raises(RuntimeError, match="named twice"):
            restart_ticket(Config(), "lide-2600", ["lide-api@x", "lide-api@y"])

    def test_bare_revision_on_one_repo_ticket_is_accepted(self):
        manifest = TicketManifest(ticket="lide-2600", repos=(
            RepoManifestEntry(name="lide-api", path="/home/u/lide-api", anchor_base="a" * 40),
        ))
        patches, backend, move = self._patches(manifest, resolver_calls=(rc := []))
        with patches["docker_status"], patches["read_manifest"], patches["session_state"], \
             patches["get_backend"], patches["find_anchor"], patches["resolver"], \
             patches["move"], patches["revert"], patches["subprocess_run"], \
             patches["launch"], patches["warm_start"]:
            restart_ticket(Config(), "lide-2600", ["feature-x"])
        assert rc == [("/home/u/lide-api", "feature-x", "isolated", "t.lide-2600")]

    def test_second_repo_failing_reverts_the_first_and_warm_starts(self):
        manifest = self._manifest()

        def move_side_effect(backend, s_hash, target, mode, session):
            if target == "d" * 40:
                return ["op2", "op1"]
            raise RuntimeError("reanchor introduced new conflicts: abc")

        def fake_resolver(cfg, exclude_session=""):
            def resolve(path, revision, mode):
                return "d" * 40 if "lide-api" in path else "e" * 40
            return resolve

        with patch("cld.ticket._docker_status", return_value="running"), \
             patch("cld.ticket.read_manifest", return_value=manifest), \
             patch("cld.ticket._session_state", return_value="none"), \
             patch("cld.ticket.get_backend", return_value=self._jj_backend()), \
             patch("cld.ticket.find_anchor_commit", return_value="s" * 40), \
             patch("cld.ticket.ticket_anchor_resolver", fake_resolver), \
             patch("cld.ticket.move_anchor_commit", side_effect=move_side_effect), \
             patch("cld.ticket.revert_ops") as revert, \
             patch("cld.ticket.subprocess.run", return_value=_res()) as run, \
             patch("cld.ticket._launch") as launch, \
             patch("cld.ticket._warm_start") as warm, \
             pytest.raises(RuntimeError, match="reanchor failed"):
            restart_ticket(Config(), "lide-2600", ["lide-api@feature-x", "diskuze-api@feature-y"])
        # Only the successfully-moved repo's ops get reverted.
        revert.assert_called_once_with(revert.call_args.args[0], ["op2", "op1"])
        warm.assert_called_once()
        launch.assert_not_called()
        assert ["docker", "rm", "t.lide-2600"] not in [c.args[0] for c in run.call_args_list]

    def test_second_repo_failing_on_a_stopped_ticket_does_not_warm_start(self):
        manifest = self._manifest()

        def move_side_effect(backend, s_hash, target, mode, session):
            if target == "d" * 40:
                return ["op2", "op1"]
            raise RuntimeError("reanchor introduced new conflicts: abc")

        def fake_resolver(cfg, exclude_session=""):
            def resolve(path, revision, mode):
                return "d" * 40 if "lide-api" in path else "e" * 40
            return resolve

        with patch("cld.ticket._docker_status", return_value="stopped"), \
             patch("cld.ticket.read_manifest", return_value=manifest), \
             patch("cld.ticket._session_state", return_value="none"), \
             patch("cld.ticket.get_backend", return_value=self._jj_backend()), \
             patch("cld.ticket.find_anchor_commit", return_value="s" * 40), \
             patch("cld.ticket.ticket_anchor_resolver", fake_resolver), \
             patch("cld.ticket.move_anchor_commit", side_effect=move_side_effect), \
             patch("cld.ticket.revert_ops"), \
             patch("cld.ticket.subprocess.run", return_value=_res()) as run, \
             patch("cld.ticket._launch") as launch, \
             patch("cld.ticket._warm_start") as warm, \
             pytest.raises(RuntimeError, match="reanchor failed"):
            restart_ticket(Config(), "lide-2600", ["lide-api@feature-x", "diskuze-api@feature-y"])
        warm.assert_not_called()
        launch.assert_not_called()
        assert ["docker", "stop", "t.lide-2600"] not in [c.args[0] for c in run.call_args_list]

    def test_exclude_session_reaches_the_resolver(self):
        manifest = self._manifest()
        patches, backend, move = self._patches(manifest, resolver_calls=(rc := []))
        with patches["docker_status"], patches["read_manifest"], patches["session_state"], \
             patches["get_backend"], patches["find_anchor"], patches["resolver"], \
             patches["move"], patches["revert"], patches["subprocess_run"], \
             patches["launch"], patches["warm_start"]:
            restart_ticket(Config(), "lide-2600", ["lide-api@feature-x"])
        assert rc[0][3] == "t.lide-2600"


class TestRebaseHints:
    """The hints name every git ref that kept its work on an old scratch commit after a reanchor:
    real repos, real worktree registration, the real move."""

    SESSION = "t.x"

    @pytest.fixture
    def lifecycle(self, git_repo, tmp_path):
        """A locked ticket worktree on the session branch at its scratch commit S, plus a host
        commit T to move the anchor onto. Returns (workspace, S, T)."""
        workspace = tmp_path.resolve() / "ws"
        base = git_repo.run(["rev-parse", "HEAD"]).stdout.strip()
        git_repo.run(["worktree", "add", "--lock", "--reason", self.SESSION, "--no-track", "-b", self.SESSION,
                      str(workspace), base])
        old = stage_in_workspace(workspace, self.SESSION, {})
        git_repo.run(["commit", "-q", "--allow-empty", "-m", "target"])
        return workspace, old, git_repo.run(["rev-parse", "HEAD"]).stdout.strip()

    def _commit(self, git_repo, workspace: Path, message: str) -> str:
        git_repo.run(["-C", str(workspace), "commit", "-q", "--allow-empty", "-m", message])
        return git_repo.run(["-C", str(workspace), "rev-parse", "HEAD"]).stdout.strip()

    def _move(self, git_repo, old: str, target: str) -> str:
        move_anchor_commit(git_repo, old, target, "isolated", self.SESSION)
        return git_repo.run(["rev-parse", f"refs/cld/anchor/{self.SESSION}"]).stdout.strip()

    def _hints(self, git_repo, old: str, workspace: Path):
        return _rebase_hints(git_repo, self.SESSION, old, str(workspace))

    def test_a_session_branch_that_followed_needs_none(self, git_repo, lifecycle):
        workspace, old, target = lifecycle
        self._move(git_repo, old, target)
        assert self._hints(git_repo, old, workspace) == []

    def test_a_session_branch_with_work_gets_the_rebase_command(self, git_repo, lifecycle):
        workspace, old, target = lifecycle
        self._commit(git_repo, workspace, "work")
        new = self._move(git_repo, old, target)
        assert self._hints(git_repo, old, workspace) == [
            RebaseHint(f"git rebase --onto {new} {old} {self.SESSION}", f"branch {self.SESSION}", True),
        ]

    def test_another_checked_out_branch_is_found_while_the_session_branch_follows(self, git_repo, lifecycle):
        """The session branch sat on S, so it moved; the work is on a branch the agent switched to."""
        workspace, old, target = lifecycle
        git_repo.run(["-C", str(workspace), "checkout", "-q", "-b", f"{self.SESSION}.mr"])
        self._commit(git_repo, workspace, "work")
        new = self._move(git_repo, old, target)
        assert git_repo.run(["rev-parse", f"refs/heads/{self.SESSION}"]).stdout.strip() == new
        assert self._hints(git_repo, old, workspace) == [
            RebaseHint(f"git rebase --onto {new} {old} {self.SESSION}.mr", f"branch {self.SESSION}.mr", True),
        ]

    def test_a_branch_checked_out_nowhere_is_not_a_worktree_command(self, git_repo, lifecycle):
        workspace, old, target = lifecycle
        git_repo.run(["-C", str(workspace), "checkout", "-q", "-b", f"{self.SESSION}.mr"])
        self._commit(git_repo, workspace, "work")
        git_repo.run(["-C", str(workspace), "checkout", "-q", self.SESSION])
        new = self._move(git_repo, old, target)
        assert self._hints(git_repo, old, workspace) == [
            RebaseHint(f"git rebase --onto {new} {old} {self.SESSION}.mr", f"branch {self.SESSION}.mr", False),
        ]

    def test_a_detached_head_gets_a_rebase_without_a_branch(self, git_repo, lifecycle):
        workspace, old, target = lifecycle
        git_repo.run(["-C", str(workspace), "checkout", "-q", "--detach"])
        self._commit(git_repo, workspace, "detached work")
        new = self._move(git_repo, old, target)
        assert self._hints(git_repo, old, workspace) == [
            RebaseHint(f"git rebase --onto {new} {old}", "the detached HEAD", True),
        ]

    def test_a_detached_head_on_the_scratch_commit_itself_is_found(self, git_repo, lifecycle):
        workspace, old, target = lifecycle
        git_repo.run(["-C", str(workspace), "checkout", "-q", "--detach"])
        new = self._move(git_repo, old, target)
        assert self._hints(git_repo, old, workspace) == [
            RebaseHint(f"git rebase --onto {new} {old}", "the detached HEAD", True),
        ]

    def test_a_detached_head_already_on_the_new_anchor_needs_none(self, git_repo, lifecycle):
        workspace, old, target = lifecycle
        new = self._move(git_repo, old, target)
        git_repo.run(["-C", str(workspace), "checkout", "-q", "--detach", new])
        assert self._hints(git_repo, old, workspace) == []

    def test_work_stranded_by_an_earlier_move_is_found_after_the_undo(self, git_repo, lifecycle, monkeypatch):
        """reanchor, the work stays on S, then the printed undo (a second reanchor to the old base)
        moves the anchor again: the hint names the scratch commit the work really sits on."""
        workspace, old, target = lifecycle
        base = git_repo.run(["rev-parse", f"{old}^"]).stdout.strip()
        git_repo.run(["-C", str(workspace), "checkout", "-q", "-b", f"{self.SESSION}.mr"])
        self._commit(git_repo, workspace, "work")
        first = self._move(git_repo, old, target)
        # Within the same second the undo would recreate the very same commit as S.
        monkeypatch.setenv("GIT_COMMITTER_DATE", "2001-01-01T00:00:00Z")
        undone = self._move(git_repo, first, base)
        assert undone != old
        assert git_repo.run(["rev-parse", f"{undone}^"]).stdout.strip() == base
        assert self._hints(git_repo, first, workspace) == [
            RebaseHint(f"git rebase --onto {undone} {old} {self.SESSION}.mr", f"branch {self.SESSION}.mr", True),
        ]

    def test_no_registration_leaves_only_branches(self, git_repo, lifecycle):
        workspace, old, target = lifecycle
        self._commit(git_repo, workspace, "work")
        new = self._move(git_repo, old, target)
        assert _rebase_hints(git_repo, self.SESSION, old, "/workspace/elsewhere") == [
            RebaseHint(f"git rebase --onto {new} {old} {self.SESSION}", f"branch {self.SESSION}", False),
        ]

    def test_a_missing_anchor_ref_needs_none(self, git_repo, lifecycle):
        workspace, old, _target = lifecycle
        git_repo.run(["update-ref", "-d", f"refs/cld/anchor/{self.SESSION}"])
        assert self._hints(git_repo, old, workspace) == []

    def test_jj_needs_none(self):
        backend = MagicMock()
        backend.name = "jj"
        assert _rebase_hints(backend, self.SESSION, "s" * 40, "/workspace/x/api") == []
        backend.run.assert_not_called()


class TestStopTicket:
    def test_running_is_stopped(self, capsys):
        with patch("cld.ticket._docker_status", return_value="running"), \
             patch("cld.ticket.subprocess.run", return_value=_res()) as run:
            stop_ticket("lide-2600")
        assert run.call_args.args[0] == ["docker", "stop", "t.lide-2600"]
        assert "paused" in capsys.readouterr().out

    def test_stopped_is_a_noop(self, capsys):
        with patch("cld.ticket._docker_status", return_value="stopped"), \
             patch("cld.ticket.subprocess.run") as run:
            stop_ticket("lide-2600")
        run.assert_not_called()
        assert "already stopped" in capsys.readouterr().out

    def test_absent_errors(self):
        with patch("cld.ticket._docker_status", return_value="absent"), \
             pytest.raises(RuntimeError, match="no ticket container"):
            stop_ticket("lide-2600")


class TestShutdownTicket:
    def test_forgets_bookmark_and_workspace_per_repo(self):
        manifest = _manifest()
        with patch("cld.ticket._docker_status", return_value="running"), \
             patch("cld.ticket.read_manifest", return_value=manifest), \
             patch("cld.ticket._stop_and_remove") as stop, \
             patch("cld.ticket.forget_session_state") as forget:
            shutdown_ticket("lide-2600")
        stop.assert_called_once_with("t.lide-2600")
        assert forget.call_args_list == [
            (("/home/u/lide-api", "t.lide-2600"),),
            (("/home/u/diskuze-api", "t.lide-2600"),),
        ]

    def test_unreadable_manifest_still_removes_the_container(self, caplog):
        with patch("cld.ticket._docker_status", return_value="stopped"), \
             patch("cld.ticket.read_manifest", side_effect=RuntimeError("no label")), \
             patch("cld.ticket._stop_and_remove") as stop, \
             patch("cld.ticket.forget_session_state") as forget, \
             caplog.at_level("WARNING"):
            shutdown_ticket("lide-2600")
        stop.assert_called_once()
        forget.assert_not_called()
        assert "manually" in caplog.text

    def test_absent_is_a_polite_noop(self, capsys):
        with patch("cld.ticket._docker_status", return_value="absent"), \
             patch("cld.ticket._stop_and_remove") as stop:
            shutdown_ticket("lide-2600")
        stop.assert_not_called()
        assert "No ticket container" in capsys.readouterr().out

    def test_shutdown_all_walks_every_ticket(self):
        with patch("cld.ticket.list_tickets",
                   return_value=[("t.b", "exited"), ("t.a", "running")]), \
             patch("cld.ticket._teardown") as teardown:
            shutdown_all_tickets()
        assert [c.args[0] for c in teardown.call_args_list] == ["t.a", "t.b"]


class TestForgetSessionState:
    """The per-repo forget shared by ticket teardown and the task-agent reap.

    Dropping the session bookmark AND the workspace registration is what makes
    the next launch under that name a fresh lifecycle; every failure mode is
    non-fatal, because teardown must finish regardless.
    """

    def test_jj_forgets_bookmark_and_workspace(self, tmp_path):
        backend = MagicMock()
        backend.name = "jj"
        backend.run.return_value = MagicMock(returncode=0, stderr="")
        with patch("cld.ticket.get_backend", return_value=backend):
            forget_session_state(str(tmp_path), "t.x")
        calls = [c.args[0] for c in backend.run.call_args_list]
        assert calls == [
            ["bookmark", "forget", "t.x"],
            ["workspace", "forget", "t.x"],
        ]

    def test_git_without_a_worktree_path_is_left_alone(self, tmp_path):
        """The task-agent callers pass none."""
        backend = MagicMock()
        backend.name = "git"
        with patch("cld.ticket.get_backend", return_value=backend):
            forget_session_state(str(tmp_path), "a.r.x")
        backend.run.assert_not_called()

    def test_forget_failure_is_non_fatal(self, tmp_path):
        backend = MagicMock()
        backend.name = "jj"
        backend.run.return_value = MagicMock(returncode=1, stderr="conflict")
        with patch("cld.ticket.get_backend", return_value=backend):
            forget_session_state(str(tmp_path), "a.r.x")
        assert backend.run.call_count == 2

    def test_missing_repo_root_never_touches_the_backend(self, tmp_path):
        with patch("cld.ticket.get_backend") as get_backend_mock:
            forget_session_state(str(tmp_path / "gone"), "a.r.x")
        get_backend_mock.assert_not_called()

    def test_get_backend_failure_is_non_fatal(self, tmp_path):
        with patch("cld.ticket.get_backend", side_effect=RuntimeError("no vcs")):
            forget_session_state(str(tmp_path), "a.r.x")


class TestForgetGitSessionState:
    """Ticket shutdown on git: drop the worktree registration, archive the work, delete the
    branch, and the anchor ref last (it is the lifecycle-alive marker)."""

    SESSION = "t.x"

    def _lifecycle(self, git_repo, tmp_path, work: bool) -> str:
        """Boot-like state: a locked worktree on the session branch with a pinned scratch commit,
        optionally one commit of work on top, and the worktree directory lost. Returns its path."""
        workspace = tmp_path.resolve() / "ws"
        base = git_repo.run(["rev-parse", "HEAD"]).stdout.strip()
        git_repo.run(["worktree", "add", "--lock", "--reason", self.SESSION, "--no-track", "-b", self.SESSION,
                      str(workspace), base])
        stage_in_workspace(workspace, self.SESSION, {})
        if work:
            (workspace / "work.txt").write_text("work\n")
            git_repo.run(["-C", str(workspace), "add", "work.txt"])
            git_repo.run(["-C", str(workspace), "commit", "-q", "-m", "work"])
        shutil.rmtree(workspace)
        return str(workspace)

    def _refs(self, git_repo) -> dict[str, str]:
        out = git_repo.run(["for-each-ref", "--format=%(refname) %(objectname)"]).stdout.splitlines()
        return dict(line.split() for line in out)

    def test_work_is_archived_before_the_branch_is_deleted(self, git_repo, tmp_path):
        workspace = self._lifecycle(git_repo, tmp_path, work=True)
        tip = git_repo.run(["rev-parse", f"refs/heads/{self.SESSION}"]).stdout.strip()
        forget_session_state(str(git_repo.repo_root), self.SESSION, workspace)
        refs = self._refs(git_repo)
        archived = [ref for ref in refs if ref.startswith(f"refs/cld/ended/{self.SESSION}/")]
        assert len(archived) == 1
        assert refs[archived[0]] == tip
        assert f"refs/heads/{self.SESSION}" not in refs
        assert f"refs/cld/anchor/{self.SESSION}" not in refs
        assert workspace not in git_repo.run(["worktree", "list", "--porcelain"]).stdout

    def test_a_branch_still_at_the_scratch_commit_is_just_deleted(self, git_repo, tmp_path):
        workspace = self._lifecycle(git_repo, tmp_path, work=False)
        forget_session_state(str(git_repo.repo_root), self.SESSION, workspace)
        assert not [ref for ref in self._refs(git_repo) if ref.startswith("refs/cld/")]
        assert f"refs/heads/{self.SESSION}" not in self._refs(git_repo)

    def _detach_with_commit(self, git_repo, workspace: str, tmp_path) -> str:
        """Recreate the worktree dir (the lost registration's path), detach HEAD and commit there."""
        git_repo.run(["worktree", "unlock", workspace])
        git_repo.run(["worktree", "remove", "--force", workspace])
        git_repo.run(["worktree", "add", "--lock", "--reason", self.SESSION, "--detach", workspace,
                      f"refs/heads/{self.SESSION}"])
        git_repo.run(["-C", workspace, "commit", "-q", "--allow-empty", "-m", "detached work"])
        head = git_repo.run(["-C", workspace, "rev-parse", "HEAD"]).stdout.strip()
        shutil.rmtree(workspace)
        return head

    def _archived(self, git_repo, suffix: str = "") -> dict[str, str]:
        prefix = f"refs/cld/ended/{self.SESSION}/"
        return {ref: sha for ref, sha in self._refs(git_repo).items()
                if ref.startswith(prefix) and ref.endswith(suffix)}

    def test_detached_work_is_archived_next_to_the_branch_tip(self, git_repo, tmp_path):
        workspace = self._lifecycle(git_repo, tmp_path, work=True)
        tip = git_repo.run(["rev-parse", f"refs/heads/{self.SESSION}"]).stdout.strip()
        head = self._detach_with_commit(git_repo, workspace, tmp_path)
        forget_session_state(str(git_repo.repo_root), self.SESSION, workspace)
        archived = self._archived(git_repo)
        tip_ref = next(ref for ref, sha in archived.items() if sha == tip)
        assert archived == {tip_ref: tip, f"{tip_ref}-detached": head}
        assert f"refs/heads/{self.SESSION}" not in self._refs(git_repo)
        assert f"refs/cld/anchor/{self.SESSION}" not in self._refs(git_repo)

    def test_detached_work_is_archived_when_the_branch_holds_nothing_more(self, git_repo, tmp_path):
        workspace = self._lifecycle(git_repo, tmp_path, work=False)
        head = self._detach_with_commit(git_repo, workspace, tmp_path)
        forget_session_state(str(git_repo.repo_root), self.SESSION, workspace)
        assert list(self._archived(git_repo).values()) == [head]

    @pytest.mark.parametrize("work", [False, True], ids=["at-the-scratch-commit", "on-the-branch-tip"])
    def test_a_detached_head_already_reachable_is_not_archived_again(self, git_repo, tmp_path, work):
        workspace = self._lifecycle(git_repo, tmp_path, work=work)
        git_repo.run(["worktree", "unlock", workspace])
        git_repo.run(["worktree", "remove", "--force", workspace])
        git_repo.run(["worktree", "add", "--lock", "--reason", self.SESSION, "--detach", workspace,
                      f"refs/heads/{self.SESSION}"])
        shutil.rmtree(workspace)
        forget_session_state(str(git_repo.repo_root), self.SESSION, workspace)
        assert not self._archived(git_repo, "-detached")
        assert len(self._archived(git_repo)) == (1 if work else 0)

    @pytest.mark.parametrize("detached", [False, True], ids=["branch-tip", "detached-head"])
    def test_an_existing_archive_is_never_overwritten(self, git_repo, tmp_path, caplog, detached):
        """Same stamp, e.g. two shutdowns within a second: the first archive stays and the
        shutdown stops before it drops the branch or the anchor ref."""
        workspace = self._lifecycle(git_repo, tmp_path, work=True)
        tip = git_repo.run(["rev-parse", f"refs/heads/{self.SESSION}"]).stdout.strip()
        if detached:
            self._detach_with_commit(git_repo, workspace, tmp_path)
        stamp = "20260101T000000Z"
        taken = f"refs/cld/ended/{self.SESSION}/{stamp}" + ("-detached" if detached else "")
        git_repo.run(["update-ref", taken, git_repo.run(["rev-parse", "HEAD"]).stdout.strip()])
        before = self._refs(git_repo)[taken]
        with patch("cld.ticket.datetime") as clock, caplog.at_level("WARNING"):
            clock.now.return_value = datetime(2026, 1, 1, tzinfo=UTC)
            forget_session_state(str(git_repo.repo_root), self.SESSION, workspace)
        refs = self._refs(git_repo)
        assert refs[taken] == before != tip
        assert f"refs/cld/anchor/{self.SESSION}" in refs
        assert "update-ref refs/cld/ended" in caplog.text

    def test_a_branch_checkout_is_not_archived_as_detached(self, git_repo, tmp_path):
        workspace = self._lifecycle(git_repo, tmp_path, work=True)
        forget_session_state(str(git_repo.repo_root), self.SESSION, workspace)
        assert not self._archived(git_repo, "-detached")

    def test_a_failed_detached_archive_keeps_the_registration(self, tmp_path, caplog):
        backend = self._scripted_backend(detached=True, fail="update-ref refs/cld/ended")
        with patch("cld.ticket.get_backend", return_value=backend), caplog.at_level("WARNING"):
            forget_session_state(str(tmp_path), self.SESSION, "/workspace/x/api")
        assert not [c for c in self._commands(backend) if c.startswith(("worktree remove", "branch -D"))]
        assert "recover with: cd /repo && git update-ref refs/cld/ended/t.x/" in caplog.text

    def test_a_missing_ref_and_registration_are_tolerated(self, git_repo, tmp_path):
        git_repo.run(["branch", self.SESSION])
        forget_session_state(str(git_repo.repo_root), self.SESSION, str(tmp_path / "never-registered"))
        assert f"refs/heads/{self.SESSION}" not in self._refs(git_repo)

    def test_other_tickets_are_untouched(self, git_repo, tmp_path):
        workspace = self._lifecycle(git_repo, tmp_path, work=True)
        git_repo.run(["update-ref", "refs/cld/anchor/t.other", "HEAD"])
        git_repo.run(["branch", "t.other"])
        forget_session_state(str(git_repo.repo_root), self.SESSION, workspace)
        refs = self._refs(git_repo)
        assert "refs/cld/anchor/t.other" in refs
        assert "refs/heads/t.other" in refs

    @staticmethod
    def _plant_worktree_hook(git_repo, tmp_path) -> Path:
        """The container-side attack: with the extension on, a config.worktree sets core.hooksPath
        to a folder holding a reference-transaction hook. Returns the marker the hook creates."""
        marker = tmp_path / "hook-ran"
        hooks = tmp_path / "evil-hooks"
        hooks.mkdir()
        hook = hooks / "reference-transaction"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
        git_repo.run(["config", "extensions.worktreeConfig", "true"])
        (git_repo.repo_root / ".git/config.worktree").write_text(f"[core]\n\thooksPath = {hooks}\n")
        return marker

    def test_the_planted_hook_works_on_a_plain_host_git(self, git_repo, tmp_path):
        """Guards the two tests below: without them this attack really runs."""
        marker = self._plant_worktree_hook(git_repo, tmp_path)
        git_repo.run(["update-ref", "refs/cld/probe", "HEAD"])
        assert marker.exists()

    def test_a_store_with_worktree_config_is_not_touched_on_shutdown(self, git_repo, tmp_path, caplog):
        workspace = self._lifecycle(git_repo, tmp_path, work=True)
        marker = self._plant_worktree_hook(git_repo, tmp_path)
        before = self._refs(git_repo)
        with caplog.at_level("WARNING"):
            forget_session_state(str(git_repo.repo_root), self.SESSION, workspace)
        assert not marker.exists()
        assert self._refs(git_repo) == before
        assert "extensions.worktreeConfig" in caplog.text
        assert "no host git runs there" in caplog.text

    def test_no_hook_runs_even_when_the_check_lets_a_store_through(self, git_repo, tmp_path):
        """Defense in depth: every ref-changing command of the shutdown disables hooks."""
        workspace = self._lifecycle(git_repo, tmp_path, work=True)
        marker = self._plant_worktree_hook(git_repo, tmp_path)
        with patch("cld.ticket.check_git_store", return_value=None):
            forget_session_state(str(git_repo.repo_root), self.SESSION, workspace)
        assert not marker.exists()
        assert f"refs/heads/{self.SESSION}" not in self._refs(git_repo)
        assert [ref for ref in self._refs(git_repo) if ref.startswith(f"refs/cld/ended/{self.SESSION}/")]

    def test_a_branch_lookup_in_such_a_store_runs_no_git(self, git_repo, tmp_path, caplog):
        self._plant_worktree_hook(git_repo, tmp_path)
        with patch.object(git_repo.__class__, "run") as run, caplog.at_level("WARNING"):
            assert _bookmark_tip(str(git_repo.repo_root), self.SESSION) == "-"
        run.assert_not_called()
        assert "extensions.worktreeConfig" in caplog.text

    @staticmethod
    def _scripted_backend(fail: str = "", detached: bool = False) -> MagicMock:
        """A git backend whose rev-parse finds a branch tip above the scratch commit and which
        fails the command that starts with *fail*. With *detached*, the worktree's HEAD is
        detached on a commit neither the branch nor the anchor holds."""
        backend = MagicMock()
        backend.name = "git"
        backend.repo_root = Path("/repo")

        def run(cmd):
            cmd = cmd[len(NO_HOOKS):] if cmd[:len(NO_HOOKS)] == list(NO_HOOKS) else cmd
            out = ""
            if cmd[0] == "worktree" and cmd[1] == "list":
                out = "worktree /workspace/x/api\n" + ("HEAD " + "c" * 40 + "\ndetached\n" if detached else "")
            elif cmd[0] == "rev-parse":
                out = "a" * 40 if "refs/heads/t.x^{commit}" in cmd else "b" * 40
            if cmd[0] == "merge-base":
                return MagicMock(returncode=1, stdout="", stderr="")
            return MagicMock(returncode=1 if fail and " ".join(cmd).startswith(fail) else 0, stdout=out, stderr="boom")

        backend.run.side_effect = run
        return backend

    def _commands(self, backend: MagicMock) -> list[str]:
        """The commands run, without the hooks-off prefix, minus the read-only rev-parse calls."""
        return [" ".join(c.args[0]).removeprefix(" ".join(NO_HOOKS) + " ")
                for c in backend.run.call_args_list if c.args[0][0] != "rev-parse"]

    def test_every_ref_changing_command_runs_with_hooks_off(self, tmp_path):
        backend = self._scripted_backend()
        with patch("cld.ticket.get_backend", return_value=backend):
            forget_session_state(str(tmp_path), self.SESSION, "/workspace/x/api")
        changing = [c.args[0] for c in backend.run.call_args_list
                    if c.args[0][0] in ("update-ref", "branch", "worktree", "-c") and c.args[0][1] != "list"]
        assert len(changing) == 5
        assert all(cmd[:len(NO_HOOKS)] == list(NO_HOOKS) for cmd in changing)

    def test_the_anchor_ref_goes_last(self, tmp_path):
        backend = self._scripted_backend()
        with patch("cld.ticket.get_backend", return_value=backend):
            forget_session_state(str(tmp_path), self.SESSION, "/workspace/x/api")
        prefixes = [
            "worktree list", "worktree unlock", "worktree remove", "update-ref refs/cld/ended/t.x/",
            "branch -D t.x", "update-ref -d refs/cld/anchor/t.x",
        ]
        commands = self._commands(backend)
        assert len(commands) == len(prefixes)
        assert all(command.startswith(prefix) for command, prefix in zip(commands, prefixes))

    @pytest.mark.parametrize("fail", ["worktree remove", "update-ref refs/cld/ended", "branch -D"])
    def test_a_failure_keeps_the_anchor_ref_and_names_the_command(self, tmp_path, caplog, fail):
        backend = self._scripted_backend(fail)
        with patch("cld.ticket.get_backend", return_value=backend), caplog.at_level("WARNING"):
            forget_session_state(str(tmp_path), self.SESSION, "/workspace/x/api")
        assert "update-ref -d refs/cld/anchor/t.x" not in self._commands(backend)
        assert f"recover with: cd /repo && git {fail}" in caplog.text


class TestExecVerbs:
    def test_claude_execs_the_wrapper_at_the_ticket_root(self):
        """`-it` matters: the wrapper's single-session flock refusal prints on
        the caller's own tty, so the host side needs no probe of its own."""
        with patch("cld.ticket._docker_status", return_value="running"), \
             patch("cld.ticket.os.execvp") as execvp:
            exec_claude("LIDE-2600", ["--model", "opus"])
        execvp.assert_called_once_with("docker", [
            "docker", "exec", "-it", "-w", "/workspace/lide-2600",
            "t.lide-2600", "/tmp/bin/claude", "--model", "opus",
        ])

    def test_claude_on_a_stopped_ticket_names_the_fix(self):
        with patch("cld.ticket._docker_status", return_value="stopped"), \
             patch("cld.ticket.os.execvp") as execvp, \
             pytest.raises(RuntimeError, match="cld start lide-2600"):
            exec_claude("lide-2600", [])
        execvp.assert_not_called()

    def test_shell_execs_a_login_bash(self):
        with patch("cld.ticket._docker_status", return_value="running"), \
             patch("cld.ticket.os.execvp") as execvp:
            exec_shell("lide-2600")
        args = execvp.call_args.args[1]
        assert args[-3] == "/bin/bash"
        assert "-it" in args
        # Sources the per-repo *_ANCHOR env file before handing off to the
        # login shell (docker exec doesn't inherit runtime-exported env).
        assert "cld-ticket-anchors.sh" in args[-1]
        assert args[-1].endswith("exec /bin/bash -l")


class TestSessionState:
    def test_free_lock_means_no_session(self):
        with patch("cld.ticket.subprocess.run", return_value=_res(0)):
            assert _session_state("c") == "none"

    def test_held_lock_means_live(self):
        with patch("cld.ticket.subprocess.run", return_value=_res(1)):
            assert _session_state("c") == "live"

    def test_exec_failure_is_unknown_not_live(self):
        with patch("cld.ticket.subprocess.run", return_value=_res(126)):
            assert _session_state("c") == "?"


class TestStatus:
    def test_roster_from_fake_inspect_output(self, capsys):
        """End to end through the real manifest reader against fake `docker
        inspect` label output."""
        manifest = _manifest()
        labels = manifest_labels(manifest, "t.lide-2600")

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["docker", "inspect"]:
                return _res(0, out=json.dumps(labels))
            raise AssertionError(f"unexpected {cmd}")

        with patch("cld.ticket.list_tickets",
                   return_value=[("t.lide-2600", "running")]), \
             patch("cld.ticket._session_state", return_value="live"), \
             patch("cld.manifest.subprocess.run", side_effect=fake_run):
            print_ticket_roster()
        out = capsys.readouterr().out
        assert "lide-2600" in out
        assert "running" in out
        assert "live" in out
        assert "lide-api, diskuze-api" in out

    def test_roster_empty(self, capsys):
        with patch("cld.ticket.list_tickets", return_value=[]):
            print_ticket_roster()
        assert "No ticket containers" in capsys.readouterr().out

    def test_roster_unreadable_manifest_row_survives(self, capsys):
        with patch("cld.ticket.list_tickets", return_value=[("t.x", "exited")]), \
             patch("cld.ticket.read_manifest", side_effect=RuntimeError("gone")):
            print_ticket_roster()
        out = capsys.readouterr().out
        assert "unreadable manifest" in out

    def test_detail_lists_per_repo_anchor_mode_and_tip(self, capsys):
        manifest = _manifest()
        with patch("cld.ticket._docker_status", return_value="running"), \
             patch("cld.ticket.read_manifest", return_value=manifest), \
             patch("cld.ticket._session_state", return_value="none"), \
             patch("cld.ticket._uptime", return_value="3h ago"), \
             patch("cld.ticket._bookmark_tip", return_value="deadbeef1234"):
            print_ticket_detail("LIDE-2600")
        out = capsys.readouterr().out
        assert "Ticket: lide-2600 (t.lide-2600)" in out
        assert "running (started 3h ago)" in out
        assert "aaaaaaaaaaaa" in out            # anchor short-hash
        assert "isolated" in out
        assert "deadbeef1234" in out            # bookmark tip
        assert "/home/u/lide-api" in out

    def test_detail_absent_errors(self):
        with patch("cld.ticket._docker_status", return_value="absent"), \
             pytest.raises(RuntimeError, match="cld start lide-2600"):
            print_ticket_detail("lide-2600")

    def test_list_tickets_parses_docker_ps(self):
        with patch("cld.ticket.subprocess.run",
                   return_value=_res(0, out="t.a\trunning\nt.b\texited\n")):
            assert list_tickets() == [("t.a", "running"), ("t.b", "exited")]

    def test_list_tickets_docker_failure_reads_as_none(self):
        with patch("cld.ticket.subprocess.run", return_value=_res(1)):
            assert list_tickets() == []


class TestGitBackendWording:
    """A git repo has a session branch and a worktree, no bookmark and workspace: tips and
    messages say so."""

    def test_bookmark_tip_is_the_session_branch_tip(self, git_repo):
        git_repo.create_branch("t.lide-2600")
        tip = git_repo.run(["rev-parse", "--short=12", "refs/heads/t.lide-2600"]).stdout.strip()
        assert _bookmark_tip(str(git_repo.repo_root), "t.lide-2600") == tip
        assert len(tip) == 12

    def test_bookmark_tip_is_a_dash_without_the_branch(self, git_repo):
        assert _bookmark_tip(str(git_repo.repo_root), "t.lide-2600") == "-"

    def test_manifest_diff_names_the_branch_not_a_bookmark(self, git_repo, capsys):
        entry = RepoManifestEntry(name="gone", path=str(git_repo.repo_root), anchor_base="c" * 40)
        _print_manifest_diff(MagicMock(added=(), removed=(entry,), changed=()))
        out = capsys.readouterr().out
        assert "session branch and worktree forgotten" in out
        assert "bookmark" not in out

    def test_manifest_diff_keeps_the_jj_wording_for_a_jj_repo(self, capsys):
        entry = RepoManifestEntry(name="gone", path="/home/u/gone", anchor_base="c" * 40)
        _print_manifest_diff(MagicMock(added=(), removed=(entry,), changed=()))
        assert "torn down: bookmark and workspace forgotten; commits survive" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("path", "ref"), [("git", "branches"), ("/home/u/lide-api", "bookmarks")], ids=["git", "jj"],
    )
    def test_restart_message_names_what_it_reattaches_at(self, git_repo, capsys, path, ref):
        repo_path = str(git_repo.repo_root) if path == "git" else path
        manifest = _manifest("lide-2600", ("lide-api", repo_path))
        with patch("cld.ticket._docker_status", return_value="stopped"), \
             patch("cld.ticket.read_manifest", return_value=manifest), \
             patch("cld.ticket.prepare_store_mounts"), \
             patch("cld.ticket._stop_and_remove"), \
             patch("cld.ticket._launch"):
            restart_ticket(Config(), "lide-2600")
        assert f"reattached at their {ref})" in capsys.readouterr().out

    def test_shutdown_tells_that_a_git_repo_archives_its_branch(self, git_repo, capsys):
        manifest = _manifest("lide-2600", ("lide-api", str(git_repo.repo_root)), ("jj-api", "/home/u/jj-api"))
        with patch("cld.ticket._docker_status", return_value="running"), \
             patch("cld.ticket.read_manifest", return_value=manifest), \
             patch("cld.ticket._stop_and_remove"), \
             patch("cld.ticket.forget_session_state"):
            shutdown_ticket("lide-2600")
        out = capsys.readouterr().out
        assert "lide-api (git): branch t.lide-2600 and its worktree forgotten" in out
        assert "kept under refs/cld/ended/t.lide-2600/" in out
        assert "jj-api" not in out

    def test_shutdown_passes_the_container_side_worktree_of_a_git_repo_only(self, git_repo):
        manifest = _manifest("lide-2600", ("lide-api", str(git_repo.repo_root)), ("jj-api", "/home/u/jj-api"))
        with patch("cld.ticket._docker_status", return_value="running"), \
             patch("cld.ticket.read_manifest", return_value=manifest), \
             patch("cld.ticket._stop_and_remove"), \
             patch("cld.ticket.forget_session_state") as forget:
            shutdown_ticket("lide-2600")
        assert forget.call_args_list == [
            ((str(git_repo.repo_root), "t.lide-2600", "/workspace/lide-2600/lide-api"),),
            (("/home/u/jj-api", "t.lide-2600"),),
        ]

    def test_unreadable_manifest_hint_has_the_git_commands_too(self, caplog):
        with patch("cld.ticket._docker_status", return_value="stopped"), \
             patch("cld.ticket.read_manifest", side_effect=RuntimeError("no label")), \
             patch("cld.ticket._stop_and_remove"), \
             caplog.at_level("WARNING"):
            shutdown_ticket("lide-2600")
        assert "jj bookmark forget t.lide-2600 && jj workspace forget t.lide-2600" in caplog.text
        assert "git branch -D t.lide-2600" in caplog.text

    def test_forget_hint_for_a_missing_repo_has_the_git_commands_too(self, tmp_path, caplog):
        with caplog.at_level("WARNING"):
            forget_session_state(str(tmp_path / "gone"), "t.x")
        assert "jj bookmark forget t.x && jj workspace forget t.x" in caplog.text
        assert "git branch -D t.x" in caplog.text


class TestLogs:
    def test_prints_both_streams(self, capsys):
        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["docker", "logs"]:
                return _res(0, out="boot line\n", err="warn line\n")
            return _res(0, out="running")
        with patch("cld.ticket.subprocess.run", side_effect=fake_run):
            print_ticket_logs("lide-2600", 40)
        captured = capsys.readouterr()
        assert captured.out == "boot line\n"
        assert captured.err == "warn line\n"

    def test_absent_errors(self):
        with patch("cld.ticket._docker_status", return_value="absent"), \
             pytest.raises(RuntimeError, match="no ticket container"):
            print_ticket_logs("lide-2600", 40)
