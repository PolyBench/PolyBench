#!/usr/bin/env python3
"""
Unified Batch Pipeline — A standalone module for batch operations.

Provides 3 specific options:
1. Auto-Pipeline: Fetch trending events -> Grab order books & news -> Run AI Analysis.
2. Resolve Check: Iterate through active markets in DB and check Polymarket for resolutions.
3. Integrity Recover: Scan and heal broken snapshots or missing predictions.
"""

import sys
import os
import time
import json
import logging
import asyncio
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

from database.models import get_db_connection, save_event, save_market
from database.peewee_models import db, Event, Market, Resolution, Prediction, MarketSnapshot, connect, close
from peewee import fn
from core.market_data import fetch_trending_markets, fetch_order_book
from core.news_fetcher import async_fetch_news_for_query
from core.analysis import analyze_market_prediction, analyze_backtest_prediction
from google_news_api.client import AsyncGoogleNewsClient
import config

logger = logging.getLogger(__name__)

# Force stdout flush
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(line_buffering=True)

# ---------------------------------------------------------
# OPTION 2: RESOLVE CHECK
# ---------------------------------------------------------

def check_event_for_resolutions(event_id, active_market_ids):
    """Worker function to check a single event for closed markets."""
    try:
        url = f"https://gamma-api.polymarket.com/events/{event_id}"
        response = requests.get(url, timeout=10)
        
        if response.status_code == 404:
            return event_id, 0, False, "Not found"
            
        response.raise_for_status()
        event_data = response.json()
        
        if not event_data or 'markets' not in event_data:
            return event_id, 0, False, "No markets"
        
        winners_found = []
        for m_data in event_data['markets']:
            m_id = str(m_data.get('id'))
            
            if m_id not in active_market_ids:
                continue
                
            closed = m_data.get('closed', False)
            outcome_prices = m_data.get('outcomePrices')
            
            if closed and outcome_prices:
                try:
                    if isinstance(outcome_prices, str):
                        prices_list = json.loads(outcome_prices)
                    else:
                        prices_list = outcome_prices
                        
                    if not prices_list:
                        continue
                        
                    max_idx = prices_list.index(max(prices_list, key=lambda x: float(x)))
                    
                    outcomes = m_data.get('outcomes', [])
                    if isinstance(outcomes, str):
                        outcomes = json.loads(outcomes)
                        
                    winning_outcome = outcomes[max_idx] if max_idx < len(outcomes) else f"Index {max_idx}"
                    resolution_source = m_data.get('resolutionSource', '')
                    resolved_at = m_data.get('closedTime', None)
                    
                    winners_found.append({
                        "m_id": m_id,
                        "winning_outcome": winning_outcome,
                        "resolution_source": resolution_source,
                        "resolved_at": resolved_at
                    })
                except (ValueError, TypeError, json.JSONDecodeError):
                    pass
                    
        return event_id, len(winners_found), True, winners_found
    except Exception as e:
        return event_id, 0, False, str(e)


def run_resolve_check(limit=None, max_workers=10):
    """Option 2: Iterate through active markets and mark them resolved if closed."""
    print("Starting Resolve Check (Syncing DB with Polymarket)...")
    connect()
    db.create_tables([Market, Resolution], safe=True)
    
    query = Market.select(Market.id, Market.event_id).where(Market.active == True)
    if limit:
        query = query.limit(limit)
        
    active_markets = list(query)
    if not active_markets:
        print("No active markets found in DB.")
        close()
        return

    active_market_ids = {m.id for m in active_markets}
    event_ids = list({m.event_id for m in active_markets if m.event_id})
    
    total_events = len(event_ids)
    print(f"Checking {len(active_markets)} active markets across {total_events} events...")

    total_resolved = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(check_event_for_resolutions, eid, active_market_ids): eid for eid in event_ids}
        
        for i, future in enumerate(as_completed(futures), 1):
            eid, num_resolved, success, result = future.result()
            
            if success and num_resolved > 0:
                print(f"  [{i}/{total_events}] Event {eid} -> {num_resolved} resolved markets found.")
                for w in result:
                    res_obj, created = Resolution.get_or_create(
                        market_id=w['m_id'],
                        defaults={
                            'winning_outcome': w['winning_outcome'],
                            'resolution_source': w['resolution_source'],
                            'resolved_at': w['resolved_at']
                        }
                    )
                    if created:
                        total_resolved += 1
                        m = Market.get(Market.id == w['m_id'])
                        m.active = False
                        m.save()
            elif not success:
                print(f"  [{i}/{total_events}] Event {eid} -> Error: {result}")
                
    close()
    print(f"\nResolve Check Complete: Marker {total_resolved} new markets as resolved.")


# ---------------------------------------------------------
# OPTION 1: AUTO-PIPELINE (Fetch -> Data -> Analyze)
# ---------------------------------------------------------

def get_unprocessed_markets(limit=50):
    """Finds active markets without predictions from the CURRENT model config in the DB."""
    preferred_provider = config.PRIMARY_PROVIDER
    if preferred_provider == "OPENROUTER":
        search_term = f"OPENROUTER ({config.OPENROUTER_MODEL} via {config.OPENROUTER_PROVIDER_ORDER})"
    else:
        search_term = str(preferred_provider)
        
    conn = get_db_connection()
    c = conn.cursor()
    # Find active markets missing predictions
    # We differentiate: To run pre-fetch, we want markets that either don't have a snapshot row, 
    # OR their snapshot is missing OB/News.
    query = """
        SELECT m.*, e.title as event_title 
        FROM markets m
        JOIN events e ON m.event_id = e.id
        LEFT JOIN market_snapshots s ON m.id = s.market_id
        WHERE m.active = 1 
        AND m.id NOT IN (SELECT market_id FROM predictions WHERE model_name = ? AND decision != 'ERROR')
        AND (s.id IS NULL OR LENGTH(IFNULL(s.news_context, '')) <= 10 OR LENGTH(IFNULL(s.order_book_snapshot, '')) <= 10)
        ORDER BY m.volume DESC
        LIMIT ?
    """
    rows = c.execute(query, (search_term, limit)).fetchall()
    conn.close()
    return [dict(row) for row in rows]

async def prefetch_single_market_async(m, client, semaphore, conn):
    async with semaphore:
        market_id = m['id']
        event_id = m['event_id']
        question = m['question']
        event_title = m.get('event_title', '')
        
        print(f"  > Fetching Data: {question[:50]}...")

        # 1. OB
        live_obs = {}
        try:
            clob_ids = json.loads(m['clob_token_ids']) if m['clob_token_ids'] else []
            outcomes = json.loads(m['outcomes']) if m['outcomes'] else []
            for idx, token_id in enumerate(clob_ids[:2]):
                if token_id:
                    ob = await asyncio.to_thread(fetch_order_book, str(token_id))
                    if ob:
                        label = outcomes[idx] if idx < len(outcomes) else str(idx)
                        live_obs[label] = ob
        except Exception as e:
            logger.warning(f"OB fetch failed for {market_id}: {e}")
        ob_snapshot = json.dumps(live_obs)

        # 2. News
        search_query = event_title or question
        try:
            news_context = await async_fetch_news_for_query(search_query, limit=5, top_k=3, client=client)
        except Exception as e:
            logger.error(f"News fetch failed for {market_id}: {e}")
            return {"market_id": market_id, "success": False, "error": str(e)}

        # 3. Prices
        prices = m.get('outcome_prices', '[]')
        
        # 4. Save
        try:
            c = conn.cursor()
            c.execute("""
            INSERT INTO market_snapshots (market_id, event_id, news_context, order_book_snapshot, market_prices, ready_for_analysis, analyzed)
            VALUES (?, ?, ?, ?, ?, 1, 0)
            """, (market_id, event_id, news_context, ob_snapshot, prices))
            conn.commit()
            return {"market_id": market_id, "success": True}
        except Exception as e:
            logger.error(f"DB insert failed for {market_id}: {e}")
            return {"market_id": market_id, "success": False, "error": str(e)}

async def run_data_prefetch_async(markets, max_workers=5):
    """Wrapper to fetch snapshots concurrently."""
    conn = get_db_connection()
    semaphore = asyncio.Semaphore(max_workers)
    prefetched = 0
    
    async with AsyncGoogleNewsClient(requests_per_minute=1000) as client:
        tasks = [prefetch_single_market_async(m, client, semaphore, conn) for m in markets]
        for coro in asyncio.as_completed(tasks):
            res = await coro
            if res['success']: prefetched += 1
            
    conn.close()
    print(f"Data Prefetch Complete: {prefetched} snapshots generated.")

def analyze_single_market(m):
    """Analyzes a single market using existing snapshot."""
    market_id = m['id']
    print(f"  > AI Analysis: {m['question'][:40]}...")
    
    conn = get_db_connection()
    c = conn.cursor()
    
    # 1. Load snapshot
    row = c.execute("SELECT news_context, order_book_snapshot, market_prices FROM market_snapshots WHERE market_id = ?", (market_id,)).fetchone()
    if not row:
        conn.close()
        return {"market": m, "success": False, "error": "No snapshot found"}
        
    news_ctx, ob_snap, prices_str = row
    
    try:
        live_obs = json.loads(ob_snap) if ob_snap else {}
        outcomes_list = json.loads(m['outcomes']) if m['outcomes'] else []
        prices_list = json.loads(prices_str) if prices_str else []
    except:
        outcomes_list, prices_list, live_obs = [], [], {}

    event_struct = {
        "title": m['event_title'],
        "description": m['description'],
        "markets": [{
            "id": market_id,
            "question": m['question'],
            "volume": m['volume'],
            "outcomes": outcomes_list,
            "prices": prices_list,
            "order_books": live_obs,
            "news_context": news_ctx
        }]
    }

    try:
        result = analyze_market_prediction(event_struct)
        conn.close()
        return {
            "market": m,
            "result": result,
            "snapshot": ob_snap,
            "news_context": news_ctx,
            "prompt_used": result.get('prompt_used', ''),
            "success": True
        }
    except Exception as e:
        conn.close()
        return {"market": m, "success": False, "error": str(e)}


def run_batch_analysis(limit=5, max_workers=3, target_markets=None):
    """Run Batch AI Analysis on given markets."""
    if not target_markets:
        # If no explicit list passed, auto-find active markets that HAVE complete snapshots 
        # but are MISSING predictions.
        preferred_provider = config.PRIMARY_PROVIDER
        if preferred_provider == "OPENROUTER":
            search_term = f"OPENROUTER ({config.OPENROUTER_MODEL} via {config.OPENROUTER_PROVIDER_ORDER})"
        else:
            search_term = str(preferred_provider)
            
        conn = get_db_connection()
        c = conn.cursor()
        query = """
            SELECT m.*, e.title as event_title 
            FROM markets m
            JOIN events e ON m.event_id = e.id
            JOIN market_snapshots s ON m.id = s.market_id
            WHERE m.active = 1 
            AND m.id NOT IN (SELECT market_id FROM predictions WHERE model_name = ? AND decision != 'ERROR')
            AND LENGTH(s.news_context) > 10 AND LENGTH(s.order_book_snapshot) > 10
            ORDER BY m.volume DESC
            LIMIT ?
        """
        rows = c.execute(query, (search_term, limit)).fetchall()
        conn.close()
        markets = [dict(row) for row in rows]
    else:
        markets = target_markets
        
    if not markets:
        print("No valid ready markets found for Analysis.")
        return

    print(f"Starting Batch Analysis Pool ({len(markets)} targets)...")
    conn = get_db_connection()
    c = conn.cursor()
    processed_count = 0
    config.VERBOSE_MODE = False
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_market = {executor.submit(analyze_single_market, m): m for m in markets}
        for future in as_completed(future_to_market):
            data = future.result()
            if not data['success']:
                print(f"  x Failed: {data['market']['question'][:30]}... ({data['error']})")
                continue
                
            m = data['market']
            result = data['result']
            snapshot = data['snapshot']
            news_context = data['news_context']
            prompt_used = data['prompt_used']
            
            decisions = result.get('decisions', [])
            model_name = result.get('provider', 'Unknown')
            raw_resp = json.dumps(result)
            
            if not decisions:
                final_decision = "ERROR" if result.get('error') or result.get('decision') == 'ERROR' else "SKIP"
                c.execute("""
                INSERT INTO predictions (market_id, event_id, model_name, decision, side, confidence, reasoning, raw_response, order_book_snapshot, news_context, prompt_used)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (m['id'], m['event_id'], model_name, final_decision, None, 0.0, result.get('reasoning', 'No trade suggested'), raw_resp, snapshot, news_context, prompt_used))
            else:
                for d in decisions:
                    c.execute("""
                    INSERT INTO predictions (market_id, event_id, model_name, decision, side, confidence, reasoning, raw_response, order_book_snapshot, news_context, prompt_used)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (m['id'], m['event_id'], model_name, 'TRADE', d.get('outcome', ''), d.get('confidence', 0), d.get('reasoning'), raw_resp, snapshot, news_context, prompt_used))
            
            # Mark snapshot analyzed
            c.execute("UPDATE market_snapshots SET analyzed = 1 WHERE market_id = ?", (m['id'],))
            conn.commit()
            processed_count += 1
            
            if not decisions and (result.get('error') or result.get('decision') == 'ERROR'):
                print(f"  x ERROR: {m['question'][:40]}... ({result.get('error', 'Analysis failed')})")
                if "401" in str(result.get('reasoning', '')) or "Unauthorized" in str(result.get('reasoning', '')):
                    print("  [!] Severe API Authentication Error. Cancelling remaining tasks...")
                    try:
                        executor.shutdown(wait=False, cancel_futures=True)
                    except TypeError:
                        executor.shutdown(wait=False)
                    break
            else:
                print(f"  ✓ Validated: {m['question'][:40]}... (Decisions: {len(decisions)})")

    conn.close()
    print(f"Batch Analysis Complete: {processed_count} markets processed.")

def run_auto_pipeline(limit=30, max_workers=5):
    """Option 1: End-to-End Pipeline."""
    print(f"\n[1] Fetching fresh active events from Polymarket (Limit {limit})...")
    events = fetch_trending_markets(limit=limit, min_volume=0, fetch_obs=False)
    
    conn = get_db_connection()
    c = conn.cursor()
    for ev in events:
        save_event(ev)
        for m in ev['markets']:
            save_market(m, ev['id'])
    conn.close()
    
    print("\n[2] Identifying unprocessed markets...")
    markets = get_unprocessed_markets(limit=limit)
    if not markets:
        print("No new markets require processing.")
        return
        
    print("\n[3] Pre-fetching Live Data (Order Books & News Snapshots)...")
    asyncio.run(run_data_prefetch_async(markets, max_workers=max_workers))
    
    print("\n[4] Running AI Inference on Snapshots...")
    run_batch_analysis(limit=limit, max_workers=3)
    
    print("\n✅ Auto-Pipeline Complete.")

# ---------------------------------------------------------
# OPTION 3: INTEGRITY RECOVER
# ---------------------------------------------------------

async def recover_snapshot_data(snapshot_rows, client, semaphore, conn):
    """Worker to recover missing OB or News for an existing snapshot."""
    async with semaphore:
        snap_id, m_id, q, e_title, m_prices, clobs, outcomes, has_news, has_ob, snap_time = snapshot_rows
        
        print(f"  > Recovering ID:{snap_id}: {q[:50]}...")
        c = conn.cursor()
        
        updates = {}
        
        # Recover OB
        if not has_ob:
            live_obs = {}
            try:
                c_ids = json.loads(clobs) if clobs else []
                o_list = json.loads(outcomes) if outcomes else []
                for idx, token_id in enumerate(c_ids[:2]):
                    if token_id:
                        ob = await asyncio.to_thread(fetch_order_book, str(token_id))
                        if ob:
                            label = o_list[idx] if idx < len(o_list) else str(idx)
                            live_obs[label] = ob
                updates['order_book_snapshot'] = json.dumps(live_obs)
            except Exception as e:
                logger.warning(f"OB recovery failed: {e}")
                
        # Recover News using original timestamp as cutoff
        if not has_news:
            search_query = e_title or q
            before_date = snap_time.split(' ')[0] if snap_time else None # YYYY-MM-DD
            try:
                news_ctx = await async_fetch_news_for_query(search_query, limit=5, top_k=3, client=client, before_date=before_date)
                updates['news_context'] = news_ctx
            except Exception as e:
                logger.warning(f"News recovery failed: {e}")

        if updates:
            # Update snapshot
            set_clause = ", ".join([f"{k} = ?" for k in updates.keys()])
            set_clause += ", timestamp = CURRENT_TIMESTAMP" # Refresh time since OB is new
            values = list(updates.values()) + [snap_id]
            c.execute(f"UPDATE market_snapshots SET {set_clause} WHERE id = ?", values)
            conn.commit()
            return True
        return False

async def run_snapshot_recovery_async(rows, max_workers=5):
    conn = get_db_connection()
    semaphore = asyncio.Semaphore(max_workers)
    recovered = 0
    
    async with AsyncGoogleNewsClient(requests_per_minute=1000) as client:
        tasks = [recover_snapshot_data(r, client, semaphore, conn) for r in rows]
        for coro in asyncio.as_completed(tasks):
            if await coro: recovered += 1
            
    conn.close()
    print(f"Integrity Recover: Restored {recovered} snapshots.")

def get_resolved_unpredicted_markets(limit=10):
    """Finds closed markets that haven't been analyzed by the retroactive AI backtest yet."""
    connect()
    
    preferred_provider = config.PRIMARY_PROVIDER
    if preferred_provider == "OPENROUTER":
        search_term = f"OPENROUTER ({config.OPENROUTER_MODEL} via {config.OPENROUTER_PROVIDER_ORDER})"
    elif preferred_provider == "GOOGLE":
        search_term = f"Google ({config.GOOGLE_MODEL})"
    elif preferred_provider == "XIAOMI":
        search_term = "Xiaomi MiMo"
    else:
        search_term = str(preferred_provider)
        
    # We want markets that have NO prediction at all OR their prediction is 'ERROR'
    valid_predictions = Prediction.select(Prediction.market_id).where(
        (Prediction.model_name == search_term) & 
        (Prediction.decision != 'ERROR')
    )
    markets_with_ob = MarketSnapshot.select(MarketSnapshot.market_id).where(fn.LENGTH(MarketSnapshot.order_book_snapshot) > 10).distinct()
    
    resolutions = (Resolution
        .select(Resolution, Market)
        .join(Market)
        .where(
            (Resolution.market_id.not_in(valid_predictions)) &
            (Resolution.market_id.in_(markets_with_ob))
        )
        .order_by(Resolution.resolved_at.desc())
        .limit(limit))
        
    results = []
    
    # Check if these are Missing or Error
    error_mkt_ids = set()
    errors_query = Prediction.select(Prediction.market_id).where(
        (Prediction.model_name == search_term) & 
        (Prediction.decision == 'ERROR')
    )
    for p in errors_query:
        error_mkt_ids.add(p.market_id)

    for res in resolutions:
        status = "Error" if res.market_id in error_mkt_ids else "Missing"
        results.append({'market': res.market, 'resolution': res, 'status': status})
        
    return results

def analyze_closed_market(item):
    """Worker function to process a single retroactive closed market for Fault E."""
    m = item['market']
    res = item['resolution']
    market_id = m.id
    
    snapshot_records = (MarketSnapshot
        .select()
        .where(MarketSnapshot.market_id == market_id)
        .order_by(MarketSnapshot.timestamp.desc())
        .limit(1)
        .execute())
        
    historical_obs = {}
    ob_snap_json = "{}"
    
    if snapshot_records:
        snap = list(snapshot_records)[0]
        if snap.order_book_snapshot:
            try:
                historical_obs = json.loads(snap.order_book_snapshot)
                ob_snap_json = snap.order_book_snapshot
            except:
                pass
                
    if not historical_obs:
        return {"market_id": market_id, "error": "No historical order book snapshot found to analyze.", "success": False}
        
    event_title = m.event.title if m.event else "Unknown Event"
    event_desc = m.event.description if m.event else ""
    
    event_struct = {
        "title": event_title,
        "description": event_desc,
        "timestamp": str(snap.timestamp) if snap else "Unknown",
        "news_context": snap.news_context if snap and snap.news_context else "(No historical news context found in snapshot)",
        "markets": [{
            "id": market_id,
            "question": m.question,
            "volume": m.volume,
            "outcomes": m.outcomes_list,
            "prices": m.prices_list,
            "order_books": historical_obs 
        }]
    }

    try:
        result = analyze_backtest_prediction(event_struct)
        return {
            "market": m,
            "resolution": res,
            "result": result,
            "snapshot": ob_snap_json,
            "success": True
        }
    except Exception as e:
        logger.error(f"Analysis failed for {market_id}: {e}")
        return {"market": m, "error": str(e), "success": False}

def run_integrity_check_only():
    """Option: Calculate and list broken/missing items across DB categories without updating."""
    print("\n--- Integrity Calculation Check (Dry Run) ---")
    conn = get_db_connection()
    c = conn.cursor()
    
    # Fault A/B/C: Snapshots missing OB or News
    query = """
        SELECT COUNT(s.id)
        FROM market_snapshots s
        JOIN markets m ON s.market_id = m.id
        WHERE m.active = 1 AND (LENGTH(s.news_context) <= 10 OR LENGTH(s.order_book_snapshot) <= 10)
    """
    broken_snaps = c.execute(query).fetchone()[0]
    
    # Configured Model Search Term
    preferred_provider = config.PRIMARY_PROVIDER
    if preferred_provider == "OPENROUTER":
        search_term = f"OPENROUTER ({config.OPENROUTER_MODEL} via {config.OPENROUTER_PROVIDER_ORDER})"
    else:
        search_term = str(preferred_provider)
        
    # Fault D: Active Markets Missing Predictions
    pred_missing_query = """
        SELECT COUNT(m.id)
        FROM markets m
        JOIN market_snapshots s ON m.id = s.market_id
        WHERE m.active = 1 
        AND m.id NOT IN (SELECT market_id FROM predictions WHERE model_name = ?)
        AND LENGTH(s.news_context) > 10 AND LENGTH(s.order_book_snapshot) > 10
    """
    missing_predictions = c.execute(pred_missing_query, (search_term,)).fetchone()[0]

    # Fault E: Active Markets with ERROR Predictions
    pred_error_query = """
        SELECT COUNT(m.id)
        FROM markets m
        JOIN market_snapshots s ON m.id = s.market_id
        WHERE m.active = 1 
        AND m.id IN (SELECT market_id FROM predictions WHERE model_name = ? AND decision = 'ERROR')
        AND LENGTH(s.news_context) > 10 AND LENGTH(s.order_book_snapshot) > 10
    """
    error_predictions = c.execute(pred_error_query, (search_term,)).fetchone()[0]
    conn.close()
    
    # Fault F & G: Closed markets missing/error retroactive backtests
    items = get_resolved_unpredicted_markets(limit=100000)
    missing_backtests = sum(1 for item in items if item['status'] == 'Missing')
    error_backtests = sum(1 for item in items if item['status'] == 'Error')
    
    print(f"• [Fault A/B/C] Broken/Empty Live Snapshots: {broken_snaps} items")
    print(f"• [Fault D] Active Markets Missing Predictions: {missing_predictions} items")
    print(f"• [Fault E] Active Markets Error Predictions: {error_predictions} items")
    print(f"• [Fault F] Closed Markets Missing Retroactive Backtests: {missing_backtests} items")
    print(f"• [Fault G] Closed Markets Error Retroactive Backtests: {error_backtests} items")
    print("\nValidation complete. Launch 'Integrity recover' to actively heal these pipelines.")

def run_integrity_recover():
    """Option 3: Heal broken DB pipeline stages."""
    print("Scanning Database for Integrity Faults...")
    conn = get_db_connection()
    c = conn.cursor()
    
    # Configured Model Search Term
    preferred_provider = config.PRIMARY_PROVIDER
    if preferred_provider == "OPENROUTER":
        search_term = f"OPENROUTER ({config.OPENROUTER_MODEL} via {config.OPENROUTER_PROVIDER_ORDER})"
    else:
        search_term = str(preferred_provider)

    # Fault A/B/C: Snapshots missing OB or News AND also missing AI prediction
    query = """
        SELECT s.id, m.id, m.question, e.title, s.market_prices, m.clob_token_ids, m.outcomes,
               CASE WHEN LENGTH(IFNULL(s.news_context, '')) > 10 THEN 1 ELSE 0 END as has_news,
               CASE WHEN LENGTH(IFNULL(s.order_book_snapshot, '')) > 10 THEN 1 ELSE 0 END as has_ob,
               s.timestamp
        FROM market_snapshots s
        JOIN markets m ON s.market_id = m.id
        JOIN events e ON m.event_id = e.id
        WHERE m.active = 1 
        AND (has_news = 0 OR has_ob = 0)
        AND m.id NOT IN (SELECT market_id FROM predictions WHERE model_name = ?)
    """
    broken_snaps = c.execute(query, (search_term,)).fetchall()
    
    if broken_snaps:
        print(f"Found {len(broken_snaps)} snapshots missing OB or News data. Launching recovery...")
        asyncio.run(run_snapshot_recovery_async(broken_snaps, max_workers=5))
    else:
        print("No broken snapshots found.")
        

    # Fault D & E: Snapshots exist but Prediction analysis missing/error for THIS model
    pred_query = """
        SELECT m.*, e.title as event_title 
        FROM markets m
        JOIN events e ON m.event_id = e.id
        JOIN market_snapshots s ON m.id = s.market_id
        WHERE m.active = 1 
        AND m.id NOT IN (SELECT market_id FROM predictions WHERE model_name = ? AND decision != 'ERROR')
        AND LENGTH(s.news_context) > 10 AND LENGTH(s.order_book_snapshot) > 10
    """
    unpredicted_markets = c.execute(pred_query, (search_term,)).fetchall()
    
    if unpredicted_markets:
        # Before we retry, delete previous ERROR predictions to avoid unique constraint issues
        mkt_ids = [m['id'] for m in unpredicted_markets]
        placeholders = ','.join('?' * len(mkt_ids))
        delete_err_query = f"DELETE FROM predictions WHERE model_name = ? AND decision = 'ERROR' AND market_id IN ({placeholders})"
        c.execute(delete_err_query, [search_term] + mkt_ids)
        conn.commit()
        conn.close()
        
        print(f"Found {len(unpredicted_markets)} missing or error predictions. Launching backfill analysis...")
        run_batch_analysis(limit=len(unpredicted_markets), max_workers=3, target_markets=[dict(m) for m in unpredicted_markets])
    else:
        conn.close()
        print("No missing active predictions found.")
        
    # Fault F & G: Closed markets that have order book snapshots, but missing/error retroactive prediction
    print("\nScanning for Fault F/G: Unpredicted Closed Markets (Retroactive Backtest)...")
    items = get_resolved_unpredicted_markets(limit=50)
    
    if items:
        print(f"Found {len(items)} closed markets to retroactively backtest. Launching pool...")
        processed_count = 0
        with ThreadPoolExecutor(max_workers=3) as executor:
            future_to_market = {executor.submit(analyze_closed_market, item): item for item in items}
            
            for future in as_completed(future_to_market):
                data = future.result()
                
                if not data['success']:
                    print(f"  x Failed: {data.get('error')}")
                    continue
                    
                m = data['market']
                res = data['resolution']
                result = data['result']
                snapshot = data['snapshot']
                
                decisions = result.get('decisions', [])
                model_name = result.get('provider', 'Unknown-Retro')
                news_context = result.get('news_context', '')
                prompt_used = result.get('prompt_used', '')
                
                # If we are retrying a previously failed retroactive backtest, clean it up first
                if item.get('status') == 'Error':
                    Prediction.delete().where(
                        (Prediction.market_id == m.id) & 
                        (Prediction.model_name == model_name) & 
                        (Prediction.decision == 'ERROR')
                    ).execute()
                
                with db.atomic():
                    if not decisions:
                        final_decision = "ERROR" if result.get('error') or result.get('decision') == 'ERROR' else "SKIP"
                        Prediction.create(
                            market=m,
                            event=m.event,
                            model_name=model_name,
                            decision=final_decision,
                            reasoning=result.get('reasoning', 'No trade suggested'),
                            raw_response=json.dumps(result),
                            order_book_snapshot=snapshot,
                            news_context=news_context,
                            prompt_used=prompt_used
                        )
                    else:
                        for d in decisions:
                            Prediction.create(
                                market=m,
                                event=m.event,
                                model_name=model_name,
                                decision="TRADE" if d.get('outcome') else "ERROR", 
                                side=d.get('outcome', ''),
                                confidence=d.get('confidence', 0),
                                reasoning=d.get('reasoning'),
                                raw_response=json.dumps(result),
                                order_book_snapshot=snapshot,
                                news_context=news_context,
                                prompt_used=prompt_used
                            )
                            
                processed_count += 1
                if not decisions and (result.get('error') or result.get('decision') == 'ERROR'):
                    print(f"  x ERROR: {m.question[:40]}... ({result.get('error', 'Analysis failed')})")
                    if "401" in str(result.get('reasoning', '')) or "Unauthorized" in str(result.get('reasoning', '')):
                        print("  [!] Severe API Authentication Error. Cancelling remaining tasks...")
                        try:
                            executor.shutdown(wait=False, cancel_futures=True)
                        except TypeError:
                            executor.shutdown(wait=False)
                        break
                else:
                    choice_str = decisions[0].get('outcome', 'UNKNOWN') if decisions else 'UNKNOWN'
                    print(f"  ✓ Backtested: {m.question[:40]}... (AI: {choice_str} | Real: {res.winning_outcome})")
                
        print(f"Retroactive Analysis Complete: {processed_count} markets assessed.")
    else:
        print("No missing retroactive backtests found.")
        
    print("\n✅ Integrity Check Complete.")


def run_error_recover():
    """Option 5: Heal specifically ERROR predictions across Active and Closed markets."""
    print("Scanning Database for ERROR Predictions...")
    conn = get_db_connection()
    c = conn.cursor()
    
    # Configured Model Search Term
    preferred_provider = config.PRIMARY_PROVIDER
    if preferred_provider == "OPENROUTER":
        search_term = f"OPENROUTER ({config.OPENROUTER_MODEL} via {config.OPENROUTER_PROVIDER_ORDER})"
    else:
        search_term = str(preferred_provider)
        
    # Active Markets with ERROR Predictions
    pred_query = """
        SELECT m.*, e.title as event_title 
        FROM markets m
        JOIN events e ON m.event_id = e.id
        JOIN market_snapshots s ON m.id = s.market_id
        WHERE m.active = 1 
        AND m.id IN (SELECT market_id FROM predictions WHERE model_name = ? AND decision = 'ERROR')
        AND LENGTH(s.news_context) > 10 AND LENGTH(s.order_book_snapshot) > 10
    """
    error_markets = c.execute(pred_query, (search_term,)).fetchall()
    
    if error_markets:
        mkt_ids = [m['id'] for m in error_markets]
        placeholders = ','.join('?' * len(mkt_ids))
        delete_err_query = f"DELETE FROM predictions WHERE model_name = ? AND decision = 'ERROR' AND market_id IN ({placeholders})"
        c.execute(delete_err_query, [search_term] + mkt_ids)
        conn.commit()
        
        print(f"Removed {len(error_markets)} active ERROR predictions. Launching backfill analysis...")
        run_batch_analysis(limit=len(error_markets), max_workers=3, target_markets=[dict(m) for m in error_markets])
    else:
        print("No active ERROR predictions found.")
        
    conn.close()
    
    # Closed markets with ERROR retroactive prediction
    print("\nScanning for Unpredicted Closed Markets with ERRORs (Retroactive Backtest)...")
    items = get_resolved_unpredicted_markets(limit=50)
    error_items = [item for item in items if item.get('status') == 'Error']
    
    if error_items:
        print(f"Found {len(error_items)} closed markets with errors to retroactively backtest. Launching pool...")
        processed_count = 0
        with ThreadPoolExecutor(max_workers=3) as executor:
            future_to_market = {executor.submit(analyze_closed_market, item): item for item in error_items}
            
            for future in as_completed(future_to_market):
                data = future.result()
                
                if not data['success']:
                    print(f"  x Failed: {data.get('error')}")
                    continue
                    
                m = data['market']
                res = data['resolution']
                result = data['result']
                snapshot = data['snapshot']
                
                decisions = result.get('decisions', [])
                model_name = result.get('provider', 'Unknown-Retro')
                news_context = result.get('news_context', '')
                prompt_used = result.get('prompt_used', '')
                
                # Clean up before inserting new
                Prediction.delete().where(
                    (Prediction.market_id == m.id) & 
                    (Prediction.model_name == model_name) & 
                    (Prediction.decision == 'ERROR')
                ).execute()
                
                with db.atomic():
                    if not decisions:
                        final_decision = "ERROR" if result.get('error') or result.get('decision') == 'ERROR' else "SKIP"
                        Prediction.create(
                            market=m,
                            event=m.event,
                            model_name=model_name,
                            decision=final_decision,
                            reasoning=result.get('reasoning', 'No trade suggested'),
                            raw_response=json.dumps(result),
                            order_book_snapshot=snapshot,
                            news_context=news_context,
                            prompt_used=prompt_used
                        )
                    else:
                        for d in decisions:
                            Prediction.create(
                                market=m,
                                event=m.event,
                                model_name=model_name,
                                decision="TRADE" if d.get('outcome') else "ERROR", 
                                side=d.get('outcome', ''),
                                confidence=d.get('confidence', 0),
                                reasoning=d.get('reasoning'),
                                raw_response=json.dumps(result),
                                order_book_snapshot=snapshot,
                                news_context=news_context,
                                prompt_used=prompt_used
                            )
                            
                processed_count += 1
                if not decisions and (result.get('error') or result.get('decision') == 'ERROR'):
                    print(f"  x ERROR: {m.question[:40]}... ({result.get('error', 'Analysis failed')})")
                    if "401" in str(result.get('reasoning', '')) or "Unauthorized" in str(result.get('reasoning', '')):
                        print("  [!] Severe API Authentication Error. Cancelling remaining tasks...")
                        try:
                            executor.shutdown(wait=False, cancel_futures=True)
                        except TypeError:
                            executor.shutdown(wait=False)
                        break
                else:
                    choice_str = decisions[0].get('outcome', 'UNKNOWN') if decisions else 'UNKNOWN'
                    print(f"  ✓ Backtested: {m.question[:40]}... (AI: {choice_str} | Real: {res.winning_outcome})")
                
        print(f"Retroactive Error Recovery Complete: {processed_count} markets assessed.")
    else:
        print("No closed ERROR retroactive backtests found.")
        
    print("\n✅ Error Recovery Complete.")
