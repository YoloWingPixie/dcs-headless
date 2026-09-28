# SPDX-License-Identifier: MIT
# Copyright (c) 2026 YoloWingPixie
"""Exceptions."""


class HeadlessError(Exception):
    """A refused or failed dcs-headless operation."""


class DcsRunning(HeadlessError):
    """A DCS.exe process is running; nothing was started or modified."""
