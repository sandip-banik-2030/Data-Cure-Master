from datetime import date, timedelta

STREAK_TARGET = 7
STREAK_BONUS_COINS = 20


def update_streak(db, user_id: int) -> dict:
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    user = db.execute(
        "SELECT streak, last_active_date, streak_bonus_date FROM users WHERE id=?",
        (user_id,),
    ).fetchone()

    # ✅ ADDED SAFETY CHECK: If user is missing in DB, return 0 streak safely!
    if not user:
        return _result(0, False, False, 0)

    streak = user["streak"] or 0
    last = user["last_active_date"]

    if last == today:
        return _result(streak, False, False, 0)

    streak = streak + 1 if last == yesterday else 1

    db.execute(
        "UPDATE users SET streak=?, last_active_date=? WHERE id=?",
        (streak, today, user_id),
    )

    bonus_granted = False
    bonus_coins = 0
    if user["streak_bonus_date"] != today:
        from services.wallet import add_coins

        add_coins(db, user_id, STREAK_BONUS_COINS, f"Day-{streak} streak bonus")
        db.execute(
            "UPDATE users SET streak_bonus_date=? WHERE id=?",
            (today, user_id),
        )
        bonus_granted = True
        bonus_coins = STREAK_BONUS_COINS

    db.commit()
    return _result(streak, True, bonus_granted, bonus_coins)


def _result(streak, is_new_day, bonus_granted, bonus_coins):
    progress = (
        int((streak % STREAK_TARGET) / STREAK_TARGET * 100) if STREAK_TARGET else 0
    )
    days_to_target = (
        STREAK_TARGET - (streak % STREAK_TARGET)
        if streak % STREAK_TARGET != 0
        else STREAK_TARGET
    )
    return {
        "streak": streak,
        "is_new_day": is_new_day,
        "bonus_granted": bonus_granted,
        "bonus_coins": bonus_coins,
        "bonus_coins_per_day": STREAK_BONUS_COINS,
        "progress": progress,
        "days_to_target": days_to_target,
        "target": STREAK_TARGET,
    }
