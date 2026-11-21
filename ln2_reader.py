#!/usr/bin/env python3
"""LN2 scale reader service: Arduino (TCP) → PostgreSQL `ln2.scale_readings`.

Replaces read_arduino.py + arduino_data.txt + ingest.py (cron). Why:
  * the old reader had no socket timeout and no EOF handling — after the
    Arduino dropped the connection it spun at 100 % CPU forever and the
    watchdog could not tell;
  * the text file grew without bound (143 MB) and ingest tracked a byte
    offset that broke on any rotation;
  * data reached the database up to 60 s late.

Behaviour:
  * connect with a timeout, TCP keepalive, reconnect with backoff on EOF /
    timeout / error;
  * parse "Weight:11.53,Temp:21.20C,Humidity:37.00%" lines; drop negative
    weights and jumps > WEIGHT_MAX_JUMP (scale glitches) exactly like ingest.py;
  * INSERT each reading immediately; if the database is down, append the line
    to a spool file and replay it when the database is back — no data loss;
  * log a line every LOG_EVERY readings (not every reading).

Runs under systemd (deploy/ln2-reader.service): Restart=always, WatchdogSec.
"""
from __future__ import annotations

import logging
import os
import re
import signal
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg2

sys.path.insert(0, "/home/cdms/bluefors_monitor")
try:
    from config_secret import LOCAL_PG_PASSWORD
except ImportError:
    LOCAL_PG_PASSWORD = None

HOST, PORT = "172.31.255.80", 5000
PG = dict(host="localhost", port=5432, user="postgres", dbname="ln2", connect_timeout=5)
if LOCAL_PG_PASSWORD:
    PG["password"] = LOCAL_PG_PASSWORD
SPOOL = Path("/mnt/harddrive/pi/logs/ln2_spool.txt")
SOCKET_TIMEOUT = 60          # the scale reports every ~5 s; 60 s of silence = dead link
RECONNECT_BACKOFF = (2, 5, 10, 30, 60)
WEIGHT_MAX_JUMP = 5.0
LOG_EVERY = 720              # ≈ one log line per hour at 5 s/reading

LINE_RE = re.compile(r"Weight:\s*(-?[\d.]+)\s*,\s*Temp:\s*(-?[\d.]+)\s*C?\s*,\s*Humidity:\s*(-?[\d.]+)")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [ln2-reader] %(levelname)s %(message)s")
log = logging.getLogger("ln2-reader")
stop = False


def sd_notify(msg: str) -> None:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    try:
        if addr.startswith("@"):
            addr = "\0" + addr[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
    except OSError:
        pass


class Sink:
    """Database writer with spool fallback."""
    def __init__(self):
        self.conn = None
        self.last_weight = None
        self.n_ok = self.n_spooled = self.n_rejected = 0

    def _connect(self):
        if self.conn is None or self.conn.closed:
            self.conn = psycopg2.connect(**PG)
            self.conn.autocommit = True
            r = self.conn.cursor()
            r.execute("SELECT weight FROM scale_readings WHERE weight >= 0 ORDER BY time DESC LIMIT 1")
            row = r.fetchone()
            self.last_weight = float(row[0]) if row else None
        return self.conn

    def accept(self, weight: float) -> bool:
        if weight < 0:
            return False
        if self.last_weight is not None and abs(weight - self.last_weight) > WEIGHT_MAX_JUMP:
            return False
        return True

    def write(self, ts: datetime, weight: float, temp: float, hum: float) -> None:
        if not self.accept(weight):
            self.n_rejected += 1
            return
        try:
            self._connect().cursor().execute(
                "INSERT INTO scale_readings (time, weight, temp, humidity) VALUES (%s,%s,%s,%s)",
                (ts, weight, temp, hum))
            self.last_weight = weight
            self.n_ok += 1
            self.replay_spool()
        except Exception as e:
            self.conn = None
            self.n_spooled += 1
            if self.n_spooled in (1, 10, 100) or self.n_spooled % 1000 == 0:
                log.error("db write failed (%s) — spooling (%d so far)", str(e).splitlines()[0][:120], self.n_spooled)
            SPOOL.parent.mkdir(parents=True, exist_ok=True)
            with SPOOL.open("a") as f:
                f.write(f"{ts.isoformat()}\t{weight}\t{temp}\t{hum}\n")

    def replay_spool(self) -> None:
        if not SPOOL.exists() or SPOOL.stat().st_size == 0:
            return
        lines = SPOOL.read_text().splitlines()
        cur = self._connect().cursor()
        n = 0
        for line in lines:
            try:
                ts, w, t, h = line.split("\t")
                cur.execute("INSERT INTO scale_readings (time, weight, temp, humidity) VALUES (%s,%s,%s,%s)",
                            (ts, float(w), float(t), float(h)))
                n += 1
            except Exception as e:
                log.error("spool replay stopped at line %d: %s", n, e)
                SPOOL.write_text("\n".join(lines[n:]) + "\n")
                return
        SPOOL.unlink(missing_ok=True)
        log.info("replayed %d spooled reading(s) into the database", n)


def read_forever() -> None:
    sink = Sink()
    fails = 0
    n = 0
    while not stop:
        try:
            with socket.create_connection((HOST, PORT), timeout=10) as s:
                s.settimeout(SOCKET_TIMEOUT)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                s.sendall(b"hello\n")
                log.info("connected to %s:%d", HOST, PORT)
                fails = 0
                buf = b""
                while not stop:
                    data = s.recv(1024)
                    if not data:
                        raise ConnectionError("EOF from Arduino")
                    buf += data
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        m = LINE_RE.search(line.decode(errors="replace"))
                        if not m:
                            continue
                        now = datetime.now(timezone.utc)
                        sink.write(now, float(m.group(1)), float(m.group(2)), float(m.group(3)))
                        n += 1
                        sd_notify("WATCHDOG=1")
                        if n % LOG_EVERY == 0:
                            log.info("%d readings (db %d, spooled %d, rejected %d); last %.2f kg %.1f C %.0f%%",
                                     n, sink.n_ok, sink.n_spooled, sink.n_rejected,
                                     float(m.group(1)), float(m.group(2)), float(m.group(3)))
        except Exception as e:
            fails += 1
            delay = RECONNECT_BACKOFF[min(fails, len(RECONNECT_BACKOFF)) - 1]
            if fails in (1, 3, 10) or fails % 60 == 0:
                log.warning("link lost (%s) — reconnecting in %ds (attempt %d)", e, delay, fails)
            for _ in range(delay):
                if stop:
                    return
                time.sleep(1)


def main() -> None:
    def _stop(*_):
        global stop
        stop = True
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    sd_notify("READY=1")
    read_forever()
    log.info("stopped")


if __name__ == "__main__":
    main()
