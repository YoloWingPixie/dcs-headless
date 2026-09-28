# SPDX-License-Identifier: MIT
# Copyright (c) 2026 YoloWingPixie
"""dcs-headless command line."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import process as proc
from .auth import remove_auth
from .errors import HeadlessError
from .paths import DEFAULT_PROFILE, Paths, resolve, to_windows
from .profile import claim, is_marked, prepare
from .runner import Interrupted, run


def _resolve(args: argparse.Namespace) -> Paths:
    return resolve(args.install_dir, args.auth_from, args.profile)


def cmd_run(args: argparse.Namespace) -> int:
    code = 0
    try:
        result = run(
            profile=args.profile,
            hooks=args.hook,
            out=args.out,
            wait_file=args.wait_file,
            wait_log=args.wait_log,
            fail_log=args.fail_log,
            timeout=args.timeout,
            stall_timeout=args.stall_timeout or None,
            install_dir=args.install_dir,
            auth_from=args.auth_from,
            options_template=args.options_template,
            mission=args.mission,
        )
    except Interrupted as exc:
        assert exc.result is not None
        result, code = exc.result, 128 + exc.signum
    print(json.dumps(result.to_json(), indent=2))
    return code or (0 if result.ok else 1)


def cmd_prepare(args: argparse.Namespace) -> int:
    paths = _resolve(args)
    claim(paths.profile, create=True)
    proc.ensure_idle()
    prepare(paths, hooks=[Path(h) for h in args.hook], options_template=args.options_template, mission=args.mission)
    print(f"prepared {paths.profile}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    paths = _resolve(args)
    claim(paths.profile, create=False)
    record = proc.read_record(paths.profile)
    if record is None:
        print(json.dumps({"state": "none"}))
        return 0
    live = proc.verify(
        record, proc.list_processes(paths.profile), exe=to_windows(paths.exe), profile_name=paths.profile.name
    )
    print(json.dumps({"state": "running" if live else "exited", "pid": record.pid, "started": record.started}))
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    paths = _resolve(args)
    claim(paths.profile, create=False)
    try:
        record = proc.read_record(paths.profile)
        state = proc.Tracker(paths, record).stop() if record else "none"
    finally:
        left = remove_auth(paths.profile)
    print(json.dumps({"state": state, "auth_removed": not left}))
    if left:
        raise HeadlessError(f"auth files could not be deleted: {', '.join(map(str, left))}")
    return 0 if state != "running" else 1


def cmd_paths(args: argparse.Namespace) -> int:
    paths = _resolve(args)
    print(
        json.dumps(
            {
                "install": str(paths.install),
                "exe": str(paths.exe),
                "auth_profile": str(paths.auth_profile),
                "saved_games": str(paths.saved_games),
                "profile": str(paths.profile),
                "profile_marked": is_marked(paths.profile),
            },
            indent=2,
        )
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--install-dir", help="DCS install dir (default: DCS_INSTALL_DIR or autodetect)")
    common.add_argument(
        "--auth-from",
        help="profile to copy authdata.bin/network.vault/options.lua from "
        "(default: DCS_HEADLESS_AUTH_FROM or autodetect)",
    )
    common.add_argument(
        "--profile",
        default=DEFAULT_PROFILE,
        help=f"isolated profile name under Saved Games (default: {DEFAULT_PROFILE})",
    )

    setup = argparse.ArgumentParser(add_help=False)
    setup.add_argument(
        "--hook",
        action="append",
        default=[],
        metavar="FILE",
        help="file to install in Scripts/Hooks (repeatable)",
    )
    setup.add_argument(
        "--options-template",
        type=Path,
        help="options.lua to use (default: <auth profile>/Config/options.lua); launcher is set to false",
    )
    setup.add_argument(
        "--mission",
        type=Path,
        metavar="FILE.miz",
        help="copy this mission into the profile and start it on a private (loopback, not public) server",
    )

    parser = argparse.ArgumentParser(prog="dcs-headless", description="Run DCS World headless from WSL.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("paths", parents=[common], help="print resolved paths as JSON")
    sub.add_parser("prepare", parents=[common, setup], help="create or reset the isolated profile")
    run_p = sub.add_parser("run", parents=[common, setup], help="prepare, launch, wait, stop, clean up")
    run_p.add_argument("--out", required=True, help="directory for dcs.log and result.json")
    run_p.add_argument(
        "--wait-file",
        action="append",
        default=[],
        metavar="REL",
        help="file (relative to the profile) that must appear; deleted before launch (repeatable)",
    )
    run_p.add_argument(
        "--wait-log",
        action="append",
        default=[],
        metavar="REGEX",
        help="regex a dcs.log line must match (repeatable)",
    )
    run_p.add_argument(
        "--fail-log",
        action="append",
        default=[],
        metavar="REGEX",
        help="regex that fails the run when a dcs.log line matches (repeatable)",
    )
    run_p.add_argument("--timeout", type=float, default=600.0, help="seconds to wait after launch (default: 600)")
    run_p.add_argument(
        "--stall-timeout",
        type=float,
        default=120.0,
        help="fail when dcs.log gains no bytes for this many seconds; 0 disables (default: 120)",
    )
    sub.add_parser("status", parents=[common], help="state of the recorded DCS process")
    sub.add_parser("stop", parents=[common], help="stop the recorded DCS process and delete auth files")
    return parser


COMMANDS = {"paths": cmd_paths, "prepare": cmd_prepare, "run": cmd_run, "status": cmd_status, "stop": cmd_stop}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except HeadlessError as exc:
        print(f"dcs-headless: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
