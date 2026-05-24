# IBKR Portmanteau 💼🌐

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Python Version](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](https://www.python.org/)
[![Model Context Protocol](https://img.shields.io/badge/MCP-Supported-brightgreen)](https://modelcontextprotocol.io/)
[![GitHub Sponsor](https://img.shields.io/badge/Sponsor-GitHub-Repository-pink.svg?style=flat-square&logo=github-sponsors)](https://github.com/sponsors/Almoon-D)
[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20Me%20A%20Coffee-Donate-orange.svg?style=flat-square&logo=buy-me-a-coffee)](https://buymeacoffee.com/almoon.d)

**IBKR Portmanteau** is a unified financial server built on the **Model Context Protocol (MCP)** standard. Designed strictly as a **Read-Only** gateway, it provides an ultra-secure channel for your Large Language Models (LLMs) within environments like Cursor, Claude Desktop, ChatGPT, Gemini or VS Code to explore, analyze, and extract advanced metrics from your portfolio and global markets in real time.

## 🌐 Deploy Anywhere (Local & Cloud Ready)
Whether you want to run it on your own machine for quick analysis or host it on a private online server (VPS) for a 24/7, always-on AI data bridge, the core architecture is built to stay lightweight, stable, safe and completely decoupled from a single desktop environment.

### Built-in Native Bridges:
* **Interactive Brokers (Client Portal API)**: Real-time portfolio balances, consolidated positions, and optional ***on-platform* market data snapshots.**
* **FRED (Federal Reserve Bank of St. Louis)** & **World Bank**: Dynamic global macroeconomic scanner utilizing direct passthrough queries.
* **Yahoo Finance**: Automated fallback layer for global multi-market equity quotes, tailorable historical data windows, and full options chains.
* **Binance**: Spot cryptocurrency data feeds tracking prices, execution spreads, and concurrent funding rates.

---

## 🛠️ System Requirements

The server architecture is built to run seamlessly across the following Python environments:
* **Python 3.11, 3.12, and 3.13** (Full production backward compatibility).
* **Python 3.14.5** (Fully optimized execution leveraging the latest asynchronous event loop performance enhancements).

All runtime dependencies are strictly managed within the accompanying `requirements.txt` file.

---

## 💻 Local Installation & Setup Guide (PC)

### 1. Clone the Repository & Prepare the Environment
Open your terminal and run the following commands to set up an isolated virtual environment:

```bash
git clone https://github.com/YOUR_USERNAME/ibkr-portmanteau.git
cd ibkr-portmanteau

# Create and activate the virtual environment
python -m venv venv
source venv/bin/activate  # On Windows use: venv\Scripts\activate

# Install validated production dependencies
pip install -r requirements.txt
```

### 2. Set Up the IBKR Client Portal Gateway
This MCP server requires local HTTP bridge access to the official Interactive Brokers gateway.

1. **Download**: Visit the official [IBKR Client Portal API Documentation](https://interactivebrokers.github.io/cpwebapi/) and download the latest zip architecture for the *Client Portal Gateway*.
2. **Extraction**: Unzip the contents to your machine (ideally outside of this repository workspace).
3. **Configuration**: If you need to modify secure network hosts or interfaces, edit the configuration profile under `root/conf/conf.yaml` (it runs securely on `https://localhost:5000` by default).
4. **Execution**:
   * **Linux/macOS**: Run `bin/run.sh conf/conf.yaml`
   * **Windows**: Run `bin/run.bat conf/conf.yaml`
5. **Authentication**: Open your preferred local browser and navigate to `https://localhost:5000`. Input your Interactive Brokers credentials and approve the Two-Factor Authentication (2FA) push notification on your mobile device.
   * *Note: Once the gateway shows an active, authenticated session status page, the MCP server will instantly hook into the pipeline upon startup.*

### 3. Environment Variables (Optional)
To activate the macroeconomic data tracking engine via FRED, expose your API token:
```bash
export FRED_API_KEY="your_api_key_here"
# If your IBKR Gateway runs on a custom host or interface:
export IBKR_GATEWAY_URL="https://localhost:5000/v1/api"
```

### 4. Running the MCP Server
To spin up the server instance under the standard STDIO transport layer (fully integrated with local developer workflows):
```bash
python server.py --transport stdio
```

---

## ☁️ Cloud Deployment Guide (Oracle Cloud / VPS)

Because this server operates purely in **Read-Only** mode, it is perfectly suited for cloud topologies due to the total absence of capital execution risk. However, due to Interactive Brokers' aggressive anti-bot perimeter defenses (Cloudflare/Akamai), **it is highly discouraged to run the Java Client Portal Gateway directly on a corporate data center IP address**, as web logins will be blocked immediately by CAPTCHAs.

The optimal design pattern to connect securely from any remote machine relies on a secure hybrid mesh architecture:

### Recommended Network Architecture via Tailscale (VPN Mesh)
```
[Your Remote Laptop / Workspace]
             ↓ (Private, encrypted WireGuard mesh tunnel)
       [Tailscale Network]
             ↓
[Your Cloud VPS Instance (MCP Server)] ──→ [Yahoo/Binance/FRED/World Bank]
             ↓ (Internal mesh traffic routed securely home)
[Your Home PC (Active Client Portal Gateway Bridge)]
```

### Deployment Steps on Oracle Cloud (Ubuntu Compute Instance):
1. **Install Tailscale**: Install the Tailscale agent on both your home machine (where the IBKR Gateway runs) and your Oracle Cloud instance. This creates a secure peer-to-peer network overlay without opening raw firewall ports.
2. **Route the Gateway API Pipeline**: Configure the network pointer variable on your cloud instance to route traffic through your home machine's private Tailscale node IP:
   ```bash
   export IBKR_GATEWAY_URL="https://100.X.X.X:5000/v1/api"  # Your Home Machine's Tailscale IP
   ```
3. **Deploy the Environment**: Clone this repository into your cloud instance, install the updated `requirements.txt` manifests, and launch the server. Use the SSE (Server-Sent Events) transport flag if exposing the server to external API gateways, or keep it on `stdio` if your LLM client runs natively on that same machine virtual stack.

---

## 📊 Exposed MCP Tools

Once initialized, the server automatically registers the following analytical capabilities directly into your LLM's context window:

1. `search_instrument`: Free-text asset discovery across global equity networks to resolve unknown symbols.
2. `get_market_snapshot`: Real-time quote captures, including order book spreads (Bid/Ask) and volatility indices (built-in automated handling for global tickers like Nintendo via `7974.T`).
3. `get_historical_ohlcv`: Custom financial time-series bars (OHLCV) with dynamic window framing variables (`limit`).
4. `get_fx_rate`: Spot currency exchange rate conversion matching live data layers (essential for multi-asset opportunistic accounts).
5. `get_options_chain_with_greeks`: Options liquidity chain ingestion, computing real-time **Delta, Gamma, Vega, and Theta** metrics via a native Black-Scholes mathematical engine.
6. `get_portfolio_summary`: Automated consolidated balance sweeps, Net Liquidation Value (NLV) calculations, and **immediate portfolio stress-testing scenarios under market drops (-10%, -20%)**.

---

## ☕ Donations & Support

If this unified server helps you optimize your portfolio, evaluate tail risks more effectively, or saves you time linking Interactive Brokers data to custom AI tooling, consider supporting the continuous maintenance of the project via GitHub Sponsors or Buy Me a Coffee.

[![GitHub Sponsor](https://img.shields.io/badge/Sponsor-GitHub-Repository-pink.svg?style=flat-square&logo=github-sponsors)](https://github.com/sponsors/Almoon-D)
[!["Buy Me A Coffee"](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://buymeacoffee.com/almoon.d)

---

## Star History

<a href="https://www.star-history.com/?repos=Almoon-D%2FIBKR-Portmanteau&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=Almoon-D/IBKR-Portmanteau&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=Almoon-D/IBKR-Portmanteau&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=Almoon-D/IBKR-Portmanteau&type=date&legend=top-left" />
 </picture>
</a>

---

## 📄 License

This repository is protected and distributed under the **GNU GPLv3** license guidelines. Review the `LICENSE` manifest file for details.

Copyright (C) 2026 AlmoonD. All rights reserved.
