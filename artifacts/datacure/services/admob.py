"""
AdMob configuration and anti-ban state management for Datacure.

IMPORTANT — Platform note:
  Google AdMob is a native Android/iOS SDK. It cannot render banners or video
  directly inside a Python/Flask browser app. This module stores the production
  config (ready for a Capacitor or Cordova native wrapper) and provides all
  server-side anti-ban logic that works on the web today.

  To go live with real ads:
    1. Wrap the app in Capacitor (npm i @capacitor/core @capacitor/admob).
    2. Set TEST_MODE = False below.
    3. The BANNER_AD_UNIT_ID / REWARDED_AD_UNIT_ID constants are consumed by
       the native plugin; this Flask server is the reward back-end.

Usage in app.py:
  from services.admob import (
      TEST_MODE, ADMOB_APP_ID,
      BANNER_AD_UNIT_ID, REWARDED_AD_UNIT_ID,
      AD_BATCH_SIZE, AD_BATCH_COOLDOWN_SECS,
      get_batch_state, record_ad_complete, check_cooldown,
  )
"""

import time
from flask import session

# ── Mode flag ──────────────────────────────────────────────────────────────────
# True  → Google's universal test IDs (safe during development, no policy risk)
# False → Your production keys (set this only when submitting to Play/App Store)
TEST_MODE: bool = False

# ── App ID ─────────────────────────────────────────────────────────────────────
ADMOB_APP_ID = "ca-app-pub-1984458211665769~3776326573"

# ── Ad unit IDs ────────────────────────────────────────────────────────────────
_PROD_BANNER_ID   = "ca-app-pub-1984458211665769/7041120862"   # Datacure_Tab_Banner
_PROD_REWARDED_ID = "ca-app-pub-1984458211665769/4573863922"   # Datacure_Earn_Money_Video

# Google's safe universal test IDs — always use these in TEST_MODE
_TEST_BANNER_ID   = "ca-app-pub-3940256099942544/6300978111"
_TEST_REWARDED_ID = "ca-app-pub-3940256099942544/5224354917"

BANNER_AD_UNIT_ID   = _TEST_BANNER_ID   if TEST_MODE else _PROD_BANNER_ID
REWARDED_AD_UNIT_ID = _TEST_REWARDED_ID if TEST_MODE else _PROD_REWARDED_ID

# ── Anti-ban parameters ────────────────────────────────────────────────────────
AD_BATCH_SIZE         = 5    # consecutive rewarded watches allowed before forced cooldown
AD_BATCH_COOLDOWN_SECS = 120  # mandatory rest (seconds) after every AD_BATCH_SIZE ads


# ── Session helpers ────────────────────────────────────────────────────────────

def get_batch_state() -> dict:
    """
    Return the current per-session batch state.

    Returns:
        {
          "batch_count":      int,   # ads watched in current batch (0–AD_BATCH_SIZE)
          "in_cooldown":      bool,
          "cooldown_secs_left": int, # seconds until cooldown ends (0 if not in cooldown)
          "cooldown_until":   float, # Unix timestamp when cooldown ends
        }
    """
    batch_count    = session.get("ad_batch_count", 0)
    cooldown_until = session.get("ad_cooldown_until", 0.0)
    now            = time.time()
    in_cooldown    = now < cooldown_until
    secs_left      = max(0, int(cooldown_until - now)) if in_cooldown else 0
    return {
        "batch_count":        batch_count,
        "in_cooldown":        in_cooldown,
        "cooldown_secs_left": secs_left,
        "cooldown_until":     cooldown_until,
    }


def check_cooldown() -> tuple[bool, int]:
    """
    Return (in_cooldown: bool, secs_left: int).
    Fast check for use in ad-start guard.
    """
    cooldown_until = session.get("ad_cooldown_until", 0.0)
    now = time.time()
    if now < cooldown_until:
        return True, max(0, int(cooldown_until - now))
    return False, 0


def record_ad_complete() -> dict:
    """
    Increment the batch counter after a successful ad completion.
    Triggers a cooldown when AD_BATCH_SIZE is reached.

    Returns:
        {
          "batch_count":         int,   # updated count (reset to 0 if cooldown triggered)
          "cooldown_triggered":  bool,
          "cooldown_secs":       int,   # AD_BATCH_COOLDOWN_SECS if triggered, else 0
          "cooldown_until":      float, # Unix timestamp, 0 if no cooldown
          "ads_in_batch":        int,   # position in current batch (1-indexed, after increment)
          "batch_size":          int,   # AD_BATCH_SIZE constant
        }
    """
    batch_count = session.get("ad_batch_count", 0) + 1

    if batch_count >= AD_BATCH_SIZE:
        cooldown_until = time.time() + AD_BATCH_COOLDOWN_SECS
        session["ad_batch_count"]    = 0
        session["ad_cooldown_until"] = cooldown_until
        return {
            "batch_count":        0,
            "cooldown_triggered": True,
            "cooldown_secs":      AD_BATCH_COOLDOWN_SECS,
            "cooldown_until":     cooldown_until,
            "ads_in_batch":       AD_BATCH_SIZE,
            "batch_size":         AD_BATCH_SIZE,
        }

    session["ad_batch_count"] = batch_count
    return {
        "batch_count":        batch_count,
        "cooldown_triggered": False,
        "cooldown_secs":      0,
        "cooldown_until":     0.0,
        "ads_in_batch":       batch_count,
        "batch_size":         AD_BATCH_SIZE,
    }
