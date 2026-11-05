# Cleaning Report: Turkish Straits maritime pipeline

> DRAFT. Everything marked `<<FILL>>` must be completed from the real `clean.py` / `validate.py` output
> and the executed notebook. Nothing in a `<<FILL>>` cell has been measured yet.

## 1. Summary
<<FILL: 5-6 sentences: data (AIS PositionReports from AISStream, Open-Meteo marine wave model), the most important
problems, what the cleaned layer contains, what we still cannot say. M2 issues fixed since the M2 tag: none / list.>>

## 2. Profile highlights
<<FILL: tables from notebooks/m3_profile.ipynb that led to findings.>> Notebook: `notebooks/m3_profile.ipynb`.

## 3. Issues register
| ID | Family | Table.column | What | Where / when / how many | Evidence | Cause | Decision | Flag | Investigated by |
|---|---|---|---|---|---|---|---|---|---|
| I-01 | Invalid | ais_position.true_heading | 511 = "not available" code | <<FILL>> rows of 72 | `SELECT COUNT(*) FROM ais_position WHERE true_heading = 511` | Confirmed: AIS specification (ITU-R M.1371) | NULL | invalid_sentinel | <<member>> |
| I-02 | Invalid | ais_position.cog | 360 = "not available" code | <<FILL>> rows | same pattern, `cog = 360` | Confirmed: AIS specification | NULL | invalid_sentinel | <<member>> |
| I-03 | Invalid | ais_position.rate_of_turn | -128 = "not available" code | <<FILL>> rows | `rate_of_turn = -128` | Confirmed: AIS specification | NULL | invalid_sentinel | <<member>> |
| I-04 | Invalid | ais_position.nav_status | 15 = "not defined" | <<FILL>> rows | `nav_status = 15` | Confirmed: AIS specification | NULL | invalid_sentinel | <<member>> |
| I-05 | Invalid | ais_position.sog | 102.3 = "not available" | <<FILL>> rows (M2 query found 0) | `sog = 102.3` | Confirmed: AIS specification | NULL | invalid_sentinel | <<member>> |
| I-06 | Missing | wave_reading.* | NULL = model has no value | <<FILL>> | per point NULL counts | <<FILL: inferred / unknown>> | NULL, flagged by reason | missing_not_reported / missing_no_model_coverage | <<member>> |
| I-07 | Failure across fields | ais_position.nav_status vs sog | moored/anchored with sog >= 2 kn | <<FILL>> | `clean.py` row_flag count | <<FILL>> | kept, row flagged | nav_status_conflicts_with_sog | <<member>> |
| I-08 | Failure across fields | wave_reading | wind-wave height above total | <<FILL>> | validate.py WARN | <<FILL>> | kept, flagged | inconsistent_exceeds_total | <<member>> |
| I-09 | Text | vessel.ship_name | '@' padding, placeholder names, Unicode | <<FILL>> | notebook section 4 | Confirmed: AIS name field is 6-bit padded with '@' | cleaned name, flagged | normalised / placeholder_name | <<member>> |
| I-10 | Identity | vessel.mmsi | same normalised name under two MMSI | <<FILL>> | validate.py WARN, notebook section 5 | <<FILL>> | <<FILL: mapping row or "not enough evidence">> | n/a (vessel_alias) | <<member>> |

### Checked and not found
| Family | Check | Result |
|---|---|---|
| Invalid | `latitude`/`longitude` outside range, (0,0), outside bbox | <<FILL: count from notebook section 3>> |
| Unusual | sog above the IQR fence; wave height robust z > 3.5 per point | <<FILL: counts; every flagged value inspected and kept>> |
| Failures across rows | longest identical run, hourly jumps, non-hourly gaps per marine point | <<FILL from notebook section 6>> |
| Failures across rows | implied speed vs reported sog | most vessels have a single position in the sample, so this check cannot be run for them (<<FILL: n vessels with 2+ positions>>) |
| Identity | MMSI with more than one ship name | 0 of 67 vessels (M2 `key_tests.py`) |

## 4. Missing values
<<FILL: for each substantial pattern: where/when, mechanism (MCAR / MAR / MNAR) with evidence, consequence for M4.
Example question to answer from the data: are wave NULLs concentrated in one marine point (no model coverage:
structural) or spread over time? Are missing AIS fields concentrated in one vessel type or status?>>

## 5. Flags and rules
Order of rules: first rule that applies wins. Cleaned value is NULL when not trusted; never imputed.

| Order | Column | Flag | Rule | Cleaned value | Count |
|---|---|---|---|---|---|
| 1 | every column | missing_not_reported | raw value NULL | NULL | <<FILL>> |
| 1 | wave_* | missing_no_model_coverage | raw NULL for every hour of that marine point and column | NULL | <<FILL>> |
| 2 | AIS numeric | invalid_sentinel | raw equals the AIS "not available" code (SENTINEL_*) | NULL | <<FILL>> |
| 3 | AIS / wave | invalid_out_of_range / invalid_negative | outside the valid range | NULL | <<FILL>> |
| 3 | latitude, longitude | invalid_null_island | both are 0 | NULL | <<FILL>> |
| 4 | latitude, longitude | invalid_outside_region | outside REGION_BBOX | NULL | <<FILL>> |
| 4 | wave_height | stuck_value | >= STUCK_RUN identical non-zero hourly values | kept | <<FILL>> |
| 5 | wave_height | spike | one hour above both neighbours by more than SPIKE_M | kept | <<FILL>> |
| 6 | sog, wave_height | unusual_high | sog above Q3 + IQR_K * IQR; wave height robust z > ROBUST_Z and >= UNUSUAL_MIN_M | kept | <<FILL>> |
| any | wind_wave_height | inconsistent_exceeds_total | wind-wave height > wave height + WIND_TOL_M | kept (cannot tell which is wrong) | <<FILL>> |
| row | ais_position | nav_status_conflicts_with_sog | nav_status in a `stationary` group (mapping CSV) and sog >= ANCHORED_MAX_SOG | kept | <<FILL>> |

### Constants (all in `src/clean_config.py`)
- `SENTINEL_*`, `*_MAX`: the AIS specification defines these "not available" codes and ranges; nothing to tune.
- `IQR_K = 1.5`: the standard Tukey fence; robust to the outliers it looks for. <<FILL: how many values it flags and why they are real>>
- `ANCHORED_MAX_SOG = 2.0` kn: a vessel at anchor can swing and drift at 1-2 kn; above that "at anchor/moored" is doubtful. Raising it removes borderline conflicts.
- `WAVE_HEIGHT_MAX = 25.0` m: above any credible significant wave height for the Black Sea, Marmara and Aegean; <<FILL: observed max>>.
- `STUCK_RUN = 6`: <<FILL: longest identical run in the profile; justify against that>>.
- `SPIKE_M = 1.0` m: a model-forecast hour that jumps more than 1 m above both neighbours is not physical for hourly data. <<FILL: observed largest jump>>
- `ROBUST_Z = 3.5`, `UNUSUAL_MIN_M = 1.0`: robust z (median, MAD) per point; the 1 m floor keeps trivial 0.3 vs 0.1 m differences out. <<FILL: what it flagged>>
- `WIND_TOL_M = 0.05` m: rounding tolerance of the model output.
- `MAX_MISSING_SHARE = 0.20`: <<FILL: justify>>.

## 6. Mapping and identity decisions
Mapping tables, all loaded from committed CSVs in `data/mappings/` with `decided_by`, `decided_at`, `note`:
`nav_status.csv` (code -> label and motion group), `name_placeholder.csv` (names that are placeholders),
`vessel_alias.csv` (MMSI re-registrations; currently empty). <<FILL: match counts at each step (exact -> normalised key ->
fuzzy proposal -> human decision), what stayed unmatched and why, evidence for every identity decision. If no identity
problem was found, say which check ran and that the alias file is intentionally empty.>>

## 7. Counts and validation
<<FILL: paste `python src/clean.py` (run twice) and `python src/validate.py` output. For every WARN: world, measurement or pipeline?>>

How each STOP check was made to fail once (automated in `src/checks_m3.py`, on a temporary copy):
- row count: deleted one row from `ais_position_clean`.
- impossible value: set one cleaned `sog` to 500.
- foreign_key_check: inserted an `ais_position` row with a non-existent `vessel_key` (foreign keys off).
- `_raw` preserved: changed one `sog_raw`.

Each of the four reported `STOP` and exited with code 1.

## 8. What we can't say
<<FILL. Likely items: the sample has 72 positions of 67 vessels, so per-vessel sequences cannot be checked; wave readings above 1.5 m are
<<FILL: count>>; wave data are a forecast model, not a measurement; AIS is a 10-minute snapshot, so waiting duration is not measurable.>>
