# TSMOM Bot — Time-Series Momentum (Carver-style)

A second futures bot, built on the strongest evidence base in systematic trading:
diversified time-series momentum / trend-following (Moskowitz-Ooi-Pedersen 2012;
AQR back to 1880; Robert Carver's *Systematic Trading* / *Advanced Futures Trading
Strategies*). Sharpe ~1.0 before costs across 58 markets is the academic benchmark.

This is fundamentally different from the Tori bot:
- Tori = single-instrument intraday (15m) trendline breakout. Fast, discretionary-style rules.
- TSMOM = multi-instrument, daily bars, continuous forecast, volatility-targeted. Slow, boring, diversified — and far better evidenced.

## Core design (Carver)

**1. Signal — EWMAC (EWMA crossover), continuous forecast**
For each instrument, compute several speeds:
- EWMAC(16,64), EWMAC(32,128), EWMAC(64,256)  (fast = slow / 4)
- raw = EWMA(price, fast) - EWMA(price, slow)
- raw_forecast = raw / daily_price_vol   (vol in price points, ~32-day EWM std of price changes)
- scaled = raw_forecast * forecast_scalar   (so long-run |forecast| ≈ 10)
  - Carver scalars: (16,64)=4.10, (32,128)=2.79, (64,256)=1.91
- capped to ±20
- combined = mean(scaled speeds) * FDM (forecast diversification multiplier ~1.1), capped ±20

**2. Position sizing — volatility targeting**
N_contracts = (forecast / 10) * (capital * tau * IDM * weight) / (multiplier * price * sigma%)
- tau = annual portfolio vol target (e.g. 0.20)
- sigma% = annualized % stdev of returns (daily_return_std * 16)
- multiplier = contract point value (ES=50, micro MES=5, etc.)
- weight = instrument weight (1/N to start)
- IDM = instrument diversification multiplier (>1, since uncorrelated instruments lower portfolio vol)

**3. Buffering (live only)** — only trade when desired position differs from held position
by more than ~10% of an average position, to cut churn/costs.

**4. Frequency** — evaluate once per day after the cash close. No intraday.

## Instruments (backtest set, across 4 asset classes for diversification)
- Equities: ES, NQ, RTY
- Bond: ZN (10y note)
- Commodity: CL (crude), GC (gold)
- FX: 6E (euro)

## Capital reality (honest)
Vol targeting wants fractional contracts. On a small account you can't size many
instruments at minimum granularity. Micros (MES $5, MNQ $2, MGC $10, M2K $5, MYM $0.50)
help but a ~€1k account still can only run 1-2 instruments properly. So:
- PAPER prototype: fine, validate the logic + measure Sharpe.
- REAL money: needs more capital (~$25k+ for a proper diversified micro book) OR
  run a reduced instrument set. This will be an explicit decision after backtest.

## Build order (validate-first, same as Tori)
1. data_daily.py — multi-year daily bars (yfinance)
2. signals.py — EWMAC forecasts
3. sizing.py — vol targeting
4. backtest.py — portfolio backtest, Sharpe/CAGR/maxDD
5. (later) wire to live via existing tori_futures/broker.py (IBKR), daily cron

## Success bar
Out-of-sample Sharpe meaningfully > 0 after costs, max drawdown tolerable.
Academic benchmark ~1.0 Sharpe before costs; realistic retail target 0.5-0.8 after costs.
If it doesn't clear that, don't deploy.
