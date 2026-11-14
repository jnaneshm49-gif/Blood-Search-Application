"""BloodLink Mangaluru - emergency blood stock finder (Flask + SQLite).

Public side : search a blood group + location -> nearby hospitals with stock.
Staff side  : hospital blood-bank staff log in and update their own stock
              (manual form, CSV upload, or API push with a per-hospital key).
Admin side  : register hospitals and verify them before they appear publicly.
"""
import csv
import hashlib
import io
import math
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

import click
from flask import (Flask, Response, abort, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

BLOOD_GROUPS = ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"]
KINDS = ["Hospital", "Blood bank"]

FRESH_MINUTES = 6 * 60        # up to 6 h old  -> "fresh"
STALE_MINUTES = 24 * 60       # over 24 h old  -> "stale" (flagged, ranked lower)
MAX_UNITS = 500
DEFAULT_RADIUS_KM = 25
MAX_RADIUS_KM = 200
MAX_CSV_ROWS = 50

IST = timezone(timedelta(hours=5, minutes=30))

# Approximate centre points for the manual-location fallback (no GPS needed).
AREAS = [
    {"name": "Hampankatta (city centre)", "lat": 12.8697, "lng": 74.8424},
    {"name": "Kankanady", "lat": 12.8667, "lng": 74.8546},
    {"name": "Attavar", "lat": 12.8725, "lng": 74.8415},
    {"name": "Bejai", "lat": 12.8860, "lng": 74.8420},
    {"name": "Kadri", "lat": 12.8870, "lng": 74.8500},
    {"name": "Kuntikana", "lat": 12.8919, "lng": 74.8453},
    {"name": "Kulshekar", "lat": 12.9000, "lng": 74.8600},
    {"name": "Pumpwell", "lat": 12.8641, "lng": 74.8660},
    {"name": "Ullal", "lat": 12.8062, "lng": 74.8429},
    {"name": "Deralakatte", "lat": 12.8100, "lng": 74.8620},
    {"name": "Bajpe", "lat": 12.9613, "lng": 74.8900},
    {"name": "Surathkal", "lat": 13.0100, "lng": 74.7940},
    {"name": "Mukka", "lat": 12.9963, "lng": 74.7939},
    {"name": "Mulki", "lat": 13.0910, "lng": 74.7870},
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS hospitals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'Hospital',
    address       TEXT,
    area          TEXT,
    lat           REAL NOT NULL,
    lng           REAL NOT NULL,
    phone         TEXT NOT NULL,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    api_key_hash  TEXT UNIQUE,
    verified      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS blood_stock (
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
    blood_group TEXT NOT NULL,
    units       INTEGER NOT NULL CHECK (units >= 0),
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (hospital_id, blood_group)
);
CREATE TABLE IF NOT EXISTS stock_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
    blood_group TEXT NOT NULL,
    old_units   INTEGER,
    new_units   INTEGER NOT NULL,
    source      TEXT NOT NULL,
    changed_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admins (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stock_group ON blood_stock(blood_group);
CREATE INDEX IF NOT EXISTS idx_log_hospital ON stock_log(hospital_id, id DESC);
"""

# Used so a wrong username takes as long to reject as a wrong password.
_DUMMY_HASH = generate_password_hash("not-a-real-password")


# ---------------------------------------------------------------- helpers ---
def db_connect(path):
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def utcnow_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def minutes_since(ts):
    if not ts:
        return None
    delta = datetime.now(timezone.utc) - parse_iso(ts)
    return max(0, int(delta.total_seconds() // 60))


def freshness(age_minutes):
    if age_minutes is None:
        return "unknown"
    if age_minutes <= FRESH_MINUTES:
        return "fresh"
    if age_minutes <= STALE_MINUTES:
        return "aging"
    return "stale"


def age_label(minutes):
    if minutes is None:
        return "never"
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hr ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


def ist_label(ts):
    if not ts:
        return "-"
    return parse_iso(ts).astimezone(IST).strftime("%d %b %Y, %I:%M %p")


def haversine_km(lat1, lng1, lat2, lng2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def normalize_group(value):
    """'a+', 'A +', 'O negative', 'ab-' -> canonical group, or None if invalid.

    A raw '+' in a query string decodes to a space ('A+' -> 'A '), so a trailing
    space on a bare A/B/AB/O is treated as '+'.
    """
    if value is None:
        return None
    raw = str(value)
    s = raw.upper().replace(" ", "")
    s = s.replace("POSITIVE", "+").replace("NEGATIVE", "-")
    s = s.replace("POS", "+").replace("NEG", "-")
    if raw != raw.rstrip() and s in ("A", "B", "AB", "O"):
        s += "+"
    return s if s in BLOOD_GROUPS else None


def parse_units(value):
    if isinstance(value, bool):
        raise ValueError("not a number")
    if isinstance(value, int):
        n = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise ValueError("must be a whole number")
        n = int(value)
    else:
        n = int(str(value).strip())
    if not 0 <= n <= MAX_UNITS:
        raise ValueError(f"must be between 0 and {MAX_UNITS}")
    return n


def validate_pairs(pairs):
    """pairs: iterable of (group_raw, units_raw) -> (clean dict, error list)."""
    clean, errors = {}, []
    for group_raw, units_raw in pairs:
        group = normalize_group(group_raw)
        if group is None:
            errors.append(f"Unknown blood group: {str(group_raw)[:20]!r}")
            continue
        if group in clean:
            errors.append(f"{group} appears more than once")
            continue
        try:
            clean[group] = parse_units(units_raw)
        except (ValueError, TypeError):
            errors.append(f"{group}: units must be a whole number from 0 to {MAX_UNITS}")
    return clean, errors


def hash_key(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def new_api_key():
    return "bl_" + secrets.token_urlsafe(24)


def set_stock(db, hospital_id, group, units, source, now=None):
    now = now or utcnow_iso()
    old = db.execute(
        "SELECT units FROM blood_stock WHERE hospital_id=? AND blood_group=?",
        (hospital_id, group),
    ).fetchone()
    db.execute(
        "INSERT INTO blood_stock (hospital_id, blood_group, units, updated_at) "
        "VALUES (?,?,?,?) ON CONFLICT(hospital_id, blood_group) "
        "DO UPDATE SET units=excluded.units, updated_at=excluded.updated_at",
        (hospital_id, group, units, now),
    )
    db.execute(
        "INSERT INTO stock_log (hospital_id, blood_group, old_units, new_units, source, changed_at) "
        "VALUES (?,?,?,?,?,?)",
        (hospital_id, group, old["units"] if old else None, units, source, now),
    )


def apply_updates(db, hospital_id, clean, source):
    """Apply a validated {group: units} dict atomically."""
    now = utcnow_iso()
    try:
        for group, units in clean.items():
            set_stock(db, hospital_id, group, units, source, now)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return now


class LoginThrottle:
    """Lock a (ip, account) pair for a while after repeated failures."""

    def __init__(self, max_fails=5, lock_seconds=300):
        self.max_fails = max_fails
        self.lock_seconds = lock_seconds
        self.state = {}

    def locked_for(self, key):
        rec = self.state.get(key)
        if rec and rec[1] > time.time():
            return int(rec[1] - time.time()) + 1
        return 0

    def fail(self, key):
        count, until = self.state.get(key, (0, 0))
        if until and until <= time.time():
            count = 0
        count += 1
        until = time.time() + self.lock_seconds if count >= self.max_fails else 0
        self.state[key] = (count, until)

    def ok(self, key):
        self.state.pop(key, None)


def load_secret_key(db_path):
    env = os.environ.get("BLOODLINK_SECRET_KEY")
    if env:
        return env
    path = os.path.join(os.path.dirname(db_path), "secret_key")
    if os.path.exists(path):
        with open(path) as fh:
            return fh.read().strip()
    key = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key)
    return key


# ------------------------------------------------------------ app factory ---
def create_app(db_path=None, config=None):
    app = Flask(__name__)
    app.config.update(
        DATABASE=db_path or os.environ.get("BLOODLINK_DB")
        or os.path.join(BASE_DIR, "instance", "bloodlink.db"),
        MAX_CONTENT_LENGTH=1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("BLOODLINK_HTTPS") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        DEMO_BANNER=os.environ.get("BLOODLINK_DEMO", "1") != "0",
    )
    if config:
        app.config.update(config)

    os.makedirs(os.path.dirname(app.config["DATABASE"]), exist_ok=True)
    app.secret_key = load_secret_key(app.config["DATABASE"])
    app.throttle = LoginThrottle()

    conn = db_connect(app.config["DATABASE"])
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

    # ---- per-request plumbing ----
    def get_db():
        if "db" not in g:
            g.db = db_connect(app.config["DATABASE"])
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    app.jinja_env.globals["csrf_token"] = csrf_token
    app.jinja_env.filters["age_label"] = age_label
    app.jinja_env.filters["ist"] = ist_label

    @app.context_processor
    def inject():
        return {
            "blood_groups": BLOOD_GROUPS,
            "demo_banner": app.config["DEMO_BANNER"],
            "is_hospital": "hospital_id" in session,
            "is_admin": "admin_id" in session,
        }

    @app.before_request
    def csrf_protect():
        if request.method in ("POST", "PUT", "PATCH", "DELETE") \
                and request.endpoint != "api_stock_push":
            expected = session.get("csrf")
            sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
            if not expected or not sent or not secrets.compare_digest(expected, sent):
                abort(400, "Your session expired or the form was invalid. "
                           "Please reload the page and try again.")

    @app.after_request
    def secure_headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; frame-ancestors 'none'; form-action 'self'"
        )
        if request.path.startswith(("/api/", "/staff", "/admin")):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(HTTPException)
    def http_error(e):
        if request.path.startswith("/api/"):
            return jsonify(error=e.description or e.name), e.code
        return render_template("error.html", code=e.code, name=e.name,
                               message=e.description), e.code

    # ---- auth helpers ----
    def hospital_required(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            hid = session.get("hospital_id")
            row = get_db().execute("SELECT * FROM hospitals WHERE id=?", (hid,)).fetchone() if hid else None
            if row is None:
                session.pop("hospital_id", None)
                flash("Please log in to continue.", "warning")
                return redirect(url_for("staff_login"))
            g.hospital = row
            return f(*args, **kwargs)
        return wrapper

    def admin_required(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            aid = session.get("admin_id")
            row = get_db().execute("SELECT * FROM admins WHERE id=?", (aid,)).fetchone() if aid else None
            if row is None:
                session.pop("admin_id", None)
                flash("Please log in to continue.", "warning")
                return redirect(url_for("admin_login"))
            g.admin = row
            return f(*args, **kwargs)
        return wrapper

    def try_login(table, session_key, template, title, success_endpoint):
        if request.method == "GET":
            return render_template(template, title=title)
        username = (request.form.get("username") or "").strip().lower()[:64]
        password = request.form.get("password") or ""
        throttle_key = f"{request.remote_addr}|{table}|{username}"
        wait = app.throttle.locked_for(throttle_key)
        if wait:
            flash(f"Too many failed attempts. Try again in {wait // 60 + 1} minute(s).", "danger")
            return render_template(template, title=title), 429
        row = get_db().execute(f"SELECT * FROM {table} WHERE username=?", (username,)).fetchone()
        valid = check_password_hash(row["password_hash"] if row else _DUMMY_HASH, password)
        if row and valid:
            app.throttle.ok(throttle_key)
            session.clear()
            session[session_key] = row["id"]
            session.permanent = True
            return redirect(url_for(success_endpoint))
        app.throttle.fail(throttle_key)
        flash("Wrong username or password.", "danger")
        return render_template(template, title=title), 401

    # ------------------------------------------------------------ public ---
    @app.get("/")
    def index():
        return render_template("index.html", areas=AREAS, default_radius=DEFAULT_RADIUS_KM)

    @app.get("/healthz")
    def healthz():
        get_db().execute("SELECT 1")
        return jsonify(status="ok")

    @app.get("/api/search")
    def api_search():
        group = normalize_group(request.args.get("group"))
        if group is None:
            return jsonify(error="Invalid or missing 'group'. Use one of: " + ", ".join(BLOOD_GROUPS)), 400
        try:
            lat = float(request.args["lat"])
            lng = float(request.args["lng"])
            if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                raise ValueError
        except (KeyError, ValueError):
            return jsonify(error="'lat' and 'lng' are required and must be valid coordinates."), 400
        try:
            radius = float(request.args.get("radius", DEFAULT_RADIUS_KM))
        except ValueError:
            return jsonify(error="'radius' must be a number (km)."), 400
        radius = min(max(radius, 1), MAX_RADIUS_KM)

        rows = get_db().execute(
            "SELECT h.id, h.name, h.kind, h.address, h.area, h.lat, h.lng, h.phone, "
            "       s.units, s.updated_at "
            "FROM hospitals h "
            "LEFT JOIN blood_stock s ON s.hospital_id = h.id AND s.blood_group = ? "
            "WHERE h.verified = 1",
            (group,),
        ).fetchall()

        results = []
        for r in rows:
            dist = haversine_km(lat, lng, r["lat"], r["lng"])
            if dist > radius:
                continue
            age = minutes_since(r["updated_at"])
            fresh = freshness(age)
            if r["units"] is None:
                availability = "no_data"
            elif r["units"] > 0:
                availability = "available"
            else:
                availability = "none"
            results.append({
                "id": r["id"],
                "name": r["name"],
                "kind": r["kind"],
                "address": r["address"],
                "area": r["area"],
                "phone": r["phone"],
                "distance_km": round(dist, 1),
                "units": r["units"],
                "availability": availability,
                "updated_at": r["updated_at"],
                "age_minutes": age,
                "updated_label": age_label(age),
                "freshness": fresh,
                "directions_url": "https://www.google.com/maps/dir/?api=1&destination="
                                  f"{r['lat']},{r['lng']}",
            })

        def rank(item):
            if item["availability"] == "available" and item["freshness"] != "stale":
                tier = 0
            elif item["availability"] == "available":
                tier = 1
            else:
                tier = 2
            return (tier, item["distance_km"])

        results.sort(key=rank)
        return jsonify(group=group, radius_km=radius, count=len(results),
                       generated_at=utcnow_iso(), results=results)

    # ------------------------------------------------- hospital API push ---
    def hospital_from_api_key():
        key = (request.headers.get("X-API-Key") or "").strip()
        if not key:
            return None
        return get_db().execute(
            "SELECT * FROM hospitals WHERE api_key_hash=?", (hash_key(key),)
        ).fetchone()

    @app.post("/api/stock")
    def api_stock_push():
        hosp = hospital_from_api_key()
        if hosp is None:
            return jsonify(error="Invalid or missing X-API-Key."), 401
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or "stock" not in data:
            return jsonify(error='Send JSON like {"stock": [{"blood_group": "A+", "units": 6}]}'), 400
        stock = data["stock"]
        if isinstance(stock, dict):
            pairs = list(stock.items())
        elif isinstance(stock, list) and all(isinstance(i, dict) for i in stock):
            pairs = [(i.get("blood_group"), i.get("units")) for i in stock]
        else:
            return jsonify(error="'stock' must be a list of {blood_group, units} objects."), 400
        if not pairs:
            return jsonify(error="'stock' is empty."), 400
        clean, errors = validate_pairs(pairs)
        if errors:
            return jsonify(error="Nothing was saved because of invalid data.", details=errors), 400
        stamp = apply_updates(get_db(), hosp["id"], clean, "api")
        return jsonify(status="ok", hospital=hosp["name"], updated=len(clean), updated_at=stamp)

    @app.get("/api/stock")
    def api_stock_get():
        hosp = hospital_from_api_key()
        if hosp is None:
            return jsonify(error="Invalid or missing X-API-Key."), 401
        rows = get_db().execute(
            "SELECT blood_group, units, updated_at FROM blood_stock WHERE hospital_id=?", (hosp["id"],)
        ).fetchall()
        by_group = {r["blood_group"]: r for r in rows}
        return jsonify(hospital=hosp["name"], stock=[
            {"blood_group": g_, "units": by_group[g_]["units"] if g_ in by_group else None,
             "updated_at": by_group[g_]["updated_at"] if g_ in by_group else None}
            for g_ in BLOOD_GROUPS
        ])

    # ------------------------------------------------------ staff pages ---
    def render_dashboard(new_key=None, status=200):
        db = get_db()
        hosp = g.hospital
        rows = {r["blood_group"]: r for r in db.execute(
            "SELECT * FROM blood_stock WHERE hospital_id=?", (hosp["id"],))}
        items, total = [], 0
        for grp in BLOOD_GROUPS:
            r = rows.get(grp)
            age = minutes_since(r["updated_at"]) if r else None
            if r:
                total += r["units"]
            items.append({"group": grp, "units": r["units"] if r else None,
                          "age": age, "freshness": freshness(age)})
        logs = db.execute(
            "SELECT * FROM stock_log WHERE hospital_id=? ORDER BY id DESC LIMIT 15", (hosp["id"],)
        ).fetchall()
        return render_template("staff_dashboard.html", hosp=hosp, items=items, total=total,
                               logs=logs, new_key=new_key, max_units=MAX_UNITS,
                               api_url=request.host_url + "api/stock"), status

    @app.route("/staff/login", methods=["GET", "POST"])
    def staff_login():
        return try_login("hospitals", "hospital_id", "login.html",
                         "Hospital / blood bank staff login", "staff_dashboard")

    @app.post("/staff/logout")
    def staff_logout():
        session.clear()
        flash("Logged out.", "info")
        return redirect(url_for("index"))

    @app.get("/staff")
    @hospital_required
    def staff_dashboard():
        return render_dashboard()

    @app.post("/staff/update")
    @hospital_required
    def staff_update():
        pairs = []
        for grp in BLOOD_GROUPS:
            raw = request.form.get("units_" + grp)
            if raw is not None and raw.strip() != "":
                pairs.append((grp, raw))
        clean, errors = validate_pairs(pairs)
        if errors:
            flash("Nothing was saved. " + "; ".join(errors), "danger")
        elif not clean:
            flash("No values were entered.", "warning")
        else:
            apply_updates(get_db(), g.hospital["id"], clean, "dashboard")
            flash("Stock saved. The public search now shows these numbers.", "success")
        return redirect(url_for("staff_dashboard"))

    @app.post("/staff/upload")
    @hospital_required
    def staff_upload():
        f = request.files.get("file")
        if not f or not f.filename:
            flash("Choose a CSV file first.", "warning")
            return redirect(url_for("staff_dashboard"))
        try:
            text = f.read().decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text))
            fields = {(n or "").strip().lower(): n for n in (reader.fieldnames or [])}
            gcol = next((fields[k] for k in ("blood_group", "group", "bloodgroup") if k in fields), None)
            ucol = next((fields[k] for k in ("units", "count", "stock") if k in fields), None)
            if not gcol or not ucol:
                raise ValueError("The first row must contain the headers: blood_group,units")
            pairs = []
            for row in reader:
                if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
                    continue
                pairs.append((row.get(gcol), row.get(ucol)))
                if len(pairs) > MAX_CSV_ROWS:
                    raise ValueError(f"Too many rows (maximum {MAX_CSV_ROWS}).")
            if not pairs:
                raise ValueError("The file has no data rows.")
            clean, errors = validate_pairs(pairs)
            if errors:
                raise ValueError("; ".join(errors))
        except UnicodeDecodeError:
            flash("Nothing was saved. The file is not a UTF-8 text/CSV file.", "danger")
            return redirect(url_for("staff_dashboard"))
        except (ValueError, csv.Error) as exc:
            flash(f"Nothing was saved. {exc}", "danger")
            return redirect(url_for("staff_dashboard"))
        apply_updates(get_db(), g.hospital["id"], clean, "csv")
        flash(f"CSV imported: {len(clean)} blood group(s) updated.", "success")
        return redirect(url_for("staff_dashboard"))

    @app.get("/staff/sample.csv")
    @hospital_required
    def staff_sample_csv():
        body = "blood_group,units\n" + "".join(f"{grp},0\n" for grp in BLOOD_GROUPS)
        return Response(body, mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=blood_stock_template.csv"})

    @app.post("/staff/api-key")
    @hospital_required
    def staff_api_key():
        key = new_api_key()
        db = get_db()
        db.execute("UPDATE hospitals SET api_key_hash=? WHERE id=?", (hash_key(key), g.hospital["id"]))
        db.commit()
        return render_dashboard(new_key=key)

    @app.post("/staff/password")
    @hospital_required
    def staff_password():
        current = request.form.get("current") or ""
        new = request.form.get("new") or ""
        confirm = request.form.get("confirm") or ""
        if not check_password_hash(g.hospital["password_hash"], current):
            flash("Current password is wrong.", "danger")
        elif len(new) < 8:
            flash("New password must be at least 8 characters.", "danger")
        elif new != confirm:
            flash("New passwords do not match.", "danger")
        else:
            db = get_db()
            db.execute("UPDATE hospitals SET password_hash=? WHERE id=?",
                       (generate_password_hash(new), g.hospital["id"]))
            db.commit()
            flash("Password changed.", "success")
        return redirect(url_for("staff_dashboard"))

    # ------------------------------------------------------- admin pages ---
    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        return try_login("admins", "admin_id", "login.html", "Administrator login", "admin_home")

    @app.post("/admin/logout")
    def admin_logout():
        session.clear()
        flash("Logged out.", "info")
        return redirect(url_for("index"))

    def render_admin(temp_password=None, temp_for=None, form=None, status=200):
        hospitals = get_db().execute(
            "SELECT h.*, (SELECT MAX(updated_at) FROM blood_stock WHERE hospital_id=h.id) AS last_update "
            "FROM hospitals h ORDER BY h.name"
        ).fetchall()
        return render_template("admin.html", hospitals=hospitals, kinds=KINDS,
                               temp_password=temp_password, temp_for=temp_for,
                               form=form or {}, age_of=minutes_since), status

    @app.get("/admin")
    @admin_required
    def admin_home():
        return render_admin()

    @app.post("/admin/hospitals")
    @admin_required
    def admin_add_hospital():
        f = request.form
        form = {k: (f.get(k) or "").strip() for k in
                ("name", "kind", "address", "area", "lat", "lng", "phone", "username")}
        password = f.get("password") or ""
        errors = []
        if not form["name"] or len(form["name"]) > 120:
            errors.append("Name is required (max 120 characters).")
        if form["kind"] not in KINDS:
            errors.append("Choose a valid type.")
        try:
            lat, lng = float(form["lat"]), float(form["lng"])
            if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                raise ValueError
        except ValueError:
            errors.append("Latitude and longitude must be valid numbers.")
        if not re.fullmatch(r"[+\d][\d\s\-+()]{4,19}", form["phone"]):
            errors.append("Enter a valid phone number.")
        if not re.fullmatch(r"[a-z0-9_.-]{3,32}", form["username"].lower()):
            errors.append("Username: 3-32 letters, digits, dot, dash or underscore.")
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        if errors:
            for e in errors:
                flash(e, "danger")
            return render_admin(form=form, status=400)
        db = get_db()
        try:
            db.execute(
                "INSERT INTO hospitals (name, kind, address, area, lat, lng, phone, username, "
                "password_hash, verified, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (form["name"], form["kind"], form["address"] or None, form["area"] or None,
                 lat, lng, form["phone"], form["username"].lower(),
                 generate_password_hash(password), 1 if f.get("verified") else 0, utcnow_iso()),
            )
            db.commit()
        except sqlite3.IntegrityError:
            flash("That username is already taken.", "danger")
            return render_admin(form=form, status=400)
        flash(f"{form['name']} added.", "success")
        return redirect(url_for("admin_home"))

    @app.post("/admin/hospitals/<int:hid>/verify")
    @admin_required
    def admin_toggle_verify(hid):
        db = get_db()
        row = db.execute("SELECT * FROM hospitals WHERE id=?", (hid,)).fetchone()
        if row is None:
            abort(404)
        db.execute("UPDATE hospitals SET verified=? WHERE id=?", (0 if row["verified"] else 1, hid))
        db.commit()
        flash(f"{row['name']} is now {'hidden from' if row['verified'] else 'visible in'} public search.", "info")
        return redirect(url_for("admin_home"))

    @app.post("/admin/hospitals/<int:hid>/reset-password")
    @admin_required
    def admin_reset_password(hid):
        db = get_db()
        row = db.execute("SELECT * FROM hospitals WHERE id=?", (hid,)).fetchone()
        if row is None:
            abort(404)
        temp = secrets.token_urlsafe(9)
        db.execute("UPDATE hospitals SET password_hash=? WHERE id=?", (generate_password_hash(temp), hid))
        db.commit()
        return render_admin(temp_password=temp, temp_for=row["name"])

    # ------------------------------------------------------------- CLI ---
    @app.cli.command("create-admin")
    @click.argument("username")
    @click.password_option()
    def create_admin(username, password):
        """Create (or reset the password of) an administrator account."""
        db = db_connect(app.config["DATABASE"])
        db.execute(
            "INSERT INTO admins (username, password_hash) VALUES (?,?) "
            "ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash",
            (username.strip().lower(), generate_password_hash(password)),
        )
        db.commit()
        db.close()
        click.echo(f"Administrator '{username}' saved.")

    return app


if __name__ == "__main__":
    application = create_app()
    if os.environ.get("BLOODLINK_SEED", "1") != "0":
        from seed import seed_demo
        seed_demo(application.config["DATABASE"])
    application.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
