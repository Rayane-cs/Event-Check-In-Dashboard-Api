import os
from urllib.parse import urlparse

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS
import mysql.connector
from mysql.connector import pooling
from werkzeug.exceptions import HTTPException

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

app = Flask(__name__)


def _parse_origins():
    raw = os.getenv("ALLOWED_ORIGINS", "*").strip()
    if raw == "*":
        return "*"
    return [o.strip() for o in raw.split(",") if o.strip()]


_origins = _parse_origins()
if _origins == "*":
    CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=False)
else:
    CORS(
        app,
        resources={r"/api/*": {"origins": _origins}},
        supports_credentials=False,
    )


def _add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = (
        "Content-Type, Authorization, X-Dashboard-Secret, X-Requested-With"
    )
    return response


@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        from flask import make_response

        resp = make_response("", 204)
        return _add_cors(resp)


@app.after_request
def after_request(response):
    return _add_cors(response)


@app.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, HTTPException):
        return _add_cors(e)
    import traceback

    traceback.print_exc()
    response = jsonify({"error": str(e)})
    response.status_code = 500
    return _add_cors(response)


def parse_mysql_url(url: str):
    parsed = urlparse(url)
    if parsed.scheme not in ("mysql", "mysql+mysqlconnector", "mysql+pymysql"):
        raise ValueError(f"Unsupported DB URL scheme: {parsed.scheme}")
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 3306,
        "user": parsed.username or "root",
        "password": parsed.password or "",
        "database": parsed.path.lstrip("/") or "railway",
    }


def get_db_config():
    for env_name in ("MYSQL_URL", "DATABASE_URL", "DB_URL"):
        val = os.getenv(env_name, "")
        if val.startswith(("mysql://", "mysql+mysqlconnector://", "mysql+pymysql://")):
            return parse_mysql_url(val)
    return {
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", 3306)),
        "user": os.getenv("DB_USER", "root"),
        "password": os.getenv("DB_PASSWORD", ""),
        "database": os.getenv("DB_NAME", "railway"),
    }


DB_CONFIG = get_db_config()
db_connection = None
last_db_error = None


def get_db():
    global db_connection, last_db_error
    try:
        if db_connection is not None and db_connection.is_connected():
            return db_connection
        db_connection = mysql.connector.connect(**DB_CONFIG)
        last_db_error = None
        return db_connection
    except Exception as e:
        last_db_error = str(e)
        raise RuntimeError(f"Database unavailable: {last_db_error}")


DASHBOARD_SECRET = os.getenv("DASHBOARD_SECRET", "").strip()
DAY_16 = os.getenv("EVENT_DAY_16", "2026-04-16").strip()
DAY_19 = os.getenv("EVENT_DAY_19", "2026-04-19").strip()


def _auth_ok():
    if not DASHBOARD_SECRET:
        return False
    sent = request.headers.get("X-Dashboard-Secret", "").strip()
    return sent == DASHBOARD_SECRET


@app.route("/", methods=["GET"])
def root():
    return jsonify({"service": "event-check-in-dashboard-api", "status": "ok"}), 200


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify(
        {
            "status": "ok",
            "db_error": last_db_error,
            "auth_configured": bool(DASHBOARD_SECRET),
        }
    ), 200


@app.route("/api/checkins", methods=["GET"])
def list_checkins():
    if not _auth_ok():
        r = jsonify({"error": "Unauthorized"})
        r.status_code = 401
        return _add_cors(r)

    flt = (request.args.get("filter") or "all").strip().lower()
    conn = cur = None
    try:
        conn = get_db()
        cur = conn.cursor(dictionary=True)

        if flt == "16":
            cur.execute(
                """
                SELECT id, full_name, speciality, level, feedback, attendance_date, created_at
                FROM event_checkins
                WHERE attendance_date = %s OR DATE(created_at) = %s
                ORDER BY created_at DESC
                """,
                (DAY_16, DAY_16),
            )
        elif flt == "19":
            cur.execute(
                """
                SELECT id, full_name, speciality, level, feedback, attendance_date, created_at
                FROM event_checkins
                WHERE attendance_date = %s OR DATE(created_at) = %s
                ORDER BY created_at DESC
                """,
                (DAY_19, DAY_19),
            )
        else:
            cur.execute(
                """
                SELECT id, full_name, speciality, level, feedback, attendance_date, created_at
                FROM event_checkins
                ORDER BY attendance_date DESC, created_at DESC
                """
            )

        rows = cur.fetchall()
        for r in rows:
            if r.get("created_at"):
                r["created_at"] = r["created_at"].isoformat()
            if r.get("attendance_date"):
                r["attendance_date"] = str(r["attendance_date"])
        
        response = jsonify(rows)
        response.headers["Cache-Control"] = "public, s-maxage=10, stale-while-revalidate=30"
        return response, 200
    except Exception as e:
        print(f"[checkins] {e}")
        return jsonify({"error": "Failed to load check-ins"}), 500
    finally:
        if cur:
            cur.close()
        # Do not close conn to keep the global connection alive


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    from waitress import serve
    serve(app, host="0.0.0.0", port=port, threads=16)
