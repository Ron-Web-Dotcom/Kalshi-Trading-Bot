"""
Adaptive confidence threshold calibrator.

Reads closed trade history from the DB, computes win rate per confidence
band, and writes a recommended threshold to /tmp/kalshi_conf_threshold.json.

Rules:
  - Needs MIN_TRADES closed trades before adjusting (otherwise stays at DEFAULT)
  - Looks at the lowest band that has ≥ MIN_BAND_TRADES samples
  - If win rate in that band ≥ WIN_RATE_HIGH  → lower threshold by 2% (edge is real)
  - If win rate in that band < WIN_RATE_LOW   → raise threshold by 2% (edge is weak)
  - Hard floor: FLOOR_CONF   Hard ceiling: CEIL_CONF
  - Called once per day at midnight — silent, no Discord, no extra memory

Usage:
    from src.utils.confidence_calibrator import calibrate, get_threshold
    await calibrate(db)        # call once at midnight
    threshold = get_threshold() # call from trade.py / bot.py
"""

import json
import logging
import os
from typing import Optional

logger = logging.getLogger("trading.calibrator")

_THRESHOLD_FILE = "/tmp/kalshi_conf_threshold.json"
_DEFAULT        = 65.0   # starting threshold before enough data
_FLOOR_CONF     = 60.0   # never go below this
_CEIL_CONF      = 75.0   # never go above this
_MIN_TRADES     = 20     # minimum closed trades before calibration kicks in
_MIN_BAND_TRADES = 5     # minimum trades in a band to use it for calibration
_WIN_RATE_HIGH  = 0.60   # ≥60% WR in the lowest band → lower the floor
_WIN_RATE_LOW   = 0.48   # <48% WR in the lowest band → raise the floor
_STEP           = 2.0    # how many % points to move per calibration


def get_threshold() -> float:
    """Return the current calibrated threshold (or default if not calibrated yet)."""
    try:
        if os.path.exists(_THRESHOLD_FILE):
            with open(_THRESHOLD_FILE) as f:
                data = json.load(f)
            val = float(data.get("threshold", _DEFAULT))
            # Clamp in case file was manually edited
            return max(_FLOOR_CONF, min(_CEIL_CONF, val))
    except Exception:
        pass
    return _DEFAULT


def _save(threshold: float, reason: str, trades_used: int) -> None:
    try:
        with open(_THRESHOLD_FILE, "w") as f:
            json.dump({
                "threshold":   round(threshold, 1),
                "reason":      reason,
                "trades_used": trades_used,
                "updated_at":  __import__("datetime").datetime.utcnow().isoformat(),
            }, f)
    except Exception as e:
        logger.warning("Could not save calibrated threshold: %s", e)


async def calibrate(db) -> float:
    """
    Read closed trades from DB, compute win rates by confidence band,
    adjust threshold. Returns the new threshold. Silent — no Discord.
    """
    if not db:
        return get_threshold()

    try:
        rows = await db.fetchall(
            "SELECT confidence, pnl FROM positions "
            "WHERE status='closed' AND pnl IS NOT NULL AND confidence IS NOT NULL "
            "ORDER BY closed_at DESC LIMIT 200"
        )
    except Exception as e:
        logger.debug("Calibrator DB read failed: %s", e)
        return get_threshold()

    if not rows:
        return get_threshold()

    rows = [dict(r) for r in rows]
    total = len(rows)

    if total < _MIN_TRADES:
        logger.debug("Calibrator: only %d closed trades — need %d, keeping %.0f%%",
                     total, _MIN_TRADES, get_threshold())
        return get_threshold()

    # Group by 5-point bands: 60-64, 65-69, 70-74, 75-79, 80+
    bands: dict = {}
    for r in rows:
        conf = float(r.get("confidence") or 0)
        pnl  = float(r.get("pnl") or 0)
        band = int(conf // 5) * 5   # floor to nearest 5
        if band not in bands:
            bands[band] = {"wins": 0, "total": 0}
        bands[band]["total"] += 1
        if pnl > 0:
            bands[band]["wins"] += 1

    current = get_threshold()

    # Find the lowest band at or above floor that has enough samples
    for band in sorted(bands.keys()):
        if band < _FLOOR_CONF:
            continue
        b = bands[band]
        if b["total"] < _MIN_BAND_TRADES:
            continue
        wr = b["wins"] / b["total"]
        logger.info(
            "Calibrator: band %d–%d → %d/%d wins (%.0f%% WR) | current threshold=%.0f%%",
            band, band + 4, b["wins"], b["total"], wr * 100, current,
        )
        if wr >= _WIN_RATE_HIGH:
            new_thresh = max(_FLOOR_CONF, current - _STEP)
            reason = f"band {band}-{band+4} WR={wr*100:.0f}% ≥ {_WIN_RATE_HIGH*100:.0f}% — lowering"
        elif wr < _WIN_RATE_LOW:
            new_thresh = min(_CEIL_CONF, current + _STEP)
            reason = f"band {band}-{band+4} WR={wr*100:.0f}% < {_WIN_RATE_LOW*100:.0f}% — raising"
        else:
            new_thresh = current
            reason = f"band {band}-{band+4} WR={wr*100:.0f}% in range — no change"
        break
    else:
        new_thresh = current
        reason = "no band had enough samples"

    _save(new_thresh, reason, total)
    if new_thresh != current:
        logger.info("🎯 Confidence threshold auto-adjusted: %.0f%% → %.0f%%  (%s)",
                    current, new_thresh, reason)
    else:
        logger.info("🎯 Confidence threshold unchanged at %.0f%%  (%s)", current, reason)

    return new_thresh
