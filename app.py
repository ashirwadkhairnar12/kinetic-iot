"""
IoT Dashboard — Flask application
Connects to a host-native TimescaleDB instance (running directly on EC2, not in Docker).

The container reaches the host via Docker's bridge gateway (172.17.0.1 by default,
or whatever DB_HOST is set to in the .env file).  A retry loop on startup handles
the brief window where PostgreSQL may not be ready when the container first starts.
"""

import os
import time
import logging
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("dashboard")

# ---------------------------------------------------------------------------
# DB config — read once from environment
# ---------------------------------------------------------------------------
DB_CONFIG = {
    "host":    os.environ.get("DB_HOST", "172.17.0.1"),   # Docker bridge → host machine
    "port":    int(os.environ.get("DB_PORT", 5432)),
    "dbname":  os.environ.get("DB_NAME", "iot_data"),
    "user":    os.environ.get("DB_USER", "iot_user"),
    "password": os.environ.get("DB_PASSWORD", "iot_password"),
}


def get_conn():
    """
    Open a fresh connection to the host-native TimescaleDB.
    Retries up to 10 times with 3-second back-off — useful at container
    startup when PostgreSQL may still be initialising.
    """
    last_err = None
    for attempt in range(1, 11):
        try:
            return psycopg2.connect(
                **DB_CONFIG,
                cursor_factory=psycopg2.extras.RealDictCursor,
                connect_timeout=5,
            )
        except psycopg2.OperationalError as exc:
            last_err = exc
            log.warning("DB connection attempt %d/10 failed: %s — retrying in 3s …", attempt, exc)
            time.sleep(3)
    raise RuntimeError(f"Cannot connect to TimescaleDB after 10 attempts: {last_err}")


def query(sql, params=None):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Routes — UI
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Routes — API
# ---------------------------------------------------------------------------

@app.route("/api/devices")
def api_devices():
    """List all known devices with their latest reading."""
    rows = query("""
        SELECT DISTINCT ON (device_id)
            device_id, device_type, status, battery_pct,
            temperature_c, humidity_pct, power_watts, time AS last_seen
        FROM sensor_readings
        ORDER BY device_id, time DESC
    """)
    return jsonify(list(rows))


@app.route("/api/summary")
def api_summary():
    """Aggregate KPIs for the stats cards."""
    rows = query("""
        SELECT
            COUNT(DISTINCT device_id)                          AS total_devices,
            COUNT(*)                                           AS total_readings,
            ROUND(AVG(temperature_c)::numeric, 2)             AS avg_temperature,
            ROUND(AVG(humidity_pct)::numeric, 2)              AS avg_humidity,
            ROUND(AVG(power_watts)::numeric, 2)               AS avg_power,
            COUNT(*) FILTER (WHERE status != 'OK')            AS alert_count
        FROM sensor_readings
        WHERE time > NOW() - INTERVAL '1 hour'
    """)
    return jsonify(rows[0] if rows else {})


@app.route("/api/timeseries/<device_id>")
def api_timeseries(device_id):
    """
    Time-bucketed averages for a given device.
    Query params:
        metric   — temperature_c | humidity_pct | power_watts | vibration_ms2  (default: temperature_c)
        hours    — look-back window in hours                                    (default: 6)
        bucket   — bucket size, e.g. '5 minutes' | '1 hour'                    (default: 5 minutes)
    """
    metric_map = {
        "temperature_c":  "temperature_c",
        "humidity_pct":   "humidity_pct",
        "power_watts":    "power_watts",
        "vibration_ms2":  "vibration_ms2",
        "battery_pct":    "battery_pct",
        "pressure_hpa":   "pressure_hpa",
    }
    metric = metric_map.get(request.args.get("metric", "temperature_c"), "temperature_c")
    hours  = min(int(request.args.get("hours", 6)), 168)   # cap at 7 days
    bucket = request.args.get("bucket", "5 minutes")

    # Whitelist bucket to prevent injection
    allowed_buckets = {"1 minute", "5 minutes", "15 minutes", "30 minutes", "1 hour", "6 hours", "1 day"}
    if bucket not in allowed_buckets:
        bucket = "5 minutes"

    sql = f"""
        SELECT
            time_bucket(%s, time) AS bucket,
            ROUND(AVG({metric})::numeric, 3) AS value
        FROM sensor_readings
        WHERE device_id = %s
          AND time > NOW() - INTERVAL '{hours} hours'
        GROUP BY bucket
        ORDER BY bucket ASC
    """
    rows = query(sql, (bucket, device_id))
    return jsonify([{"time": r["bucket"].isoformat(), "value": float(r["value"] or 0)} for r in rows])


@app.route("/api/alerts")
def api_alerts():
    """Recent non-OK status events."""
    rows = query("""
        SELECT time, device_id, status, battery_pct, temperature_c
        FROM sensor_readings
        WHERE status != 'OK'
        ORDER BY time DESC
        LIMIT 50
    """)
    return jsonify(list(rows))


@app.route("/api/heatmap")
def api_heatmap():
    """Latest reading per device for the map view."""
    rows = query("""
        SELECT DISTINCT ON (device_id)
            device_id, device_type, latitude, longitude,
            temperature_c, status, time AS last_seen
        FROM sensor_readings
        WHERE latitude IS NOT NULL
        ORDER BY device_id, time DESC
    """)
    return jsonify(list(rows))


@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat()})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=os.environ.get("FLASK_DEBUG", "0") == "1")
