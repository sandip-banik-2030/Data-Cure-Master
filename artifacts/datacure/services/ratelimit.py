import time

WINDOW = 5
MAX_REQUESTS = 5


def rl_key(endpoint: str, uid) -> str:
    return f"rl:{endpoint}:{uid}"


def check_rate_limit(session, key: str):
    now = time.time()
    window_start = now - WINDOW
    timestamps = [t for t in session.get(key, []) if t > window_start]
    if len(timestamps) >= MAX_REQUESTS:
        retry_after = int(WINDOW - (now - timestamps[0])) + 1
        session[key] = timestamps
        return False, retry_after
    timestamps.append(now)
    session[key] = timestamps
    return True, 0
