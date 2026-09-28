# SPDX-License-Identifier: MIT
# Copyright (c) 2026 YoloWingPixie
"""Isolated profile ownership and preparation."""

from __future__ import annotations

import json
import re
import secrets
import shutil
from pathlib import Path

from .auth import remove_auth
from .errors import HeadlessError
from .paths import Paths

MARKER = ".dcs-headless"
OWNER = "dcs-headless"

DEDICATED_SERVER_LUA = (
    "-- dcs-headless: does not start a server or mission.\n"
    'log.write("DCS_HEADLESS", log.INFO, "dedicatedServer.lua no-op loaded")\n'
)

# Logged by the mission dedicatedServer.lua when the server does not start; ``run``
# fails on it.
START_FAILED = "dcs-headless: net.start_server failed"
START_FAILED_REGEX = r"DCS_HEADLESS.*net\.start_server failed"
SERVER_PORT = 10408

# Loaded by DCS's MissionEditor/dedicatedServerGUI.lua (onReadyToStartServer) with
# dofile() in place of starting a server from Config/serverSettings.lua. Built from
# DCS's defaults so no serverSettings.lua is read.
MISSION_SERVER_LUA = """-- dcs-headless: starts a private server running one mission.
local net = require("net")
local mission = (lfs.writedir() .. {mission}):gsub("\\\\", "/")
local settings = net.get_default_server_settings()
settings.name = "dcs-headless"
settings.password = {password}
settings.bind_address = "127.0.0.1"
settings.port = {port}
settings.isPublic = false
settings.maxPlayers = 1
settings.missionList = {{ mission }}
settings.listStartIndex = 1
settings.listShuffle = false
settings.listLoop = false
settings.advanced.resume_mode = net.RESUME_ON_LOAD
settings.advanced.pause_on_load = false
settings.advanced.pause_without_clients = false
log.write("DCS_HEADLESS", log.INFO, "starting mission " .. mission)
local res = net.start_server(settings)
if res ~= 0 then
    log.write("DCS_HEADLESS", log.ERROR, "{failed} with code " .. tostring(res))
end
"""

_LAUNCHER = re.compile(r'(\["launcher"\]\s*=\s*)(true|false)')


def is_marked(profile: Path) -> bool:
    try:
        data = json.loads((profile / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("owner") == OWNER


def claim(profile: Path, *, create: bool) -> None:
    """Ensure ``profile`` (from ``paths.resolve``) is an isolated profile created
    by dcs-headless.

    A missing profile is created and marked when ``create`` is set. An existing
    directory without the marker is refused.
    """
    if is_marked(profile):
        return
    if profile.exists():
        raise HeadlessError(f"{profile} was not created by dcs-headless (no valid {MARKER}); refusing to touch it")
    if not create:
        raise HeadlessError(f"{profile} does not exist; run prepare first")
    if not profile.parent.is_dir():
        raise HeadlessError(f"Saved Games directory does not exist: {profile.parent}")
    profile.mkdir()
    (profile / MARKER).write_text(json.dumps({"owner": OWNER}) + "\n", encoding="utf-8")


def options_with_launcher_off(text: str, source: Path) -> str:
    matches = list(_LAUNCHER.finditer(text))
    if len(matches) != 1:
        raise HeadlessError(f'expected one ["launcher"] option in {source}, found {len(matches)}')
    return _LAUNCHER.sub(r"\1false", text)


def lua_string(text: str) -> str:
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise HeadlessError(f"control characters are not allowed: {text!r}")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def check_mission(mission: Path) -> None:
    if mission.suffix.lower() != ".miz":
        raise HeadlessError(f"mission must be a .miz file: {mission}")
    if not mission.is_file():
        raise HeadlessError(f"mission file not found: {mission}")
    lua_string(mission.name)


def mission_server_lua(mission_rel: str, password: str) -> str:
    return MISSION_SERVER_LUA.format(
        mission=lua_string(mission_rel), password=lua_string(password), port=SERVER_PORT, failed=START_FAILED
    )


def prepare(
    paths: Paths,
    *,
    hooks: list[Path],
    options_template: Path | None = None,
    mission: Path | None = None,
) -> None:
    """Reset the claimed isolated profile for a launch.

    Writes ``Config/options.lua`` (``options_template``, default the auth
    profile's ``Config/options.lua``, with ``launcher = false``), an empty
    ``Config/autoexec.cfg`` and ``Scripts/dedicatedServer.lua``; replaces
    ``Scripts/Hooks`` with exactly ``hooks``; removes ``Logs/dcs.log``,
    ``Config/serverSettings.lua``, ``Missions/`` and any leftover auth files.

    Without ``mission`` the dedicatedServer.lua starts nothing. With it, the .miz
    is copied to ``Missions/`` and dedicatedServer.lua starts a private server
    (loopback, not public, random password) running it.
    """
    if mission is not None:
        check_mission(mission)
    names = [h.name for h in hooks]
    if len(set(names)) != len(names):
        raise HeadlessError(f"duplicate hook file names: {names}")
    for hook in hooks:
        if not hook.is_file():
            raise HeadlessError(f"hook file not found: {hook}")
    source = options_template or paths.auth_profile / "Config" / "options.lua"
    if not source.is_file():
        raise HeadlessError(f"options.lua not found: {source}; pass --options-template")
    options = options_with_launcher_off(source.read_text(encoding="utf-8", errors="replace"), source)

    profile = paths.profile
    remove_auth(profile)

    config = profile / "Config"
    config.mkdir(exist_ok=True)
    (config / "options.lua").write_text(options, encoding="utf-8")
    (config / "autoexec.cfg").write_text("", encoding="utf-8")
    (config / "serverSettings.lua").unlink(missing_ok=True)

    missions = profile / "Missions"
    if missions.exists():
        shutil.rmtree(missions)
    server_lua = DEDICATED_SERVER_LUA
    if mission is not None:
        missions.mkdir()
        shutil.copyfile(mission, missions / mission.name)
        server_lua = mission_server_lua(f"Missions/{mission.name}", secrets.token_hex(16))

    scripts = profile / "Scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "dedicatedServer.lua").write_text(server_lua, encoding="utf-8")

    hooks_dir = scripts / "Hooks"
    if hooks_dir.exists():
        shutil.rmtree(hooks_dir)
    hooks_dir.mkdir()
    for hook in hooks:
        shutil.copyfile(hook, hooks_dir / hook.name)

    (profile / "Logs" / "dcs.log").unlink(missing_ok=True)
    (profile / "Tracks").mkdir(exist_ok=True)
