# SPDX-License-Identifier: MIT
# Copyright (c) 2026 YoloWingPixie
import json

import pytest
from conftest import EXE_WIN, EXPLORER_PID, LIVE_PID, LIVE_STARTED, dcs_command, dcs_proc

from dcs_headless import HeadlessError, process
from dcs_headless.errors import DcsRunning
from dcs_headless.paths import resolve
from dcs_headless.process import Proc, Record, Tracker, is_owned, split_command_line, uses_profile, verify
from dcs_headless.profile import claim

USER_GAME = dcs_proc(41236, f'"{EXE_WIN}" ', "2026-09-27T18:02:11.4410021Z")

# Recorded List output: a user's normal session, a process whose command line
# was unreadable (elevated), and our headless process.
RECORDED_LIST = (
    "\ufeff"
    + json.dumps(
        [USER_GAME, dcs_proc(60001, "", None), dcs_proc(LIVE_PID, dcs_command("DCS.headless"))],
        separators=(",", ":"),
    )
    + "\r\n"
)


def owned(command: str, name: str = "DCS.exe", profile: str = "DCS.headless") -> bool:
    return is_owned(Proc(1, name, command, LIVE_STARTED), exe=EXE_WIN, profile_name=profile)


def test_split_command_line():
    assert split_command_line(dcs_command("DCS.headless")) == [EXE_WIN, "-w", "DCS.headless", "--server", "--norender"]
    assert split_command_line('  a  "b c"d  ""  ') == ["a", "b cd", ""]


def test_live_command_line_is_owned():
    assert owned('"D:\\DCS World OpenBeta\\bin-mt\\DCS.exe" -w DCS.headless --server --norender')
    assert owned('"d:\\dcs world openbeta\\BIN-MT\\dcs.exe" -w dcs.HEADLESS --norender --server')


@pytest.mark.parametrize(
    "command",
    [
        f'"{EXE_WIN}"',  # the user's own session
        f'"{EXE_WIN}" -w DCS.openbeta --server --norender',
        f'"{EXE_WIN}" -w DCS.headless2 --server --norender',
        f'"{EXE_WIN}" -w DCS.headless --server',  # rendering
        f'"{EXE_WIN}" -w DCS.headless --norender',  # not a server
        f'"{EXE_WIN}" -w DCS.headless -w DCS.other --server --norender',
        f'"{EXE_WIN}" --server --norender DCS.headless',
        '"E:\\Other DCS\\bin-mt\\DCS.exe" -w DCS.headless --server --norender',
        '"D:\\DCS World OpenBeta\\bin\\DCS.exe" -w DCS.headless --server --norender',
        "",
    ],
)
def test_foreign_command_lines_are_not_owned(command):
    assert not owned(command)


def test_process_name_must_be_dcs_exe():
    assert not owned(dcs_command("DCS.headless"), name="DCS_server.exe")


def test_uses_profile():
    assert uses_profile(Proc(1, "DCS.exe", dcs_command("DCS.Headless"), None), "dcs.headless")
    assert not uses_profile(Proc(1, "DCS.exe", f'"{EXE_WIN}" -w', None), "DCS.headless")


@pytest.fixture
def paths(env):
    p = resolve()
    claim(p.profile, create=True)
    return p


def test_list_processes_parses_recorded_output(paths, monkeypatch):
    argvs = []
    monkeypatch.setattr(process, "_powershell", lambda argv: argvs.append(argv) or RECORDED_LIST)
    procs = process.list_processes(paths.profile)
    assert [p.pid for p in procs] == [41236, 60001, LIVE_PID]
    assert procs[1].command == "" and procs[1].started is None
    assert procs[2].parent == EXPLORER_PID
    assert argvs[0][6] == "C:\\Users\\shepard\\Saved Games\\DCS.headless\\run\\control.ps1"
    assert (paths.profile / "run" / "control.ps1").read_bytes().startswith(b"# SPDX-License-Identifier: MIT")
    assert [p.pid for p in process.owned_processes(paths, procs)] == [LIVE_PID]


def test_control_script_is_rewritten_only_when_it_differs(paths, monkeypatch):
    monkeypatch.setattr(process, "_powershell", lambda argv: "[]")
    process.list_processes(paths.profile)
    script = paths.profile / "run" / "control.ps1"
    mtime = script.stat().st_mtime_ns
    process.list_processes(paths.profile)
    assert script.stat().st_mtime_ns == mtime
    script.write_text("stale")
    process.list_processes(paths.profile)
    assert script.read_bytes().startswith(b"# SPDX-License-Identifier: MIT")


def test_list_processes_empty_and_single_row(paths, monkeypatch):
    monkeypatch.setattr(process, "_powershell", lambda argv: "[]\r\n")
    assert process.list_processes(paths.profile) == []
    monkeypatch.setattr(process, "_powershell", lambda argv: json.dumps(USER_GAME))
    assert [p.pid for p in process.list_processes(paths.profile)] == [41236]


def test_garbage_output_is_an_error(paths, monkeypatch):
    monkeypatch.setattr(process, "_powershell", lambda argv: "Get-CimInstance : Access denied\r\n")
    with pytest.raises(HeadlessError, match="unexpected PowerShell List output"):
        process.list_processes(paths.profile)


def test_dcs_pids_parses_tasklist(monkeypatch):
    out = (
        '"DCS.exe","41236","Console","1","8,123,456 K"\r\n'
        '"DCS.exe","80120","Console","1","5,000,000 K"\r\n'
        '"notepad.exe","1","Console","1","1 K"\r\n'
    )
    monkeypatch.setattr(process, "_tasklist", lambda: out)
    assert process.dcs_pids() == {41236, LIVE_PID}
    monkeypatch.setattr(process, "_tasklist", lambda: "INFO: No tasks are running which match the criteria.\r\n")
    assert process.dcs_pids() == set()


def test_ensure_idle(windows):
    process.ensure_idle()
    windows.procs.append(USER_GAME)
    with pytest.raises(DcsRunning, match="41236"):
        process.ensure_idle()
    assert windows.calls == []


# Launch output recorded shape: one PowerShell call starts DCS and returns the new process.
LAUNCH_OUTPUT = (
    "\ufeff"
    + json.dumps({"running": [], "procs": dcs_proc(LIVE_PID, dcs_command("DCS.headless"))}, separators=(",", ":"))
    + "\r\n"
)


def test_launch_is_one_call(paths, monkeypatch):
    argvs = []
    monkeypatch.setattr(process, "_powershell", lambda argv: argvs.append(argv) or LAUNCH_OUTPUT)
    tracker = process.launch(paths)
    assert tracker.record == Record(pid=LIVE_PID, started=LIVE_STARTED)
    assert process.read_record(paths.profile) == tracker.record
    assert len(argvs) == 1
    params = dict(zip(argvs[0][9::2], argvs[0][10::2], strict=True))
    assert argvs[0][8] == "Launch"
    assert params == {
        "-Exe": EXE_WIN,
        "-WorkingDirectory": "D:\\DCS World OpenBeta",
        "-ProfileName": "DCS.headless",
        "-TimeoutSeconds": "15",
    }


def test_launch_refused_when_dcs_runs(paths, monkeypatch):
    out = json.dumps({"running": [USER_GAME], "procs": []})
    monkeypatch.setattr(process, "_powershell", lambda argv: out)
    with pytest.raises(DcsRunning, match="41236"):
        process.launch(paths)
    assert process.read_record(paths.profile) is None


@pytest.mark.parametrize(
    ("procs", "match"),
    [
        ([], "did not appear"),
        ([USER_GAME], "did not appear.*41236"),
        ([dcs_proc(1, dcs_command("DCS.headless")), dcs_proc(2, dcs_command("DCS.headless"))], "more than one"),
    ],
)
def test_launch_without_exactly_one_match(paths, monkeypatch, procs, match):
    monkeypatch.setattr(process, "_powershell", lambda argv: json.dumps({"running": [], "procs": procs}))
    with pytest.raises(HeadlessError, match=match):
        process.launch(paths)
    assert process.read_record(paths.profile) is None


RECORD = Record(pid=LIVE_PID, started=LIVE_STARTED)


def test_verify_accepts_matching_process():
    procs = [Proc(LIVE_PID, "DCS.exe", dcs_command("DCS.headless"), LIVE_STARTED)]
    assert verify(RECORD, procs, exe=EXE_WIN, profile_name="DCS.headless") == procs[0]
    assert verify(RECORD, [], exe=EXE_WIN, profile_name="DCS.headless") is None


@pytest.mark.parametrize(
    "proc",
    [
        Proc(LIVE_PID, "DCS.exe", dcs_command("DCS.headless"), "2026-09-28T01:10:00.0000000Z"),  # PID reused
        Proc(LIVE_PID, "DCS.exe", f'"{EXE_WIN}"', LIVE_STARTED),  # user's session
        Proc(LIVE_PID, "notepad.exe", dcs_command("DCS.headless"), LIVE_STARTED),
    ],
)
def test_verify_refuses_mismatch(proc):
    with pytest.raises(HeadlessError, match="no longer matches"):
        verify(RECORD, [proc], exe=EXE_WIN, profile_name="DCS.headless")


RESTART_STARTED = "2026-09-28T00:53:10.0000000Z"


def test_descendant_of_ours_is_recognised(paths, windows):
    tracker = process.launch(paths)
    # DCS restarts itself: a child DCS.exe without -w, then the original exits.
    windows.procs.append(dcs_proc(90001, f'"{EXE_WIN}"', RESTART_STARTED, parent=LIVE_PID))
    assert tracker.check() is None
    assert windows.actions() == ["Launch", "List"]
    windows.procs[:] = [p for p in windows.procs if p["pid"] != LIVE_PID]
    assert tracker.check() is None
    assert tracker.running == {90001}
    assert windows.actions() == ["Launch", "List"]  # nothing unknown: tasklist only
    assert tracker.stop() == "exited"
    assert windows.calls[-1] == (
        "Stop",
        {"-ProcessId": "90001", "-Started": RESTART_STARTED, "-ParentId": str(LIVE_PID)},
    )
    assert windows.procs == []


def test_child_of_exited_parent_needs_our_profile(paths, windows):
    tracker = process.launch(paths)
    windows.procs.clear()
    assert tracker.check() == "DCS exited"
    # Our PID reused by another process that started a DCS.exe: not ours.
    windows.procs.append(dcs_proc(90002, f'"{EXE_WIN}"', RESTART_STARTED, parent=LIVE_PID))
    assert tracker.check() == "another DCS started (PID 90002); aborting"
    # A child that runs our profile is ours even when its parent already exited.
    tracker2 = Tracker(paths, RECORD)
    windows.procs[:] = [dcs_proc(90003, dcs_command("DCS.headless"), RESTART_STARTED, parent=LIVE_PID)]
    assert tracker2.check() is None and tracker2.running == {90003}


def test_child_started_before_parent_is_not_ours(paths, windows):
    tracker = process.launch(paths)
    windows.procs.append(dcs_proc(90004, dcs_command("DCS.headless"), "2026-09-27T00:00:00.0Z", parent=LIVE_PID))
    assert tracker.check() == "another DCS started (PID 90004); aborting"


def test_foreign_dcs_is_reported_once_and_never_stopped(paths, windows):
    tracker = process.launch(paths)
    windows.procs.append(USER_GAME)
    assert tracker.check() == "another DCS started (PID 41236); aborting"
    assert tracker.check() == "another DCS started (PID 41236); aborting"
    assert windows.actions() == ["Launch", "List"]  # classified once
    assert tracker.stop() == "exited"
    assert windows.stopped() == [LIVE_PID]
    assert windows.procs == [USER_GAME]


def test_stop_passes_verification_to_powershell(paths, windows):
    tracker = process.launch(paths)
    assert tracker.stop() == "exited"
    assert windows.actions() == ["Launch", "Stop"]
    assert windows.calls[-1][1] == {
        "-ProcessId": str(LIVE_PID),
        "-Started": LIVE_STARTED,
        "-ProfileName": "DCS.headless",
    }
    assert process.read_record(paths.profile) is None


def test_stop_does_not_touch_a_reused_pid(paths, windows):
    process.write_record(paths.profile, RECORD)
    user = dcs_proc(LIVE_PID, f'"{EXE_WIN}"', LIVE_STARTED)
    windows.procs.append(user)
    with pytest.raises(HeadlessError, match="not running the DCS.headless profile|not running the profile"):
        Tracker(paths, RECORD).stop()
    assert windows.procs == [user]
    assert process.read_record(paths.profile) == RECORD


def test_stop_when_already_exited(paths, windows):
    process.write_record(paths.profile, RECORD)
    assert Tracker(paths, RECORD).stop() == "exited"
    assert windows.calls == []
    assert process.read_record(paths.profile) is None


def test_old_record_with_bom_is_read(paths):
    (paths.profile / "run").mkdir(exist_ok=True)
    (paths.profile / "run" / "process.json").write_text(
        "\ufeff" + json.dumps({"pid": 1, "started": "s", "mode": "server", "command": "c"}), encoding="utf-8"
    )
    assert process.read_record(paths.profile) == Record(1, "s")
