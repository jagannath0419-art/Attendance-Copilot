import os
import math
import logging
from datetime import datetime

from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_mysqldb import MySQL
from flask_wtf import CSRFProtect
import MySQLdb.cursors
from werkzeug.security import generate_password_hash, check_password_hash

# ---------------------------------------------------------------------------
# Logging (replaces print() debugging with proper structured logging)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("attendance_copilot")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Secret key: never silently fall back to a hardcoded secret in production.
# ---------------------------------------------------------------------------
_is_production = bool(os.environ.get("RENDER")) or os.environ.get("FLASK_ENV") == "production"
_secret_key = os.environ.get("FLASK_SECRET_KEY")
if not _secret_key:
    if _is_production:
        raise RuntimeError(
            "FLASK_SECRET_KEY environment variable must be set in production. "
            "Set it in your Render environment variables."
        )
    _secret_key = "dev-only-insecure-key-change-me"
    logger.warning(
        "FLASK_SECRET_KEY is not set. Using an insecure development-only fallback key. "
        "This is NOT safe for production."
    )
app.secret_key = _secret_key

# ---------------------------------------------------------------------------
# Session cookie hardening
# ---------------------------------------------------------------------------
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = _is_production

# ---------------------------------------------------------------------------
# MySQL Database Configuration
# ---------------------------------------------------------------------------
app.config["MYSQL_HOST"] = os.environ.get("DB_HOST", "localhost")
app.config["MYSQL_USER"] = os.environ.get("DB_USER", "root")
app.config["MYSQL_PASSWORD"] = os.environ.get("DB_PASS", "")
app.config["MYSQL_DB"] = os.environ.get("DB_NAME", "attendance_copilot")
app.config["MYSQL_PORT"] = int(os.environ.get("DB_PORT", 3306))

mysql = MySQL(app)

# ---------------------------------------------------------------------------
# CSRF protection for every state-changing form/AJAX request
# ---------------------------------------------------------------------------
csrf = CSRFProtect(app)

# Whitelisted values used for server-side input validation
ALLOWED_STRUCTURE_TYPES = {"Theory Only", "Practical Only", "Theory & Practical Both"}
ALLOWED_COLOR_THEMES = {"indigo", "rose", "emerald"}
ALLOWED_COUNTER_FIELDS = {"theory_conducted", "theory_attended", "labs_conducted", "labs_attended"}


def get_cursor(dict_cursor=False):
    """Returns a fresh cursor, pinging/reconnecting the MySQL connection first.

    flask-mysqldb keeps a single long-lived connection; on cloud DBs (e.g. Aiven)
    idle connections get dropped and the next query fails with
    'MySQL server has gone away'. Pinging with reconnect=True avoids that.
    """
    try:
        mysql.connection.ping(True)
    except Exception:
        # If ping itself fails, let the subsequent query raise a clear error.
        pass
    if dict_cursor:
        return mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    return mysql.connection.cursor()


def parse_int(value, default=0, min_val=None, max_val=None):
    """Safely parses an int from user input, clamped to an optional range."""
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if min_val is not None:
        result = max(min_val, result)
    if max_val is not None:
        result = min(max_val, result)
    return result


def parse_date(value):
    """Returns a validated 'YYYY-MM-DD' string or None."""
    if not value:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return value
    except ValueError:
        return None


def init_db_schema():
    """Idempotently ensures that all database tables exist. Safe to run on every boot."""
    cursor = mysql.connection.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INT AUTO_INCREMENT PRIMARY KEY,
                fullname VARCHAR(255) NOT NULL,
                email VARCHAR(255) UNIQUE NOT NULL,
                password VARCHAR(255) NOT NULL,
                department VARCHAR(100),
                semester VARCHAR(50)
            ) ENGINE=InnoDB;
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS subjects (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_id INT NOT NULL,
                subject_name VARCHAR(255) NOT NULL,
                structure_type VARCHAR(50) NOT NULL,
                active_month VARCHAR(50),
                start_date DATE,
                end_date DATE,
                color_theme VARCHAR(50),
                attendance_target INT DEFAULT 75,
                theory_conducted INT DEFAULT 0,
                theory_attended INT DEFAULT 0,
                labs_conducted INT DEFAULT 0,
                labs_attended INT DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            ) ENGINE=InnoDB;
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS attendance_logs (
                id INT AUTO_INCREMENT PRIMARY KEY,
                subject_id INT NOT NULL,
                session_type ENUM('Theory', 'Practical') NOT NULL,
                status ENUM('Attended', 'Missed') NOT NULL,
                lecture_date DATE NOT NULL,
                time_slot VARCHAR(50) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (subject_id) REFERENCES subjects(id) ON DELETE CASCADE
            ) ENGINE=InnoDB;
        """)
        mysql.connection.commit()
    except Exception:
        logger.exception("Error initializing DB schema")
    finally:
        cursor.close()


with app.app_context():
    try:
        init_db_schema()
    except Exception:
        logger.exception("Database validation connection failed")


def calculate_subject_analytics(sub, target_goal):
    """Calculates attendance percentages and predicts safe skip margins or safe-zone streak margins."""
    tc = sub.get("theory_conducted") or 0
    ta = sub.get("theory_attended") or 0
    lc = sub.get("labs_conducted") or 0
    la = sub.get("labs_attended") or 0
    st = sub.get("structure_type")

    if st == "Theory Only":
        total_conducted, total_attended = tc, ta
    elif st == "Practical Only":
        total_conducted, total_attended = lc, la
    else:
        total_conducted, total_attended = (tc + lc), (ta + la)

    current_pct = 100 if total_conducted == 0 else round((total_attended / total_conducted) * 100)
    target_fraction = target_goal / 100.0

    if current_pct >= target_goal:
        status = "Safe"
        skippable = (
            math.floor((total_attended - (total_conducted * target_fraction)) / target_fraction)
            if target_fraction > 0
            else 0
        )
        skippable = max(0, skippable)
        guidance_msg = f"Safe! You can skip the next <strong>{skippable}</strong> session(s)."
    else:
        status = "Defaulter"
        required_streak = (
            math.ceil(((target_fraction * total_conducted) - total_attended) / (1.0 - target_fraction))
            if target_fraction < 1
            else 1
        )
        guidance_msg = f"Defaulter! You must attend the next <strong>{max(1, required_streak)}</strong> session(s) consecutively."

    return current_pct, total_conducted, total_attended, status, guidance_msg


@app.route("/")
def home():
    processed_subjects = []
    global_metrics = {"avg": 0, "total_cond": 0, "total_att": 0, "defaulters": 0, "safe": 0}

    if "user_id" in session:
        cursor = get_cursor(dict_cursor=True)
        try:
            cursor.execute("SELECT * FROM subjects WHERE user_id = %s", (session["user_id"],))
            raw_subjects = cursor.fetchall()
        except Exception:
            logger.exception("Error fetching subjects for dashboard")
            raw_subjects = []
        finally:
            cursor.close()

        total_cond, total_att, def_count, safe_count = 0, 0, 0, 0

        for s in raw_subjects:
            pct, cond, att, status, msg = calculate_subject_analytics(s, s["attendance_target"])
            s.update({
                "calculated_percentage": pct,
                "calculated_status": status,
                "calculated_message": msg,
                "start_date": str(s["start_date"]) if s["start_date"] else "",
                "end_date": str(s["end_date"]) if s["end_date"] else "",
            })
            total_cond += cond
            total_att += att
            if status == "Defaulter":
                def_count += 1
            else:
                safe_count += 1
            processed_subjects.append(s)

        global_metrics = {
            "avg": round((total_att / total_cond) * 100) if total_cond > 0 else 100,
            "total_cond": total_cond,
            "total_att": total_att,
            "defaulters": def_count,
            "safe": safe_count,
        }

    return render_template("index.html", subjects=processed_subjects, metrics=global_metrics)


@app.route("/auth/register", methods=["POST"])
def register():
    fullname = (request.form.get("fullname") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    raw_password = request.form.get("password") or ""
    department = (request.form.get("department") or "").strip()
    semester = (request.form.get("semester") or "").strip()

    if not fullname or not email or len(raw_password) < 6:
        logger.info("Registration rejected: missing fields or password too short for %s", email)
        return redirect(url_for("home"))

    password = generate_password_hash(raw_password)

    cursor = get_cursor(dict_cursor=True)
    try:
        cursor.execute(
            "INSERT INTO users (fullname, email, password, department, semester) VALUES (%s, %s, %s, %s, %s)",
            (fullname, email, password, department, semester),
        )
        mysql.connection.commit()

        cursor.execute("SELECT id, fullname, department, semester FROM users WHERE email = %s", (email,))
        user = cursor.fetchone()
        if user:
            session["user_id"] = user["id"]
            session["fullname"] = user["fullname"]
            session["department"] = user["department"]
            session["semester"] = user["semester"]
    except Exception:
        logger.exception("Registration error for email=%s", email)
    finally:
        cursor.close()
    return redirect(url_for("home"))


@app.route("/auth/login", methods=["POST"])
def login():
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""

    cursor = get_cursor(dict_cursor=True)
    try:
        cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
        user = cursor.fetchone()
        if user and check_password_hash(user["password"], password):
            session.update({
                "user_id": user["id"],
                "fullname": user["fullname"],
                "department": user["department"],
                "semester": user["semester"],
            })
        else:
            logger.info("Failed login attempt for email=%s", email)
    except Exception:
        logger.exception("Login error for email=%s", email)
    finally:
        cursor.close()
    return redirect(url_for("home"))


@app.route("/auth/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/edit_profile", methods=["POST"])
def edit_profile():
    if "user_id" not in session:
        return redirect(url_for("home"))

    fullname = (request.form.get("fullname") or "").strip()
    department = (request.form.get("department") or "").strip()
    semester = (request.form.get("semester") or "").strip()

    if not fullname:
        return redirect(url_for("home"))

    cursor = get_cursor()
    try:
        cursor.execute(
            "UPDATE users SET fullname = %s, department = %s, semester = %s WHERE id = %s",
            (fullname, department, semester, session["user_id"]),
        )
        mysql.connection.commit()
        session["fullname"] = fullname
        session["department"] = department
        session["semester"] = semester
    except Exception:
        logger.exception("Error updating profile for user_id=%s", session.get("user_id"))
    finally:
        cursor.close()
    return redirect(url_for("home"))


def _extract_subject_form():
    """Parses and validates the shared add/edit subject form fields."""
    structure_type = request.form.get("structure_type")
    if structure_type not in ALLOWED_STRUCTURE_TYPES:
        structure_type = "Theory & Practical Both"

    color_theme = request.form.get("color_theme")
    if color_theme not in ALLOWED_COLOR_THEMES:
        color_theme = "indigo"

    return {
        "subject_name": (request.form.get("subject_name") or "").strip()[:255],
        "structure_type": structure_type,
        "active_month": (request.form.get("active_month") or "").strip()[:50],
        "color_theme": color_theme,
        "start_date": parse_date(request.form.get("start_date")),
        "end_date": parse_date(request.form.get("end_date")),
        "attendance_target": parse_int(request.form.get("attendance_target"), default=75, min_val=1, max_val=100),
        "theory_conducted": parse_int(request.form.get("theory_conducted"), default=0, min_val=0),
        "theory_attended": parse_int(request.form.get("theory_attended"), default=0, min_val=0),
        "labs_conducted": parse_int(request.form.get("labs_conducted"), default=0, min_val=0),
        "labs_attended": parse_int(request.form.get("labs_attended"), default=0, min_val=0),
    }


@app.route("/add_subject", methods=["POST"])
def add_subject():
    if "user_id" not in session:
        return redirect(url_for("home"))

    data = _extract_subject_form()
    if not data["subject_name"]:
        return redirect(url_for("home"))

    cursor = get_cursor()
    try:
        query = """
            INSERT INTO subjects (
                user_id, subject_name, structure_type, active_month,
                start_date, end_date, color_theme, attendance_target,
                theory_conducted, theory_attended, labs_conducted, labs_attended
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        values = (
            session["user_id"], data["subject_name"], data["structure_type"], data["active_month"],
            data["start_date"], data["end_date"], data["color_theme"], data["attendance_target"],
            data["theory_conducted"], data["theory_attended"], data["labs_conducted"], data["labs_attended"],
        )
        cursor.execute(query, values)
        mysql.connection.commit()
    except Exception:
        logger.exception("Error adding subject for user_id=%s", session.get("user_id"))
    finally:
        cursor.close()

    return redirect(url_for("home"))


@app.route("/edit_subject", methods=["POST"])
def edit_subject():
    if "user_id" not in session:
        return redirect(url_for("home"))

    subject_id = parse_int(request.form.get("subject_id"), default=0, min_val=0)
    if not subject_id:
        return redirect(url_for("home"))

    data = _extract_subject_form()
    if not data["subject_name"]:
        return redirect(url_for("home"))

    cursor = get_cursor()
    try:
        query = """
            UPDATE subjects SET
                subject_name = %s, structure_type = %s, active_month = %s,
                color_theme = %s, start_date = %s, end_date = %s, attendance_target = %s,
                theory_conducted = %s, theory_attended = %s, labs_conducted = %s, labs_attended = %s
            WHERE id = %s AND user_id = %s
        """
        values = (
            data["subject_name"], data["structure_type"], data["active_month"], data["color_theme"],
            data["start_date"], data["end_date"], data["attendance_target"],
            data["theory_conducted"], data["theory_attended"], data["labs_conducted"], data["labs_attended"],
            subject_id, session["user_id"],
        )
        cursor.execute(query, values)
        mysql.connection.commit()
    except Exception:
        logger.exception("Error editing subject_id=%s", subject_id)
    finally:
        cursor.close()

    return redirect(url_for("home"))


@app.route("/delete_subject", methods=["POST"])
def delete_subject():
    if "user_id" not in session:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    subject_id = parse_int(request.form.get("subject_id"), default=0, min_val=0)
    if not subject_id:
        return jsonify({"success": False, "error": "Invalid subject id"}), 400

    cursor = get_cursor()
    try:
        cursor.execute("DELETE FROM subjects WHERE id = %s AND user_id = %s", (subject_id, session["user_id"]))
        mysql.connection.commit()
        return jsonify({"success": True})
    except Exception:
        logger.exception("Error deleting subject_id=%s", subject_id)
        return jsonify({"success": False, "error": "Could not delete subject. Please try again."}), 500
    finally:
        cursor.close()


# Explicit, whitelisted UPDATE statements. No user-controlled value is ever
# interpolated into SQL text — this removes the injection surface entirely,
# rather than relying only on validating the field name beforehand.
_COUNTER_QUERIES = {
    ("theory_conducted", False): "UPDATE subjects SET theory_conducted = %s WHERE id = %s AND user_id = %s",
    ("theory_attended", False): "UPDATE subjects SET theory_attended = %s WHERE id = %s AND user_id = %s",
    ("theory_attended", True): (
        "UPDATE subjects SET theory_attended = %s, theory_conducted = theory_conducted + 1 "
        "WHERE id = %s AND user_id = %s"
    ),
    ("labs_conducted", False): "UPDATE subjects SET labs_conducted = %s WHERE id = %s AND user_id = %s",
    ("labs_attended", False): "UPDATE subjects SET labs_attended = %s WHERE id = %s AND user_id = %s",
    ("labs_attended", True): (
        "UPDATE subjects SET labs_attended = %s, labs_conducted = labs_conducted + 1 "
        "WHERE id = %s AND user_id = %s"
    ),
}


@app.route("/update_counter", methods=["POST"])
def update_counter():
    """Updates individual lecture counters and logs history securely."""
    if "user_id" not in session:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    subject_id = parse_int(data.get("subject_id"), default=0, min_val=0)
    field = data.get("field")
    action = data.get("action")
    lecture_date = parse_date(data.get("lecture_date"))
    time_slot = (data.get("time_slot") or "")[:50]

    if not subject_id:
        return jsonify({"success": False, "error": "Invalid subject id"}), 400
    if field not in ALLOWED_COUNTER_FIELDS:
        return jsonify({"success": False, "error": "Invalid field"}), 400
    if action not in ("increment", "decrement"):
        return jsonify({"success": False, "error": "Invalid action"}), 400

    cursor = get_cursor(dict_cursor=True)
    try:
        cursor.execute("SELECT * FROM subjects WHERE id = %s AND user_id = %s", (subject_id, session["user_id"]))
        subject = cursor.fetchone()

        if not subject:
            return jsonify({"success": False, "error": "Subject not found"}), 404

        current_val = subject.get(field) or 0
        new_val = current_val + 1 if action == "increment" else max(0, current_val - 1)

        bumps_conducted = action == "increment" and "attended" in field

        # Validate the resulting state stays within the mathematical domain
        # (attended can never exceed conducted) before writing anything.
        temp_subject = subject.copy()
        temp_subject[field] = new_val
        if bumps_conducted:
            conducted_partner = field.replace("attended", "conducted")
            temp_subject[conducted_partner] = (temp_subject.get(conducted_partner) or 0) + 1

        tc = temp_subject.get("theory_conducted") or 0
        ta = temp_subject.get("theory_attended") or 0
        lc = temp_subject.get("labs_conducted") or 0
        la = temp_subject.get("labs_attended") or 0

        if ta > tc or la > lc:
            return jsonify({"success": False, "error": "Attended lectures cannot exceed conducted lectures."}), 400

        query = _COUNTER_QUERIES[(field, bumps_conducted)]
        cursor.execute(query, (new_val, subject_id, session["user_id"]))

        if action == "increment" and lecture_date and time_slot:
            session_type = "Theory" if "theory" in field else "Practical"
            status = "Attended" if "attended" in field else "Missed"
            cursor.execute(
                """
                INSERT INTO attendance_logs (subject_id, session_type, status, lecture_date, time_slot)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (subject_id, session_type, status, lecture_date, time_slot),
            )

        mysql.connection.commit()
        return jsonify({"success": True})
    except Exception:
        logger.exception("Error updating counter for subject_id=%s field=%s", subject_id, field)
        return jsonify({"success": False, "error": "Could not update attendance. Please try again."}), 500
    finally:
        cursor.close()


# ---------------------------------------------------------------------------
# Basic security headers for every response
# ---------------------------------------------------------------------------
@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)