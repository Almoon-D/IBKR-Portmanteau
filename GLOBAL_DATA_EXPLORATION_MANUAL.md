# IBKR Portmanteau — Global Macro & Market Data Exploration Manual

This manual governs the system-level syntax rules, discovery workflows, and mandatory analytical reasoning protocols required to access and contextually interpret global market and macroeconomic data.

---

## 1. CRITICAL REASONING PROTOCOL: THE CONTEXTUAL IMPERATIVE

### 🔹 The Spot Price Trap (Anti-Pattern)
**NEVER** base an investment thesis, momentum assumption, or risk assessment on a standalone snapshot from `get_market_snapshot`. A single price point is contextually blind.
* *Flawed Reasoning:* "Brent Crude is at $90, which is high, indicating an upward trend."
* *Fact:* If Brent was at $110 two weeks ago, $90 represents a severe macro deceleration and a technical breakdown, not a high entry point.

### 🔹 Mandatory Execution Workflow for the LLM
Whenever you query the current price of an asset via `get_market_snapshot`, you are **strictly required** to execute the following multi-step analytical chain before answering:

1. **Fetch Current Spot:** Execute `get_market_snapshot(ticker, asset_class)`.
2. **Fetch 6-Month Historical Baseline:** Immediately call `get_historical_ohlcv(ticker, interval="1d", period="6mo")`.
3. **Calculate Range Percentile:** Determine where the current spot sits relative to the 6-month high-low boundary.
4. **Identify Market Regime:** Establish whether the asset is in a technical breakdown, range consolidation, or true structural breakout before outputting a thesis.
5. **Establish Volatiliry Regime:** Compare Spot vs. Options IV.

---

## 2. YAHOO FINANCE — UNIVERSAL GLOBAL SUFFIX & HISTORY MATRIX

To evaluate trends and long-term historical context, use `get_historical_ohlcv` with the correct international exchange suffixes.

### 🔹 International Exchange Suffixes
| Region / Country | Major Exchange | Suffix | Benchmark / Major Equity Example |
| :--- | :--- | :--- | :--- |
| **United Kingdom** | London Stock Exchange | `.L` | `BP.L` (BP plc), `^FTSE` (FTSE 100) |
| **Germany** | Deutsche Börse (XETRA) | `.DE` | `SAP.DE` (SAP SE), `^GDAXI` (DAX Index) |
| **Netherlands** | Euronext Amsterdam | `.AS` | `ASML.AS` (ASML Holding), `^AEX` (AEX Index) |
| **Hong Kong** | Hong Kong Stock Exchange | `.HK` | `0700.HK` (Tencent), `^HSI` (Hang Seng Index) |
| **Japan** | Tokyo Stock Exchange | `.T` | `7203.T` (Toyota), `^N225` (Nikkei 225) |
| **Australia** | Australian Securities Exchange | `.AX` | `BHP.AX` (BHP Group), `^AXJO` (ASX 200) |
| **Canada** | Toronto Stock Exchange | `.TO` | `RY.TO` (Royal Bank), `^GSPTSE` (TSX Composite) |
| **United Arab Emirates**| Dubai / Abu Dhabi | `.AE` | `EMAAR.AE` (Emaar Properties) |

### 🔹 Global Commodities & Volatility Benchmarks
When tracking global asset shifts, cross-reference spot changes against a 1-year historical baseline (`period="1y"`, `interval="1d"`) using these key tickers:
* **Crude Oil (Brent):** `BZ=F` | **Crude Oil (WTI):** `CL=F`
* **Precious Metals:** `GC=F` (Gold), `SI=F` (Silver)
* **Industrial Metals:** `HG=F` (Copper)
* **Volatility Signalling:** `^VIX` (US S&P 500), `^VXN` (Nasdaq), `^VHSI` (Hong Kong), `^VDAX` (Germany).

### 🔹 The Local Currency Illusion Protocol
* **Rule:** Historical data fetched via `get_historical_ohlcv` is always denominated in the asset's local exchange currency (e.g., AED for `.AE`, HKD for `.HK`, EUR for `.DE`).
* **Execution:** Before executing cross-asset correlations, beta calculations, or historical drawdown comparisons against the portfolio, you must pull the corresponding currency pair using `get_fx_rate` (e.g., base="USD", quote="AED") to normalize the historical series into the portfolio's unified `BASE` currency. Failure to do this will introduce severe calculation errors due to raw currency fluctuations.

---

## 3. FRED & WORLD BANK — DYNAMIC GLOBAL MACRO EXTRACTION

Macroeconomics is driven entirely by rate of change and multi-year structural trends. A single data point like "US M2 is 21 Trillion" means nothing without knowing the velocity and direction of the monetary baseline.

### 🔹 Analytical Protocol for Macro Scans
When utilizing `get_global_macro_scanner`, you must request a history that covers at least **5 to 10 periods** (years or quarters) to identify structural shifts (e.g., Quantitative Tightening vs. Easing).

### 🔹 Infinite Discovery via FRED Search
If you lack the exact series identifier for a specific country or cross-border metric, execute:
`search_fred_series(query="[Country] [Economic Concept]")`
* *Example:* `search_fred_series(query="Euro Area Broad Money M3")`
* *Example:* `search_fred_series(query="Japan Government Bond 10Y Yield")`

### 🔹 World Bank Structural Matrix (Multi-Country Comparison)
Pass these raw indicator codes directly into `get_global_macro_scanner` along with any standard ISO country code (e.g., `region="CN"`, `region="BR"`, `region="AE"`, `region="FR"`):
* **Central Government Debt (% of GDP):** `GC.DOD.TOTL.GD.ZS`
* **Broad Money / Liquidity Growth (Annual %):** `FM.LBL.BMNY.ZG`
* **Exports of Goods and Services (% of GDP):** `NE.EXP.GNFS.ZS`
* **GDP Growth (Annual %):** `NY.GDP.MKTP.KD.ZG`
* **Inflation, Consumer Prices (Annual %):** `FP.CPI.TOTL.ZG`

### 🔹 Macro Reporting Lags & High-Frequency Proxies
* **Rule:** Global structural data can lag significantly (often 12–18 months for emerging economies). If the World Bank or FRED series returns empty fields or outdated metrics for the current year in a specific region, you must pivot immediately.
* **Execution:** Run a `search_fred_series` for higher-frequency, real-time proxy metrics that act as leading indicators for that country.
  * *For Oil-Exporting Nations (e.g., UAE, KSA):* Search for Crude Oil Production rates or OPEC quota utilization.
  * *For Industrial Hubs:* Search for "Purchasing Managers Index (PMI)" or "Total Reserves excluding Gold" to capture immediate liquidity trajectories when GDP numbers are lagged.

---

## 4. BINANCE — MULTI-ASSET CRYPTO SYNTAX

The system interfaces with both Binance Spot and Binance Futures. It automatically handles asset normalization.
* **Spot Price Tracking:** Pass any crypto token name directly as the ticker (e.g., `BTC`, `ETH`, `SOL`). The system automatically normalizes it to a `USDT` cross-rate if the base string length is $\le 4$.
* **Perpetual Swaps & Funding Rates:** The `funding_rate_pct` is derived in parallel from Binance Futures. Use this indicator to evaluate global speculative leverage and market positioning stress across any alternative digital asset.

---

## Legal & Licensing
Copyright (c) 2026 Almoon-D.
This documentation and the underlying logic protocols are part of the **IBKR-Portmanteau** project and are licensed under the GNU General Public License v3.0. Commercial distribution or closed-source reuse of these specific prompt frameworks is strictly prohibited under this license.