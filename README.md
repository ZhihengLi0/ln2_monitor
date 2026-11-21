# LN2 Scale Monitor

Real-time monitoring and Slack alerting for a **liquid-nitrogen scale** (weight,
temperature, humidity) read from an Arduino. Runs on the same Raspberry Pi as the
[BlueFors CS2 monitor](https://github.com/ZhihengLi0/column_monitor) and, since
**v2.0.0 (2026-11-20)**, the alarms and Slack commands are part of that monitor's
`bfm` package. This repository holds the reader service and the schema.

---

## Architecture

```
Arduino (scale, TCP 172.31.255.80:5000)
        │  one line every ~5 s
        ▼
ln2-reader.service  (ln2_reader.py — systemd, always on)
        │  INSERT per reading; spool file + replay if PostgreSQL is down
        ▼
PostgreSQL  ln2 . scale_readings
        │
        ▼
bluefors-monitor.service  (bfm package: LN2 alarms, `ln2` / `plot ln2 …` commands)
        │
        ▼
Slack  (shared BlueFors bot + channel)
```

The Arduino sends lines like:

```
2026-07-07 10:11:57  Weight:0.14,Temp:24.20C,Humidity:51.00%
```

### `ln2_reader.py`

* TCP client with a 60 s socket timeout, TCP keepalive and reconnect back-off
  (the old reader spun at 100 % CPU after the Arduino dropped the connection).
* Parses each line, rejects negative weights and jumps > 5 kg (Arduino start-up
  fragments), inserts directly into `scale_readings`.
* If the database is unreachable the reading goes to
  `/mnt/harddrive/pi/logs/ln2_spool.txt` and is replayed when it is back —
  no gap in the data.
* `Type=notify` + `WatchdogSec=600`: no reading for 10 min → systemd restarts it.

```bash
systemctl status ln2-reader
journalctl -u ln2-reader -n 30
sudo systemctl restart ln2-reader      # after editing ln2_reader.py
```

The unit file lives in the BlueFors repo: `bluefors_monitor/deploy/ln2-reader.service`
(installed by `deploy/install.sh`).

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

## Slack commands (handled by `bfm/commands/ln2.py`)

All via `@BlueFors-Alert` in the shared channel — natural language works:

| Command | Description |
|---|---|
| `ln2` · `how much liquid nitrogen` · `液氮还剩多少` · `weight` | Current weight, temperature, humidity, **dew point** + 1-hour weight trend |
| `plot ln2 weight 6h` · `液氮曲线 2h` | Weight-vs-time chart |
| `plot ln2 temperature 2h` · `plot ln2 humidity` · `plot ln2 dewpoint` | Temperature / humidity / dew point vs time |
| `silence ln2 2h` · `unsilence ln2` | Mute / unmute all LN2 alarms |

## Alerts (handled by `bfm/alarms.py`, thresholds in `bfm/config.py`)

| Alert | Condition | Config key |
|---|---|---|
| LN2 weight low (warning) | weight < `LN2_WEIGHT_LOW` | `LN2_WEIGHT_LOW` |
| LN2 weight critical | weight < `LN2_WEIGHT_CRITICAL` | `LN2_WEIGHT_CRITICAL` |
| Temperature high / low | temp out of `[LN2_TEMP_LOW, LN2_TEMP_HIGH]` | `LN2_TEMP_*` |
| Dew point | dew point above the room-condensation limit | `LN2_DEW_POINT_HIGH` |
| Data stale | no new reading for `LN2_DATA_STALE_MINUTES` | `LN2_DATA_STALE_MINUTES` |

Same cooldown, silence and ✅ Cleared behaviour as every other alarm in the
BlueFors monitor.

## Setup

```bash
# 1. Create the database + table
psql -h localhost -U postgres -c "CREATE DATABASE ln2;"
psql -h localhost -U postgres -d ln2 -f schema.sql

# 2. Install the service (from the BlueFors repo)
/home/cdms/bluefors_monitor/deploy/install.sh
```

## Files

| File | Description |
|---|---|
| `ln2_reader.py` | Arduino TCP → PostgreSQL reader (systemd service) |
| `schema.sql` | Database schema |

## History

| Version | Description |
|---|---|
| 1.x | `read_arduino.py` → `arduino_data.txt` → `ingest.py` (cron) → `monitor.py` (cron) + `ln2_query.py` imported by the BlueFors responder. Tag `v1.1.1-legacy`. |
| 2.0.0 | Reader rewritten as a supervised service writing straight to PostgreSQL with spool/replay; alarms, commands and thresholds merged into the BlueFors `bfm` package (one state, one Slack queue). |
