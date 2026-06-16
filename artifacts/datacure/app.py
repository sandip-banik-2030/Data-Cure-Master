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
from services.security import (
    log_event,
    EVENT_LOGIN_SUCCESS, EVENT_LOGIN_FAILED,
    EVENT_AD_STARTED, EVENT_AD_COMPLETED, EVENT_AD_ABORTED,
    EVENT_AD_DUPLICATE, EVENT_AD_TOO_FAST, EVENT_AD_DAILY_LIMIT,
    EVENT_ADMIN_ACTION, EVENT_RATE_LIMITED, EVENT_SPAM_ATTEMPT,
    EVENT_INVALID_TOKEN, EVENT_DATA_CAP_HIT, EVENT_DATA_DUPE,
)
from services.admob import (
    TEST_MODE as ADMOB_TEST_MODE,
    ADMOB_APP_ID, BANNER_AD_UNIT_ID, REWARDED_AD_UNIT_ID,
    AD_BATCH_SIZE, AD_BATCH_COOLDOWN_SECS,
    get_batch_state, check_cooldown, record_ad_complete as admob_record_complete,
)

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "datacure-dev-secret-2024")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

COINS_PER_100MB     = 5
COINS_PER_AD        = 10
COINS_PER_RUPEE     = 100
MAX_ADS_PER_DAY     = 15
MB_PER_REWARD       = 100
FIXED_OTP           = "1234"
REDEEM_PACKS        = {15: 1500, 20: 2000}
RECHARGE_PACK_INFO  = {
    15: {"coins": 1500, "data": "1 GB", "validity": "28 days"},
    20: {"coins": 2000, "data": "2 GB", "validity": "28 days"},
}
OPERATORS           = ["Jio", "Airtel", "Vi", "BSNL"]
ACTIVE_STATUSES     = ("pending", "processing")
TARGET_BONUS_COINS  = 20
MAX_DATA_TARGET_MB  = 500    # User-set daily target cannot exceed the hard cap
DAILY_DATA_CAP_MB   = 500    # Hard ceiling on data logged per user per day
AD_DURATION_SECS    = 15


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


def _sync_today_data(db, user_id: int, today: str) -> int:
    """Reset today_data_saved if it's a new day. Returns current today_data_saved."""
    row = db.execute(
        "SELECT today_data_saved, last_data_date FROM users WHERE id=?", (user_id,)
    ).fetchone()
    if not row:
        return 0
    if row["last_data_date"] != today:
        db.execute(
            "UPDATE users SET today_data_saved=0, last_data_date=? WHERE id=?",
            (today, user_id),
        )
        db.commit()
        return 0
    return row["today_data_saved"] or 0


def _check_target_bonus(db, user_id: int, today_saved: int, today: str) -> dict:
    """Award +20 coins if daily target is hit for the first time today."""
    row = db.execute(
        "SELECT daily_data_target, target_bonus_date FROM users WHERE id=?", (user_id,)
    ).fetchone()
    target = row["daily_data_target"] or 0
    if target <= 0 or today_saved < target:
        return {"hit": False, "bonus": 0, "target": target}
    if row["target_bonus_date"] == today:
        return {"hit": True, "bonus": 0, "target": target, "already": True}
    add_coins(db, user_id, TARGET_BONUS_COINS, f"Daily target bonus — {target} MB reached!")
    db.execute("UPDATE users SET target_bonus_date=? WHERE id=?", (today, user_id))
    db.commit()
    return {"hit": True, "bonus": TARGET_BONUS_COINS, "target": target}


def _reissue_log_token():
    """Rotate the session log token so the next submission can proceed."""
    session["log_token"] = secrets.token_hex(16)


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

        ip = request.remote_addr or ""
        if not user:
            log_event(get_db(), EVENT_LOGIN_FAILED,
                      f"Unknown phone attempted login", user_id=None, ip=ip)
            flash("No account found with this number.", "error")
            return redirect(url_for("login"), 303)
        if not check_password_hash(user["password_hash"], password):
            log_event(get_db(), EVENT_LOGIN_FAILED,
                      f"Wrong password for user {user['id']}", user_id=user["id"], ip=ip)
            flash("Incorrect password.", "error")
            return redirect(url_for("login"), 303)

        log_event(get_db(), EVENT_LOGIN_SUCCESS,
                  f"Login OK", user_id=user["id"], ip=ip)
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
    today = date.today().isoformat()

    streak_info = update_streak(db, uid)

    # Sync today's data (resets if new day), then fetch user once
    today_data_saved = _sync_today_data(db, uid, today)
    user = get_user(uid)

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
    daily_target   = user["daily_data_target"] if user["daily_data_target"] else 200
    target_pct     = min(100, round((today_data_saved / daily_target) * 100)) if daily_target > 0 else 0

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
        today_data_saved=today_data_saved,
        daily_target=daily_target,
        target_pct=target_pct,
        target_bonus_coins=TARGET_BONUS_COINS,
        max_data_target=MAX_DATA_TARGET_MB,
    )


# ── Routes: Log Data ───────────────────────────────────────────────────────────
@app.route("/log-data", methods=["GET"])
@login_required
def log_data():
    token = secrets.token_hex(16)
    session["log_token"] = token
    db  = get_db()
    uid = session["user_id"]
    today = date.today().isoformat()
    today_data_saved = _sync_today_data(db, uid, today)
    user = get_user(uid)
    daily_target = user["daily_data_target"] if user["daily_data_target"] else 200
    target_pct   = min(100, round((today_data_saved / daily_target) * 100)) if daily_target > 0 else 0
    remaining_mb = max(0, DAILY_DATA_CAP_MB - today_data_saved)
    daily_cap_hit = today_data_saved >= DAILY_DATA_CAP_MB
    return render_template(
        "log_data.html",
        coins_per_100mb=COINS_PER_100MB,
        mb_per_reward=MB_PER_REWARD,
        submit_token=token,
        user=user,
        today_data_saved=today_data_saved,
        daily_target=daily_target,
        target_pct=target_pct,
        target_bonus_coins=TARGET_BONUS_COINS,
        max_data_target=MAX_DATA_TARGET_MB,
        daily_cap_mb=DAILY_DATA_CAP_MB,
        remaining_mb=remaining_mb,
        daily_cap_hit=daily_cap_hit,
    )


@app.route("/x/log-data", methods=["POST"])
@login_required
@rate_limited(is_api=True)
def api_log_data():
    data    = request.get_json(silent=True) or {}
    token   = data.get("token", "")
    user_id = session["user_id"]
    ip      = request.remote_addr

    # ── 1. Token guard: prevents replay and double-submit ──────────────────
    if not token or token != session.get("log_token"):
        log_event(get_db(), EVENT_INVALID_TOKEN,
                  "log-data: invalid/reused token", user_id=user_id, ip=ip)
        return jsonify({"success": False, "error": "Duplicate or invalid submission."}), 400
    session.pop("log_token", None)

    # ── 2. Basic input validation ──────────────────────────────────────────
    raw_mb = data.get("mb_saved")
    try:
        mb_saved = int(raw_mb)
    except (TypeError, ValueError):
        _reissue_log_token()
        return jsonify({"success": False, "error": "MB value must be a whole number.",
                        "next_token": session.get("log_token")}), 400

    if mb_saved < 1:
        _reissue_log_token()
        return jsonify({"success": False, "error": "Value must be at least 1 MB.",
                        "next_token": session.get("log_token")}), 400
    if mb_saved > DAILY_DATA_CAP_MB:
        _reissue_log_token()
        return jsonify({"success": False,
                        "error": f"Maximum is {DAILY_DATA_CAP_MB} MB per submission.",
                        "next_token": session.get("log_token")}), 400

    db    = get_db()
    today = date.today().isoformat()

    # ── 3. Daily cap check (server-authoritative) ──────────────────────────
    # Read the authoritative today_data_saved from the DB (not the client).
    today_before = _sync_today_data(db, user_id, today)
    remaining_mb = DAILY_DATA_CAP_MB - today_before

    if remaining_mb <= 0:
        # User already hit 2000 MB today — log the abuse attempt and reject.
        log_event(db, EVENT_DATA_CAP_HIT,
                  f"log-data: daily cap already reached (today={today_before} MB)",
                  user_id=user_id, ip=ip)
        _reissue_log_token()
        return jsonify({
            "success":          False,
            "error":            "Daily limit of 2000 MB reached! Try again tomorrow.",
            "daily_cap_hit":    True,
            "today_data_saved": today_before,
            "remaining_mb":     0,
            "next_token":       session.get("log_token"),
        }), 429

    if today_before + mb_saved > DAILY_DATA_CAP_MB:
        # Submission would overshoot the cap — tell user the exact headroom.
        _reissue_log_token()
        return jsonify({
            "success":       False,
            "error":         f"Only {remaining_mb} MB remaining for today. Reduce your entry.",
            "remaining_mb":  remaining_mb,
            "daily_cap_hit": False,
            "next_token":    session.get("log_token"),
        }), 400

    # ── 4. Minute-level duplicate guard ────────────────────────────────────
    # Prevents a burst of concurrent requests that all pass the token check
    # before the first write completes (e.g., two tabs submitting at once).
    recent_dup = db.execute(
        """SELECT id FROM transactions
           WHERE user_id = ?
             AND description LIKE 'Logged % MB saved'
             AND created_at > datetime('now', '-60 seconds')
           LIMIT 1""",
        (user_id,),
    ).fetchone()
    if recent_dup:
        log_event(db, EVENT_DATA_DUPE,
                  "log-data: submission within 60s of previous",
                  user_id=user_id, ip=ip)
        _reissue_log_token()
        return jsonify({
            "success":    False,
            "error":      "Please wait 60 seconds before logging again.",
            "next_token": session.get("log_token"),
        }), 429

    # ── 5. All checks passed — compute coins and persist ───────────────────
    today_after  = today_before + mb_saved
    coins_earned = math.floor(mb_saved / 100) * COINS_PER_100MB

    # Write today_saved + lifetime together; also stamp last_data_date so
    # the daily-reset logic in _sync_today_data works correctly next day.
    db.execute(
        """UPDATE users
           SET lifetime_data_saved = lifetime_data_saved + ?,
               today_data_saved    = ?,
               last_data_date      = ?
           WHERE id = ?""",
        (mb_saved, today_after, today, user_id),
    )

    new_balance = get_balance(db, user_id)
    if coins_earned > 0:
        new_balance = add_coins(db, user_id, coins_earned, f"Logged {mb_saved} MB saved")
    else:
        db.commit()

    # ── 6. Check daily target bonus ────────────────────────────────────────
    target_result = _check_target_bonus(db, user_id, today_after, today)
    if target_result.get("bonus", 0) > 0:
        new_balance = get_balance(db, user_id)

    # ── 7. Issue next token ────────────────────────────────────────────────
    _reissue_log_token()
    new_token = session["log_token"]

    user = get_user(user_id)
    daily_target     = user["daily_data_target"] if user["daily_data_target"] else 200
    remaining_after  = max(0, DAILY_DATA_CAP_MB - today_after)

    return jsonify({
        "success":          True,
        "new_coins":        coins_earned,
        "total_coins":      new_balance,
        "mb_saved":         mb_saved,
        "next_token":       new_token,
        "today_data_saved": today_after,
        "daily_target":     daily_target,
        "target_hit":       target_result.get("hit", False),
        "target_bonus":     target_result.get("bonus", 0),
        "target_pct":       min(100, round((today_after / daily_target) * 100)) if daily_target > 0 else 0,
        "daily_cap_hit":    today_after >= DAILY_DATA_CAP_MB,
        "remaining_mb":     remaining_after,
    })


# ── Routes: Set Daily Target ───────────────────────────────────────────────────
@app.route("/x/set-target", methods=["POST"])
@login_required
def api_set_target():
    data = request.get_json(silent=True) or {}
    try:
        target = int(data.get("target", 0))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Invalid target value."}), 400

    if target < 0 or target > MAX_DATA_TARGET_MB:
        return jsonify({"success": False, "error": f"Target must be 0–{MAX_DATA_TARGET_MB} MB."}), 400

    db = get_db()
    db.execute("UPDATE users SET daily_data_target=? WHERE id=?", (target, session["user_id"]))
    db.commit()
    return jsonify({"success": True, "target": target})


# ── Routes: Dashboard Stats (AJAX) ─────────────────────────────────────────────
@app.route("/x/dashboard-stats")
@login_required
def api_dashboard_stats():
    db    = get_db()
    uid   = session["user_id"]
    today = date.today().isoformat()

    today_data_saved = _sync_today_data(db, uid, today)
    user   = get_user(uid)
    coins  = get_balance(db, uid)

    ads_row = db.execute(
        "SELECT ads_watched FROM ad_rewards WHERE user_id=? AND reward_date=?",
        (uid, today),
    ).fetchone()
    ads_today = ads_row["ads_watched"] if ads_row else 0
    daily_target = user["daily_data_target"] if user["daily_data_target"] else 200

    return jsonify({
        "coins":              coins,
        "rupees":             round(coins / COINS_PER_RUPEE, 2),
        "today_data_saved":   today_data_saved,
        "lifetime_data_saved": user["lifetime_data_saved"],
        "streak":             user["streak"],
        "ads_today":          ads_today,
        "daily_data_target":  daily_target,
        "target_pct":         min(100, round((today_data_saved / daily_target) * 100)) if daily_target > 0 else 0,
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
    batch = get_batch_state()
    return render_template(
        "rewards.html",
        user=user,
        coins_per_ad=COINS_PER_AD,
        ads_today=ads_today,
        max_ads=MAX_ADS_PER_DAY,
        ad_duration=AD_DURATION_SECS,
        csrf_token=session.get("csrf_token", ""),
        ad_batch_size=AD_BATCH_SIZE,
        ad_batch_cooldown=AD_BATCH_COOLDOWN_SECS,
        batch_count=batch["batch_count"],
        in_cooldown=batch["in_cooldown"],
        cooldown_secs_left=batch["cooldown_secs_left"],
        admob_test_mode=ADMOB_TEST_MODE,
        rewarded_ad_unit_id=REWARDED_AD_UNIT_ID,
    )


def _get_client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "")


def _check_csrf():
    """Validate CSRF token on AJAX requests. Returns True if valid."""
    data = request.get_json(silent=True) or {}
    client_csrf = (data.get("csrf_token") or
                   request.headers.get("X-DC-CSRF", ""))
    return bool(client_csrf and client_csrf == session.get("csrf_token"))


@app.route("/x/ad/start", methods=["POST"])
@login_required
@rate_limited(is_api=True)
def api_ad_start():
    uid = session["user_id"]
    ip  = _get_client_ip()
    db  = get_db()

    if not _check_csrf():
        log_event(db, EVENT_SPAM_ATTEMPT, "Ad start: bad CSRF", user_id=uid, ip=ip)
        return jsonify({"ok": False, "error": "Invalid request."}), 403

    # ── Batch cooldown guard ───────────────────────────────────────────────
    in_cooldown, secs_left = check_cooldown()
    if in_cooldown:
        return jsonify({
            "ok":           False,
            "reason":       "batch_cooldown",
            "cooldown_secs": secs_left,
            "error":        f"Batch cooldown active. Please wait {secs_left}s.",
        }), 429

    today = date.today().isoformat()
    ads_row = db.execute(
        "SELECT ads_watched FROM ad_rewards WHERE user_id=? AND reward_date=?",
        (uid, today),
    ).fetchone()
    ads_today = ads_row["ads_watched"] if ads_row else 0

    if ads_today >= MAX_ADS_PER_DAY:
        log_event(db, EVENT_AD_DAILY_LIMIT,
                  f"Daily limit hit ({ads_today}/{MAX_ADS_PER_DAY})",
                  user_id=uid, ip=ip)
        return jsonify({"ok": False, "reason": "daily_limit",
                        "error": f"Daily limit of {MAX_ADS_PER_DAY} ads reached."}), 429

    watch_token = secrets.token_hex(24)
    session["ad_token"]      = watch_token
    session["ad_token_time"] = time.time()
    batch = get_batch_state()
    log_event(db, EVENT_AD_STARTED,
              f"Ad session started (batch {batch['batch_count']+1}/{AD_BATCH_SIZE})",
              user_id=uid, ip=ip)
    return jsonify({
        "ok":          True,
        "token":       watch_token,
        "duration":    AD_DURATION_SECS,
        "batch_pos":   batch["batch_count"] + 1,
        "batch_size":  AD_BATCH_SIZE,
    })


@app.route("/x/ad/complete", methods=["POST"])
@login_required
@rate_limited(is_api=True)
def api_ad_complete():
    data  = request.get_json(silent=True) or {}
    token = data.get("token", "")
    uid   = session["user_id"]
    ip    = _get_client_ip()
    db    = get_db()

    if not _check_csrf():
        log_event(db, EVENT_SPAM_ATTEMPT, "Ad complete: bad CSRF", user_id=uid, ip=ip)
        return jsonify({"ok": False, "error": "Invalid request."}), 403

    stored_token = session.get("ad_token")
    if not token or not stored_token or token != stored_token:
        log_event(db, EVENT_AD_DUPLICATE,
                  f"Bad/duplicate token on ad complete", user_id=uid, ip=ip)
        return jsonify({"ok": False, "error": "Invalid or expired session. Start a new ad."}), 400

    elapsed = time.time() - session.get("ad_token_time", 0)
    min_elapsed = AD_DURATION_SECS - 1   # 1s grace
    if elapsed < min_elapsed:
        log_event(db, EVENT_AD_TOO_FAST,
                  f"Too fast: {elapsed:.1f}s (min {min_elapsed}s)", user_id=uid, ip=ip)
        session.pop("ad_token", None)
        session.pop("ad_token_time", None)
        return jsonify({"ok": False, "error": "Ad not fully watched. Please watch the complete ad."}), 400

    # Consume the token immediately to prevent replay
    session.pop("ad_token", None)
    session.pop("ad_token_time", None)

    today = date.today().isoformat()
    ads_row = db.execute(
        "SELECT id, ads_watched FROM ad_rewards WHERE user_id=? AND reward_date=?",
        (uid, today),
    ).fetchone()
    ads_today = ads_row["ads_watched"] if ads_row else 0

    if ads_today >= MAX_ADS_PER_DAY:
        log_event(db, EVENT_AD_DAILY_LIMIT,
                  f"Tried to complete after daily limit", user_id=uid, ip=ip)
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
    new_ads_today = ads_today + 1
    remaining = max(0, MAX_ADS_PER_DAY - new_ads_today)

    # ── Batch cooldown tracking ────────────────────────────────────────────
    batch_result = admob_record_complete()

    log_event(db, EVENT_AD_COMPLETED,
              f"Reward granted ({elapsed:.1f}s elapsed) ads_today={new_ads_today} "
              f"batch={batch_result['ads_in_batch']}/{AD_BATCH_SIZE}"
              f"{' → cooldown' if batch_result['cooldown_triggered'] else ''}",
              user_id=uid, ip=ip)

    return jsonify({
        "ok":                  True,
        "new_coins":           COINS_PER_AD,
        "total_coins":         new_balance,
        "ads_today":           new_ads_today,
        "remaining":           remaining,
        "batch_pos":           batch_result["ads_in_batch"],
        "batch_size":          batch_result["batch_size"],
        "cooldown_triggered":  batch_result["cooldown_triggered"],
        "cooldown_secs":       batch_result["cooldown_secs"],
    })


@app.route("/x/ad/abort", methods=["POST"])
@login_required
def api_ad_abort():
    """Called by the client when an ad is interrupted (tab hidden, page unload)."""
    data   = request.get_json(silent=True) or {}
    token  = data.get("token", "")
    reason = data.get("reason", "unknown")[:64]
    uid    = session["user_id"]
    ip     = _get_client_ip()
    db     = get_db()

    stored = session.get("ad_token")
    if stored and token == stored:
        session.pop("ad_token", None)
        session.pop("ad_token_time", None)
        log_event(db, EVENT_AD_ABORTED,
                  f"Ad aborted: {reason}", user_id=uid, ip=ip)

    return jsonify({"ok": True})


# ── Routes: Redeem ─────────────────────────────────────────────────────────────
@app.route("/redeem", methods=["GET", "POST"])
@login_required
@rate_limited(redirect_to="redeem")
def redeem():
    db  = get_db()
    uid = session["user_id"]
    user = get_user(uid)
    ip   = _get_client_ip()

    # Check for existing active request BEFORE processing POST
    active = db.execute(
        "SELECT * FROM recharge_requests WHERE user_id=? AND status IN ('pending','processing') LIMIT 1",
        (uid,),
    ).fetchone()

    if request.method == "POST":
        # CSRF validation
        if request.form.get("csrf_token") != session.get("csrf_token"):
            flash("Security token mismatch. Please try again.", "error")
            return redirect(url_for("redeem"), 303)

        # Block duplicate requests
        if active:
            flash("You already have a pending or processing request. Wait for it to be resolved.", "error")
            log_event(db, EVENT_SPAM_ATTEMPT,
                      f"Tried to create duplicate request while {active['status']} exists",
                      user_id=uid, ip=ip)
            return redirect(url_for("redeem"), 303)

        amount_inr = request.form.get("amount_inr", "")
        phone      = request.form.get("phone", "").strip()
        operator   = request.form.get("operator", "").strip()

        # Validate pack
        try:
            amount_inr = int(amount_inr)
        except (TypeError, ValueError):
            flash("Invalid pack selection.", "error")
            return redirect(url_for("redeem"), 303)

        if amount_inr not in REDEEM_PACKS:
            flash("Invalid pack selection.", "error")
            return redirect(url_for("redeem"), 303)

        # Validate phone (exactly 10 digits)
        err = validate_phone(phone)
        if err:
            flash(err, "error")
            return redirect(url_for("redeem"), 303)

        # Validate operator
        if operator not in OPERATORS:
            flash("Please select a valid operator.", "error")
            return redirect(url_for("redeem"), 303)

        coins_required = REDEEM_PACKS[amount_inr]

        # Server-side balance check (before deduct, prevents race conditions)
        balance = get_balance(db, uid)
        if balance < coins_required:
            flash(f"Insufficient coins. You need {coins_required} but have {balance}.", "error")
            log_event(db, EVENT_SPAM_ATTEMPT,
                      f"Redeem attempt with insufficient funds: {balance}<{coins_required}",
                      user_id=uid, ip=ip)
            return redirect(url_for("redeem"), 303)

        # Deduct coins atomically via wallet service (never goes negative)
        try:
            deduct_coins(db, uid, coins_required, f"Recharge ₹{amount_inr} ({operator}) to {phone}")
        except InsufficientFundsError:
            flash("Insufficient coins. Please try again.", "error")
            return redirect(url_for("redeem"), 303)
        except WalletError as e:
            flash(str(e), "error")
            return redirect(url_for("redeem"), 303)

        # Store phone encoded, insert request
        enc_phone = encode_phone(phone)
        pack_label = f"₹{amount_inr} — {RECHARGE_PACK_INFO[amount_inr]['data']}"
        db.execute(
            """INSERT INTO recharge_requests
               (user_id, phone_number, operator, recharge_pack, recharge_amount, coins_used, status)
               VALUES (?,?,?,?,?,?,'pending')""",
            (uid, enc_phone, operator, pack_label, amount_inr, coins_required),
        )
        db.commit()

        log_event(db, EVENT_ADMIN_ACTION,
                  f"Recharge request created: ₹{amount_inr} to {phone} via {operator}",
                  user_id=uid, ip=ip)

        flash(f"₹{amount_inr} recharge request submitted for {phone} ({operator}). Processing within 24 hours.", "success")
        return redirect(url_for("my_requests"), 303)

    # GET — collect flash messages
    error = success = None
    for cat, msg in get_flashed_messages(with_categories=True):
        if cat == "error":
            error = msg
        elif cat == "success":
            success = msg

    # Pre-fill phone from account
    prefill_phone = decode_phone(user["phone_encrypted"])

    return render_template(
        "redeem.html",
        user=user,
        pack_info=RECHARGE_PACK_INFO,
        redeem_packs=REDEEM_PACKS,
        coins_per_rupee=COINS_PER_RUPEE,
        operators=OPERATORS,
        decode_phone=decode_phone,
        prefill_phone=prefill_phone,
        blocked_request=active,
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

    # Find any active (pending/processing) request for the banner
    active_request = next(
        (r for r in requests_list if r["status"] in ACTIVE_STATUSES), None
    )

    return render_template(
        "my_requests.html",
        user=user,
        requests=requests_list,
        active_request=active_request,
        decode_phone=decode_phone,
    )


# ── Routes: Admin ──────────────────────────────────────────────────────────────
@app.route("/admin")
@admin_required
def admin():
    db = get_db()

    users_list = db.execute(
        """SELECT id, name, phone_encrypted, coins, lifetime_data_saved,
                  streak, total_ads_watched, created_at, is_admin
           FROM users ORDER BY created_at DESC"""
    ).fetchall()

    all_requests = db.execute(
        """SELECT r.*, u.name AS user_name
           FROM recharge_requests r
           JOIN users u ON u.id = r.user_id
           ORDER BY r.created_at DESC"""
    ).fetchall()

    stats = db.execute(
        """SELECT
               COUNT(*)                        AS total_users,
               COALESCE(SUM(coins), 0)         AS total_coins,
               COALESCE(SUM(lifetime_data_saved), 0) AS total_data_mb,
               COALESCE(SUM(total_ads_watched), 0)   AS total_ads
           FROM users"""
    ).fetchone()

    req_stats = db.execute(
        """SELECT
               COUNT(*) AS total,
               COALESCE(SUM(CASE WHEN status='pending'    THEN 1 ELSE 0 END), 0) AS pending,
               COALESCE(SUM(CASE WHEN status='processing' THEN 1 ELSE 0 END), 0) AS processing,
               COALESCE(SUM(CASE WHEN status='completed'  THEN 1 ELSE 0 END), 0) AS completed,
               COALESCE(SUM(CASE WHEN status='failed'     THEN 1 ELSE 0 END), 0) AS failed,
               COALESCE(SUM(coins_used), 0) AS total_coins_redeemed
           FROM recharge_requests"""
    ).fetchone()

    return render_template(
        "admin.html",
        users=users_list,
        all_requests=all_requests,
        stats=stats,
        req_stats=req_stats,
        decode_phone=decode_phone,
    )


@app.route("/admin/request/<int:req_id>/<action>", methods=["POST"])
@admin_required
def admin_request_action(req_id, action):
    if action not in ("complete", "fail", "processing"):
        return redirect(url_for("admin"), 303)
    db = get_db()
    req = db.execute(
        "SELECT id, user_id, coins_used, status FROM recharge_requests WHERE id=?",
        (req_id,),
    ).fetchone()
    if not req:
        flash("Request not found.", "error")
        return redirect(url_for("admin"), 303)

    status = {"complete": "completed", "fail": "failed", "processing": "processing"}[action]

    # Refund coins when marking as failed (only if not already failed)
    if status == "failed" and req["status"] != "failed":
        try:
            add_coins(db, req["user_id"], req["coins_used"],
                      f"Refund for failed recharge request #{req_id}")
        except WalletError:
            pass  # user may be deleted; log and continue

    db.execute("UPDATE recharge_requests SET status=? WHERE id=?", (status, req_id))
    db.commit()
    log_event(db, EVENT_ADMIN_ACTION,
              f"Request {req_id} marked {status}",
              user_id=session.get("user_id"), ip=_get_client_ip())
    return redirect(url_for("admin"), 303)


@app.route("/x/admin/status", methods=["POST"])
@admin_required
def admin_status_ajax():
    """AJAX status update — returns JSON so the admin page updates without reload."""
    data       = request.get_json(silent=True) or {}
    req_id     = data.get("req_id")
    new_status = data.get("status", "")
    valid      = ("pending", "processing", "completed", "failed")
    if not req_id or new_status not in valid:
        return jsonify({"ok": False, "error": "Invalid parameters"}), 400
    db = get_db()
    row = db.execute(
        "SELECT id, user_id, coins_used, status FROM recharge_requests WHERE id=?",
        (req_id,),
    ).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Request not found"}), 404

    # Refund coins when marking as failed (only if not already failed)
    if new_status == "failed" and row["status"] != "failed":
        try:
            add_coins(db, row["user_id"], row["coins_used"],
                      f"Refund for failed recharge request #{req_id}")
        except WalletError:
            pass  # user may be deleted; continue with status update

    db.execute("UPDATE recharge_requests SET status=? WHERE id=?", (new_status, req_id))
    db.commit()
    log_event(db, EVENT_ADMIN_ACTION,
              f"AJAX status update: #{req_id} → {new_status}",
              user_id=session.get("user_id"), ip=_get_client_ip())
    return jsonify({"ok": True, "req_id": req_id, "status": new_status})


@app.route("/admin/export/pending.csv")
@admin_required
def admin_export_pending():
    """Download pending requests as CSV — filename includes today's date."""
    import csv, io
    db   = get_db()
    rows = db.execute(
        """SELECT r.id, u.name AS user_name, r.phone_number, r.operator,
                  r.recharge_pack, r.recharge_amount, r.coins_used, r.created_at
           FROM recharge_requests r
           JOIN users u ON u.id = r.user_id
           WHERE r.status = 'pending'
           ORDER BY r.created_at DESC"""
    ).fetchall()

    buf = io.StringIO()
    w   = csv.writer(buf)
    w.writerow(["ID", "User", "Phone", "Operator", "Pack", "Amount (INR)", "Coins Used", "Submitted At"])
    for r in rows:
        w.writerow([
            r["id"],
            r["user_name"],
            decode_phone(r["phone_number"]),
            r["operator"],
            r["recharge_pack"],
            r["recharge_amount"],
            r["coins_used"],
            r["created_at"][:16].replace("T", " "),
        ])

    filename = f"pending_requests_{date.today().isoformat().replace('-', '_')}.csv"
    return app.response_class(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ── Template context helpers ───────────────────────────────────────────────────
@app.context_processor
def inject_helpers():
    def get_user_coins():
        if "user_id" not in session:
            return 0
        row = get_db().execute("SELECT coins FROM users WHERE id=?", (session["user_id"],)).fetchone()
        return row["coins"] if row else 0

    # Per-session CSRF token (regenerated on new session)
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(24)

    return {
        "get_user_coins": get_user_coins,
        "csrf_token": session.get("csrf_token", ""),
    }


if __name__ == "__main__":
    init_db(app)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
