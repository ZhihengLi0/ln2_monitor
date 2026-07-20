#!/usr/bin/env python3
"""LN2 scale query helpers — used by the shared Slack responder to answer
natural-language questions about the liquid-nitrogen scale.

Public functions return ready-to-send Slack text (or an image path for plots).
"""

import sys
import re
import math
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg2

LOCAL_TZ = ZoneInfo("America/Chicago")


def dew_point(temp_c, rh):
    """Dew point (°C) from temperature (°C) and relative humidity (%),
    via the Magnus-Tetens approximation. Returns None if inputs are invalid."""
    if temp_c is None or rh is None or rh <= 0:
        return None
    a, b = 17.62, 243.12
    gamma = math.log(rh / 100.0) + (a * temp_c) / (b + temp_c)
    return (b * gamma) / (a - gamma)

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
# Silence / mute the LN2 alarms
SILENCE_RE = re.compile(r"silence|mute|quiet|snooze|stop.*alert|静音|安静|别报|停止", re.IGNORECASE)
UNSILENCE_RE = re.compile(r"unsilence|unmute|resume|un-?silence|恢复|取消静音|解除", re.IGNORECASE)

STATE_FILE = Path(__file__).parent / "monitor_state.json"


def _parse_duration_minutes(text: str):
    """Minutes from a phrase like '2h', '30 min', '2 days'. Default 120 if a
    silence keyword is present but no duration. Returns None if unparseable."""
    m = DUR_RE.search(text)
    if not m:
        return 120.0
    n, unit = float(m.group(1)), m.group(2).lower()
    if unit.startswith("d"):
        return n * 1440
    if unit.startswith("h") or unit.startswith("hr"):
        return n * 60
    return n


def silence_alarms(minutes: float) -> str:
    """Silence ALL LN2 alarms for `minutes` by writing silence_until into the
    ln2 monitor state file (the ln2 cron reads it each run). Returns Slack text."""
    import json
    try:
        state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    except Exception:
        state = {}
    until = datetime.now() + timedelta(minutes=minutes)
    state.setdefault("silence_until", {})["__all__"] = until.isoformat()
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, default=str, indent=2))
    tmp.replace(STATE_FILE)
    if minutes >= 1440:
        dur = f"{minutes/1440:g} day(s)"
    elif minutes >= 60:
        dur = f"{minutes/60:g} hour(s)"
    else:
        dur = f"{minutes:g} min"
    return (f":no_bell: *LN2 alarms silenced for {dur}* (until "
            f"{until.astimezone(LOCAL_TZ).strftime('%m-%d %H:%M')} CDT).\n"
            "Say `resume LN2 alerts` to re-enable early.")


def unsilence_alarms() -> str:
    import json
    try:
        state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    except Exception:
        state = {}
    state.setdefault("silence_until", {}).pop("__all__", None)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, default=str, indent=2))
    tmp.replace(STATE_FILE)
    return ":bell: *LN2 alarms re-enabled.*"


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


def latest_dewpoint():
    """Return (dew_point_C, temp_C, humidity, ts) for the latest LN2 reading,
    or None. Used by the fridge monitor for the coolant-in vs dew-point alarm."""
    try:
        conn = _conn()
    except Exception:
        return None
    try:
        with conn.cursor() as cur:
            row = _latest(cur)
    finally:
        conn.close()
    if not row:
        return None
    ts, weight, temp, humidity = row
    dp = dew_point(temp, humidity)
    if dp is None:
        return None
    return dp, temp, humidity, ts


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

    dp = dew_point(temp, humidity)
    dp_line = f"\n  • Dew point: `{dp:.1f} °C`" if dp is not None else ""

    return (":balance_scale: *LN2 Scale — current reading*\n"
            f"  • Weight: `{weight:g} {u}`\n"
            f"  • Temperature: `{temp:g} °C`\n"
            f"  • Humidity: `{humidity:g} %`"
            f"{dp_line}{trend}\n"
            f"_at {str(ts)[:19]}_{fresh}")


# Which quantity to plot: (db_column, label, unit). Weight unit comes from config.
# "dewpoint" is computed from temp + humidity rather than being a column.
def _quantity(text: str):
    t = text.lower()
    if re.search(r"dew|露点", t):
        return "dewpoint", "Dew Point", "°C"
    if re.search(r"temp|温度", t):
        return "temp", "Temperature", "°C"
    if re.search(r"humid|湿度|潮", t):
        return "humidity", "Humidity", "%"
    return "weight", "Weight", config.WEIGHT_UNIT


def plot_quantity(column: str, label: str, unit: str, minutes: float = 120) -> tuple:
    """Return (image_path, caption) for a <column>-vs-time plot, or (None, msg)."""
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
            if column == "dewpoint":
                cur.execute("SELECT time, temp, humidity FROM scale_readings "
                            "WHERE time >= now() - (%s || ' minutes')::interval "
                            "ORDER BY time", (minutes,))
                rows = [(t, dew_point(tp, h)) for t, tp, h in cur.fetchall()]
                rows = [r for r in rows if r[1] is not None]
            else:
                cur.execute(f"SELECT time, {column} FROM scale_readings "
                            "WHERE time >= now() - (%s || ' minutes')::interval "
                            "ORDER BY time", (minutes,))
                rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        return None, "No LN2 data in that time range."

    # Convert to local wall-clock time so the axis isn't shown in UTC (which
    # looks ~5 h in the "future" against CDT).
    times = [r[0].astimezone(LOCAL_TZ).replace(tzinfo=None) for r in rows]
    vals  = [r[1] for r in rows]
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(times, vals, "-", color="#1f77b4", linewidth=1.3)
    rng = f"{minutes/1440:g} days" if minutes >= 1440 else \
          f"{minutes/60:g} h" if minutes >= 60 else f"{minutes:g} min"
    ax.set_title(f"LN2 Scale {label} — last {rng} (CDT)  ({len(rows)} points)")
    ax.set_ylabel(f"{label} ({unit})")
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    fig.autofmt_xdate()
    out = Path(__file__).parent / f"ln2_{column}.png"
    fig.savefig(out, dpi=110, bbox_inches="tight")
    plt.close(fig)
    return str(out), f"LN2 {label.lower()} — last {rng}"


def handle(text: str):
    """Route an LN2 request. Returns ('text', str) or ('plot', (path, caption))."""
    # Silence / resume (check before plot so 'stop LN2 alerts' isn't a plot).
    if UNSILENCE_RE.search(text):
        return "text", unsilence_alarms()
    if SILENCE_RE.search(text):
        return "text", silence_alarms(_parse_duration_minutes(text))
    if PLOT_RE.search(text):
        m = DUR_RE.search(text)
        minutes = 120
        if m:
            n, unit = float(m.group(1)), m.group(2).lower()
            minutes = n * (1440 if unit.startswith("d") else 60 if unit.startswith("h") else 1)
        column, label, u = _quantity(text)
        return "plot", plot_quantity(column, label, u, minutes)
    return "text", status_text()
