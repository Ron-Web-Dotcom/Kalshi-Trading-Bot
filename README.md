# Kalshi AI Trading Bot

Autonomous prediction market trading bot for [Kalshi](https://kalshi.com).  
Runs 24/7 on a VPS — paper trading by default, live trading when you're ready.

---

## Features

- **Real-time Kalshi market data** — fetches all open markets every 5 minutes
- **Arbitrage detection** — cross-market (Kalshi vs Polymarket) and internal mispricing
- **Claude AI decisions** — BUY / SELL / HOLD with confidence score and reasoning
- **Paper trading** — simulate trades with full PnL tracking before risking real money
- **Risk management** — daily loss limit, cooldowns, max size, sector caps
- **Auto-scaling** — trade size grows after profits, shrinks after losses
- **Discord alerts** — trade notifications, errors, and performance summaries
- **Systemd service** — runs automatically on boot, restarts on crash

---

## Process Flow Map

```
                    ┌─────────────────────────────────┐
                    │     🧠  BOT BRAIN  (bot.py)      │
                    │  Async event loop · 60s cycles   │
                    └──────────────┬──────────────────┘
                                   │
          ┌────────────────────────┼────────────────────────┐
          ▼                        ▼                         ▼
 ┌────────────────┐     ┌────────────────────┐    ┌──────────────────┐
 │  🟦 KALSHI API │     │  🟣 POLYMARKET API │    │  🌐 EXTERNAL DATA│
 │kalshi_client.py│     │polymarket_client.py│    │context_builder.py│
 ├────────────────┤     ├────────────────────┤    ├──────────────────┤
 │ Markets/Prices │     │  YES/NO prices     │    │ Sports scores    │
 │ YES/NO ask     │     │  Volume · Close    │    │ News / web search│
 │ Volume · Book  │     │  CLOB order book   │    │ Weather · YouTube │
 │ Balance · Auth │     │  Arb cross-check   │    │ Crypto/eq feeds  │
 └───────┬────────┘     └─────────┬──────────┘    └────────┬─────────┘
          └────────────────────────┼────────────────────────┘
                                   │
                                   ▼
                    ┌──────────────────────────────────┐
                    │  🔍 JUNK FILTER + DEDUP           │
                    │  junk_filter.py · external_markets│
                    │  ▸ Price window: 8¢ – 92¢        │
                    │  ▸ Today + Tomorrow only          │
                    │  ▸ Junk phrases blocked           │
                    │  ▸ Correlated positions blocked   │
                    │  ▸ Kalshi ↔ Poly arb check       │
                    └──────────────┬───────────────────┘
                                   │
                                   ▼
                    ┌──────────────────────────────────┐
                    │  ⚡ AI ENGINE                     │
                    │  decide.py · rule_engine.py      │
                    │  GPT-4o-mini · rule-engine fallback│
                    ├──────────────────────────────────┤
                    │  EV Calculation (Kelly criterion) │
                    │  Confidence score  (min 70%)     │
                    │  Decision: BUY / HOLD / SKIP     │
                    │  Side: YES or NO · Kelly size    │
                    └──────────────┬───────────────────┘
                                   │
                                   ▼
                    ┌──────────────────────────────────┐
                    │  🛡️  RISK GATE                    │
                    │  risk/manager.py · scaling.py    │
                    │  ▸ Daily loss limit              │
                    │  ▸ Max open positions (50)       │
                    │  ▸ Cooldown per ticker+platform  │
                    │  ▸ Kill switch                   │
                    │  ▸ Consecutive loss brake        │
                    │  ▸ Auto-scale up / down          │
                    └──────────────┬───────────────────┘
                                   │
                    ┌──────────────┴───────────────┐
                    ▼                               ▼
       ┌────────────────────┐         ┌─────────────────────┐
       │  📄 PAPER TRADER   │         │  🔴 LIVE TRADER      │
       │  paper_trader.py   │         │  live_trader.py      │
       │  poly_paper_trader │         │  (disabled by default│
       │  Simulated fills   │         │  LIVE_TRADING=false) │
       │  SQLite trade_logs │         │  RSA auth · Kalshi   │
       │  PnL tracking      │         │  Duplicate guard     │
       └────────────────────┘         └─────────────────────┘
                                   │
                                   ▼
          ┌────────────────────────────────────────────────┐
          │           BACKGROUND LOOPS (async, parallel)   │
          │  ⏰ Trade Cycle   · Every 60s                  │
          │  📍 Live Scanner  · Every 60s (10 live slots)  │
          │  📊 Position Tracker · P&L · Stop/Take · Reeval│
          │  💓 Heartbeat     · 3·9·15·21 ET (Discord)    │
          │  📋 Daily Summary · 6am & 6pm ET              │
          │  🌙 Sleep Mode    · 3am–5am ET (quiet hours)  │
          └──────────────────────┬─────────────────────────┘
                                   │
                                   ▼
          ┌────────────────────────────────────────────────┐
          │              DISCORD ALERTS (discord.py)       │
          │  📈 Trade Alert  · BUY executed, EV, size     │
          │  💓 Heartbeat    · Open bets, PnL, best picks │
          │  📋 Summary      · 6am/6pm win rate, all-time │
          │  ⚠️  Error Alert  · API failures, kill switch  │
          └──────────────────────┬─────────────────────────┘
                                   │
                                   ▼
          ┌────────────────────────────────────────────────┐
          │                   CI GUARDS                    │
          │  ✅ Ruff Lint          · every push            │
          │  🧪 21 Regression Tests · every push           │
          │  🔒 No secrets in git  · .env never committed  │
          └────────────────────────────────────────────────┘
```

---

## Data Source Tree

```
                         🤖 KALSHI AI TRADING BOT
                                    │
          ┌─────────────────────────┼──────────────────────────┐
          │                         │                           │
    ┌─────┴──────┐          ┌───────┴───────┐         ┌────────┴────────┐
    │ KALSHI API │          │ POLYMARKET API│         │  EXTERNAL DATA  │
    │(kalshi_    │          │(polymarket_   │         │(context_builder │
    │ client.py) │          │ client.py)    │         │    .py)         │
    └─────┬──────┘          └───────┬───────┘         └────────┬────────┘
          │                         │                           │
     ┌────┴────┐               ┌────┴────┐              ┌──────┴──────┐
     │ Markets │               │ Markets │              │   Sports    │
     │ Prices  │               │ Prices  │              │  Scores &   │
     │YES ask  │               │YES/NO   │              │   Odds      │
     │NO ask   │               │ (cents) │              │(SofaScore,  │
     │ Volume  │               │ Volume  │              │ ESPN, etc.) │
     │  Book   │               │  CLOB   │              └──────┬──────┘
     │ Balance │               │  Book   │                     │
     └────┬────┘               └────┬────┘              ┌──────┴──────┐
          │                         │                    │    News &   │
     ┌────┴────┐               ┌────┴────┐              │ Web Search  │
     │  Close  │               │  Close  │              │ (live facts)│
     │  time   │               │  time   │              └──────┬──────┘
     │  Status │               │  Status │                     │
     │  Auth   │               │  Arb    │              ┌──────┴──────┐
     └────┬────┘               └────┬────┘              │  Weather ·  │
          │                         │                    │  YouTube ·  │
          └────────────┬────────────┘                    │  Crypto/EQ  │
                       │                                 └──────┬──────┘
                       └───────────────────┬─────────────────────┘
                                           │
                          ┌────────────────┴──────────────────┐
                          │       PRE-FILTER + DEDUP           │
                          │  junk_filter.py · external_markets │
                          │  8¢–92¢ · today+tomorrow · dedup  │
                          └────────────────┬──────────────────┘
                                           │
                     ┌─────────────────────┼───────────────────────┐
                                        ▼
                              ┌──────────────────┐
                              │   GPT-4o-mini    │
                              │   (OpenAI)       │
                              └─────────┬────────┘
                                        │
                              ┌─────────┴──────────┐
                              │   RULE ENGINE       │
                              │   (fallback)        │
                              └─────────┬──────────┘
                                        │
                              ┌─────────┴──────────┐
                              │    RISK GATE        │
                              │  risk/manager.py   │
                              └─────────┬──────────┘
                                        │
                     ┌──────────────────┴──────────────────┐
                     ▼                                       ▼
           ┌──────────────────┐                  ┌──────────────────────┐
           │   PAPER TRADER   │                  │  DISCORD ALERTS      │
           │  (SQLite PnL)    │                  │  Trade · PnL · Error │
           └──────────────────┘                  └──────────────────────┘
```

---

## Architecture

```
bot.py                      ← main entry point (run this on VPS)
src/
  config/settings.py        ← all config from .env
  clients/kalshi_client.py  ← Kalshi API v2 (RSA auth)
  data/
    market_data.py          ← live market prices with real-time logging
    external_markets.py     ← Polymarket price comparison (read-only)
  strategy/arbitrage.py     ← arbitrage signal detection
  execution/paper_trader.py ← simulated trade execution + PnL tracking
  risk/
    manager.py              ← daily loss, cooldown, size, sector limits
    scaling.py              ← auto trade sizing on profit/loss milestones
  alerts/discord.py         ← Discord webhook notifications
  ai/decision.py            ← Claude AI BUY/SELL/HOLD engine
  jobs/
    ingest.py               ← fetch markets → SQLite database
    trade.py                ← orchestrate one full trading cycle
    track.py                ← monitor and close resolved positions
    evaluate.py             ← compute PnL, update scaler, send summaries
  utils/
    database.py             ← SQLite (trading_system.db)
    logging_setup.py        ← console + rotating file logs
deploy/
  install.sh                ← VPS one-command installer
  kalshi-bot.service        ← systemd unit file
  update.sh                 ← zero-downtime code update
  logrotate.conf            ← log rotation (30 days)
```

---

## VPS Deployment (Ubuntu 22.04 / Debian 12)

### 1. Provision a VPS

Any provider works. Minimum: 1 vCPU, 512 MB RAM, 10 GB disk.  
Recommended: Hetzner CX22, DigitalOcean Basic, or Vultr — ~$4–6/month.

### 2. SSH in and clone the repo

```bash
ssh root@YOUR_VPS_IP
git clone https://github.com/Ron-Web-Dotcom/Kalshi-Trading-Bot.git /opt/kalshi-bot
cd /opt/kalshi-bot
```

### 3. Run the one-command installer

```bash
bash deploy/install.sh
```

This will:
- Install Python 3 and system packages
- Create a `kalshi` system user (no login shell — more secure)
- Set up a Python virtual environment at `/opt/kalshi-bot/.venv`
- Install all dependencies
- Create `/opt/kalshi-bot/.env` from the template
- Register and enable the `kalshi-bot` systemd service

### 4. Add your API keys

```bash
nano /opt/kalshi-bot/.env
```

At minimum, fill in:

```env
# Kalshi — from https://trading.kalshi.com/settings/api
KALSHI_API_KEY_ID=your-key-id-uuid-here
KALSHI_PRIVATE_KEY_PATH=/opt/kalshi-bot/kalshi_private_key.pem
KALSHI_USE_DEMO=true          # keep true until ready for real money

# Discord alerts (optional but strongly recommended)
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR/WEBHOOK

# SAFETY: leave false until you've reviewed paper trade results
LIVE_TRADING_ENABLED=false
```

Upload your Kalshi RSA private key from your local machine:

```bash
scp ~/kalshi_private_key.pem root@YOUR_VPS_IP:/opt/kalshi-bot/
# Back on the VPS:
chmod 600 /opt/kalshi-bot/kalshi_private_key.pem
chown kalshi:kalshi /opt/kalshi-bot/kalshi_private_key.pem
```

### 5. Start the bot

```bash
systemctl start kalshi-bot
systemctl status kalshi-bot   # should show: active (running)
```

### 6. Watch the live logs

```bash
journalctl -u kalshi-bot -f
```

You should see market prices printing every 5 minutes, trading cycles every 60 seconds, and Discord alerts for any trades.

---

## Daily Operations

| Task | Command |
|---|---|
| Start | `systemctl start kalshi-bot` |
| Stop | `systemctl stop kalshi-bot` |
| Restart | `systemctl restart kalshi-bot` |
| Live logs | `journalctl -u kalshi-bot -f` |
| Last 200 lines | `journalctl -u kalshi-bot -n 200` |
| Portfolio status | `cd /opt/kalshi-bot && .venv/bin/python cli.py status` |
| Trade history | `cd /opt/kalshi-bot && .venv/bin/python cli.py history` |
| Category scores | `cd /opt/kalshi-bot && .venv/bin/python cli.py scores` |
| Health check | `cd /opt/kalshi-bot && .venv/bin/python cli.py health` |
| Update code | `bash /opt/kalshi-bot/deploy/update.sh` |
| Close all positions | `cd /opt/kalshi-bot && .venv/bin/python cli.py close-all --live` |

---

## Enabling Live Trading

Only do this after verifying paper trade results look profitable and reasonable.

1. Edit `/opt/kalshi-bot/.env`:
   ```env
   LIVE_TRADING_ENABLED=true
   KALSHI_USE_DEMO=false
   ```

2. Restart:
   ```bash
   systemctl restart kalshi-bot
   ```

3. Confirm live mode is active:
   ```bash
   journalctl -u kalshi-bot -n 20
   # Should print: LIVE TRADING MODE — REAL MONEY WILL BE USED
   ```

---

## Configuration Reference

All settings are in `/opt/kalshi-bot/.env`. Restart the bot after any change.

### Kalshi API
| Variable | Default | Description |
|---|---|---|
| `KALSHI_API_KEY_ID` | — | Your API key UUID from Kalshi settings |
| `KALSHI_PRIVATE_KEY_PATH` | `kalshi_private_key.pem` | Path to RSA private key file |
| `KALSHI_PRIVATE_KEY_PEM` | — | Inline PEM (alternative to file) |
| `KALSHI_USE_DEMO` | `true` | Use demo environment (no real money) |
| `LIVE_TRADING_ENABLED` | `false` | Enable real money trading |

### Trade Sizing
| Variable | Default | Description |
|---|---|---|
| `BASE_TRADE_SIZE` | `10.0` | Starting dollar size per trade |
| `MAX_TRADE_SIZE` | `100.0` | Hard ceiling on trade size |
| `MIN_TRADE_SIZE` | `1.0` | Hard floor on trade size |
| `MAX_POSITION_PCT` | `3.0` | Max % of portfolio in one position |

### Risk Management
| Variable | Default | Description |
|---|---|---|
| `MAX_DAILY_LOSS_PCT` | `10.0` | Pause trading if daily loss hits this % |
| `MAX_DRAWDOWN_PCT` | `15.0` | Portfolio drawdown circuit breaker |
| `MAX_SECTOR_EXPOSURE_PCT` | `30.0` | Max % of portfolio in one market category |
| `TRADE_COOLDOWN_SECONDS` | `30` | Min gap between trades on the same ticker |

### Arbitrage
| Variable | Default | Description |
|---|---|---|
| `ARBITRAGE_THRESHOLD_PCT` | `5.0` | Min price diff (%) to generate an arb signal |
| `OVERTRADE_COOLDOWN_MINUTES` | `5` | Min minutes between signals on same ticker |

### AI (GPT-4o-mini)
| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | — | Your OpenAI API key |
| `AI_MODEL` | `gpt-4o-mini` | OpenAI model to use |
| `MIN_AI_CONFIDENCE` | `70.0` | AI must be ≥ this % confident to trade |
| `AI_ENABLED` | `true` | Disable to use rule-based fallback only |
| `DAILY_AI_BUDGET` | `10.0` | Max OpenAI API spend per day (USD) |

### Auto-Scaling
| Variable | Default | Description |
|---|---|---|
| `ENABLE_AUTO_SCALING` | `true` | Dynamically adjust trade size |
| `SCALE_UP_MILESTONE` | `50.0` | Profit $ milestone → increase size 25% |
| `SCALE_UP_FACTOR` | `1.25` | Multiplier when scaling up |
| `SCALE_DOWN_MILESTONE` | `25.0` | Loss $ milestone → decrease size 20% |
| `SCALE_DOWN_FACTOR` | `0.8` | Multiplier when scaling down |

### Alerts
| Variable | Default | Description |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | — | Full Discord webhook URL |
| `ALERT_ON_TRADE` | `true` | Alert when a trade executes |
| `ALERT_ON_SIGNAL` | `false` | Alert on every detected signal |
| `ALERT_ON_ERROR` | `true` | Alert on bot errors |

### Logging
| Variable | Default | Description |
|---|---|---|
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `ENABLE_FILE_LOGGING` | `true` | Write logs to `logs/` directory |
| `LOG_DIR` | `logs` | Directory for log files |

---

## Database

All data is in `/opt/kalshi-bot/trading_system.db` (SQLite, no external DB needed).

| Table | Contents |
|---|---|
| `markets` | Cached Kalshi market universe (refreshed every 5 min) |
| `positions` | Open and closed positions with mark-to-market PnL |
| `trade_logs` | Every paper and live trade with AI reasoning |
| `paper_signals` | AI + arbitrage signals with eventual outcomes |
| `ai_decisions` | Full Claude response log including cost per call |
| `performance_metrics` | Historical PnL snapshots |

**Backup:**
```bash
cp /opt/kalshi-bot/trading_system.db /opt/kalshi-bot/trading_system.db.bak
```

---

## Log Rotation

Prevent the logs directory from filling your disk:

```bash
cp /opt/kalshi-bot/deploy/logrotate.conf /etc/logrotate.d/kalshi-bot
```

Rotates daily, keeps 30 days, compresses old files automatically.

---

## How It Decides to Trade

Every 60 seconds the bot runs one cycle:

1. **Load markets** from the database (background ingestion refreshes every 5 min)
2. **Compare prices** with Polymarket — flag any > `ARBITRAGE_THRESHOLD_PCT` difference
3. **Detect internal arb** — YES + NO ask < 100¢ on the same market
4. **AI analysis** — top markets sent to Claude with prices, volume, and signals
5. **Confidence gate** — Claude must return BUY/SELL with ≥ `MIN_AI_CONFIDENCE`
6. **Risk gate** — validates cooldown, daily loss limit, sector exposure, size limits
7. **Execute** — paper trade (log to DB) or live Kalshi order
8. **Track** — every 2 min, check resolved markets and close positions
9. **Evaluate** — every 5 min, compute PnL, update scale factor, Discord every 10 trades

---

## Troubleshooting

**Bot won't start**
```bash
journalctl -u kalshi-bot -n 100
cd /opt/kalshi-bot && .venv/bin/python cli.py health
```

**403 Forbidden from Kalshi**
- `KALSHI_USE_DEMO=true` but key was created on production (or vice versa)
- Private key file isn't readable: `ls -la /opt/kalshi-bot/kalshi_private_key.pem`

**401 Unauthorized from Kalshi**
- Key ID and private key don't match — re-download the pair from Kalshi settings

**AI always returns HOLD**
- Verify `OPENAI_API_KEY` is set correctly
- Check OpenAI credits at https://platform.openai.com/usage
- Lower `MIN_AI_CONFIDENCE` to 60 and watch logs

**Discord not receiving alerts**
```bash
cd /opt/kalshi-bot
.venv/bin/python -c "
import asyncio
from src.alerts.discord import DiscordAlerter
asyncio.run(DiscordAlerter().error_alert('Bot connectivity test'))
"
```

**High Claude API costs**
- Lower `DAILY_AI_BUDGET` to reduce spending
- Set `AI_ENABLED=false` to use rule-based logic only (free)
