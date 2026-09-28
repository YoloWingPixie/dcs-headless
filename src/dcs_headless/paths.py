"""WSL <-> Windows path translation and DCS path resolution."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .errors import HeadlessError

MOUNT_ROOT = Path("/mnt")

INSTALL_CANDIDATES = (
    "DCS World OpenBeta",
    "DCS World",
    "Program Files/Eagle Dynamics/DCS World OpenBeta",
    "Program Files/Eagle Dynamics/DCS World",
    "Program Files (x86)/Steam/steamapps/common/DCSWorld",
    "SteamLibrary/steamapps/common/DCSWorld",
    "Games/DCS World OpenBeta",
    "Games/DCS World",
)
AUTH_PROFILE_CANDIDATES = ("DCS.openbeta", "DCS")
EXE = Path("bin-mt") / "DCS.exe"
DEFAULT_PROFILE = "DCS.headless"
RESERVED = frozenset({"dcs", "dcs.openbeta", "dcs.openalpha", "dcs.release_server"})

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_WIN_PATH = re.compile(r"^([A-Za-z]):(?:[\\/](.*))?$")


def check_name(name: str) -> None:
    if not _NAME.match(name):
        raise HeadlessError(f"invalid profile name {name!r}: use letters, digits, '.', '_', '-'")
    if name.casefold() in RESERVED:
        raise HeadlessError(f"refusing to use the main DCS profile {name!r}")


def to_wsl(path: str) -> Path:
    """``D:\\DCS World`` -> ``/mnt/d/DCS World``; other paths are returned as-is."""
    m = _WIN_PATH.match(path)
    if not m:
        return Path(path)
    rest = [p for p in re.split(r"[\\/]", m.group(2) or "") if p]
    return MOUNT_ROOT.joinpath(m.group(1).lower(), *rest)


def to_windows(path: Path | str) -> str:
    """``/mnt/d/DCS World`` -> ``D:\\DCS World``. Paths outside a drive mount are an error."""
    p = PurePosixPath(os.path.abspath(path))
    try:
        parts = p.relative_to(MOUNT_ROOT).parts
    except ValueError:
        raise HeadlessError(f"not on a Windows drive mount: {path}") from None
    if not parts or len(parts[0]) != 1 or not parts[0].isalpha():
        raise HeadlessError(f"not on a Windows drive mount: {path}")
    return parts[0].upper() + ":\\" + "\\".join(parts[1:])


def _drives() -> list[Path]:
    return sorted(p for p in MOUNT_ROOT.glob("?") if p.is_dir())


def _from_setting(flag: str | Path | None, env: str) -> Path | None:
    value = flag or os.environ.get(env)
    return to_wsl(str(value)) if value else None


def resolve_install_dir(flag: str | Path | None = None) -> Path:
    """``--install-dir``, else ``DCS_INSTALL_DIR``, else the first known install
    location under ``/mnt/<drive>`` that contains ``bin-mt/DCS.exe``."""
    given = _from_setting(flag, "DCS_INSTALL_DIR")
    if given is not None:
        if not (given / EXE).is_file():
            raise HeadlessError(f"no {EXE} under install dir {given}")
        return given
    for drive in _drives():
        for rel in INSTALL_CANDIDATES:
            if (drive / rel / EXE).is_file():
                return drive / rel
    raise HeadlessError("DCS install not found; pass --install-dir or set DCS_INSTALL_DIR")


def resolve_auth_profile(flag: str | Path | None = None) -> Path:
    """The user's real profile that auth files are read from: ``--auth-from``,
    else ``DCS_HEADLESS_AUTH_FROM``, else the first ``DCS.openbeta``/``DCS``
    under ``/mnt/<drive>/Users/<user>/Saved Games`` holding ``Config/authdata.bin``."""
    given = _from_setting(flag, "DCS_HEADLESS_AUTH_FROM")
    if given is not None:
        if not given.is_dir():
            raise HeadlessError(f"auth profile does not exist: {given}")
        return given
    skip = {"Public", "Default", "Default User", "All Users"}
    for drive in _drives():
        users = drive / "Users"
        if not users.is_dir():
            continue
        for user in sorted(p.name for p in users.iterdir() if p.is_dir()):
            if user in skip:
                continue
            for name in AUTH_PROFILE_CANDIDATES:
                candidate = users / user / "Saved Games" / name
                if (candidate / "Config" / "authdata.bin").is_file():
                    return candidate
    raise HeadlessError("no DCS profile with Config/authdata.bin found; pass --auth-from or set DCS_HEADLESS_AUTH_FROM")


@dataclass(frozen=True)
class Paths:
    install: Path
    auth_profile: Path
    profile: Path

    @property
    def exe(self) -> Path:
        return self.install / EXE

    @property
    def saved_games(self) -> Path:
        return self.profile.parent


def resolve(
    install_dir: str | Path | None = None,
    auth_from: str | Path | None = None,
    profile: str = DEFAULT_PROFILE,
) -> Paths:
    """Resolve all paths. The isolated profile is a sibling of the auth profile.
    This is the only place a profile name becomes a path, so it is validated here."""
    check_name(profile)
    auth = resolve_auth_profile(auth_from)
    isolated = auth.parent / profile
    if str(isolated.absolute()).casefold() == str(auth.absolute()).casefold():
        raise HeadlessError(f"isolated profile {profile} is the auth source profile")
    return Paths(install=resolve_install_dir(install_dir), auth_profile=auth, profile=isolated)
