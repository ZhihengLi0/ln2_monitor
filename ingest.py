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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ln2-ingest] %(levelname)s %(message)s",
    handlers=[logging.FileHandler(Path(__file__).parent / "ingest.log"),
              logging.StreamHandler()],
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

    rows = []
    for line in lines:
        m = LINE_RE.match(line.strip())
        if not m:
            continue
        ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=LOCAL_TZ)
        rows.append((ts, float(m.group(2)), float(m.group(3)), float(m.group(4))))

    if rows:
        conn = psycopg2.connect(host=config.PG_HOST, port=config.PG_PORT,
                                user=config.PG_USER, password=config.PG_PASSWORD,
                                dbname=config.PG_DB, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO scale_readings (time, weight, temp, humidity) "
                    "VALUES (%s, %s, %s, %s)", rows)
            conn.commit()
        finally:
            conn.close()
        log.info(f"Ingested {len(rows)} reading(s); offset {offset} → {new_offset}")

    save_offset(new_offset)


if __name__ == "__main__":
    main()
