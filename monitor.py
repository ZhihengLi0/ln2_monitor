#!/usr/bin/env python3
"""LN2 scale monitor — reads the ln2 database and sends Slack alerts.

Runs every minute via cron. Alerts (to the shared BlueFors Slack channel) on:
  - LN2 weight low / critical (time to refill)
  - Temperature out of range
  - Data staleness (Arduino/scale stopped reporting)

Usage:
  python3 monitor.py            # normal alert run
  python3 monitor.py --status   # post the current reading to Slack on demand
"""

import sys
import json
import logging
import requests
import psycopg2
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).parent))
import ln2_config as config

STATUS_MODE = "--status" in sys.argv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ln2-monitor] %(levelname)s %(message)s",
    handlers=[logging.FileHandler(Path(__file__).parent / "monitor.log"),
              logging.StreamHandler()],
)
log = logging.getLogger("ln2-monitor")

STATE_FILE = Path(__file__).parent / "monitor_state.json"


# ── Slack ──────────────────────────────────────────────────────────────────────

def send_slack(text: str, color: str = "danger", thread_ts: str = None):
    if not config.SLACK_BOT_TOKEN or config.SLACK_BOT_TOKEN.startswith("YOUR_"):
        log.warning(f"[SLACK NOT CONFIGURED] {text}")
        return None
    payload = {
        "channel": config.SLACK_CHANNEL,
        "attachments": [{"color": color, "text": text, "mrkdwn_in": ["text"]}],
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts
    try:
        r = requests.post("https://slack.com/api/chat.postMessage",
                          headers={"Authorization": f"Bearer {config.SLACK_BOT_TOKEN}"},
                          json=payload, timeout=10)
        resp = r.json()
        if not resp.get("ok"):
            log.error(f"Slack error: {resp.get('error')}")
        return resp.get("ts")
    except Exception as e:
        log.error(f"Slack send failed: {e}")
        return None


# ── State (alert cooldowns) ────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            log.error("State file corrupt — starting fresh")
    return {"last_alert_time": {}}


def save_state(state: dict):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, default=str, indent=2))
    tmp.replace(STATE_FILE)


def _cooldown_ok(state: dict, key: str) -> bool:
    last = state.get("last_alert_time", {}).get(key)
    if last:
        if datetime.now() - datetime.fromisoformat(last) < timedelta(
                minutes=config.ALERT_COOLDOWN_MINUTES):
            return False
    state.setdefault("last_alert_time", {})[key] = datetime.now().isoformat()
    return True


# ── DB ─────────────────────────────────────────────────────────────────────────

def local_conn():
    return psycopg2.connect(host=config.PG_HOST, port=config.PG_PORT,
                            user=config.PG_USER, password=config.PG_PASSWORD,
                            dbname=config.PG_DB, connect_timeout=5)


def latest_reading(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT time, weight, temp, humidity "
                    "FROM scale_readings ORDER BY time DESC LIMIT 1")
        return cur.fetchone()


# ── Checks ─────────────────────────────────────────────────────────────────────

def check_reading(conn, state: dict) -> list:
    row = latest_reading(conn)
    if row is None:
        return []
    ts, weight, temp, humidity = row
    u = config.WEIGHT_UNIT
    alerts = []

    # Data freshness
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60
    if age_min > config.DATA_STALE_MINUTES:
        if _cooldown_ok(state, "stale"):
            alerts.append((":sos: *LN2 scale data stale* — no new reading for "
                           f"{age_min:.0f} min. Arduino/scale may be disconnected.", "danger"))
        return alerts        # don't trust threshold checks on stale data

    # Weight
    if weight is not None and config.WEIGHT_CRITICAL is not None and weight < config.WEIGHT_CRITICAL:
        if _cooldown_ok(state, "weight_crit"):
            alerts.append((f":rotating_light: *CRITICAL — LN2 weight very low* :rotating_light:\n"
                           f"Weight `{weight:g} {u}` (critical below {config.WEIGHT_CRITICAL:g} {u}). "
                           "Refill liquid nitrogen now.", "danger"))
    elif weight is not None and config.WEIGHT_LOW is not None and weight < config.WEIGHT_LOW:
        if _cooldown_ok(state, "weight_low"):
            alerts.append((f":warning: *LN2 weight low* — `{weight:g} {u}` "
                           f"(below {config.WEIGHT_LOW:g} {u}). Consider refilling.", "warning"))

    # Temperature
    if temp is not None and config.TEMP_HIGH is not None and temp > config.TEMP_HIGH:
        if _cooldown_ok(state, "temp_high"):
            alerts.append((f":warning: *Temperature high* — `{temp:g} °C` "
                           f"(above {config.TEMP_HIGH:g} °C).", "warning"))
    if temp is not None and config.TEMP_LOW is not None and temp < config.TEMP_LOW:
        if _cooldown_ok(state, "temp_low"):
            alerts.append((f":warning: *Temperature low* — `{temp:g} °C` "
                           f"(below {config.TEMP_LOW:g} °C).", "warning"))
    return alerts


def post_status(conn):
    row = latest_reading(conn)
    if row is None:
        send_slack("No LN2 scale data yet.", color="#999999")
        return
    ts, weight, temp, humidity = row
    u = config.WEIGHT_UNIT
    msg = (":balance_scale: *LN2 Scale — current reading*\n"
           f"  • Weight: `{weight:g} {u}`\n"
           f"  • Temperature: `{temp:g} °C`\n"
           f"  • Humidity: `{humidity:g} %`\n"
           f"_at {str(ts)[:19]}_")
    send_slack(msg, color="#2196F3")
    log.info("Posted LN2 status")


def run():
    try:
        conn = local_conn()
    except Exception as e:
        log.error(f"DB connect failed: {e}")
        return
    try:
        if STATUS_MODE:
            post_status(conn)
            return
        state = load_state()
        alerts = check_reading(conn, state)
        for text, color in alerts:
            send_slack(text, color=color)
        save_state(state)
        if alerts:
            log.info(f"Sent {len(alerts)} alert(s)")
    finally:
        conn.close()


if __name__ == "__main__":
    run()
