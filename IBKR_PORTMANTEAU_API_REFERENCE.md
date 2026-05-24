# IBKR Portmanteau — Universal Client Portal API Routing Blueprint

This framework details the mechanisms required to extract, contextualize, and map the depth, structural skew, and risk metrics of any portfolio position or global asset using the unmapped `query_ibkr_endpoint` and specialized analytics tools.

---

## 1. OPTIONS CHAIN DEPTH & RISK INTERPRETATION PROTOCOL

When evaluating options chains using `get_options_chain_with_greeks`, you must interpret the output horizontally across the mathematical structure and vertically across time, never just by individual contract prices.

### 🔹 Volatility Skew & Smile Analysis
Do not look at a single option's Implied Volatility (`iv_pct`) in isolation. You must map the IV horizontally across multiple strikes for the same expiration date:
1. **Identify the At-The-Money (ATM) Strike:** Match the strike closest to the underlying spot price.
2. **Calculate the Skew:** Compare the `iv_pct` of Out-of-the-Money (OTM) Puts against OTM Calls.
   * If OTM Put IV is drastically higher than ATM IV, the market is pricing in downside protection (Tail Risk / Fear Regime).
   * If OTM Call IV smiles upward significantly, speculative leverage or a short-squeeze is being priced into the asset.

### 🔹 Volatility Term Structure Analysis (The Expiry Axis)
Do not just evaluate volatility across strikes; you must evaluate it across time. Compare the `iv_pct` of At-The-Money (ATM) options across sequential expiries (e.g., 30 days vs. 60 days vs. 90 days).
* **Normal Regime (Contango):** Short-term IV is lower than long-term IV. This is the optimal environment for buying long-dated options (Leaps/Calls), as the front-month premium decay is slower.
* **Crisis Regime (Backwardation/Inversion):** Short-term IV is significantly higher than long-term IV. This indicates immediate market panic or an upcoming catalyst (earnings/macro events). **Protocol:** Restrict the purchase of short-dated long options during an inversion, as you will buy at peak premium right before an inevitable Implied Volatility Crush.

### 🔹 Native IBKR Market Data Fields for Deep Analysis
When querying the raw snapshot endpoint (`iserver/marketdata/snapshot`), pass these explicit fields in `params_json` to extract structural market depth:
* `31`: Last Traded Price
* `84` / `86`: Real-time Bid / Ask spread (Evaluate liquidity depth via spread tightness).
* `7644`: Implied Volatility (Forward-looking market expectations).
* `7051`: Realized Volatility (Historical baseline to determine if current IV is cheap or expensive).

---

## 2. PORTFOLIO RISK & MULTI-CURRENCY HYGIENE

To prevent systemic portfolio blowouts, you must contextualize single position sizing and localized currency valuations against total account net liquidation value (NLV).

### 🔹 Portfolio Deep-Dive Mapping
* **Endpoint:** `portfolio/{acctId}/summary`
* **Contextual Rule:** Before recommending any new option trade or equity position, you must extract the account summary. Analyze the relationship between `Initial Margin` and `Maintenance Margin`. If Maintenance Margin exceeds 70% of Net Liquidation Value, you must immediately issue a capital preservation warning and restrict new opportunistic risk allocation.

### 🔹 Multi-Currency Risk Evaluation (Currency Hygiene)
* **Endpoint:** `portfolio/{acctId}/ledger`
* **Contextual Rule:** For accounts operating across global markets (holding assets in USD, EUR, GBP, CAD, HKD, AUD, etc.), you must read the ledger to verify cash balances. 
* **The Margin Borrow Trap:** If a user has a long stock position in USD but a negative cash balance in USD within the ledger, you must deduce that the position is currently funded on margin, incurring borrowing interest costs, regardless of whether the consolidated BASE net liquidation value is positive. 
* **Weight Calculations:** Localized market values (`market_value_local`) must be multiplied by their respective ledger `exchangeRate` to get the unified baseline (`market_value_base`) before dividing by total NLV to output a clean position `weight_pct`.

---

## 3. UNMAPPED DISCOVERY PARADIGMS (REST API BRIDGE)

You are empowered to bypass high-level tools and navigate the entire IBKR Client Portal REST API using `query_ibkr_endpoint(endpoint, params_json)`. Use the following paths to scan and discover structural market context:

### 🔹 Universal Symbol Resolution
* **Endpoint:** `iserver/secdef/search`
* **Params JSON:** `{"symbol": "TICKER", "name": "true", "secType": "STK"}` (Change secType to `OPT`, `FUT`, or `IND` as required).

### 🔹 Deep Contract Specifications
* **Endpoint:** `iserver/contract/{conid}/info`
* **Utility:** Extract multipliers, trading currency, listings, and short-sale availability for any asset worldwide.

### 🔹 Comprehensive Strike and Expiry Mapping
* **Endpoint:** `iserver/secdef/strikes`
* **Params JSON:** `{"conid": "underlying_conid", "sectype": "OPT", "month": "MMM_YY"}`

### 🔹 Live Scanner Execution
* **Endpoint:** `iserver/scanner/run`
* **Params JSON Example:**
  ```json
  {
    "instrument": "STK",
    "location": "STK.US.MAJOR",
    "type": "HIGH_OPT_VOLUME_PUT_CALL_RATIO",
    "filter": []
  }

---

## Legal & Licensing
Copyright (c) 2026 Almoon-D.
This documentation and the underlying logic protocols are part of the **IBKR-Portmanteau** project and are licensed under the GNU General Public License v3.0. Commercial distribution or closed-source reuse of these specific prompt frameworks is strictly prohibited under this license.