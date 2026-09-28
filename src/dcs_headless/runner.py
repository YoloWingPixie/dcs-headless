"""One headless run: prepare, launch, wait, stop, clean up."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import signal
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from . import process as proc
from .auth import copy_auth, remove_auth
from .errors import DcsRunning, HeadlessError
from .paths import DEFAULT_PROFILE, Paths, resolve
from .profile import START_FAILED_REGEX, check_mission, claim, prepare
from .wait import Outcome, wait_for

POLL_SECONDS = 1.0
WATCH_SECONDS = 3.0
DEFAULT_TIMEOUT = 600.0
DEFAULT_STALL = 120.0


@dataclass(frozen=True)
class RunResult:
    ok: bool
    reason: str
    profile: Path
    pid: int | None
    elapsed_seconds: float
    log: Path | None
    cleanup_errors: list[str]

    def to_json(self) -> dict[str, object]:
        data = asdict(self)
        data["profile"] = str(self.profile)
        data["log"] = str(self.log) if self.log else None
        return data


class Interrupted(Exception):
    """A signal ended the run. Cleanup has completed; ``result`` holds the outcome."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"interrupted by signal {signal.Signals(signum).name}")
        self.signum = signum
        self.result: RunResult | None = None


@contextmanager
def _signals_raise() -> Iterator[dict[str, bool]]:
    """Turn SIGINT/SIGTERM/SIGHUP into Interrupted until ``state["cleanup"]`` is set;
    after that they are ignored so cleanup runs to completion. Signal handlers can
    only be installed from the main thread; elsewhere this does nothing."""
    state = {"cleanup": False}
    if threading.current_thread() is not threading.main_thread():
        yield state
        return

    def handler(signum: int, _frame: object) -> None:
        if not state["cleanup"]:
            raise Interrupted(signum)

    signums = [signal.SIGINT, signal.SIGTERM] + ([signal.SIGHUP] if hasattr(signal, "SIGHUP") else [])
    previous = {s: signal.signal(s, handler) for s in signums}
    try:
        yield state
    finally:
        for s, h in previous.items():
            signal.signal(s, h)


def _as_list(value: str | Sequence[str] | None) -> list[str]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def _wait_file(profile: Path, rel: str) -> Path:
    if os.path.isabs(rel):
        raise HeadlessError(f"wait file must be relative to the profile: {rel}")
    path = Path(os.path.normpath(profile / rel))
    if not path.is_relative_to(profile) or path == profile:
        raise HeadlessError(f"wait file must stay inside the profile: {rel}")
    return path


def _regexes(values: list[str], what: str) -> list[re.Pattern[str]]:
    try:
        return [re.compile(v) for v in values]
    except re.error as exc:
        raise HeadlessError(f"invalid {what} regex: {exc}") from None


def _cleanup(paths: Paths, tracker: proc.Tracker | None, launched: bool, out: Path) -> list[str]:
    """Stop our DCS processes, copy dcs.log to ``out``, delete the auth files.
    Every step runs; failures are returned."""
    errors: list[str] = []
    if launched:
        try:
            if tracker is None:
                tracker = proc.find_late(paths)
            if tracker is not None and tracker.stop() == "running":
                errors.append("DCS did not exit after Stop-Process")
        except Exception as exc:  # noqa: BLE001 - cleanup must continue
            errors.append(f"stop failed: {exc}")
    try:
        log = paths.profile / "Logs" / "dcs.log"
        if log.is_file():
            shutil.copyfile(log, out / "dcs.log")
    except OSError as exc:
        errors.append(f"log capture failed: {exc}")
    left = remove_auth(paths.profile)
    if left:
        errors.append(f"auth files could not be deleted: {', '.join(map(str, left))}")
    return errors


def run(
    *,
    profile: str = DEFAULT_PROFILE,
    hooks: Sequence[Path | str] = (),
    out: Path | str,
    wait_file: str | Sequence[str] | None = None,
    wait_log: str | Sequence[str] | None = None,
    fail_log: str | Sequence[str] | None = None,
    timeout: float | None = None,
    stall_timeout: float | None = None,
    install_dir: str | Path | None = None,
    auth_from: str | Path | None = None,
    options_template: Path | str | None = None,
    mission: Path | str | None = None,
    until_stopped: bool = False,
) -> RunResult:
    """Prepare the isolated profile, launch DCS headless, wait for the condition,
    stop DCS, copy dcs.log and write ``result.json`` to ``out``, delete the auth files.

    Refusals (DCS already running, invalid or unmarked profile, missing install,
    bad arguments) raise HeadlessError before anything is launched. A failed run
    returns ``ok=False``. A signal raises Interrupted after cleanup.

    ``mission`` (a .miz) is copied into the profile's ``Missions/`` and started by
    a private dedicated server, so a map is loaded; the run fails if the server
    reports that it could not start. Without it no server or mission starts.

    ``timeout`` defaults to 600 s and ``stall_timeout`` to 120 s; a
    ``stall_timeout`` of 0 disables the stall check.

    ``until_stopped`` keeps DCS running with no wait condition, timeout or stall
    check (giving any of them is an error). Only a signal (Interrupted), a
    ``fail_log`` match, DCS exiting (``ok=False``, "DCS exited") or another DCS
    starting ends it.
    """
    ready = _regexes(_as_list(wait_log), "wait_log")
    fail = _regexes(_as_list(fail_log), "fail_log")
    mission_path = Path(mission) if mission is not None else None
    if mission_path is not None:
        check_mission(mission_path)
        fail.append(re.compile(START_FAILED_REGEX))
    wait_rel = _as_list(wait_file)
    if until_stopped and (wait_rel or ready):
        raise HeadlessError("until_stopped takes no wait_file or wait_log")
    if until_stopped and (timeout is not None or stall_timeout is not None):
        raise HeadlessError("until_stopped takes no timeout or stall_timeout")
    if not until_stopped and not wait_rel and not ready:
        raise HeadlessError("give at least one wait_file or wait_log, or until_stopped")
    out = Path(out)

    paths = resolve(install_dir, auth_from, profile)
    claim(paths.profile, create=True)
    wait_files = [_wait_file(paths.profile, rel) for rel in wait_rel]
    proc.ensure_idle()
    proc.clear_record(paths.profile)
    prepare(
        paths,
        hooks=[Path(h) for h in hooks],
        options_template=Path(options_template) if options_template else None,
        mission=mission_path,
    )
    for f in wait_files:
        f.unlink(missing_ok=True)
    out.mkdir(parents=True, exist_ok=True)

    started_at = time.monotonic()
    outcome = Outcome(False, "not started")
    launched = False
    tracker: proc.Tracker | None = None
    interrupted: Interrupted | None = None
    refused: DcsRunning | None = None
    with _signals_raise() as state:
        try:
            copy_auth(paths.auth_profile, paths.profile)
            launched = True
            tracker = proc.launch(paths)
            outcome = wait_for(
                files=wait_files,
                log=paths.profile / "Logs" / "dcs.log",
                ready=ready,
                fail=fail,
                timeout=math.inf if until_stopped else DEFAULT_TIMEOUT if timeout is None else timeout,
                stall_timeout=None if until_stopped else DEFAULT_STALL if stall_timeout is None else stall_timeout,
                until_stopped=until_stopped,
                watch=tracker.check,
                poll=POLL_SECONDS,
                watch_every=WATCH_SECONDS,
            )
        except Interrupted as exc:
            interrupted = exc
            outcome = Outcome(False, str(exc))
        except DcsRunning as exc:
            launched = False  # Launch refused before starting anything
            refused = exc
            outcome = Outcome(False, str(exc))
        except HeadlessError as exc:
            outcome = Outcome(False, str(exc))
        finally:
            state["cleanup"] = True
            errors = _cleanup(paths, tracker, launched, out)

    log = out / "dcs.log"
    result = RunResult(
        ok=outcome.ok and not errors,
        reason=outcome.reason,
        profile=paths.profile,
        pid=tracker.record.pid if tracker else None,
        elapsed_seconds=round(time.monotonic() - started_at, 1),
        log=log if log.is_file() else None,
        cleanup_errors=errors,
    )
    (out / "result.json").write_text(json.dumps(result.to_json(), indent=2) + "\n", encoding="utf-8")
    if refused is not None:
        raise refused
    if interrupted is not None:
        interrupted.result = result
        raise interrupted
    return result
