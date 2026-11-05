"""Every threshold and constant of the M3 cleaning layer, in one place.
Each one is justified in docs/cleaning_report.md (section 5). clean.py and validate.py import this file."""

# ---- AIS "not available" codes and valid ranges (ITU-R M.1371 position report) -----------------
SENTINEL_SOG = 102.3        # knots; 102.3 = speed not available
SOG_MAX = 102.2             # 102.2 = "102.2 knots or more", still a measurement
SENTINEL_COG = 360.0        # degrees; 360 = course not available
COG_MAX = 359.9
SENTINEL_HEADING = 511      # 511 = true heading not available
HEADING_MAX = 359
SENTINEL_NAV_STATUS = 15    # 15 = "not defined" (default)
NAV_STATUS_MAX = 14
SENTINEL_ROT = -128         # -128 = rate of turn not available
ROT_MIN, ROT_MAX = -127, 127
SENTINEL_LAT = 91.0         # 91 = latitude not available
SENTINEL_LON = 181.0        # 181 = longitude not available
REGION_BBOX = (35.0, 25.0, 42.5, 36.0)  # lat_min, lon_min, lat_max, lon_max = the AIS_BBOX ingest.py subscribes to

# ---- AIS rules that need a threshold -----------------------------------------------------------
IQR_K = 1.5                 # fence = Q3 + IQR_K * IQR on valid sog (flag unusual_high, value kept)
ANCHORED_MAX_SOG = 2.0      # knots; nav_status "at anchor"/"moored"/"aground" with sog >= this is a conflict
MMSI_LENGTH = 9
MMSI_SHIP_FIRST_DIGITS = "234567"   # MID 2xx-7xx = ship stations; other prefixes (0, 1, 8, 9) are not ships

# ---- Wave readings (Open-Meteo marine model) ---------------------------------------------------
WAVE_HEIGHT_MAX = 25.0      # metres; above this the model value is not credible
WAVE_PERIOD_MAX = 30.0      # seconds
DIRECTION_MAX = 360.0       # degrees
STUCK_RUN = 6               # consecutive hours of the identical non-zero wave height -> stuck_value (kept)
SPIKE_M = 1.0               # metres; one hour higher than BOTH neighbours by more than this -> spike (kept)
ROBUST_Z = 3.5              # robust z = 0.6745 * (x - median) / MAD, per marine point
UNUSUAL_MIN_M = 1.0         # unusual_high only for wave heights of at least this many metres
WIND_TOL_M = 0.05           # wind-wave height may exceed total wave height by at most this (rounding)

# ---- validate.py -------------------------------------------------------------------------------
MAX_MISSING_SHARE = 0.20    # WARN when a marine point has more than this share of missing_not_reported heights
