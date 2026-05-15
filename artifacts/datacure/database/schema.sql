CREATE TABLE IF NOT EXISTS users (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL DEFAULT '',
    phone_encrypted   TEXT    NOT NULL UNIQUE,
    password_hash     TEXT    NOT NULL,
    coins             INTEGER NOT NULL DEFAULT 0 CHECK (coins >= 0),
    lifetime_data_saved INTEGER NOT NULL DEFAULT 0,
    streak            INTEGER NOT NULL DEFAULT 0,
    total_ads_watched INTEGER NOT NULL DEFAULT 0,
    last_active_date  TEXT,
    streak_bonus_date TEXT,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    is_admin          INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS recharge_requests (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id),
    phone_number   TEXT    NOT NULL,
    operator       TEXT    NOT NULL DEFAULT 'Unknown',
    recharge_pack  TEXT    NOT NULL,
    recharge_amount INTEGER NOT NULL,
    coins_used     INTEGER NOT NULL,
    status         TEXT    NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','completed','failed')),
    created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS transactions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(id),
    transaction_type TEXT    NOT NULL CHECK (transaction_type IN ('credit','debit')),
    amount           INTEGER NOT NULL CHECK (amount > 0),
    description      TEXT    NOT NULL DEFAULT '',
    created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ad_rewards (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    reward_date TEXT    NOT NULL,
    ads_watched INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_recharge_user ON recharge_requests(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ad_rewards_user_date ON ad_rewards(user_id, reward_date);
