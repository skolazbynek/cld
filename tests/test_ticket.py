"""Tests for the ticket container lifecycle (cld/ticket.py).

Docker is faked at the subprocess seam (the test_docker.py style) or one
level up (patching the ticket module's own helpers), so nothing here needs
a daemon.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer

from cld.config import Config
from cld.manifest import RepoManifestEntry, TicketManifest, manifest_labels
from cld.registry import RepoEntry
from cld.ticket import (
    _echo_new_log_lines,
    _parse_reanchor,
    _session_state,
    exec_claude,
    exec_shell,
    forget_session_state,
    list_tickets,
    print_ticket_detail,
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

    def test_git_repo_is_refused(self):
        manifest = self._manifest()
        patches, backend, move = self._patches(manifest, backend=self._jj_backend("git"))
        with patches["docker_status"], patches["read_manifest"], patches["session_state"], \
             patches["get_backend"], patches["find_anchor"], patches["resolver"], \
             patches["move"] as move_mock, patches["subprocess_run"] as run, \
             pytest.raises(RuntimeError, match="needs jj"):
            restart_ticket(Config(), "lide-2600", ["lide-api@feature-x"])
        move_mock.assert_not_called()
        run.assert_not_called()

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

    def test_git_backend_skips_forget(self, tmp_path):
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
