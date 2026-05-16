"""
Security logging service — writes tamper-evident audit events to the DB.
All reward attempts, login failures, and admin actions are tracked here.
"""
from datetime import datetime


EVENT_LOGIN_SUCCESS   = "login_success"
EVENT_LOGIN_FAILED    = "login_failed"
EVENT_AD_STARTED      = "ad_started"
EVENT_AD_COMPLETED    = "ad_completed"
EVENT_AD_ABORTED      = "ad_aborted"
EVENT_AD_DUPLICATE    = "ad_duplicate"
EVENT_AD_TOO_FAST     = "ad_too_fast"
EVENT_AD_DAILY_LIMIT  = "ad_daily_limit"
EVENT_ADMIN_ACTION    = "admin_action"
EVENT_RATE_LIMITED    = "rate_limited"
EVENT_SPAM_ATTEMPT    = "spam_attempt"
EVENT_INVALID_TOKEN   = "invalid_token"


def log_event(db, event_type: str, details: str = "",
              user_id: int | None = None, ip: str = "") -> None:
    """Write a security event to the audit log. Never raises — fails silently."""
    try:
        db.execute(
            """INSERT INTO security_logs
               (user_id, event_type, details, ip_address, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, event_type, details[:1024], ip[:64],
             datetime.utcnow().isoformat(sep=" ", timespec="seconds")),
        )
        db.commit()
    except Exception:
        pass


def get_recent_events(db, user_id: int | None = None,
                      event_type: str | None = None,
                      limit: int = 50) -> list:
    """Fetch recent events for admin inspection."""
    clauses, params = [], []
    if user_id is not None:
        clauses.append("user_id = ?")
        params.append(user_id)
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    return db.execute(
        f"SELECT * FROM security_logs {where} ORDER BY created_at DESC LIMIT ?",
        params,
    ).fetchall()
