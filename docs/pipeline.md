# Pipeline: Turkish Straits maritime pipeline

> Status of this document: the design, operations and drill procedures are final. Every place marked `<<FILL>>` needs the
> output of the real schedule (seven days of runs) and must be pasted from the machine that ran it. Nothing in a `<<FILL>>` has been measured yet.

## 1. The graph
```
 AISStream (WebSocket, live)  ---> ingest.py ---> data/raw/ais/<run stamp>.jsonl     (a 10-minute window per run)
 Open-Meteo marine (HTTP)     ---/            \--> data/raw/marine/<run stamp>.json  (since the last file + overlap)
                                                         |
 data/raw/**  ---> load.py ---> data/maritime.db: vessel, marine_point, ais_position, wave_reading, quarantine   (M2 tables)
 M2 tables    ---> clean.py --> vessel_clean, ais_position_clean, wave_reading_clean (+ mapping tables)         (M3 layer)
 both layers  ---> validate.py -> PASS / WARN / STOP on stdout, exit code 1 on STOP
 run_pipeline.py runs the four steps as separate processes and writes data/pipeline_log.db; monitor.py reads both, read-only.
```
| Step | Input | Output | Rebuildable from |
|---|---|---|---|
| `src/ingest.py` | the two sources, `--run-time` | one raw file per source and run | not rebuildable: AIS has no history, marine only 92 days |
| `src/load.py` | all raw files | M2 tables | raw files (full reload, idempotent) |
| `src/clean.py` | M2 tables | cleaned layer, mapping tables | M2 tables (full rebuild) |
| `src/validate.py` | both layers, `--run-time` | PASS/WARN/STOP lines | nothing is written |
| `src/run_pipeline.py` | the four steps | `data/pipeline_log.db`, `data/pipeline.lock` | the log is not rebuildable (it is evidence) |

## 2. Design decisions
| Decision | Our choice | Why |
|---|---|---|
| Ingestion: where each run starts | Marine: `past_days` from the newest landed marine file (`plan_marine_window` in `ingest.py`). AIS: a fresh 10-minute window | A file exists only after a fully valid fetch, so "newest file" is "last success". AIS is a live stream: there is nothing to ask for "since" |
| Overlap | 24 hours (`OVERLAP_HOURS`) | Forecasts of past hours are revised between fetches (M2: 177 keys differ across files). See the delay table below |
| Loading | Full reload of all raw files on every run | Simple and correct; `load.py` prints its time (below). The upsert is idempotent, so a reload changes nothing unless a file is new |
| Version rule | `retrieved_at` (= the run time that stamped the file); upsert guarded with `WHERE excluded.retrieved_at > <table>.retrieved_at` | The source has no version stamp of its own. The guard means an old file loaded later cannot undo a newer value (drill 5) |
| Cleaning | Full rebuild of the cleaned layer inside one transaction on every run | Flags are always computed on the whole history. Old flags may change as history grows: `unusual_high` uses each wave point's median and MAD over all its readings, and the speed fence uses all positions |
| The clock | `--run-time` (default: now) is passed to every step; file names, "since" and freshness all come from it. `datetime.now()` appears only in `db.parse_run_time` (the default) and in `run_pipeline.py` for `started_at` / `finished_at` | A rerun with the same run time gives identical tables; a backfill is a rerun |
| Schedule | Hourly, pipeline at minute 7, monitor at minute 20 | AIS is live and sampled for 10 minutes per run, so hourly gives a sample every hour. Open-Meteo allows far more calls than we use (8 calls per run, about 192 a day against a free-tier limit of about 10,000; confirm in `docs/sources.md`); its model updates a few times a day, so most hourly fetches repeat values, which the idempotent load absorbs |
| Lock | `O_CREAT \| O_EXCL` file `data/pipeline.lock`, removed in `finally`; a lock whose process is gone (or older than 6 h) is taken over | A crash or power loss must not block the schedule forever |
| Timeouts | ingest 1500 s, load 600 s, clean 600 s, validate 300 s | ingest is AIS collection (600 s) plus HTTP retries (at most 5 x 30 s plus back-off); the others take seconds today, a timeout is a hang |

### How late our data arrives
Produced by `python src/delay_report.py` on the landed raw files (paste the output):
```
<<FILL: output of python src/delay_report.py after the seven days>>
```
Reading it: the marine series is a forecast window, so most readings are fetched *before* the hour they describe (negative delay) and
re-fetched later, when they are revised. The "oldest age at which a value still changed" line is the minimum overlap; `OVERLAP_HOURS = 24`
is kept if that line is below 24 h and raised if not. AIS positions arrive within the window of the run that fetched them and are never revised.

### Loading time
`load.py` prints `load time: X s for N raw files`. <<FILL: today's value and the estimate: with t seconds per file and 48 new files a day, the 600 s timeout is reached after about (600 / 2) / (48 t) days; at that point switch to a manifest of loaded files>>

### The source's memory
- **Open-Meteo marine**: `past_days` is at most 92 (documented), and the past hours come from archived forecasts. An outage longer than 92 days loses the oldest hours; `ingest.py` then lands what exists and exits non-zero with a message starting `GAP:` naming the lost window. Shorter outages are healed by the next run (`past_days` is computed from the gap).
- **AISStream**: no history at all. Every missed run loses that 10-minute window of vessel positions for good. `ingest.py` prints a `WARNING` when the last AIS window is older than 3 h, and `monitor.py` raises an ALARM for every hour without positions.

## 3. Operations
- Run by hand: `python src/run_pipeline.py` (fetch, load, clean, validate) and `python src/monitor.py`.
- Rebuild without the network: `python src/run_pipeline.py --skip-ingest [--run-time 2026-11-20T08:10Z]`.
- Backfill after changing a cleaning rule: the same command. Everything after raw is rebuilt, and the same run time gives identical tables.
- Schedule: `sh ops/install_cron.sh` on the scheduling machine writes `ops/crontab.txt` with the real paths and installs the two lines. Check with `crontab -l`.
- Exit codes: 0 success - 1 a step failed - 75 another run in progress - 124 a step timed out - 127 a step could not start. The runner exits with the failing step's code.
- Logs: `logs/pipeline.log` and `logs/monitor.log` (cron output), `data/pipeline_log.db` table `pipeline_run` (one row per step per run), `data/pipeline.lock` while running. All three are git-ignored.
- macOS note: cron cannot read `~/Desktop` or `~/Documents` unless `/usr/sbin/cron` has Full Disk Access (System Settings, Privacy and Security). Keep the repository elsewhere, or grant it.

## 4. Evidence
### The schedule
Machine: <<FILL: whose machine, operating system>>. Interval: hourly (minute 7, monitor minute 20). File: `ops/crontab.txt`. Started: <<FILL: date>>.

### The run log
```sql
SELECT MIN(run_time) AS first_run, MAX(run_time) AS last_run, COUNT(DISTINCT run_id) AS runs,
       COUNT(DISTINCT substr(run_time, 1, 10)) AS days,
       SUM(exit_code <> 0) AS failed_steps
FROM pipeline_run;
SELECT step, exit_code, COUNT(*) AS n FROM pipeline_run WHERE exit_code <> 0 GROUP BY step, exit_code;
```
```
<<FILL: output of the two queries, and the cause of each failure>>
```

### Missed runs
What we do: disable the schedule for at least two days (`crontab -r` after saving `crontab -l > /tmp/saved`, then `crontab /tmp/saved` to resume;
or shut the computer down), so that at least two scheduled runs in a row are missed and the gap is longer than the one-day forecast horizon.
Before the next run, and again after it, record what `python src/monitor.py` says (its completeness checks are the queries of Appendix C: `WAVE_COMPLETENESS_SQL` and `AIS_COMPLETENESS_SQL` in `src/monitor.py`).
Expected: before, ALARM on `gaps between runs` and on `completeness: AIS hours`
(and on `completeness: wave hours` if the outage exceeded the forecast horizon); after, the gap ALARM stays (the gap happened) until it leaves the 7-day window,
the wave completeness ALARM is gone (healed), and the AIS completeness ALARM stays (AIS has no history: those hours are lost for good).
```
<<FILL: monitor.py output before, ingest log line "asking for past_days=...", monitor.py output after>>
```

### Drills
Run `python src/drills.py` on the scheduling machine (add `--network` for the wrong-key drill) and paste the output; the drills run in a sandbox copy and never touch the real log.
```
<<FILL: output of python src/drills.py: drills 1 to 5; drill 6 is the missed-runs demonstration above>>
```
| # | Drill | What we do | Expected | What the log shows |
|---|---|---|---|---|
| 1 | Credential failure | run without `AISSTREAM_API_KEY` (and with a wrong one, `--network`) | ingest exits 1, runner exits 1, no later step | one row: ingest 1 |
| 2 | A step fails | trigger on `wave_reading_clean` that aborts inserts | runner stops at clean, database unchanged | load 0, clean 1 |
| 3 | A step hangs | `PIPELINE_DRILL=clean=hang` with `--timeout 4` | exit 124 | clean 124 |
| 4 | Two runs at once | start a second run while the first holds the lock | second exits 75 and runs nothing | no new rows |
| 5 | An old file re-loaded | load the newest marine file, then only the oldest | the newer value stays | n/a (database) |
| 6 | Missed runs | see above | monitor.py ALARM, next run heals marine | gap in `run_time` |

The hook behind drills 2 to 4 is the environment variable `PIPELINE_DRILL` (for example `load=fail`, `clean=hang`); it is off unless set and documented in `src/db.py`.
Each of the four monitor checks was also made to ALARM on purpose (`src/checks_m5.py`): last success too old, a gap between runs, a missing wave hour, a missing AIS hour, stale per-point and per-feed freshness, and a missing run log.

### monitor.py on the day we tagged
```
<<FILL: paste python src/monitor.py output on the tagging day>>
```

## 5. What can still go wrong
- **The machine is off or asleep.** The schedule stops. `monitor.py` runs on the same machine, so when the whole machine is off nothing alarms until it is back; only the next run's gap check shows it. An outside heartbeat (a free uptime service that expects a ping) would close this and is not built.
- **AIS has no history.** Every missed run loses vessel positions permanently; we notice (ALARM) but cannot recover.
- **A sample, not a record.** AIS is a 10-minute window per hour: it says nothing about the other 50 minutes, and waiting durations stay unmeasurable.
- **Silent source changes.** If Open-Meteo changes a variable or the AISStream message format, the data still lands; the loader quarantines what it cannot parse and `validate.py` counts them, but a renamed field that still parses would pass.
- **`retrieved_at` is the pipeline's clock, not the model's.** Two fetches in one minute would share a stamp (the second run fails with "file already exists"); revisions inside one model run cannot be told apart.
- **Single machine, single copy.** A disk failure loses the log and the raw files (AIS irrecoverably). No backup is automated.
- **Full reload cost.** Load time grows with every file; the estimate above says when to move to a manifest.
- **Clock skew.** The run time comes from the machine's clock; a wrong clock stamps files with wrong times. Not checked.
- **Cleaning decisions change as history grows** (`unusual_high` thresholds), so a flag seen today may be gone tomorrow; this is by design and stated above.
- **Quiet WARNs.** The WARNs that stay (nav-status conflicts, stuck values) only look at the last 7 days so old events cannot keep them on; a real recurring problem would show up on every run and should then be fixed, not ignored.
