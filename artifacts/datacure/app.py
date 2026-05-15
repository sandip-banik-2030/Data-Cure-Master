import os
import math
import secrets
import time
from datetime import date
from functools import wraps

from flask import (
    Flask, render_template, request, redirect,
    url_for, session, jsonify, g, flash, get_flashed_messages
)
from werkzeug.security import generate_password_hash, check_password_hash

from database.db import get_db, init_db
from services.streak import update_streak, STREAK_TARGET, STREAK_BONUS_COINS
from services.ratelimit import check_rate_limit, rl_key
from services.wallet import add_coins, deduct_coins, get_balance, WalletError, InsufficientFundsError
from services.auth import (
    encode_phone, decode_phone, detect_operator,
    validate_name, validate_phone, validate_password
)

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "datacure-dev-secret-2024")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

COINS_PER_100MB  = 5
COINS_PER_AD     = 10
COINS_PER_RUPEE  = 100
MAX_ADS_PER_DAY  = 15
MB_PER_REWARD    = 100
FIXED_OTP        = "1234"
REDEEM_PACKS     = {15: 1500, 20: 2000}


# ── DB teardown ────────────────────────────────────────────────────────────────
@app.teardown_appcontext
def close_db(error):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ── Decorators ─────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"), 303)
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"), 303)
        user = get_user(session["user_id"])
        if not user or not user["is_admin"]:
            return redirect(url_for("dashboard"), 303)
        return f(*args, **kwargs)
    return decorated


def rate_limited(is_api=False, redirect_to=None, post_only=True):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if post_only and request.method != "POST":
                return f(*args, **kwargs)
            uid = session.get("user_id", request.remote_addr)
            key = rl_key(f.__name__, uid)
            allowed, retry_after = check_rate_limit(session, key)
            if not allowed:
                msg = f"Too many requests. Please wait {retry_after}s."
                if is_api:
                    return jsonify({"success": False, "error": msg}), 429
                flash(msg, "error")
                dest = url_for(redirect_to) if redirect_to else (request.referrer or url_for("dashboard"))
                return redirect(dest, 303)
            return f(*args, **kwargs)
        return decorated
    return decorator


# ── Helpers ────────────────────────────────────────────────────────────────────
def get_user(user_id):
    return get_db().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def get_flash_error():
    for cat, msg in get_flashed_messages(with_categories=True):
        if cat == "error":
            return msg
    return None


# ── Routes: Root ───────────────────────────────────────────────────────────────
@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"), 303)
    return redirect(url_for("login"), 303)


# ── Routes: Register ───────────────────────────────────────────────────────────
@app.route("/register", methods=["GET", "POST"])
@rate_limited(redirect_to="register")
def register():
    if "user_id" in session:
        return redirect(url_for("dashboard"), 303)

    if request.method == "POST":
        name     = request.form.get("name", "").strip()
        phone    = request.form.get("phone", "").strip()
        password = request.form.get("password", "").strip()
        otp      = request.form.get("otp", "").strip()

        err = validate_name(name) or validate_phone(phone) or validate_password(password)
        if err:
            flash(err, "error")
            return redirect(url_for("register"), 303)

        if otp != FIXED_OTP:
            flash("Invalid OTP. Use 1234 for demo.", "error")
            return redirect(url_for("register"), 303)

        enc_phone = encode_phone(phone)
        db = get_db()
        if db.execute("SELECT id FROM users WHERE phone_encrypted=?", (enc_phone,)).fetchone():
            flash("An account with this number already exists.", "error")
            return redirect(url_for("register"), 303)

        pw_hash = generate_password_hash(password)
        db.execute(
            "INSERT INTO users (name, phone_encrypted, password_hash, coins) VALUES (?,?,?,0)",
            (name, enc_phone, pw_hash),
        )
        db.commit()
        user = db.execute("SELECT id FROM users WHERE phone_encrypted=?", (enc_phone,)).fetchone()
        session.clear()
        session["user_id"] = user["id"]
        return redirect(url_for("dashboard"), 303)

    return render_template("register.html", error=get_flash_error())


# ── Routes: Login ──────────────────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
@rate_limited(redirect_to="login")
def login():
    if "user_id" in session:
        return redirect(url_for("dashboard"), 303)

    if request.method == "POST":
        phone    = request.form.get("phone", "").strip()
        password = request.form.get("password", "").strip()

        err = validate_phone(phone)
        if err:
            flash(err, "error")
            return redirect(url_for("login"), 303)

        enc_phone = encode_phone(phone)
        db   = get_db()
        user = db.execute("SELECT * FROM users WHERE phone_encrypted=?", (enc_phone,)).fetchone()

        if not user:
            flash("No account found with this number.", "error")
            return redirect(url_for("login"), 303)
        if not check_password_hash(user["password_hash"], password):
            flash("Incorrect password.", "error")
            return redirect(url_for("login"), 303)

        session.clear()
        session["user_id"] = user["id"]
        return redirect(url_for("dashboard"), 303)

    return render_template("login.html", error=get_flash_error())


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"), 303)


# ── Routes: Dashboard ──────────────────────────────────────────────────────────
@app.route("/dashboard")
@login_required
def dashboard():
    db  = get_db()
    uid = session["user_id"]

    streak_info = update_streak(db, uid)
    user = get_user(uid)

    today     = date.today().isoformat()
    ads_row   = db.execute(
        "SELECT ads_watched FROM ad_rewards WHERE user_id=? AND reward_date=?",
        (uid, today),
    ).fetchone()
    ads_today = ads_row["ads_watched"] if ads_row else 0

    recent = db.execute(
        "SELECT * FROM transactions WHERE user_id=? ORDER BY created_at DESC LIMIT 10",
        (uid,),
    ).fetchall()

    redemptions = db.execute(
        "SELECT * FROM recharge_requests WHERE user_id=? ORDER BY created_at DESC LIMIT 5",
        (uid,),
    ).fetchall()

    coins          = get_balance(db, uid)
    rupees_balance = coins / COINS_PER_RUPEE

    return render_template(
        "dashboard.html",
        user=user,
        ads_today=ads_today,
        max_ads=MAX_ADS_PER_DAY,
        recent=recent,
        redemptions=redemptions,
        rupees_balance=rupees_balance,
        coins_per_rupee=COINS_PER_RUPEE,
        decode_phone=decode_phone,
        streak_info=streak_info,
    )


# ── Routes: Log Data ───────────────────────────────────────────────────────────
@app.route("/log-data", methods=["GET"])
@login_required
def log_data():
    token = secrets.token_hex(16)
    session["log_token"] = token
    user = get_user(session["user_id"])
    return render_template(
        "log_data.html",
        coins_per_100mb=COINS_PER_100MB,
        mb_per_reward=MB_PER_REWARD,
        submit_token=token,
        user=user,
    )


@app.route("/api/log-data", methods=["POST"])
@login_required
@rate_limited(is_api=True)
def api_log_data():
    data    = request.get_json(silent=True) or {}
    token   = data.get("token", "")
    user_id = session["user_id"]

    if not token or token != session.get("log_token"):
        return jsonify({"success": False, "error": "Duplicate or invalid submission."}), 400
    session.pop("log_token", None)

    raw_mb = data.get("mb_saved")
    try:
        mb_saved = int(raw_mb)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "MB value must be a whole number."}), 400

    if mb_saved <= 0:
        return jsonify({"success": False, "error": "Value must be at least 1 MB."}), 400
    if mb_saved > 2000:
        return jsonify({"success": False, "error": "Maximum is 2000 MB per submission."}), 400

    coins_earned = math.floor(mb_saved / 100) * COINS_PER_100MB
    db = get_db()

    db.execute(
        "UPDATE users SET lifetime_data_saved = lifetime_data_saved + ? WHERE id=?",
        (mb_saved, user_id),
    )

    new_balance = get_balance(db, user_id)
    if coins_earned > 0:
        new_balance = add_coins(db, user_id, coins_earned, f"Logged {mb_saved} MB saved")
    else:
        db.commit()

    new_token = secrets.token_hex(16)
    session["log_token"] = new_token

    return jsonify({
        "success":     True,
        "new_coins":   coins_earned,
        "total_coins": new_balance,
        "mb_saved":    mb_saved,
        "next_token":  new_token,
    })


# ── Routes: Ad Rewards ─────────────────────────────────────────────────────────
@app.route("/rewards")
@login_required
def rewards():
    db  = get_db()
    uid = session["user_id"]
    today = date.today().isoformat()
    ads_row = db.execute(
        "SELECT ads_watched FROM ad_rewards WHERE user_id=? AND reward_date=?",
        (uid, today),
    ).fetchone()
    ads_today = ads_row["ads_watched"] if ads_row else 0
    user = get_user(uid)
    return render_template(
        "rewards.html",
        user=user,
        coins_per_ad=COINS_PER_AD,
        ads_today=ads_today,
        max_ads=MAX_ADS_PER_DAY,
    )


@app.route("/api/ad/start", methods=["POST"])
@login_required
@rate_limited(is_api=True)
def api_ad_start():
    today = date.today().isoformat()
    db    = get_db()
    uid   = session["user_id"]

    ads_row = db.execute(
        "SELECT ads_watched FROM ad_rewards WHERE user_id=? AND reward_date=?",
        (uid, today),
    ).fetchone()
    ads_today = ads_row["ads_watched"] if ads_row else 0

    if ads_today >= MAX_ADS_PER_DAY:
        return jsonify({"ok": False, "reason": "daily_limit"}), 429

    watch_token = secrets.token_hex(16)
    session["ad_token"]      = watch_token
    session["ad_token_time"] = time.time()
    return jsonify({"ok": True, "token": watch_token, "duration": 15})


@app.route("/api/ad/complete", methods=["POST"])
@login_required
@rate_limited(is_api=True)
def api_ad_complete():
    data  = request.get_json(silent=True) or {}
    token = data.get("token", "")
    uid   = session["user_id"]

    if token != session.get("ad_token"):
        return jsonify({"ok": False, "error": "Invalid token."}), 400

    elapsed = time.time() - session.get("ad_token_time", 0)
    if elapsed < 14:
        return jsonify({"ok": False, "error": "Ad not fully watched."}), 400

    session.pop("ad_token", None)
    session.pop("ad_token_time", None)

    today = date.today().isoformat()
    db    = get_db()

    ads_row = db.execute(
        "SELECT id, ads_watched FROM ad_rewards WHERE user_id=? AND reward_date=?",
        (uid, today),
    ).fetchone()
    ads_today = ads_row["ads_watched"] if ads_row else 0

    if ads_today >= MAX_ADS_PER_DAY:
        return jsonify({"ok": False, "error": "Daily limit reached."}), 429

    if ads_row:
        db.execute(
            "UPDATE ad_rewards SET ads_watched = ads_watched + 1 WHERE id=?",
            (ads_row["id"],),
        )
    else:
        db.execute(
            "INSERT INTO ad_rewards (user_id, reward_date, ads_watched) VALUES (?,?,1)",
            (uid, today),
        )

    db.execute("UPDATE users SET total_ads_watched = total_ads_watched + 1 WHERE id=?", (uid,))
    new_balance = add_coins(db, uid, COINS_PER_AD, "Watched reward ad")

    return jsonify({
        "ok":         True,
        "new_coins":  COINS_PER_AD,
        "total_coins": new_balance,
        "ads_today":  ads_today + 1,
    })


# ── Routes: Redeem ─────────────────────────────────────────────────────────────
@app.route("/redeem", methods=["GET", "POST"])
@login_required
@rate_limited(redirect_to="redeem")
def redeem():
    db  = get_db()
    uid = session["user_id"]
    user = get_user(uid)

    if request.method == "POST":
        amount_inr = request.form.get("amount_inr", "")
        phone      = request.form.get("phone", "").strip()

        try:
            amount_inr = int(amount_inr)
        except (TypeError, ValueError):
            flash("Invalid pack selection.", "error")
            return redirect(url_for("redeem"), 303)

        if amount_inr not in REDEEM_PACKS:
            flash("Invalid pack selection.", "error")
            return redirect(url_for("redeem"), 303)

        err = validate_phone(phone)
        if err:
            flash(err, "error")
            return redirect(url_for("redeem"), 303)

        coins_required = REDEEM_PACKS[amount_inr]
        balance = get_balance(db, uid)

        if balance < coins_required:
            flash(f"Insufficient coins. You need {coins_required} coins.", "error")
            return redirect(url_for("redeem"), 303)

        operator = detect_operator(phone)

        try:
            deduct_coins(db, uid, coins_required, f"Recharge ₹{amount_inr} to {phone}")
        except InsufficientFundsError:
            flash("Insufficient coins.", "error")
            return redirect(url_for("redeem"), 303)

        enc_phone = encode_phone(phone)
        db.execute(
            """INSERT INTO recharge_requests
               (user_id, phone_number, operator, recharge_pack, recharge_amount, coins_used, status)
               VALUES (?,?,?,?,?,?,'pending')""",
            (uid, enc_phone, operator, f"₹{amount_inr} Recharge", amount_inr, coins_required),
        )
        db.commit()

        flash(f"₹{amount_inr} recharge request submitted for {phone}!", "success")
        return redirect(url_for("my_requests"), 303)

    error = None
    success = None
    for cat, msg in get_flashed_messages(with_categories=True):
        if cat == "error":
            error = msg
        elif cat == "success":
            success = msg

    return render_template(
        "redeem.html",
        user=user,
        redeem_packs=REDEEM_PACKS,
        coins_per_rupee=COINS_PER_RUPEE,
        decode_phone=decode_phone,
        error=error,
        success=success,
    )


# ── Routes: My Requests ────────────────────────────────────────────────────────
@app.route("/my_requests")
@login_required
def my_requests():
    db  = get_db()
    uid = session["user_id"]
    user = get_user(uid)

    requests_list = db.execute(
        "SELECT * FROM recharge_requests WHERE user_id=? ORDER BY created_at DESC",
        (uid,),
    ).fetchall()

    return render_template(
        "my_requests.html",
        user=user,
        requests=requests_list,
        decode_phone=decode_phone,
    )


# ── Routes: Admin ──────────────────────────────────────────────────────────────
@app.route("/admin")
@admin_required
def admin():
    db = get_db()

    users_list = db.execute(
        "SELECT id, name, phone_encrypted, coins, lifetime_data_saved, streak, total_ads_watched, created_at, is_admin FROM users ORDER BY created_at DESC"
    ).fetchall()

    pending_requests = db.execute(
        """SELECT r.*, u.name as user_name
           FROM recharge_requests r
           JOIN users u ON u.id = r.user_id
           WHERE r.status = 'pending'
           ORDER BY r.created_at DESC"""
    ).fetchall()

    stats = db.execute(
        """SELECT
           COUNT(*) as total_users,
           SUM(coins) as total_coins,
           SUM(lifetime_data_saved) as total_data_mb,
           SUM(total_ads_watched) as total_ads
           FROM users"""
    ).fetchone()

    return render_template(
        "admin.html",
        users=users_list,
        pending_requests=pending_requests,
        stats=stats,
        decode_phone=decode_phone,
    )


@app.route("/admin/request/<int:req_id>/<action>", methods=["POST"])
@admin_required
def admin_request_action(req_id, action):
    if action not in ("complete", "fail"):
        return redirect(url_for("admin"), 303)
    db = get_db()
    status = "completed" if action == "complete" else "failed"
    db.execute("UPDATE recharge_requests SET status=? WHERE id=?", (status, req_id))
    db.commit()
    return redirect(url_for("admin"), 303)


# ── Template context helpers ───────────────────────────────────────────────────
@app.context_processor
def inject_helpers():
    def get_user_coins():
        if "user_id" not in session:
            return 0
        row = get_db().execute("SELECT coins FROM users WHERE id=?", (session["user_id"],)).fetchone()
        return row["coins"] if row else 0
    return {"get_user_coins": get_user_coins}


if __name__ == "__main__":
    init_db(app)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
