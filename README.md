# PolyBench: Benchmarking LLM Forecasting and Trading Capabilities on Live Prediction Market Data

Welcome to the official repository for **PolyBench**, the first large-scale, contamination-proof benchmark that evaluates Large Language Models (LLMs) as autonomous trading agents on live decentralized prediction markets.

This repository contains the full data collection, alignment, AI assessment, and trading execution pipeline introduced in our paper:  
*PolyBench: Benchmarking LLM Forecasting and Trading Capabilities on Live Prediction Market Data* 

> Download dataset from [OneDrive Link](https://1drv.ms/u/c/4d62feca782041b1/IQDR4BGbCmakS7Cid8OzCcHxAR-iaHdX0WPtmmdWQ_ab2Tg?e=cHQ2MZ), or use the repo to create your own!

---

## 📖 Overview

Predicting real-world events from live market signals demands systems that fuse qualitative news with quantitative order-book dynamics under strict temporal discipline. Existing benchmarks often reduce forecasting to static text question answering.

We present **PolyBench**, a multimodal evaluation framework built on Polymarket data that synchronously couples:
1. **Event Resolution Criteria**: Strict conditional definitions and settlement rules.
2. **Central Limit Order Book (CLOB) States**: Real-time liquidity, bid-ask spreads, and midpoint pricing.
3. **Exogenous News Streams**: Pre-fetched Google News context aligned temporally with the market snapshot.

Using PolyBench, we evaluated seven state-of-the-art LLMs (e.g., Gemini-3-Flash, MiMo-V2-Flash, DeepSeek-V3.2, GPT-OSS-120B) across 38,666 binary prediction markets spanning 4,997 events. The framework measures their practical financial viability via our proposed **Confidence-Weighted Return (CWR)**, Annualized Percentage Yield (APY), and Sharpe ratio.

---

## 🛠 Project Architecture

This is a self-contained, standalone CLI intended for continuous market evaluation and batch processing:

```text
poly-analysis/
├── config.py              # Central Configuration (API keys, constraints)
├── .env                   # Environment variables for secure credential storage
├── main.py                # Main CLI entry point
│
├── core/                  # Core Business Logic
│   ├── analysis.py        # LLM inference engine & heuristic prompt building
│   ├── market_data.py     # Polymarket Gamma API and CLOB interactions 
│   ├── news_fetcher.py    # Exogenous context scraping pipeline
│   ├── trading_engine.py  # Polygon blockchain transaction builders
│
├── batch/                 # Automated & Sequential Processing
│   ├── unified_batch.py   # Consolidated pipeline for Data Prefetch, AI healing, Error Recovery, and Backtesting
│
├── database/              # Storage Layer
│   ├── peewee_models.py   # ORM Schema (Markets, Snapshots, Predictions)
│   ├── polymarket.db      # Live interactive execution DB (Need to be downloaded)
│
├── scripts/               # Utility & Operational Scripts
    ├── evaluate_mimo.py   # Compute CWR, APY and Sharpe ratios from historical predictions
    ├── plot_portfolio.py  # Generate dynamic returns / capital allocation visualizations
    ├── db_cli.py          # Interactive terminal database inspector
```

---

## 🚀 Quickstart

### 1. Installation
Clone the repository and install the required dependencies inside a virtual environment (Python 3.10+ recommended):
```bash
conda create -n polybench python=3.10
conda activate polybench
pip install -r requirements.txt
```

### 2. Configuration (`.env`)
Create an `.env` file in the root directory mirroring the necessary keys for fetching news, loading order books, and pinging AI models. 
```dotenv
PRIMARY_PROVIDER='OPENROUTER'
OPENROUTER_API_KEY='your_api_key_here'
OPENROUTER_MODEL='google/gemini-3-flash-preview'
OPENROUTER_PROVIDER_ORDER='google-vertex'
# OPENROUTER_MODEL='xiaomi/mimo-v2-flash'
# OPENROUTER_PROVIDER_ORDER='xiaomi/fp8'

VERBOSE_MODE=false
DEBUG_MODE=false
```

### 3. Execution
Launch the interactive terminal dashboard:
```bash
python main.py
```

## 📊 Evaluation Metrics
PolyBench enforces a strict Bayesian baseline locked to specific timestamps, discarding any predictions that fall below a $c < 0.6$ confidence limit or fail to identify positive Expected Value (EV+) opportunities.

* **Confidence-Weighted Return (CWR):** Evaluates pure capital allocation efficiency, linearly scaling simulated investments against the active Order Book based on stated model conviction.
* **Annualized Percentage Yield (APY):** Time-normalized CWR discounting the opportunity cost of locked capital.
* **Sharpe Ratio ($\mu_r / \sigma_r$):** Pure risk-adjusted portfolio performance penalizing erratic draw-downs.

*For rigorous execution instructions, please see the [WALKTHROUGH.md](./WALKTHROUGH.md).*
