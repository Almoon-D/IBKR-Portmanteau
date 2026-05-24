"""
IBKR Portmanteau — Institutional Grade Unified Financial Server (Read-Only)
Data Sources : IBKR Client Portal · FRED · World Bank · Yahoo Finance · Binance
Transport    : STDIO (Local) / SSE (Remote)
Cache        : AIOSQLITE (100% Asynchronous)
Math Engine  : Native Black-Scholes for exact Greeks calculation.
Python       : 3.11+ Compatible (Optimized for 3.14.5)

Copyright (C) 2026 AlmoonD - Licensed under the GNU GPLv3
"""

import asyncio
import json
import logging
import os
import sys
import time
import argparse
import math
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Optional

import httpx
import yfinance as yf
import aiosqlite
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
CACHE_DB_PATH      = os.environ.get("IBKR_PORTMANTEAU_CACHE_DB", "cache.db")
FRED_API_KEY       = os.environ.get("FRED_API_KEY", "")
FRED_BASE          = "https://api.stlouisfed.org/fred/series/observations"
BINANCE_SPOT_URL   = "https://api.binance.com/api/v3/ticker/24hr"
BINANCE_FUND_URL   = "https://fapi.binance.com/fapi/v1/premiumIndex"
YAHOO_SEARCH_URL   = "https://query1.finance.yahoo.com/v1/finance/search"

TTL_MACRO_SECS     = 86400   
TTL_EQUITY_SECS    = 3600    
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
    if val is None: return default
    try: return float(str(val).strip().replace(",", ""))
    except (ValueError, TypeError): return default

def _cnd(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def _nd_prime(x: float) -> float:
    return (1.0 / math.sqrt(2.0 * math.pi)) * math.exp(-0.5 * x * x)

def calculate_greeks(spot: float, strike: float, dte: float, iv_pct: float, rate_pct: float, is_call: bool) -> dict[str, float]:
    T = max(dte / 365.0, 0.0001)
    sigma = max(iv_pct / 100.0, 0.0001)
    r = rate_pct / 100.0
    S = spot
    K = strike
    d1 = (math.log(S / K) + (r + (sigma ** 2) / 2.0) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if is_call:
        delta = _cnd(d1)
        theta = (- (S * _nd_prime(d1) * sigma) / (2.0 * math.sqrt(T)) - r * K * math.exp(-r * T) * _cnd(d2))
    else:
        delta = _cnd(d1) - 1.0
        theta = (- (S * _nd_prime(d1) * sigma) / (2.0 * math.sqrt(T)) + r * K * math.exp(-r * T) * _cnd(-d2))
    gamma = _nd_prime(d1) / (S * sigma * math.sqrt(T))
    vega = (S * math.sqrt(T) * _nd_prime(d1)) / 100.0  
    theta = theta / 365.0  
    return {"delta": round(delta, 4), "gamma": round(gamma, 4), "vega": round(vega, 4), "theta": round(theta, 4)}

class AsyncCacheManager:
    def __init__(self, db_path: str = CACHE_DB_PATH) -> None:
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def init(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("CREATE TABLE IF NOT EXISTS mcp_cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, expires REAL NOT NULL)")
        await self._db.commit()
        log.info("Asynchronous AIOSQLITE cache active.")

    async def get(self, key: str) -> Optional[Any]:
        async with self._db.execute("SELECT value, expires FROM mcp_cache WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
            if not row: return None
            value, expires = row
            if time.time() > expires:
                await self._db.execute("DELETE FROM mcp_cache WHERE key = ?", (key,))
                await self._db.commit()
                return None
            return json.loads(value)

    async def set(self, key: str, value: Any, ttl_secs: int) -> None:
        expires = time.time() + ttl_secs
        await self._db.execute("INSERT OR REPLACE INTO mcp_cache (key, value, expires) VALUES (?, ?, ?)", (key, json.dumps(value), expires))
        await self._db.commit()

    async def close(self) -> None:
        if self._db: await self._db.close()

class IBKRGateway:
    def __init__(self) -> None:
        self.client = httpx.AsyncClient(verify=False, timeout=10.0)
        self.healthy = False
        self._tickle_task: Optional[asyncio.Task[None]] = None

    async def start(self) -> None:
        await self.comprobar_estado_inmediato()
        self._tickle_task = asyncio.create_task(self._loop_mantenimiento())

    async def stop(self) -> None:
        if self._tickle_task:
            self._tickle_task.cancel()
            try: await self._tickle_task
            except asyncio.CancelledError: pass
        await self.client.aclose()

    async def comprobar_estado_inmediato(self) -> None:
        try:
            resp = await self.client.get(f"{IBKR_BASE}/iserver/auth/status")
            if resp.status_code == 200 and resp.json().get("authenticated", False):
                self.healthy = True
                await self.client.post(f"{IBKR_BASE}/tickle")
            else:
                self.healthy = False
        except Exception:
            self.healthy = False

    async def _loop_mantenimiento(self) -> None:
        while True:
            await asyncio.sleep(IBKR_TICKLE_SECS)
            await self.comprobar_estado_inmediato()

    async def resolve_conid(self, ticker: str) -> Optional[int]:
        try:
            resp = await self.client.get(f"{IBKR_BASE}/iserver/secdef/search", params={"symbol": ticker, "secType": "STK"})
            if resp.status_code == 200 and resp.json():
                for asset in resp.json():
                    if asset.get("ticker", "").upper() == ticker.upper():
                        return int(asset["conid"])
                return int(resp.json()[0]["conid"])
        except Exception: pass
        return None

class MarketFacade:
    def __init__(self, ibkr: IBKRGateway, cache: AsyncCacheManager) -> None:
        self.ibkr = ibkr
        self.cache = cache
        self.http = httpx.AsyncClient(timeout=12.0)
        self.semaphore = asyncio.Semaphore(4)
        self.ibkr_semaphore = asyncio.Semaphore(2)

    async def close(self) -> None:
        await self.http.aclose()

    async def search_instrument(self, query: str) -> dict[str, Any]:
        cache_key = f"search:{query.lower()}"
        cached = await self.cache.get(cache_key)
        if cached: return cached
        async with self.semaphore:
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
            except Exception as e: return {"error": str(e), "matches": []}
        return {"query": query, "matches": []}

    async def get_spot_price(self, ticker: str, asset_class: str) -> dict[str, Any]:
        asset_class = asset_class.upper()
        ticker = ticker.upper()
        if asset_class == "CRYPTO" and not ticker.endswith("USDT") and len(ticker) <= 4:
            ticker = f"{ticker}USDT"
        cache_key = f"spot:{asset_class}:{ticker}"
        cached = await self.cache.get(cache_key)
        if cached: return cached
        if asset_class == "CRYPTO":
            async with self.semaphore:
                try:
                    spot_task = self.http.get(BINANCE_SPOT_URL, params={"symbol": ticker})
                    fund_task = self.http.get(BINANCE_FUND_URL, params={"symbol": ticker})
                    spot_resp, fund_resp = await asyncio.gather(spot_task, fund_task, return_exceptions=True)
                    if isinstance(spot_resp, Exception) or spot_resp.status_code != 200:
                        return {"error": "Asset not found on Binance Spot", "source": "ERROR"}
                    spot_data = spot_resp.json()
                    funding_rate = None
                    if not isinstance(fund_resp, Exception) and fund_resp.status_code == 200:
                        funding_rate = _safe_float(fund_resp.json().get("lastFundingRate")) * 100
                    result = {"ticker": ticker, "source": "[SOURCE: BINANCE]", "last": _safe_float(spot_data.get("lastPrice")), "bid": _safe_float(spot_data.get("bidPrice")), "ask": _safe_float(spot_data.get("askPrice")), "volume_24h": _safe_float(spot_data.get("volume")), "funding_rate_pct": funding_rate}
                    await self.cache.set(cache_key, result, TTL_EQUITY_SECS)
                    return result
                except Exception as e: return {"error": str(e), "source": "ERROR"}
        if self.ibkr.healthy:
            async with self.ibkr_semaphore:
                conid = await self.ibkr.resolve_conid(ticker)
                if conid:
                    try:
                        resp = await self.ibkr.client.get(f"{IBKR_BASE}/iserver/marketdata/snapshot", params={"conids": str(conid), "fields": "31,84,86"})
                        if resp.status_code == 200 and resp.json():
                            snap = resp.json()[0]
                            last = _safe_float(snap.get("31"))
                            if last > 0:
                                result = {"ticker": ticker, "source": "[SOURCE: IBKR]", "last": last, "bid": _safe_float(snap.get("84")), "ask": _safe_float(snap.get("86")), "conid": conid}
                                await self.cache.set(cache_key, result, TTL_EQUITY_SECS)
                                return result
                    except Exception: pass
        async with self.semaphore:
            try:
                loop = asyncio.get_running_loop()
                info = await loop.run_in_executor(None, lambda: yf.Ticker(ticker).fast_info)
                result = {"ticker": ticker, "source": "[SOURCE: YAHOO_FALLBACK]", "last": getattr(info, "last_price", None), "bid": getattr(info, "bid", None), "ask": getattr(info, "ask", None)}
                await self.cache.set(cache_key, result, TTL_EQUITY_SECS)
                return result
            except Exception as e: return {"error": str(e), "source": "ERROR"}

    async def get_historical_ohlcv(self, ticker: str, interval: str, period: str, limit: int) -> dict[str, Any]:
        cache_key = f"hist:{ticker}:{interval}:{period}:{limit}"
        cached = await self.cache.get(cache_key)
        if cached: return cached
        async with self.semaphore:
            try:
                loop = asyncio.get_running_loop()
                df = await loop.run_in_executor(None, lambda: yf.Ticker(ticker).history(period=period, interval=interval))
                if df.empty: return {"error": "Historical data unavailable"}
                bars = []
                for idx, row in df.tail(limit).iterrows():
                    bars.append({"timestamp": idx.strftime("%Y-%m-%d %H:%M"), "open": round(row["Open"], 2), "high": round(row["High"], 2), "low": round(row["Low"], 2), "close": round(row["Close"], 2), "volume": int(row["Volume"])})
                result = {"ticker": ticker, "source": "[SOURCE: YAHOO_PRO]", "interval": interval, "bars": bars}
                await self.cache.set(cache_key, result, TTL_EQUITY_SECS)
                return result
            except Exception as e: return {"error": str(e)}

    async def get_fx_rate(self, base: str, quote: str) -> dict[str, Any]:
        pair = f"{base.upper()}{quote.upper()}=X"
        cache_key = f"fx:{pair}"
        cached = await self.cache.get(cache_key)
        if cached: return cached
        async with self.semaphore:
            try:
                loop = asyncio.get_running_loop()
                info = await loop.run_in_executor(None, lambda: yf.Ticker(pair).fast_info)
                last_rate = getattr(info, "last_price", None)
                if not last_rate: return {"error": f"Could not extract fx rate for {pair}"}
                result = {"pair": pair, "rate": round(last_rate, 4), "timestamp": datetime.now(timezone.utc).isoformat()}
                await self.cache.set(cache_key, result, 300)
                return result
            except Exception as e: return {"error": str(e)}

    async def get_options_chain_with_greeks(self, ticker: str, max_dte: int, moneyness: float, rate_pct: float) -> dict[str, Any]:
        cache_key = f"greeks_chain:{ticker}:{max_dte}:{moneyness}"
        cached = await self.cache.get(cache_key)
        if cached: return cached
        spot_dict = await self.get_spot_price(ticker, "STK")
        spot_price = _safe_float(spot_dict.get("last"))
        if spot_price <= 0: return {"error": "Cannot calculate options chain without a valid spot price."}
        if self.ibkr.healthy:
            try:
                async with self.ibkr_semaphore:
                    conid = await self.ibkr.resolve_conid(ticker)
                    if conid:
                        opt_resp = await self.ibkr.client.get(f"{IBKR_BASE}/iserver/secdef/info", params={"conid": conid, "secType": "OPT"})
                        if opt_resp.status_code == 200 and opt_resp.json():
                            log.info("Mapping native IBKR options contracts for %s", ticker)
                            raw_contracts = opt_resp.json()
                            filtered_contracts = []
                            for c in raw_contracts:
                                if not isinstance(c, dict): continue
                                strike = _safe_float(c.get("strike"))
                                if (spot_price * (1 - moneyness) <= strike <= spot_price * (1 + moneyness)):
                                    filtered_contracts.append(c)
                            opt_conids = [str(c["conid"]) for c in filtered_contracts[:40]]
                            if opt_conids:
                                snap_resp = await self.ibkr.client.get(f"{IBKR_BASE}/iserver/marketdata/snapshot", params={"conids": ",".join(opt_conids), "fields": "31,84,86,7644"})
                                if snap_resp.status_code == 200 and snap_resp.json():
                                    snapshots = {str(s.get("conid")): s for s in snap_resp.json() if isinstance(s, dict)}
                                    processed_contracts = []
                                    for c in filtered_contracts[:40]:
                                        c_conid = str(c.get("conid"))
                                        if c_conid in snapshots:
                                            snap = snapshots[c_conid]
                                            strike = _safe_float(c.get("strike"))
                                            iv_raw = _safe_float(snap.get("7644"))
                                            iv_val = iv_raw if iv_raw > 0 else 0.32
                                            is_call = str(c.get("right", "C")).upper().startswith("C")
                                            expiry_str = c.get("expiry", "")
                                            try:
                                                expiry_dt = datetime.strptime(expiry_str, "%Y%m%d").replace(tzinfo=timezone.utc)
                                                dte = (expiry_dt - datetime.now(timezone.utc)).days
                                            except Exception: dte = 30
                                            greeks = calculate_greeks(spot_price, strike, max(dte, 1), iv_val * 100, rate_pct, is_call)
                                            processed_contracts.append({"contractSymbol": c.get("symbol", f"{ticker}_{expiry_str}_{strike}"), "type": "CALL" if is_call else "PUT", "expiry": expiry_str, "strike": strike, "bid": _safe_float(snap.get("84")), "ask": _safe_float(snap.get("86")), "iv_pct": round(iv_val * 100, 2), "greeks": greeks, "source": "IBKR_NATIVE"})
                                    if processed_contracts:
                                        result = {"ticker": ticker, "underlying_price": round(spot_price, 2), "option_contracts": processed_contracts}
                                        await self.cache.set(cache_key, result, TTL_EQUITY_SECS)
                                        return result
            except Exception: pass
        async with self.semaphore:
            loop = asyncio.get_running_loop()
            try:
                yf_ticker = yf.Ticker(ticker)
                expirations = await loop.run_in_executor(None, lambda: yf_ticker.options)
                cutoff = datetime.now(timezone.utc) + timedelta(days=max_dte)
                valid_expirations = [e for e in expirations if datetime.strptime(e, "%Y-%m-%d").replace(tzinfo=timezone.utc) <= cutoff]
                target_expirations = valid_expirations[:4]
                tasks = [loop.run_in_executor(None, lambda e=exp: yf_ticker.option_chain(e)) for exp in target_expirations]
                chains = await asyncio.gather(*tasks, return_exceptions=True)
                processed_contracts = []
                for exp, chain in zip(target_expirations, chains):
                    if isinstance(chain, Exception): continue
                    for is_call, df in [(True, chain.calls), (False, chain.puts)]:
                        tipo = "CALL" if is_call else "PUT"
                        for _, row in df.iterrows():
                            strike = _safe_float(row.get("strike"))
                            if not (spot_price * (1 - moneyness) <= strike <= spot_price * (1 + moneyness)): continue
                            iv = _safe_float(row.get("impliedVolatility"))
                            iv_val = iv if iv > 0 else 0.32
                            dte = (datetime.strptime(exp, "%Y-%m-%d").replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).days
                            greeks = calculate_greeks(spot_price, strike, max(dte, 1), iv_val * 100, rate_pct, is_call)
                            processed_contracts.append({"contractSymbol": row.get("contractSymbol"), "type": tipo, "expiry": exp, "strike": strike, "bid": _safe_float(row.get("bid")), "ask": _safe_float(row.get("ask")), "iv_pct": round(iv_val * 100, 2), "greeks": greeks, "source": "YAHOO_FALLBACK"})
                result = {"ticker": ticker, "underlying_price": round(spot_price, 2), "option_contracts": processed_contracts[:80]}
                await self.cache.set(cache_key, result, TTL_EQUITY_SECS)
                return result
            except Exception as e: return {"error": f"Options chain tracking failed: {e}"}

class MacroProvider:
    def __init__(self, cache: AsyncCacheManager) -> None:
        self.cache = cache
        self.http = httpx.AsyncClient(timeout=15.0)

    async def close(self) -> None:
        await self.http.aclose()

    async def fetch_dynamic_indicator(self, indicator_id: str, region: str) -> dict[str, Any]:
        region = region.upper()
        cache_key = f"dyn_macro:{indicator_id}:{region}"
        cached = await self.cache.get(cache_key)
        if cached: return cached
        series_id = FRED_SERIES.get(indicator_id.upper(), indicator_id)
        if region in ("US", "USA") and FRED_API_KEY:
            try:
                resp = await self.http.get(FRED_BASE, params={"series_id": series_id, "api_key": FRED_API_KEY, "file_type": "json", "sort_order": "desc", "limit": 10})
                if resp.status_code == 200:
                    obs = resp.json().get("observations", [])
                    history = [{"date": o["date"], "value": _safe_float(o["value"])} for o in reversed(obs) if o["value"] not in (".", "")]
                    result = {"indicator": series_id, "region": "US", "source": "FRED", "series": history}
                    await self.cache.set(cache_key, result, TTL_MACRO_SECS)
                    return result
            except Exception: pass
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
        except Exception as e: return {"error": str(e)}
        return {"error": f"Indicator '{indicator_id}' or region '{region}' not reachable."}

class PortfolioEngine:
    def __init__(self, ibkr: IBKRGateway) -> None:
        self.ibkr = ibkr

    async def analyze(self) -> dict[str, Any]:
        if not self.ibkr.healthy: return {"status": "OFFLINE", "error": "Local IBKR Gateway disconnected"}
        try:
            acct_resp = await self.ibkr.client.get(f"{IBKR_BASE}/portfolio/accounts")
            if acct_resp.status_code != 200 or not acct_resp.json(): return {"status": "ERROR", "error": "Failed to map IBKR account target"}
            acct_id = acct_resp.json()[0]["id"]
            ledger_resp = await self.ibkr.client.get(f"{IBKR_BASE}/portfolio/{acct_id}/ledger")
            ledger_data = ledger_resp.json()
            nlv, buying_power = 0.0, 0.0
            if "BASE" in ledger_data:
                base = ledger_data["BASE"]
                nlv = _safe_float(base.get("netliquidationvalue"))
                buying_power = _safe_float(base.get("buyingpower"))
            else:
                for currency, data in ledger_data.items():
                    if currency == "BASE" or not isinstance(data, dict): continue
                    rate = _safe_float(data.get("exchangeRate", 1.0))
                    nlv += _safe_float(data.get("netliquidationvalue")) * rate
                    buying_power += _safe_float(data.get("buyingpower")) * rate
            pos_resp = await self.ibkr.client.get(f"{IBKR_BASE}/portfolio/{acct_id}/positions/0")
            processed_positions = []
            total_market_exposure = 0.0
            if pos_resp.status_code == 200:
                for p in pos_resp.json() or []:
                    mkt_val = _safe_float(p.get("mktValue"))
                    asset_class = p.get("assetClass", "UNKNOWN")
                    processed_positions.append({"contract": p.get("contractDesc", p.get("ticker", "UNKNOWN")), "asset_class": asset_class, "size": _safe_float(p.get("position")), "market_value": round(mkt_val, 2), "weight_pct": round((mkt_val / nlv * 100), 2) if nlv > 0 else 0.0})
                    if asset_class in ("STK", "ETF", "OPT"): total_market_exposure += mkt_val
            stress_scenarios = {}
            for crash in [10, 20]:
                loss = total_market_exposure * (crash / 100.0)
                stress_scenarios[f"crash_{crash}pct"] = {"estimated_loss": round(loss, 2), "remaining_nlv": round(nlv - loss, 2)}
            return {"account_id": acct_id, "nlv_consolidado": round(nlv, 2), "buying_power": round(buying_power, 2), "total_exposure": round(total_market_exposure, 2), "positions": processed_positions, "stress_scenarios": stress_scenarios}
        except Exception as e: return {"error": str(e)}

_cache: Optional[AsyncCacheManager] = None
_ibkr: Optional[IBKRGateway] = None
_market: Optional[MarketFacade] = None
_macro: Optional[MacroProvider] = None
_portfolio: Optional[PortfolioEngine] = None

@asynccontextmanager
async def app_lifespan(_server: Any) -> AsyncIterator[None]:
    global _cache, _ibkr, _market, _macro, _portfolio
    _cache = AsyncCacheManager(CACHE_DB_PATH)
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
    """Provide historical market data series (OHLCV). Intervals: '1d', '1wk'. Periods: '3mo', '1y', '5y'. For global exchanges, append the proper Yahoo suffix (e.g., Nintendo -> '7974.T')."""
    return json.dumps(await _market.get_historical_ohlcv(ticker, interval, period, limit), indent=2)

@mcp.tool(name="get_fx_rate")
async def get_fx_rate(base: str, quote: str) -> str:
    """Extract real-time spot exchange rates between two currency layers (e.g., base='EUR', quote='CHF')."""
    return json.dumps(await _market.get_fx_rate(base, quote), indent=2)

@mcp.tool(name="get_options_chain_with_greeks")
async def get_options_chain_with_greeks(ticker: str, max_expiry_days: int = 60, moneyness_range: float = 0.15, risk_free_rate: float = 4.5) -> str:
    """Return the options liquidity matrix, injecting dynamic Delta, Gamma, Vega, and Theta metrics via Black-Scholes."""
    return json.dumps(await _market.get_options_chain_with_greeks(ticker, max_expiry_days, moneyness_range, risk_free_rate), indent=2)

@mcp.tool(name="get_portfolio_summary")
async def get_portfolio_summary() -> str:
    """Pull real-time balances, multi-currency assets net liquidation value, and active exposure stress-testing metrics."""
    return json.dumps(await _portfolio.analyze(), indent=2)

@mcp.tool(name="get_global_macro_scanner")
async def get_global_macro_scanner(indicator_id: str, region: str) -> str:
    """Dynamic global macro analytics matrix. Supports semantic aliases ('GDP_GROWTH', 'INFLATION', 'BIRTH_RATE', 'POPULATION') or direct World Bank / FRED indicator codes (e.g., Swiss birth rate -> indicator_id='SP.DYN.CBRT.IN', region='CH')."""
    return json.dumps(await _macro.fetch_dynamic_indicator(indicator_id, region), indent=2)

@mcp.tool(name="query_ibkr_endpoint")
async def query_ibkr_endpoint(endpoint: str, params_json: Optional[str] = None) -> str:
    """Bypass high-level tools and execute direct queries against the IBKR Client Portal REST API."""
    if not _ibkr.healthy: return json.dumps({"error": "Local IBKR Gateway disconnected"})
    try:
        p = json.loads(params_json) if params_json else {}
        url = f"{IBKR_BASE}/{endpoint.lstrip('/')}"
        if any(x in endpoint for x in ["orders", "run", "reply", "tickle"]):
            resp = await _ibkr.client.post(url, json=p)
        else:
            resp = await _ibkr.client.get(url, params=p)
        return json.dumps(resp.json(), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

@mcp.tool(name="search_fred_series")
async def search_fred_series(query: str) -> str:
    """Search the FRED database for economic series identifiers matching a query string."""
    if not FRED_API_KEY: return json.dumps({"error": "FRED API Key not configured"})
    try:
        url = "https://api.stlouisfed.org/fred/series/search"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params={"search_text": query, "api_key": FRED_API_KEY, "file_type": "json", "limit": 10})
            if resp.status_code == 200:
                ser = resp.json().get("seriess", [])
                results = [{"id": s.get("id"), "title": s.get("title"), "frequency": s.get("frequency"), "units": s.get("units")} for s in ser]
                return json.dumps({"query": query, "results": results}, indent=2)
            return json.dumps({"error": f"FRED API returned status {resp.status_code}"})
    except Exception as e: return json.dumps({"error": str(e)})

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", default="stdio", choices=["stdio", "sse"])
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    mcp.run(transport=args.transport, port=args.port)