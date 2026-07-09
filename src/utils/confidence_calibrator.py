"""
Adaptive confidence threshold calibrator.

Rules (evaluated at midnight each day):
  Zero activity (0 closed, 0 open) -> nudge DOWN 1% per day to find entries
  < 20 closed trades               -> hold current, not enough data
  >= 20 closed trades              -> adjust by win rate in lowest band:
      >= 60% WR -> lower 2%  (edge proven, be more active)
      < 48% WR  -> raise 2%  (weak edge, tighten up)
      otherwise -> no change

Floor 60% / Ceiling 75% / Default 65%
Silent — no Discord, no new loops, no memory impact.
"""

import json
import logging
import os
from zoneinfo import ZoneInfo
_ET = ZoneInfo("America/New_York")

logger = logging.getLogger("trading.calibrator")

_THRESHOLD_FILE  = os.path.join(os.path.dirname(__file__), "..", "..", "data", "conf_threshold.json")
_DEFAULT         = 75.0
_FLOOR_CONF      = 75.0
_CEIL_CONF       = 85.0
_MIN_TRADES      = 20
_MIN_BAND_TRADES = 5
_WIN_RATE_HIGH   = 0.60
_WIN_RATE_LOW    = 0.48
_STEP            = 2.0
_NUDGE_DOWN      = 1.0


def get_threshold() -> float:
    """Return the calibrated threshold, or 65% default if not yet set."""
    try:
        if os.path.exists(_THRESHOLD_FILE):
            with open(_THRESHOLD_FILE) as f:
                data = json.load(f)
            val = float(data.get("threshold", _DEFAULT))
            return max(_FLOOR_CONF, min(_CEIL_CONF, val))
    except Exception:
        pass
    return _DEFAULT


def _save(threshold: float, reason: str, trades_used: int) -> None:
    try:
        import datetime
        with open(_THRESHOLD_FILE, "w") as f:
            json.dump({
                "threshold":   round(threshold, 1),
                "reason":      reason,
                "trades_used": trades_used,
                "updated_at":  datetime.datetime.now(_ET).isoformat(),
            }, f)
    except Exception as e:
        logger.warning("Could not save calibrated threshold: %s", e)


async def calibrate(db) -> float:
    """Adjust threshold based on closed trade win rates. Called once at midnight."""
    if not db:
        return get_threshold()

    current = get_threshold()

    try:
        open_row = await db.fetchone(
            "SELECT COUNT(*) as n FROM positions WHERE status='open'"
        )
        open_count = int((open_row or {}).get("n", 0))
    except Exception:
        open_count = 0

    try:
        rows = await db.fetchall(
            "SELECT tl.ai_confidence AS confidence, "
            "CASE WHEN tl.result='WIN' THEN 1 ELSE 0 END as won "
            "FROM trade_logs tl "
            "WHERE tl.result IN ('WIN','LOSS','BREAK_EVEN') "
            "  AND tl.ai_confidence IS NOT NULL "
            "ORDER BY tl.resolved_at DESC LIMIT 200"
        )
    except Exception as e:
        logger.debug("Calibrator DB read failed: %s", e)
        return current

    rows = [dict(r) for r in (rows or [])]
    total = len(rows)

    # Zero activity -> nudge down so bot finds entries
    if total == 0 and open_count == 0:
        new_thresh = max(_FLOOR_CONF, current - _NUDGE_DOWN)
        reason = "zero activity — nudging down to find entries"
        _save(new_thresh, reason, 0)
        if new_thresh != current:
            logger.info("Confidence nudged down: %.0f%% -> %.0f%%  (%s)", current, new_thresh, reason)
        return new_thresh

    if total < _MIN_TRADES:
        logger.debug("Calibrator: %d/%d closed trades — keeping %.0f%%", total, _MIN_TRADES, current)
        return current

    # Win rate by 5-point band
    bands: dict = {}
    for r in rows:
        conf = float(r.get("confidence") or 0)
        band = int(conf // 5) * 5
        if band not in bands:
            bands[band] = {"wins": 0, "total": 0}
        bands[band]["total"] += 1
        if r.get("won"):
            bands[band]["wins"] += 1

    new_thresh = current
    reason     = "no band had enough samples"

    for band in sorted(bands.keys()):
        if band < _FLOOR_CONF:
            continue
        b = bands[band]
        if b["total"] < _MIN_BAND_TRADES:
            continue
        wr = b["wins"] / b["total"]
        logger.info("Calibrator: band %d-%d -> %d/%d wins (%.0f%% WR) | threshold=%.0f%%",
                    band, band + 4, b["wins"], b["total"], wr * 100, current)
        if wr >= _WIN_RATE_HIGH:
            new_thresh = max(_FLOOR_CONF, current - _STEP)
            reason = f"band {band}-{band+4} WR={wr*100:.0f}% >= 60% — lowering"
        elif wr < _WIN_RATE_LOW:
            new_thresh = min(_CEIL_CONF, current + _STEP)
            reason = f"band {band}-{band+4} WR={wr*100:.0f}% < 48% — raising"
        else:
            reason = f"band {band}-{band+4} WR={wr*100:.0f}% in range — no change"
        break

    _save(new_thresh, reason, total)
    if new_thresh != current:
        logger.info("Confidence adjusted: %.0f%% -> %.0f%%  (%s)", current, new_thresh, reason)
    else:
        logger.info("Confidence unchanged at %.0f%%  (%s)", current, reason)

    return new_thresh
