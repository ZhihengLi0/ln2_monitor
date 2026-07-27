#!/usr/bin/env python3
"""Ingest Arduino LN2-scale readings from the text file into PostgreSQL.

The Arduino appends one line every ~5 s:
    2026-07-07 10:11:57  Weight:0.14,Temp:24.20C,Humidity:51.00%

Runs every minute via cron. Tracks a byte offset so only new lines are parsed;
malformed lines (e.g. Arduino startup fragments) are skipped.
"""

import os
import re
import sys
import json
import logging
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
import ln2_config as config

_handlers = [logging.FileHandler(Path(__file__).parent / "ingest.log")]
if sys.stdout.isatty():                       # console only when run interactively
    _handlers.append(logging.StreamHandler())
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ln2-ingest] %(levelname)s %(message)s",
    handlers=_handlers,
)
log = logging.getLogger("ln2-ingest")

STATE_FILE = Path(__file__).parent / "ingest_state.json"
LOCAL_TZ   = ZoneInfo("America/Chicago")

# TIMESTAMP  Weight:<w>,Temp:<t>C,Humidity:<h>%
LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+"
    r"Weight:(-?[\d.]+),Temp:(-?[\d.]+)C,Humidity:(-?[\d.]+)%"
)


def load_offset() -> int:
    if STATE_FILE.exists():
        try:
            return int(json.loads(STATE_FILE.read_text()).get("offset", 0))
        except Exception:
            return 0
    return 0


def save_offset(offset: int):
    tmp = STATE_FILE.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"offset": offset}))
    tmp.replace(STATE_FILE)


def main():
    path = Path(config.ARDUINO_FILE)
    if not path.exists():
        log.warning(f"Arduino file not found: {path}")
        return

    offset = load_offset()
    size   = path.stat().st_size
    if size < offset:               # file rotated / truncated → start over
        log.info("File shrank — resetting offset to 0")
        offset = 0

    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    new_offset = offset + len(data)

    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if not text.endswith("\n") and lines:
        partial = lines.pop()        # incomplete final line — leave for next run
        new_offset -= len(partial.encode("utf-8"))

    # Parse lines first; DB filtering (spike detection) happens below once we know
    # the last valid weight for continuity across runs.
    parsed = []
    for line in lines:
        m = LINE_RE.match(line.strip())
        if not m:
            continue
        ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=LOCAL_TZ)
        parsed.append((ts, float(m.group(2)), float(m.group(3)), float(m.group(4))))

    if not parsed:
        save_offset(new_offset)
        return

    conn = psycopg2.connect(host=config.PG_HOST, port=config.PG_PORT,
                            user=config.PG_USER, password=config.PG_PASSWORD,
                            dbname=config.PG_DB, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT weight FROM scale_readings WHERE weight >= 0 "
                        "ORDER BY time DESC LIMIT 1")
            r = cur.fetchone()
        last_valid = r[0] if r else None

        max_jump = getattr(config, "WEIGHT_MAX_JUMP", 5.0)
        rows, neg, spike, streak = [], 0, 0, 0
        for ts, weight, temp, humidity in parsed:
            if weight < 0:                      # impossible → NULL
                weight = None; neg += 1
            elif last_valid is not None and abs(weight - last_valid) > max_jump:
                streak += 1
                if streak >= 4:                 # sustained new level → accept baseline
                    last_valid = weight; streak = 0
                else:                           # transient spike → NULL
                    weight = None; spike += 1
            else:
                last_valid = weight; streak = 0
            rows.append((ts, weight, temp, humidity))

        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO scale_readings (time, weight, temp, humidity) "
                "VALUES (%s, %s, %s, %s)", rows)
        conn.commit()
    finally:
        conn.close()

    dropped = []
    if neg:   dropped.append(f"{neg} negative")
    if spike: dropped.append(f"{spike} spike(>{max_jump}kg)")
    log.info(f"Ingested {len(rows)} reading(s); offset {offset} → {new_offset}"
             + (f"; dropped weight: {', '.join(dropped)}" if dropped else ""))
    save_offset(new_offset)


if __name__ == "__main__":
    main()
