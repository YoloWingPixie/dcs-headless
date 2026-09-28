# SPDX-License-Identifier: MIT
# Copyright (c) 2026 YoloWingPixie
"""Copy DCS login files into the isolated profile and remove them again.

File contents are never read into Python strings or logged.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .errors import HeadlessError

AUTH_FILES = ("authdata.bin", "network.vault")


def copy_auth(source_profile: Path, profile: Path) -> None:
    """Copy ``Config/{authdata.bin,network.vault}`` from ``source_profile`` into
    ``profile``, which the caller has claimed."""
    sources = [source_profile / "Config" / name for name in AUTH_FILES]
    missing = [str(p) for p in sources if not p.is_file()]
    if missing:
        raise HeadlessError(f"auth files missing: {', '.join(missing)}")
    (profile / "Config").mkdir(parents=True, exist_ok=True)
    for src in sources:
        shutil.copyfile(src, profile / "Config" / src.name)


def remove_auth(profile: Path) -> list[Path]:
    """Delete the auth files from ``profile``. Returns the paths that still exist."""
    left: list[Path] = []
    for name in AUTH_FILES:
        path = profile / "Config" / name
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        if path.exists():
            left.append(path)
    return left
