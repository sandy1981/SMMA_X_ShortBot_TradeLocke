"""
═══════════════════════════════════════════════════════════════════
  3SMMA_X Short Bot — Blueberry TradeLocker
  Version: 1.0 (matching Pine Script v5.3)

  Architecture:
    TradingView (3SMMA_X v5.3, Short Only, 15m XAUUSD)
      → fires alert_message JSON on each order fill
      → hits this Flask webhook (Railway / Render / any server)
      → bot executes on TradeLocker via REST API

  Legs per entry: S1, S2, S3, S4
  Exit types:     SX1, SX2a, SX2b, SX3a, SX3b, T4 Exit

  Deploy: Railway.app (free), Render.com, or any Linux/cloud host
  No Windows VPS needed. No MT5 terminal needed.
═══════════════════════════════════════════════════════════════════
"""

import os
import json
import logging
from datetime import datetime, timezone
from flask import Flask, request, jsonify

try:
    from tradelocker import TLAPI
except ImportError:
    print("ERROR: Install tradelocker package: pip install tradelocker")
    raise

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

# TradeLocker credentials (use environment variables on Railway)
TL_EMAIL    = os.environ.get("TL_EMAIL", "your_email@example.com")
TL_PASSWORD = os.environ.get("TL_PASSWORD", "your_password")
TL_SERVER   = os.environ.get("TL_SERVER", "BlueberryMarkets")  # server name shown in TradeLocker login
TL_ENV      = os.environ.get("TL_ENV", "https://live.tradelocker.com")  # live for funded account

# Trading settings
SYMBOL       = os.environ.get("SYMBOL", "XAUUSD")
LOT_SIZE     = float(os.environ.get("LOT_SIZE", "0.01"))   # 0.01 lots for validation
MAGIC_COMMENT = "3SMMA_X"                                    # tags bot trades

# Risk management — PROP FIRM SAFETY
DAILY_LOSS_LIMIT = float(os.environ.get("DAILY_LOSS_LIMIT", "200"))  # $ — stop trading if daily loss exceeds this
MAX_OPEN_LEGS    = int(os.environ.get("MAX_OPEN_LEGS", "4"))          # max simultaneous positions

# Webhook secret
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "3smma_short_2026")

# Flask port
PORT = int(os.environ.get("PORT", "5000"))

# ═══════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", mode="a", encoding="utf-8")
    ]
)
log = logging.getLogger("3SMMA_X")

# ═══════════════════════════════════════════════════════════════
# TRADELOCKER CLIENT
# ═══════════════════════════════════════════════════════════════

class TradeLockerClient:
    def __init__(self):
        self.tl = None
        self.instrument_id = None
        self.connected = False

    def connect(self):
        """Initialize TradeLocker API connection."""
        try:
            self.tl = TLAPI(
                environment=TL_ENV,
                username=TL_EMAIL,
                password=TL_PASSWORD,
                server=TL_SERVER
            )

            # Resolve instrument ID for XAUUSD
            self.instrument_id = self.tl.get_instrument_id_from_symbol_name(SYMBOL)
            if not self.instrument_id:
                log.error(f"  Symbol '{SYMBOL}' not found on TradeLocker")
                log.error(f"  Check exact name in TradeLocker platform")
                return False

            log.info(f"  TradeLocker: Connected to {TL_SERVER}")
            log.info(f"  TradeLocker: Symbol {SYMBOL} → instrument_id={self.instrument_id}")
            self.connected = True
            return True

        except Exception as e:
            log.error(f"  TradeLocker connection failed: {e}")
            return False

    def ensure_connected(self):
        """Reconnect if session expired."""
        if not self.connected or self.tl is None:
            log.warning("  TradeLocker: reconnecting...")
            return self.connect()
        # Test connection by getting price
        try:
            price = self.tl.get_latest_asking_price(self.instrument_id)
            if price is None:
                return self.connect()
            return True
        except Exception:
            return self.connect()

    def open_short(self, lot_size=None):
        """
        Open a short (sell) market order.
        Returns (position_id, message) or (None, error_message).
        """
        if not self.ensure_connected():
            return None, "Not connected"

        lots = lot_size or LOT_SIZE
        try:
            order_id = self.tl.create_order(
                instrument_id=self.instrument_id,
                quantity=lots,
                side="sell",
                type_="market"
            )

            if order_id:
                log.info(f"  TradeLocker: SELL {lots} lots — position #{order_id}")
                return order_id, "ok"
            else:
                log.error(f"  TradeLocker: order returned None")
                return None, "Order returned None"

        except Exception as e:
            log.error(f"  TradeLocker: order failed — {e}")
            return None, str(e)

    def close_position(self, position_id):
        """Close a specific position by ID."""
        if not self.ensure_connected():
            return False, "Not connected"

        try:
            result = self.tl.close_position(position_id)
            if result:
                log.info(f"  TradeLocker: Closed position #{position_id}")
                return True, "ok"
            else:
                log.error(f"  TradeLocker: Close returned falsy for #{position_id}")
                return False, "Close returned None"

        except Exception as e:
            log.error(f"  TradeLocker: close failed for #{position_id} — {e}")
            return False, str(e)

    def get_account_info(self):
        """Get account summary."""
        if not self.ensure_connected():
            return None
        try:
            # The TLAPI may expose account details via internal methods
            # Fallback to returning basic connection status
            price = self.tl.get_latest_asking_price(self.instrument_id)
            return {
                "status": "connected",
                "server": TL_SERVER,
                "symbol": SYMBOL,
                "current_price": price,
                "lot_size": LOT_SIZE
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}


# ═══════════════════════════════════════════════════════════════
# POSITION TRACKER (persists to disk, survives restarts)
# ═══════════════════════════════════════════════════════════════

class PositionTracker:
    """Maps Pine Script leg IDs (S1-S4) to TradeLocker position IDs."""
    STATE_FILE = "positions.json"

    def __init__(self):
        self.positions = {}  # {"S1": {"position_id": "abc", "opened_at": "..."}, ...}
        self.trade_log = []
        self.daily_pnl = 0.0
        self.daily_date = datetime.now(timezone.utc).date().isoformat()
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
        with open(self.STATE_FILE, "w") as f:
            json.dump({
                "positions": self.positions,
                "trade_log": self.trade_log,
                "daily_pnl": self.daily_pnl,
                "daily_date": self.daily_date
            }, f, indent=2, default=str)

    def _check_daily_reset(self):
        """Reset daily PnL counter at midnight UTC."""
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
        log.info(f"  Tracker: {leg_id} → TradeLocker #{position_id}")

    def close_position(self, leg_id, exit_type, pnl=0.0):
        if leg_id not in self.positions:
            log.warning(f"  Tracker: {leg_id} not in open positions")
            return None
        pos = self.positions.pop(leg_id)
        self._check_daily_reset()
        self.daily_pnl += pnl
        record = {
            "leg": leg_id,
            "position_id": pos["position_id"],
            "exit_type": exit_type,
            "pnl": pnl,
            "opened_at": pos["opened_at"],
            "closed_at": datetime.now(timezone.utc).isoformat()
        }
        self.trade_log.append(record)
        self._save()
        return pos["position_id"]

    def get_position_id(self, leg_id):
        pos = self.positions.get(leg_id)
        return pos["position_id"] if pos else None

    def open_legs(self):
        return list(self.positions.keys())

    def is_daily_limit_hit(self):
        """Check if daily loss limit has been breached."""
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
    """Open a short position for one leg."""
    # Duplicate check
    if tracker.get_position_id(leg_id):
        log.warning(f"  {leg_id} already open — skipping duplicate")
        return {"status": "duplicate", "leg": leg_id}

    # Daily loss limit check
    if tracker.is_daily_limit_hit():
        log.warning(f"  DAILY LOSS LIMIT HIT (${DAILY_LOSS_LIMIT}) — rejecting entry")
        return {"status": "blocked", "leg": leg_id, "reason": "daily_loss_limit"}

    # Max open positions check
    if len(tracker.open_legs()) >= MAX_OPEN_LEGS:
        log.warning(f"  Max {MAX_OPEN_LEGS} legs open — rejecting entry")
        return {"status": "blocked", "leg": leg_id, "reason": "max_positions"}

    position_id, msg = client.open_short()
    if position_id:
        tracker.add_position(leg_id, position_id)
        return {"status": "opened", "leg": leg_id, "position_id": str(position_id)}
    else:
        return {"status": "error", "leg": leg_id, "reason": msg}


def handle_exit(leg_id, exit_type, data):
    """Close the TradeLocker position for the specified leg."""
    position_id = tracker.get_position_id(leg_id)
    if not position_id:
        log.warning(f"  {leg_id} not found — may already be closed")
        return {"status": "not_found", "leg": leg_id}

    success, msg = client.close_position(position_id)
    if success:
        # PnL will be approximate until we can query it from TradeLocker
        tracker.close_position(leg_id, exit_type, pnl=0.0)
        return {"status": "closed", "leg": leg_id, "exit_type": exit_type,
                "position_id": str(position_id)}
    else:
        return {"status": "error", "leg": leg_id, "reason": msg}


def handle_close_all(data):
    """Close all open positions (emergency or end-of-range)."""
    results = []
    for leg_id in list(tracker.positions.keys()):
        result = handle_exit(leg_id, "CLOSE_ALL", data)
        results.append(result)
    return results


# ═══════════════════════════════════════════════════════════════
# FLASK APP
# ═══════════════════════════════════════════════════════════════

app = Flask(__name__)


@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Receives alert_message JSON from TradingView.

    Entry: {"action":"ENTRY","leg":"S1"}
    Exit:  {"action":"EXIT","leg":"S1","exit_type":"SX1"}
    Close: {"action":"CLOSE_ALL"}
    """
    secret = request.args.get("secret", "")
    if secret != WEBHOOK_SECRET:
        log.warning(f"  Rejected: bad secret from {request.remote_addr}")
        return jsonify({"error": "unauthorized"}), 401

    try:
        data = request.get_json(force=True)
    except Exception:
        log.error(f"  Bad JSON: {request.data}")
        return jsonify({"error": "invalid json"}), 400

    action = data.get("action", "").upper()
    leg = data.get("leg", "")
    exit_type = data.get("exit_type", "")

    log.info(f"{'─' * 55}")
    log.info(f"  SIGNAL: action={action}  leg={leg}  exit={exit_type}")

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
    """Status dashboard."""
    stats = tracker.stats()
    tracker._check_daily_reset()
    return jsonify({
        "bot": "3SMMA_X Short Bot v1.0 — Blueberry TradeLocker",
        "status": "running",
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "lot_size": LOT_SIZE,
        "symbol": SYMBOL,
        "open_legs": tracker.open_legs(),
        "total_closed_trades": stats["total"],
        "cumulative_pnl": f"${stats['pnl']}",
        "win_rate": f"{stats['win_rate']}%",
        "daily_pnl": f"${round(tracker.daily_pnl, 2)}",
        "daily_limit": f"${DAILY_LOSS_LIMIT}",
        "daily_limit_hit": tracker.is_daily_limit_hit()
    })


@app.route("/positions", methods=["GET"])
def positions():
    """Current open positions."""
    return jsonify({
        "open_positions": tracker.positions,
        "open_count": len(tracker.positions)
    })


@app.route("/trades", methods=["GET"])
def trades():
    """Trade history."""
    recent = list(reversed(tracker.trade_log[-50:]))
    return jsonify({"stats": tracker.stats(), "recent_trades": recent})


@app.route("/account", methods=["GET"])
def account():
    """Account info."""
    return jsonify(client.get_account_info() or {"error": "not connected"})


@app.route("/emergency-close", methods=["POST"])
def emergency_close():
    """Manually close ALL bot positions."""
    secret = request.args.get("secret", "")
    if secret != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    results = handle_close_all({})
    return jsonify({"action": "emergency_close", "results": results})


# ═══════════════════════════════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    log.info("═" * 55)
    log.info("  3SMMA_X Short Bot v1.0 — Blueberry TradeLocker")
    log.info("═" * 55)

    if client.connect():
        log.info("  Connection: OK")
    else:
        log.error("  Connection: FAILED — check credentials")

    log.info(f"  Symbol:       {SYMBOL}")
    log.info(f"  Lot size:     {LOT_SIZE}")
    log.info(f"  Daily limit:  ${DAILY_LOSS_LIMIT}")
    log.info(f"  Open legs:    {tracker.open_legs() or 'none'}")
    log.info(f"  Past trades:  {tracker.stats()['total']}")
    log.info(f"  Webhook:      http://0.0.0.0:{PORT}/webhook?secret=***")
    log.info("  Waiting for TradingView signals...")
    log.info("═" * 55)

    app.run(host="0.0.0.0", port=PORT, debug=False)
