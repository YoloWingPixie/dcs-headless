# SPDX-License-Identifier: MIT
# Copyright (c) 2026 YoloWingPixie
"""Launch, identify, watch and stop our DCS process.

PowerShell (control.ps1) is used for launching, listing with command lines and
start times, and stopping. Watching during a run uses ``tasklist.exe``, which
is much cheaper, and falls back to a PowerShell List only when an unknown
DCS.exe PID shows up.
"""

from __future__ import annotations

import csv
import json
import ntpath
import subprocess
from dataclasses import asdict, dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any

from .errors import DcsRunning, HeadlessError
from .paths import Paths, to_windows

SCRIPT = "control.ps1"
RECORD = Path("run") / "process.json"
LAUNCH_TIMEOUT = 15


@dataclass(frozen=True)
class Proc:
    pid: int
    name: str
    command: str
    started: str | None
    parent: int = 0


@dataclass(frozen=True)
class Record:
    pid: int
    started: str


def _powershell(argv: list[str]) -> str:
    """Run powershell.exe and return stdout. Tests replace this function."""
    try:
        result = subprocess.run(argv, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise HeadlessError("powershell.exe not found; dcs-headless needs WSL with Windows interop") from None
    except subprocess.CalledProcessError as exc:
        raise HeadlessError(f"PowerShell {argv[argv.index('-Action') + 1]} failed: {exc.stderr.strip()}") from None
    return result.stdout


def _tasklist() -> str:
    """Run tasklist.exe for DCS.exe as CSV. Tests replace this function."""
    argv = ["tasklist.exe", "/FO", "CSV", "/NH", "/FI", "IMAGENAME eq DCS.exe"]
    try:
        result = subprocess.run(argv, check=True, capture_output=True, text=True, errors="replace")
    except FileNotFoundError:
        raise HeadlessError("tasklist.exe not found; dcs-headless needs WSL with Windows interop") from None
    except subprocess.CalledProcessError as exc:
        raise HeadlessError(f"tasklist.exe failed: {exc.stderr.strip()}") from None
    return result.stdout


def dcs_pids() -> set[int]:
    """PIDs of all running DCS.exe processes."""
    return {
        int(row[1])
        for row in csv.reader(_tasklist().splitlines())
        if len(row) >= 2 and row[0].casefold() == "dcs.exe" and row[1].isdigit()
    }


def ensure_idle() -> None:
    """Refuse when any DCS.exe is running. Used before anything is prepared or launched."""
    pids = dcs_pids()
    if pids:
        raise DcsRunning(
            f"DCS is already running (PID {', '.join(map(str, sorted(pids)))}); not starting or modifying anything"
        )


def call(profile: Path, action: str, **params: object) -> Any:
    """Run one control.ps1 action. The script is copied to ``<profile>/run``
    (when its bytes differ) because PowerShell does not run scripts reliably
    from a WSL path."""
    run_dir = profile / "run"
    run_dir.mkdir(exist_ok=True)
    script = run_dir / SCRIPT
    data = files("dcs_headless").joinpath(SCRIPT).read_bytes()
    if not script.is_file() or script.read_bytes() != data:
        script.write_bytes(data)
    argv = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        to_windows(script),
        "-Action",
        action,
    ]
    for key, value in params.items():
        argv += [f"-{key}", str(value)]
    out = _powershell(argv).lstrip("﻿").strip()
    try:
        return json.loads(out) if out else None
    except ValueError:
        raise HeadlessError(f"unexpected PowerShell {action} output: {out[:200]!r}") from None


def _procs(rows: Any) -> list[Proc]:
    if isinstance(rows, dict):
        rows = [rows]
    return [
        Proc(
            pid=int(r["pid"]),
            name=str(r["name"]),
            command=str(r.get("command") or ""),
            started=r.get("started"),
            parent=int(r.get("parent") or 0),
        )
        for r in rows or []
    ]


def list_processes(profile: Path) -> list[Proc]:
    return _procs(call(profile, "List"))


def split_command_line(command: str) -> list[str]:
    """Split a Windows command line on unquoted whitespace, removing double quotes."""
    args: list[str] = []
    current: list[str] = []
    quoted = in_arg = False
    for ch in command:
        if ch == '"':
            quoted = not quoted
            in_arg = True
        elif ch in " \t" and not quoted:
            if in_arg:
                args.append("".join(current))
                current, in_arg = [], False
        else:
            current.append(ch)
            in_arg = True
    if in_arg:
        args.append("".join(current))
    return args


def uses_profile(proc: Proc, profile_name: str) -> bool:
    args = split_command_line(proc.command)[1:]
    return any(a == "-w" and b.casefold() == profile_name.casefold() for a, b in zip(args, args[1:], strict=False))


def is_owned(proc: Proc, *, exe: str, profile_name: str) -> bool:
    """True when ``proc`` is ``<exe> -w <profile_name> --server --norender``."""
    if proc.name.casefold() != "dcs.exe":
        return False
    argv = split_command_line(proc.command)
    if not argv or ntpath.normcase(argv[0]) != ntpath.normcase(exe):
        return False
    args = argv[1:]
    return args.count("-w") == 1 and uses_profile(proc, profile_name) and "--norender" in args and "--server" in args


def owned_processes(paths: Paths, procs: list[Proc]) -> list[Proc]:
    exe = to_windows(paths.exe)
    return [p for p in procs if p.started and is_owned(p, exe=exe, profile_name=paths.profile.name)]


def write_record(profile: Path, record: Record) -> None:
    (profile / RECORD).parent.mkdir(exist_ok=True)
    (profile / RECORD).write_text(json.dumps(asdict(record), indent=2) + "\n", encoding="utf-8")


def read_record(profile: Path) -> Record | None:
    path = profile / RECORD
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return Record(pid=int(data["pid"]), started=str(data["started"]))


def clear_record(profile: Path) -> None:
    (profile / RECORD).unlink(missing_ok=True)


def verify(record: Record, procs: list[Proc], *, exe: str, profile_name: str) -> Proc | None:
    """The live process for ``record``, or None if that PID no longer exists.

    Raises when the PID exists but its name, command line or start time differ
    from the record (the PID was reused or the record is wrong).
    """
    proc = next((p for p in procs if p.pid == record.pid), None)
    if proc is None:
        return None
    if proc.started != record.started or not is_owned(proc, exe=exe, profile_name=profile_name):
        raise HeadlessError(f"PID {record.pid} no longer matches the launched DCS process; not touching it")
    return proc


@dataclass
class Tracker:
    """The launched DCS process and the DCS.exe processes it started (for
    example when DCS restarts itself). Only these are ever stopped."""

    paths: Paths
    record: Record
    lineage: dict[int, str] = field(default_factory=dict)  # pid -> start time, including exited ones
    parents: dict[int, int] = field(default_factory=dict)  # descendant pid -> parent pid
    running: set[int] = field(default_factory=set)  # ours, as of the last refresh
    foreign: set[int] = field(default_factory=set)  # DCS.exe PIDs classified as not ours

    def __post_init__(self) -> None:
        self.lineage[self.record.pid] = self.record.started
        self.running.add(self.record.pid)

    def adopt(self, procs: list[Proc]) -> None:
        """Classify ``procs``: a DCS.exe is ours when its parent is ours, it started
        after that parent, and either the parent is alive with the recorded start
        time or its own command line uses our profile. Everything else is foreign."""
        live = {p.pid: p for p in procs}
        changed = True
        while changed:
            changed = False
            for p in procs:
                if p.pid in self.running or p.started is None or p.parent not in self.lineage:
                    continue
                parent_started = self.lineage[p.parent]
                parent = live.get(p.parent)
                parent_ok = parent is not None and parent.started == parent_started and p.parent in self.running
                if p.started >= parent_started and (parent_ok or uses_profile(p, self.paths.profile.name)):
                    self.lineage[p.pid] = p.started
                    self.parents[p.pid] = p.parent
                    self.running.add(p.pid)
                    changed = True
        self.foreign |= {p.pid for p in procs if p.pid not in self.running}

    def refresh(self) -> set[int]:
        """Update ``running`` from tasklist; returns the running foreign PIDs.
        A PowerShell List runs only when an unclassified PID appears."""
        pids = dcs_pids()
        self.running &= pids
        self.foreign &= pids
        if pids - self.running - self.foreign:
            self.adopt(list_processes(self.paths.profile))
        return pids - self.running

    def check(self) -> str | None:
        """None while only our DCS runs; otherwise why the run must end."""
        foreign = sorted(self.refresh())
        if foreign:
            return f"another DCS started (PID {', '.join(map(str, foreign))}); aborting"
        if not self.running:
            return "DCS exited"
        return None

    def stop(self) -> str:
        """Stop every running process of ours. Returns ``exited`` or ``running``.
        control.ps1 re-verifies each PID immediately before Stop-Process."""
        self.refresh()
        state = "exited"
        errors: list[str] = []
        for pid in sorted(self.running, key=lambda p: (p != self.record.pid, p)):
            if pid == self.record.pid:
                params: dict[str, object] = {"ProfileName": self.paths.profile.name}
            else:
                params = {"ParentId": self.parents[pid]}
            try:
                result = call(self.paths.profile, "Stop", ProcessId=pid, Started=self.lineage[pid], **params)
            except HeadlessError as exc:
                errors.append(str(exc))
                continue
            if result["state"] == "running":
                state = "running"
            else:
                self.running.discard(pid)
        if errors:
            raise HeadlessError("; ".join(errors))
        if state == "exited":
            clear_record(self.paths.profile)
        return state


def launch(paths: Paths, *, timeout: int = LAUNCH_TIMEOUT) -> Tracker:
    """Start DCS and identify it, in one PowerShell call. Raises DcsRunning when
    any DCS.exe is already running (nothing is started then)."""
    result = call(
        paths.profile,
        "Launch",
        Exe=to_windows(paths.exe),
        WorkingDirectory=to_windows(paths.install),
        ProfileName=paths.profile.name,
        TimeoutSeconds=timeout,
    )
    running = _procs(result.get("running"))
    if running:
        pids = ", ".join(str(p.pid) for p in running)
        raise DcsRunning(f"DCS is already running (PID {pids}); not starting another instance")
    procs = _procs(result.get("procs"))
    owned = owned_processes(paths, procs)
    if len(owned) > 1:
        raise HeadlessError(f"more than one DCS process matches the profile: {[p.pid for p in owned]}")
    if not owned:
        seen = f"; other DCS.exe: {[p.pid for p in procs]}" if procs else ""
        raise HeadlessError(f"DCS did not appear within {timeout}s of launch{seen}")
    return track(paths, owned[0], procs)


def track(paths: Paths, proc: Proc, procs: list[Proc]) -> Tracker:
    """Record ``proc`` as our launched process and classify the others."""
    assert proc.started is not None
    record = Record(pid=proc.pid, started=proc.started)
    write_record(paths.profile, record)
    tracker = Tracker(paths, record)
    tracker.adopt(procs)
    return tracker


def find_late(paths: Paths) -> Tracker | None:
    """After a failed launch: our process if exactly one DCS.exe matches the profile."""
    procs = list_processes(paths.profile)
    owned = owned_processes(paths, procs)
    return track(paths, owned[0], procs) if len(owned) == 1 else None
