# 3SMMA_X Short Bot — Blueberry TradeLocker

## How it works

```
TradingView (3SMMA_X v5.3, 15m XAUUSD, Short Only)
    ↓ webhook fires on each order fill
Python bot (Railway.app — free hosting)
    ↓ REST API call
Blueberry TradeLocker (funded account, 0.01 lots)
```

No Windows VPS. No MT5 terminal. Just a free Railway deployment.

---

## Setup (5 steps)

### Step 1 — Deploy to Railway

1. Go to **railway.app** → sign up (free with GitHub)
2. New Project → Deploy from GitHub (or "Empty Project" → add files)
3. Upload these files: `bot.py`, `requirements.txt`, `Procfile`
4. Railway auto-detects Python and deploys

### Step 2 — Set environment variables in Railway

Go to your Railway project → Variables tab → add these:

```
TL_EMAIL       = your_tradelocker_email
TL_PASSWORD    = your_tradelocker_password
TL_SERVER      = BlueberryMarkets        (check exact name in TradeLocker login screen)
TL_ENV         = https://live.tradelocker.com
SYMBOL         = XAUUSD                  (check exact name in TradeLocker)
LOT_SIZE       = 0.01
DAILY_LOSS_LIMIT = 200
WEBHOOK_SECRET = 3smma_short_2026
```

Railway gives you a public URL like: `https://your-bot-abc123.up.railway.app`

### Step 3 — Modify Pine Script

Open 3SMMA_X v5.3 in TradingView Pine Editor.
Add `alert_message` parameters as shown in `PINE_SCRIPT_CHANGES.pine`.
Save. Verify backtester results are unchanged.

### Step 4 — Create TradingView Alert

1. Open 15m XAUUSD chart with 3SMMA_X loaded
2. Alert button → create alert:
   - **Condition**: 3SMMA_X
   - **Trigger**: "Order fills only"
   - **Webhook URL**: `https://your-bot-abc123.up.railway.app/webhook?secret=3smma_short_2026`
   - **Message**: leave empty
   - **Expiration**: Open-ended
3. Create

### Step 5 — Verify

Visit `https://your-bot-url/` in browser → should show bot status JSON.
Wait for next signal or test with a temporary config change.

---

## Monitoring (from your phone)

| URL | Shows |
|-----|-------|
| `/` | Bot status, open legs, PnL, daily limit status |
| `/positions` | Current open positions |
| `/trades` | Completed trade history with win rate |
| `/account` | TradeLocker connection status |

### Emergency close all positions:
```
curl -X POST "https://your-bot-url/emergency-close?secret=3smma_short_2026"
```

---

## Prop Firm Safety Features

**Daily loss circuit breaker**: If cumulative daily losses hit $200 (configurable),
the bot stops accepting new entries until midnight UTC. Existing positions continue
running — only new entries are blocked. This protects against breaching the
challenge's daily drawdown rule.

**Max open positions cap**: Defaults to 4 (one full S1-S4 set). Prevents
overlapping entries from stacking positions beyond what the margin can handle.

**Duplicate entry protection**: If TradingView sends the same leg signal twice
(can happen with alert misfires), the bot ignores the duplicate.

---

## Position sizing for challenge phases

| Phase | LOT_SIZE | Risk per entry | Purpose |
|-------|----------|----------------|---------|
| Challenge | 0.01 | ~$2-5 per 4 legs | Pass the evaluation |
| Funded | Increase gradually | Per risk rules | Production |

---

## Finding your TradeLocker credentials

**TL_SERVER**: Open TradeLocker → login screen → the server name dropdown.
Copy the exact text (e.g., "BlueberryMarkets" or "Blueberry-Live").

**SYMBOL**: Open TradeLocker → search for gold in the instrument list.
Could be "XAUUSD", "XAU/USD", "Gold", etc. Copy exact string.

**TL_EMAIL / TL_PASSWORD**: Same credentials you use to log into TradeLocker.

---

## Files

| File | Purpose |
|------|---------|
| `bot.py` | Webhook server + TradeLocker execution |
| `requirements.txt` | Python dependencies |
| `Procfile` | Railway deployment config |
| `PINE_SCRIPT_CHANGES.pine` | What to add to your Pine Script |
| `positions.json` | Auto-created — position state (survives restarts) |
| `bot.log` | Auto-created — full activity log |

---

## Troubleshooting

**"Symbol not found"**
→ Check exact symbol name in TradeLocker. Update SYMBOL env var.

**"Connection failed"**
→ Verify TL_EMAIL, TL_PASSWORD, TL_SERVER are correct.
→ Make sure TL_ENV is https://live.tradelocker.com for funded account.

**Webhook not firing**
→ TradingView alert must be "Order fills only" mode.
→ Check Railway logs for incoming requests.

**Railway deployment fails**
→ Check build logs. Most common: Python version mismatch.
→ Add a `runtime.txt` with `python-3.11.0` if needed.
