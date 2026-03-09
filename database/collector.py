
import logging
import time
import requests
import json
import sys
import os

from database.models import init_db, get_db_connection, save_event, save_market

# Re-use existing fetching logic where possible, but need pagination here
from core.market_data import fetch_order_book

logger = logging.getLogger(__name__)

GAMMA_API_URL = "https://gamma-api.polymarket.com/events"


STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "collector_state.json")

def save_state(offset):
    with open(STATE_FILE, "w") as f:
        json.dump({"offset": offset}, f)

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f).get("offset", 0)
        except:
            return 0
    return 0

def fetch_full_mirror(limit_per_page=100, max_pages=5000):
    """
    Fetches EVERYTHING (Active & Closed) robustly.
    Resumes from last saved offset.
    """
    offset = load_state()
    total_fetched = 0
    
    conn = get_db_connection()
    c = conn.cursor()
    
    print(f"Starting FULL MIRROR collection (Resuming from Offset: {offset})...")
    
    for page in range(max_pages):
        # 1. API Call with Retries
        events = None
        for attempt in range(3):
            try:
                params = {
                    "limit": limit_per_page,
                    "offset": offset,
                    # "active": "true",  <- Removed to get closed events
                    # "closed": "false"  <- Removed
                }
                print(f"  Fetching page {page+1} (Offset: {offset})... Attempt {attempt+1}")
                response = requests.get(GAMMA_API_URL, params=params, timeout=30)
                
                if response.status_code == 429:
                    print("    Rate Limit 429. Sleeping 60s...")
                    time.sleep(60)
                    continue
                    
                response.raise_for_status()
                events = response.json()
                break # Success
            except Exception as e:
                print(f"    Error: {e}. Retrying in {2**attempt}s...")
                time.sleep(2**attempt)
        
        if events is None:
            print("  Failed to fetch page after retries. Saving state and exiting.")
            save_state(offset)
            break
            
        if not events:
            print("  No more events found (End of List).")
            # Reset state for next run? Or keep it? Let's keep it.
            break

        # 2. Process Events
        processed_count = 0
        for event in events:
            try:
                # Ensure data types
                vol = float(event.get('volume', 0) or 0)
                
                # Insert Event
                c.execute("""
                INSERT INTO events (id, title, description, slug, start_date, end_date, total_volume, active, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    description=excluded.description,
                    total_volume=excluded.total_volume,
                    active=excluded.active,
                    last_updated=CURRENT_TIMESTAMP
                """, (
                    str(event.get("id")), 
                    event.get("title"), 
                    event.get("description", ""), 
                    event.get("slug"), 
                    event.get("startDate"), 
                    event.get("endDate"), 
                    vol, 
                    1 if event.get("active") else 0
                ))
                
                # Insert Markets
                markets = event.get('markets', [])
                for m in markets:
                    m_vol = float(m.get('volume', 0) or 0)
                    
                    def ensure_list(val):
                        if isinstance(val, str):
                            try: return json.loads(val)
                            except: return []
                        return val if val else []

                    outcomes = json.dumps(ensure_list(m.get('outcomes', [])))
                    prices = json.dumps(ensure_list(m.get('outcomePrices', [])))
                    clobs = json.dumps(ensure_list(m.get('clobTokenIds', [])))
                    
                    c.execute("""
                    INSERT INTO markets (id, event_id, question, description, outcomes, outcome_prices, clob_token_ids, volume, liquidity, end_date, active, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(id) DO UPDATE SET
                        outcome_prices=excluded.outcome_prices,
                        volume=excluded.volume,
                        active=excluded.active,
                        last_updated=CURRENT_TIMESTAMP
                    """, (
                        str(m.get('id')),
                        str(event.get("id")),
                        m.get('question'),
                        m.get('description', ''),
                        outcomes,
                        prices,
                        clobs,
                        m_vol,
                        float(m.get('liquidity', 0) or 0),
                        m.get('endDate'),
                        1 if m.get("active") else 0
                    ))
                
                processed_count += 1
            except Exception as e:
                logger.error(f"Error processing event {event.get('id')}: {e}")

        conn.commit()
        print(f"  > Saved {processed_count} events.")
        
        offset += limit_per_page
        total_fetched += processed_count
        save_state(offset) # Checkpoint
        
        if len(events) < limit_per_page:
            print("  Reached end of list (Partial page).")
            break
            
        time.sleep(0.5)

    conn.close()
    print(f"Done. Total events processed in this run: {total_fetched}")



def fetch_all_active_tradable_events(limit_per_page=100, max_events=5000):
    """
    Fetches ONLY active/tradable events efficiently.
    Guarantees a full mirror of CURRENTLY active markets.
    Stops after max_events are fetched.
    """
    offset = 0
    total_fetched = 0
    
    conn = get_db_connection()
    c = conn.cursor()
    
    print(f"Starting ACTIVE TRADABLE events collection (max: {max_events})...")
    
    while total_fetched < max_events:
        # 1. API Call with Retries
        events = None
        for attempt in range(3):
            try:
                params = {
                    "limit": limit_per_page,
                    "offset": offset,
                    "active": "true",
                    "closed": "false"
                }
                print(f"  Fetching page (Offset: {offset})... Attempt {attempt+1}")
                response = requests.get(GAMMA_API_URL, params=params, timeout=30)
                
                if response.status_code == 429:
                    print("    Rate Limit 429. Sleeping 60s...")
                    time.sleep(60)
                    continue
                    
                response.raise_for_status()
                events = response.json()
                break # Success
            except Exception as e:
                print(f"    Error: {e}. Retrying in {2**attempt}s...")
                time.sleep(2**attempt)
        
        if events is None:
            print("  Failed to fetch page after retries. Exiting.")
            break
            
        if not events:
            print("  No more events found (End of List).")
            break

        # 2. Process Events
        processed_count = 0
        for event in events:
            try:
                # Ensure data types
                vol = float(event.get('volume', 0) or 0)
                
                # Insert Event
                c.execute("""
                INSERT INTO events (id, title, description, slug, start_date, end_date, total_volume, active, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    description=excluded.description,
                    total_volume=excluded.total_volume,
                    active=1,
                    last_updated=CURRENT_TIMESTAMP
                """, (
                    str(event.get("id")), 
                    event.get("title"), 
                    event.get("description", ""), 
                    event.get("slug"), 
                    event.get("startDate"), 
                    event.get("endDate"), 
                    vol,
                    1
                ))
                
                # Insert Markets
                markets = event.get('markets', [])
                for m in markets:
                    m_vol = float(m.get('volume', 0) or 0)
                    
                    def ensure_list(val):
                        if isinstance(val, str):
                            try: return json.loads(val)
                            except: return []
                        return val if val else []

                    outcomes = json.dumps(ensure_list(m.get('outcomes', [])))
                    prices = json.dumps(ensure_list(m.get('outcomePrices', [])))
                    clobs = json.dumps(ensure_list(m.get('clobTokenIds', [])))
                    
                    c.execute("""
                    INSERT INTO markets (id, event_id, question, description, outcomes, outcome_prices, clob_token_ids, volume, liquidity, end_date, active, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(id) DO UPDATE SET
                        outcome_prices=excluded.outcome_prices,
                        volume=excluded.volume,
                        active=1,
                        last_updated=CURRENT_TIMESTAMP
                    """, (
                        str(m.get('id')),
                        str(event.get("id")),
                        m.get('question'),
                        m.get('description', ''),
                        outcomes,
                        prices,
                        clobs,
                        m_vol,
                        float(m.get('liquidity', 0) or 0),
                        m.get('endDate'),
                        1
                    ))
                
                processed_count += 1
            except Exception as e:
                logger.error(f"Error processing event {event.get('id')}: {e}")

        conn.commit()
        print(f"  > Saved {processed_count} events.")
        
        offset += limit_per_page
        total_fetched += processed_count
        
        if len(events) < limit_per_page:
            print("  Reached end of list (Partial page).")
            break
            
        time.sleep(0.5)

    conn.close()
    print(f"Done. Total ACTIVE events processed: {total_fetched}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
    # fetch_full_mirror() # Old full history
    fetch_all_active_tradable_events() # New Active Only


