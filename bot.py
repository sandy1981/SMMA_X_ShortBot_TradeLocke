"""
═══════════════════════════════════════════════════════════════════
  3SMMA_X Short Bot — Blueberry TradeLocker
  Version: 1.1 (Railway-compatible, lazy connection)
═══════════════════════════════════════════════════════════════════
"""

import os
import json
import logging
from datetime import datetime, timezone
from flask import Flask, request, jsonify

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION (set via Railway Variables tab)
# ═══════════════════════════════════════════════════════════════

TL_EMAIL       = os.environ.get("TL_EMAIL", "")
TL_PASSWORD    = os.environ.get("TL_PASSWORD", "")
TL_SERVER      = os.environ.get("TL_SERVER", "")
TL_ENV         = os.environ.get("TL_ENV", "https://live.tradelocker.com")
SYMBOL         = os.environ.get("SYMBOL", "XAUUSD")
LOT_SIZE       = float(os.environ.get("LOT_SIZE", "0.01"))
DAILY_LOSS_LIMIT = float(os.environ.get("DAILY_LOSS_LIMIT", "200"))
MAX_OPEN_LEGS  = int(os.environ.get("MAX_OPEN_LEGS", "4"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "3smma_short_2026")
PORT           = int(os.environ.get("PORT", "5000"))

# ═══════════════════════════════════════════════════════════════
# LOGGING (console only — Railway captures stdout)
# ═══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("3SMMA_X")

# ═══════════════════════════════════════════════════════════════
# TRADELOCKER CLIENT (lazy connection — connects on first use)
# ═══════════════════════════════════════════════════════════════

class TradeLockerClient:
    def __init__(self):
        self.tl = None
        self.instrument_id = None
        self.connected = False
        self.last_error = None

    def _is_configured(self):
        return bool(TL_EMAIL and TL_PASSWORD and TL_SERVER)

    def connect(self):
        if not self._is_configured():
            log.warning("  TradeLocker credentials not set")
            return False
        try:
            from tradelocker import TLAPI
            log.info(f"  Connecting to {TL_ENV} as {TL_EMAIL} on server {TL_SERVER}...")
            self.tl = TLAPI(
                environment=TL_ENV,
                username=TL_EMAIL,
                password=TL_PASSWORD,
                server=TL_SERVER
            )
            log.info("  Auth OK — fetching instrument...")
            self.instrument_id = self.tl.get_instrument_id_from_symbol_name(SYMBOL)
            if not self.instrument_id:
                self.last_error = f"Symbol '{SYMBOL}' not found"
                log.error(f"  {self.last_error}")
                return False
            log.info(f"  TradeLocker: Connected — {SYMBOL} → id={self.instrument_id}")
            self.connected = True
            self.last_error = None
            return True
        except Exception as e:
            self.last_error = str(e)
            log.error(f"  TradeLocker connection failed: {e}")
            return False

    def ensure_connected(self):
        if not self.connected:
            return self.connect()
        try:
            price = self.tl.get_latest_asking_price(self.instrument_id)
            if price is None:
                return self.connect()
            return True
        except Exception:
            return self.connect()

    def open_short(self):
        if not self.ensure_connected():
            return None, "Not connected"
        try:
            order_id = self.tl.create_order(
                instrument_id=self.instrument_id,
                quantity=LOT_SIZE,
                side="sell",
                type_="market"
            )
            if order_id:
                log.info(f"  TradeLocker: SELL {LOT_SIZE} lots — position #{order_id}")
                return order_id, "ok"
            else:
                log.error("  TradeLocker: order returned None")
                return None, "Order returned None"
        except Exception as e:
            log.error(f"  TradeLocker: order failed — {e}")
            return None, str(e)

    def close_position(self, position_id):
        if not self.ensure_connected():
            return False, "Not connected"
        try:
            result = self.tl.close_position(position_id)
            if result:
                log.info(f"  TradeLocker: Closed #{position_id}")
                return True, "ok"
            else:
                log.error(f"  TradeLocker: Close failed for #{position_id}")
                return False, "Close returned None"
        except Exception as e:
            log.error(f"  TradeLocker: close failed — {e}")
            return False, str(e)

    def get_status(self):
        if not self._is_configured():
            return {"status": "NOT CONFIGURED — add TL_EMAIL, TL_PASSWORD, TL_SERVER in Railway Variables"}
        if not self.connected:
            # Try to connect now so the status page shows real result
            success = self.connect()
            if not success:
                return {"status": "connection_failed", "server": TL_SERVER,
                        "error": getattr(self, 'last_error', 'unknown'),
                        "hint": "Check TL_EMAIL, TL_PASSWORD, TL_SERVER values"}
        try:
            price = self.tl.get_latest_asking_price(self.instrument_id)
            return {"status": "connected", "server": TL_SERVER, "symbol": SYMBOL, "price": price}
        except Exception as e:
            return {"status": "error", "message": str(e)}


# ═══════════════════════════════════════════════════════════════
# POSITION TRACKER
# ═══════════════════════════════════════════════════════════════

class PositionTracker:
    STATE_FILE = "/tmp/positions.json"  # /tmp is writable on Railway

    def __init__(self):
        self.positions = {}
        self.trade_log = []
        self.daily_pnl = 0.0
        self.daily_date = ""
        self._load()

    def _load(self):
        try:
            with open(self.STATE_FILE, "r") as f:
                data = json.load(f)
                self.positions = data.get("positions", {})
                self.trade_log = data.get("trade_log", [])
                self.daily_pnl = data.get("daily_pnl", 0.0)
                self.daily_date = data.get("daily_date", "")
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _save(self):
        try:
            with open(self.STATE_FILE, "w") as f:
                json.dump({
                    "positions": self.positions,
                    "trade_log": self.trade_log,
                    "daily_pnl": self.daily_pnl,
                    "daily_date": self.daily_date
                }, f, indent=2, default=str)
        except Exception as e:
            log.error(f"  Failed to save state: {e}")

    def _check_daily_reset(self):
        today = datetime.now(timezone.utc).date().isoformat()
        if today != self.daily_date:
            self.daily_pnl = 0.0
            self.daily_date = today
            self._save()

    def add_position(self, leg_id, position_id):
        self.positions[leg_id] = {
            "position_id": str(position_id),
            "opened_at": datetime.now(timezone.utc).isoformat()
        }
        self._save()
        log.info(f"  Tracker: {leg_id} → #{position_id}")

    def close_position(self, leg_id, exit_type, pnl=0.0):
        if leg_id not in self.positions:
            return None
        pos = self.positions.pop(leg_id)
        self._check_daily_reset()
        self.daily_pnl += pnl
        self.trade_log.append({
            "leg": leg_id,
            "position_id": pos["position_id"],
            "exit_type": exit_type,
            "pnl": pnl,
            "opened_at": pos["opened_at"],
            "closed_at": datetime.now(timezone.utc).isoformat()
        })
        self._save()
        return pos["position_id"]

    def get_position_id(self, leg_id):
        pos = self.positions.get(leg_id)
        return pos["position_id"] if pos else None

    def open_legs(self):
        return list(self.positions.keys())

    def is_daily_limit_hit(self):
        self._check_daily_reset()
        return self.daily_pnl <= -abs(DAILY_LOSS_LIMIT)

    def stats(self):
        total = len(self.trade_log)
        if total == 0:
            return {"total": 0, "pnl": 0, "wins": 0, "losses": 0, "win_rate": 0}
        total_pnl = sum(t.get("pnl", 0) for t in self.trade_log)
        wins = sum(1 for t in self.trade_log if t.get("pnl", 0) > 0)
        losses = sum(1 for t in self.trade_log if t.get("pnl", 0) < 0)
        return {
            "total": total,
            "pnl": round(total_pnl, 2),
            "wins": wins,
            "losses": losses,
            "win_rate": round(100 * wins / total, 1) if total > 0 else 0
        }


# ═══════════════════════════════════════════════════════════════
# SIGNAL HANDLER
# ═══════════════════════════════════════════════════════════════

client = TradeLockerClient()
tracker = PositionTracker()


def handle_entry(leg_id, data):
    if tracker.get_position_id(leg_id):
        log.warning(f"  {leg_id} already open — skipping")
        return {"status": "duplicate", "leg": leg_id}
    if tracker.is_daily_limit_hit():
        log.warning(f"  DAILY LOSS LIMIT — rejecting entry")
        return {"status": "blocked", "leg": leg_id, "reason": "daily_loss_limit"}
    if len(tracker.open_legs()) >= MAX_OPEN_LEGS:
        log.warning(f"  Max {MAX_OPEN_LEGS} legs — rejecting")
        return {"status": "blocked", "leg": leg_id, "reason": "max_positions"}

    position_id, msg = client.open_short()
    if position_id:
        tracker.add_position(leg_id, position_id)
        return {"status": "opened", "leg": leg_id, "position_id": str(position_id)}
    return {"status": "error", "leg": leg_id, "reason": msg}


def handle_exit(leg_id, exit_type, data):
    position_id = tracker.get_position_id(leg_id)
    if not position_id:
        log.warning(f"  {leg_id} not found")
        return {"status": "not_found", "leg": leg_id}
    success, msg = client.close_position(position_id)
    if success:
        tracker.close_position(leg_id, exit_type, pnl=0.0)
        return {"status": "closed", "leg": leg_id, "exit_type": exit_type}
    return {"status": "error", "leg": leg_id, "reason": msg}


def handle_close_all(data):
    results = []
    for leg_id in list(tracker.positions.keys()):
        results.append(handle_exit(leg_id, "CLOSE_ALL", data))
    return results


# ═══════════════════════════════════════════════════════════════
# FLASK APP
# ═══════════════════════════════════════════════════════════════

app = Flask(__name__)


@app.route("/webhook", methods=["POST"])
def webhook():
    secret = request.args.get("secret", "")
    if secret != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "invalid json"}), 400

    action = data.get("action", "").upper()
    leg = data.get("leg", "")
    exit_type = data.get("exit_type", "")

    log.info(f"  SIGNAL: action={action} leg={leg} exit={exit_type}")

    if action == "ENTRY":
        if leg not in ("S1", "S2", "S3", "S4"):
            return jsonify({"error": f"unknown leg: {leg}"}), 400
        result = handle_entry(leg, data)
    elif action == "EXIT":
        if not leg:
            return jsonify({"error": "missing leg"}), 400
        result = handle_exit(leg, exit_type, data)
    elif action == "CLOSE_ALL":
        result = handle_close_all(data)
    else:
        return jsonify({"error": f"unknown action: {action}"}), 400

    log.info(f"  Result: {result}")
    return jsonify(result), 200


@app.route("/", methods=["GET"])
def health():
    stats = tracker.stats()
    tracker._check_daily_reset()
    return jsonify({
        "bot": "3SMMA_X Short Bot v1.1",
        "status": "running",
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "config": {
            "symbol": SYMBOL,
            "lot_size": LOT_SIZE,
            "daily_limit": DAILY_LOSS_LIMIT
        },
        "tradelocker": client.get_status(),
        "open_legs": tracker.open_legs(),
        "stats": stats,
        "daily_pnl": round(tracker.daily_pnl, 2),
        "daily_limit_hit": tracker.is_daily_limit_hit()
    })


@app.route("/positions", methods=["GET"])
def positions():
    return jsonify({"open": tracker.positions, "count": len(tracker.positions)})


@app.route("/trades", methods=["GET"])
def trades():
    return jsonify({"stats": tracker.stats(), "recent": list(reversed(tracker.trade_log[-50:]))})


@app.route("/emergency-close", methods=["POST"])
def emergency_close():
    secret = request.args.get("secret", "")
    if secret != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"results": handle_close_all({})})


# ═══════════════════════════════════════════════════════════════
# LOCAL DEV STARTUP (Railway uses gunicorn via Procfile)
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    log.info("  3SMMA_X Short Bot v1.1 — starting locally")
    app.run(host="0.0.0.0", port=PORT, debug=False)
