from pathlib import Path

import pytest

from dcs_headless import HeadlessError
from dcs_headless import paths as p


def test_to_wsl_translates_drive_paths(monkeypatch):
    monkeypatch.setattr(p, "MOUNT_ROOT", Path("/mnt"))
    assert p.to_wsl("D:\\DCS World OpenBeta") == Path("/mnt/d/DCS World OpenBeta")
    assert p.to_wsl("C:/Users/shepard/Saved Games/") == Path("/mnt/c/Users/shepard/Saved Games")
    assert p.to_wsl("e:") == Path("/mnt/e")
    assert p.to_wsl("/mnt/d/already") == Path("/mnt/d/already")


def test_to_windows_translates_mount_paths(monkeypatch):
    monkeypatch.setattr(p, "MOUNT_ROOT", Path("/mnt"))
    assert p.to_windows("/mnt/d/DCS World OpenBeta/bin-mt/DCS.exe") == "D:\\DCS World OpenBeta\\bin-mt\\DCS.exe"
    assert p.to_windows(Path("/mnt/c/Users/shepard/Saved Games/")) == "C:\\Users\\shepard\\Saved Games"
    assert p.to_windows("/mnt/c") == "C:\\"


@pytest.mark.parametrize("bad", ["/home/user/x", "/mnt", "/mnt/wsl/x", "/tmp/mnt/c/x"])
def test_to_windows_rejects_non_drive_paths(monkeypatch, bad):
    monkeypatch.setattr(p, "MOUNT_ROOT", Path("/mnt"))
    with pytest.raises(HeadlessError):
        p.to_windows(bad)


def test_round_trip(monkeypatch):
    monkeypatch.setattr(p, "MOUNT_ROOT", Path("/mnt"))
    win = "C:\\Users\\shepard\\Saved Games\\DCS.headless\\run\\control.ps1"
    assert p.to_windows(p.to_wsl(win)) == win


def test_autodetect(env):
    paths = p.resolve(None, None, "DCS.headless")
    assert paths.install == env.install
    assert paths.auth_profile == env.user_profile
    assert paths.profile == env.saved_games / "DCS.headless"
    assert paths.exe == env.install / "bin-mt" / "DCS.exe"


def test_env_overrides_accept_windows_paths(env, monkeypatch):
    other = env.mnt / "e" / "DCS"
    (other / "bin-mt").mkdir(parents=True)
    (other / "bin-mt" / "DCS.exe").write_bytes(b"MZ")
    monkeypatch.setenv("DCS_INSTALL_DIR", "E:\\DCS")
    monkeypatch.setenv("DCS_HEADLESS_AUTH_FROM", "C:\\Users\\shepard\\Saved Games\\DCS.openbeta")
    paths = p.resolve(None, None, "DCS.x")
    assert paths.install == other
    assert paths.auth_profile == env.user_profile


def test_install_dir_without_exe_is_refused(env):
    with pytest.raises(HeadlessError, match="bin-mt"):
        p.resolve(str(env.mnt / "c"), None, "DCS.x")


def test_profile_equal_to_auth_source_is_refused(env):
    custom = env.saved_games / "DCS.custom"
    custom.mkdir()
    with pytest.raises(HeadlessError, match="auth source"):
        p.resolve(None, str(custom), "dcs.CUSTOM")


@pytest.mark.parametrize("name", ["a/b", "../x", "DCS", "DCS World"])
def test_resolve_validates_profile_name(env, name):
    with pytest.raises(HeadlessError):
        p.resolve(None, None, name)
