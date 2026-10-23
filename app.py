"""EUBA osobný obedový plán — Flask backend."""

import hmac
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path
from functools import wraps

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from openai import OpenAI
from werkzeug.security import check_password_hash, generate_password_hash


ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / "jedalnicek.db"
IORDERS_URL = "https://jedalen.euba.sk/iorders/"
WEEKDAYS_SK = ("Pondelok", "Utorok", "Streda", "Štvrtok", "Piatok", "Sobota", "Nedeľa")
KNOWN_ALLERGENS = {str(number) for number in range(1, 16)} | {"BL", "BM", "D", "E"}

load_dotenv(ROOT / ".env", override=True)

app = Flask(__name__, template_folder=str(ROOT))
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "false").lower() == "true",
)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()A


def init_db():
    with sqlite3.connect(DATABASE) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL,
                profile_json TEXT,
                created_at TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS meal_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                plan_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)


init_db()


@app.before_request
def prepare_request():
    session.setdefault("csrf_token", secrets.token_urlsafe(32))

    if request.method == "POST":
        expected = session.get("csrf_token", "")
        supplied = (
            request.headers.get("X-CSRF-Token")
            or request.form.get("csrf_token")
            or ""
        )
        if not expected or not hmac.compare_digest(expected, supplied):
            abort(400, description="Formulár vypršal. Obnov stránku a skús to znova.")


@app.context_processor
def template_values():
    user = None
    if session.get("user_id"):
        user = get_db().execute(
            "SELECT id, username FROM users WHERE id = ?",
            (session["user_id"],),
        ).fetchone()
    return {
        "current_user": user,
        "csrf_token": session.get("csrf_token", ""),
    }


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login_page"))
        return view(*args, **kwargs)
    return wrapped_view


def load_menu():
    """Načíta aktuálne zverejnenú ponuku z iOrders EUBA."""
    response = requests.get(
        IORDERS_URL,
        headers={"User-Agent": "Mozilla/5.0 (compatible; EUBA-MealPlanner/1.0)"},
        timeout=(5, 20),
    )
    response.raise_for_status()
    response.encoding = "windows-1250"

    soup = BeautifulSoup(response.text, "html.parser")
    table = soup.select_one("div.foodtable table")
    if table is None:
        raise RuntimeError("Na stránke iOrders sa nenašla tabuľka s jedálnym lístkom.")

    meals = []
    current_date = None
    current_category = None

    for row in table.select("tr"):
        day_cell = row.select_one("td.foodtableday")
        if day_cell:
            day_link = day_cell.select_one("a[id^='mnu']")
            match = re.search(r"mnu(\d{6})", day_link.get("id", "")) if day_link else None
            current_date = (
                datetime.strptime(match.group(1), "%d%m%y").date()
                if match else None
            )
            current_category = None
            continue

        category_cell = row.select_one("td.foodtabletype")
        if category_cell:
            current_category = category_cell.get_text(" ", strip=True)

        code_cell = row.select_one("td.foodtablecode")
        if not current_date or not current_category or not code_cell:
            continue

        food_cell = next(
            (
                cell for cell in row.find_all("td", recursive=False)
                if cell.select_one("span.foodtable--itemprefix")
            ),
            None,
        )
        if food_cell is None:
            food_cell = next(
                (
                    cell for cell in row.find_all("td", recursive=False)
                    if not cell.get("class") and cell.get_text(" ", strip=True)
                ),
                None,
            )
        if food_cell is None:
            continue

        lines = [
            line.strip()
            for line in food_cell.get_text("\n", strip=True).splitlines()
            if line.strip()
        ]
        description = " ".join(lines)
        if not description:
            continue
        if re.match(r"Minútkové jedlo\s+M\d+\s*$", description, re.IGNORECASE):
            continue

        found_codes = re.findall(
            r"(?<![A-Za-z0-9])(?:BL|BM|[A-Z]|\d{1,2})(?![A-Za-z0-9])",
            description,
        )
        allergens = sorted({
            token.upper() for token in found_codes
            if token.upper() in KNOWN_ALLERGENS
        })
        weights = re.findall(
            r"\d+(?:[.,]\d+)?\s?(?:kg|g|ml|l|ks)\b",
            description,
            re.IGNORECASE,
        )

        meals.append({
            "id": (
                f"{current_date.isoformat()}-"
                f"{current_category}-{code_cell.get_text(strip=True)}"
            ),
            "date": current_date.isoformat(),
            "day": WEEKDAYS_SK[current_date.weekday()],
            "category": current_category,
            "code": code_cell.get_text(strip=True),
            "soup": lines[0] if len(lines) > 1 else None,
            "name": lines[-1],
            "description": description,
            "weight": ", ".join(weights) if weights else None,
            "allergens": allergens,
        })

    if not meals:
        raise RuntimeError("Ponuku sa načítalo, ale nenašli sa v nej žiadne jedlá.")
    return meals


def parse_agent_json(text):
    """Prečíta JSON aj v prípade, že ho agent omylom obalí do code fence."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


def create_agent_plan(profile, safe_meals):
    agent_id = os.getenv("OPENAI_AGENT_ID")
    if not agent_id:
        raise RuntimeError("V súbore .env chýba OPENAI_AGENT_ID.")

    payload = {
        "profil_pouzivatela": profile,
        "aktualne_menu_iOrders": safe_meals,
        "pokyn": (
            "Vyber najviac jedno jedlo na každý dátum. Používaj iba ID jedál "
            "zo vstupného menu. Vráť JSON objekt s poľom recommendations. "
            "Každá položka má mať date, meal_id, reason a score od 0 do 100. "
            "Nevymýšľaj názvy, alergény ani výživové údaje."
        ),
    }

    client = OpenAI()
    events = client.beta.agents.sessions.create(
        agent_id=agent_id,
        environment={"type": "none"},
        input=json.dumps(payload, ensure_ascii=False),
        stream=True,
    )

    with events.with_result_collection() as stream:
        for _event in stream:
            pass
        result = stream.get_final_result()

    return parse_agent_json(result.output_text)


def filter_and_validate_plan(agent_response, safe_meals):
    """Akceptuje iba jedlá, ktoré naozaj prišli z aktuálneho menu."""
    meals_by_id = {meal["id"]: meal for meal in safe_meals}
    recommendations = []
    used_dates = set()

    for item in agent_response.get("recommendations", []):
        if not isinstance(item, dict):
            continue

        meal = meals_by_id.get(str(item.get("meal_id", "")))
        if not meal or meal["date"] in used_dates:
            continue

        try:
            score = max(0, min(100, int(item.get("score", 0))))
        except (TypeError, ValueError):
            score = 0

        recommendations.append({
            "date": meal["date"],
            "day": meal["day"],
            "meal_id": meal["id"],
            "name": meal["name"],
            "description": meal["description"],
            "category": meal["category"],
            "weight": meal["weight"],
            "allergens": meal["allergens"],
            "reason": str(item.get("reason", ""))[:500],
            "score": score,
        })
        used_dates.add(meal["date"])

    return sorted(recommendations, key=lambda item: item["date"])


@app.get("/style.css")
def stylesheet():
    return send_from_directory(ROOT, "style.css")


@app.get("/")
def home():
    return render_template("index.html")


@app.get("/register")
def register_page():
    if session.get("user_id"):
        return redirect(url_for("profile_page"))
    return render_template("register.html")


@app.post("/register")
def register():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    confirmation = request.form.get("password_confirm", "")

    if len(username) < 3 or len(username) > 40:
        return render_template(
            "register.html",
            error="Používateľské meno musí mať 3 až 40 znakov.",
        ), 400
    if len(password) < 10:
        return render_template(
            "register.html",
            error="Heslo musí mať aspoň 10 znakov.",
        ), 400
    if password != confirmation:
        return render_template(
            "register.html",
            error="Heslá sa nezhodujú.",
        ), 400

    try:
        cursor = get_db().execute(
            """
            INSERT INTO users (username, password_hash, created_at)
            VALUES (?, ?, ?)
            """,
            (username, generate_password_hash(password), datetime.utcnow().isoformat()),
        )
        get_db().commit()
    except sqlite3.IntegrityError:
        return render_template(
            "register.html",
            error="Toto používateľské meno už existuje.",
        ), 409

    session.clear()
    session["user_id"] = cursor.lastrowid
    session["csrf_token"] = secrets.token_urlsafe(32)
    return redirect(url_for("profile_page"))


@app.get("/login")
def login_page():
    if session.get("user_id"):
        return redirect(url_for("profile_page"))
    return render_template("login.html")


@app.post("/login")
def login():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    user = get_db().execute(
        "SELECT id, password_hash FROM users WHERE username = ?",
        (username,),
    ).fetchone()

    if not user or not check_password_hash(user["password_hash"], password):
        return render_template(
            "login.html",
            error="Nesprávne meno alebo heslo.",
        ), 401

    session.clear()
    session["user_id"] = user["id"]
    session["csrf_token"] = secrets.token_urlsafe(32)
    return redirect(url_for("profile_page"))


@app.post("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.get("/profile")
@login_required
def profile_page():
    user = get_db().execute(
        "SELECT profile_json FROM users WHERE id = ?",
        (session["user_id"],),
    ).fetchone()
    profile = json.loads(user["profile_json"]) if user and user["profile_json"] else None
    return render_template("profile.html", profile=profile)


@app.post("/api/profile")
@login_required
def save_profile():
    profile = request.get_json(silent=True)
    if not isinstance(profile, dict):
        return jsonify({"error": "Profil sa nepodarilo prečítať."}), 400

    required_fields = {
        "goal", "cuisines", "proteins", "cooking_methods",
        "sides", "soup_preference", "vegetarian_days",
        "spice_level", "likes", "allergens",
    }
    if not required_fields.issubset(profile):
        return jsonify({"error": "Vyplň všetkých 10 otázok."}), 400

    if not isinstance(profile["allergens"], list):
        return jsonify({"error": "Alergény musia byť zoznam kódov."}), 400

    profile["allergens"] = sorted({
        str(code).strip().upper()
        for code in profile["allergens"]
        if str(code).strip().upper() in KNOWN_ALLERGENS
    })

    get_db().execute(
        "UPDATE users SET profile_json = ? WHERE id = ?",
        (json.dumps(profile, ensure_ascii=False), session["user_id"]),
    )
    get_db().commit()
    return jsonify({"ok": True})


@app.get("/compare")
@login_required
def compare_page():
    # Starú stránku porovnávania nahrádza priamy výber agenta.
    return redirect(url_for("results_page"))


@app.get("/results")
@login_required
def results_page():
    return render_template("results.html")


@app.get("/api/menu")
@login_required
def menu():
    try:
        meals = load_menu()
        return jsonify({"meals": meals})
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        app.logger.exception("Načítanie iOrders menu zlyhalo")
        return jsonify({"error": str(exc)}), 502


@app.post("/api/plan")
@login_required
def create_plan():
    user = get_db().execute(
        "SELECT profile_json FROM users WHERE id = ?",
        (session["user_id"],),
    ).fetchone()

    if not user or not user["profile_json"]:
        return jsonify({"error": "Najprv vyplň svoj profil."}), 400

    profile = json.loads(user["profile_json"])

    try:
        meals = load_menu()
    except Exception as exc:
        app.logger.exception("Vytvorenie plánu agentom zlyhalo")
        return jsonify({
            "error": f"{type(exc).__name__}: {exc}"
        }), 502

    excluded = set(profile.get("allergens", []))
    safe_meals = [
        meal for meal in meals
        if not (set(meal.get("allergens", [])) & excluded)
    ]

    if not safe_meals:
        return jsonify({
            "error": "Po vyradení zadaných alergénov nezostali žiadne jedlá."
        }), 422

    try:
        agent_response = create_agent_plan(profile, safe_meals)
        recommendations = filter_and_validate_plan(agent_response, safe_meals)
    except Exception as exc:
        app.logger.exception("Vytvorenie plánu agentom zlyhalo")
        return jsonify({
            "error": f"{type(exc).__name__}: {exc}"
        }), 502

    plan = {
        "recommendations": recommendations,
        "menu_source": "Aktuálna ponuka iOrders EUBA",
        "nutrition_note": (
            "iOrders pri jedlách neuvádza kalórie ani množstvo bielkovín."
        ),
        "created_at": datetime.utcnow().isoformat(),
    }

    get_db().execute(
        """
        INSERT INTO meal_plans (user_id, plan_json, created_at)
        VALUES (?, ?, ?)
        """,
        (
            session["user_id"],
            json.dumps(plan, ensure_ascii=False),
            plan["created_at"],
        ),
    )
    get_db().execute(
        "DELETE FROM meal_plans WHERE user_id = ?",
        (session["user_id"],),
    )
    get_db().commit()

    return jsonify(plan)


@app.get("/api/plan/latest")
@login_required
def latest_plan():
    row = get_db().execute(
        """
        SELECT plan_json
        FROM meal_plans
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (session["user_id"],),
    ).fetchone()

    if not row:
        return jsonify({"plan": None})
    return jsonify({"plan": json.loads(row["plan_json"])})


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG", "true").lower() == "true")
