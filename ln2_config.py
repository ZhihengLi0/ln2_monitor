# LN2 Scale Monitor Configuration

import sys
from pathlib import Path

# ── Reuse the Slack bot token from the existing BlueFors monitor ───────────────
# Same bot, same channel (per user's choice). Token stays only in the BlueFors
# config_secret.py (git-ignored); we import it rather than duplicating it.
_BF_DIR = "/home/cdms/bluefors_monitor"
sys.path.insert(0, _BF_DIR)
try:
    from config_secret import SLACK_BOT_TOKEN
except ImportError:
    SLACK_BOT_TOKEN = "YOUR_SLACK_BOT_TOKEN"

# Slack channel — same channel as the fridge alerts.
SLACK_CHANNEL     = "C0B42G4AU0N"
SLACK_BOT_USER_ID = "U0BBGRB0HC4"

# ── Local PostgreSQL (ln2 database) ────────────────────────────────────────────
PG_HOST = "localhost"
PG_PORT = 5432
PG_USER = "postgres"
PG_DB   = "ln2"
try:
    from config_secret import LOCAL_PG_PASSWORD as PG_PASSWORD
except ImportError:
    PG_PASSWORD = "YOUR_PG_PASSWORD"

# ── Data source: the Arduino text file ─────────────────────────────────────────
ARDUINO_FILE = "/home/cdms/arduino_data.txt"

# ── Alert thresholds (PLACEHOLDERS — confirm real values with the user) ────────
# Weight unit is whatever the Arduino prints (assumed kg). Set to None to disable
# a given check.
WEIGHT_UNIT        = "kg"
WEIGHT_LOW         = -2.5    # alert when weight < this (time to refill LN2)
WEIGHT_CRITICAL    = None    # critical when weight < this
TEMP_HIGH          = None    # alert when temperature > this (°C)
TEMP_LOW           = None    # alert when temperature < this (°C)
DEW_POINT_HIGH     = None    # alert when dew point > this (°C) — condensation risk

# Minutes before the same alert can fire again
ALERT_COOLDOWN_MINUTES = 30

# Alert if no new reading has arrived for this many minutes (scale/Arduino down)
DATA_STALE_MINUTES = 10
