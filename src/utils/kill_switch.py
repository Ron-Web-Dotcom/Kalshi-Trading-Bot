"""Kill switch — write KILL to kill_switch.txt to halt all trading immediately."""

import logging
from pathlib import Path

logger = logging.getLogger("trading.kill_switch")

_KILL_SWITCH_FILE = Path(__file__).resolve().parent.parent.parent / "kill_switch.txt"


def is_active() -> bool:
    """Returns True if the kill switch is engaged (file contains 'KILL')."""
    from src.config.settings import settings
    if not settings.trading.kill_switch_enabled:
        return False
    try:
        if _KILL_SWITCH_FILE.exists():
            content = _KILL_SWITCH_FILE.read_text().strip()
            return "KILL" in content.upper()
    except Exception as e:
        logger.warning("Error reading kill switch file: %s", e)
    return False


def engage(reason: str = "") -> None:
    """Write KILL + reason to kill_switch.txt and log CRITICAL."""
    content = f"KILL\n{reason}" if reason else "KILL"
    try:
        _KILL_SWITCH_FILE.write_text(content)
        logger.critical("KILL SWITCH ENGAGED: %s", reason or "(no reason given)")
    except Exception as e:
        logger.error("Failed to write kill switch file: %s", e)


def disengage() -> None:
    """Delete or clear the kill switch file."""
    try:
        if _KILL_SWITCH_FILE.exists():
            _KILL_SWITCH_FILE.unlink()
            logger.info("Kill switch disengaged.")
    except Exception as e:
        logger.warning("Failed to remove kill switch file: %s", e)


def check_kill_switch() -> bool:
    """Log a WARNING if the kill switch is active; return is_active()."""
    active = is_active()
    if active:
        try:
            reason = _KILL_SWITCH_FILE.read_text().strip()
        except Exception:
            reason = "(unknown)"
        logger.warning("KILL SWITCH ACTIVE — trading halted. Reason: %s", reason)
    return active
