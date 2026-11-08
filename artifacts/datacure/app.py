import os
import math
import secrets
import time
import hashlib
import datetime
from datetime import date, timedelta
from functools import wraps

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    jsonify,
    g,
    flash,
    get_flashed_messages,
)
from werkzeug.security import generate_password_hash, check_password_hash

from database.db import get_db, init_db
from services.streak import update_streak, STREAK_TARGET, STREAK_BONUS_COINS
from services.ratelimit import check_rate_limit, rl_key
from services.wallet import (
    add_coins,
    deduct_coins,
    get_balance,
    WalletError,
    InsufficientFundsError,
)
from services.auth import (
    encode_phone,
    decode_phone,
    detect_operator,
    validate_name,
    validate_phone,
    validate_password,
    validate_user_id,
)
from services.security import (
    log_event,
    EVENT_LOGIN_SUCCESS,
    EVENT_LOGIN_FAILED,
    EVENT_AD_STARTED,
    EVENT_AD_COMPLETED,
    EVENT_AD_ABORTED,
    EVENT_AD_DUPLICATE,
    EVENT_AD_TOO_FAST,
    EVENT_AD_DAILY_LIMIT,
    EVENT_ADMIN_ACTION,
    EVENT_RATE_LIMITED,
    EVENT_SPAM_ATTEMPT,
    EVENT_INVALID_TOKEN,
    EVENT_DATA_CAP_HIT,
    EVENT_DATA_DUPE,
)

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "datacure-dev-secret-2024")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=3650)


@app.before_request
def make_session_permanent():
    session.permanent = True


COINS_PER_100MB = 5
COINS_PER_AD = 10
COINS_PER_RUPEE = 100
MAX_ADS_PER_DAY = 15
MB_PER_REWARD = 100
AD_BATCH_SIZE = 5
AD_BATCH_COOLDOWN_SECS = 60
AD_DURATION_SECS = 30
FIXED_OTP = "1234"
REDEEM_PACKS_LIST = [
    {
        "id": "jio_1gb_1d",
        "name": "Jio 1GB Booster",
        "operator": "Jio",
        "data": "1 GB",
        "validity": "1 Day",
        "coins": 2250,
    },
    {
        "id": "airtel_1gb_1d",
        "name": "Airtel 1GB Booster",
        "operator": "Airtel",
        "data": "1 GB",
        "validity": "1 Day",
        "coins": 2250,
    },
    {
        "id": "vi_1gb_1d",
        "name": "Vi 1GB Booster",
        "operator": "Vi",
        "data": "1 GB",
        "validity": "1 Day",
        "coins": 2250,
    },
    {
        "id": "jio_2gb_2d",
        "name": "Jio 2GB Booster",
        "operator": "Jio",
        "data": "2 GB",
        "validity": "2 Days",
        "coins": 3200,
    },
    {
        "id": "vi_6gb_2d",
        "name": "Vi 6GB Booster",
        "operator": "Vi",
        "data": "6 GB",
        "validity": "2 Days",
        "coins": 8250,
    },
    {
        "id": "bsnl_ul_2d",
        "name": "BSNL Unlimited + 1GB/Day",
        "operator": "BSNL",
        "data": "Unlimited+1GB/Day",
        "validity": "2 Days",
        "coins": 2250,
    },
    {
        "id": "airtel_5gb_7d",
        "name": "Airtel 5GB Booster",
        "operator": "Airtel",
        "data": "5 GB",
        "validity": "7 Days",
        "coins": 8500,
    },
    {
        "id": "bsnl_ul_7d",
        "name": "BSNL Unlimited + 1GB/Day",
        "operator": "BSNL",
        "data": "Unlimited+1GB/Day",
        "validity": "7 Days",
        "coins": 6500,
    },
]
REDEEM_PACKS_BY_ID = {p["id"]: p for p in REDEEM_PACKS_LIST}
OPERATORS = ["Jio", "Airtel", "Vi", "BSNL"]
ACTIVE_STATUSES = ("pending", "processing")
SECRET_ADMIN_PASSWORD = "datacure-ops-2024"
SECRET_ADMIN_SESSION_KEY = "secret_admin_authed"
TARGET_BONUS_COINS = 20
MAX_DATA_TARGET_MB = 500
DAILY_DATA_CAP_MB = 500


# ── Referral Helper Functions ──────────────────────────────────────────────────
def generate_secure_ref_code(user_id):
    salt = "DatacureSecureSalt2026_NexoraByte"
    raw = f"{user_id}-{salt}"
    return "DC" + hashlib.sha256(raw.encode()).hexdigest()[:6].upper()


def process_user_milestone_completion(db, user_id, earned_coins):
    """
    Triggers when User B earns coins or completes a BitLabs task.
    Unlocks 500 coins for User A if User B crosses the threshold.
    """
    row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        return
    user = dict(row)

    if user.get("referral_status") == "PENDING_MILESTONE":
        total_earned_check = db.execute(
            "SELECT COALESCE(SUM(amount), 0) as total FROM transactions WHERE user_id=? AND amount > 0",
            (user_id,),
        ).fetchone()

        current_earned = (
            total_earned_check["total"] if total_earned_check else earned_coins
        )

        if current_earned >= 200:
            db.execute(
                "UPDATE users SET referral_status = 'COMPLETED' WHERE id=?", (user_id,)
            )

            referrer_id = user.get("referred_by")
            if referrer_id:
                add_coins(
                    db,
                    referrer_id,
                    500,
                    "Referral bonus: Friend completed task milestone!",
                )
                db.execute(
                    "INSERT INTO transactions (user_id, type, amount, description) VALUES (?, 'referral_bonus', 500, ?)",
                    (referrer_id, f"Bonus for inviting user #{user_id}"),
                )
            db.commit()


# ── Ad Batch & Cooldown Helper Functions ───────────────────────────────────────
def get_batch_state():
    return {
        "batch_count": session.get("batch_count", 0),
        "in_cooldown": session.get("in_cooldown", False),
        "cooldown_secs_left": session.get("cooldown_secs_left", 0),
    }


def check_cooldown():
    in_cooldown = session.get("in_cooldown", False)
    secs_left = session.get("cooldown_secs_left", 0)
    return in_cooldown, secs_left


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
        user = get_user(session["user_id"])
        if not user:
            session.clear()
            return redirect(url_for("login"), 303)
        return f(*args, **kwargs)

    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"), 303)
        user = get_user(session["user_id"])
        if not user or not user.get("is_admin"):
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
                dest = (
                    url_for(redirect_to)
                    if redirect_to
                    else (request.referrer or url_for("dashboard"))
                )
                return redirect(dest, 303)
            return f(*args, **kwargs)

        return decorated

    return decorator


# ── Helpers ────────────────────────────────────────────────────────────────────
def get_user(user_id):
    row = get_db().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_flash_error():
    for cat, msg in get_flashed_messages(with_categories=True):
        if cat == "error":
            return msg
    return None


def _sync_today_data(db, user_id: int, today: str) -> int:
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
    row = db.execute(
        "SELECT daily_data_target, target_bonus_date FROM users WHERE id=?", (user_id,)
    ).fetchone()
    target = row["daily_data_target"] or 0
    if target <= 0 or today_saved < target:
        return {"hit": False, "bonus": 0, "target": target}
    if row["target_bonus_date"] == today:
        return {"hit": True, "bonus": 0, "target": target, "already": True}
    add_coins(
        db, user_id, TARGET_BONUS_COINS, f"Daily target bonus — {target} MB reached!"
    )
    db.execute("UPDATE users SET target_bonus_date=? WHERE id=?", (today, user_id))
    db.commit()
    return {"hit": True, "bonus": TARGET_BONUS_COINS, "target": target}


def _reissue_log_token():
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
        uid_input = request.form.get("user_id", "").strip()
        password = request.form.get("password", "").strip()
        incoming_ref = request.form.get("referral_code", "").strip()

        err = validate_user_id(uid_input) or validate_password(password)
        if err:
            flash(err, "error")
            return redirect(url_for("register"), 303)

        enc_uid = encode_phone(uid_input)
        db = get_db()
        if db.execute(
            "SELECT id FROM users WHERE phone_encrypted=?", (enc_uid,)
        ).fetchone():
            flash("This User ID is already taken. Try a different one.", "error")
            return redirect(url_for("register"), 303)

        referrer_id = None
        if incoming_ref:
            ref_row = db.execute(
                "SELECT id FROM users WHERE ref_code=?", (incoming_ref,)
            ).fetchone()
            if ref_row:
                referrer_id = ref_row["id"]

        pw_hash = generate_password_hash(password)

        cursor = db.cursor()
        cursor.execute(
            "INSERT INTO users (name, phone_encrypted, password_hash, coins, referred_by, referral_status) VALUES (?,?,?,0,?,?)",
            (uid_input, enc_uid, pw_hash, referrer_id, "PENDING_MILESTONE"),
        )
        db.commit()

        user_row = db.execute(
            "SELECT id FROM users WHERE phone_encrypted=?", (enc_uid,)
        ).fetchone()
        new_id = user_row["id"]

        my_ref_code = generate_secure_ref_code(new_id)
        db.execute("UPDATE users SET ref_code=? WHERE id=?", (my_ref_code, new_id))
        db.commit()

        session.clear()
        session.permanent = True
        session["user_id"] = new_id
        return redirect(url_for("dashboard"), 303)

    return render_template("register.html", error=get_flash_error())


# ── Routes: Login ──────────────────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
@rate_limited(redirect_to="login")
def login():
    if "user_id" in session:
        return redirect(url_for("dashboard"), 303)

    if request.method == "POST":
        uid_input = request.form.get("user_id", "").strip()
        password = request.form.get("password", "").strip()

        err = validate_user_id(uid_input)
        if err:
            flash(err, "error")
            return redirect(url_for("login"), 303)

        enc_uid = encode_phone(uid_input)
        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE phone_encrypted=?", (enc_uid,)
        ).fetchone()

        ip = request.remote_addr or ""
        if not user:
            log_event(
                get_db(),
                EVENT_LOGIN_FAILED,
                "Unknown User ID attempted login",
                user_id=None,
                ip=ip,
            )
            flash("No account found with this User ID.", "error")
            return redirect(url_for("login"), 303)
        if not check_password_hash(user["password_hash"], password):
            log_event(
                get_db(),
                EVENT_LOGIN_FAILED,
                f"Wrong password for user {user['id']}",
                user_id=user["id"],
                ip=ip,
            )
            flash("Incorrect password.", "error")
            return redirect(url_for("login"), 303)

        log_event(get_db(), EVENT_LOGIN_SUCCESS, "Login OK", user_id=user["id"], ip=ip)
        session.clear()
        session.permanent = True
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
    db = get_db()
    uid = session["user_id"]
    today = date.today().isoformat()

    user = get_user(uid)
    if not user:
        session.clear()
        return redirect(url_for("login"), 303)

    streak_info = update_streak(db, uid)
    today_data_saved = _sync_today_data(db, uid, today)

    ads_row = db.execute(
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

    invited_friends = db.execute(
        "SELECT name, referral_status, created_at FROM users WHERE referred_by=? ORDER BY created_at DESC",
        (uid,),
    ).fetchall()

    coins = get_balance(db, uid)
    rupees_balance = coins / COINS_PER_RUPEE

    daily_target = user.get("daily_data_target") or 200
    target_pct = (
        min(100, round((today_data_saved / daily_target) * 100))
        if daily_target > 0
        else 0
    )

    return render_template(
        "dashboard.html",
        user=user,
        ads_today=ads_today,
        max_ads=MAX_ADS_PER_DAY,
        recent=recent,
        redemptions=redemptions,
        invited_friends=invited_friends,
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


# ── Routes: Requests / History Page ────────────────────────────────────────────
@app.route("/requests")
@login_required
def requests_page():
    db = get_db()
    uid = session["user_id"]
    user = get_user(uid)
    if not user:
        session.clear()
        return redirect(url_for("login"), 303)

    requests_list = db.execute(
        "SELECT * FROM recharge_requests WHERE user_id=? ORDER BY created_at DESC",
        (uid,),
    ).fetchall()

    active_req = db.execute(
        "SELECT * FROM recharge_requests WHERE user_id=? AND status IN ('pending', 'processing') ORDER BY created_at DESC LIMIT 1",
        (uid,),
    ).fetchone()

    invited_friends = db.execute(
        "SELECT name, referral_status, created_at FROM users WHERE referred_by=? ORDER BY created_at DESC",
        (uid,),
    ).fetchall()

    return render_template(
        "my_requests.html",
        user=user,
        requests=requests_list,
        active_request=active_req,
        invited_friends=invited_friends,
        decode_phone=decode_phone,
    )


# ── Routes: BitLabs Webhook Callback (Server-to-Server) ───────────────────────
@app.route("/x/bitlabs/callback", methods=["GET", "POST"])
def bitlabs_callback():
    data = (
        request.args
        if request.method == "GET"
        else (request.get_json(silent=True) or request.form)
    )

    uid = data.get("uid") or data.get("user_id")
    reward = data.get("reward") or data.get("coins")

    if not uid or not reward:
        return jsonify({"success": False, "error": "Missing parameters"}), 400

    try:
        user_id = int(uid)
        earned_coins = int(float(reward))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Invalid types"}), 400

    db = get_db()
    user = get_user(user_id)
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 404

    add_coins(db, user_id, earned_coins, "BitLabs Offerwall Reward")
    process_user_milestone_completion(db, user_id, earned_coins)

    return jsonify({"success": True, "message": "Callback processed successfully"})


# ── Routes: Log Data ───────────────────────────────────────────────────────────
@app.route("/log-data", methods=["GET"])
@login_required
def log_data():
    token = secrets.token_hex(16)
    session["log_token"] = token
    db = get_db()
    uid = session["user_id"]
    today = date.today().isoformat()
    today_data_saved = _sync_today_data(db, uid, today)
    user = get_user(uid)

    if not user:
        session.clear()
        return redirect(url_for("login"), 303)

    daily_target = user.get("daily_data_target") or 200
    target_pct = (
        min(100, round((today_data_saved / daily_target) * 100))
        if daily_target > 0
        else 0
    )
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
    data = request.get_json(silent=True) or {}
    token = data.get("token", "")
    user_id = session["user_id"]
    ip = request.remote_addr or "0.0.0.0"

    if not token or token != session.get("log_token"):
        log_event(
            get_db(),
            EVENT_INVALID_TOKEN,
            "log-data: invalid/reused token",
            user_id=user_id,
            ip=ip,
        )
        return jsonify(
            {"success": False, "error": "Duplicate or invalid submission."}
        ), 400
    session.pop("log_token", None)

    raw_mb = data.get("mb_saved", 0)
    try:
        mb_saved = int(raw_mb or 0)
    except (TypeError, ValueError):
        _reissue_log_token()
        return jsonify(
            {
                "success": False,
                "error": "MB value must be a whole number.",
                "next_token": session.get("log_token"),
            }
        ), 400

    if mb_saved < 1:
        _reissue_log_token()
        return jsonify(
            {
                "success": False,
                "error": "Value must be at least 1 MB.",
                "next_token": session.get("log_token"),
            }
        ), 400
    if mb_saved > DAILY_DATA_CAP_MB:
        _reissue_log_token()
        return jsonify(
            {
                "success": False,
                "error": f"Maximum is {DAILY_DATA_CAP_MB} MB per submission.",
                "next_token": session.get("log_token"),
            }
        ), 400

    db = get_db()
    today = date.today().isoformat()
    today_before = _sync_today_data(db, user_id, today)
    remaining_mb = DAILY_DATA_CAP_MB - today_before

    if remaining_mb <= 0:
        log_event(
            db,
            EVENT_DATA_CAP_HIT,
            f"log-data: daily cap already reached (today={today_before} MB)",
            user_id=user_id,
            ip=ip,
        )
        _reissue_log_token()
        return jsonify(
            {
                "success": False,
                "error": "Daily limit reached! Try again tomorrow.",
                "daily_cap_hit": True,
                "today_data_saved": today_before,
                "remaining_mb": 0,
                "next_token": session.get("log_token"),
            }
        ), 429

    if today_before + mb_saved > DAILY_DATA_CAP_MB:
        _reissue_log_token()
        return jsonify(
            {
                "success": False,
                "error": f"Only {remaining_mb} MB remaining for today. Reduce your entry.",
                "remaining_mb": remaining_mb,
                "daily_cap_hit": False,
                "next_token": session.get("log_token"),
            }
        ), 400

    sixty_sec_ago = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=60)
    ).strftime("%Y-%m-%d %H:%M:%S")

    recent_dup = db.execute(
        """SELECT id FROM transactions 
           WHERE user_id = ? 
           AND type = 'data_earn' 
           AND created_at > ?""",
        (user_id, sixty_sec_ago),
    ).fetchone()
    if recent_dup:
        log_event(
            db,
            EVENT_DATA_DUPE,
            "log-data: submission within 60s of previous",
            user_id=user_id,
            ip=ip,
        )
        _reissue_log_token()
        return jsonify(
            {
                "success": False,
                "error": "Please wait 60 seconds before logging again.",
                "next_token": session.get("log_token"),
            }
        ), 429

    today_after = today_before + mb_saved
    coins_earned = math.floor(mb_saved / 100) * COINS_PER_100MB

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
        new_balance = add_coins(
            db, user_id, coins_earned, f"Logged {mb_saved} MB saved"
        )
    else:
        db.commit()

    target_result = _check_target_bonus(db, user_id, today_after, today)
    if target_result.get("bonus", 0) > 0:
        new_balance = get_balance(db, user_id)

    _reissue_log_token()
    new_token = session["log_token"]

    user = get_user(user_id)
    daily_target = user["daily_data_target"] if user["daily_data_target"] else 200
    remaining_after = max(0, DAILY_DATA_CAP_MB - today_after)

    return jsonify(
        {
            "success": True,
            "new_coins": coins_earned,
            "total_coins": new_balance,
            "mb_saved": mb_saved,
            "next_token": new_token,
            "today_data_saved": today_after,
            "daily_target": daily_target,
            "target_hit": target_result.get("hit", False),
            "target_bonus": target_result.get("bonus", 0),
            "target_pct": min(100, round((today_after / daily_target) * 100))
            if daily_target > 0
            else 0,
            "daily_cap_hit": today_after >= DAILY_DATA_CAP_MB,
            "remaining_mb": remaining_after,
        }
    )


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
        return jsonify(
            {"success": False, "error": f"Target must be 0–{MAX_DATA_TARGET_MB} MB."}
        ), 400

    db = get_db()
    db.execute(
        "UPDATE users SET daily_data_target=? WHERE id=?", (target, session["user_id"])
    )
    db.commit()
    return jsonify({"success": True, "target": target})


# ── Routes: Dashboard Stats (AJAX) ─────────────────────────────────────────────
@app.route("/x/dashboard-stats")
@login_required
def api_dashboard_stats():
    db = get_db()
    uid = session["user_id"]
    today = date.today().isoformat()

    today_data_saved = _sync_today_data(db, uid, today)
    user = get_user(uid)

    if not user:
        return jsonify({"success": False, "error": "User session invalid."}), 401

    coins = get_balance(db, uid)

    ads_row = db.execute(
        "SELECT ads_watched FROM ad_rewards WHERE user_id=? AND reward_date=?",
        (uid, today),
    ).fetchone()
    ads_today = ads_row["ads_watched"] if ads_row else 0

    daily_target = user.get("daily_data_target") or 200

    return jsonify(
        {
            "coins": coins,
            "rupees": round(coins / COINS_PER_RUPEE, 2),
            "today_data_saved": today_data_saved,
            "lifetime_data_saved": user.get("lifetime_data_saved", 0),
            "streak": user.get("streak", 0),
            "ads_today": ads_today,
            "daily_data_target": daily_target,
            "target_pct": min(100, round((today_data_saved / daily_target) * 100))
            if daily_target > 0
            else 0,
        }
    )


# ── Routes: Ad Rewards ─────────────────────────────────────────────────────────
@app.route("/rewards")
@login_required
def rewards():
    db = get_db()
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
    )


def _get_client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "")


def _check_csrf():
    data = request.get_json(silent=True) or {}
    client_csrf = data.get("csrf_token") or request.headers.get("X-DC-CSRF", "")
    return bool(client_csrf and client_csrf == session.get("csrf_token"))


@app.route("/x/ad/start", methods=["POST"])
@login_required
@rate_limited(is_api=True)
def api_ad_start():
    uid = session["user_id"]
    ip = _get_client_ip()
    db = get_db()

    if not _check_csrf():
        log_event(db, EVENT_SPAM_ATTEMPT, "Ad start: bad CSRF", user_id=uid, ip=ip)
        return jsonify({"ok": False, "error": "Invalid request."}), 403

    in_cooldown, secs_left = check_cooldown()
    if in_cooldown:
        return jsonify(
            {
                "ok": False,
                "reason": "batch_cooldown",
                "cooldown_secs": secs_left,
                "error": f"Batch cooldown active. Please wait {secs_left}s.",
            }
        ), 429

    today = date.today().isoformat()
    ads_row = db.execute(
        "SELECT ads_watched FROM ad_rewards WHERE user_id=? AND reward_date=?",
        (uid, today),
    ).fetchone()
    ads_today = ads_row["ads_watched"] if ads_row else 0

    if ads_today >= MAX_ADS_PER_DAY:
        log_event(
            db,
            EVENT_AD_DAILY_LIMIT,
            f"Daily limit hit ({ads_today}/{MAX_ADS_PER_DAY})",
            user_id=uid,
            ip=ip,
        )
        return jsonify(
            {
                "ok": False,
                "reason": "daily_limit",
                "error": f"Daily limit of {MAX_ADS_PER_DAY} ads reached.",
            }
        ), 429

    watch_token = secrets.token_hex(24)
    session["ad_token"] = watch_token
    session["ad_token_time"] = time.time()
    batch = get_batch_state()
    log_event(
        db,
        EVENT_AD_STARTED,
        f"Ad session started (batch {batch['batch_count'] + 1}/{AD_BATCH_SIZE})",
        user_id=uid,
        ip=ip,
    )
    return jsonify(
        {
            "ok": True,
            "token": watch_token,
            "duration": AD_DURATION_SECS,
            "batch_pos": batch["batch_count"] + 1,
            "batch_size": AD_BATCH_SIZE,
        }
    )


@app.route("/x/ad/complete", methods=["POST"])
@login_required
@rate_limited(is_api=True)
def api_ad_complete():
    data = request.get_json(silent=True) or {}
    token = data.get("token", "")
    uid = session["user_id"]
    ip = _get_client_ip()
    db = get_db()

    if not _check_csrf():
        log_event(db, EVENT_SPAM_ATTEMPT, "Ad complete: bad CSRF", user_id=uid, ip=ip)
        return jsonify({"ok": False, "error": "Invalid request."}), 403

    stored_token = session.get("ad_token")
    if not token or not stored_token or token != stored_token:
        log_event(
            db,
            EVENT_AD_DUPLICATE,
            "Bad/duplicate token on ad complete",
            user_id=uid,
            ip=ip,
        )
        return jsonify(
            {"ok": False, "error": "Invalid or expired session. Start a new ad."}
        ), 400

    elapsed = time.time() - session.get("ad_token_time", 0)
    min_elapsed = AD_DURATION_SECS - 1
    if elapsed < min_elapsed:
        log_event(
            db,
            EVENT_AD_TOO_FAST,
            f"Too fast: {elapsed:.1f}s (min {min_elapsed}s)",
            user_id=uid,
            ip=ip,
        )
        session.pop("ad_token", None)
        session.pop("ad_token_time", None)
        return jsonify(
            {
                "ok": False,
                "error": "Ad not fully watched. Please watch the complete ad.",
            }
        ), 400

    session.pop("ad_token", None)
    session.pop("ad_token_time", None)

    today = date.today().isoformat()
    ads_row = db.execute(
        "SELECT id, ads_watched FROM ad_rewards WHERE user_id=? AND reward_date=?",
        (uid, today),
    ).fetchone()
    ads_today = ads_row["ads_watched"] if ads_row else 0

    if ads_today >= MAX_ADS_PER_DAY:
        log_event(
            db,
            EVENT_AD_DAILY_LIMIT,
            "Tried to complete after daily limit",
            user_id=uid,
            ip=ip,
        )
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

    db.execute(
        "UPDATE users SET total_ads_watched = total_ads_watched + 1 WHERE id=?", (uid,)
    )

    new_balance = add_coins(db, uid, COINS_PER_AD, "Watched reward ad")
    process_user_milestone_completion(db, uid, COINS_PER_AD)

    cursor = db.cursor()
    cursor.execute("SELECT coins, ads_watched FROM users WHERE id = ?", (uid,))
    user_row = cursor.fetchone()
    new_balance = user_row["coins"]
    new_ads_today = user_row["ads_watched"]

    remaining = max(0, MAX_ADS_PER_DAY - new_ads_today)
    batch_result = {
        "ads_in_batch": new_ads_today % AD_BATCH_SIZE,
        "batch_size": AD_BATCH_SIZE,
        "cooldown_triggered": False,
        "cooldown_secs": 0,
    }

    log_event(
        db,
        EVENT_AD_COMPLETED,
        f"Reward granted ({elapsed:.1f}s elapsed) ads_today={new_ads_today}",
        user_id=uid,
        ip=ip,
    )

    return jsonify(
        {
            "ok": True,
            "new_coins": COINS_PER_AD,
            "total_coins": new_balance,
            "ads_today": new_ads_today,
            "remaining": remaining,
            "batch_pos": batch_result["ads_in_batch"],
            "batch_size": batch_result["batch_size"],
            "cooldown_triggered": batch_result["cooldown_triggered"],
            "cooldown_secs": batch_result["cooldown_secs"],
        }
    )


@app.route("/x/ad/abort", methods=["POST"])
@login_required
def api_ad_abort():
    data = request.get_json(silent=True) or {}
    token = data.get("token", "")
    uid = session["user_id"]
    ip = _get_client_ip()
    db = get_db()

    stored = session.get("ad_token")
    if stored and token == stored:
        session.pop("ad_token", None)
        session.pop("ad_token_time", None)
        log_event(db, EVENT_AD_ABORTED, "Ad aborted", user_id=uid, ip=ip)
        return jsonify({"ok": True})
    return jsonify({"ok": False})
