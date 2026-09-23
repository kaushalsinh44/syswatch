# SysWatch

A background logger + dashboard that tracks system behavior over time and flags anomalies you
can investigate. All four phases are built: baseline logging, a background service, anomaly
detection, and a local dashboard with a "case file" drill-down -- plus three extras: automatic
daily anomaly detection, weekly Markdown case reports, and long-term battery health tracking.

Three optional background tasks keep everything current without manual runs:

| Task | Script | Trigger |
|------|--------|---------|
| Telemetry logging | `service/setup_scheduled_task.ps1` | at logon, runs forever |
| Anomaly detection | `service/setup_daily_anomaly_task.ps1` | daily, 02:00 |
| Weekly case report | `service/setup_weekly_report_task.ps1` | Mondays, 02:30 |

## Requirements

- Python **3.12** (this machine also has 3.14, but 3.12 was chosen deliberately: it has full,
  mature wheel support for every dependency this project will need, including numpy/Flask in
  later phases, which a brand-new Python release may lag on).
- Windows 11 is the tested/primary platform. The Python code itself (`logger.py`) is
  cross-platform via `psutil`; only the background-service wiring is Windows-specific for now
  (macOS/Linux service templates are included but untested -- see below).

## Setup

```powershell
py -3.12 -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If activation is blocked by PowerShell's execution policy, run once:
```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

## Running manually (foreground)

```powershell
python logger.py
python logger.py --interval 30   # override the sampling interval (seconds, default 45)
```

Watch the console fill up with one summary line per sample. Stop with Ctrl+C -- shutdown is
clean since every sample is committed to the database individually (nothing is buffered in
memory across intervals).

Inspect the database directly with the `sqlite3` CLI, or a GUI tool like
[DB Browser for SQLite](https://sqlitebrowser.org/):

```powershell
sqlite3 data\telemetry.db "SELECT * FROM samples ORDER BY id DESC LIMIT 5;"
```

## Schema

**`samples`** -- one row per sampling interval:

| column             | meaning                                                              |
|--------------------|-----------------------------------------------------------------------|
| `timestamp`        | ISO-8601 UTC                                                          |
| `interval_sec`     | actual elapsed seconds since the previous sample (not the configured interval -- used so later rate math divides by reality) |
| `cpu_pct`          | system-wide CPU %                                                    |
| `mem_pct`          | system-wide memory %                                                  |
| `disk_read_bytes`  | disk bytes read **since the last sample** (delta, not cumulative)     |
| `disk_write_bytes` | disk bytes written since the last sample                              |
| `net_sent_bytes`   | network bytes sent since the last sample                              |
| `net_recv_bytes`   | network bytes received since the last sample                          |
| `battery_pct`      | battery % (`NULL` if this machine has no battery sensor)              |
| `charging`         | 0/1 (`NULL` if unknown or no battery)                                 |

**`process_samples`** -- up to 10 rows per sample (top 5 by CPU + top 5 by memory; a process
can legitimately appear in both sets):

| column         | meaning                                  |
|----------------|-------------------------------------------|
| `sample_id`    | FK to `samples.id`                        |
| `timestamp`    | denormalized copy of the parent sample's timestamp (avoids a join for common queries) |
| `process_name` | e.g. `chrome.exe`                          |
| `pid`          | process ID at sample time                  |
| `cpu_pct`      | per-process CPU % (psutil reports this per-core-summed, so it can exceed 100 on multi-core machines -- this is expected and correct) |
| `mem_pct`      | per-process memory %                       |

`disk_io`/`net_io` are stored as **per-interval deltas, split by direction**, not cumulative
totals -- `psutil`'s counters are monotonic since boot, so raw totals would need cross-row
subtraction (with edge cases around counter resets on reboot/sleep) just to get a usable rate.
Deltas make every row directly usable for the rolling-average anomaly checks in Phase 3, and
splitting by direction (read vs write, sent vs received) preserves signal that a combined
number would lose -- an upload spike and a download spike are different mysteries.

**`anomalies`** -- written by `anomaly.py`, read by the dashboard (see Phase 3 below):
`target_date`, `timestamp`, `category`, `subject` (process name or I/O metric, if applicable),
`value`, `baseline_mean`, `baseline_std`, `z_score`, `description`.

**`battery_health`** -- written by `logger.py` about once a day (see Battery health tracking
below): `timestamp`, `full_charge_capacity_mwh`, `design_capacity_mwh` (nullable), `cycle_count`,
`health_pct` (nullable, only set when design capacity is available).

**`process_events`** -- written by `logger.py` every sampling interval: `timestamp`, `event`
(`started` or `stopped`), `process_name`, `pid`. Built by diffing `psutil.pids()` against the
previous loop's snapshot (seeded at startup so the first sample doesn't flag every already-running
process as "started"). This is what powers the app-launch correlation in the case file view --
it can't be backfilled for dates before this feature was added, since it depends on catching the
transition in real time.

## Phase 2 -- running as a background service (Windows)

`logger.py` contains its own infinite sampling loop, so the scheduled task fires **once at
logon** and lets the process run forever -- it is not a recurring "run every N seconds" task
(that would spawn duplicate concurrent writers).

```powershell
.\service\setup_scheduled_task.ps1
```

This registers a Scheduled Task named `LaptopMysteryDetectiveLogger` that:
- starts at logon, running `venv\Scripts\pythonw.exe logger.py` (windowless, so no console
  flash), with stdout/stderr redirected to `service\logger_output.log` / `logger_error.log`
- has no execution time limit (Task Scheduler's default 3-day limit is disabled -- otherwise it
  would silently kill the logger right when multi-day data collection matters most)
- retries up to 3 times, 1 minute apart, if the process crashes
- only runs while you're logged in (no stored password needed)

The script also starts the task immediately so you can verify it works without logging off/on.

**Day-to-day commands:**

```powershell
# Check status
Get-ScheduledTask -TaskName LaptopMysteryDetectiveLogger | Select-Object State
Get-Process pythonw

# Tail live output
Get-Content service\logger_output.log -Wait -Tail 20

# Stop (task stays registered, restarts at next logon)
Stop-ScheduledTask -TaskName LaptopMysteryDetectiveLogger

# Fully remove
Unregister-ScheduledTask -TaskName LaptopMysteryDetectiveLogger -Confirm:$false
```

### macOS / Linux (untested templates)

`service/com.syswatch.logger.plist` (launchd) and `service/syswatch.service`
(systemd, per-user unit) are provided as documented starting points if you ever run this on
macOS or Linux. Both are marked untested -- Windows is the only platform actually verified.
Edit the placeholder paths before use; install instructions are in comments within each file.

## Phase 3 -- anomaly detection

```powershell
python anomaly.py                        # detect anomalies for today (UTC), vs. a 14-day baseline
python anomaly.py --date 2026-09-15      # detect for a specific past date
python anomaly.py --all                  # recompute for every day in the database (re-run after backfilling old data)
python anomaly.py --lookback-days 7      # use a shorter baseline window
```

For each process/hour-of-day and each system-wide I/O metric/hour-of-day, it computes the mean
and standard deviation across the lookback window (excluding the target day itself) and flags a
reading as anomalous when it's more than `Z_THRESHOLD` (3.0) standard deviations above that
baseline **and** the absolute difference clears a minimum floor (`MIN_ABS_DELTA` in `anomaly.py`).
That floor matters: a process that's almost always idle has a near-zero standard deviation, so
without it, any nonzero reading would produce an enormous z-score for a trivial blip. Consecutive
flagged samples for the same process/metric are collapsed into a single "episode" (peak reading
kept, time range recorded) so one ten-minute spike shows up as one row, not twenty.

Three categories are flagged: `process_cpu` / `process_mem` (a process unusually busy for this
hour), `battery_drain` (an on-battery session draining faster than usual), and `disk_io` /
`net_io` (a system-wide I/O spike for this hour). Results are written to an `anomalies` table
(re-running for a date replaces that date's rows, so it's safe to re-run).

`Z_THRESHOLD` was raised from the spec's initial suggestion of ~2 to 3.0 after testing against a
full month of real data -- at 2.0, ordinary bursty-but-normal processes (`System`, `dwm.exe`,
Windows Defender's `MsMpEng.exe`) produced hundreds of low-value flags per day. `System Idle
Process` is excluded entirely: it measures unused CPU capacity, so a "spike" in it is the
*inverse* of a load spike, not a signal.

### Running detection automatically

```powershell
.\service\setup_daily_anomaly_task.ps1
```

Registers a daily Scheduled Task (`LaptopMysteryDetectiveAnomalyDetection`, 02:00 local by
default) that runs `anomaly.py --all`, so the anomalies table -- and therefore the dashboard --
stays current without manual runs. `--all` is used rather than just "today" since it's cheap
(a few seconds per day analyzed against a 14-day baseline) and idempotent.

### Desktop notifications

Any time `anomaly.py` finds an anomaly with z-score >= `NOTIFY_Z_THRESHOLD` (8.0) **for the
current day**, it fires a Windows toast notification via PowerShell's WinRT toast API -- no
extra pip dependency, same subprocess pattern as the battery-health WMI query. It never notifies
while backfilling old history (`--all` only notifies for today's slice of the run, and `--date`
for a past date never notifies), so re-analyzing a month of history won't trigger a notification
storm. A missed/failed notification never breaks detection itself -- it's entirely best-effort.

## Phase 4 -- dashboard

```powershell
python dashboard/app.py
```

Opens a local Flask app (default `http://127.0.0.1:5050`, override with `$env:PORT`) with:
- live stat cards (CPU/mem/battery right now, mysteries today, apps launched, days
  investigated) that auto-refresh every 20s, and a CPU/memory/battery timeline chart (last 24h
  or 7d, or a specific historical day/week via the anomaly date picker below)
- a **"Top processes right now"** panel with an **End task** button per process -- confirms
  before acting, and refuses to touch anything in a small protected-process list (`System`,
  `explorer.exe`, `python.exe`/`pythonw.exe` so it can't kill its own logger/dashboard, etc.)
  both client- and server-side. The server also re-verifies the PID still matches the expected
  process name right before killing, in case it already exited and the PID was reused.
- today's flagged anomalies (or the most recent date that has any), ranked by z-score, capped
  at the top 20 with a note on how many more exist
- a **case file** view per anomaly (`/case/<id>`): every sample and process reading in the
  +/-15 minute window around the flagged spike, which apps launched or exited in that window
  (bursts of near-simultaneous launches from one app -- common with Electron apps like
  Discord/Chrome/Steam -- are grouped into one row with a `x<count>` badge instead of repeating
  the same line), and any other anomalies flagged in that same window -- this is the actual
  investigation payoff. For example, a large `msedgewebview2.exe` CPU spike in this dataset
  lines up with a FiveM game session, Discord, and a Windows Defender scan all active in the
  same window -- a real, explainable mystery solved.
- **notes on anomalies**: once you've investigated a pattern, annotate it right from the case
  file ("just Discord launching, normal"). The note is keyed by `(category, subject)` -- e.g.
  "process_cpu / Discord.exe" -- not a specific anomaly row, since `anomaly.py --all` reruns
  delete and re-insert the anomalies table per date, so row ids aren't stable across reruns but
  the *kind* of pattern is. The note then shows up automatically on every future occurrence of
  that same pattern, on both the case file and the main anomaly list, so recurring
  already-understood mysteries don't need re-investigating each time.

The dashboard never writes to the database (WAL mode makes it safe to run alongside the logger)
except for the notes you add, which are the one piece of state the dashboard itself owns. "End
task" also has a real side effect outside the database -- it terminates an actual process on
your machine, guarded by the confirmation + protected-name checks described above.

## Trend insights

```powershell
python trends.py                        # compute trends as of today
python trends.py --as-of 2026-09-15      # compute as of a specific past date
```

For every process, compares its average CPU/memory over the last 7 days against the 21 days
before that (needs ~4 weeks of history to say anything), and reports anything that changed by
25% or more. This is the one thing a generic "PC optimizer" fundamentally can't do -- those
tools have no memory of what your machine looked like last month, so they can't tell you a
process has been quietly getting worse over time. View it at `/trends`, or see the single
biggest mover as a teaser on the main dashboard. Not currently wired into a scheduled task --
run it manually (or add a call to it in `service/setup_daily_anomaly_task.ps1` if you want it
automatic).

## Battery health tracking

`logger.py` also samples battery **full-charge capacity** and **cycle count** via WMI
(`root\wmi`'s `BatteryFullChargedCapacity` / `BatteryCycleCount` classes), about once a day --
capacity drifts slowly, so there's no point checking every 45s like the rest of the metrics.
This is Windows-only and best-effort: `BatteryStaticData` (design capacity, needed for a clean
health %) isn't exposed by every battery driver -- it fails outright on the machine this was
built on -- so `design_capacity_mwh` / `health_pct` are stored as `NULL` when unavailable rather
than skipping the sample. A declining full-charge-capacity trend over months is itself the
degradation signal even without a fixed denominator.

View it at `/battery` in the dashboard: a chart of full-charge capacity and cycle count (plus
health % when available) over time. Meaningful trends need months of data -- this only just
started collecting, so don't expect to see anything interesting for a while.

## Weekly case reports

```powershell
python report.py                          # report for the most recently completed Mon-Sun week
python report.py --week-start 2026-09-14  # report for a specific week
```

Summarizes a week's flagged anomalies (from the `anomalies` table -- run `anomaly.py` first) into
a readable Markdown file in `reports/`: top mysteries ranked by z-score with links into the
dashboard's case-file view, a breakdown by category, the most-flagged processes, and any battery
drain sessions. It's purely a summary of what `anomaly.py` already found -- no new analysis.

To generate one automatically every Monday for the week that just ended:

```powershell
.\service\setup_weekly_report_task.ps1
```

Registers `LaptopMysteryDetectiveWeeklyReport` (Mondays at 02:30 local by default -- after the
02:00 daily anomaly task, so the previous day's data is finalized first).

## Testing

```powershell
pip install pytest  # already in requirements.txt
pytest               # runs in ~1-2 seconds
```

Unit tests for the core logic -- z-score anomaly detection, episode collapsing, trend
comparison, the kill-process safety checks (protected-name denylist, PID-reuse guard, CSRF
guard), and the fresh/empty-database handling. Every test uses a throwaway temp database
(`tests/conftest.py`'s `temp_db_path` fixture) -- none of them ever touch your real
`data/telemetry.db`.

Two bugs were actually found by writing these tests, not just guessed at: a `zscore()` edge
case where a *perfectly* constant historical baseline (zero variance) made any deviation,
however extreme, silently register as "normal" instead of maximally anomalous (real data is
never perfectly constant, which is why this never surfaced before); and the crash-on-fresh-clone
issue described above, caught by literally swapping out the real database for an empty one and
hitting every dashboard route.

## Troubleshooting

- **Venv activation blocked**: see the `Set-ExecutionPolicy` command above.
- **`pythonw.exe` not found**: make sure the venv was created (`py -3.12 -m venv venv`) before
  running `setup_scheduled_task.ps1`.
- **Database appears locked**: the logger uses WAL mode specifically so a dashboard or GUI
  browser can read concurrently while it writes; if you still see lock errors, make sure only
  one logger process is running (check both the scheduled task and any manual foreground run).
- **Port 5000 already in use**: the dashboard defaults to port 5050 for this reason. Override
  with `$env:PORT = "5051"; python dashboard/app.py` if 5050 is also taken.
- **No anomalies showing**: run `python anomaly.py --all` to backfill detection across every day
  currently in the database -- the `anomalies` table is only populated when `anomaly.py` runs,
  it isn't computed live by the dashboard.
