# Cleaning Report: Turkish Straits maritime pipeline

## 1. Summary
The data are 72 AIS position reports from 67 vessels (AISStream) and 960 hourly wave readings at 8 grid points (Open-Meteo marine model), 2026-10-09 to 2026-10-22. The AIS data hold "not available" codes in `true_heading` (20 rows), `rate_of_turn` (20), `cog` (11) and `nav_status` (3); they become NULL with a flag and the raw value is kept next to them. One position has `nav_status` moored at 8 knots and is flagged, not changed. No values are NULL, no coordinate, name or identity problem was found, but the wave data has a 9-day hole (11-19 Oct) because ingest ran only on 10 Oct and 20-22 Oct. The wave data is calm: the highest wave in the whole sample is 0.64 m, far below the 1.5 m the project question needs, so cleaning cannot make the question answerable. M2 issues fixed since the M2 tag: none.

## 2. Profile highlights
| What the profile showed | Number | Led to |
|---|---|---|
| Rows | 67 vessels, 72 AIS positions, 8 wave points, 960 wave readings, 0 in quarantine | baseline for the row-count check |
| AIS positions per vessel | 62 vessels have 1 position, 5 have 2 | per-vessel sequence checks are almost impossible |
| AIS collection windows | 36 positions in the hour of 2026-10-10T11, 15 in 2026-10-21T16, 21 in 2026-10-22T11 | three short windows (about 10 minutes each) |
| Wave rows per day | 192 (8 points x 24 h) on 9, 10, 20, 21 and 22 Oct, nothing between | I-09 |
| NULLs | 0 in every AIS and wave column | no missing-value mechanism to argue |
| AIS sentinel candidates | `sog` 102.3: 0; `cog` 360: 11; heading 511: 20; `nav_status` 15: 3; rate of turn -128: 20; lat 91, lon 181, (0, 0): 0 | I-01 to I-04 |
| `sog` mode | 0.0 in 27 of 72 rows, median 0.1, max 16.3 kn | 0.0 is a real stationary reading (at anchor, `sog` max 0.1), not a sentinel |
| Wave height | min 0.02, median 0.04, max 0.64 m; 0 rows above 1.5 m | what we cannot say (section 8) |
| `wind_wave_height` | 0.0 in 676 of 960 rows, max 0.4 m | a real value on calm days (0 rows where it exceeds the total) |
| AIS positions, latitude | 35.04 to 42.49, median 36.83; the southernmost wave point is at 38.46 | I-10 |
| Ship names | 67 distinct, none with NULL, padding, `@`, NBSP, non-ASCII or double spaces; all 67 MMSI have 9 digits, first digit 2 to 6 | I-07 |

Full output: `notebooks/m3_profile.ipynb`. Notebook: `notebooks/m3_profile.ipynb`.

## 3. Issues register
| ID | Family | Table.column | What | Where / when / how many | Evidence | Cause | Decision | Flag | Investigated by |
|---|---|---|---|---|---|---|---|---|---|
| I-01 | Invalid | ais_position.true_heading | 511 = "not available" code | 20 of 72 rows (28%) | `SELECT COUNT(*) FROM ais_position WHERE true_heading = 511`; clean.py `true_heading_flag = invalid_sentinel`: 20 | Confirmed: AIS specification (ITU-R M.1371) | NULL | invalid_sentinel | Hüseyin Sefa Endes |
| I-02 | Invalid | ais_position.cog | 360 = "not available" code | 11 of 72 rows (15%) | `cog = 360` | Confirmed: AIS specification | NULL | invalid_sentinel | Hüseyin Sefa Endes |
| I-03 | Invalid | ais_position.rate_of_turn | -128 = "not available" code | 20 of 72 rows (28%) | `rate_of_turn = -128` | Confirmed: AIS specification | NULL | invalid_sentinel | Hüseyin Sefa Endes |
| I-04 | Invalid | ais_position.nav_status | 15 = "not defined" | 3 of 72 rows (4%) | `nav_status = 15` | Confirmed: AIS specification | NULL | invalid_sentinel | Hüseyin Sefa Endes |
| I-05 | Failure across fields | ais_position.nav_status vs sog | status 5 (moored) with sog 8.0 kn: vessel LE CHAMPLAIN (MMSI 578001100), 2026-10-21T16:12:06Z | 1 of 72 rows | `row_flag = nav_status_conflicts_with_sog` | Inferred: nav_status is entered by the crew and was probably not updated before sailing; nothing in the data confirms it | value kept, row flagged | nav_status_conflicts_with_sog | Buse Selin Özmen |
| I-06 | Failure across rows | wave_reading.wave_height | identical values for many hours in a row. All values are multiples of 0.02 m; per point only 4 to 27 distinct values; point 3 holds 0.02 m for 97 hours | all 8 points; 9 rows at or above 0.3 m are flagged (point 8, 2026-10-22 15:00-23:00Z, 0.32 m) | diagnostic of run lengths; `diag2` listing; the same point holds 0.28 m for 6 h on 9 Oct and point 1 holds 0.04 m for 18 h | Inferred: smooth model curve on a 0.02 m grid plateaus; not a sensor fault (a model has no sensor) | first rule flagged 618 of 960 (64%), the wrong rule for this data; revised so only runs at or above 0.3 m count; flagged rows are kept | stuck_value (kept) | Şeyma Büyükaydın |
| I-07 | Text | vessel.ship_name | padding, placeholders, Unicode | 0 of 67 names needed a change | `ship_name_flag`: ok = 67 | Checked, nothing found | none | ok | Buse Selin Özmen |
| I-08 | Unusual | wave_reading.wave_height | values high relative to the point's own calm level | 29 rows: point 1 (38.46 N 25.88 E, Aegean) on 2026-10-22 07:00-23:00Z (17 rows, 0.38-0.50 m), point 2 (40.04 N 26.13 E, south of the Dardanelles) on 2026-10-22 12:00-23:00Z (12 rows, 0.46-0.64 m) | robust z (median, MAD) per point; consecutive hourly changes are at most 0.04 m, so these are slow rises and falls, not jumps | Inferred: a real build-up of sea state at the end of the sample (slight sea, not a storm) | kept, flagged | unusual_high | Şeyma Büyükaydın |
| I-09 | Missing | wave_reading (all columns) | a whole period is absent: no rows from 2026-10-11 to 2026-10-19 | all 8 points: 120 hourly rows each = 5 days (9, 10, 20, 21, 22 Oct); 960 = 8 x 120 | listings for points 1, 2 and 8 jump from 2026-10-10T23:00Z to 2026-10-20T00:00Z | Confirmed: `ingest.py` asks Open-Meteo for `past_days=1, forecast_days=1`, so each fetch covers 2 days, and we ran ingest only on 10 Oct and 20-22 Oct | nothing to clean; documented, validate.py WARN | n/a | Şeyma Büyükaydın |
| I-10 | Missing (entities) | wave_reading vs ais_position | most AIS positions have no wave point nearby | at least 36 of 72 positions lie south of 36.83 N; the southernmost wave point is at 38.46 N, over 1.6 degrees (about 180 km) away | notebook section 3 (latitude median 36.83) and section 1 (wave point coordinates) | Confirmed: `ingest.py` subscribes AIS to 35.0-42.5 N, 25.0-36.0 E (the whole eastern Mediterranean) while the 8 wave points sit in the straits and the northern Aegean | nothing to clean; documented, M4 must restrict to positions near a wave point | n/a | Buse Selin Özmen |

### Checked and not found
| Family | Check | Result |
|---|---|---|
| Missing | NULL in any M2 column | 0 AIS and 0 wave NULLs; load summary "NULL in source (kept): 0"; every flag `missing_*` count is 0 in clean.py |
| Invalid | `sog` = 102.3, negative, or above range | 0 rows (`sog_flag` is ok for all 72) |
| Invalid | latitude/longitude 91/181, out of range, (0,0), outside `REGION_BBOX` | 0 rows (all 72 are inside; AISStream filters by the same box, so the bbox check has little power) |
| Unusual | `nav_status` code 9 (reserved) | 2 positions, both `sog` 0.0; inside the valid range 0-14, so not flagged |
| Text | ship names: NULL, padding, `@`, NBSP, non-ASCII, double spaces; MMSI length and prefix | 0 of 67 names; all MMSI have 9 digits and a ship prefix (2 to 6), so `mmsi_flag` is ok for all 67 |
| Invalid | wave_height negative or above 25 m; direction outside 0-360; period negative | 0 rows |
| Failures across fields | wind-wave height above total wave height | 0 inconsistent rows (validate.py PASS) |
| Failures across rows | implied speed vs reported sog | 5 vessels have 2 positions: implied speed 0.0 kn in all five (reported sog 0.0 to 0.7), consistent with stationary vessels. The other 62 vessels have one position, so this check cannot run for them |
| Identity | two vessels sharing a normalised name; one MMSI with two names | none: validate.py "no candidates"; 0 of 67 MMSI with two names (M2 key_tests) |
| Model coverage | a marine point with NULL wave data for every hour | none: all 8 points covered |

## 4. Missing values
No value is NULL in the M2 tables (0 missing in `ais_position` and `wave_reading`), so there is no NULL pattern to argue. Two other kinds of "missing" exist:

- **AIS "not available" codes** (`true_heading` 20, `rate_of_turn` 20, `cog` 11, `nav_status` 3 of 72 rows). The mechanism is probably missing-not-at-random by equipment: heading and rate of turn need a heading sensor that small vessels often lack (inferred; vessel type is not in our data). Consequence for M4: do not average heading or turn over all vessels, the ones that report them are not a random sample.
- **A whole period is absent** (I-09): no wave rows from 11 to 19 Oct at any point. The cause is when we ran `ingest.py` (confirmed), and it does not depend on the waves, so we treat it as missing completely at random with respect to sea state. Consequence for M4: never compute a trend or a daily series across the gap; analyse 9-10 Oct and 20-22 Oct as two separate periods. An AIS position can only be matched to a wave reading when its hour lies in those 5 days. All 72 positions were collected on 10, 21 and 22 Oct, which are days with wave data, so none is lost to the gap.

## 5. Flags and rules
Order of rules: first rule that applies wins. Cleaned value is NULL when not trusted; never imputed.

| Order | Column | Flag | Rule | Cleaned value | Count |
|---|---|---|---|---|---|
| 1 | every column | missing_not_reported | raw value NULL | NULL | 0 |
| 1 | wave_* | missing_no_model_coverage | raw NULL for every hour of that marine point and column | NULL | 0 |
| 2 | AIS numeric | invalid_sentinel | raw equals the AIS "not available" code (SENTINEL_*) | NULL | cog 11, true_heading 20, nav_status 3, rate_of_turn 20, sog 0, latitude 0, longitude 0 |
| 3 | AIS / wave | invalid_out_of_range / invalid_negative | outside the valid range | NULL | 0 |
| 3 | latitude, longitude | invalid_null_island | both are 0 | NULL | 0 |
| 4 | latitude, longitude | invalid_outside_region | outside REGION_BBOX | NULL | 0 |
| 4 | wave_height | stuck_value | >= STUCK_RUN identical hourly values at or above STUCK_MIN_M | kept | 9 (point 8) |
| 5 | wave_height | spike | one hour above both neighbours by more than SPIKE_M | kept | 0 |
| 6 | sog | unusual_high | sog above Q3 + IQR_K * IQR | kept | 0 |
| 6 | wave_height | unusual_high | robust z > ROBUST_Z per point and at least UNUSUAL_MIN_M | kept | 29 (point 1: 17, point 2: 12) |
| any | wind_wave_height | inconsistent_exceeds_total | wind-wave height > wave height + WIND_TOL_M | kept (cannot tell which is wrong) | 0 |
| row | ais_position | nav_status_conflicts_with_sog | nav_status in a `stationary` group (mapping CSV) and sog >= ANCHORED_MAX_SOG | kept | 1 |

### Constants (all in `src/clean_config.py`)
- `SENTINEL_*`, `*_MAX`: the AIS specification defines these "not available" codes and ranges; nothing to tune.
- `IQR_K = 1.5`: the standard Tukey fence; it uses quartiles, so it is not distorted by the outliers it looks for. It flagged 0 of 72 `sog` values. Q1 = 0.0 and Q3 = 9.325 kn give a fence of 23.31 kn; the highest `sog` is 16.3 kn.
- `ANCHORED_MAX_SOG = 2.0` kn: a vessel at anchor can swing and drift at 1-2 kn; above that "at anchor/moored" is doubtful. Raising it removes borderline conflicts.
- `WAVE_HEIGHT_MAX = 25.0` m: above any credible significant wave height for the Black Sea, Marmara and Aegean; observed maximum in the sample 0.64 m.
- `STUCK_RUN = 6`, `STUCK_MIN_M = 0.3` m: six hours is a quarter of a day. Honest limit: in this model data the rule cannot tell a fault from a plateau. The curve is smooth and sits on a 0.02 m grid, so it repeats a value for hours at any level (point 1: 0.04 m for 18 h; point 8: 0.28 m for 6 h on 9 Oct; 0.32 m for 9 h on 22 Oct). The first version flagged 618 of 960 readings (64%); with the 0.3 m floor, 9 rows are flagged. The 0.3 m boundary is a judgment, not a physical line. Moving STUCK_RUN to 12 would flag none of the 9; moving the floor to 0.2 m would add the 0.28 m plateau.
- `SPIKE_M = 1.0` m: a forecast hour more than 1 m above both neighbours is not physical for hourly data. The largest hourly change in the points listed is 0.04 m and the sample maximum is 0.64 m, so the rule cannot fire here. Largest change between consecutive rows is at most 0.12 m at points 1 to 7. At point 8 it is 0.32 m, but that is the step across the 9-day gap (0.12 m on 10 Oct to 0.44 m on 20 Oct), not an hourly jump.
- `ROBUST_Z = 3.5`, `UNUSUAL_MIN_M = 0.3`: robust z (median, MAD) per point, because points differ in level (point 3 never exceeds 0.08 m, point 2 reaches 0.64 m). The floor started at 1.0 m but the sample maximum is 0.64 m, so nothing could be flagged; 0.3 m is above the calm-sea level. Flagged: 29 rows, all on 22 Oct at points 1 and 2; inspected (hourly changes at most 0.04 m): slow, smooth rises, kept. Without the floor, the notebook's robust z would also flag 15 values at points 5 and 7 (6 and 9 rows, maxima 0.16 and 0.22 m); `clean.py` does not, because they sit below 0.3 m, and a 0.1 m wiggle is not an unusual sea.
- `WIND_TOL_M = 0.05` m: rounding tolerance of the model output.
- `MAX_MISSING_SHARE = 0.20`: one reading in five missing is where a per-point average starts to depend on which hours are present. Observed: 0% missing at every point.
- `MAX_GAP_HOURS = 24`: a gap of more than a day breaks the hourly series. Observed: a 9-day gap (11-19 Oct) at every point.

## 6. Mapping and identity decisions
Mapping tables, all loaded from committed CSVs in `data/mappings/` with `decided_by`, `decided_at`, `note`:
`nav_status.csv` (code -> label and motion group), `name_placeholder.csv` (names that are placeholders),
`vessel_alias.csv` (MMSI re-registrations; currently empty). Steps and counts:

| Step | Result |
|---|---|
| Exact match on the source identifier | `vessel_alias` is empty: no MMSI is recorded as the alias of another. 0 of 67 MMSI carry two ship names (M2 `key_tests.py`) |
| Normalised key (NFC, Turkish letters folded, casefold) | all 67 names equal their cleaned form (`ship_name_flag` ok = 67); 0 pairs of vessels share a normalised name (validate.py "no candidates") |
| Fuzzy proposal | not run: with no shared key there is nothing to propose, and fuzzy matching without a person maps unrelated names |
| Human decision | 0 identity decisions; the empty `vessel_alias.csv` is intentional and says so by being empty |
| Placeholder names | 9 entries in `name_placeholder.csv`; they match 0 of 67 vessels (every vessel has a real name) |
| Navigational status | 15 codes loaded from `nav_status.csv`; only 5 occur in the data (0, 1, 5, 8, 9); code 15 in the data is the "not defined" sentinel |

Honest limit: with 67 vessels and mostly one position each, an identity split (a re-registered vessel) could not show up as two histories that adjoin in time. The notebook found no two MMSI with the same normalised name and no MMSI with two names. Every vessel was seen in at most two of three short collection windows, so the first-seen/last-seen test for a re-registered vessel (one MMSI ends where another begins) has no power here.
Mapping files: `data/mappings/nav_status.csv`, `name_placeholder.csv`, `vessel_alias.csv`.

## 7. Counts and validation
Final run outputs (committed): `clean_output.txt` (clean.py run twice, identical), `validate_output.txt` (11 PASS, 3 WARN, 0 STOP).

| WARN | Where is the problem? |
|---|---|
| 9 `stuck_value` wave heights | in the world / the model: smooth model output plateaus on a 0.02 m grid (inferred, see constants). Not a pipeline fault |
| 1 `nav_status` conflicts with sog (LE CHAMPLAIN, moored at 8 kn) | in the measurement: the status is entered by the crew and was probably not updated (inferred) |
| wave gap longer than 24 h (9 days, every point) | in our collection: when we ran `ingest.py` (confirmed). Not a model or pipeline fault |

How each STOP check was made to fail once (automated in `src/checks_m3.py`, on a temporary copy):
- row count: deleted one row from `ais_position_clean`.
- impossible value: set one cleaned `sog` to 500.
- foreign_key_check: inserted an `ais_position` row with a non-existent `vessel_key` (foreign keys off).
- `_raw` preserved: changed one `sog_raw`.

Each of the four reported `STOP` and exited with code 1.

## 8. What we can't say
- No wave above 0.64 m at any of the 8 points: the 1.5 m and 2.5 m classes of the project question are empty. Rough weather has to be collected before M4 can answer it.
- The wave series has a 9-day hole (11-19 Oct) at every point: only 5 days of hourly data per point exist.
- The `stuck_value` rule cannot separate a fault from a model plateau in this data (see constants); its 9 rows are kept and flagged, not evidence of a fault.
- 62 of 67 vessels have a single AIS position and 5 have two, so per-vessel sequences (implied speed, stuck values) can hardly be tested; the five pairs we have are consistent (implied speed 0.0 kn).
- At least half of the AIS positions are more than 180 km from every wave point (I-10), so the wave-versus-speed question can only use the positions near the northern points.
- Zero wave readings above 1.5 m.
- Wave heights are a forecast model on a 0.02 m grid, not measurements; identical runs in calm seas are expected, not faults.
- AIS is a 10-minute snapshot, so how long a vessel waited is not measurable.
- The `heading`, `rate_of_turn` and `cog` NULLs follow vessel equipment (inferred), so averages of these fields are biased.
