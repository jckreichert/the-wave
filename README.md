# One-Time Market + Macro Tracker (Deep-Dive Version)

This is a **single-run** (“once version”) market intelligence snapshot that now goes deeper with:

- **Multiple market APIs/providers** (Stooq primary, Yahoo fallback, synthetic deterministic fallback)
- **Broader coverage** (major indices, commodities, sectors, plus credit/rates/USD proxies)
- **Richer KPIs** (trend, momentum, volatility, drawdown, RSI, MACD, ATR, Sharpe, beta/correlation)
- **Macro analysis** (market-implied macro regime + optional direct FRED macro series)
- **Macro sensitivity mapping** (each asset’s rolling correlation to macro factors)

## Coverage

### Core requested assets

- S&P 500, Dow Jones, Russell 2000
- Gold, Silver
- Major industries/sectors (consumer, energy, tech, healthcare, financials, etc.)

### Additional context assets

- Nasdaq-100 (`QQQ`)
- Crude oil proxy (`USO`)
- High-yield credit (`HYG`)
- Long duration rates proxy (`TLT`)
- U.S. dollar proxy (`UUP`)

## KPIs calculated

- Returns: 1-week, 1-month, 3-month, YTD
- Trend: % above/below 20/50/200-day moving averages
- Momentum: 20-day and 60-day
- Technicals: RSI(14), MACD line/signal/histogram, ATR(14)%
- Risk: annualized vol (20d/60d), downside vol, max drawdown (1y)
- Risk-adjusted: Sharpe(60d) with configurable risk-free rate
- Relative risk: beta and correlation vs S&P 500 (120d)
- Macro sensitivity: rolling correlation to key macro factor proxies

## Macro overlay (deeper analysis)

### Market-implied factors

- **Rates duration**: `TLT`
- **USD strength**: `UUP`
- **Oil growth/inflation pressure**: `USO`
- **Credit risk appetite**: `HYG`

The script computes a market-implied regime:

- `RISK_ON`
- `NEUTRAL`
- `RISK_OFF`

and reports inflation pressure and growth/risk-appetite scores.

### Optional direct macro API (FRED)

The script attempts to fetch latest:

- Fed Funds
- CPI
- Unemployment rate
- U.S. 10Y yield

Set `FRED_API_KEY` to improve reliability where required by endpoint policy.

## Run

```bash
python3 stock_tracker_once.py --output-dir data
```

Offline deterministic mode:

```bash
python3 stock_tracker_once.py --offline-only --output-dir data
```

Custom risk-free rate for Sharpe KPI:

```bash
python3 stock_tracker_once.py --risk-free-rate 0.05 --output-dir data
```

## Outputs

- `data/market_snapshot_<unix_ts>.json`
- `data/market_snapshot_<unix_ts>.csv`
- `data/latest_snapshot.json`
- `data/latest_snapshot.csv`

## Notes

- In restricted environments (e.g., outbound market API blocked), the tool auto-falls back to synthetic deterministic series and still computes full KPI + macro structure.
- This version still runs once per command (no scheduler included).
