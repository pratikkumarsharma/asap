import os
import uuid
import hashlib
import sqlite3
import threading
import time
from datetime import datetime, timedelta

from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash

# --- App Initialization ---
app = Flask(__name__, template_folder="templates", static_folder="static")
# IMPORTANT: Change this secret key for production environments.
# You can generate a secure key using: python -c 'import os; print(os.urandom(24))'
app.config["SECRET_KEY"] = os.environ.get(
    "FLASK_SECRET_KEY", "a-very-secure-dev-secret-key"
)
app.permanent_session_lifetime = timedelta(hours=24)

CORS(
    app,
    # Allow both HTTPS and HTTP localhost origins for development (camera access on localhost works without HTTPS)
    resources={r"/api/*": {"origins": [
        "https://localhost:5000", "https://127.0.0.1:5000",
        "http://localhost:5000", "http://127.0.0.1:5000"
    ]}},
    supports_credentials=True,
)

# --- Database Management (Singleton Pattern) ---
class DatabaseManager:
    """Singleton to manage SQLite database initialization and connections."""

    _instance = None

    def __new__(cls, db_path="campus_guardian.db"):
        if cls._instance is None:
            cls._instance = super(DatabaseManager, cls).__new__(cls)
            cls._instance.db_path = db_path
            cls._instance.init_database()
        return cls._instance

    def get_connection(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def init_database(self):
        """Create tables and seed default data if needed."""
        conn = self.get_connection()
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    email TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('student', 'teacher')),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS classes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    class_name TEXT NOT NULL,
                    teacher_id TEXT NOT NULL,
                    class_code TEXT UNIQUE NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (teacher_id) REFERENCES users (user_id)
                );
                CREATE TABLE IF NOT EXISTS attendance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id TEXT NOT NULL,
                    class_id INTEGER NOT NULL,
                    attendance_date DATE NOT NULL,
                    status TEXT DEFAULT 'present',
                    marked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (student_id) REFERENCES users (user_id),
                    FOREIGN KEY (class_id) REFERENCES classes (id),
                    UNIQUE(student_id, class_id, attendance_date)
                );
                CREATE TABLE IF NOT EXISTS qr_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT UNIQUE NOT NULL,
                    class_id INTEGER NOT NULL,
                    teacher_id TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NOT NULL,
                    is_active BOOLEAN DEFAULT TRUE,
                    FOREIGN KEY (class_id) REFERENCES classes (id),
                    FOREIGN KEY (teacher_id) REFERENCES users (user_id)
                );
                """
            )
            conn.commit()
            self._create_default_data(conn)
        except Exception as e:
            app.logger.exception("Database initialization error")
        finally:
            conn.close()

    def _create_default_data(self, conn):
        """Insert a default teacher, student and class for development/demo."""
        try:
            teacher_pass = os.environ.get("DEFAULT_TEACHER_PASS", "teacher123")
            student_pass = os.environ.get("DEFAULT_STUDENT_PASS", "student123")

            conn.execute(
                "INSERT OR IGNORE INTO users (user_id, name, email, password_hash, role) VALUES (?, ?, ?, ?, ?)",
                (
                    "teacher001",
                    "Dr. Ada Lovelace",
                    "teacher@campus.edu",
                    generate_password_hash(teacher_pass),
                    "teacher",
                ),
            )
            conn.execute(
                "INSERT OR IGNORE INTO users (user_id, name, email, password_hash, role) VALUES (?, ?, ?, ?, ?)",
                (
                    "2024001",
                    "Alan Turing",
                    "student@campus.edu",
                    generate_password_hash(student_pass),
                    "student",
                ),
            )
            conn.execute(
                "INSERT OR IGNORE INTO classes (class_name, teacher_id, class_code) VALUES (?, ?, ?)",
                ("Computer Science 101", "teacher001", "CS101"),
            )
            conn.commit()
        except Exception:
            app.logger.exception("Error creating default data")


# --- QR Code Management (Thread-Safe) ---
class QRCodeManager:
    TOKEN_WINDOW_SECONDS = 30

    def __init__(self):
        self.active_session = {}
        self.lock = threading.Lock()
        try:
            self._load_active_session_from_db()
        except Exception:
            app.logger.exception("Failed to load active QR session during startup")

    def _load_active_session_from_db(self):
        conn = db_manager.get_connection()
        try:
            row = conn.execute(
                "SELECT session_id, class_id, teacher_id, expires_at FROM qr_sessions WHERE is_active = 1 ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if row:
                expires_at = datetime.fromisoformat(row["expires_at"])
                if datetime.utcnow() < expires_at:
                    self.active_session = {
                        "session_id": row["session_id"],
                        "class_id": row["class_id"],
                        "teacher_id": row["teacher_id"],
                        "expires_at": expires_at,
                    }
                else:
                    conn.execute("UPDATE qr_sessions SET is_active = 0 WHERE session_id = ?", (row["session_id"],))
                    conn.commit()
        finally:
            conn.close()

    def start_session(self, class_id, teacher_id, duration_minutes=60):
        with self.lock:
            session_id = str(uuid.uuid4())
            expires_at = datetime.utcnow() + timedelta(minutes=int(duration_minutes))
            conn = db_manager.get_connection()
            try:
                conn.execute("UPDATE qr_sessions SET is_active = 0 WHERE teacher_id = ? AND is_active = 1", (teacher_id,))
                conn.execute(
                    "INSERT INTO qr_sessions (session_id, class_id, teacher_id, expires_at, is_active) VALUES (?, ?, ?, ?, 1)",
                    (session_id, class_id, teacher_id, expires_at.isoformat()),
                )
                conn.commit()
                self.active_session = {
                    "session_id": session_id,
                    "class_id": class_id,
                    "teacher_id": teacher_id,
                    "expires_at": expires_at,
                }
                return self.active_session
            finally:
                conn.close()

    def stop_session(self):
        with self.lock:
            if self.active_session:
                conn = db_manager.get_connection()
                try:
                    conn.execute("UPDATE qr_sessions SET is_active = 0 WHERE session_id = ?", (self.active_session["session_id"],))
                    conn.commit()
                finally:
                    conn.close()
            self.active_session = {}
        return True

    def _ensure_session_is_active(self):
        if not self.active_session or datetime.utcnow() >= self.active_session.get("expires_at", datetime.min):
            self._load_active_session_from_db()

    def get_current_token(self):
        with self.lock:
            self._ensure_session_is_active()
            if not self.active_session:
                return None
            token_window = int(time.time()) // self.TOKEN_WINDOW_SECONDS
            token_data = f"{self.active_session['session_id']}:{token_window}"
            token_hash = hashlib.sha256(token_data.encode()).hexdigest()[:16]
            return {"token": token_hash, "session_id": self.active_session["session_id"]}

    def validate_token(self, token, session_id):
        with self.lock:
            self._ensure_session_is_active()
            if not self.active_session or self.active_session.get("session_id") != session_id:
                return False

            current_window = int(time.time()) // self.TOKEN_WINDOW_SECONDS
            for offset in (0, -1):
                token_window = current_window + offset
                expected_token = hashlib.sha256(f"{session_id}:{token_window}".encode()).hexdigest()[:16]
                if token == expected_token:
                    return True
            return False


# --- Global Instances ---
db_manager = DatabaseManager()
qr_manager = QRCodeManager()


# --- Routes ---
@app.route("/")
def index():
    if "user_id" in session:
        role = session.get("role")
        if role == "teacher":
            return render_template("teacher_dashboard.html")
        elif role == "student":
            return render_template("student_dashboard.html")
    return redirect(url_for("login_page"))


@app.route("/login")
def login_page():
    return render_template("login.html")


# --- API: Authentication ---
@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json() or {}
    user_id = data.get("user_id")
    password = data.get("password")
    if not user_id or not password:
        return jsonify({"success": False, "message": "User ID and password are required."}), 400

    conn = db_manager.get_connection()
    try:
        user_row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if user_row and check_password_hash(user_row["password_hash"], password):
            session.permanent = True
            session["user_id"] = user_row["user_id"]
            session["role"] = user_row["role"]
            user_data = {"user_id": user_row["user_id"], "name": user_row["name"], "role": user_row["role"]}
            return jsonify({"success": True, "user": user_data, "redirect": url_for("index")})
        return jsonify({"success": False, "message": "Invalid user ID or password."}), 401
    finally:
        conn.close()


@app.route("/api/logout", methods=["POST"])
def logout():
    try:
        if session.get("role") == "teacher":
            qr_manager.stop_session()
    except Exception:
        app.logger.exception("Error stopping QR session during logout")
    session.clear()
    return jsonify({"success": True, "message": "You have been logged out successfully."})


@app.route("/api/user", methods=["GET"])
def get_current_user():
    if "user_id" in session:
        return jsonify({"user_id": session["user_id"], "role": session.get("role")})
    return jsonify({"error": "Not authenticated. Please log in."}), 401


# --- API: Teacher & QR ---
@app.route("/api/teacher/generate-qr", methods=["POST"])
def generate_qr():
    if session.get("role") != "teacher":
        return jsonify({"error": "Only teachers can perform this action."}), 403
    data = request.get_json() or {}
    try:
        duration = int(data.get("duration", 60))
    except (ValueError, TypeError):
        duration = 60
    class_code = data.get("class_code")
    if not class_code:
        return jsonify({"error": "Class code is required."}), 400

    conn = db_manager.get_connection()
    try:
        class_info = conn.execute("SELECT id, class_name FROM classes WHERE class_code = ? AND teacher_id = ?", (class_code, session["user_id"])).fetchone()
        if not class_info:
            return jsonify({"error": "Class not found or you are not authorized to start a session for this class."}), 404
        session_data = qr_manager.start_session(class_info["id"], session["user_id"], duration)
        return jsonify({
            "success": True,
            "session_id": session_data["session_id"],
            "class_name": class_info["class_name"],
            "expires_at": session_data["expires_at"].isoformat()
        })
    finally:
        conn.close()


@app.route("/api/teacher/stop-qr", methods=["POST"])
def stop_qr():
    if session.get("role") != "teacher":
        return jsonify({"error": "Only teachers can perform this action."}), 403
    qr_manager.stop_session()
    return jsonify({"success": True, "message": "QR session has been stopped."})


@app.route("/api/qr-token", methods=["GET"])
def get_qr_token():
    token_data = qr_manager.get_current_token()
    if token_data:
        if qr_manager.active_session.get("expires_at"):
            token_data["expires_at"] = qr_manager.active_session["expires_at"].isoformat()
        return jsonify(token_data)
    return jsonify({"error": "There is no active QR session."}), 404


# --- API: Classes & Analytics ---
@app.route("/api/classes", methods=["GET"])
def list_classes():
    if "user_id" not in session:
        return jsonify({"error": "Not authenticated."}), 401
    user_id = session["user_id"]
    conn = db_manager.get_connection()
    try:
        role = session.get("role")
        if role == "teacher":
            rows = conn.execute("SELECT id, class_name, class_code, created_at FROM classes WHERE teacher_id = ?", (user_id,)).fetchall()
            return jsonify({"classes": [dict(r) for r in rows]})
        else:
            rows = conn.execute("SELECT id, class_name, class_code, teacher_id FROM classes").fetchall()
            return jsonify({"classes": [dict(r) for r in rows]})
    finally:
        conn.close()


@app.route("/api/teacher/analytics", methods=["GET"])
def teacher_analytics():
    if session.get("role") != "teacher":
        return jsonify({"error": "Only teachers can access analytics."}), 403
    teacher_id = session["user_id"]
    conn = db_manager.get_connection()
    try:
        rows = conn.execute(
            """
            SELECT c.id as class_id, c.class_name,
                COUNT(a.id) as marks,
                (SELECT COUNT(DISTINCT a2.attendance_date) FROM attendance a2 WHERE a2.class_id = c.id AND a2.attendance_date >= date('now', '-30 days')) as days_count
            FROM classes c
            LEFT JOIN attendance a ON a.class_id = c.id
            WHERE c.teacher_id = ?
            GROUP BY c.id
            """,
            (teacher_id,),
        ).fetchall()

        classes = []
        for r in rows:
            marks = r["marks"] or 0
            days = r["days_count"] or 0
            avg = 0
            if days > 0:
                avg = round(marks / days, 2)
            classes.append({"class_id": r["class_id"], "class_name": r["class_name"], "attendance_marks": marks, "days": days, "avg_marks_per_day": avg})

        low_rows = conn.execute(
            """
            SELECT u.user_id, u.name, u.email, c.class_name,
                SUM(CASE WHEN a.status = 'present' THEN 1 ELSE 0 END) as presents,
                COUNT(DISTINCT a.attendance_date) as days_marked
            FROM users u
            JOIN attendance a ON a.student_id = u.user_id
            JOIN classes c ON c.id = a.class_id
            WHERE c.teacher_id = ? AND a.attendance_date >= date('now', '-30 days')
            GROUP BY u.user_id, c.id
            HAVING (CAST(presents AS FLOAT) / NULLIF(days_marked,0)) < 0.6
            ORDER BY presents ASC
            LIMIT 10
            """,
            (teacher_id,),
        ).fetchall()

        at_risk = [dict(r) for r in low_rows]

        return jsonify({"classes": classes, "at_risk_students": at_risk})
    finally:
        conn.close()


# --- API: Student ---
@app.route("/api/student/scan-qr", methods=["POST"])
def scan_qr():
    if session.get("role") != "student":
        return jsonify({"error": "Only students can perform this action."}), 403
    data = request.get_json() or {}
    token = data.get("token")
    session_id = data.get("session_id")
    if not token or not session_id:
        return jsonify({"success": False, "message": "Token and session ID are required."}), 400

    if not qr_manager.validate_token(token, session_id):
        return jsonify({"success": False, "message": "The QR code is invalid or has expired."}), 400

    conn = db_manager.get_connection()
    try:
        today = datetime.utcnow().date().isoformat()
        class_id = qr_manager.active_session.get("class_id") if qr_manager.active_session else None
        if not class_id:
            return jsonify({"success": False, "message": "The QR session is no longer active."}), 400
        exists = conn.execute(
            "SELECT 1 FROM attendance WHERE student_id = ? AND class_id = ? AND attendance_date = ?",
            (session["user_id"], class_id, today),
        ).fetchone()
        if exists:
            return jsonify({"success": False, "message": "You have already marked attendance for this class today."}), 409
        conn.execute("INSERT INTO attendance (student_id, class_id, attendance_date) VALUES (?, ?, ?)", (session["user_id"], class_id, today))
        conn.commit()
        return jsonify({"success": True, "message": "Attendance marked successfully!"})
    except sqlite3.Error:
        app.logger.exception("Database error while marking attendance")
        return jsonify({"success": False, "message": "A database error occurred."}), 500
    finally:
        conn.close()


@app.route("/api/student/attendance-history", methods=["GET"])
def get_attendance_history():
    if session.get("role") != "student":
        return jsonify({"error": "Only students can perform this action."}), 403
    conn = db_manager.get_connection()
    try:
        records = conn.execute(
            """
            SELECT a.attendance_date as date, a.status, c.class_name FROM attendance a
            JOIN classes c ON a.class_id = c.id WHERE a.student_id = ?
            ORDER BY a.attendance_date DESC LIMIT 30
            """,
            (session["user_id"],),
        ).fetchall()
        return jsonify({"attendance_records": [dict(row) for row in records]})
    finally:
        conn.close()


if __name__ == "__main__":
    # To run this application with camera access, you need to generate a self-signed SSL certificate,
    # provide certificate paths via environment variables, or run without SSL on localhost.
    # Options:
    #  - Provide real certificate files and set environment variables:
    #      SSL_CERT_FILE=/path/to/cert.pem
    #      SSL_KEY_FILE=/path/to/key.pem
    #  - Use ad-hoc SSL (Werkzeug) for development only:
    #      USE_ADHOC_SSL=1
    #  - Or run without SSL (http://localhost:5000) which is acceptable for camera access on localhost.
    app.logger.info("--- Campus Guardian Backend ---")

    # Read cert paths from environment to allow flexibility
    cert_file = os.environ.get("SSL_CERT_FILE", "cert.pem")
    key_file = os.environ.get("SSL_KEY_FILE", "key.pem")
    use_adhoc = os.environ.get("USE_ADHOC_SSL", "").lower() in ("1", "true", "yes")

    # Prioritize explicit cert files if they both exist.
    if cert_file and key_file and os.path.exists(cert_file) and os.path.exists(key_file):
        app.logger.info(f"Starting server at https://localhost:5000 using provided cert files: {cert_file}, {key_file}")
        app.run(debug=True, port=5000, ssl_context=(cert_file, key_file))
    else:
        if use_adhoc:
            # Werkzeug will generate a temporary self-signed certificate for HTTPS.
            app.logger.info("Starting server at https://localhost:5000 using ad-hoc SSL (temporary self-signed certificate).")
            app.run(debug=True, port=5000, ssl_context="adhoc")
        else:
            app.logger.info("Starting server at http://localhost:5000 (no SSL). Browsers allow camera access on localhost without HTTPS.")
            app.run(debug=True, port=5000)