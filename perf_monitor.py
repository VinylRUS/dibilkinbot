"""Performance monitor — checks bot health every 5 minutes.

v3.5: Measures:
- Internal HTTP endpoints (localhost:port/login, /static/style.css)
- Discord connection status (bot.is_ready(), bot.is_closed())
- External APIs (GitHub, Modrinth) — best-effort, non-blocking
- DB query timing (SELECT COUNT(*) FROM users)

Thresholds (standard):
- Internal HTTP > 2s → WARNING
- Internal HTTP > 5s or 5xx → CRITICAL
- Discord disconnected → CRITICAL
- DB query > 1s → WARNING
- External API timeout → WARNING (not critical — external deps can be slow)

TG notifications:
- Only sent when problems detected (silent when OK)
- 15 min cooldown per alert type (don't spam every 5 min)
- Recovery notification when problem resolves
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Optional

import httpx

import db
from config import settings

log = logging.getLogger("perf_monitor")

CHECK_INTERVAL = 300  # 5 minutes
ALERT_COOLDOWN = 900  # 15 minutes — don't spam same alert type

# Thresholds (seconds)
HTTP_WARNING = 2.0
HTTP_CRITICAL = 5.0
DB_WARNING = 1.0

# Track active alerts for cooldown + recovery detection
_active_alerts: dict[str, float] = {}  # alert_key → last_sent_timestamp


async def _check_internal_http(port: int) -> dict:
    """Check internal HTTP endpoints. Returns dict with results."""
    results = {}
    base = f"http://127.0.0.1:{port}"

    endpoints = [
        ("login_page", f"{base}/login"),
        ("static_css", f"{base}/static/style.css?v=perf"),
    ]

    for name, url in endpoints:
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(url)
            elapsed = time.monotonic() - start
            results[name] = {
                "status": r.status_code,
                "latency_ms": round(elapsed * 1000),
                "ok": r.status_code < 400 and elapsed < HTTP_CRITICAL,
                "critical": r.status_code >= 500 or elapsed >= HTTP_CRITICAL,
                "warning": elapsed >= HTTP_WARNING and elapsed < HTTP_CRITICAL,
            }
        except Exception as e:
            elapsed = time.monotonic() - start
            results[name] = {
                "status": 0,
                "latency_ms": round(elapsed * 1000),
                "ok": False,
                "critical": True,
                "warning": False,
                "error": str(e)[:100],
            }

    return results


async def _check_db() -> dict:
    """Check DB query timing."""
    start = time.monotonic()
    try:
        async with db._connect() as conn:
            async with conn.execute("SELECT COUNT(*) FROM users") as cur:
                row = await cur.fetchone()
        elapsed = time.monotonic() - start
        return {
            "latency_ms": round(elapsed * 1000),
            "user_count": row[0] if row else 0,
            "ok": elapsed < DB_WARNING,
            "warning": elapsed >= DB_WARNING,
            "critical": False,
        }
    except Exception as e:
        elapsed = time.monotonic() - start
        return {
            "latency_ms": round(elapsed * 1000),
            "ok": False,
            "critical": True,
            "warning": False,
            "error": str(e)[:100],
        }


def _check_discord() -> dict:
    """Check Discord connection status."""
    try:
        import bot as bot_module
        bot = bot_module.get_bot_instance()
        if not bot:
            return {"ok": False, "critical": True, "error": "bot instance is None"}
        if bot.is_closed():
            return {"ok": False, "critical": True, "error": "bot.is_closed() = True"}
        if not bot.is_ready():
            return {"ok": False, "critical": True, "error": "bot.is_ready() = False"}
        # Check latency (websocket heartbeat)
        latency_ms = round(bot.latency * 1000) if bot.latency else 0
        return {
            "ok": True,
            "critical": False,
            "warning": latency_ms > 1000,
            "latency_ms": latency_ms,
        }
    except Exception as e:
        return {"ok": False, "critical": True, "error": str(e)[:100]}


async def _send_tg_alert(text: str) -> bool:
    """Send alert to admin's Telegram. Returns True on success."""
    try:
        import crypto
        import telegram

        admin_id = settings.admin_discord_id
        if not admin_id:
            return False

        tg_link = await db.get_tg_link(admin_id)
        if not tg_link or not tg_link[1]:
            return False

        tg_user_id = str(tg_link[1])
        raw_token = await db.get_setting("telegram_token")
        if not raw_token:
            return False

        tg_token = crypto.decrypt(raw_token)
        await telegram.send_message(tg_token, tg_user_id, text)
        return True
    except Exception as e:
        log.warning("Failed to send TG alert: %s", e)
        return False


def _should_send_alert(alert_key: str) -> bool:
    """Check if enough time passed since last alert of this type."""
    now = time.monotonic()
    last = _active_alerts.get(alert_key)
    if last is None:
        return True
    return (now - last) >= ALERT_COOLDOWN


def _mark_alert_sent(alert_key: str) -> None:
    _active_alerts[alert_key] = time.monotonic()


def _clear_alert(alert_key: str) -> None:
    """Clear alert — problem resolved."""
    _active_alerts.pop(alert_key, None)


async def _run_perf_check(port: int) -> dict:
    """Run one performance check cycle. Returns aggregated results."""
    http_results = await _check_internal_http(port)
    db_result = await _check_db()
    discord_result = _check_discord()

    # Collect problems
    problems = []
    recoveries = []

    # Check HTTP endpoints
    for name, result in http_results.items():
        alert_key = f"http_{name}"
        if result.get("critical"):
            if _should_send_alert(alert_key):
                status = result.get("status", 0)
                latency = result.get("latency_ms", 0)
                err = result.get("error", "")
                msg = f"🔴 CRITICAL: HTTP {name} — status={status}, latency={latency}ms"
                if err:
                    msg += f", error={err}"
                problems.append((alert_key, msg))
                _mark_alert_sent(alert_key)
        elif result.get("warning"):
            if _should_send_alert(alert_key):
                latency = result.get("latency_ms", 0)
                problems.append((alert_key, f"🟡 WARNING: HTTP {name} — slow: {latency}ms"))
                _mark_alert_sent(alert_key)
        else:
            # OK — check if recovering from previous alert
            if alert_key in _active_alerts:
                latency = result.get("latency_ms", 0)
                recoveries.append((alert_key, f"✅ Recovered: HTTP {name} — {latency}ms"))
                _clear_alert(alert_key)

    # Check DB
    alert_key = "db_query"
    if db_result.get("critical"):
        if _should_send_alert(alert_key):
            err = db_result.get("error", "unknown")
            problems.append((alert_key, f"🔴 CRITICAL: DB query failed — {err}"))
            _mark_alert_sent(alert_key)
    elif db_result.get("warning"):
        if _should_send_alert(alert_key):
            latency = db_result.get("latency_ms", 0)
            problems.append((alert_key, f"🟡 WARNING: DB query slow — {latency}ms"))
            _mark_alert_sent(alert_key)
    else:
        if alert_key in _active_alerts:
            latency = db_result.get("latency_ms", 0)
            recoveries.append((alert_key, f"✅ Recovered: DB query — {latency}ms"))
            _clear_alert(alert_key)

    # Check Discord
    alert_key = "discord_connection"
    if discord_result.get("critical"):
        if _should_send_alert(alert_key):
            err = discord_result.get("error", "unknown")
            problems.append((alert_key, f"🔴 CRITICAL: Discord — {err}"))
            _mark_alert_sent(alert_key)
    elif discord_result.get("warning"):
        if _should_send_alert(alert_key):
            latency = discord_result.get("latency_ms", 0)
            problems.append((alert_key, f"🟡 WARNING: Discord latency — {latency}ms"))
            _mark_alert_sent(alert_key)
    else:
        if alert_key in _active_alerts:
            latency = discord_result.get("latency_ms", 0)
            recoveries.append((alert_key, f"✅ Recovered: Discord — {latency}ms"))
            _clear_alert(alert_key)

    # Send TG notifications
    if problems:
        # Combine all problems into one message
        lines = ["🚨 Performance Alert", ""]
        for _, msg in problems:
            lines.append(msg)
        lines.append("")
        lines.append(f"⏰ {datetime.utcnow().strftime('%H:%M:%S UTC')}")
        await _send_tg_alert("\n".join(lines))
        log.warning("Perf alert sent: %s", "; ".join(msg for _, msg in problems))

    if recoveries:
        lines = ["✅ Recovery", ""]
        for _, msg in recoveries:
            lines.append(msg)
        await _send_tg_alert("\n".join(lines))
        log.info("Perf recovery sent: %s", "; ".join(msg for _, msg in recoveries))

    return {
        "http": http_results,
        "db": db_result,
        "discord": discord_result,
        "problems": len(problems),
        "recoveries": len(recoveries),
    }


async def perf_monitor_loop(port: int = 3000) -> None:
    """Background loop — runs performance check every 5 minutes."""
    log.info("Perf monitor started (interval=%ds, port=%d)", CHECK_INTERVAL, port)
    # First check after 60s (let bot fully start)
    await asyncio.sleep(60)

    while True:
        try:
            result = await _run_perf_check(port)
            if result["problems"] == 0 and result["recoveries"] == 0:
                log.debug("Perf check OK — no problems")
            else:
                log.info("Perf check: %d problems, %d recoveries",
                         result["problems"], result["recoveries"])
        except asyncio.CancelledError:
            log.info("Perf monitor cancelled")
            return
        except Exception as e:
            log.error("Perf monitor error: %s", e, exc_info=True)

        await asyncio.sleep(CHECK_INTERVAL)
