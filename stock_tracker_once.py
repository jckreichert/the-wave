#!/usr/bin/env python3
"""One-shot market + macro snapshot generator.

This script creates a single comprehensive market snapshot with:
- Multi-provider market data ingestion (Stooq + Yahoo Finance fallback)
- Broad asset coverage (indices, commodities, sectors, risk/style proxies)
- Rich KPI computation (trend, momentum, volatility, drawdown, RSI, MACD, beta, Sharpe)
- Macro overlay via market-implied factors + optional FRED macro API
- Asset-to-macro sensitivity analysis (rolling correlations)
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List

LOOKBACK_DAYS = 500
TRADING_DAYS_PER_YEAR = 252
SENSITIVITY_WINDOW = 120

# Core market assets requested + expanded set for deeper context.
ASSETS = {
    "SP500": {"stooq": "spy.us", "yahoo": "SPY"},
    "DOW_JONES": {"stooq": "dia.us", "yahoo": "DIA"},
    "RUSSELL_2000": {"stooq": "iwm.us", "yahoo": "IWM"},
    "NASDAQ_100": {"stooq": "qqq.us", "yahoo": "QQQ"},
    "GOLD": {"stooq": "gld.us", "yahoo": "GLD"},
    "SILVER": {"stooq": "slv.us", "yahoo": "SLV"},
    "CRUDE_OIL": {"stooq": "uso.us", "yahoo": "USO"},
    "CONSUMER_DISCRETIONARY": {"stooq": "xly.us", "yahoo": "XLY"},
    "CONSUMER_STAPLES": {"stooq": "xlp.us", "yahoo": "XLP"},
    "ENERGY": {"stooq": "xle.us", "yahoo": "XLE"},
    "FINANCIALS": {"stooq": "xlf.us", "yahoo": "XLF"},
    "HEALTH_CARE": {"stooq": "xlv.us", "yahoo": "XLV"},
    "INDUSTRIALS": {"stooq": "xli.us", "yahoo": "XLI"},
    "MATERIALS": {"stooq": "xlb.us", "yahoo": "XLB"},
    "REAL_ESTATE": {"stooq": "xlre.us", "yahoo": "XLRE"},
    "TECH": {"stooq": "xlk.us", "yahoo": "XLK"},
    "UTILITIES": {"stooq": "xlu.us", "yahoo": "XLU"},
    "COMM_SERVICES": {"stooq": "xlc.us", "yahoo": "XLC"},
    "HIGH_YIELD_CREDIT": {"stooq": "hyg.us", "yahoo": "HYG"},
    "LONG_TREASURY": {"stooq": "tlt.us", "yahoo": "TLT"},
    "USD_INDEX_PROXY": {"stooq": "uup.us", "yahoo": "UUP"},
}

# Market-implied macro factors (tradable proxies).
MACRO_FACTORS = {
    "RATES_DURATION": {
        "description": "Duration proxy; rising TLT implies falling long rates",
        "asset_key": "LONG_TREASURY",
    },
    "USD_STRENGTH": {
        "description": "US dollar strength proxy (UUP)",
        "asset_key": "USD_INDEX_PROXY",
    },
    "OIL_GROWTH_INFLATION": {
        "description": "Oil demand/inflation pressure proxy (USO)",
        "asset_key": "CRUDE_OIL",
    },
    "CREDIT_RISK": {
        "description": "Credit appetite proxy (HYG)",
        "asset_key": "HIGH_YIELD_CREDIT",
    },
}

# Optional direct macro API via FRED (can run without key if your endpoint policy permits).
FRED_SERIES = {
    "FED_FUNDS_RATE": "FEDFUNDS",
    "CPI_YOY": "CPIAUCSL",
    "UNEMPLOYMENT_RATE": "UNRATE",
    "US10Y_YIELD": "DGS10",
}


@dataclass
class PriceBar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Snapshot:
    asset: str
    source_used: str
    last_date: str
    last_close: float
    return_1w_pct: float | None
    return_1m_pct: float | None
    return_3m_pct: float | None
    return_ytd_pct: float | None
    trend_vs_sma20_pct: float | None
    trend_vs_sma50_pct: float | None
    trend_vs_sma200_pct: float | None
    momentum_20d_pct: float | None
    momentum_60d_pct: float | None
    rsi14: float | None
    macd_line: float | None
    macd_signal: float | None
    macd_hist: float | None
    atr14_pct: float | None
    volatility_20d_ann_pct: float | None
    volatility_60d_ann_pct: float | None
    downside_vol_60d_ann_pct: float | None
    sharpe_60d: float | None
    max_drawdown_1y_pct: float | None
    beta_to_sp500_120d: float | None
    corr_to_sp500_120d: float | None
    macro_sensitivity: dict[str, float]


def _safe_float(value: str | int | float | None) -> float | None:
    if value in (None, "", "N/A", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def pct_change(newer: float, older: float) -> float:
    return (newer / older - 1.0) * 100.0


def mean(values: Iterable[float]) -> float:
    vals = list(values)
    return sum(vals) / len(vals)


def stooq_csv_url(symbol: str) -> str:
    return f"https://stooq.com/q/d/l/?s={symbol}&i=d"


def parse_history_stooq(csv_text: str) -> List[PriceBar]:
    rows: List[PriceBar] = []
    reader = csv.DictReader(csv_text.splitlines())
    for row in reader:
        date_str = row.get("Date", "")
        open_ = _safe_float(row.get("Open"))
        high = _safe_float(row.get("High"))
        low = _safe_float(row.get("Low"))
        close = _safe_float(row.get("Close"))
        volume = _safe_float(row.get("Volume")) or 0.0
        if not date_str or close is None:
            continue
        o = open_ if open_ is not None else close
        h = high if high is not None else max(o, close)
        l = low if low is not None else min(o, close)
        rows.append(PriceBar(date=date_str, open=o, high=h, low=l, close=close, volume=volume))
    rows.sort(key=lambda x: x.date)
    return rows[-LOOKBACK_DAYS:]


def fetch_history_stooq(symbol: str, timeout_s: float = 15.0) -> List[PriceBar]:
    req = urllib.request.Request(stooq_csv_url(symbol), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout_s) as response:
        payload = response.read().decode("utf-8", errors="replace")
    history = parse_history_stooq(payload)
    if not history:
        raise ValueError(f"No rows parsed for Stooq symbol {symbol}")
    return history


def yahoo_chart_url(symbol: str) -> str:
    encoded = urllib.parse.quote(symbol)
    return (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}"
        "?range=2y&interval=1d&events=history"
    )


def fetch_history_yahoo(symbol: str, timeout_s: float = 15.0) -> List[PriceBar]:
    req = urllib.request.Request(yahoo_chart_url(symbol), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout_s) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))

    result = payload.get("chart", {}).get("result", [])
    if not result:
        raise ValueError(f"Yahoo missing chart result for {symbol}")

    series = result[0]
    timestamps = series.get("timestamp") or []
    quote = (series.get("indicators", {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []

    rows: List[PriceBar] = []
    n = min(len(timestamps), len(closes), len(opens), len(highs), len(lows), len(volumes))
    for i in range(n):
        close = closes[i]
        open_ = opens[i]
        high = highs[i]
        low = lows[i]
        if close is None or open_ is None or high is None or low is None:
            continue
        date_str = datetime.fromtimestamp(timestamps[i], tz=timezone.utc).strftime("%Y-%m-%d")
        rows.append(
            PriceBar(
                date=date_str,
                open=float(open_),
                high=float(high),
                low=float(low),
                close=float(close),
                volume=float(volumes[i] or 0.0),
            )
        )
    if not rows:
        raise ValueError(f"No rows parsed for Yahoo symbol {symbol}")
    rows.sort(key=lambda x: x.date)
    return rows[-LOOKBACK_DAYS:]


def synthetic_history(seed_text: str, days: int = 320) -> List[PriceBar]:
    """Deterministic offline fallback if network data is unavailable."""
    rng = random.Random(seed_text)
    price = 100.0 + rng.uniform(-15.0, 15.0)
    rows: List[PriceBar] = []
    now = int(time.time())
    for i in range(days):
        ret = 0.0002 + rng.gauss(0.0, 0.011)
        prev = price
        price *= 1.0 + ret
        high = max(prev, price) * (1.0 + abs(rng.gauss(0, 0.003)))
        low = min(prev, price) * (1.0 - abs(rng.gauss(0, 0.003)))
        day = datetime.fromtimestamp(now - (days - i) * 86400, tz=timezone.utc).strftime("%Y-%m-%d")
        rows.append(
            PriceBar(
                date=day,
                open=round(prev, 4),
                high=round(high, 4),
                low=round(low, 4),
                close=round(price, 4),
                volume=float(rng.randint(100_000, 10_000_000)),
            )
        )
    return rows


def ema(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(values: List[float], period: int = 14) -> float | None:
    if len(values) < period + 1:
        return None
    gains: List[float] = []
    losses: List[float] = []
    for i in range(-period, 0):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = mean(gains)
    avg_loss = mean(losses)
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def true_range(curr: PriceBar, prev_close: float) -> float:
    return max(curr.high - curr.low, abs(curr.high - prev_close), abs(curr.low - prev_close))


def rolling_corr(a: List[float], b: List[float]) -> float | None:
    if len(a) != len(b) or len(a) < 3:
        return None
    if statistics.pstdev(a) == 0 or statistics.pstdev(b) == 0:
        return None
    return statistics.correlation(a, b)


def beta(asset_rets: List[float], bench_rets: List[float]) -> float | None:
    if len(asset_rets) != len(bench_rets) or len(asset_rets) < 3:
        return None
    mean_asset = mean(asset_rets)
    mean_bench = mean(bench_rets)
    cov = sum((a - mean_asset) * (b - mean_bench) for a, b in zip(asset_rets, bench_rets)) / (len(asset_rets) - 1)
    var_bench = statistics.variance(bench_rets)
    if var_bench == 0:
        return None
    return cov / var_bench


def ytd_return(dates: List[str], closes: List[float]) -> float | None:
    if not dates or not closes:
        return None
    year = dates[-1][:4]
    candidates = [i for i, d in enumerate(dates) if d.startswith(year)]
    if not candidates:
        return None
    first_idx = candidates[0]
    if first_idx >= len(closes):
        return None
    return pct_change(closes[-1], closes[first_idx])


def max_drawdown_pct(closes: List[float], window: int = 252) -> float | None:
    if len(closes) < 2:
        return None
    segment = closes[-window:] if len(closes) > window else closes
    peak = segment[0]
    mdd = 0.0
    for c in segment:
        peak = max(peak, c)
        dd = (c / peak - 1.0) * 100.0
        mdd = min(mdd, dd)
    return mdd


def returns(closes: List[float]) -> List[float]:
    return [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]


def fetch_optional_fred_series(timeout_s: float = 15.0) -> tuple[dict[str, float], list[str]]:
    """Attempt to fetch latest macro values from FRED API.

    Uses FRED_API_KEY if present. If unavailable/blocked, returns empty with warnings.
    """
    api_key = os.environ.get("FRED_API_KEY", "")
    warnings: list[str] = []
    out: dict[str, float] = {}

    for label, series_id in FRED_SERIES.items():
        params = {
            "series_id": series_id,
            "file_type": "json",
            "sort_order": "desc",
            "limit": "1",
        }
        if api_key:
            params["api_key"] = api_key
        url = "https://api.stlouisfed.org/fred/series/observations?" + urllib.parse.urlencode(params)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout_s) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
            obs = payload.get("observations", [])
            if not obs:
                warnings.append(f"FRED {series_id}: no observations")
                continue
            value = _safe_float(obs[0].get("value"))
            if value is None:
                warnings.append(f"FRED {series_id}: latest value missing")
                continue
            out[label] = value
        except Exception as exc:
            warnings.append(f"FRED {series_id}: {exc}")
    return out, warnings


def compute_macro_regime(macro_factor_returns: Dict[str, List[float]]) -> dict:
    def last_momentum(rets: List[float], window: int) -> float:
        if len(rets) < window:
            return 0.0
        gross = 1.0
        for r in rets[-window:]:
            gross *= 1.0 + r
        return (gross - 1.0) * 100.0

    usd = last_momentum(macro_factor_returns.get("USD_STRENGTH", []), 20)
    oil = last_momentum(macro_factor_returns.get("OIL_GROWTH_INFLATION", []), 20)
    credit = last_momentum(macro_factor_returns.get("CREDIT_RISK", []), 20)
    duration = last_momentum(macro_factor_returns.get("RATES_DURATION", []), 20)

    inflation_pressure_score = oil - duration - 0.5 * usd
    growth_risk_appetite_score = credit + 0.5 * oil - 0.5 * usd

    if growth_risk_appetite_score > 2.0:
        regime = "RISK_ON"
    elif growth_risk_appetite_score < -2.0:
        regime = "RISK_OFF"
    else:
        regime = "NEUTRAL"

    return {
        "regime": regime,
        "growth_risk_appetite_score": round(growth_risk_appetite_score, 3),
        "inflation_pressure_score": round(inflation_pressure_score, 3),
        "drivers_20d_momentum_pct": {
            "USD_STRENGTH": round(usd, 3),
            "OIL_GROWTH_INFLATION": round(oil, 3),
            "CREDIT_RISK": round(credit, 3),
            "RATES_DURATION": round(duration, 3),
        },
    }


def compute_snapshot(
    asset: str,
    source_used: str,
    bars: List[PriceBar],
    benchmark_returns_120d: List[float] | None,
    macro_factor_returns: Dict[str, List[float]],
    risk_free_rate_annual: float,
) -> Snapshot:
    if len(bars) < 220:
        raise ValueError(f"Need at least 220 rows for robust KPI set; got {len(bars)}")

    dates = [b.date for b in bars]
    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]

    ret = returns(closes)
    r20 = ret[-20:] if len(ret) >= 20 else []
    r60 = ret[-60:] if len(ret) >= 60 else []
    r120 = ret[-SENSITIVITY_WINDOW:] if len(ret) >= SENSITIVITY_WINDOW else []

    sma20 = mean(closes[-20:])
    sma50 = mean(closes[-50:])
    sma200 = mean(closes[-200:])

    macd_fast = ema(closes, 12)
    macd_slow = ema(closes, 26)
    macd_line_series = [a - b for a, b in zip(macd_fast, macd_slow)]
    macd_signal_series = ema(macd_line_series, 9)

    trs: List[float] = []
    for i in range(1, len(bars)):
        trs.append(true_range(bars[i], bars[i - 1].close))
    atr14 = mean(trs[-14:]) if len(trs) >= 14 else None

    down_rets = [x for x in r60 if x < 0]
    vol20 = statistics.stdev(r20) * math.sqrt(TRADING_DAYS_PER_YEAR) * 100.0 if len(r20) >= 2 else None
    vol60 = statistics.stdev(r60) * math.sqrt(TRADING_DAYS_PER_YEAR) * 100.0 if len(r60) >= 2 else None
    downside60 = (
        statistics.stdev(down_rets) * math.sqrt(TRADING_DAYS_PER_YEAR) * 100.0 if len(down_rets) >= 2 else None
    )

    rf_daily = risk_free_rate_annual / TRADING_DAYS_PER_YEAR
    excess60 = [x - rf_daily for x in r60]
    sharpe60 = None
    if len(excess60) >= 2 and statistics.stdev(excess60) > 0:
        sharpe60 = mean(excess60) / statistics.stdev(excess60) * math.sqrt(TRADING_DAYS_PER_YEAR)

    beta_120 = beta(r120, benchmark_returns_120d) if benchmark_returns_120d and len(r120) == len(benchmark_returns_120d) else None
    corr_120 = (
        rolling_corr(r120, benchmark_returns_120d)
        if benchmark_returns_120d and len(r120) == len(benchmark_returns_120d)
        else None
    )

    sensitivities: dict[str, float] = {}
    for factor_name, frets in macro_factor_returns.items():
        if len(r120) >= SENSITIVITY_WINDOW and len(frets) >= SENSITIVITY_WINDOW:
            c = rolling_corr(r120, frets[-SENSITIVITY_WINDOW:])
            if c is not None:
                sensitivities[factor_name] = round(c, 3)

    return Snapshot(
        asset=asset,
        source_used=source_used,
        last_date=dates[-1],
        last_close=round(closes[-1], 4),
        return_1w_pct=round(pct_change(closes[-1], closes[-6]), 3),
        return_1m_pct=round(pct_change(closes[-1], closes[-22]), 3),
        return_3m_pct=round(pct_change(closes[-1], closes[-63]), 3),
        return_ytd_pct=round(ytd_return(dates, closes), 3) if ytd_return(dates, closes) is not None else None,
        trend_vs_sma20_pct=round(pct_change(closes[-1], sma20), 3),
        trend_vs_sma50_pct=round(pct_change(closes[-1], sma50), 3),
        trend_vs_sma200_pct=round(pct_change(closes[-1], sma200), 3),
        momentum_20d_pct=round(pct_change(closes[-1], closes[-21]), 3),
        momentum_60d_pct=round(pct_change(closes[-1], closes[-61]), 3),
        rsi14=round(rsi(closes, 14), 3) if rsi(closes, 14) is not None else None,
        macd_line=round(macd_line_series[-1], 4) if macd_line_series else None,
        macd_signal=round(macd_signal_series[-1], 4) if macd_signal_series else None,
        macd_hist=round(macd_line_series[-1] - macd_signal_series[-1], 4)
        if macd_line_series and macd_signal_series
        else None,
        atr14_pct=round((atr14 / closes[-1]) * 100.0, 3) if atr14 else None,
        volatility_20d_ann_pct=round(vol20, 3) if vol20 is not None else None,
        volatility_60d_ann_pct=round(vol60, 3) if vol60 is not None else None,
        downside_vol_60d_ann_pct=round(downside60, 3) if downside60 is not None else None,
        sharpe_60d=round(sharpe60, 3) if sharpe60 is not None else None,
        max_drawdown_1y_pct=round(max_drawdown_pct(closes, 252), 3) if max_drawdown_pct(closes, 252) is not None else None,
        beta_to_sp500_120d=round(beta_120, 3) if beta_120 is not None else None,
        corr_to_sp500_120d=round(corr_120, 3) if corr_120 is not None else None,
        macro_sensitivity=sensitivities,
    )


def fetch_asset_history(symbol_map: dict, offline_only: bool) -> tuple[List[PriceBar], str, str | None]:
    """Return bars, source_used, warning."""
    if offline_only:
        return synthetic_history(str(symbol_map)), "synthetic", None

    warnings: list[str] = []

    stooq_symbol = symbol_map.get("stooq")
    if stooq_symbol:
        try:
            return fetch_history_stooq(stooq_symbol), "stooq", None
        except Exception as exc:
            warnings.append(f"stooq({stooq_symbol}): {exc}")

    yahoo_symbol = symbol_map.get("yahoo")
    if yahoo_symbol:
        try:
            return fetch_history_yahoo(yahoo_symbol), "yahoo", None
        except Exception as exc:
            warnings.append(f"yahoo({yahoo_symbol}): {exc}")

    return synthetic_history(str(symbol_map)), "synthetic", "; ".join(warnings) if warnings else "live providers unavailable"


def write_outputs(payload: dict, snapshots: list[Snapshot], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())

    json_path = output_dir / f"market_snapshot_{ts}.json"
    csv_path = output_dir / f"market_snapshot_{ts}.csv"

    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(snapshots[0]).keys()))
        writer.writeheader()
        for snap in snapshots:
            row = asdict(snap)
            row["macro_sensitivity"] = json.dumps(row["macro_sensitivity"], separators=(",", ":"))
            writer.writerow(row)

    (output_dir / "latest_snapshot.json").write_text(json_path.read_text(encoding="utf-8"), encoding="utf-8")
    (output_dir / "latest_snapshot.csv").write_text(csv_path.read_text(encoding="utf-8"), encoding="utf-8")
    return json_path, csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a one-time market + macro snapshot")
    parser.add_argument("--output-dir", default="data", help="Directory for JSON/CSV output")
    parser.add_argument("--offline-only", action="store_true", help="Use synthetic deterministic data only")
    parser.add_argument(
        "--risk-free-rate",
        type=float,
        default=0.045,
        help="Annualized risk free rate (decimal) for Sharpe calculations, default 0.045",
    )
    args = parser.parse_args()

    warnings: list[str] = []
    history_by_asset: dict[str, List[PriceBar]] = {}
    source_by_asset: dict[str, str] = {}

    for asset, symbols in ASSETS.items():
        bars, source, warn = fetch_asset_history(symbols, args.offline_only)
        history_by_asset[asset] = bars
        source_by_asset[asset] = source
        if warn:
            warnings.append(f"{asset}: {warn}; using {source}")

    # Build macro factor returns from fetched market proxies.
    macro_factor_returns: dict[str, List[float]] = {}
    for factor_name, cfg in MACRO_FACTORS.items():
        key = cfg["asset_key"]
        bars = history_by_asset.get(key, [])
        if bars:
            macro_factor_returns[factor_name] = returns([b.close for b in bars])

    # Benchmark = SP500 proxy for beta/correlation.
    benchmark_returns_120d = None
    if "SP500" in history_by_asset:
        bret = returns([b.close for b in history_by_asset["SP500"]])
        if len(bret) >= SENSITIVITY_WINDOW:
            benchmark_returns_120d = bret[-SENSITIVITY_WINDOW:]

    snapshots: list[Snapshot] = []
    for asset, bars in history_by_asset.items():
        try:
            snap = compute_snapshot(
                asset=asset,
                source_used=source_by_asset[asset],
                bars=bars,
                benchmark_returns_120d=benchmark_returns_120d,
                macro_factor_returns=macro_factor_returns,
                risk_free_rate_annual=args.risk_free_rate,
            )
            snapshots.append(snap)
        except Exception as exc:
            warnings.append(f"{asset}: KPI compute error: {exc}")

    if not snapshots:
        print("No snapshots generated.")
        for w in warnings:
            print(f" - {w}")
        return 1

    fred_values, fred_warnings = fetch_optional_fred_series()
    warnings.extend(fred_warnings)

    macro_regime = compute_macro_regime(macro_factor_returns)

    payload = {
        "generated_at_unix": int(time.time()),
        "generated_at_utc": datetime.now(tz=timezone.utc).isoformat(),
        "count": len(snapshots),
        "data_sources": {
            "market": ["stooq", "yahoo", "synthetic_fallback"],
            "macro": ["market_implied_factors", "fred_optional"],
        },
        "macro_context": {
            "macro_factors": MACRO_FACTORS,
            "market_implied_regime": macro_regime,
            "fred_latest": fred_values,
        },
        "assets": [asdict(s) for s in snapshots],
        "warnings": warnings,
    }

    json_path, csv_path = write_outputs(payload, snapshots, Path(args.output_dir))
    print(f"Generated {len(snapshots)} snapshots")
    print(f"JSON: {json_path}")
    print(f"CSV:  {csv_path}")

    if warnings:
        print("\nWarnings:")
        for w in warnings:
            print(f" - {w}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
