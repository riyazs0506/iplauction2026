import eventlet
eventlet.monkey_patch(all=True)


import redis
from dotenv import load_dotenv
load_dotenv()

import os
import json
import logging
from config import MYSQL_CONFIG   # ✅ ADD THIS
from flask import jsonify

import mysql.connector
from mysql.connector import pooling
from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_socketio import SocketIO
from functools import wraps
from datetime import datetime



app = Flask(__name__)
app.secret_key = "ipl_secure_login_2026"


socketio = SocketIO(
    app,
    cors_allowed_origins=os.environ.get("SOCKET_CORS"),
    async_mode="eventlet",
    message_queue=os.environ.get("REDIS_URL"),  # 🔥 required for production scaling
    ping_timeout=20,
    ping_interval=10
)

# ================= GLOBAL CURRENT PLAYER =================
current_player_id = None

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per minute"],
    storage_uri=os.environ.get("REDIS_URL")
)



@app.before_request
def limit_request_size():
    if request.content_length and request.content_length > 10 * 1024 * 1024:
        return "Request too large", 413

# ================= DATABASE CONNECTION =================
# ================= DATABASE CONFIG =================
# ================= PRODUCTION LOGGING =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# ================= CONNECTION POOL =================
db_pool = pooling.MySQLConnectionPool(
    pool_name="ipl_pool",
    pool_size=10,
    pool_reset_session=True,
    **MYSQL_CONFIG
)

def get_db():
    return db_pool.get_connection()

# Create initial connection (keeps your logic unchanged)
def get_cursor():
    db = get_db()
    cursor = db.cursor(dictionary=True, buffered=True)
    return db, cursor


# ================= REDIS CONFIG =================
# ================= REDIS CONFIG =================
try:
    redis_url = os.environ.get("REDIS_URL")

    if redis_url:
        redis_client = redis.from_url(redis_url, decode_responses=True)
        redis_client.ping()
        logging.info("Redis connected successfully")
    else:
        redis_client = None
        logging.warning("REDIS_URL not set. Redis disabled.")

except Exception as e:
    redis_client = None
    logging.error(f"Redis connection failed: {e}")

# ================= STRATEGY POINT CALCULATION =================
def calculate_strategy(p, price):
    base_score = (
        (p["matches"] * 2) +
        (p["form_rating"] * 5) +
        (p["consistency"] * 4)
    )

    value_for_money = 20 if price <= p["base_price"] else 10
    indian_bonus = 15 if p["nationality"] == "India" else 5

    role_weight = {
        "Batsman": 1.2,
        "Bowler": 1.3,
        "All-rounder": 1.5,
        "Wicket-Keeper": 1.1
    }

    return int((base_score + value_for_money + indian_bonus)
               * role_weight.get(p["category"], 1))

def make_json_safe(data):
    if not data:
        return {}

    safe_data = {}

    for key, value in data.items():
        if isinstance(value, datetime):
            safe_data[key] = value.strftime("%Y-%m-%d %H:%M:%S")
        else:
            safe_data[key] = value

    return safe_data

# ================= REDIS CACHE HELPERS =================
# ================= REDIS CACHE HELPERS =================
def cache_set(key, data, expiry=60):
    if not redis_client:
        return
    try:
        redis_client.setex(key, expiry, json.dumps(data))
    except Exception as e:
        logging.error(f"Redis set error: {e}")

def cache_get(key):
    if not redis_client:
        return None
    try:
        data = redis_client.get(key)
        return json.loads(data) if data else None
    except Exception as e:
        logging.error(f"Redis get error: {e}")
        return None

def cache_delete(key):
    if not redis_client:
        return
    try:
        redis_client.delete(key)
    except Exception as e:
        logging.error(f"Redis delete error: {e}")


# ================= ADMIN LOGIN REQUIRED =================
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):

        if not session.get("admin"):
            flash("❌ Admin access only!", "danger")
            return redirect(url_for("login"))  # change if needed

        return f(*args, **kwargs)

    return decorated_function


# ================= TEAM LOGIN REQUIRED =================
def team_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "team_id" not in session:
            return redirect(url_for("team_login"))
        return f(*args, **kwargs)
    return decorated_function

from functools import wraps

def login_required(role=None):
    def wrapper(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if "role" not in session:
                return redirect(url_for("index"))

            if role and session.get("role") != role:
                return redirect(url_for("index"))

            return f(*args, **kwargs)
        return decorated_function
    return wrapper


# ================= HOME =================
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/admin-login", methods=["GET", "POST"])
def admin_login():

    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        if username == "admin" and password == "admin12345678":
            session.clear()
            session["admin"] = True
            return redirect(url_for("auction"))
        else:
            flash("Invalid Admin Credentials")

    return render_template("admin_login.html")

@app.route("/team-login", methods=["GET", "POST"])
def team_login():

    if request.method == "POST":

        db, cursor = get_cursor()

        try:
            username = request.form["username"]
            password = request.form["password"]

            cursor.execute("""
                SELECT * FROM teams
                WHERE username=%s AND password=%s
            """, (username, password))

            team = cursor.fetchone()

        finally:
            cursor.close()
            db.close()

        if team:
            session.clear()
            session["team_id"] = team["id"]
            session["team_name"] = team["name"]
            return redirect(url_for("team_dashboard"))
        else:
            flash("Invalid Team Login")

    return render_template("team_login.html")


@app.route("/logout")
def logout():

    role = session.get("role")

    session.clear()

    if role == "team":
        return redirect(url_for("team_login"))

    elif role == "admin":
        return redirect(url_for("admin_login"))

    else:
        return redirect(url_for("index"))



@app.route("/team-dashboard")
@team_required  # if you are using login protection
def team_dashboard():

    db, cursor = get_cursor()

    try:
        team_id = session.get("team_id")

        cursor.execute("SELECT * FROM teams WHERE id=%s", (team_id,))
        team = cursor.fetchone()

        cursor.execute("""
            SELECT name, category, nationality, sold_price
            FROM players
            WHERE team_id=%s
        """, (team_id,))
        players = cursor.fetchall()

        cursor.execute("""
            SELECT category, COUNT(*) as c
            FROM players
            WHERE team_id=%s
            GROUP BY category
        """, (team_id,))
        category_data = cursor.fetchall()

        category_map = {row["category"]: row["c"] for row in category_data}

        cursor.execute("""
            SELECT COUNT(*) as c
            FROM players
            WHERE team_id=%s AND nationality!='India'
        """, (team_id,))
        overseas_count = cursor.fetchone()["c"]

    finally:
        cursor.close()
        db.close()

    # ================= REMAINING LIMITS =================
    limits = {
        "batsman": 4 - category_map.get("Batsman", 0),
        "bowler": 3 - category_map.get("Bowler", 0),
        "allrounder": 3 - category_map.get("All-rounder", 0),
        "overseas": 4 - overseas_count
    }

    # Prevent negative numbers
    for key in limits:
        if limits[key] < 0:
            limits[key] = 0

    # ================= TEAM COLOR =================
    # ================= TEAM IPL OFFICIAL COLORS =================
    theme_map = {
        "CSK": "#FFD700",                # Yellow
        "MI": "#004BA0",                 # Blue
        "RCB": "#EC1C24",                # Red
        "KKR": "#3A225D",                # Purple
        "SRH": "#FF822A",                # Orange
        "DC": "#17449B",                 # Blue
        "RR": "#FF69B4",                 # Pink
        "LSG": "#00AEEF",                # Sky Blue
        "PBKS": "#ED1B24",               # Red
        "GT": "#0C2340"                  # Navy
    }

    theme_color = theme_map.get(team["name"], "#ffffff")


    return render_template(
        "team_dashboard.html",
        team=team,
        players=players,
        limits=limits,      # ✅ THIS WAS MISSING
        theme_color=theme_color
    )


@app.route("/broadcast")
def broadcast():
    return render_template("broadcast.html")

# ================= AUCTION =================
@app.route("/auction")
@admin_required
def auction():

    global current_player_id

    db, cursor = get_cursor()

    try:
        category = request.args.get("category")
        reauction = request.args.get("reauction")

        if not category:
            return render_template("auction.html", players=[], category=None)

        # Fetch players
        if reauction:
            cursor.execute("""
                SELECT * FROM players
                WHERE category=%s AND status='UNSOLD'
                ORDER BY id ASC
            """, (category,))
        else:
            cursor.execute("""
                SELECT * FROM players
                WHERE category=%s AND status='AVAILABLE'
                ORDER BY id ASC
            """, (category,))

        players = cursor.fetchall()
        current = players[0] if players else None

        # Send to broadcast
        if current:
            current_player_id = current["id"]
            safe_player = make_json_safe(current)
            socketio.emit("player_update", safe_player)
        else:
            current_player_id = None
            socketio.emit("player_update", {})

        # Unsold count
        cursor.execute("""
            SELECT COUNT(*) AS c FROM players
            WHERE category=%s AND status='UNSOLD'
        """, (category,))
        unsold_count = cursor.fetchone()["c"]

        # Teams
        cursor.execute("SELECT * FROM teams")
        teams = cursor.fetchall()

        limits = {
            "Batsman": 4,
            "Bowler": 3,
            "All-rounder": 3,
            "Wicket-Keeper": 1
        }

        for t in teams:
            tid = t["id"]

            cursor.execute("SELECT COUNT(*) c FROM players WHERE team_id=%s", (tid,))
            total = cursor.fetchone()["c"]

            cursor.execute("""
                SELECT category, COUNT(*) c
                FROM players
                WHERE team_id=%s
                GROUP BY category
            """, (tid,))
            cat_map = {r["category"]: r["c"] for r in cursor.fetchall()}

            cursor.execute("""
                SELECT COUNT(*) c FROM players
                WHERE team_id=%s AND nationality!='India'
            """, (tid,))
            overseas = cursor.fetchone()["c"]

            disabled = False
            reason = ""

            if total >= 11:
                disabled = True
                reason = "Maximum 11 players reached"

            elif overseas >= 4 and current and current["nationality"] != "India":
                disabled = True
                reason = "Overseas player limit (4) reached"

            elif current and cat_map.get(current["category"], 0) >= limits.get(current["category"], 0):
                disabled = True
                reason = f"{current['category']} limit reached"

            elif t["spent"] >= t["purse"]:
                disabled = True
                reason = "Purse limit exceeded"

            t["disabled"] = disabled
            t["reason"] = reason

        return render_template(
            "auction.html",
            players=players,
            current=current,
            teams=teams,
            category=category,
            unsold_count=unsold_count
        )

    finally:
        cursor.close()
        db.close()

def check_team_constraints(team_id, player, price):

    db, cursor = get_cursor()

    try:
        errors = []

        limits = {
            "Batsman": 4,
            "Bowler": 3,
            "All-rounder": 3,
            "Wicket-Keeper": 1
        }

        cursor.execute("SELECT COUNT(*) c FROM players WHERE team_id=%s", (team_id,))
        total = cursor.fetchone()["c"]
        if total >= 11:
            errors.append("Maximum 11 players allowed")

        cursor.execute("""
            SELECT category, COUNT(*) c
            FROM players
            WHERE team_id=%s
            GROUP BY category
        """, (team_id,))
        cat_map = {r["category"]: r["c"] for r in cursor.fetchall()}

        if cat_map.get(player["category"], 0) >= limits[player["category"]]:
            errors.append(f"{player['category']} limit reached")

        cursor.execute("""
            SELECT COUNT(*) c FROM players
            WHERE team_id=%s AND nationality!='India'
        """, (team_id,))
        overseas = cursor.fetchone()["c"]

        if player["nationality"] != "India" and overseas >= 4:
            errors.append("Overseas limit reached")

        cursor.execute("SELECT spent FROM teams WHERE id=%s", (team_id,))
        spent = cursor.fetchone()["spent"]

        if spent + price > 120:
            errors.append("Purse limit exceeded")

        return errors

    finally:
        cursor.close()
        db.close()

# ================= SELL PLAYER =================
@app.route("/sell", methods=["POST"])
def sell():

    db, cursor = get_cursor()

    try:
        pid = int(request.form["player_id"])
        tid = int(request.form["team_id"])
        price = int(request.form["price"])
        category = request.form["category"]

        # 🔹 Get Player
        cursor.execute("SELECT * FROM players WHERE id=%s", (pid,))
        player = cursor.fetchone()

        if not player:
            return redirect(f"/auction?category={category}")

        # 🔹 Check Constraints
        errors = check_team_constraints(tid, player, price)
        if errors:
            return redirect(f"/auction?category={category}")

        # 🔹 Calculate Strategy
        points = calculate_strategy(player, price)

        # 🔹 Update Player
        cursor.execute("""
            UPDATE players
            SET sold_price=%s,
                team_id=%s,
                strategy_points=%s,
                status='SOLD'
            WHERE id=%s
        """, (price, tid, points, pid))

        # 🔹 Update Team
        cursor.execute("""
            UPDATE teams
            SET spent=spent+%s,
                total_points=total_points+%s
            WHERE id=%s
        """, (price, points, tid))

        db.commit()

        # 🔹 Get Team Name
        cursor.execute("SELECT name FROM teams WHERE id=%s", (tid,))
        team_data = cursor.fetchone()

        # 🔥 Prepare Broadcast Payload (SAFE)
        sold_payload = {
            "id": player["id"],
            "player_name": player["name"],
            "sold_price": price,
            "team_name": team_data["name"] if team_data else "Unknown",
            "status": "SOLD"
        }

    finally:
        cursor.close()
        db.close()

    # 🔥 Broadcast AFTER DB closed
    socketio.emit("auction_result", sold_payload)

    # 🔥 Clear Cache
    cache_delete("result_page")
    cache_delete("team_balance")
    cache_delete(f"auction_{category}")

    # ⏳ Show SOLD animation delay
    socketio.sleep(2)

    # 🔥 Send next player
    send_next_player(category)

    return redirect(f"/auction?category={category}")


# ================= UNSOLD PLAYER =================
@app.route("/unsold", methods=["POST"])
@admin_required
def unsold_player():

    db, cursor = get_cursor()

    try:
        pid = int(request.form.get("player_id"))
        category = request.form.get("category")

        cursor.execute("SELECT status, name FROM players WHERE id=%s", (pid,))
        player = cursor.fetchone()

        if not player:
            return redirect(url_for("auction", category=category))

        if player["status"] == "UNSOLD":
            cursor.execute("""
                UPDATE players
                SET status='REJECTED'
                WHERE id=%s
            """, (pid,))
            final_status = "REJECTED"
        else:
            cursor.execute("""
                UPDATE players
                SET status='UNSOLD',
                    sold_price=NULL,
                    team_id=NULL
                WHERE id=%s
            """, (pid,))
            final_status = "UNSOLD"

        db.commit()

    finally:
        cursor.close()
        db.close()

    # 🔥 Broadcast outside DB
    unsold_payload = {
        "id": pid,
        "player_name": player["name"],
        "status": final_status
    }

    socketio.emit("auction_result", unsold_payload)

    cache_delete("result_page")
    cache_delete("team_balance")
    cache_delete(f"auction_{category}")

    socketio.sleep(2)
    send_next_player(category)

    return redirect(url_for("auction", category=category))

# ================= SEND NEXT PLAYER =================
def send_next_player(category):

    db, cursor = get_cursor()

    try:
        cursor.execute("""
            SELECT * FROM players
            WHERE category=%s AND status='AVAILABLE'
            ORDER BY id ASC
            LIMIT 1
        """, (category,))

        next_player = cursor.fetchone()

    finally:
        cursor.close()
        db.close()

    if next_player:
        safe_player = make_json_safe(next_player)
        socketio.emit("player_update", safe_player)
    else:
        socketio.emit("player_update", {})

@app.route("/update-sold-details", methods=["POST"])
def update_sold_details():

    db, cursor = get_cursor()

    try:
        pid = int(request.form["player_id"])
        new_price = int(request.form["sold_price"])
        new_team_id = int(request.form["team_id"])

        cursor.execute("SELECT * FROM players WHERE id=%s", (pid,))
        player = cursor.fetchone()

        if not player:
            return redirect(url_for("all_players"))

        old_team_id = player["team_id"]
        old_price = player["sold_price"] or 0
        old_points = player["strategy_points"] or 0

        if old_team_id:
            cursor.execute("""
                UPDATE teams
                SET spent = spent - %s,
                    total_points = total_points - %s
                WHERE id = %s
            """, (old_price, old_points, old_team_id))

        new_points = calculate_strategy(player, new_price)

        cursor.execute("""
            UPDATE players
            SET sold_price=%s,
                team_id=%s,
                strategy_points=%s,
                status='SOLD'
            WHERE id=%s
        """, (new_price, new_team_id, new_points, pid))

        cursor.execute("""
            UPDATE teams
            SET spent = spent + %s,
                total_points = total_points + %s
            WHERE id = %s
        """, (new_price, new_points, new_team_id))

        db.commit()

        cursor.execute("SELECT * FROM players WHERE id=%s", (pid,))
        updated_player = cursor.fetchone()

    finally:
        cursor.close()
        db.close()

    cache_delete("result_page")
    cache_delete("team_balance")

    safe_player = make_json_safe(updated_player)
    socketio.emit("player_update", safe_player)

    return redirect(url_for("all_players"))

@app.route("/update-player", methods=["POST"])
def update_player():

    db, cursor = get_cursor()

    try:
        pid = request.form["player_id"]
        name = request.form["name"]
        category = request.form["category"]
        base_price = request.form["base_price"]

        cursor.execute("""
            UPDATE players
            SET name=%s,
                category=%s,
                base_price=%s
            WHERE id=%s
        """, (name, category, base_price, pid))

        db.commit()

        cursor.execute("SELECT * FROM players WHERE id=%s", (pid,))
        updated_player = cursor.fetchone()

    finally:
        cursor.close()
        db.close()

    cache_delete("result_page")
    cache_delete("team_balance")

    safe_player = make_json_safe(updated_player)
    socketio.emit("player_update", safe_player)

    return redirect(url_for("all_players"))

@app.route("/players")
@admin_required
def all_players():

    db, cursor = get_cursor()

    try:
        cursor.execute("""
            SELECT 
                p.id,
                p.name,
                p.category,
                p.nationality,
                p.base_price,
                p.sold_price,
                p.status,
                t.name AS team
            FROM players p
            LEFT JOIN teams t ON p.team_id = t.id
            ORDER BY p.id
        """)
        players = cursor.fetchall()

        cursor.execute("SELECT * FROM teams")
        teams = cursor.fetchall()

    finally:
        cursor.close()
        db.close()

    return render_template("players.html",
                           players=players,
                           teams=teams)

@app.route("/result")
@admin_required
def result():

    db, cursor = get_cursor()

    try:
        cursor.execute("""
            SELECT 
                t.id,
                t.name,
                t.purse,
                t.spent,
                (t.purse - t.spent) AS remaining,
                t.total_points,
                COUNT(p.id) AS total_players
            FROM teams t
            LEFT JOIN players p 
                ON p.team_id = t.id AND p.status='SOLD'
            GROUP BY t.id
            ORDER BY t.total_points DESC
        """)
        teams = cursor.fetchall()

        cursor.execute("""
            SELECT 
                t.name AS team,
                p.name AS player,
                p.category,
                p.strategy_points,
                p.sold_price
            FROM players p
            JOIN teams t ON p.team_id = t.id
            WHERE p.status='SOLD'
            ORDER BY p.strategy_points DESC
        """)
        players = cursor.fetchall()

    finally:
        cursor.close()
        db.close()

    winner = teams[0] if teams else None

    return render_template(
        "result.html",
        teams=teams,
        players=players,
        winner=winner
    )
@app.route("/strategy")
@admin_required
def strategy():

    db, cursor = get_cursor()

    try:
        cursor.execute("""
            SELECT 
                p.name,
                p.category,
                p.strategy_points,
                p.sold_price,
                t.name AS team
            FROM players p
            JOIN teams t ON p.team_id = t.id
            WHERE p.status='SOLD'
            ORDER BY p.strategy_points DESC
        """)
        players = cursor.fetchall()

    finally:
        cursor.close()
        db.close()

    return render_template("strategy.html", players=players)

@app.route("/team-balance")
@admin_required
def team_balance():

    db, cursor = get_cursor()

    try:
        cursor.execute("""
            SELECT 
                t.id,
                t.name,
                t.purse,
                t.spent,

                COUNT(CASE WHEN p.status='SOLD' THEN 1 END) AS player_count,

                SUM(CASE WHEN p.category='Batsman' AND p.status='SOLD' THEN 1 ELSE 0 END) AS batsman_count,
                SUM(CASE WHEN p.category='Bowler' AND p.status='SOLD' THEN 1 ELSE 0 END) AS bowler_count,
                SUM(CASE WHEN p.category='All-rounder' AND p.status='SOLD' THEN 1 ELSE 0 END) AS allrounder_count,
                SUM(CASE WHEN p.category='Wicket-Keeper' AND p.status='SOLD' THEN 1 ELSE 0 END) AS wk_count,

                SUM(CASE WHEN p.nationality!='India' AND p.status='SOLD' THEN 1 ELSE 0 END) AS overseas_count

            FROM teams t
            LEFT JOIN players p ON p.team_id = t.id
            GROUP BY t.id
            ORDER BY t.name
        """)
        raw_teams = cursor.fetchall()

    finally:
        cursor.close()
        db.close()

    teams = []

    for t in raw_teams:
        team = dict(t)
        team["batsman_left"] = 4 - (team["batsman_count"] or 0)
        team["bowler_left"] = 3 - (team["bowler_count"] or 0)
        team["allrounder_left"] = 3 - (team["allrounder_count"] or 0)
        team["wk_left"] = 1 - (team["wk_count"] or 0)
        team["overseas_left"] = 4 - (team["overseas_count"] or 0)
        teams.append(team)

    return render_template("team_balance.html", teams=teams)

@app.route("/team-balance-data")
def team_balance_data():

    db, cursor = get_cursor()

    try:
        cursor.execute("""
            SELECT name,
                   purse,
                   spent,
                   (purse - spent) AS remaining
            FROM teams
        """)
        teams = cursor.fetchall()

    finally:
        cursor.close()
        db.close()

    return jsonify(teams)



@app.route("/health")
def health():
    return "OK", 200

# ================= SOCKET CONNECT =================
@socketio.on("connect")
def handle_connect():
    print("Client connected")

# ================= RUN =================
if __name__ == "__main__":
    socketio.run(app, debug=True)
