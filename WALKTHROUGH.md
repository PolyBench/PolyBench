# PolyBench CLI Walkthrough

This guide provides a step-by-step tutorial on how to use the PolyBench Standalone CLI to continuously collect live Polymarket events, prefetch multimodal states, generate LLM predictions, and evaluate their financial viability.

---

## 🚀 Starting the Application

Launch the interactive dashboard from the root of the repository:

```bash
python main.py
```

You will be greeted by the Main Menu:

```text
==================================================
  Polymarket AI Analysis - Standalone CLI
==================================================
Active DB: [...]\release\database\polymarket_analysis.db
1. Select / Reselect Database
2. Sequence Mode (Fetch -> Analyze -> Trade)
3. Unified Batch Mode (Automated Pipeline)
4. Evaluate Metrics & Export Plots
5. Database Inspection CLI
6. Archive ERROR Predictions
0. Exit
```

> **Note:** When starting `Sequence Mode` or the `Auto-Pipeline`, you will be prompted to enter a custom limit for the maximum number of trending events to fetch (e.g., `20` or `30`). This allows you to dynamically control the batch size of your analysis runs.

---

## 📂 Step 1: Database Selection (Option 1)

By default, the application connects to `polymarket_analysis.db` (the production database). We highly recommend using `test.db` when prototyping or running evaluations without committing live capital.

Select **Option 1**, which will recursively list all available SQLite `.db` binaries in your `database/` directory. Input the corresponding number to swap your active connection.

---

## 🔍 Step 2: Sequence Mode (Option 2)

**Sequence Mode** is a manual, step-by-step interactive loop primarily used for direct observation and targeted discretionary trading on live Polymarket events.

Selecting **Option 2** will:
1. Fetch the absolute most heavily traded/trending events directly from Polymarket in real-time.
2. Interactively display the **Resolution Rules**, the live Order Book `[Bid/Ask]` spreads, and the fetched Google News context for each event sequentially.
3. Automatically stream the context into your configured LLM and display exactly what the model "thinks" about the market, including its stated confidence and text reasoning.
4. Finally, prompt you: `> Do you want to trade on this event? (y/n/skip)` 

If you select `y`, you can execute an actual transaction on the Polygon blockchain directly out of the CLI based on the AI's advice. Once completed, the result is stored, and it loops to the next trending event. 

_This mode is excellent for verifying how your Prompt performs on live data before deploying Batch pipelines._

---

## ⚙️ Step 3: Unified Batch Mode (Option 3)

The core engine of PolyBench is the **Unified Batch Mode**. Select **Option 3** to open the Batch Pipeline Menu:

```text
--- Unified Batch Mode ---
1. Auto-pipeline (Event -> Market Order Books & News -> AI prediction)
2. Resolve check (Iterate through each market to check whether resolved)
3. Integrity check (Calculate missing/error items needed to be updated)
4. Integrity recover (Recover Market OBs, News, and AI prediction integrity)
5. Error recover (Clear all ERROR predictions and re-predict)
6. Return to Main Menu
```

### 🔹 Auto-Pipeline (Option 1)
The Auto-Pipeline manages the end-to-end forecasting loop entirely autonomously:
1. **Event Ingestion**: Contacts the Polymarket Gamma API to pull active markets exceeding your specified volume threshold (`limit_events`).
2. **Data Prefetching**: Concurrently queries Google News for identical real-world context and captures the 5-level deep active Central Limit Order Book (CLOB). Returns a locked "Snapshot".
3. **AI Inference**: Dispatches the identical multimodal snapshot to the provider defined in your `.env` (e.g., `OPENROUTER_MODEL='google/gemini-3-flash-preview'`).

### 🔹 Resolve Check (Option 2)
Polymarket events close asynchronously. Run this option periodically to query the blockchain block state and identify exactly which prediction markets within your database have officially resolved. 

### 🔹 Integrity Check & Recovery (Options 3 & 4)
Because fetching real-world data and large-scale AI generation can fail due to network timeouts or rate limits, **Integrity Check (Option 3)** performs a dry-run calculation identifying precisely what is broken.

There are seven monitored faults:
* **Fault A**: Empty live context snapshots
* **Fault B**: Snapshot failed to acquire Order Book data
* **Fault C**: Snapshot failed to acquire Google News data
* **Fault D**: An active market successfully fetched a snapshot but is *missing* an AI prediction from the currently selected `.env` Model.
* **Fault E**: An active market has a snapshot but specifically holds an *`ERROR`* prediction.
* **Fault F**: A closed/resolved market is *missing* a retroactive AI backtest.
* **Fault G**: A closed/resolved market has an *`ERROR`* prediction in its backtest.

Selecting **Integrity Recover (Option 4)** will actively heal the pipeline, patching missing news, rebuilding broken order-books, and looping the current AI model over any missing predictions sequentially.

### 🔹 Error Recovery (Option 5)
If AI predictions fail (e.g., due to rate limits or context window size issues), they are logged as `ERROR`. Selecting **Error recover (Option 5)** will specifically target these broken items, delete the faulty `ERROR` row to prevent unique constraint conflicts, and immediately re-run the AI analysis for those specific markets.

---

## 📈 Step 4: Performance Evaluation (Option 4)

Once markets have resolved, you can gauge how your LLMs actually performed using the exact calculations outlined in the PolyBench paper.

Selecting **Option 4** from the Main Menu launches the evaluation engine:
1. It queries **all** executed trades where the agent expressed confidence $> 0.60$.
2. It simulates acquiring shares of the predicted asset dynamically against the pre-fetched historical CLOB.
3. It settles the trade against the deterministic outcome retrieved during the *Resolve Check*.

The console will print out the aggregate:
* **Accuracy:** The raw percentage of correct directional guesses.
* **Non-CWR:** Flat-weight return on investment.
* **Confidence-Weighted Return (CWR):** Net profit heavily skewing capital allocation linearly by the AI's stated conviction.
* **APY:** Annualized returns based on the duration the capital was theoretically locked.
* **Sharpe Ratio:** Risk-adjusted volatility.

*Note: For granular exploration of internal portfolio degradation caused by order-book volume slippage (e.g., $L = \$1,000$ base sizing), run the auxiliary GUI visualizer directly via `python scripts/plot_portfolio.py`.*

---

## 🔍 Database Inspection (Option 5)

If you need to manually review the raw JSON outputs returned by an LLM, verify the exact wording of a scraped news article, or manually amend an incorrect market resolution:

Select **Option 5** to open the embedded `db_cli.py` tool.
* You can search for specific events via substrings.
* Edit rows safely.
* View foreign-key linked Predictions directly.

---

## 🔑 API Key Management

**Startup Validation**
When PolyBench launches, it automatically checks the `OPENROUTER_API_KEY` defined in your `.env` against the official `openrouter.ai/api/v1/auth/key` endpoint. If the key is invalid or unauthorized, the application safely halts startup to prevent wasteful network requests.

---

## 🗑️ Error Archiving (Option 6)

**Option 6: Archive ERROR Predictions**
Should you encounter persistent AI failures mapped to specific constraints (like context limits or model timeouts), selecting this option will actively purge all broken `ERROR` entries from your active database. Before deletion, it compiles a detailed summary of the exact failure reasons (grouped by Model) and archives them safely to `model_errors_log.md` in your root repository for later audit.

