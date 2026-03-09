
import sqlite3
import os
import sys
import json
import logging
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

# Automatically use the configured DB path from release config
DB_NAME = config.DB_PATH

def set_db_path(new_path):
    """Dynamically switch the active database context for sqlite models."""
    global DB_NAME
    config.DB_PATH = new_path
    DB_NAME = new_path

def get_db_connection():
    try:
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as e:
        logger.error(f"Error connecting to database: {e}")
        return None

def init_db():
    conn = get_db_connection()
    if not conn:
        return
        
    c = conn.cursor()
    
    # 1. Events Table
    c.execute("""
    CREATE TABLE IF NOT EXISTS events (
        id TEXT PRIMARY KEY,
        title TEXT,
        description TEXT,
        slug TEXT,
        start_date TEXT,
        end_date TEXT,
        total_volume REAL,
        active BOOLEAN DEFAULT 1,
        tags TEXT, -- JSON Array of categories
        last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    # 2. Markets Table
    c.execute("""
    CREATE TABLE IF NOT EXISTS markets (
        id TEXT PRIMARY KEY,
        event_id TEXT,
        question TEXT,
        description TEXT,
        outcomes TEXT, -- JSON Array
        outcome_prices TEXT, -- JSON Array (last seen)
        clob_token_ids TEXT, -- JSON Array
        volume REAL,
        liquidity REAL,
        end_date TEXT,
        active BOOLEAN DEFAULT 1,
        last_updated DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (event_id) REFERENCES events (id)
    )
    """)
    
    # 3. Predictions Table (AI Decisions)
    c.execute("""
    CREATE TABLE IF NOT EXISTS predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        market_id TEXT,
        event_id TEXT,
        model_name TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        decision TEXT, -- BUY / SELL / HOLD / SKIP
        side TEXT, -- YES / NO
        confidence REAL,
        reasoning TEXT,
        raw_response TEXT, -- Full JSON dump of AI output
        order_book_snapshot TEXT, -- JSON dump of order book at time of analysis
        news_context TEXT, -- JSON dump of news articles fetched
        prompt_used TEXT, -- The exact prompt sent to the AI
        FOREIGN KEY (market_id) REFERENCES markets (id)
    )
    """)
    
    # 4. Trades Table (Virtual P&L Tracking)
    c.execute("""
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        prediction_id INTEGER,
        market_id TEXT,
        status TEXT DEFAULT 'OPEN', -- OPEN, CLOSED, EXPIRED
        side TEXT, -- YES / NO
        entry_price REAL,
        exit_price REAL,
        pnl REAL,
        entry_date DATETIME DEFAULT CURRENT_TIMESTAMP,
        exit_date DATETIME,
        notes TEXT,
        FOREIGN KEY (prediction_id) REFERENCES predictions (id)
    )
    """)
    
    # 5. Market Snapshots - Pre-fetched data for AI analysis
    c.execute("""
    CREATE TABLE IF NOT EXISTS market_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        market_id TEXT,
        event_id TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        news_context TEXT, -- Pre-fetched news articles
        order_book_snapshot TEXT, -- Order book at snapshot time
        market_prices TEXT, -- Current outcome prices
        ready_for_analysis INTEGER DEFAULT 1, -- 1=ready, 0=in progress
        analyzed INTEGER DEFAULT 0, -- 0=pending, 1=done
        FOREIGN KEY (market_id) REFERENCES markets (id)
    )
    """)
    
    # 6. Resolutions Table
    c.execute("""
    CREATE TABLE IF NOT EXISTS resolutions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        market_id TEXT UNIQUE,
        winning_outcome TEXT,
        resolution_source TEXT,
        resolved_at DATETIME,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (market_id) REFERENCES markets (id)
    )
    """)
    
    conn.commit()
    conn.close()
    logger.info(f"Database initialized at {DB_NAME}")
    print(f"Database initialized at {DB_NAME}")

def save_event(event_data):
    """Upsert event into DB."""
    conn = get_db_connection()
    c = conn.cursor()
    
    try:
        tags_json = json.dumps(event_data.get('tags', []))
        
        c.execute("""
        INSERT INTO events (id, title, description, slug, start_date, end_date, total_volume, active, tags, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET
            title=excluded.title,
            description=excluded.description,
            total_volume=excluded.total_volume,
            tags=excluded.tags,
            last_updated=CURRENT_TIMESTAMP
        """, (
            event_data['id'],
            event_data['title'],
            event_data.get('description', ''),
            event_data.get('slug', ''),
            event_data.get('start_date'),
            event_data.get('end_date'),
            event_data.get('total_volume', 0),
            1, # active
            tags_json
        ))
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to save event {event_data.get('id')}: {e}")
    finally:
        conn.close()

def save_market(market_data, event_id):
    """Upsert market into DB."""
    conn = get_db_connection()
    c = conn.cursor()
    
    try:
        # Convert list fields to JSON strings
        outcomes_json = json.dumps(market_data.get('outcomes', []))
        prices_json = json.dumps(market_data.get('prices', []))
        tokens_json = json.dumps(market_data.get('clobTokenIds', []))
        
        c.execute("""
        INSERT INTO markets (id, event_id, question, description, outcomes, outcome_prices, clob_token_ids, volume, liquidity, end_date, active, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET
            outcome_prices=excluded.outcome_prices,
            volume=excluded.volume,
            liquidity=excluded.liquidity,
            last_updated=CURRENT_TIMESTAMP
        """, (
            market_data['id'],
            event_id,
            market_data['question'],
            market_data.get('description', ''),
            outcomes_json,
            prices_json,
            tokens_json,
            market_data.get('volume', 0),
            market_data.get('liquidity', 0),
            market_data.get('end_date'),
            1 # active
        ))
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to save market {market_data.get('id')}: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
