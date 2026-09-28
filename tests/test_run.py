"""Auth copy/deletion, the library ``run()`` and the CLI against a fake Windows."""

import dataclasses
import inspect
import json
import os
import signal
from pathlib import Path

import pytest
from conftest import (
    AUTH_BYTES,
    EXE_WIN,
    GRPC_INSTALL,
    LIVE_PID,
    LIVE_STARTED,
    dcs_command,
    dcs_proc,
    grpc_left,
    install_grpc,
)

import dcs_headless
from dcs_headless import HeadlessError, RunResult, cli, process, runner
from dcs_headless.auth import AUTH_FILES, copy_auth, remove_auth
from dcs_headless.errors import DcsRunning
from dcs_headless.paths import Paths, resolve
from dcs_headless.profile import claim

WAIT = "DCS.Lua.Exporter/_G/__DCS_VERSION__.lua"
USER_GAME = dcs_proc(41236, f'"{EXE_WIN}"', "2026-09-28T01:00:00.0000000Z")


def auth_left(profile):
    return [n for n in AUTH_FILES if (profile / "Config" / n).exists()]


def test_copy_auth_copies_bytes(env):
    claim(env.profile, create=True)
    copy_auth(env.user_profile, env.profile)
    for name, data in AUTH_BYTES.items():
        assert (env.profile / "Config" / name).read_bytes() == data
    assert remove_auth(env.profile) == []
    assert auth_left(env.profile) == []
    assert sorted(p.name for p in (env.user_profile / "Config").iterdir()) == sorted([*AUTH_BYTES, "options.lua"])


def test_copy_auth_missing_source(env):
    claim(env.profile, create=True)
    (env.user_profile / "Config" / "network.vault").unlink()
    with pytest.raises(HeadlessError, match="network.vault"):
        copy_auth(env.user_profile, env.profile)
    assert auth_left(env.profile) == []


@pytest.fixture
def hooks(tmp_path):
    out = []
    for name in ("dump-globals.lua", "serialize.lua"):
        (tmp_path / name).write_text(f"-- {name}\n")
        out.append(str(tmp_path / name))
    return out


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr(runner, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(runner, "WATCH_SECONDS", 0.01)


def run_cli(env, tmp_path, hooks, *extra):
    argv = ["run", "--out", str(tmp_path / "out"), "--wait-file", WAIT, "--timeout", "5", *extra]
    for h in hooks:
        argv += ["--hook", h]
    return cli.main(argv)


def dcs_writes(env, *lines, marker=True, check_auth=True, profile=None):
    """What DCS does after launch in these tests."""
    profile = profile or env.profile

    def act():
        if check_auth:
            assert auth_left(profile) == list(AUTH_FILES), "auth must be present while DCS runs"
        (profile / "Logs").mkdir(exist_ok=True)
        (profile / "Logs" / "dcs.log").write_text("".join(lines))
        if marker:
            path = profile / WAIT
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("2.9.29.27468")

    return act


def result(tmp_path):
    return json.loads((tmp_path / "out" / "result.json").read_text())


def test_run_success(env, tmp_path, hooks, windows, capsys):
    windows.on_launch = dcs_writes(env, "INFO Export complete in 4.76 s\n")
    assert run_cli(env, tmp_path, hooks) == 0

    assert windows.actions() == ["Launch", "Stop"]
    assert auth_left(env.profile) == []
    r = result(tmp_path)
    assert r["ok"] and r["pid"] == LIVE_PID and r["cleanup_errors"] == []
    assert r["log"] == str(tmp_path / "out" / "dcs.log")
    assert (tmp_path / "out" / "dcs.log").read_text() == "INFO Export complete in 4.76 s\n"
    assert sorted(p.name for p in (env.profile / "Scripts" / "Hooks").iterdir()) == [
        "dump-globals.lua",
        "serialize.lua",
    ]
    assert '["launcher"] = false' in (env.profile / "Config" / "options.lua").read_text()
    assert windows.procs == []
    assert not (env.profile / "Missions").exists()
    assert "no-op" in (env.profile / "Scripts" / "dedicatedServer.lua").read_text()
    out = capsys.readouterr()
    assert json.loads(out.out) == r
    for data in AUTH_BYTES.values():
        assert data.decode("latin-1") not in out.out + out.err


def test_run_refuses_when_dcs_is_running(env, tmp_path, hooks, windows):
    windows.procs.append(USER_GAME)
    assert run_cli(env, tmp_path, hooks) == 1
    assert windows.calls == []
    assert windows.procs == [USER_GAME]
    assert auth_left(env.profile) == []
    assert not (tmp_path / "out").exists()


def test_run_refused_by_launch_race(env, tmp_path, hooks, windows, monkeypatch):
    """The user starts DCS between the idle check and Launch: nothing is stopped."""
    monkeypatch.setattr(process, "ensure_idle", lambda: windows.procs.append(USER_GAME))
    with pytest.raises(DcsRunning, match="41236"):
        runner.run(out=tmp_path / "out", hooks=hooks, wait_file=WAIT)
    assert windows.actions() == ["Launch"]
    assert windows.procs == [USER_GAME]
    assert auth_left(env.profile) == []


def test_run_refuses_main_profile(env, tmp_path, hooks, windows):
    assert run_cli(env, tmp_path, hooks, "--profile", "DCS.openbeta") == 1
    assert windows.calls == [] and windows.tasklists == 0


def test_run_refuses_unmarked_profile(env, tmp_path, hooks, windows):
    other = env.saved_games / "DCS.other"
    other.mkdir()
    with pytest.raises(HeadlessError, match="not created by dcs-headless"):
        runner.run(profile="DCS.other", out=tmp_path / "out", hooks=hooks, wait_file=WAIT)
    assert list(other.iterdir()) == []
    assert windows.tasklists == 0


def test_stale_wait_file_does_not_satisfy_run(env, tmp_path, hooks, windows):
    claim(env.profile, create=True)
    stale = env.profile / WAIT
    stale.parent.mkdir(parents=True)
    stale.write_text("old")
    windows.on_launch = dcs_writes(env, "INFO starting\n", marker=False)
    assert run_cli(env, tmp_path, hooks, "--timeout", "0.05") == 1
    assert result(tmp_path)["reason"].startswith("timed out")
    assert auth_left(env.profile) == []
    assert windows.actions()[-1] == "Stop"


def test_stall_fails_run_with_last_line(env, tmp_path, hooks, windows):
    windows.on_launch = dcs_writes(env, "INFO    APP (Main): DCS/2.9.29.27468\n", marker=False)
    assert run_cli(env, tmp_path, hooks, "--stall-timeout", "0.05") == 1
    reason = result(tmp_path)["reason"]
    assert reason.startswith("dcs.log stalled: no new output for 0.05s")
    assert reason.endswith("last line: INFO    APP (Main): DCS/2.9.29.27468")
    assert windows.procs == [] and auth_left(env.profile) == []


def test_failure_pattern_stops_and_deletes_auth(env, tmp_path, hooks, windows):
    windows.on_launch = dcs_writes(env, "ERROR Export aborted: out of memory\n", marker=False)
    assert run_cli(env, tmp_path, hooks, "--fail-log", "Export aborted|records failed") == 1
    r = result(tmp_path)
    assert "Export aborted" in r["reason"]
    assert windows.actions()[-1] == "Stop" and windows.procs == []
    assert auth_left(env.profile) == []


def test_foreign_dcs_mid_run_aborts_and_only_ours_is_stopped(env, tmp_path, hooks, windows):
    def launched():
        dcs_writes(env, "INFO loading\n", marker=False)()
        windows.on_tasklist = lambda n: windows.procs.append(USER_GAME) if USER_GAME not in windows.procs else None

    windows.on_launch = launched
    assert run_cli(env, tmp_path, hooks) == 1
    assert result(tmp_path)["reason"] == "another DCS started (PID 41236); aborting"
    assert windows.stopped() == [LIVE_PID]
    assert all(params.get("-ProcessId") != "41236" for _, params in windows.calls)
    assert windows.procs == [USER_GAME]
    assert auth_left(env.profile) == []


def test_our_dcs_exiting_ends_run(env, tmp_path, hooks, windows):
    def launched():
        dcs_writes(env, "INFO loading\n", marker=False)()
        windows.on_tasklist = lambda n: windows.procs.clear()

    windows.on_launch = launched
    assert run_cli(env, tmp_path, hooks) == 1
    assert result(tmp_path)["reason"] == "DCS exited"
    assert "Stop" not in windows.actions()


def test_self_restart_child_is_followed_and_stopped(env, tmp_path, hooks, windows):
    # The parent is gone before the child is first seen, so the child must run our profile.
    child = dcs_proc(90001, dcs_command("DCS.headless"), "2026-09-28T00:53:00.0000000Z", parent=LIVE_PID)

    def restart(n):
        if n == 3:
            windows.procs[:] = [child]
        if n == 6:
            dcs_writes(env, "INFO done\n")()

    windows.on_launch = lambda: setattr(windows, "on_tasklist", restart)
    assert run_cli(env, tmp_path, hooks) == 0
    assert windows.stopped() == [90001]
    assert windows.calls[-1][1]["-ParentId"] == str(LIVE_PID)
    assert windows.procs == []


def test_auth_deleted_when_launch_raises(env, tmp_path, hooks, windows, monkeypatch):
    def boom(paths):
        assert auth_left(paths.profile) == list(AUTH_FILES)
        raise HeadlessError("The Windows desktop shell view is unavailable.")

    monkeypatch.setattr(process, "launch", boom)
    assert run_cli(env, tmp_path, hooks) == 1
    assert "desktop shell" in result(tmp_path)["reason"]
    assert auth_left(env.profile) == []


def test_auth_deleted_on_unexpected_exception(env, tmp_path, hooks, windows, monkeypatch):
    windows.on_launch = dcs_writes(env, "", marker=False)

    def crash(**kw):
        raise RuntimeError("bug")

    monkeypatch.setattr(runner, "wait_for", crash)
    with pytest.raises(RuntimeError, match="bug"):
        run_cli(env, tmp_path, hooks)
    assert auth_left(env.profile) == []
    assert windows.actions()[-1] == "Stop" and windows.procs == []


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_auth_deleted_on_signal(env, tmp_path, hooks, windows, monkeypatch, signum):
    windows.on_launch = dcs_writes(env, "", marker=False)
    sent = []

    def interrupted_wait(**kw):
        sent.append(signum)
        os.kill(os.getpid(), signum)
        raise AssertionError("signal handler did not raise")

    monkeypatch.setattr(runner, "wait_for", interrupted_wait)
    before = signal.getsignal(signum)
    assert run_cli(env, tmp_path, hooks) == 128 + signum
    assert sent == [signum]
    assert "interrupted" in result(tmp_path)["reason"]
    assert auth_left(env.profile) == []
    assert windows.actions()[-1] == "Stop" and windows.procs == []
    assert signal.getsignal(signum) == before


def test_signal_during_cleanup_is_ignored(env, tmp_path, hooks, windows, monkeypatch):
    windows.on_launch = dcs_writes(env, "", marker=False)
    monkeypatch.setattr(runner, "wait_for", lambda **kw: runner.Outcome(False, "x"))
    real_stop = process.Tracker.stop

    def stop_with_signal(self):
        os.kill(os.getpid(), signal.SIGTERM)
        return real_stop(self)

    monkeypatch.setattr(process.Tracker, "stop", stop_with_signal)
    assert run_cli(env, tmp_path, hooks) == 1
    assert auth_left(env.profile) == []
    assert windows.procs == []


def test_process_found_late_is_still_stopped(env, tmp_path, hooks, windows, monkeypatch):
    def late(paths):
        windows.procs.append(dcs_proc(LIVE_PID, dcs_command("DCS.headless")))
        raise HeadlessError("DCS did not appear within 15s of launch")

    monkeypatch.setattr(process, "launch", late)
    assert run_cli(env, tmp_path, hooks) == 1
    assert windows.actions() == ["List", "Stop"]
    assert windows.procs == []
    assert auth_left(env.profile) == []


def test_stop_failure_still_deletes_auth(env, tmp_path, hooks, windows):
    windows.stop_exits = False
    windows.on_launch = dcs_writes(env, "", marker=True)
    assert run_cli(env, tmp_path, hooks) == 1
    r = result(tmp_path)
    assert r["cleanup_errors"] == ["DCS did not exit after Stop-Process"]
    assert auth_left(env.profile) == []


@pytest.mark.parametrize("rel", ["/abs/file", "../DCS.openbeta/Config/authdata.bin", "."])
def test_wait_file_must_stay_in_profile(env, tmp_path, hooks, windows, rel):
    assert cli.main(["run", "--out", str(tmp_path / "out"), "--wait-file", rel]) == 1
    assert windows.calls == []


def test_run_needs_a_condition(env, tmp_path, windows):
    assert cli.main(["run", "--out", str(tmp_path / "out")]) == 1
    assert windows.calls == []


def test_stop_command_removes_leftover_auth(env, windows):
    claim(env.profile, create=True)
    (env.profile / "Config").mkdir()
    (env.profile / "Config" / "authdata.bin").write_bytes(b"x")
    assert cli.main(["stop"]) == 0
    assert auth_left(env.profile) == []


def test_status_and_prepare_commands(env, tmp_path, hooks, windows, capsys):
    assert cli.main(["status"]) == 1
    assert cli.main(["prepare", "--hook", hooks[0]]) == 0
    assert cli.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {"state": "none"}
    windows.procs.append(USER_GAME)
    assert cli.main(["prepare"]) == 1  # any running DCS blocks prepare
    assert (env.profile / "Scripts" / "Hooks" / "dump-globals.lua").exists()
    assert windows.procs == [USER_GAME]


def test_stop_command_deletes_auth_even_when_pid_mismatches(env, windows):
    claim(env.profile, create=True)
    process.write_record(env.profile, process.Record(LIVE_PID, LIVE_STARTED))
    user = dcs_proc(LIVE_PID, f'"{EXE_WIN}"', "2026-09-28T01:00:00.0000000Z")
    windows.procs.append(user)
    (env.profile / "Config").mkdir()
    (env.profile / "Config" / "network.vault").write_bytes(b"x")
    assert cli.main(["stop"]) == 1
    assert windows.procs == [user]
    assert auth_left(env.profile) == []


# --- library API contract used by dcs-world-schema -------------------------------------


def test_library_contract():
    assert dcs_headless.run is runner.run
    assert dcs_headless.RunResult is RunResult
    assert [f.name for f in dataclasses.fields(RunResult)] == [
        "ok",
        "reason",
        "profile",
        "pid",
        "elapsed_seconds",
        "log",
        "cleanup_errors",
        "warnings",
    ]
    params = inspect.signature(runner.run).parameters
    assert {name: p.default for name, p in params.items()} == {
        "profile": "DCS.headless",
        "hooks": (),
        "out": inspect.Parameter.empty,
        "wait_file": None,
        "wait_log": None,
        "fail_log": None,
        "timeout": None,
        "stall_timeout": None,
        "install_dir": None,
        "auth_from": None,
        "options_template": None,
        "mission": None,
        "until_stopped": False,
        "with_grpc": False,
    }
    assert [f.name for f in dataclasses.fields(Paths)] == ["install", "auth_profile", "profile"]
    assert {name: p.default for name, p in inspect.signature(resolve).parameters.items()} == {
        "install_dir": None,
        "auth_from": None,
        "profile": "DCS.headless",
    }


def test_library_run_datamine(env, tmp_path, hooks, windows):
    windows.on_launch = dcs_writes(env, "INFO Export complete in 4.76 s\n", profile=env.saved_games / "DCS.datamine")
    res = dcs_headless.run(
        profile="DCS.datamine",
        hooks=[Path(h) for h in hooks],
        out=tmp_path / "out",
        wait_file="DCS.Lua.Exporter/_G/__DCS_VERSION__.lua",
        fail_log="Export aborted|records failed",
        timeout=600,
        stall_timeout=120,
    )
    paths = resolve(profile="DCS.datamine")
    assert paths.exe == env.install / "bin-mt" / "DCS.exe" and paths.saved_games == env.saved_games
    assert res.ok and res.reason == "condition met" and res.cleanup_errors == []
    assert res.profile == paths.profile == env.saved_games / "DCS.datamine"
    assert res.pid == LIVE_PID and res.elapsed_seconds >= 0
    assert res.log == tmp_path / "out" / "dcs.log" and res.log.is_file()
    assert (res.profile / WAIT).is_file()
    assert auth_left(res.profile) == []


def test_library_run_failure_returns_result(env, tmp_path, hooks, windows):
    windows.on_launch = dcs_writes(
        env, "ERROR 3 records failed\n", marker=False, profile=env.saved_games / "DCS.datamine"
    )
    res = dcs_headless.run(profile="DCS.datamine", hooks=hooks, out=tmp_path / "out", wait_file=WAIT, fail_log="failed")
    assert not res.ok and "records failed" in res.reason and res.pid == LIVE_PID


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"profile": "DCS.openbeta"}, "main DCS profile"),
        ({"install_dir": "/nowhere"}, "no bin-mt"),
        ({"wait_file": None}, "at least one"),
        ({"wait_log": "("}, "invalid wait_log regex"),
    ],
)
def test_library_refusals_raise(env, tmp_path, windows, kwargs, match):
    args = {"out": tmp_path / "out", "wait_file": WAIT} | kwargs
    with pytest.raises(HeadlessError, match=match):
        dcs_headless.run(**args)
    assert windows.calls == []


# --- mission runs ----------------------------------------------------------------------


@pytest.fixture
def miz(tmp_path):
    path = tmp_path / "Caucasus.miz"
    path.write_bytes(b"PK\x03\x04fake-miz")
    return path


def test_run_with_mission(env, tmp_path, hooks, windows, miz):
    def launched():
        assert (env.profile / "Missions" / "Caucasus.miz").read_bytes() == miz.read_bytes()
        assert "net.start_server(settings)" in (env.profile / "Scripts" / "dedicatedServer.lua").read_text()
        dcs_writes(env, "INFO    DCS_HEADLESS (Main): starting mission C:/.../Missions/Caucasus.miz\n")()

    windows.on_launch = launched
    assert run_cli(env, tmp_path, hooks, "--mission", str(miz)) == 0
    assert windows.actions() == ["Launch", "Stop"] and windows.procs == []
    assert auth_left(env.profile) == []
    assert result(tmp_path)["ok"]


def test_mission_server_start_failure_fails_run(env, tmp_path, hooks, windows, miz):
    line = "ERROR   DCS_HEADLESS (Main): dcs-headless: net.start_server failed with code 2\n"
    windows.on_launch = dcs_writes(env, line, marker=False)
    res = dcs_headless.run(hooks=hooks, out=tmp_path / "out", wait_file=WAIT, mission=miz)
    assert not res.ok and "net.start_server failed" in res.reason
    assert windows.actions()[-1] == "Stop" and windows.procs == []
    assert auth_left(env.profile) == []


@pytest.mark.parametrize("name", ["Caucasus.lua", "missing.miz"])
def test_run_refuses_bad_mission(env, tmp_path, hooks, windows, name):
    if name.endswith(".lua"):
        (tmp_path / name).write_text("")
    with pytest.raises(HeadlessError, match="mission"):
        dcs_headless.run(hooks=hooks, out=tmp_path / "out", wait_file=WAIT, mission=tmp_path / name)
    assert windows.calls == [] and windows.tasklists == 0
    assert not env.profile.exists()
    assert run_cli(env, tmp_path, hooks, "--mission", str(tmp_path / name)) == 1
    assert windows.calls == []


# --- DCS-gRPC ---------------------------------------------------------------------------


def test_run_with_grpc(env, tmp_path, hooks, windows):
    install_grpc(env.user_profile)

    def launched():
        for rel, text in GRPC_INSTALL.items():
            assert (env.profile / rel).read_text() == text
        dcs_writes(env, "INFO done\n")()

    windows.on_launch = launched
    assert run_cli(env, tmp_path, hooks, "--with-grpc") == 0
    assert windows.actions() == ["Launch", "Stop"] and auth_left(env.profile) == []

    windows.calls.clear()
    windows.on_launch = dcs_writes(env, "INFO done\n")
    assert run_cli(env, tmp_path, hooks) == 0
    assert grpc_left(env.profile) == []


def test_run_with_grpc_missing_in_source_runs_without_it(env, tmp_path, hooks, windows, capsys):
    install_grpc(env.user_profile)
    (env.user_profile / "Config" / "dcs-grpc.lua").unlink()
    windows.on_launch = dcs_writes(env, "INFO done\n")
    res = dcs_headless.run(hooks=hooks, out=tmp_path / "out", wait_file=WAIT, with_grpc=True)
    warning = f"DCS-gRPC not copied, missing in {env.user_profile}: Config/dcs-grpc.lua"
    assert res.ok and res.warnings == [warning]
    assert result(tmp_path)["warnings"] == [warning]
    assert grpc_left(env.profile) == []

    windows.on_launch = dcs_writes(env, "INFO done\n")
    assert run_cli(env, tmp_path, hooks, "--with-grpc") == 0
    assert capsys.readouterr().err == f"dcs-headless: warning: {warning}\n"
    assert result(tmp_path)["warnings"] == [warning]


def test_run_without_grpc_has_no_warnings(env, tmp_path, hooks, windows, capsys):
    windows.on_launch = dcs_writes(env, "INFO done\n")
    assert run_cli(env, tmp_path, hooks) == 0
    assert result(tmp_path)["warnings"] == [] and capsys.readouterr().err == ""


def test_run_with_grpc_hook_collision_fails_before_launch(env, tmp_path, windows):
    install_grpc(env.user_profile)
    (tmp_path / "DCS-gRPC.lua").write_text("")
    assert run_cli(env, tmp_path, [str(tmp_path / "DCS-gRPC.lua")], "--with-grpc") == 1
    assert windows.actions() == []
    assert auth_left(env.profile) == []


def test_prepare_command_with_grpc(env, windows, capsys):
    assert cli.main(["prepare", "--with-grpc"]) == 0
    assert capsys.readouterr().err.startswith("dcs-headless: warning: DCS-gRPC not copied, missing in ")
    install_grpc(env.user_profile)
    assert cli.main(["prepare", "--with-grpc"]) == 0
    assert len(grpc_left(env.profile)) == 4 and capsys.readouterr().err == ""
    assert cli.main(["prepare"]) == 0
    assert grpc_left(env.profile) == []


# --- until stopped ---------------------------------------------------------------------


def serve(env, tmp_path, *extra):
    return cli.main(["run", "--until-stopped", "--out", str(tmp_path / "out"), *extra])


def after_launch(env, windows, on_tasklist, line="INFO    Dispatcher (Main): //=== END OF INIT ===//\n"):
    def launched():
        dcs_writes(env, line, marker=False)()
        windows.on_tasklist = on_tasklist

    windows.on_launch = launched


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_until_stopped_runs_until_signal(env, tmp_path, windows, miz, signum):
    """The log stops growing and no timeout applies; a signal stops DCS and cleans up."""
    after_launch(env, windows, lambda n: os.kill(os.getpid(), signum) if n == 30 else None)
    assert serve(env, tmp_path, "--mission", str(miz)) == 128 + signum
    assert windows.tasklists >= 30
    r = result(tmp_path)
    assert not r["ok"] and r["reason"] == f"interrupted by signal {signal.Signals(signum).name}"
    assert r["elapsed_seconds"] >= 0 and r["cleanup_errors"] == []
    assert windows.actions() == ["Launch", "Stop"] and windows.procs == []
    assert auth_left(env.profile) == []
    assert "net.start_server(settings)" in (env.profile / "Scripts" / "dedicatedServer.lua").read_text()


def test_until_stopped_dcs_exiting_fails_run(env, tmp_path, windows):
    after_launch(env, windows, lambda n: windows.procs.clear() if n == 10 else None)
    assert serve(env, tmp_path) == 1
    assert result(tmp_path)["reason"] == "DCS exited"
    assert "Stop" not in windows.actions()
    assert auth_left(env.profile) == []


def test_until_stopped_fail_log_applies(env, tmp_path, windows):
    after_launch(env, windows, None, line="ERROR   Lua::Config (Main): hook crashed\n")
    assert serve(env, tmp_path, "--fail-log", "hook crashed") == 1
    assert "hook crashed" in result(tmp_path)["reason"]
    assert windows.actions()[-1] == "Stop" and windows.procs == []
    assert auth_left(env.profile) == []


def test_until_stopped_foreign_dcs_aborts(env, tmp_path, windows):
    after_launch(env, windows, lambda n: windows.procs.append(USER_GAME) if n == 5 else None)
    assert serve(env, tmp_path) == 1
    assert result(tmp_path)["reason"] == "another DCS started (PID 41236); aborting"
    assert windows.stopped() == [LIVE_PID] and windows.procs == [USER_GAME]
    assert auth_left(env.profile) == []


@pytest.mark.parametrize(
    "extra",
    [["--wait-log", "END OF INIT"], ["--wait-file", WAIT], ["--timeout", "600"], ["--stall-timeout", "0"]],
)
def test_until_stopped_rejects_conditions_and_limits(env, tmp_path, windows, extra):
    assert serve(env, tmp_path, *extra) == 1
    assert windows.calls == [] and windows.tasklists == 0


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [({"wait_log": "x"}, "no wait_file"), ({"timeout": 600}, "no timeout"), ({"stall_timeout": 0}, "no timeout")],
)
def test_library_until_stopped_rejects(env, tmp_path, windows, kwargs, match):
    with pytest.raises(HeadlessError, match=match):
        dcs_headless.run(out=tmp_path / "out", until_stopped=True, **kwargs)
    assert windows.calls == []


def test_stall_timeout_zero_disables_stall(env, tmp_path, windows, monkeypatch):
    seen = {}

    def fake_wait(**kw):
        seen.update(kw)
        return runner.Outcome(True, "condition met")

    monkeypatch.setattr(runner, "wait_for", fake_wait)
    assert cli.main(["run", "--out", str(tmp_path / "out"), "--wait-file", WAIT, "--stall-timeout", "0"]) == 0
    assert seen["timeout"] == 600.0 and seen["stall_timeout"] == 0
    assert cli.main(["run", "--out", str(tmp_path / "out"), "--wait-file", WAIT]) == 0
    assert seen["stall_timeout"] == 120.0
