# dcs-headless

Runs DCS World from WSL with `--norender` in a separate Saved Games profile,
waits for a condition (a file appears, a `dcs.log` line matches), then stops
DCS. Built for GameGUI hooks that dump data once DCS has loaded.

DCS is started as:

```
<install>\bin-mt\DCS.exe -w <profile> --server --norender
```

Server mode only. By default `Scripts/dedicatedServer.lua` is a no-op, so DCS
sits at the menu with no map loaded. `--mission` loads one (see
[Missions](#missions)).

## Requirements

- WSL 2 with Windows interop (`powershell.exe` and `tasklist.exe` on the PATH).
- DCS World installed on a Windows drive (`/mnt/<drive>`).
- A DCS profile you have logged in with: `Config/authdata.bin`,
  `Config/network.vault` and `Config/options.lua`.
- A logged-in, unlocked Windows desktop. DCS is launched through
  `Shell.Application` so it runs as you.
- Python 3.12+ and [uv](https://docs.astral.sh/uv/). [Task](https://taskfile.dev) is optional.

## Install

```sh
git clone https://github.com/YoloWingPixie/dcs-headless
cd dcs-headless
uv sync
uv run dcs-headless paths
```

Or `uv tool install .` to get `dcs-headless` on the PATH.

## Paths

| Setting | Flag | Env | Default |
| --- | --- | --- | --- |
| DCS install | `--install-dir` | `DCS_INSTALL_DIR` | first `DCS World OpenBeta`, `DCS World`, `Program Files/Eagle Dynamics/...`, Steam or `Games/...` dir under `/mnt/<drive>` with `bin-mt/DCS.exe` |
| Source profile | `--auth-from` | `DCS_HEADLESS_AUTH_FROM` | first `Users/<user>/Saved Games/DCS.openbeta` or `.../DCS` with `Config/authdata.bin` |
| Isolated profile | `--profile` | | `DCS.headless` |

Windows (`D:\DCS World`) and WSL paths both work. The isolated profile is
created next to the source profile. `dcs-headless paths` prints the resolved
paths as JSON.

## CLI

```
dcs-headless paths                 print resolved paths
dcs-headless prepare [options]     create or reset the isolated profile
dcs-headless run --out DIR ...     prepare, launch, wait, stop, clean up
dcs-headless status                state of the recorded DCS process
dcs-headless stop                  stop the recorded DCS process, delete auth files
```

All commands take `--install-dir`, `--auth-from` and `--profile`.

| Flag | Commands | |
| --- | --- | --- |
| `--hook FILE` | prepare, run | install into `Scripts/Hooks/`; repeatable |
| `--options-template FILE` | prepare, run | use instead of the source profile's `options.lua` |
| `--mission FILE.miz` | prepare, run | start this mission on a private server |
| `--out DIR` | run | required; gets `dcs.log` and `result.json` |
| `--wait-file REL` | run | file relative to the profile that must exist; deleted before launch; repeatable |
| `--wait-log REGEX` | run | must match a `dcs.log` line; repeatable |
| `--fail-log REGEX` | run | fails the run on a matching line; repeatable |
| `--timeout S` | run | default 600 |
| `--stall-timeout S` | run | fail if `dcs.log` doesn't grow for S seconds; default 120, 0 disables |

`run` needs at least one `--wait-file` or `--wait-log`. It succeeds once every
wait file exists and every wait regex has matched. It fails on a `--fail-log`
match, a stall, a timeout, DCS exiting, or another `DCS.exe` starting.

`run` prints the result as JSON and writes the same to `--out/result.json`.

Exit status: 0 when the condition was met and cleanup was clean, 1 when the
run failed or was refused (message on stderr), 128+N when interrupted by
signal N (cleanup still runs).

`stop` exits 1 if a process is still running or the auth files could not be
deleted.

Example (the dcs-world-schema `_G` dump, which `task datamine` there runs via
the library):

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

## Library

```python
from pathlib import Path
from dcs_headless import run

result = run(
    profile="DCS.datamine",
    hooks=[Path("hook/dump-globals.lua"), Path("hook/serialize.lua")],
    out=Path("out/datamine"),
    wait_file="DCS.Lua.Exporter/_G/__DCS_VERSION__.lua",
    fail_log="Export aborted|records failed",
)
if not result.ok:
    raise SystemExit(result.reason)
dump = result.profile / "DCS.Lua.Exporter" / "_G"
```

`run()` takes keyword arguments only:

```python
run(*, profile="DCS.headless", hooks=(), out, wait_file=None, wait_log=None,
    fail_log=None, timeout=600, stall_timeout=120, install_dir=None,
    auth_from=None, options_template=None, mission=None) -> RunResult
```

`wait_file`, `wait_log` and `fail_log` take a string or a list of strings.
`stall_timeout=None` or `0` disables the stall check.

`RunResult`:

| Field | Type | |
| --- | --- | --- |
| `ok` | `bool` | condition met and no cleanup errors |
| `reason` | `str` | `condition met` or why it failed |
| `profile` | `Path` | isolated profile |
| `pid` | `int \| None` | launched DCS PID |
| `elapsed_seconds` | `float` | |
| `log` | `Path \| None` | `dcs.log` copied to `out` |
| `cleanup_errors` | `list[str]` | |

Errors:

- `HeadlessError` is raised before launch for: DCS already running, bad or
  reserved profile name, existing profile without the `.dcs-headless` marker,
  install not found, missing hook or `options.lua`, duplicate hook names,
  mission not an existing `.miz`, no wait condition, bad regex, wait file
  outside the profile.
- If a `DCS.exe` appears between prepare and launch, `run` cleans up, writes
  `result.json` and raises `DcsRunning` (a `HeadlessError`).
- Anything after that (missing auth files, DCS not appearing within 15 s,
  timeout, ...) returns `ok=False`.
- SIGINT/SIGTERM/SIGHUP raise `Interrupted` after cleanup; the result is in
  `.result` and the signal in `.signum`. Handlers are only installed when
  `run` is called from the main thread.

`dcs_headless.paths.resolve(install_dir=None, auth_from=None,
profile="DCS.headless") -> Paths` resolves paths without writing anything.
`Paths` has `install`, `auth_profile`, `profile`, `exe` and `saved_games`.

## Profile

`prepare` (and `run`) create the profile with a `.dcs-headless` marker and
reset it:

- `Config/options.lua`: copy of the source profile's `options.lua` (or
  `--options-template`) with `["launcher"]` set to `false`. The file must
  contain exactly one `["launcher"]` entry. The install's default
  `options.lua` is not used because DCS stalls with it.
- `Config/autoexec.cfg`: empty.
- `Scripts/dedicatedServer.lua`: no-op, or the mission starter.
- `Scripts/Hooks/`: exactly the `--hook` files.
- `Missions/`: removed; recreated with only the `.miz` when `--mission` is given.
- `Tracks/`: created.
- Removed: `Logs/dcs.log`, `Config/serverSettings.lua`, leftover auth files.

## Missions

`--mission FILE.miz` (or `mission=`) copies the file into `Missions/` and
starts it on a local server, so terrain APIs work. One mission per run;
call `run` once per map. Copy anything you need out of the profile between
runs, since the next run resets it.

The server is built from `net.get_default_server_settings()`:

- name `dcs-headless`, `isPublic = false`, `bind_address = "127.0.0.1"`,
  port 10408, `maxPlayers = 1`, random 32-hex password per run
- single-mission list, no shuffle or loop
- `resume_mode = RESUME_ON_LOAD`, `pause_on_load = false`,
  `pause_without_clients = false`

If `net.start_server` fails, the script logs
`DCS_HEADLESS ... net.start_server failed with code N` and the run fails on
that line. Hooks get `onSimulationStart` once the mission is running.

## Auth files

`authdata.bin` and `network.vault` are copied from the source profile into
the isolated profile's `Config/` at the start of each `run`, and deleted when
it ends, whether it succeeds, fails or is interrupted. Signals during cleanup
are ignored. If the process is SIGKILLed, the next `prepare`, `run` or `stop`
deletes them. `.gitignore` excludes `authdata.bin`, `network.vault` and
`*.vault`.

## Other DCS processes

dcs-headless only stops DCS processes it started.

- `prepare` and `run` refuse while any `DCS.exe` is running, and launch checks
  again in the same PowerShell call that starts DCS.
- The launched process must be `<install>\bin-mt\DCS.exe` with exactly one
  `-w <profile>`, plus `--server` and `--norender`. Its PID and start time go
  in `<profile>/run/process.json`. A `DCS.exe` started by it (DCS restarting
  itself) is also treated as ours.
- During a run `tasklist.exe` is checked every 3 s. If any other `DCS.exe`
  appears, the run aborts and stops only its own process.
- Before each `Stop-Process`, `control.ps1` re-checks the PID's name, start
  time and command line (or parent). On mismatch it refuses.
- Refused profiles: `DCS`, `DCS.openbeta`, `DCS.openalpha`,
  `DCS.release_server` (any case), the source profile, and any existing
  directory without a valid marker. Names are limited to letters, digits,
  `.`, `_` and `-`.
- Nothing is written outside the isolated profile except `--out`.

## Development

```sh
task setup   # uv sync
task test    # pytest, no DCS or Windows needed
task lint    # ruff check + format check
task fmt     # ruff fix + format
task ci      # setup, lint, test
```

## License

MIT
