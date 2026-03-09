"""
Peewee ORM model classes for the Polymarket Analysis database.

These models mirror the schema defined in database/models.py (raw sqlite3)
and the actual tables in polymarket_analysis.db.

Usage:
    from database.peewee_models import db, Event, Market, Prediction, Trade, MarketSnapshot

    # Query examples:
    for m in Market.select().where(Market.volume > 100000).order_by(Market.volume.desc()).limit(10):
        print(m.question, m.volume)

    # Join example:
    query = (Market
        .select(Market, Event)
        .join(Event)
        .where(Market.active == True)
        .order_by(Market.volume.desc()))
"""

import os
import sys
import json

# Add parent directory to sys.path to easily import config
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from peewee import (
    SqliteDatabase, Model,
    TextField, FloatField, BooleanField, IntegerField,
    DateTimeField, ForeignKeyField, AutoField,
    SQL
)

# ─── Database Connection ───────────────────────────────────────────────
DB_PATH = config.DB_PATH

db = SqliteDatabase(DB_PATH, pragmas={
    'journal_mode': 'wal',       # Better concurrent read performance
    'cache_size': -64 * 1000,    # 64MB cache
    'foreign_keys': 1,           # Enforce FK constraints
})

def set_db_path(new_path):
    """Dynamically switch the active database context for peewee models."""
    global DB_PATH
    config.DB_PATH = new_path
    DB_PATH = new_path
    if not db.is_closed():
        db.close()
    db.init(DB_PATH)


# ─── Base Model ────────────────────────────────────────────────────────
class BaseModel(Model):
    """Base model that binds all models to the same database."""
    class Meta:
        database = db


# ─── Events ────────────────────────────────────────────────────────────
class Event(BaseModel):
    """
    Polymarket events — top-level grouping of markets.
    Example: "2024 US Presidential Election" contains markets like
    "Will Trump win?", "Will Biden win?", etc.

    Row count: ~4,997
    """
    id = TextField(primary_key=True)
    title = TextField(null=True)
    description = TextField(null=True)
    slug = TextField(null=True)
    start_date = TextField(null=True)         # Stored as ISO string
    end_date = TextField(null=True)           # Stored as ISO string
    total_volume = FloatField(null=True)      # Total USD volume across all markets
    active = BooleanField(default=True)       # Whether the event is still active
    tags = TextField(null=True)               # JSON Array of categories
    last_updated = DateTimeField(constraints=[SQL('DEFAULT CURRENT_TIMESTAMP')])

    class Meta:
        table_name = 'events'

    def __str__(self):
        status = "ACTIVE" if self.active else "CLOSED"
        return f"[{status}] {self.title} (Vol: ${self.total_volume or 0:,.0f})"

    @property
    def market_count(self):
        return self.markets.count()


# ─── Markets ───────────────────────────────────────────────────────────
class Market(BaseModel):
    """
    Individual prediction markets within an event.
    Each market has a YES/NO outcome with order book trading.

    Row count: ~38,666
    JSON fields: outcomes, outcome_prices, clob_token_ids (stored as JSON strings)
    """
    id = TextField(primary_key=True)
    event = ForeignKeyField(Event, backref='markets', column_name='event_id', null=True)
    question = TextField(null=True)
    description = TextField(null=True)        # Resolution rules
    outcomes = TextField(null=True)           # JSON array, e.g. '["Yes", "No"]'
    outcome_prices = TextField(null=True)     # JSON array, e.g. '[\"0.65\", \"0.35\"]'
    clob_token_ids = TextField(null=True)     # JSON array of CLOB token IDs
    volume = FloatField(null=True)            # USD trading volume
    liquidity = FloatField(null=True)         # Current liquidity
    end_date = TextField(null=True)           # Stored as ISO string
    active = BooleanField(default=True)
    last_updated = DateTimeField(constraints=[SQL('DEFAULT CURRENT_TIMESTAMP')])

    class Meta:
        table_name = 'markets'

    def __str__(self):
        prices_str = ""
        if self.outcome_prices:
            try:
                prices = json.loads(self.outcome_prices)
                if prices and len(prices) >= 1:
                    yes_pct = float(prices[0]) * 100
                    prices_str = f" @ {yes_pct:.0f}%"
            except (json.JSONDecodeError, ValueError, IndexError):
                pass
        return f"{self.question}{prices_str} (Vol: ${self.volume or 0:,.0f})"

    @property
    def outcomes_list(self):
        """Parse outcomes JSON string into a Python list."""
        if self.outcomes:
            try:
                return json.loads(self.outcomes)
            except json.JSONDecodeError:
                pass
        return []

    @property
    def prices_list(self):
        """Parse outcome_prices JSON string into a list of floats."""
        if self.outcome_prices:
            try:
                return [float(p) for p in json.loads(self.outcome_prices)]
            except (json.JSONDecodeError, ValueError):
                pass
        return []

    @property
    def token_ids(self):
        """Parse clob_token_ids JSON string into a Python list."""
        if self.clob_token_ids:
            try:
                return json.loads(self.clob_token_ids)
            except json.JSONDecodeError:
                pass
        return []


# ─── Predictions ───────────────────────────────────────────────────────
class Prediction(BaseModel):
    """
    AI analysis decisions for markets.
    Each row = one AI decision (BUY/SELL/HOLD/SKIP) for a market.

    Row count: ~0 (populated by batch_analyzer.py)
    """
    id = AutoField(primary_key=True)
    market = ForeignKeyField(Market, backref='predictions', column_name='market_id', null=True)
    event = ForeignKeyField(Event, backref='predictions', column_name='event_id', null=True)
    model_name = TextField(null=True)         # e.g. "gemini-3-flash-preview" or "xiaomi_mimo"
    timestamp = DateTimeField(constraints=[SQL('DEFAULT CURRENT_TIMESTAMP')])
    decision = TextField(null=True)           # BUY / SELL / HOLD / SKIP
    side = TextField(null=True)               # YES / NO
    confidence = FloatField(null=True)        # 0.0 - 1.0
    reasoning = TextField(null=True)          # AI's reasoning text
    raw_response = TextField(null=True)       # Full JSON dump of AI output
    order_book_snapshot = TextField(null=True) # JSON dump of order book at analysis time
    news_context = TextField(null=True)       # JSON dump of news articles fetched
    prompt_used = TextField(null=True)        # The exact prompt sent to AI

    class Meta:
        table_name = 'predictions'

    def __str__(self):
        return f"[{self.decision}] {self.side or ''} conf={self.confidence or 0:.0%} ({self.model_name})"


# ─── Trades ────────────────────────────────────────────────────────────
class Trade(BaseModel):
    """
    Virtual P&L tracking for paper trades.
    Links to a prediction that triggered the trade.

    Row count: ~0
    """
    id = AutoField(primary_key=True)
    prediction = ForeignKeyField(Prediction, backref='trades', column_name='prediction_id', null=True)
    market = ForeignKeyField(Market, backref='trades', column_name='market_id', null=True)
    status = TextField(default='OPEN')        # OPEN / CLOSED / EXPIRED
    side = TextField(null=True)               # YES / NO
    entry_price = FloatField(null=True)
    exit_price = FloatField(null=True)
    pnl = FloatField(null=True)               # Profit/Loss
    entry_date = DateTimeField(constraints=[SQL('DEFAULT CURRENT_TIMESTAMP')])
    exit_date = DateTimeField(null=True)
    notes = TextField(null=True)

    class Meta:
        table_name = 'trades'

    def __str__(self):
        return f"[{self.status}] {self.side} entry={self.entry_price} pnl={self.pnl}"


# ─── Market Snapshots ──────────────────────────────────────────────────
class MarketSnapshot(BaseModel):
    """
    Pre-fetched data for AI analysis.
    Contains news articles and order book snapshots captured at a point in time.
    Populated by batch/prefetch_data.py.

    Row count: ~38,743
    """
    id = AutoField(primary_key=True)
    market = ForeignKeyField(Market, backref='snapshots', column_name='market_id', null=True)
    event = ForeignKeyField(Event, backref='snapshots', column_name='event_id', null=True)
    timestamp = DateTimeField(constraints=[SQL('DEFAULT CURRENT_TIMESTAMP')])
    news_context = TextField(null=True)       # Pre-fetched news articles
    order_book_snapshot = TextField(null=True) # Order book at snapshot time
    market_prices = TextField(null=True)      # Current outcome prices at snapshot time
    ready_for_analysis = IntegerField(default=1)  # 1=ready, 0=in progress
    analyzed = IntegerField(default=0)            # 0=pending, 1=done

    class Meta:
        table_name = 'market_snapshots'

    def __str__(self):
        status = "READY" if self.ready_for_analysis else "PENDING"
        analyzed = "ANALYZED" if self.analyzed else "UNANALYZED"
        return f"[{status}/{analyzed}] market={self.market_id} @ {self.timestamp}"


# ─── Resolutions ───────────────────────────────────────────────────────
class Resolution(BaseModel):
    """
    Final resolution results for closed markets.
    """
    id = AutoField(primary_key=True)
    market = ForeignKeyField(Market, backref='resolutions', column_name='market_id', unique=True, null=True)
    winning_outcome = TextField(null=True)
    resolution_source = TextField(null=True)
    resolved_at = DateTimeField(null=True)
    timestamp = DateTimeField(constraints=[SQL('DEFAULT CURRENT_TIMESTAMP')])

    class Meta:
        table_name = 'resolutions'

    def __str__(self):
        return f"[RESOLVED] market={self.market_id} winner={self.winning_outcome}"


# ─── Convenience ───────────────────────────────────────────────────────
ALL_MODELS = [Event, Market, Prediction, Trade, MarketSnapshot, Resolution]

def connect():
    """Open database connection."""
    if db.is_closed():
        db.connect()

def close():
    """Close database connection."""
    if not db.is_closed():
        db.close()
