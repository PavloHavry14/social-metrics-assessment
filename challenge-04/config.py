"""Tunable parameters for ADB automation.

All timing values, coordinate ratios, thresholds, and app-specific
identifiers are centralised here so operators can adjust behaviour
without touching action logic.
"""

CONFIG = {
    # ── App target ──────────────────────────────────────────────
    "app_package": "com.zhiliaoapp.musically",  # TikTok package name
    "app_activity": "com.ss.android.ugc.aweme.splash.SplashActivity",

    # ── Scroll behaviour ────────────────────────────────────────
    "scroll_speed_range": (300, 800),    # swipe duration in ms
    "scroll_pause_range": (1.0, 3.0),   # seconds to pause between scrolls

    # ── Like behaviour ──────────────────────────────────────────
    "like_count": 5,                     # number of posts to like per run

    # ── Adaptive-wait settings ──────────────────────────────────
    "wait_timeout": 10,                  # max seconds to wait for a state
    "poll_interval": 0.5,               # seconds between state polls

    # ── Recovery ────────────────────────────────────────────────
    "max_recovery_attempts": 3,
    "back_press_limit": 3,              # back presses before force-stop

    # ── Post / upload ──────────────────────────────────────────
    "caption_text": "Check out this amazing content! #fyp",
    "video_path": "/sdcard/DCIM/upload_video.mp4",

    # ── Coordinate ratios (proportion of screen W x H) ─────────
    # These are default hints; screen_detector may override them
    # from the actual UI hierarchy when available.
    "coords": {
        "home_tab":       (0.10, 0.97),
        "upload_button":  (0.50, 0.97),
        "profile_tab":    (0.90, 0.97),
        "like_button":    (0.95, 0.50),
        "back_button":    (0.05, 0.05),
        "post_button":    (0.90, 0.05),
        "caption_field":  (0.50, 0.30),
        # Scroll gesture: start → end (x stays, y changes)
        "scroll_start":   (0.50, 0.75),
        "scroll_end":     (0.50, 0.25),
    },

    # ── Logging ─────────────────────────────────────────────────
    "log_file": "automation.log",
    "log_level": "DEBUG",
}
