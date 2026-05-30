"""
IBKR Portmanteau — Unified Financial Server (Read-Only)
Data Sources : IBKR Client Portal (Read-Only) · FRED · World Bank · Yahoo Finance · Binance
Transport    : STDIO (Local) / SSE (Remote)
Cache        : High-Performance Lock-Isolated Active-Evict Memory Layer
Math Engine  : High-Precision Pure Vectorized Black-Scholes-Merton (BSM) Engine with Newton-Raphson IV Solver
Python       : 3.11+ Compatible (Optimized for 3.14)
"""

import asyncio
import json
import logging
import os
import sys
import time
import argparse
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional
from collections import defaultdict

import httpx
import numpy as np
from mcp.server.fastmcp import FastMCP
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ibkr_portmanteau")
logging.getLogger("httpx").setLevel(logging.WARNING)

IBKR_BASE          = os.environ.get("IBKR_GATEWAY_URL", "https://localhost:5000/v1/api")
IBKR_TICKLE_SECS   = 45
FRED_API_KEY       = os.environ.get("FRED_API_KEY", "")
FRED_BASE          = "https://api.stlouisfed.org/fred/series/observations"
BINANCE_SPOT_URL   = "https://api.binance.com/api/v3/ticker/24hr"
BINANCE_FUND_URL   = "https://fapi.binance.com/fapi/v1/premiumIndex"
YAHOO_SEARCH_URL   = "https://query1.finance.yahoo.com/v1/finance/search"

TTL_MACRO_SECS     = 86400   
TTL_SPOT_SECS      = 10      
TTL_OPTION_SECS    = 300     
TTL_SEARCH_SECS    = 14400   

FRED_SERIES = {
    "GDP_GROWTH": "A191RL1Q225SBEA", 
    "INFLATION": "CPIAUCSL", 
    "MONEY_SUPPLY_M2": "M2SL", 
    "UNEMPLOYMENT": "UNRATE"
}

WORLD_BANK_INDICATORS = {
    "GDP_GROWTH": "NY.GDP.MKTP.KD.ZG", 
    "INFLATION": "FP.CPI.TOTL.ZG",
    "MONEY_SUPPLY_M2": "FM.LBL.BMNY.CN", 
    "UNEMPLOYMENT": "SL.UEM.TOTL.ZS",
    "DEBT_PCT_GDP": "GC.DOD.TOTL.GD.ZS",
    "BIRTH_RATE": "SP.DYN.CBRT.IN",
    "POPULATION": "SP.POP.TOTL",
    "GDP_PER_CAPITA": "NY.GDP.PCAP.CD"
}

def _safe_float(val: Any, default: float = 0.0) -> float:
    if val is None: 
        return default
    try: 
        return float(str(val).strip().replace(",", ""))
    except (ValueError, TypeError): 
        return default

def _cnd_vectorized(x: np.ndarray) -> np.ndarray:
    """High-precision native NumPy vectorized Cumulative Normal Distribution."""
    a1 =  0.319381530
    a2 = -0.356563782
    a3 =  1.781477937
    a4 = -1.821255978
    a5 =  1.330274429
    p  =  0.2316419
    abs_x = np.abs(x)
    k = 1.0 / (1.0 + p * abs_x)
    cnd_val = 1.0 - (1.0 / np.sqrt(2.0 * np.pi)) * np.exp(-abs_x * abs_x / 2.0) * (
        a1 * k + a2 * (k ** 2) + a3 * (k ** 3) + a4 * (k ** 4) + a5 * (k ** 5)
    )
    return np.where(x >= 0, cnd_val, 1.0 - cnd_val)

def calculate_bsm_prices_vectorized(spot: np.ndarray, strikes: np.ndarray, dtes: np.ndarray, ivs_pct: np.ndarray, rate_pct: np.ndarray, is_calls: np.ndarray, dividend_yield_pct: np.ndarray) -> np.ndarray:
    """Calculates multiple arbitrary option contract values simultaneously using pure matrix execution."""
    if len(strikes) == 0:
        return np.array([])
    T = np.maximum(dtes / 365.0, 0.0001)
    sigma = np.maximum(ivs_pct / 100.0, 0.0001)
    r = rate_pct / 100.0
    q = dividend_yield_pct / 100.0
    
    safe_strikes = np.maximum(strikes, 0.001)
    safe_spot = np.maximum(spot, 0.001)
    
    d1 = (np.log(safe_spot / safe_strikes) + (r - q + (sigma ** 2) / 2.0) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    
    cnd_d1 = _cnd_vectorized(d1)
    cnd_d2 = _cnd_vectorized(d2)
    cnd_minus_d1 = _cnd_vectorized(-d1)
    cnd_minus_d2 = _cnd_vectorized(-d2)
    
    price_call = safe_spot * np.exp(-q * T) * cnd_d1 - safe_strikes * np.exp(-r * T) * cnd_d2
    price_put = safe_strikes * np.exp(-r * T) * cnd_minus_d2 - safe_spot * np.exp(-q * T) * cnd_minus_d1
    
    return np.where(is_calls, price_call, price_put)

def calculate_greeks_vectorized(spot: float, strikes: np.ndarray, dtes: np.ndarray, ivs_pct: np.ndarray, rate_pct: float, is_calls: np.ndarray, dividend_yield_pct: float = 0.0) -> list[dict[str, float]]:
    """Calculates entire option chain surfaces vectorially under the Black-Scholes-Merton model."""
    if len(strikes) == 0:
        return []
        
    T = np.maximum(dtes / 365.0, 0.0001)
    sigma = np.maximum(ivs_pct / 100.0, 0.0001)
    r = rate_pct / 100.0
    q = dividend_yield_pct / 100.0
    S = max(spot, 0.001)
    K = np.maximum(strikes, 0.001)
    
    d1 = (np.log(S / K) + (r - q + (sigma ** 2) / 2.0) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    
    cnd_d1 = _cnd_vectorized(d1)
    cnd_d2 = _cnd_vectorized(d2)
    cnd_minus_d1 = _cnd_vectorized(-d1)
    cnd_minus_d2 = _cnd_vectorized(-d2)
    
    nd_prime_d1 = (1.0 / np.sqrt(2.0 * np.pi)) * np.exp(-0.5 * d1 * d1)
    
    delta_call = np.exp(-q * T) * cnd_d1
    delta_put = np.exp(-q * T) * (cnd_d1 - 1.0)
    
    theta_call = (- (S * np.exp(-q * T) * nd_prime_d1 * sigma) / (2.0 * np.sqrt(T)) 
                  - r * K * np.exp(-r * T) * cnd_d2 
                  + q * S * np.exp(-q * T) * cnd_d1) / 365.0
                  
    theta_put = (- (S * np.exp(-q * T) * nd_prime_d1 * sigma) / (2.0 * np.sqrt(T)) 
                 + r * K * np.exp(-r * T) * cnd_minus_d2 
                 - q * S * np.exp(-q * T) * cnd_minus_d1) / 365.0
    
    gamma = (np.exp(-q * T) * nd_prime_d1) / (S * sigma * np.sqrt(T))
    vega = (S * np.exp(-q * T) * np.sqrt(T) * nd_prime_d1) / 100.0
    
    deltas = np.where(is_calls, delta_call, delta_put)
    thetas = np.where(is_calls, theta_call, theta_put)
    
    results = []
    for i in range(len(strikes)):
        results.append({
            "delta": round(float(deltas[i]), 4),
            "gamma": round(float(gamma[i]), 4),
            "vega": round(float(vega[i]), 4),
            "theta": round(float(thetas[i]), 4)
        })
    return results

def calculate_iv_vectorized(spot: float, strikes: np.ndarray, dtes: np.ndarray, market_prices: np.ndarray, rate_pct: float, is_calls: np.ndarray, dividend_yield_pct: float = 0.0) -> np.ndarray:
    """Derives Implied Volatility directly from options mid-market spreads using a vectorized Newton-Raphson engine with localized item masking optimizations."""
    if len(strikes) == 0:
        return np.array([])
        
    T = np.maximum(dtes / 365.0, 0.0001)
    r = rate_pct / 100.0
    q = dividend_yield_pct / 100.0
    
    safe_strikes = np.maximum(strikes, 0.001)
    safe_spot = max(spot, 0.001)
    
    sigma = np.full(len(strikes), 0.35)
    
    for _ in range(12):
        d1 = (np.log(safe_spot / safe_strikes) + (r - q + (sigma ** 2) / 2.0) * T) / (sigma * np.sqrt(T))
        d2 = d1 - sigma * np.sqrt(T)
        
        price_call = safe_spot * np.exp(-q * T) * _cnd_vectorized(d1) - safe_strikes * np.exp(-r * T) * _cnd_vectorized(d2)
        price_put = safe_strikes * np.exp(-r * T) * _cnd_vectorized(-d2) - safe_spot * np.exp(-q * T) * _cnd_vectorized(-d1)
        current_prices = np.where(is_calls, price_call, price_put)
        
        diff = current_prices - market_prices
        abs_diff = np.abs(diff)
        
        not_converged = abs_diff >= 1e-4
        if not np.any(not_converged):
            break
            
        nd_prime_d1 = (1.0 / np.sqrt(2.0 * np.pi)) * np.exp(-0.5 * d1 * d1)
        vega = np.maximum(safe_spot * np.exp(-q * T) * np.sqrt(T) * nd_prime_d1, 1e-5)
        
        sigma[not_converged] = np.clip(
            sigma[not_converged] - diff[not_converged] / vega[not_converged], 0.01, 4.0
        )
            
    return sigma * 100.0

class AsyncMemoryCacheManager:
    """Async-safe high-performance memory cache featuring active task background eviction loops."""
    def __init__(self) -> None:
        self._storage: dict[str, tuple[Any, float]] = {}
        self._lock = asyncio.Lock()
        self._eviction_task: Optional[asyncio.Task[None]] = None

    async def init(self) -> None:
        log.info("High-performance lock-isolated memory cache architecture activated.")
        self._eviction_task = asyncio.create_task(self._active_eviction_loop())

    async def get(self, key: str) -> Optional[Any]:
        async with self._lock:
            if key not in self._storage:
                return None
            val, expires = self._storage[key]
            if time.time() > expires:
                del self._storage[key]
                return None
            return val

    async def set(self, key: str, value: Any, ttl_secs: int) -> None:
        async with self._lock:
            self._storage[key] = (value, time.time() + ttl_secs)

    async def _active_eviction_loop(self) -> None:
        """Sweeps database metrics asynchronously every 300 seconds to fully mitigate long-tail memory leaks."""
        try:
            while True:
                await asyncio.sleep(300)
                async with self._lock:
                    now = time.time()
                    expired_keys = [k for k, (_, exp) in self._storage.items() if now > exp]
                    for k in expired_keys:
                        del self._storage[k]
                    if expired_keys:
                        log.info(f"Evicted {len(expired_keys)} lingering keys from memory layer container.")
        except asyncio.CancelledError:
            pass

    async def close(self) -> None:
        if self._eviction_task:
            self._eviction_task.cancel()
            try:
                await self._eviction_task
            except asyncio.CancelledError:
                pass
        async with self._lock:
            self._storage.clear()

class IBKRGateway:
    def __init__(self) -> None:
        ca_cert = os.environ.get("IBKR_CACERT")
        verify_value = ca_cert if (ca_cert and os.path.exists(ca_cert)) else False
        if not verify_value:
            log.warning("IBKR_CACERT not found. Native validation deactivated.")
            
        self.client = httpx.AsyncClient(verify=verify_value, timeout=10.0)
        self.healthy = False
        self._tickle_task: Optional[asyncio.Task[None]] = None

    async def start(self) -> None:
        await self.check_status_immediately()
        self._tickle_task = asyncio.create_task(self._maintenance_loop())

    async def stop(self) -> None:
        if self._tickle_task:
            self._tickle_task.cancel()
            try: 
                await self._tickle_task
            except asyncio.CancelledError: 
                pass
        await self.client.aclose()

    async def check_status_immediately(self) -> None:
        try:
            resp = await self.client.get(f"{IBKR_BASE}/iserver/auth/status")
            if resp.status_code == 200 and resp.json().get("authenticated", False):
                self.healthy = True
                await self.client.post(f"{IBKR_BASE}/tickle")
            else:
                self.healthy = False
        except Exception:
            self.healthy = False

    async def _maintenance_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(IBKR_TICKLE_SECS)
                await self.check_status_immediately()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception(f"Exception in IBKR maintenance link loop: {e}.")
                await asyncio.sleep(10)

    async def resolve_conid(self, ticker: str) -> Optional[int]:
        try:
            resp = await self.client.get(f"{IBKR_BASE}/iserver/secdef/search", params={"symbol": ticker, "secType": "STK"})
            if resp.status_code == 200 and resp.json():
                for asset in resp.json():
                    if asset.get("ticker", "").upper() == ticker.upper():
                        return int(asset["conid"])
                return int(resp.json()[0]["conid"])
        except Exception: 
            pass
        return None

class MarketFacade:
    def __init__(self, ibkr: IBKRGateway, cache: AsyncMemoryCacheManager) -> None:
        self.ibkr = ibkr
        self.cache = cache
        self.http = httpx.AsyncClient(timeout=12.0, headers={"User-Agent": "Mozilla/5.0"})
        
        self.network_semaphore = asyncio.Semaphore(8)
        self.fred_cooldown_until = 0.0

    async def close(self) -> None:
        await self.http.aclose()

    async def search_instrument(self, query: str) -> dict[str, Any]:
        cache_key = f"search:{query.lower()}"
        cached = await self.cache.get(cache_key)
        if cached: 
            return cached
        
        async with self.network_semaphore:
            try:
                resp = await self.http.get(YAHOO_SEARCH_URL, params={"q": query, "quotesCount": 5, "newsCount": 0})
                if resp.status_code == 200:
                    quotes = resp.json().get("quotes", [])
                    results = []
                    for q in quotes:
                        results.append({
                            "ticker": q.get("symbol"),
                            "name": q.get("shortname") or q.get("longname"),
                            "exchange": q.get("exchange"),
                            "type": q.get("quoteType")
                        })
                    final_res = {"query": query, "matches": results}
                    await self.cache.set(cache_key, final_res, TTL_SEARCH_SECS)
                    return final_res
            except Exception as e:
                return {"error": str(e), "matches": []}
        return {"query": query, "matches": []}

    async def get_spot_price(self, ticker: str, asset_class: str) -> dict[str, Any]:
        asset_class = asset_class.upper()
        ticker = ticker.upper()
        
        if asset_class == "CRYPTO" and not any(ticker.endswith(sf) for sf in ["USDT", "BUSD", "USDC", "EUR"]):
            ticker = f"{ticker}USDT"
                
        cache_key = f"spot:{asset_class}:{ticker}"
        cached = await self.cache.get(cache_key)
        if cached: 
            return cached

        if asset_class == "CRYPTO":
            async with self.network_semaphore:
                try:
                    s_task = self.http.get(BINANCE_SPOT_URL, params={"symbol": ticker})
                    f_task = self.http.get(BINANCE_FUND_URL, params={"symbol": ticker})
                    spot_resp, fund_resp = await asyncio.gather(s_task, f_task, return_exceptions=True)
                    if isinstance(spot_resp, Exception) or spot_resp.status_code != 200:
                        return {"error": f"Crypto '{ticker}' missing on Binance infrastructure layer", "source": "ERROR"}
                    
                    spot_data = spot_resp.json()
                    funding_rate = None
                    if not isinstance(fund_resp, Exception) and fund_resp.status_code == 200:
                        try:
                            fund_json = fund_resp.json()
                            f_item = fund_json[0] if isinstance(fund_json, list) else fund_json
                            funding_rate = _safe_float(f_item.get("lastFundingRate")) * 100
                        except Exception: 
                            pass
                            
                    result = {"ticker": ticker, "source": "[SOURCE: BINANCE]", "last": _safe_float(spot_data.get("lastPrice")), "bid": _safe_float(spot_data.get("bidPrice")), "ask": _safe_float(spot_data.get("askPrice")), "volume_24h": _safe_float(spot_data.get("volume")), "funding_rate_pct": funding_rate}
                    await self.cache.set(cache_key, result, TTL_SPOT_SECS)
                    return result
                except Exception as e: 
                    return {"error": str(e), "source": "ERROR"}

        if self.ibkr.healthy:
            conid = await self.ibkr.resolve_conid(ticker)
            if conid:
                try:
                    resp = await self.ibkr.client.get(f"{IBKR_BASE}/iserver/marketdata/snapshot", params={"conids": str(conid), "fields": "31,84,86"})
                    if resp.status_code == 200 and resp.json():
                        snap = resp.json()[0]
                        last = _safe_float(snap.get("31"))
                        if last > 0:
                            result = {"ticker": ticker, "source": "[SOURCE: IBKR]", "last": last, "bid": _safe_float(snap.get("84")), "ask": _safe_float(snap.get("86")), "conid": conid}
                            await self.cache.set(cache_key, result, TTL_SPOT_SECS)
                            return result
                except Exception: 
                    pass

        async with self.network_semaphore:
            try:
                url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={ticker}"
                resp = await self.http.get(url)
                if resp.status_code != 200:
                    return {"error": f"Asset '{ticker}' not found on core async fallback networks.", "source": "ERROR"}
                
                res_list = resp.json().get("quoteResponse", {}).get("result", [])
                if not res_list:
                    return {"error": f"Asset '{ticker}' missing from fallback endpoint parsing lists.", "source": "ERROR"}
                    
                quote = res_list[0]
                div_yield = _safe_float(quote.get("trailingAnnualDividendYield", quote.get("dividendYield", 0.0)))
                if div_yield < 1.0 and div_yield > 0.0:
                    div_yield *= 100.0  
                    
                last_p = _safe_float(quote.get("regularMarketPrice"))
                result = {
                    "ticker": ticker, 
                    "source": "[SOURCE: YAHOO_DIRECT_ASYNC]", 
                    "last": last_p, 
                    "bid": _safe_float(quote.get("bid", last_p)), 
                    "ask": _safe_float(quote.get("ask", last_p)),
                    "dividend_yield_pct": round(div_yield, 2)
                }
                await self.cache.set(cache_key, result, TTL_SPOT_SECS)
                return result
            except Exception as e: 
                return {"error": str(e), "source": "ERROR"}

    async def get_historical_ohlcv(self, ticker: str, interval: str, period: str, limit: int) -> dict[str, Any]:
        cache_key = f"hist:{ticker}:{interval}:{period}:{limit}"
        cached = await self.cache.get(cache_key)
        if cached: 
            return cached
            
        async with self.network_semaphore:
            try:
                url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range={period}&interval={interval}"
                resp = await self.http.get(url)
                if resp.status_code != 200:
                    return {"error": "Historical series structural payload context unavailable"}
                
                payload = resp.json()
                chart_data = payload.get("chart", {})
                result_list = chart_data.get("result")
                
                if not result_list or result_list[0] is None:
                    error_msg = chart_data.get("error", {}).get("description", "Asset symbol not found or delisted.")
                    return {"error": f"Yahoo Finance Error: {error_msg}"}
                
                root = result_list[0]
                timestamps = root.get("timestamp", [])
                indicators = root.get("indicators", {}).get("quote", [{}])[0]
                
                if not timestamps or "close" not in indicators:
                    return {"error": "Corrupt metrics schema returned via core network targets."}
                
                df_len = len(timestamps)
                opens  = indicators.get("open", [0.0] * df_len)
                highs  = indicators.get("high", [0.0] * df_len)
                lows   = indicators.get("low", [0.0] * df_len)
                closes = indicators.get("close", [0.0] * df_len)
                vols   = indicators.get("volume", [0] * df_len)
                
                bars = []
                start_idx = max(0, df_len - limit)
                for idx in range(start_idx, df_len):
                    if closes[idx] is None: 
                        continue
                    ts_str = datetime.fromtimestamp(timestamps[idx], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                    bars.append({
                        "timestamp": ts_str,
                        "open": round(_safe_float(opens[idx]), 2),
                        "high": round(_safe_float(highs[idx]), 2),
                        "low": round(_safe_float(lows[idx]), 2),
                        "close": round(_safe_float(closes[idx]), 2),
                        "volume": int(_safe_float(vols[idx]))
                    })
                    
                result = {"ticker": ticker, "source": "[SOURCE: YAHOO_DIRECT_HIST]", "interval": interval, "bars": bars}
                await self.cache.set(cache_key, result, TTL_OPTION_SECS)
                return result
            except Exception as e: 
                return {"error": str(e)}

    async def get_fx_rate(self, base: str, quote: str) -> dict[str, Any]:
        pair = f"{base.upper()}{quote.upper()}=X"
        cache_key = f"fx:{pair}"
        cached = await self.cache.get(cache_key)
        if cached: 
            return cached
            
        async with self.network_semaphore:
            try:
                url = f"https://query1.finance.yahoo.com/v8/finance/chart/{pair}?interval=1m&range=1d"
                resp = await self.http.get(url)
                if resp.status_code != 200:
                    return {"error": f"FX rate service unavailable (Status {resp.status_code})"}
                
                payload = resp.json()
                chart_data = payload.get("chart", {})
                result_list = chart_data.get("result")
                
                if not result_list or result_list[0] is None:
                    error_msg = chart_data.get("error", {}).get("description", "Invalid currency layer codes.")
                    return {"error": f"Yahoo Finance Error: {error_msg}"}
                    
                meta = result_list[0].get("meta", {})
                rate = _safe_float(meta.get("regularMarketPrice"))
                if rate <= 0:
                    return {"error": f"Failed structural extraction context for {pair}"}
                    
                result = {"pair": pair, "rate": round(rate, 4), "timestamp": datetime.now(timezone.utc).isoformat()}
                await self.cache.set(cache_key, result, 300)
                return result
            except Exception as e: 
                return {"error": str(e)}

    async def get_options_chain_with_greeks(self, ticker: str, max_expiry_days: int, moneyness_range: float, risk_free_rate: Optional[float] = None) -> dict[str, Any]:
        cache_key = f"greeks_chain:{ticker}:{max_expiry_days}:{moneyness_range}"
        cached = await self.cache.get(cache_key)
        if cached: 
            return cached

        spot_dict = await self.get_spot_price(ticker, "STK")
        spot_price = _safe_float(spot_dict.get("last"))
        effective_q = _safe_float(spot_dict.get("dividend_yield_pct", 0.0))

        if spot_price <= 0:
            return {"error": "Cannot map options structures without valid spot price foundations."}

        effective_rf = risk_free_rate if risk_free_rate is not None else 4.5
        if risk_free_rate is None and FRED_API_KEY and time.time() > self.fred_cooldown_until:
            try:
                cached_rf = await self.cache.get("macro:rf_rate:DTB3")
                if cached_rf is not None:
                    effective_rf = float(cached_rf)
                else:
                    resp = await self.http.get(FRED_BASE, params={"series_id": "DTB3", "api_key": FRED_API_KEY, "file_type": "json", "sort_order": "desc", "limit": 1})
                    if resp.status_code == 200:
                        obs = resp.json().get("observations", [])
                        if obs and obs[0].get("value", ".") != ".":
                            effective_rf = float(obs[0]["value"])
                            await self.cache.set("macro:rf_rate:DTB3", effective_rf, 86400)
            except Exception:
                self.fred_cooldown_until = time.time() + 900.0

        async with self.network_semaphore:
            try:
                url = f"https://query1.finance.yahoo.com/v7/finance/options/{ticker}"
                resp = await self.http.get(url)
                root_res = resp.json()["optionChain"]["result"][0]
                exp_timestamps = root_res.get("expirationDates", [])
                
                now_ts = time.time()
                cutoff_ts = now_ts + (max_expiry_days * 86400)
                # Fixed pre-existing weeklies expansion ceiling from 4 to 15 entries
                valid_timestamps = [ts for ts in exp_timestamps if now_ts < ts <= cutoff_ts][:15]
                
                tasks = [self.http.get(f"https://query1.finance.yahoo.com/v7/finance/options/{ticker}?date={ts}") for ts in valid_timestamps]
                chain_responses = await asyncio.gather(*tasks, return_exceptions=True)
                
                processed_contracts = []
                for idx, c_resp in enumerate(chain_responses):
                    if isinstance(c_resp, Exception) or c_resp.status_code != 200: 
                        continue
                        
                    exp_data = c_resp.json()["optionChain"]["result"][0]
                    options_block = exp_data.get("options", [{}])[0]
                    exp_str = datetime.fromtimestamp(valid_timestamps[idx], tz=timezone.utc).strftime("%Y-%m-%d")
                    dte_val = max(1, int((valid_timestamps[idx] - now_ts) / 86400))
                    
                    for side_key, is_call in [("calls", True), ("puts", False)]:
                        contracts_list = options_block.get(side_key, [])
                        for opt in contracts_list:
                            strike = _safe_float(opt.get("strike"))
                            if not (spot_price * (1.0 - moneyness_range) <= strike <= spot_price * (1.0 + moneyness_range)):
                                continue
                                
                            bid = _safe_float(opt.get("bid"))
                            ask = _safe_float(opt.get("ask"))
                            mid_market = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else _safe_float(opt.get("lastPrice"))
                            
                            if mid_market <= 0: 
                                continue
                                
                            processed_contracts.append({
                                "symbol": opt.get("contractSymbol"),
                                "type": "CALL" if is_call else "PUT",
                                "expiry": exp_str,
                                "strike": strike,
                                "bid": bid,
                                "ask": ask,
                                "mid": mid_market,
                                "dte": dte_val,
                                "is_call": is_call
                            })
                            
                if processed_contracts:
                    by_expiry = defaultdict(list)
                    for c in processed_contracts:
                        by_expiry[c["expiry"]].append(c)
                        
                    balanced_contracts = []
                    for exp_date, c_list in by_expiry.items():
                        c_list.sort(key=lambda x: abs(x["strike"] - spot_price))
                        balanced_contracts.extend(c_list[:20])  
                        
                    processed_contracts = balanced_contracts
                    
                    strikes_arr = np.array([c["strike"] for c in processed_contracts])
                    dtes_arr = np.array([c["dte"] for c in processed_contracts])
                    mkt_prices_arr = np.array([c["mid"] for c in processed_contracts])
                    calls_arr = np.array([c["is_call"] for c in processed_contracts])
                    
                    calculated_ivs = calculate_iv_vectorized(spot_price, strikes_arr, dtes_arr, mkt_prices_arr, effective_rf, calls_arr, effective_q)
                    greeks_surface = calculate_greeks_vectorized(spot_price, strikes_arr, dtes_arr, calculated_ivs, effective_rf, calls_arr, effective_q)
                    
                    for i, c in enumerate(processed_contracts):
                        c["iv_pct"] = round(float(calculated_ivs[i]), 2)
                        c["greeks"] = greeks_surface[i]
                        del c["is_call"]
                        
                    processed_contracts.sort(key=lambda x: (x["expiry"], abs(x["strike"] - spot_price)))
                    result = {"ticker": ticker, "underlying_price": round(spot_price, 2), "option_contracts": processed_contracts}
                    await self.cache.set(cache_key, result, TTL_OPTION_SECS)
                    return result
            except Exception as e:
                return {"error": f"Asynchronous native derivatives pricing layer failure: {e}"}
        return {"ticker": ticker, "underlying_price": round(spot_price, 2), "option_contracts": []}

class MacroProvider:
    def __init__(self, cache: AsyncMemoryCacheManager) -> None:
        self.cache = cache
        self.http = httpx.AsyncClient(timeout=15.0)

    async def close(self) -> None:
        await self.http.aclose()

    async def fetch_dynamic_indicator(self, indicator_id: str, region: str) -> dict[str, Any]:
        region = region.upper()
        cache_key = f"dyn_macro:{indicator_id}:{region}"
        cached = await self.cache.get(cache_key)
        if cached: 
            return cached
        
        series_id = FRED_SERIES.get(indicator_id.upper(), indicator_id)
        if region in ("US", "USA") and FRED_API_KEY:
            try:
                resp = await self.http.get(FRED_BASE, params={"series_id": series_id, "api_key": FRED_API_KEY, "file_type": "json", "sort_order": "desc", "limit": 15})
                if resp.status_code == 200:
                    obs = resp.json().get("observations", [])
                    history = [{"date": o["date"], "value": _safe_float(o["value"])} for o in reversed(obs) if o["value"] not in (".", "")]
                    result = {"indicator": series_id, "region": "US", "source": "FRED", "series": history}
                    await self.cache.set(cache_key, result, TTL_MACRO_SECS)
                    return result
            except Exception: 
                pass

        wb_code = WORLD_BANK_INDICATORS.get(indicator_id.upper(), indicator_id)
        try:
            resp = await self.http.get(f"https://api.worldbank.org/v2/country/{region}/indicator/{wb_code}", params={"format": "json", "per_page": 20})
            raw = resp.json()
            if isinstance(raw, list) and len(raw) >= 2 and isinstance(raw[1], list) and raw[1]:
                history = [{"date": r["date"], "value": _safe_float(r["value"])} for r in reversed(raw[1]) if r.get("value") is not None]
                region_name = region
                if len(raw[1]) > 0 and isinstance(raw[1][0], dict):
                    region_name = raw[1][0].get("country", {}).get("value", region)
                result = {"indicator": wb_code, "region": region_name, "source": "WORLD_BANK", "series": history}
                await self.cache.set(cache_key, result, TTL_MACRO_SECS)
                return result
        except Exception as e: 
            return {"error": str(e)}
        return {"error": f"Macro asset pointer '{indicator_id}' unreached."}

class PortfolioEngine:
    def __init__(self, ibkr: IBKRGateway) -> None:
        self.ibkr = ibkr

    async def _fetch_contract_info_safe(self, conid: int) -> Optional[dict[str, Any]]:
        try:
            resp = await self.ibkr.client.get(f"{IBKR_BASE}/iserver/contract/{conid}/info")
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return None

    async def analyze(self, market: Optional[MarketFacade] = None, risk_free_rate: Optional[float] = None) -> dict[str, Any]:
        if not self.ibkr.healthy: 
            return {"status": "OFFLINE", "error": "Native IBKR Execution Gateway Proxy Disconnected"}
        try:
            acct_resp = await self.ibkr.client.get(f"{IBKR_BASE}/portfolio/accounts")
            if acct_resp.status_code != 200 or not acct_resp.json(): 
                return {"status": "ERROR", "error": "Failed account definition sequence mapping on network pipes."}
            acct_id = acct_resp.json()[0]["id"]
            ledger_resp = await self.ibkr.client.get(f"{IBKR_BASE}/portfolio/{acct_id}/ledger")
            ledger_data = ledger_resp.json()
            
            fx_rates = {}
            nlv, buying_power = 0.0, 0.0
            if "BASE" in ledger_data:
                base = ledger_data["BASE"]
                nlv = _safe_float(base.get("netliquidationvalue"))
                buying_power = _safe_float(base.get("buyingpower"))
            
            for currency, data in ledger_data.items():
                if isinstance(data, dict):
                    fx_rates[currency.upper()] = _safe_float(data.get("exchangeRate", 1.0))
                    if "BASE" not in ledger_data and currency != "BASE":
                        rate = _safe_float(data.get("exchangeRate", 1.0))
                        nlv += _safe_float(data.get("netliquidationvalue")) * rate
                        buying_power += _safe_float(data.get("buyingpower")) * rate
                        
            pos_resp = await self.ibkr.client.get(f"{IBKR_BASE}/portfolio/{acct_id}/positions/0")
            
            processed_positions = []
            total_linear_exposure = 0.0
            options_positions = []
            
            if pos_resp.status_code == 200:
                for p in pos_resp.json() or []:
                    mkt_val_local = _safe_float(p.get("mktValue"))
                    pos_curr = p.get("currency", "USD").upper()
                    rate = fx_rates.get(pos_curr, 1.0)
                    
                    mkt_val_base = mkt_val_local * rate
                    asset_class = p.get("assetClass", "UNKNOWN")
                    contract_desc = p.get("contractDesc", p.get("ticker", "UNKNOWN"))
                    
                    processed_positions.append({
                        "contract": contract_desc, 
                        "asset_class": asset_class, 
                        "size": _safe_float(p.get("position")), 
                        "market_value_local": round(mkt_val_local, 2),
                        "market_value_base": round(mkt_val_base, 2), 
                        "weight_pct": round((mkt_val_base / nlv * 100), 2) if nlv > 0 else 0.0
                    })
                    
                    if asset_class in ("STK", "ETF"): 
                        total_linear_exposure += mkt_val_base
                    elif asset_class == "OPT":
                        options_positions.append(p)
                        
            underlying_spots = {}
            underlying_yields = {}
            if market and options_positions:
                unique_tickers = []
                for p in options_positions:
                    t = p.get("symbol", p.get("ticker", "")).upper()
                    c_desc = p.get("contractDesc", "")
                    if not t and c_desc:
                        t = c_desc.split()[0]
                    if t:
                        unique_tickers.append(t)
                unique_tickers = list(set(unique_tickers))
                
                spot_results = await asyncio.gather(*[market.get_spot_price(t, "STK") for t in unique_tickers], return_exceptions=True)
                for t, res in zip(unique_tickers, spot_results):
                    if not isinstance(res, Exception) and "last" in res:
                        underlying_spots[t] = _safe_float(res["last"])
                        underlying_yields[t] = _safe_float(res.get("dividend_yield_pct", 0.0))

            effective_rf = risk_free_rate if risk_free_rate is not None else 4.5
            if risk_free_rate is None and market and market.cache:
                cached_rf = await market.cache.get("macro:rf_rate:DTB3")
                if cached_rf is not None:
                    effective_rf = float(cached_rf)

            stress_scenarios = {}
            skipped_positions = []  
            
            if options_positions:
                resolution_tasks = []
                for p in options_positions:
                    strike = _safe_float(p.get("strike"))
                    expiry_str = p.get("expiry", "")
                    conid = p.get("conid")
                    if (strike <= 0 or not expiry_str) and conid:
                        resolution_tasks.append(self._fetch_contract_info_safe(int(conid)))
                    else:
                        resolution_tasks.append(asyncio.sleep(0, result=None))
                        
                # Fixed concurrency landmine by ensuring single request issues do not drop the analysis thread
                resolved_infos = await asyncio.gather(*resolution_tasks, return_exceptions=True)
                
                valid_opts = []
                spots_arr, strikes_arr, dtes_arr, ivs_arr, calls_mask, sizes_arr, fx_arr, multipliers_arr, yields_arr = [], [], [], [], [], [], [], [], []
                
                for idx, p in enumerate(options_positions):
                    ticker = p.get("symbol", p.get("ticker", "")).upper()
                    contract_desc = p.get("contractDesc", "")
                    if not ticker and contract_desc:
                        ticker = contract_desc.split()[0]
                        
                    spot_price = underlying_spots.get(ticker, 0.0)
                    strike = _safe_float(p.get("strike"))
                    expiry_str = p.get("expiry", "")
                    right = p.get("putCall", p.get("right", "C")).upper()
                    
                    info = resolved_infos[idx]
                    if info and isinstance(info, dict):
                        strike = _safe_float(info.get("strike"))
                        expiry_str = info.get("expiry", "")
                        right = info.get("right", info.get("putCall", "C")).upper()

                    if (strike <= 0 or not expiry_str) and contract_desc:
                        parts = contract_desc.split()
                        if len(parts) >= 4:  
                            try:
                                right = parts[-1].upper()
                                strike = _safe_float(parts[-2])
                                expiry_dt = datetime.strptime(parts[1], "%d%b%y")
                                expiry_str = expiry_dt.strftime("%Y%m%d")
                            except Exception:
                                pass
                        else:  
                            match = re.match(r"([A-Z]+)\s*(\d{6})([CP])(\d{8})", contract_desc.replace(" ", ""))
                            if match:
                                try:
                                    _, sym_date, sym_right, sym_strike = match.groups()
                                    right = sym_right
                                    strike = float(sym_strike) / 1000.0
                                    expiry_dt = datetime.strptime(sym_date, "%y%m%d")
                                    expiry_str = expiry_dt.strftime("%Y%m%d")
                                except Exception:
                                    pass

                    if spot_price <= 0 or strike <= 0:
                        skipped_positions.append({
                            "contract": contract_desc or ticker,
                            "reason": f"Underlying spot definition context ({spot_price}) or strike ({strike}) unavailable."
                        })
                        continue 
                        
                    try:
                        expiry_dt = datetime.strptime(expiry_str, "%Y%m%d").replace(tzinfo=timezone.utc)
                        dte = max((expiry_dt - datetime.now(timezone.utc)).days, 1)
                    except Exception:
                        try:
                            expiry_dt = datetime.strptime(expiry_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                            dte = max((expiry_dt - datetime.now(timezone.utc)).days, 1)
                        except Exception:
                            dte = 30
                    
                    iv_val = _safe_float(p.get("impliedVol", p.get("impliedVolatility")))
                    ivs_arr.append(iv_val * 100.0 if iv_val > 0 else 32.0)
                    
                    spots_arr.append(spot_price)
                    strikes_arr.append(strike)
                    dtes_arr.append(dte)
                    calls_mask.append(right.startswith("C"))
                    sizes_arr.append(_safe_float(p.get("position")))
                    fx_arr.append(fx_rates.get(p.get("currency", "USD").upper(), 1.0))
                    multipliers_arr.append(_safe_float(p.get("multiplier", 100.0)))
                    yields_arr.append(underlying_yields.get(ticker, 0.0))
                    valid_opts.append(p)
                
                if valid_opts:
                    np_spots = np.array(spots_arr)
                    np_strikes = np.array(strikes_arr)
                    np_dtes = np.array(dtes_arr)
                    np_ivs = np.array(ivs_arr)
                    np_calls = np.array(calls_mask)
                    np_sizes = np.array(sizes_arr)
                    np_fx = np.array(fx_arr)
                    np_mult = np.array(multipliers_arr)
                    np_q = np.array(yields_arr)
                    np_rf = np.full(len(valid_opts), effective_rf)
                    
                    baseline_theory = calculate_bsm_prices_vectorized(np_spots, np_strikes, np_dtes, np_ivs, np_rf, np_calls, np_q)
                    
                    for crash in [10, 20]:
                        linear_loss = total_linear_exposure * (crash / 100.0)
                        
                        skew_factor = np.where(np_calls, 1.15, 1.45 if crash == 20 else 1.25)
                        shocked_ivs = np_ivs * skew_factor
                        shocked_spots = np_spots * (1.0 - crash / 100.0)
                        
                        shocked_theory = calculate_bsm_prices_vectorized(shocked_spots, np_strikes, np_dtes, shocked_ivs, np_rf, np_calls, np_q)
                        contract_losses = (baseline_theory - shocked_theory) * np_sizes * np_mult * np_fx
                        options_loss = float(np.sum(contract_losses))
                        
                        total_combined_loss = linear_loss + options_loss
                        stress_scenarios[f"crash_{crash}pct"] = {
                            "linear_loss": round(linear_loss, 2),
                            "options_convex_impact": round(options_loss, 2),
                            "total_combined_loss": round(total_combined_loss, 2), 
                            "remaining_nlv_estimate": round(nlv - total_combined_loss, 2)
                        }
            
            if not stress_scenarios:
                for crash in [10, 20]:
                    linear_loss = total_linear_exposure * (crash / 100.0)
                    stress_scenarios[f"crash_{crash}pct"] = {
                        "linear_loss": round(linear_loss, 2),
                        "options_convex_impact": 0.0,
                        "total_combined_loss": round(linear_loss, 2), 
                        "remaining_nlv_estimate": round(nlv - linear_loss, 2)
                    }
                
            output_payload = {
                "account_id": acct_id, 
                "nlv_consolidated": round(nlv, 2), 
                "buying_power": round(buying_power, 2), 
                "total_linear_exposure": round(total_linear_exposure, 2), 
                "positions": processed_positions, 
                "stress_scenarios": stress_scenarios
            }
            if skipped_positions:
                output_payload["warnings"] = skipped_positions  
                
            return output_payload
        except Exception as e: 
            return {"error": str(e)}

_cache: Optional[AsyncMemoryCacheManager] = None
_ibkr: Optional[IBKRGateway] = None
_market: Optional[MarketFacade] = None
_macro: Optional[MacroProvider] = None
_portfolio: Optional[PortfolioEngine] = None

@asynccontextmanager
async def app_lifespan(_server: Any) -> AsyncIterator[None]:
    global _cache, _ibkr, _market, _macro, _portfolio
    _cache = AsyncMemoryCacheManager()
    await _cache.init()
    _ibkr = IBKRGateway()
    await _ibkr.start()
    _market = MarketFacade(_ibkr, _cache)
    _macro = MacroProvider(_cache)
    _portfolio = PortfolioEngine(_ibkr)
    yield
    await _ibkr.stop()
    await _market.close()
    await _macro.close()
    await _cache.close()

mcp = FastMCP("ibkr_portmanteau", lifespan=app_lifespan)

@mcp.resource("manuals://exploration_manual")
def exploration_manual() -> str:
    """Global Macro & Market Data Exploration Manual Guidelines"""
    try:
        with open("GLOBAL_DATA_EXPLORATION_MANUAL.md", "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Error loading exploration manual path: {str(e)}"

@mcp.resource("manuals://api_reference")
def api_reference() -> str:
    """IBKR Portmanteau Client Portal API Routing Blueprint"""
    try:
        with open("IBKR_PORTMANTEAU_API_REFERENCE.md", "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Error loading API reference path: {str(e)}"

@mcp.tool(name="search_instrument")
async def search_instrument(query: str) -> str:
    """Search for global financial assets and tickers using free-text search."""
    return json.dumps(await _market.search_instrument(query), indent=2)

@mcp.tool(name="get_market_snapshot")
async def get_market_snapshot(ticker: str, asset_class: str = "STK") -> str:
    """Fetch current asset quote. For global exchanges outside the US, append the proper Yahoo suffix (e.g., Nintendo -> '7974.T')."""
    return json.dumps(await _market.get_spot_price(ticker, asset_class), indent=2)

@mcp.tool(name="get_historical_ohlcv")
async def get_historical_ohlcv(ticker: str, interval: str = "1d", period: str = "3mo", limit: int = 100) -> str:
    """Provide historical market data series (OHLCV). Intervals: '1d', '1wk'. Periods: '3mo', '1y', '5y'."""
    return json.dumps(await _market.get_historical_ohlcv(ticker, interval, period, limit), indent=2)

@mcp.tool(name="get_fx_rate")
async def get_fx_rate(base: str, quote: str) -> str:
    """Extract real-time spot exchange rates between two currency layers (e.g., base='EUR', quote='CHF')."""
    return json.dumps(await _market.get_fx_rate(base, quote), indent=2)

@mcp.tool(name="get_options_chain_with_greeks")
async def get_options_chain_with_greeks(ticker: str, max_expiry_days: int = 60, moneyness_range: float = 0.15, risk_free_rate: Optional[float] = None) -> str:
    """Return the options liquidity matrix, injecting dynamic Newton-Raphson derived implied volatility, Delta, Gamma, Vega, and Theta metrics."""
    return json.dumps(await _market.get_options_chain_with_greeks(ticker, max_expiry_days, moneyness_range, risk_free_rate), indent=2)

@mcp.tool(name="get_portfolio_summary")
async def get_portfolio_summary(risk_free_rate: Optional[float] = None) -> str:
    """Pull real-time balances, multi-currency assets net liquidation value, and active exposure stress-testing metrics."""
    return json.dumps(await _portfolio.analyze(_market, risk_free_rate=risk_free_rate), indent=2)

@mcp.tool(name="get_global_macro_scanner")
async def get_global_macro_scanner(indicator_id: str, region: str) -> str:
    """Dynamic global macro analytics matrix. Supports semantic aliases or direct codes."""
    return json.dumps(await _macro.fetch_dynamic_indicator(indicator_id, region), indent=2)

@mcp.tool(name="query_ibkr_endpoint")
async def query_ibkr_endpoint(endpoint: str, params_json: Optional[str] = None) -> str:
    """Bypass high-level tools and execute direct queries against the IBKR Client Portal REST API."""
    clean_endpoint = endpoint.lower().strip().lstrip("/")
    
    dangerous_patterns = ["order", "trade", "buy", "sell", "submit", "replace", "cancel", "delete", "modify"]
    if any(p in clean_endpoint for p in dangerous_patterns):
        return json.dumps({"error": "Unauthorized: Command string contains destructive or non-read-only state-altering structural keywords."})
        
    allowed_post_endpoints = ["iserver/scanner/run", "tickle", "logout"]
    # Fixed route breaking validation by utilizing prefix containment patterns instead of rigid absolute matching rules
    is_explicit_post_whitelist = any(clean_endpoint.startswith(path) for path in allowed_post_endpoints)
    
    try:
        p = json.loads(params_json) if params_json else {}
        url = f"{IBKR_BASE}/{endpoint.lstrip('/')}"
        
        if is_explicit_post_whitelist:
            resp = await _ibkr.client.post(url, json=p)
        else:
            resp = await _ibkr.client.get(url, params=p)
            
        return json.dumps(resp.json(), indent=2)
    except Exception as e: 
        return json.dumps({"error": str(e)})

@mcp.tool(name="search_fred_series")
async def search_fred_series(query: str) -> str:
    """Search the FRED database for economic series identifiers matching a query string."""
    if not FRED_API_KEY: 
        return json.dumps({"error": "FRED API Key not configured"})
    try:
        url = "https://api.stlouisfed.org/fred/series/search"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params={"search_text": query, "api_key": FRED_API_KEY, "file_type": "json", "limit": 10})
            if resp.status_code == 200:
                ser = resp.json().get("seriess", [])
                results = [{"id": s.get("id"), "title": s.get("title"), "frequency": s.get("frequency"), "units": s.get("units")} for s in ser]
                return json.dumps({"query": query, "results": results}, indent=2)
            return json.dumps({"error": f"FRED API returned unexpected status code {resp.status_code}"})
    except Exception as e: 
        return json.dumps({"error": str(e)})

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", default="stdio", choices=["stdio", "sse"])
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    mcp.run(transport=args.transport, port=args.port)
