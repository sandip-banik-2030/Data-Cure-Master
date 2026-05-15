class WalletError(Exception):
    pass

class InsufficientFundsError(WalletError):
    pass

class InvalidAmountError(WalletError):
    pass

ALLOWED_TYPES = {"credit", "debit"}


def get_balance(db, user_id: int) -> int:
    row = db.execute("SELECT coins FROM users WHERE id=?", (user_id,)).fetchone()
    return row["coins"] if row else 0


def add_coins(db, user_id: int, amount: int, description: str) -> int:
    if not isinstance(amount, int) or amount <= 0:
        raise InvalidAmountError("Amount must be a positive integer.")
    row = db.execute("SELECT coins FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None:
        raise WalletError(f"User {user_id} not found.")
    db.execute("UPDATE users SET coins = coins + ? WHERE id=?", (amount, user_id))
    db.execute(
        "INSERT INTO transactions (user_id, transaction_type, amount, description) VALUES (?,?,?,?)",
        (user_id, "credit", amount, description),
    )
    db.commit()
    return row["coins"] + amount


def deduct_coins(db, user_id: int, amount: int, description: str) -> int:
    if not isinstance(amount, int) or amount <= 0:
        raise InvalidAmountError("Amount must be a positive integer.")
    row = db.execute("SELECT coins FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None:
        raise WalletError(f"User {user_id} not found.")
    if row["coins"] < amount:
        raise InsufficientFundsError("Insufficient coins.")
    db.execute("UPDATE users SET coins = coins - ? WHERE id=?", (amount, user_id))
    db.execute(
        "INSERT INTO transactions (user_id, transaction_type, amount, description) VALUES (?,?,?,?)",
        (user_id, "debit", amount, description),
    )
    db.commit()
    return row["coins"] - amount
