# dcs-headless

Runs DCS World without rendering (`--norender`) from WSL, in an isolated Saved
Games profile, until a caller-specified condition is met, then stops it. Meant
for automation that installs a GameGUI hook, lets DCS load, and collects what
the hook writes.

DCS always runs as `<install>\bin-mt\DCS.exe -w <profile> --server --norender`
with a no-op `Scripts/dedicatedServer.lua`, so no mission or map is loaded;
with `--mission FILE.miz` it starts that mission on a private server instead
(see [Mission runs](#mission-runs)). Client mode is not supported: its login
dialog is invisible when headless, and server mode loads everything a GameGUI
hook needs.

A run:

1. Refuses to start if any `DCS.exe` is running.
2. Creates or resets the isolated profile (see [Profile](#profile)).
3. Copies `authdata.bin` and `network.vault` from your real profile.
4. In one PowerShell call: checks again that no `DCS.exe` runs, launches DCS
   through the Windows desktop shell, and records the PID and start time of
   the new process whose command line matches.
5. Waits until every `--wait-file` exists and every `--wait-log` regex has
   matched a `dcs.log` line. It fails early when a `--fail-log` regex matches,
   when `dcs.log` gains no bytes for `--stall-timeout` seconds, when our DCS
   exits, when another `DCS.exe` appears, or at `--timeout`.
6. Stops our DCS process(es), copies `Logs/dcs.log` to `--out`, deletes the
   auth files, and writes `--out/result.json`.

## Requirements

- WSL 2 with Windows interop (`powershell.exe` callable from WSL).
- A Windows DCS World install on a drive mounted under `/mnt/<drive>`.
- A DCS profile you have logged in with, holding `Config/authdata.bin` and
  `Config/network.vault`.
- An unlocked Windows desktop session: DCS is started through the desktop
  shell (`Shell.Application`) so it runs as the logged-in user.
- Python 3.12, [uv](https://docs.astral.sh/uv/), and optionally
  [Task](https://taskfile.dev).

## Install

```sh
git clone https://github.com/YoloWingPixie/dcs-headless
cd dcs-headless
uv sync
uv run dcs-headless paths
```

Or install the command: `uv tool install .`

## Paths

| Setting | Flag | Environment | Default |
| --- | --- | --- | --- |
| DCS install | `--install-dir` | `DCS_INSTALL_DIR` | first of `DCS World OpenBeta`, `DCS World`, `Program Files/Eagle Dynamics/...`, Steam and `Games/...` locations under `/mnt/<drive>` that contains `bin-mt/DCS.exe` |
| Auth source profile (auth files, `options.lua`) | `--auth-from` | `DCS_HEADLESS_AUTH_FROM` | first `Saved Games/DCS.openbeta` or `Saved Games/DCS` under `/mnt/<drive>/Users/<user>` holding `Config/authdata.bin` |
| Isolated profile name | `--profile` | | `DCS.headless` |

Windows paths (`D:\DCS World OpenBeta`) and WSL paths are both accepted. The
isolated profile is created next to the auth source profile, in the same
Saved Games directory. `dcs-headless paths` prints the resolved values as JSON.

## Usage

```
dcs-headless paths                 print resolved paths
dcs-headless prepare [options]     create or reset the isolated profile
dcs-headless run --out DIR ...     prepare, launch, wait, stop, clean up
dcs-headless status                state of the recorded DCS process
dcs-headless stop                  stop the recorded process, delete auth files
```

`prepare` and `run` take `--hook FILE` (repeatable), `--options-template
FILE` and `--mission FILE.miz`. `run` also takes `--wait-file REL`, `--wait-log REGEX`, `--fail-log
REGEX` (all repeatable; at least one wait condition is required), `--timeout
SECONDS` (default 600) and `--stall-timeout SECONDS` (default 120, 0 disables).
Regexes are matched against single `dcs.log` lines. `--wait-file` paths are
relative to the isolated profile and are deleted before launch, so a file left
by an earlier run cannot satisfy the condition. A stall fails with the last
`dcs.log` line in the reason, e.g. `dcs.log stalled: no new output for 120s;
last line: ...`.

Exit status of `run`: 0 when the condition was met and cleanup succeeded,
1 otherwise, 128 + signal number when interrupted. `result.json` holds the
`RunResult` fields below.

## Library API

```python
from pathlib import Path
from dcs_headless import run, RunResult, HeadlessError
from dcs_headless.paths import resolve, Paths

result: RunResult = run(
    profile="DCS.datamine",
    hooks=[Path("tools/datamine/hook/dump-globals.lua"), Path("tools/datamine/hook/serialize.lua")],
    out=Path("out/datamine"),
    wait_file="DCS.Lua.Exporter/_G/__DCS_VERSION__.lua",
    fail_log="Export aborted|records failed",
    timeout=600,
    stall_timeout=120,
)
if not result.ok:
    raise SystemExit(f"datamine failed: {result.reason}")
dump = result.profile / "DCS.Lua.Exporter" / "_G"
```

`run(profile="DCS.headless", hooks=(), out, wait_file=None, wait_log=None,
fail_log=None, timeout=600, stall_timeout=120, install_dir=None,
auth_from=None, options_template=None, mission=None) -> RunResult`. `wait_file`,
`wait_log` and `fail_log` take a string or a list of strings.

`RunResult`: `ok: bool`, `reason: str`, `profile: Path`, `pid: int | None`,
`elapsed_seconds: float`, `log: Path | None` (the copy in `out`),
`cleanup_errors: list[str]`.

Refusals raise `HeadlessError` before anything is launched: DCS already
running, invalid or unmarked profile, main profile, missing install, missing
hook or `options.lua`, mission that is not an existing `.miz`, bad wait
condition. A run that starts and fails
returns `ok=False`. SIGINT/SIGTERM/SIGHUP (main thread only) raise
`dcs_headless.Interrupted` after cleanup, with the result in `.result`.

`resolve(install_dir=None, auth_from=None, profile="DCS.headless") -> Paths`
returns `install`, `exe`, `auth_profile`, `saved_games` and `profile` without
touching anything.

### Example: dcs-world-schema datamine

The dcs-world-schema `_G` dump hook writes `DCS.Lua.Exporter/_G/` in the
profile it runs in and writes `__DCS_VERSION__.lua` there last. `task
datamine` in dcs-world-schema calls `run()` as above; the CLI equivalent is:

```sh
SCHEMA=../dcs-world-schema
uv run dcs-headless run \
  --profile DCS.datamine \
  --hook "$SCHEMA/tools/datamine/hook/dump-globals.lua" \
  --hook "$SCHEMA/tools/datamine/hook/serialize.lua" \
  --wait-file DCS.Lua.Exporter/_G/__DCS_VERSION__.lua \
  --fail-log 'Export aborted|records failed' \
  --out ./out/datamine
```

## Mission runs

Without a mission, DCS reaches the main menu with no terrain loaded, so
`world`, `land`, `Terrain` and friends have nothing to answer. `mission=`
(`--mission FILE.miz`) loads one:

- The `.miz` (must exist and end in `.miz`) is copied to the isolated
  profile's `Missions/`. The source is only read.
- `Scripts/dedicatedServer.lua` builds settings from DCS's
  `net.get_default_server_settings()` and calls `net.start_server` with that
  one mission: `missionList = { <profile>/Missions/<name>.miz }`,
  `listStartIndex = 1`, no shuffle or loop, `resume_mode = RESUME_ON_LOAD`,
  `pause_on_load = false`, `pause_without_clients = false`. Any
  `Config/serverSettings.lua` is deleted and not used.
- The server is private: `isPublic = false` (not in the public server list),
  `bind_address = "127.0.0.1"`, port 10408 (not DCS's default 10308),
  `maxPlayers = 1`, and a random 32-hex-digit password generated per run.
  It is named `dcs-headless`.
- If `net.start_server` returns an error, the script logs
  `DCS_HEADLESS ... net.start_server failed with code N` and the run fails on
  that line.

One mission per run. To cover several terrains, call `run()` once per
mission (each run relaunches DCS).

GameGUI hooks see `onSimulationStart` once the mission is loaded and running,
and can read the mission environment with `net.dostring_in("mission", code)`,
which returns the result as a string.

### Example: per-terrain pass

A hook (sketch) that dumps terrain data once the mission runs, then writes a
done-marker the run waits for:

```lua
-- terrain-dump.lua (GameGUI hook)
local out = lfs.writedir() .. "TerrainDump/"
local code = [[
  local ab = {}
  for _, a in ipairs(world.getAirbases()) do
    ab[#ab + 1] = a:getName() .. "|" .. #a:getRunways() .. "|" .. #a:getParking()
  end
  return table.concat(ab, "\n")
]]
DCS.setUserCallbacks({
  onSimulationStart = function()
    lfs.mkdir(out)
    local text, ok = net.dostring_in("mission", code)
    local f = io.open(out .. "airbases.txt", "w"); f:write(tostring(text)); f:close()
    local d = io.open(out .. "done", "w"); d:write(tostring(ok)); d:close()
  end,
})
```

```python
from pathlib import Path
from dcs_headless import run

for miz in sorted(Path("missions").glob("*.miz")):  # one empty mission per terrain
    result = run(
        profile="DCS.terrain",
        hooks=[Path("hooks/terrain-dump.lua")],
        mission=miz,
        out=Path("out/terrain") / miz.stem,
        wait_file="TerrainDump/done",
        timeout=900,
    )
    if not result.ok:
        raise SystemExit(f"{miz.name}: {result.reason}")
    # copy result.profile / "TerrainDump" before the next run overwrites it
```

## Profile

`prepare` (and `run`) create the profile directory with a `.dcs-headless`
marker file, then write:

- `Config/options.lua`: a copy of the auth source profile's
  `Config/options.lua` (read, never modified) with its single
  `["launcher"] = ...` entry set to `false`. The DCS install's default
  `MissionEditor/data/scripts/options.lua` is not used: DCS stalled after the
  startup banner with it. `--options-template FILE` uses another file. A
  source without exactly one `["launcher"]` entry is refused.
- `Config/autoexec.cfg`: empty.
- `Scripts/dedicatedServer.lua`: only writes a log line, so no server or
  mission starts; with a mission, starts it (see [Mission runs](#mission-runs)).
- `Missions/`: removed; with a mission, recreated holding only that `.miz`.
- `Scripts/Hooks/`: replaced with exactly the `--hook` files.
- `Tracks/`: created.

It removes `Logs/dcs.log`, `Config/serverSettings.lua` and any auth files
left in the profile.

## Auth policy

- `authdata.bin` and `network.vault` are copied from the auth source profile
  into the isolated profile's `Config/` at the start of each `run`, and never
  at any other time.
- They are deleted after every run: on success, on failure, on timeout, on
  exceptions, and on SIGINT/SIGTERM/SIGHUP. Signals received during cleanup
  are ignored so cleanup completes.
- A process killed with SIGKILL cannot clean up. The next `prepare`, `run` or
  `stop` deletes any auth files left in the profile.
- The files are copied as bytes; their contents are never read into
  program text or logged.
- `.gitignore` excludes `authdata.bin`, `network.vault` and `*.vault`.

## Safety

dcs-headless never stops, or asks to stop, a DCS process it did not start.

- `run` and `prepare` do nothing while any `DCS.exe` is running. Launch
  checks again in the same PowerShell call that starts DCS.
- During a run, `tasklist.exe` is polled every 3 s. If a `DCS.exe` appears
  that is not ours (for example you start the game), the run is aborted:
  only our process is stopped and the other one is left alone.
- Ours means the launched process, or a `DCS.exe` whose parent is ours, that
  started after it, and whose parent is still alive with the recorded start
  time or whose command line runs our profile (DCS restarting itself).
  PowerShell is only used to classify a PID when tasklist shows one not yet
  classified.
- The launched process is identified only if its name is `DCS.exe`, its
  executable is `<install>\bin-mt\DCS.exe`, and its command line has exactly
  one `-w <profile>`, `--server` and `--norender`. Its PID and start time are
  recorded in `<profile>/run/process.json`.
- Immediately before `Stop-Process`, control.ps1 checks the PID's name and
  start time, and either its command line (launched process) or its parent
  (descendant). A mismatch refuses the stop.
- Profiles named `DCS`, `DCS.openbeta`, `DCS.openalpha` or
  `DCS.release_server` (any case), the auth source profile, and any existing
  directory without a valid `.dcs-headless` marker are refused.
- Profile names are limited to letters, digits, `.`, `_` and `-`.
- The only files read outside the isolated profile are the auth files,
  `options.lua` (or `--options-template`), the hook files and the mission. Nothing is
  written outside the isolated profile except `--out`.

## Development

```sh
task setup   # uv sync
task test    # pytest; no DCS or Windows needed
task lint    # ruff check + format check
task ci      # all of the above
```

## License

MIT. See [LICENSE](LICENSE).
