"""Poll for a run's completion condition: files, dcs.log patterns, timeout."""

from __future__ import annotations

import codecs
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Outcome:
    ok: bool
    reason: str


class LogTail:
    """Complete new lines appended to a log file since the previous call."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self.partial = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def new_lines(self) -> list[str]:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return []
        if size < self.offset:
            self.offset, self.partial = 0, ""
            self.decoder.reset()
        with self.path.open("rb") as fh:
            fh.seek(self.offset)
            data = fh.read()
        self.offset += len(data)
        lines = (self.partial + self.decoder.decode(data)).split("\n")
        self.partial = lines.pop()
        return [line.rstrip("\r") for line in lines]


def wait_for(
    *,
    files: list[Path],
    log: Path,
    ready: list[re.Pattern[str]],
    fail: list[re.Pattern[str]],
    timeout: float,
    stall_timeout: float | None = None,
    until_stopped: bool = False,
    watch: Callable[[], str | None] | None = None,
    poll: float = 1.0,
    watch_every: float = 3.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Outcome:
    """Succeed once every file exists and every ``ready`` pattern has matched a
    log line. Fail on the first log line matching a ``fail`` pattern, when
    ``watch()`` returns a reason, when the log gains no bytes for
    ``stall_timeout`` seconds, or after ``timeout`` seconds. With
    ``until_stopped`` it never succeeds; only a failure ends it."""
    tail = LogTail(log)
    pending = list(ready)
    start = clock()
    next_watch = start + watch_every
    last_growth, last_offset, last_line = start, tail.offset, ""
    while True:
        for line in tail.new_lines():
            for pattern in fail:
                if pattern.search(line):
                    return Outcome(False, f"dcs.log matched failure pattern {pattern.pattern!r}: {line.strip()}")
            pending = [p for p in pending if not p.search(line)]
            if line.strip():
                last_line = line.strip()
        missing = [str(f) for f in files if not f.exists()]
        if not until_stopped and not pending and not missing:
            return Outcome(True, "condition met")
        now = clock()
        if tail.offset != last_offset:
            last_growth, last_offset = now, tail.offset
        if now - start >= timeout:
            waiting = missing + [f"log /{p.pattern}/" for p in pending]
            return Outcome(False, f"timed out after {timeout:g}s waiting for: {', '.join(waiting)}")
        if stall_timeout and now - last_growth >= stall_timeout:
            last = (last_line or tail.partial.strip()) or "(dcs.log is empty or missing)"
            return Outcome(False, f"dcs.log stalled: no new output for {stall_timeout:g}s; last line: {last}")
        if watch is not None and now >= next_watch:
            reason = watch()
            if reason:
                return Outcome(False, reason)
            next_watch = now + watch_every
        sleep(poll)
