"""A fake /mnt tree with a DCS install and user profile, and a fake PowerShell."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from dcs_headless import HeadlessError, process
from dcs_headless import paths as paths_mod

AUTH_BYTES = {"authdata.bin": b"\x00secret-auth\xff", "network.vault": b"vault-bytes"}

# Shape of a real user options.lua (heavily trimmed).
USER_OPTIONS = """options = {
\t["graphics"] = {
\t\t["width"] = 2560,
\t},
\t["miscellaneous"] = {
\t\t["headmove"] = false,
\t\t["launcher"] = true,
\t\t["mgrs_grid_visible"] = true,
\t},
}
"""

# Recorded from a live headless launch (run/process.json of that run).
LIVE_PID = 80120
LIVE_STARTED = "2026-09-28T00:52:00.6156992Z"
EXPLORER_PID = 5816
EXE_WIN = "D:\\DCS World OpenBeta\\bin-mt\\DCS.exe"


def dcs_command(profile: str) -> str:
    return f'"{EXE_WIN}" -w {profile} --server --norender'


def dcs_proc(pid: int, command: str, started: str | None = LIVE_STARTED, parent: int = EXPLORER_PID) -> dict:
    return {"pid": pid, "name": "DCS.exe", "command": command, "started": started, "parent": parent}


GRPC_INSTALL = {
    "Scripts/DCS-gRPC/grpc-mission.lua": "-- grpc-mission\n",
    "Scripts/DCS-gRPC/methods/mission.lua": "-- methods\n",
    "Mods/tech/DCS-gRPC/dcs_grpc.dll": "MZ",
    "Scripts/Hooks/DCS-gRPC.lua": "-- grpc hook\n",
    "Config/dcs-grpc.lua": 'host = "127.0.0.1"\n',
}


def install_grpc(profile: Path) -> None:
    for rel, text in GRPC_INSTALL.items():
        (profile / rel).parent.mkdir(parents=True, exist_ok=True)
        (profile / rel).write_text(text)


def grpc_left(profile: Path) -> list[str]:
    paths = ["Scripts/DCS-gRPC", "Mods/tech/DCS-gRPC", "Scripts/Hooks/DCS-gRPC.lua", "Config/dcs-grpc.lua"]
    return [rel for rel in paths if (profile / rel).exists()]


@dataclass
class Env:
    mnt: Path
    install: Path
    saved_games: Path
    user_profile: Path

    @property
    def profile(self) -> Path:
        return self.saved_games / "DCS.headless"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    mnt = tmp_path / "mnt"
    install = mnt / "d" / "DCS World OpenBeta"
    (install / "bin-mt").mkdir(parents=True)
    (install / "bin-mt" / "DCS.exe").write_bytes(b"MZ")
    saved = mnt / "c" / "Users" / "shepard" / "Saved Games"
    user = saved / "DCS.openbeta"
    (user / "Config").mkdir(parents=True)
    for name, data in AUTH_BYTES.items():
        (user / "Config" / name).write_bytes(data)
    (user / "Config" / "options.lua").write_text(USER_OPTIONS)
    monkeypatch.setattr(paths_mod, "MOUNT_ROOT", mnt)
    monkeypatch.delenv("DCS_INSTALL_DIR", raising=False)
    monkeypatch.delenv("DCS_HEADLESS_AUTH_FROM", raising=False)
    return Env(mnt=mnt, install=install, saved_games=saved, user_profile=user)


@dataclass
class FakeWindows:
    """Stands in for powershell.exe running control.ps1, and for tasklist.exe."""

    procs: list[dict] = field(default_factory=list)
    calls: list[tuple[str, dict]] = field(default_factory=list)
    on_launch: Callable[[], None] | None = None
    on_tasklist: Callable[[int], None] | None = None
    stop_exits: bool = True
    tasklists: int = 0

    def __call__(self, argv: list[str]) -> str:
        assert argv[:6] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"]
        assert argv[6].endswith("\\run\\control.ps1") and argv[6][1:3] == ":\\"
        action = argv[8]
        params = dict(zip(argv[9::2], argv[10::2], strict=True))
        self.calls.append((action, params))
        if action == "List":
            return "\ufeff" + json.dumps(self.procs, separators=(",", ":")) + "\r\n"
        if action == "Launch":
            if self.procs:
                return json.dumps({"running": list(self.procs), "procs": []}) + "\r\n"
            command = f'"{params["-Exe"]}" -w {params["-ProfileName"]} --server --norender'
            self.procs.append(dcs_proc(LIVE_PID, command))
            if self.on_launch:
                self.on_launch()
            return json.dumps({"running": [], "procs": self.procs}) + "\r\n"
        if action == "Stop":
            return self._stop(params)
        raise AssertionError(f"unknown action {action}")

    def _stop(self, params: dict) -> str:
        """control.ps1 Stop: verify name, start time and command line or parent."""
        pid = int(params["-ProcessId"])
        match = [p for p in self.procs if p["pid"] == pid]
        if not match:
            return '{"state":"exited"}\r\n'
        p = match[0]
        if p["name"] != "DCS.exe" or p["started"] != params["-Started"]:
            raise HeadlessError(f"PowerShell Stop failed: Process {pid} is not the recorded DCS process.")
        if "-ParentId" in params:
            if p["parent"] != int(params["-ParentId"]):
                raise HeadlessError(f"PowerShell Stop failed: Process {pid} was not started by DCS")
        elif f"-w {params['-ProfileName']} --server --norender" not in p["command"]:
            raise HeadlessError(f"PowerShell Stop failed: Process {pid} is not running the profile headless.")
        if self.stop_exits:
            self.procs.remove(p)
            return '{"state":"exited"}\r\n'
        return '{"state":"running"}\r\n'

    def tasklist(self) -> str:
        self.tasklists += 1
        if self.on_tasklist:
            self.on_tasklist(self.tasklists)
        rows = [p for p in self.procs if p["name"] == "DCS.exe"]
        if not rows:
            return "INFO: No tasks are running which match the specified criteria.\r\n"
        return "".join(f'"{p["name"]}","{p["pid"]}","Console","1","1,234,567 K"\r\n' for p in rows)

    def actions(self) -> list[str]:
        return [a for a, _ in self.calls]

    def stopped(self) -> list[int]:
        return [int(p["-ProcessId"]) for a, p in self.calls if a == "Stop"]


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> FakeWindows:
    fake = FakeWindows()
    monkeypatch.setattr(process, "_powershell", fake)
    monkeypatch.setattr(process, "_tasklist", fake.tasklist)
    return fake
