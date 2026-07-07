#!/usr/bin/env python3
"""LN2 scale query helpers — used by the shared Slack responder to answer
natural-language questions about the liquid-nitrogen scale.

Public functions return ready-to-send Slack text (or an image path for plots).
"""

import sys
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg2

LOCAL_TZ = ZoneInfo("America/Chicago")

sys.path.insert(0, str(Path(__file__).parent))
import ln2_config as config

# Keywords (EN + 中文) that mean "the user is asking about the LN2 scale".
LN2_RE = re.compile(
    r"\bln2\b|liquid\s*nitrogen|nitrogen|\bweight\b|\bscale\b|"
    r"液氮|氮气|氮|秤|重量|称重|磅",
    re.IGNORECASE,
)
# Does the request ask for a plot / trend?
PLOT_RE = re.compile(r"\bplot\b|\bgraph\b|\btrend\b|\bchart\b|图|曲线|趋势", re.IGNORECASE)
# Duration like 2h / 30min / 3 days
DUR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|min|m)\b", re.I)


def _conn():
    return psycopg2.connect(host=config.PG_HOST, port=config.PG_PORT,
                            user=config.PG_USER, password=config.PG_PASSWORD,
                            dbname=config.PG_DB, connect_timeout=5)


def _latest(cur):
    cur.execute("SELECT time, weight, temp, humidity "
                "FROM scale_readings ORDER BY time DESC LIMIT 1")
    return cur.fetchone()


def _weight_ago(cur, minutes):
    cur.execute("SELECT weight FROM scale_readings "
                "WHERE time <= now() - (%s || ' minutes')::interval "
                "ORDER BY time DESC LIMIT 1", (minutes,))
    r = cur.fetchone()
    return r[0] if r else None


def status_text() -> str:
    """Current LN2 scale reading + 1-hour weight trend."""
    u = config.WEIGHT_UNIT
    try:
        conn = _conn()
    except Exception as e:
        return f":warning: Cannot read LN2 database: {e}"
    try:
        with conn.cursor() as cur:
            row = _latest(cur)
            if row is None:
                return "No LN2 scale data yet."
            ts, weight, temp, humidity = row
            w1h = _weight_ago(cur, 60)
    finally:
        conn.close()

    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60
    fresh = "" if age_min <= config.DATA_STALE_MINUTES else \
        f"\n:sos: _data is {age_min:.0f} min old — scale may be offline_"

    trend = ""
    if w1h is not None and weight is not None:
        d = weight - w1h
        arrow = ":arrow_up:" if d > 0.001 else ":arrow_down:" if d < -0.001 else ":left_right_arrow:"
        trend = f"\n  • 1 h change: {arrow} `{d:+.2f} {u}`"

    return (":balance_scale: *LN2 Scale — current reading*\n"
            f"  • Weight: `{weight:g} {u}`\n"
            f"  • Temperature: `{temp:g} °C`\n"
            f"  • Humidity: `{humidity:g} %`{trend}\n"
            f"_at {str(ts)[:19]}_{fresh}")


def plot_weight(minutes: float = 120) -> tuple:
    """Return (image_path, caption) for a weight-vs-time plot, or (None, msg)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    try:
        conn = _conn()
    except Exception as e:
        return None, f":warning: Cannot read LN2 database: {e}"
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT time, weight FROM scale_readings "
                        "WHERE time >= now() - (%s || ' minutes')::interval "
                        "ORDER BY time", (minutes,))
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        return None, "No LN2 data in that time range."

    # Convert to local wall-clock time so the axis isn't shown in UTC (which
    # looks ~5 h in the "future" against CDT).
    times   = [r[0].astimezone(LOCAL_TZ).replace(tzinfo=None) for r in rows]
    weights = [r[1] for r in rows]
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(times, weights, "-", color="#1f77b4", linewidth=1.3)
    rng = f"{minutes/1440:g} days" if minutes >= 1440 else \
          f"{minutes/60:g} h" if minutes >= 60 else f"{minutes:g} min"
    ax.set_title(f"LN2 Scale Weight — last {rng} (CDT)  ({len(rows)} points)")
    ax.set_ylabel(f"Weight ({config.WEIGHT_UNIT})")
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    fig.autofmt_xdate()
    out = Path(__file__).parent / "ln2_weight.png"
    fig.savefig(out, dpi=110, bbox_inches="tight")
    plt.close(fig)
    return str(out), f"LN2 weight — last {rng}"


def handle(text: str):
    """Route an LN2 request. Returns ('text', str) or ('plot', (path, caption))."""
    if PLOT_RE.search(text):
        m = DUR_RE.search(text)
        minutes = 120
        if m:
            n, unit = float(m.group(1)), m.group(2).lower()
            minutes = n * (1440 if unit.startswith("d") else 60 if unit.startswith("h") else 1)
        return "plot", plot_weight(minutes)
    return "text", status_text()
