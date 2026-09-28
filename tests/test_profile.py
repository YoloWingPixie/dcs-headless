import json
import re
import shutil
import subprocess

import pytest
from conftest import GRPC_INSTALL, USER_OPTIONS, grpc_left, install_grpc

from dcs_headless import HeadlessError
from dcs_headless.paths import resolve
from dcs_headless.profile import (
    DEDICATED_SERVER_LUA,
    MARKER,
    START_FAILED_REGEX,
    claim,
    is_marked,
    mission_server_lua,
    prepare,
)


@pytest.fixture
def paths(env):
    p = resolve()
    claim(p.profile, create=True)
    return p


def test_prepare_uses_auth_profile_options_with_launcher_off(env, paths, tmp_path):
    hook = tmp_path / "dump-globals.lua"
    hook.write_text("-- hook\n")
    prepare(paths, hooks=[hook])

    assert json.loads((env.profile / MARKER).read_text()) == {"owner": "dcs-headless"}
    assert is_marked(env.profile)
    options = (env.profile / "Config" / "options.lua").read_text()
    assert options == USER_OPTIONS.replace('["launcher"] = true', '["launcher"] = false')
    assert (env.user_profile / "Config" / "options.lua").read_text() == USER_OPTIONS  # source untouched
    assert (env.profile / "Config" / "autoexec.cfg").read_text() == ""
    assert (env.profile / "Scripts" / "dedicatedServer.lua").read_text() == DEDICATED_SERVER_LUA
    assert sorted(p.name for p in (env.profile / "Scripts" / "Hooks").iterdir()) == ["dump-globals.lua"]
    assert (env.profile / "Tracks").is_dir()
    assert not (env.profile / "Missions").exists()
    assert not (env.profile / "Config" / "serverSettings.lua").exists()


def test_prepare_resets_existing_profile(env, paths, tmp_path):
    a, b = tmp_path / "a.lua", tmp_path / "b.lua"
    a.write_text("a")
    b.write_text("b")
    prepare(paths, hooks=[a])
    (env.profile / "Logs").mkdir()
    (env.profile / "Logs" / "dcs.log").write_text("old run\n")
    (env.profile / "Config" / "authdata.bin").write_bytes(b"left over")

    prepare(paths, hooks=[b])

    assert [p.name for p in (env.profile / "Scripts" / "Hooks").iterdir()] == ["b.lua"]
    assert not (env.profile / "Logs" / "dcs.log").exists()
    assert not (env.profile / "Config" / "authdata.bin").exists()


def test_custom_options_template(env, paths, tmp_path):
    template = tmp_path / "options.lua"
    template.write_text('options = { ["miscellaneous"] = { ["launcher"] = true } }\n')
    prepare(paths, hooks=[], options_template=template)
    assert (env.profile / "Config" / "options.lua").read_text() == (
        'options = { ["miscellaneous"] = { ["launcher"] = false } }\n'
    )


@pytest.mark.parametrize("text", ["options = {}\n", 'a = {["launcher"] = true, b = {["launcher"] = false}}\n'])
def test_options_without_single_launcher_entry_are_refused(env, paths, text):
    (env.user_profile / "Config" / "options.lua").write_text(text)
    with pytest.raises(HeadlessError, match="launcher"):
        prepare(paths, hooks=[])
    assert not (env.profile / "Config").exists()


def test_missing_user_options_is_refused(env, paths):
    (env.user_profile / "Config" / "options.lua").unlink()
    with pytest.raises(HeadlessError, match="options.lua not found.*--options-template"):
        prepare(paths, hooks=[])


def test_missing_or_duplicate_hooks_are_refused(env, paths, tmp_path):
    with pytest.raises(HeadlessError, match="not found"):
        prepare(paths, hooks=[tmp_path / "nope.lua"])
    (tmp_path / "x").mkdir()
    for d in (tmp_path, tmp_path / "x"):
        (d / "h.lua").write_text("")
    with pytest.raises(HeadlessError, match="duplicate"):
        prepare(paths, hooks=[tmp_path / "h.lua", tmp_path / "x" / "h.lua"])


def test_prepare_with_grpc_copies_the_install(env, paths, tmp_path):
    install_grpc(env.user_profile)
    hook = tmp_path / "h.lua"
    hook.write_text("")
    prepare(paths, hooks=[hook], with_grpc=True)
    for rel, text in GRPC_INSTALL.items():
        assert (env.profile / rel).read_text() == text
    assert sorted(p.name for p in (env.profile / "Scripts" / "Hooks").iterdir()) == ["DCS-gRPC.lua", "h.lua"]

    (env.profile / "Scripts" / "DCS-gRPC" / "stale.lua").write_text("")
    (env.user_profile / "Config" / "dcs-grpc.lua").write_text("changed\n")
    prepare(paths, hooks=[], with_grpc=True)
    assert not (env.profile / "Scripts" / "DCS-gRPC" / "stale.lua").exists()
    assert (env.profile / "Config" / "dcs-grpc.lua").read_text() == "changed\n"


def test_prepare_without_grpc_removes_previous_copy(env, paths):
    install_grpc(env.user_profile)
    prepare(paths, hooks=[], with_grpc=True)
    (env.profile / "Mods" / "tech" / "Other").mkdir()
    prepare(paths, hooks=[])
    assert grpc_left(env.profile) == []
    assert (env.profile / "Mods" / "tech" / "Other").is_dir()
    assert len(grpc_left(env.user_profile)) == 4  # source untouched


@pytest.mark.parametrize(
    "rel", ["Scripts/DCS-gRPC/", "Mods/tech/DCS-gRPC/", "Scripts/Hooks/DCS-gRPC.lua", "Config/dcs-grpc.lua"]
)
def test_prepare_with_grpc_partly_missing_in_source_copies_nothing(env, paths, rel):
    install_grpc(env.user_profile)
    prepare(paths, hooks=[], with_grpc=True)
    path = env.user_profile / rel
    if rel.endswith("/"):
        shutil.rmtree(path)
    else:
        path.unlink()
    assert prepare(paths, hooks=[], with_grpc=True) == [f"DCS-gRPC not copied, missing in {env.user_profile}: {rel}"]
    assert grpc_left(env.profile) == []
    assert (env.profile / "Config" / "options.lua").is_file()


def test_prepare_with_grpc_reports_every_missing_part(env, paths):
    (warning,) = prepare(paths, hooks=[], with_grpc=True)
    assert warning.endswith(": Scripts/DCS-gRPC/, Mods/tech/DCS-gRPC/, Config/dcs-grpc.lua, Scripts/Hooks/DCS-gRPC.lua")
    assert prepare(paths, hooks=[]) == []


def test_grpc_hook_name_collision_is_refused(env, paths, tmp_path):
    install_grpc(env.user_profile)
    hook = tmp_path / "DCS-gRPC.lua"
    hook.write_text("")
    with pytest.raises(HeadlessError, match="duplicate"):
        prepare(paths, hooks=[hook], with_grpc=True)
    assert not (env.profile / "Config").exists()
    prepare(paths, hooks=[hook])


@pytest.mark.parametrize("name", ["DCS", "DCS.openbeta", "dcs.OpenBeta", "DCS.openalpha", "DCS.release_server"])
def test_main_profiles_are_refused_even_when_marked(env, name):
    profile = env.saved_games / name
    profile.mkdir(exist_ok=True)
    (profile / MARKER).write_text(json.dumps({"owner": "dcs-headless"}))
    with pytest.raises(HeadlessError, match="main DCS profile"):
        resolve(profile=name)


def test_unmarked_existing_profile_is_refused_and_untouched(env):
    other = env.saved_games / "DCS.other-tool"
    (other / "Config").mkdir(parents=True)
    (other / "Config" / "options.lua").write_text("user options")
    with pytest.raises(HeadlessError, match="not created by dcs-headless"):
        claim(resolve(profile="DCS.other-tool").profile, create=True)
    assert (other / "Config" / "options.lua").read_text() == "user options"
    assert sorted(p.name for p in other.rglob("*")) == ["Config", "options.lua"]


def test_marker_with_wrong_owner_is_refused(env):
    env.profile.mkdir()
    (env.profile / MARKER).write_text('{"owner": "someone-else"}')
    with pytest.raises(HeadlessError, match="not created by dcs-headless"):
        claim(env.profile, create=True)


@pytest.mark.parametrize("name", ["", "..", "../DCS.openbeta", "DCS headless", "-w", "a/b"])
def test_invalid_names_are_refused(env, name):
    with pytest.raises(HeadlessError):
        resolve(profile=name)


def test_claim_without_create_requires_existing_profile(env):
    with pytest.raises(HeadlessError, match="run prepare first"):
        claim(env.profile, create=False)
    assert not env.profile.exists()


@pytest.fixture
def miz(tmp_path):
    path = tmp_path / "Caucasus datamine.miz"
    path.write_bytes(b"PK\x03\x04fake-miz")
    return path


def server_values(lua: str) -> dict[str, str]:
    return dict(re.findall(r"^settings\.([\w.]+) = (.+)$", lua, re.M))


def test_prepare_with_mission_copies_it_and_starts_a_private_server(env, paths, miz):
    prepare(paths, hooks=[], mission=miz)

    assert [p.name for p in (env.profile / "Missions").iterdir()] == [miz.name]
    assert (env.profile / "Missions" / miz.name).read_bytes() == miz.read_bytes()
    assert miz.is_file()  # source untouched
    lua = (env.profile / "Scripts" / "dedicatedServer.lua").read_text()
    assert '(lfs.writedir() .. "Missions/Caucasus datamine.miz"):gsub("\\\\", "/")' in lua
    values = server_values(lua)
    password = values.pop("password")
    assert re.fullmatch(r'"[0-9a-f]{32}"', password)
    assert values == {
        "name": '"dcs-headless"',
        "bind_address": '"127.0.0.1"',
        "port": "10408",
        "isPublic": "false",
        "maxPlayers": "1",
        "missionList": "{ mission }",
        "listStartIndex": "1",
        "listShuffle": "false",
        "listLoop": "false",
        "advanced.resume_mode": "net.RESUME_ON_LOAD",
        "advanced.pause_on_load": "false",
        "advanced.pause_without_clients": "false",
    }
    assert "net.get_default_server_settings()" in lua and "net.start_server(settings)" in lua
    assert not (env.profile / "Config" / "serverSettings.lua").exists()


def test_prepare_without_mission_removes_previous_mission(env, paths, miz):
    prepare(paths, hooks=[], mission=miz)
    (env.profile / "Config" / "serverSettings.lua").write_text("cfg = {}\n")
    prepare(paths, hooks=[])
    assert not (env.profile / "Missions").exists()
    assert not (env.profile / "Config" / "serverSettings.lua").exists()
    assert (env.profile / "Scripts" / "dedicatedServer.lua").read_text() == DEDICATED_SERVER_LUA


@pytest.mark.parametrize("name", ["mission.lua", "missing.miz"])
def test_mission_must_be_an_existing_miz(env, paths, tmp_path, name):
    if name.endswith(".lua"):
        (tmp_path / name).write_text("")
    with pytest.raises(HeadlessError, match="mission"):
        prepare(paths, hooks=[], mission=tmp_path / name)
    assert not (env.profile / "Config").exists()


STUBS = """
local started, logged = nil, {}
package.preload["net"] = function()
    return {
        RESUME_ON_LOAD = 1,
        get_default_server_settings = function() return {advanced = {}, missionList = {"default.miz"}} end,
        start_server = function(s) started = s; return RESULT end,
    }
end
lfs = {writedir = function() return "C:\\\\Users\\\\me\\\\Saved Games\\\\DCS.headless\\\\" end}
log = {INFO = "INFO", ERROR = "ERROR", write = function(_, level, msg) logged[#logged + 1] = level .. " " .. msg end}
"""
REPORT = """
local s = started
print(s.missionList[1], #s.missionList, s.isPublic, s.bind_address, s.advanced.resume_mode)
for _, line in ipairs(logged) do print(line) end
"""


@pytest.mark.skipif(shutil.which("lua5.1") is None, reason="lua5.1 not installed")
@pytest.mark.parametrize("code", [0, 3])
def test_mission_server_lua_runs(code):
    script = f"RESULT = {code}\n" + STUBS + mission_server_lua("Missions/Syria.miz", "pw") + REPORT
    out = subprocess.run(["lua5.1", "-"], input=script, capture_output=True, text=True, check=True).stdout
    lines = out.splitlines()
    mission = "C:/Users/me/Saved Games/DCS.headless/Missions/Syria.miz"
    assert lines[0] == f"{mission}\t1\tfalse\t127.0.0.1\t1"
    assert lines[1] == f"INFO starting mission {mission}"
    failed = [line for line in lines if re.search(START_FAILED_REGEX, "DCS_HEADLESS (Main): " + line)]
    assert failed == ([] if code == 0 else [f"ERROR dcs-headless: net.start_server failed with code {code}"])
