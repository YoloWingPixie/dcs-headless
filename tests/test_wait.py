# SPDX-License-Identifier: MIT
# Copyright (c) 2026 YoloWingPixie
import re

from dcs_headless.wait import LogTail, Outcome, wait_for

LOG_LINES = [
    "2026-09-28 00:52:36.758 INFO    EDCORE (Main): (dDispatcher)enterToState_:1\n",
    "2026-09-28 00:52:38.745 INFO    DCS.Lua.Exporter (Main): Dumping _G for DCS 2.9.29.27468\n",
    "2026-09-28 00:52:43.500 INFO    DCS.Lua.Exporter (Main): Export complete in 4.76 s\n",
]


class Clock:
    """Fake time; each sleep advances it and runs the scheduled step."""

    def __init__(self, steps=None):
        self.now = 0.0
        self.steps = steps or {}
        self.ticks = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.ticks += 1
        if self.ticks in self.steps:
            self.steps[self.ticks]()


def run(tmp_path, clock, **kw):
    kw.setdefault("files", [])
    kw.setdefault("ready", [])
    kw.setdefault("fail", [])
    kw.setdefault("timeout", 60)
    return wait_for(log=tmp_path / "dcs.log", clock=clock, sleep=clock.sleep, **kw)


def append(path, text):
    with path.open("a", encoding="utf-8", newline="") as fh:
        fh.write(text)


def test_file_appears(tmp_path):
    marker = tmp_path / "_G" / "__DCS_VERSION__.lua"

    def write():
        marker.parent.mkdir()
        marker.write_text("2.9.29.27468")

    clock = Clock({3: write})
    outcome = run(tmp_path, clock, files=[marker])
    assert outcome.ok and clock.ticks == 3


def test_log_ready_pattern(tmp_path):
    log = tmp_path / "dcs.log"
    clock = Clock({1: lambda: append(log, LOG_LINES[0]), 2: lambda: append(log, LOG_LINES[1] + LOG_LINES[2])})
    outcome = run(tmp_path, clock, ready=[re.compile(r"Export complete")])
    assert outcome.ok and clock.ticks == 2


def test_file_and_log_both_required(tmp_path):
    log, marker = tmp_path / "dcs.log", tmp_path / "done"
    clock = Clock({1: lambda: append(log, LOG_LINES[2]), 4: lambda: marker.write_text("")})
    outcome = run(tmp_path, clock, files=[marker], ready=[re.compile("Export complete")])
    assert outcome.ok and clock.ticks == 4


def test_failure_pattern_wins(tmp_path):
    log = tmp_path / "dcs.log"
    marker = tmp_path / "done"

    def fail_and_mark():
        append(log, "2026-09-28 00:52:43.500 ERROR   DCS.Lua.Exporter (Main): 3 records failed\n")
        marker.write_text("")

    clock = Clock({2: fail_and_mark})
    outcome = run(tmp_path, clock, files=[marker], fail=[re.compile("Export aborted|records failed")])
    assert not outcome.ok
    assert "records failed" in outcome.reason


def test_partial_line_is_not_matched_until_complete(tmp_path):
    log = tmp_path / "dcs.log"
    clock = Clock({1: lambda: append(log, "Export compl"), 2: lambda: append(log, "ete in 1 s\r\n")})
    outcome = run(tmp_path, clock, ready=[re.compile(r"Export complete in 1 s$")])
    assert outcome.ok and clock.ticks == 2


def test_timeout(tmp_path):
    clock = Clock()
    outcome = run(tmp_path, clock, files=[tmp_path / "never"], ready=[re.compile("never")], timeout=10, poll=1)
    assert not outcome.ok
    assert outcome.reason.startswith("timed out after 10s")
    assert "never" in outcome.reason
    assert clock.now == 10


def test_watch_reason_ends_wait(tmp_path):
    clock = Clock()
    calls = []

    def watch():
        calls.append(clock.now)
        return "another DCS started (PID 41236); aborting" if len(calls) == 2 else None

    outcome = run(tmp_path, clock, files=[tmp_path / "never"], watch=watch, watch_every=3, poll=1, timeout=600)
    assert outcome == Outcome(False, "another DCS started (PID 41236); aborting")
    assert calls == [3, 6]


def test_stall_timeout_reports_last_line(tmp_path):
    log = tmp_path / "dcs.log"
    banner = "2026-09-28 00:52:36.758 INFO    APP (Main): DCS/2.9.29.27468 (x86_64; MT; Windows NT 10.0.26100)\n"
    clock = Clock({1: lambda: append(log, LOG_LINES[0]), 5: lambda: append(log, banner)})
    outcome = run(tmp_path, clock, files=[tmp_path / "never"], stall_timeout=120, poll=1, timeout=600)
    assert not outcome.ok
    assert outcome.reason == f"dcs.log stalled: no new output for 120s; last line: {banner.strip()}"
    assert clock.now == 125


def test_stall_timeout_without_log(tmp_path):
    clock = Clock()
    outcome = run(tmp_path, clock, files=[tmp_path / "never"], stall_timeout=30, poll=1, timeout=600)
    assert outcome.reason == "dcs.log stalled: no new output for 30s; last line: (dcs.log is empty or missing)"


def test_growing_log_does_not_stall(tmp_path):
    log = tmp_path / "dcs.log"
    marker = tmp_path / "done"
    steps = {i: (lambda: append(log, "x")) for i in range(1, 300, 50)}
    steps[300] = lambda: marker.write_text("")
    clock = Clock(steps)
    outcome = run(tmp_path, clock, files=[marker], stall_timeout=60, poll=1, timeout=600)
    assert outcome.ok and clock.ticks == 300


def test_log_tail_handles_truncation_and_split_utf8(tmp_path):
    log = tmp_path / "dcs.log"
    tail = LogTail(log)
    assert tail.new_lines() == []
    data = "café line\n".encode()
    log.write_bytes(data[:4])
    assert tail.new_lines() == []
    with log.open("ab") as fh:
        fh.write(data[4:])
    assert tail.new_lines() == ["café line"]
    log.write_text("new\n")
    assert tail.new_lines() == ["new"]
