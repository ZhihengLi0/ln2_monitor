# LN2 Scale Monitor

Real-time monitoring and Slack alerting for a **liquid-nitrogen scale** (weight,
temperature, humidity) read from an Arduino. Runs on the same Raspberry Pi as the
[BlueFors CS2 monitor](https://github.com/ZhihengLi0/column_monitor) and reuses
the **same Slack bot and channel**, but is otherwise an independent system with
its own PostgreSQL database.

---

## Architecture

```
Arduino (scale)  →  /home/cdms/arduino_data.txt   (one line every ~5 s)
                        │  ingest.py   (cron, every minute)
                        ▼
                 PostgreSQL  ln2 . scale_readings
                        │  monitor.py  (cron, every minute)  → alerts
                        │  ln2_query.py (called by the shared Slack responder)
                        ▼
                     Slack  (shared BlueFors bot + channel)
```

The Arduino appends lines like:

```
2026-07-07 10:11:57  Weight:0.14,Temp:24.20C,Humidity:51.00%
```

- **`ingest.py`** — parses new lines (byte-offset tracking; skips Arduino startup
  fragments) and inserts them into `ln2.scale_readings`.
- **`monitor.py`** — reads the latest reading and sends Slack alerts on weight
  low/critical, temperature out of range, and data staleness. `--status` posts
  the current reading on demand.
- **`ln2_query.py`** — answers natural-language Slack queries (current reading,
  weight plot). Imported by the BlueFors Slack responder.

## Database

Same PostgreSQL server as `cs2`, separate database `ln2`:

```sql
CREATE TABLE scale_readings (
    id       BIGSERIAL PRIMARY KEY,
    time     TIMESTAMPTZ NOT NULL,
    weight   DOUBLE PRECISION,
    temp     DOUBLE PRECISION,
    humidity DOUBLE PRECISION
);
CREATE INDEX idx_scale_time ON scale_readings (time DESC);
```

Connect: `psql -h localhost -U postgres -d ln2` (credentials shared with `cs2`).

## Slack commands

All via `@BlueFors-Alert` in the shared channel — natural language works:

| Command | Description |
|---|---|
| `ln2` · `how much liquid nitrogen` · `液氮还剩多少` · `weight` | Current weight, temperature, humidity + 1-hour weight trend |
| `plot ln2 weight 6h` · `液氮曲线 2h` | Weight-vs-time chart (min / hour / day) |

Keywords that route to this system: `ln2`, `nitrogen`, `weight`, `scale`, 液氮,
氮, 秤, 重量, 称重.

## Alerts

Checked every minute (to the shared Slack channel):

| Alert | Condition | Config key |
|---|---|---|
| LN2 weight low (warning) | weight < `WEIGHT_LOW` | `WEIGHT_LOW` |
| LN2 weight critical | weight < `WEIGHT_CRITICAL` | `WEIGHT_CRITICAL` |
| Temperature high / low | temp out of `[TEMP_LOW, TEMP_HIGH]` | `TEMP_HIGH`, `TEMP_LOW` |
| Data stale | no new reading for `DATA_STALE_MINUTES` | `DATA_STALE_MINUTES` |

Same alert repeats at most every `ALERT_COOLDOWN_MINUTES`. Set a threshold to
`None` to disable that check. Thresholds live in **`ln2_config.py`**.

## Configuration

`ln2_config.py` holds all settings. It contains **no secrets** — the DB password
and Slack token are imported from the BlueFors `config_secret.py` (git-ignored,
local only), so nothing sensitive is committed here.

## Setup

```bash
# 1. Create the database + table
psql -h localhost -U postgres -c "CREATE DATABASE ln2;"
psql -h localhost -U postgres -d ln2 -f schema.sql

# 2. Install cron jobs (ingest + monitor, every minute)
crontab -e
#   * * * * * python3 /home/cdms/ln2_monitor/ingest.py  >> /home/cdms/ln2_monitor/ingest.log  2>&1
#   * * * * * python3 /home/cdms/ln2_monitor/monitor.py  >> /home/cdms/ln2_monitor/monitor.log 2>&1
```

## Files

| File | Description |
|---|---|
| `ingest.py` | Parse the Arduino text file → `scale_readings` (cron) |
| `monitor.py` | Alert monitor + `--status` (cron) |
| `ln2_query.py` | Natural-language Slack query/plot helpers |
| `ln2_config.py` | Thresholds, DB, Slack channel (no secrets) |
| `schema.sql` | Database schema |
| `*.log`, `*_state.json`, `*.png` | Runtime artefacts (git-ignored) |
