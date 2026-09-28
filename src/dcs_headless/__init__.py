# SPDX-License-Identifier: MIT
# Copyright (c) 2026 YoloWingPixie
"""Run DCS World headless in an isolated Saved Games profile."""

from .errors import HeadlessError
from .runner import Interrupted, RunResult, run

__all__ = ["HeadlessError", "Interrupted", "RunResult", "run"]
