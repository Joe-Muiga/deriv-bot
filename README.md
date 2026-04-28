# SIFM Deriv Trading Bot

Autonomous 24/7 trading bot implementing the **Structural-Indicator Fusion Model (SIFM)**
on the Deriv platform, deployed on Render's free tier.

---

## Architecture

```
main.py
├── Flask web server           → health endpoint + Render keep-alive
├── Bot engine (async thread)
│   ├── DerivClient            → WebSocket API (ticks, candles, buy/sell)
│   ├── CandlestickBuilder     → tick → OHLCV conversion
│   ├── SMCAnalyzer (Phase A)  → OBs, FVGs, liquidity, HTF bias
│   ├── SignalEngine (Phase B)
│   │   ├── Module 1: MTFA + RSI divergence
│   │   ├── Module 2: Candlestick confluence (Bulkowski patterns)
│   │   └── Module 3: 7-indicator quantitative vote
│   └── RiskManager (Phase C)  → 9% daily loss limit, 1% position sizing
└── NewsFilter                 → blocks trades 30 min before high-impact events
```

---

## Quick Start

### 1. Get your Deriv credentials

| What | Where |
|------|-------|
| API Token (trade + read) | https://app.deriv.com/account/api-token |
| App ID | https://api.deriv.com/app-registration (or use `1089` for testing) |

### 2. Local testing

```bash
git clone <your-repo>
cd deriv_trading_bot

cp .env.example .env
# Edit .env and add your DERIV_API_TOKEN

pip install -r requirements.txt
python main.py
```

Visit http://localhost:8080 to see the bot dashboard.
Visit http://localhost:8080/stats for live P&L.

### 3. Deploy to Render

1. Push this repo to GitHub.
2. Go to https://dashboard.render.com → **New → Web Service**.
3. Connect your GitHub repo.
4. Render will auto-detect `render.yaml`.
5. In **Environment Variables**, add:
   - `DERIV_API_TOKEN` = your token
   - `DERIV_APP_ID`    = your app ID
6. Click **Deploy**.

The bot self-pings `/health` every 40 seconds to prevent Render's free-tier sleep.

---

## Risk Management

| Rule | Value |
|------|-------|
| Daily loss limit | **9%** of that day's starting balance |
| Risk per trade | **1%** of current (compounded) balance |
| Max concurrent trades | 3 |
| Min stake | $0.35 (Deriv minimum) |
| Max stake | $500 (safety cap) |
| News blackout | 30 min before high-impact events |
| Volatility filter | Skip if ATR(LTF) > 2×ATR(HTF) |

When the 9% daily limit is hit, **trading pauses automatically** until UTC midnight, 
then resumes with the new day's opening balance as the reference.

---

## Compound Growth Math

With the 1% risk rule and SIFM's expected performance:
- Win rate: ~58–62%
- Average R:R: ~1.8
- Expected value per trade: `0.60 × 1.8% − 0.40 × 1% = +0.68%` per trade

Starting from **$1**:
| Milestone | Approx. trades needed |
|-----------|----------------------|
| $10 | ~340 |
| $100 | ~680 |
| $1,000 | ~1,020 |
| $10,000 | ~1,360 |

Past performance estimates do not guarantee future results.

---

## Monitored Assets

- **Forex**: EURUSD, GBPUSD, USDJPY, AUDUSD + 20 more pairs
- **Metals**: Gold (XAU), Silver (XAG), Palladium, Platinum
- **Crypto**: BTC, ETH, LTC, XRP, SOL, ADA + more
- **Indices**: S&P 500, NASDAQ, DAX, FTSE, Nikkei + more
- **Commodities**: WTI Oil, Brent, Natural Gas, Copper
- **Synthetics**: Volatility 10–100, Boom/Crash, Jump indices (24/7)

---

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Bot status JSON |
| `GET /health` | Health check (used by keep-alive pinger) |
| `GET /stats` | Detailed P&L and trade statistics |

---

## Files

```
main.py                 Entry point
keep_alive.py           Flask server + self-ping thread
config.py               All configuration (reads from env)
bot_engine.py           Main async trading loop
deriv_client.py         Deriv WebSocket API client
candlestick_builder.py  Tick → OHLCV conversion
smc_analyzer.py         SMC/ICT Phase A analysis
signal_engine.py        M1/M2/M3 signal modules (Phase B)
risk_manager.py         Phase C risk + position sizing
news_filter.py          Economic calendar news filter
indicators.py           RSI, MACD, BB, StochRSI, ADX, ATR
symbols.py              All Deriv symbol lists
requirements.txt        Python dependencies
render.yaml             Render deployment config
.env.example            Environment variable template
```

---

## Disclaimer

This bot trades real money. Algorithmic trading carries significant risk of loss.
The SIFM strategy's projected win rate (58–62%) and profit factor (1.3–1.7) are
engineering estimates based on back-tests – **not guarantees of future performance**.
Never risk more than you can afford to lose. Test on a demo account first.
