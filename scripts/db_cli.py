#!/usr/bin/env python3
"""
Polymarket Database CLI — Inspect and query the analysis database.

Usage:
    python scripts/db_cli.py stats                    # Database overview
    python scripts/db_cli.py events [EVENT_ID]        # List events by volume or show event details
    python scripts/db_cli.py markets [MARKET_ID]      # List markets by volume or show market details
    python scripts/db_cli.py search "bitcoin"         # Search events & markets
    python scripts/db_cli.py predictions [PRED_ID]    # List AI predictions or show prediction details 
    python scripts/db_cli.py snapshots [--recent N]   # Snapshot statistics
    python scripts/db_cli.py resolutions [--top N]    # List recent market resolutions
    python scripts/db_cli.py sql "SELECT ..."         # Run raw SQL query

Note: Commands have singular/plural aliases, so `event` and `events` are fully interchangeable.
Run with --help for full options.
"""

import argparse
import json
import sys
import os
import textwrap

# Add project root to path so we can import database package
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.peewee_models import (
    db, connect, close,
    Event, Market, Prediction, Trade, MarketSnapshot, Resolution,
    ALL_MODELS
)
from peewee import fn, SQL, DoesNotExist


# ─── Formatting Helpers ────────────────────────────────────────────────

def fmt_volume(v):
    """Format volume as human-readable USD string."""
    if v is None:
        return "$0"
    if v >= 1_000_000:
        return f"${v / 1_000_000:,.1f}M"
    elif v >= 1_000:
        return f"${v / 1_000:,.1f}K"
    else:
        return f"${v:,.0f}"


def fmt_price(p):
    """Format a probability price as percentage."""
    if p is None:
        return "—"
    return f"{float(p) * 100:.1f}%"


def truncate(text, length=80):
    """Truncate text with ellipsis."""
    if not text:
        return ""
    text = text.replace('\n', ' ').replace('\r', '')
    if len(text) > length:
        return text[:length - 3] + "..."
    return text


def print_divider(char="─", width=80):
    print(char * width)


def print_header(title, width=80):
    print()
    print_divider("═", width)
    print(f"  {title}")
    print_divider("═", width)


def print_section(title):
    print(f"\n  --- {title} ---")


def print_kv(key, value, indent=4):
    """Print a key-value pair with alignment."""
    spaces = " " * indent
    print(f"{spaces}{key + ':':<22s} {value}")


def print_json_field(key, json_str, indent=4):
    """Parse and pretty-print a JSON field."""
    spaces = " " * indent
    if not json_str:
        print(f"{spaces}{key + ':':<22s} (empty)")
        return
    try:
        data = json.loads(json_str)
        if isinstance(data, list) and len(data) <= 5:
            print(f"{spaces}{key + ':':<22s} {data}")
        else:
            formatted = json.dumps(data, indent=2)
            # Indent each line
            lines = formatted.split('\n')
            print(f"{spaces}{key + ':':<22s}")
            for line in lines[:20]:
                print(f"{spaces}  {line}")
            if len(lines) > 20:
                print(f"{spaces}  ... ({len(lines) - 20} more lines)")
    except json.JSONDecodeError:
        print(f"{spaces}{key + ':':<22s} {truncate(json_str, 60)}")


# ─── Commands ──────────────────────────────────────────────────────────

def cmd_stats(args):
    """Show database overview statistics."""
    target_table = getattr(args, 'table', None)

    if target_table:
        target_table = target_table.lower()
        valid_tables = {m._meta.table_name: m for m in ALL_MODELS}
        if target_table not in valid_tables:
            print(f"Error: Unknown table '{target_table}'. Available tables: {', '.join(valid_tables.keys())}")
            return
        print_header(f"STATISTICS FOR: {target_table.upper()}")
    else:
        print_header("DATABASE OVERVIEW")

        # Table row counts
        print_section("Table Sizes")
        for model in ALL_MODELS:
            count = model.select().count()
            print_kv(model._meta.table_name, f"{count:,} rows")

    # Event stats
    if not target_table or target_table == 'events':
        print_section("Event Statistics")
        total_events = Event.select().count()
        active_events = Event.select().where(Event.active == True).count()
        total_vol = Event.select(fn.SUM(Event.total_volume)).scalar() or 0
        print_kv("Total events", f"{total_events:,}")
        print_kv("Active events", f"{active_events:,}")
        print_kv("Total volume", fmt_volume(total_vol))
        
        # Tag breakdown
        tag_counts = {}
        for ev in Event.select(Event.tags):
            if ev.tags:
                try:
                    tags = json.loads(ev.tags)
                    for t in tags:
                        tag_counts[t] = tag_counts.get(t, 0) + 1
                except:
                    pass
        if tag_counts:
            print("\n  Categorical Tags:")
            for tag, c in sorted(tag_counts.items(), key=lambda x: x[1], reverse=True):
                print_kv(f"    {tag}", f"{c:,} events")

    # Market stats
    if not target_table or target_table == 'markets':
        print_section("Market Statistics")
        total_markets = Market.select().count()
        active_markets = Market.select().where(Market.active == True).count()
        total_mkt_vol = Market.select(fn.SUM(Market.volume)).scalar() or 0
        avg_vol = Market.select(fn.AVG(Market.volume)).scalar() or 0
        max_vol = Market.select(fn.MAX(Market.volume)).scalar() or 0
        print_kv("Total markets", f"{total_markets:,}")
        print_kv("Active markets", f"{active_markets:,}")
        print_kv("Total volume", fmt_volume(total_mkt_vol))
        print_kv("Avg volume", fmt_volume(avg_vol))
        print_kv("Max volume", fmt_volume(max_vol))

    # Resolution stats
    if not target_table or target_table == 'resolutions':
        print_section("Resolution Statistics")
        total_resolutions = Resolution.select().count()
        print_kv("Total resolutions", f"{total_resolutions:,}")
        if total_resolutions > 0:
            latest = Resolution.select().order_by(Resolution.timestamp.desc()).first()
            print_kv("Latest grabbed", str(latest.timestamp))

    # Snapshot stats
    if not target_table or target_table == 'snapshots':
        print_section("Snapshot Statistics")
        total_snaps = MarketSnapshot.select().count()
        ready_snaps = MarketSnapshot.select().where(MarketSnapshot.ready_for_analysis == 1).count()
        analyzed_snaps = MarketSnapshot.select().where(MarketSnapshot.analyzed == 1).count()
        with_news = MarketSnapshot.select().where(
            fn.LENGTH(MarketSnapshot.news_context) > 100
        ).count()
        with_ob = MarketSnapshot.select().where(
            fn.LENGTH(MarketSnapshot.order_book_snapshot) > 10
        ).count()
        # Snapshots by market active status (join to markets table)
        snaps_active = (MarketSnapshot
            .select()
            .join(Market, on=(MarketSnapshot.market == Market.id))
            .where(Market.active == True)
            .count())
        snaps_inactive = total_snaps - snaps_active
        print_kv("Total snapshots", f"{total_snaps:,}")
        print_kv("  Active markets", f"{snaps_active:,} ({100*snaps_active/max(total_snaps,1):.1f}%)")
        print_kv("  Inactive markets", f"{snaps_inactive:,} ({100*snaps_inactive/max(total_snaps,1):.1f}%)")
        print_kv("Ready for analysis", f"{ready_snaps:,}")
        print_kv("Analyzed", f"{analyzed_snaps:,}")
        print_kv("With news data", f"{with_news:,}")
        
        news_failed = MarketSnapshot.select().where(
            MarketSnapshot.news_context.contains('[Failed to fetch content') |
            MarketSnapshot.news_context.contains('(Content unavailable)')
        ).count()
        news_js_blocked = MarketSnapshot.select().where(
            MarketSnapshot.news_context.contains('enable JS') |
            MarketSnapshot.news_context.contains('JavaScript is not available') |
            MarketSnapshot.news_context.contains('ad blocker') |
            MarketSnapshot.news_context.contains('Enable JavaScript')
        ).count()
        print_kv("  News fetch errors", f"{news_failed:,} ({news_failed/max(total_snaps,1):.1%})")
        print_kv("  News JS blocked", f"{news_js_blocked:,} ({news_js_blocked/max(total_snaps,1):.1%})")
        
        print_kv("With order book", f"{with_ob:,}")

        pred_ready = Resolution.select().where(
            Resolution.market_id.in_(
                MarketSnapshot.select(MarketSnapshot.market_id).where(fn.LENGTH(MarketSnapshot.order_book_snapshot) > 10)
            )
        ).count()
        print_kv("Prediction-ready", f"{pred_ready:,}")

    # Prediction stats
    if not target_table or target_table == 'predictions':
        print_section("Prediction Statistics")
        total_preds = Prediction.select().count()
        if total_preds > 0:
            models = [p.model_name for p in Prediction.select(Prediction.model_name).distinct().order_by(Prediction.model_name)]
            for model in models:
                model_display = model if model else "Unknown Model"
                model_preds = Prediction.select().where(Prediction.model_name == model).count()
                print(f"  --- {model_display} ({model_preds:,} total) ---")
                for display_dec, db_decs in [('TRADE', ['TRADE', 'BUY', 'SELL', 'HOLD']), ('SKIP', ['SKIP']), ('ERROR', ['ERROR'])]:
                    count = Prediction.select().where((Prediction.model_name == model) & (Prediction.decision.in_(db_decs))).count()
                    print_kv(display_dec, f"{count:,}")
                avg_conf = Prediction.select(fn.AVG(Prediction.confidence)).where(Prediction.model_name == model).scalar() or 0
                print_kv("Avg confidence", f"{avg_conf:.1%}")
                print()
            
            print(f"  --- OVERALL ({total_preds:,} total) ---")
            for display_dec, db_decs in [('TRADE', ['TRADE', 'BUY', 'SELL', 'HOLD']), ('SKIP', ['SKIP']), ('ERROR', ['ERROR'])]:
                count = Prediction.select().where(Prediction.decision.in_(db_decs)).count()
                print_kv(display_dec, f"{count:,}")
            avg_conf = Prediction.select(fn.AVG(Prediction.confidence)).scalar() or 0
            print_kv("Avg confidence", f"{avg_conf:.1%}")
        else:
            print_kv("Total", "0 (no predictions yet)")

    # Trades stats
    if target_table == 'trades':
        print_section("Trade Statistics")
        total_trades = Trade.select().count()
        print_kv("Total trades", f"{total_trades:,}")

    print()


def cmd_events(args):
    """List events sorted by volume."""
    limit = args.top
    active_only = not args.all

    query = Event.select()
    if active_only:
        query = query.where(Event.active == True)
    query = query.order_by(Event.total_volume.desc()).limit(limit)

    title = f"TOP {limit} EVENTS" + (" (active only)" if active_only else " (all)")
    print_header(title)

    print(f"  {'#':<4} {'Volume':>12}  {'Markets':>7}  {'Title':<50}")
    print_divider()

    for i, event in enumerate(query, 1):
        mkt_count = event.markets.count()
        title = truncate(event.title or "(no title)", 50)
        vol = fmt_volume(event.total_volume)
        print(f"  {i:<4} {vol:>12}  {mkt_count:>7}  {title}")

    print(f"\n  Showing {min(limit, query.count())} of {Event.select().count()} total events.")
    print(f"  Tip: use --all to include closed events, --top N to change limit.\n")


def cmd_markets(args):
    """List markets sorted by volume."""
    limit = args.top
    active_only = not args.all

    query = (Market
        .select(Market, Event.title.alias('event_title'))
        .join(Event)
    )
    if active_only:
        query = query.where(Market.active == True)
    query = query.order_by(Market.volume.desc()).limit(limit)

    title = f"TOP {limit} MARKETS" + (" (active only)" if active_only else " (all)")
    print_header(title)

    print(f"  {'#':<4} {'Volume':>12}  {'Price':>7}  {'Question':<52}")
    print_divider()

    for i, m in enumerate(query, 1):
        prices = m.prices_list
        price_str = fmt_price(prices[0]) if prices else "—"
        question = truncate(m.question or "(no question)", 52)
        vol = fmt_volume(m.volume)
        print(f"  {i:<4} {vol:>12}  {price_str:>7}  {question}")

    total = Market.select().count()
    print(f"\n  Showing {min(limit, query.count())} of {total:,} total markets.")
    print(f"  Tip: use 'db_cli.py market <ID>' to see full details.\n")


def cmd_search(args):
    """Search events and markets by keyword."""
    keyword = args.keyword
    limit = args.top

    print_header(f"SEARCH: '{keyword}'")

    # Search events
    event_results = (Event
        .select()
        .where(
            (Event.title.contains(keyword)) |
            (Event.description.contains(keyword))
        )
        .order_by(Event.total_volume.desc())
        .limit(limit)
    )

    event_count = event_results.count()
    print_section(f"Events ({event_count} matches)")

    if event_count > 0:
        for i, e in enumerate(event_results, 1):
            mkt_count = e.markets.count()
            print(f"    {i}. [{e.id}] {truncate(e.title, 55)}")
            print(f"       Vol: {fmt_volume(e.total_volume)}  |  Markets: {mkt_count}  |  Active: {e.active}")
    else:
        print("    (no matching events)")

    # Search markets
    market_results = (Market
        .select(Market, Event.title.alias('event_title'))
        .join(Event)
        .where(
            (Market.question.contains(keyword)) |
            (Market.description.contains(keyword))
        )
        .order_by(Market.volume.desc())
        .limit(limit)
    )

    mkt_count = market_results.count()
    print_section(f"Markets ({mkt_count} matches)")

    if mkt_count > 0:
        for i, m in enumerate(market_results, 1):
            prices = m.prices_list
            price_str = f"@ {fmt_price(prices[0])}" if prices else ""
            print(f"    {i}. [{m.id}] {truncate(m.question, 50)} {price_str}")
            print(f"       Event: {truncate(m.event.title, 40)}  |  Vol: {fmt_volume(m.volume)}  |  Active: {m.active}")
    else:
        print("    (no matching markets)")

    print()


def cmd_event_detail(args):
    """Show full details for a specific event."""
    event_id = args.event_id

    try:
        event = Event.get_by_id(event_id)
    except DoesNotExist:
        # Try partial match
        results = Event.select().where(Event.id.contains(event_id)).limit(5)
        if results.count() == 0:
            print(f"Event '{event_id}' not found.")
            return
        elif results.count() == 1:
            event = results[0]
        else:
            print(f"Multiple matches for '{event_id}':")
            for e in results:
                print(f"  [{e.id}] {truncate(e.title, 60)}")
            return

    print_header(f"EVENT: {event.title}")

    print_section("Details")
    print_kv("ID", event.id)
    print_kv("Title", event.title or "(none)")
    print_kv("Slug", event.slug or "(none)")
    print_kv("Tags", event.tags or "(none)")
    print_kv("Volume", fmt_volume(event.total_volume))
    print_kv("Active", str(event.active))
    print_kv("Start Date", event.start_date or "(none)")
    print_kv("End Date", event.end_date or "(none)")
    print_kv("Last Updated", str(event.last_updated))

    if event.description:
        print_section("Description / Rules")
        wrapped = textwrap.fill(event.description[:500], width=76, initial_indent="    ", subsequent_indent="    ")
        print(wrapped)
        if len(event.description) > 500:
            print(f"    ... ({len(event.description)} chars total)")

    # List markets in this event
    markets = (Market
        .select()
        .where(Market.event == event)
        .order_by(Market.volume.desc())
    )

    print_section(f"Markets ({markets.count()})")
    for i, m in enumerate(markets, 1):
        prices = m.prices_list
        price_str = f"@ {fmt_price(prices[0])}" if prices else ""
        print(f"    {i}. [{m.id}] {truncate(m.question, 45)} {price_str}")
        print(f"       Vol: {fmt_volume(m.volume)}  |  Active: {m.active}")

    print()


def cmd_market_detail(args):
    """Show full details for a specific market."""
    market_id = args.market_id

    try:
        market = Market.get_by_id(market_id)
    except DoesNotExist:
        # Try partial/numeric match
        results = Market.select().where(Market.id.contains(market_id)).limit(5)
        if results.count() == 0:
            print(f"Market '{market_id}' not found.")
            return
        elif results.count() == 1:
            market = results[0]
        else:
            print(f"Multiple matches for '{market_id}':")
            for m in results:
                print(f"  [{m.id}] {truncate(m.question, 60)}")
            return

    print_header(f"MARKET: {market.question}")

    print_section("Basic Info")
    print_kv("ID", market.id)
    print_kv("Question", market.question or "(none)")
    print_kv("Event", market.event.title if market.event else "(none)")
    print_kv("Event ID", market.event_id)
    print_kv("Volume", fmt_volume(market.volume))
    print_kv("Liquidity", fmt_volume(market.liquidity))
    print_kv("Active", str(market.active))
    print_kv("End Date", market.end_date or "(none)")
    print_kv("Last Updated", str(market.last_updated))

    print_section("Outcomes & Prices")
    outcomes = market.outcomes_list
    prices = market.prices_list
    tokens = market.token_ids
    for i, outcome in enumerate(outcomes):
        price = fmt_price(prices[i]) if i < len(prices) else "—"
        token = tokens[i] if i < len(tokens) else "—"
        print(f"    {outcome:<10s}  Price: {price:>7s}  Token: {truncate(token, 40)}")

    if market.description:
        print_section("Resolution Rules")
        wrapped = textwrap.fill(market.description[:800], width=76, initial_indent="    ", subsequent_indent="    ")
        print(wrapped)
        if len(market.description) > 800:
            print(f"    ... ({len(market.description)} chars total)")

    # Resolution Data
    try:
        res = Resolution.get(Resolution.market == market)
        print_section("Resolution Result [CLOSED]")
        print_kv("Winner", res.winning_outcome or "(Unknown)")
        print_kv("Source", res.resolution_source or "(Unknown)")
        print_kv("Resolved At", str(res.resolved_at))
        print_kv("Grabbed At", str(res.timestamp))
    except DoesNotExist:
        print_section("Resolution Status")
        print("    Market is not yet marked as resolved in the database.")

    # Snapshots
    snapshots = (MarketSnapshot
        .select()
        .where(MarketSnapshot.market == market)
        .order_by(MarketSnapshot.timestamp.desc())
        .limit(3)
    )
    snap_count = MarketSnapshot.select().where(MarketSnapshot.market == market).count()

    print_section(f"Snapshots ({snap_count} total, showing latest {min(3, snap_count)})")
    for i, snap in enumerate(snapshots):
        news_len = len(snap.news_context) if snap.news_context else 0
        ob_len = len(snap.order_book_snapshot) if snap.order_book_snapshot else 0
        
        news_errs = 0
        if snap.news_context:
            news_errs += snap.news_context.count('[Failed to fetch content')
            news_errs += snap.news_context.count('(Content unavailable)')
            news_errs += snap.news_context.count('enable JS')
            news_errs += snap.news_context.count('JavaScript is not available')
            news_errs += snap.news_context.count('ad blocker')
            news_errs += snap.news_context.count('Enable JavaScript')
            
        err_str = f"  |  Failed news: {news_errs}" if news_errs > 0 else ""
        
        print(f"    #{snap.id}  @ {snap.timestamp}")
        print(f"      News: {news_len:,} chars{err_str}  |  Order Book: {ob_len:,} chars  |  Analyzed: {bool(snap.analyzed)}")
        
        if i == 0 and snap.order_book_snapshot:
            print("      --- Latest Order Book Data ---")
            try:
                from core.market_data import format_order_book_display
                ob_data = json.loads(snap.order_book_snapshot)
                prices = market.prices_list
                formatted_ob = format_order_book_display(ob_data, actual_prices=prices, top_n=5)
                # Intend the output to fit visually under the snapshot
                indented_ob = textwrap.indent(formatted_ob, "      ")
                print(indented_ob)
            except Exception as e:
                print(f"      Error formatting order book: {e}")
                print_json_field("Order Book", snap.order_book_snapshot, indent=6)

    # Predictions
    predictions = (Prediction
        .select()
        .where(Prediction.market == market)
        .order_by(Prediction.timestamp.desc())
        .limit(5)
    )
    pred_count = Prediction.select().where(Prediction.market == market).count()

    print_section(f"AI Predictions ({pred_count} total)")
    if pred_count == 0:
        print("    (no predictions yet)")
    else:
        for pred in predictions:
            print(f"    [{pred.decision}] Side: {pred.side or '—'}  Confidence: {pred.confidence or 0:.0%}")
            print(f"      ID: {pred.id}  Model: {pred.model_name}  @ {pred.timestamp}")
            if pred.reasoning:
                wrapped = textwrap.fill(truncate(pred.reasoning, 200), width=72, initial_indent="      ", subsequent_indent="      ")
                print(wrapped)

    print()


def cmd_predictions(args):
    """List AI predictions grouped by model."""
    limit = args.top

    models = [p.model_name for p in Prediction.select(Prediction.model_name).distinct().order_by(Prediction.model_name)]
    total = Prediction.select().count()

    print_header(f"AI PREDICTIONS ({total} total)")

    if total == 0:
        print("  No predictions yet. Run batch_analyzer.py to generate predictions.")
        print()
        return

    for model in models:
        query = (Prediction
            .select(Prediction, Market.question)
            .join(Market, on=(Prediction.market == Market.id))
            .where(Prediction.model_name == model)
            .order_by(Prediction.timestamp.desc())
            .limit(limit)
        )
        
        model_display = model if model else "Unknown Model"
        count_for_model = Prediction.select().where(Prediction.model_name == model).count()
        print(f"\n  --- Model: {model_display} ({count_for_model} total) ---")
        print(f"  {'ID':<6} {'Decision':<8} {'Side':<5} {'Conf':>5}  {'Market':<45}")
        print_divider()

        for pred in query:
            conf_str = f"{pred.confidence:.0%}" if pred.confidence else "—"
            question = truncate(pred.market.question or "—", 45)
            print(f"  {pred.id:<6} {pred.decision or '—':<8} {pred.side or '—':<5} {conf_str:>5}  {question}")
            
            if getattr(args, 'reasoning', False) and pred.reasoning:
                wrapped = textwrap.wrap(pred.reasoning, width=90)
                for line in wrapped:
                    print(f"       ↳ {line}")
    print()

def cmd_prediction_detail(args):
    """Show full details for a specific prediction."""
    pred_id = args.prediction_id

    try:
        pred = Prediction.get_by_id(pred_id)
    except DoesNotExist:
        print(f"Prediction '{pred_id}' not found.")
        return

    print_header(f"PREDICTION: #{pred.id}")

    print_section("Basic Info")
    print_kv("ID", str(pred.id))
    print_kv("Market ID", pred.market_id)
    print_kv("Market Q", pred.market.question if pred.market else "(none)")
    print_kv("Event ID", pred.event_id)
    print_kv("Model", pred.model_name or "(none)")
    print_kv("Timestamp", str(pred.timestamp))
    print_kv("Decision", pred.decision or "(none)")
    print_kv("Side", pred.side or "(none)")
    print_kv("Confidence", f"{pred.confidence:.0%}" if pred.confidence is not None else "(none)")

    if pred.reasoning:
        print_section("Reasoning")
        wrapped = textwrap.fill(pred.reasoning, width=76, initial_indent="    ", subsequent_indent="    ")
        print(wrapped)

    if pred.raw_response:
        print_section("Raw Output")
        print_json_field("raw_response", pred.raw_response)
        
    if pred.prompt_used:
        print_section("Prompt Used")
        wrapped = textwrap.fill(pred.prompt_used[:800], width=76, initial_indent="    ", subsequent_indent="    ")
        print(wrapped)
        if len(pred.prompt_used) > 800:
            print(f"    ... ({len(pred.prompt_used)} chars total)")

    print()

def cmd_resolutions(args):
    """List most recent market resolutions."""
    limit = args.top

    query = (Resolution
        .select(Resolution, Market.question)
        .join(Market, on=(Resolution.market == Market.id))
        .order_by(Resolution.resolved_at.desc())
        .limit(limit)
    )

    total = Resolution.select().count()
    print_header(f"RECENT RESOLUTIONS ({total} total)")

    if total == 0:
        print("  No resolutions found. Run batch/bulk_grab_resolutions.py first.")
        print()
        return

    print(f"  {'#':<4} {'Date':<12} {'Winner':<10} {'Market Question':<45}")
    print_divider()

    for i, res in enumerate(query, 1):
        winner = truncate(res.winning_outcome or "—", 10)
        question = truncate(res.market.question or "—", 45)
        # Handle string or datetime for resolved_at
        date_str = str(res.resolved_at)[:10] if res.resolved_at else "Unknown"
        print(f"  {i:<4} {date_str:<12} {winner:<10} {question}")

    print()


def cmd_snapshot_detail(args):
    """Show details for a specific snapshot."""
    snapshot_id = args.snapshot_id

    try:
        snap = MarketSnapshot.get_by_id(snapshot_id)
    except DoesNotExist:
        print(f"Snapshot '{snapshot_id}' not found.")
        return

    print_header(f"SNAPSHOT: #{snap.id}")

    print_section("Basic Info")
    print_kv("ID", str(snap.id))
    print_kv("Market ID", snap.market_id)
    print_kv("Market Q", snap.market.question if snap.market else "(none)")
    print_kv("Event ID", snap.event_id)
    print_kv("Timestamp", str(snap.timestamp))
    print_kv("Analyzed", str(bool(snap.analyzed)))
    print_kv("Ready for Analysis", str(bool(snap.ready_for_analysis)))

    if snap.market_prices:
        print_section("Market Prices")
        print_json_field("market_prices", snap.market_prices)

    if snap.order_book_snapshot:
        print_section("Order Book Snapshot")
        print_json_field("order_book", snap.order_book_snapshot)

    if snap.news_context:
        print_section("News Context")
        wrapped = textwrap.fill(snap.news_context[:1000], width=76, initial_indent="    ", subsequent_indent="    ")
        print(wrapped)
        if len(snap.news_context) > 1000:
            print(f"    ... ({len(snap.news_context):,} chars total)")

    print()


def cmd_snapshots(args):
    """Show snapshot statistics and status."""
    print_header("MARKET SNAPSHOTS")

    total = MarketSnapshot.select().count()
    ready = MarketSnapshot.select().where(MarketSnapshot.ready_for_analysis == 1).count()
    analyzed = MarketSnapshot.select().where(MarketSnapshot.analyzed == 1).count()
    pending = ready - analyzed

    with_news = MarketSnapshot.select().where(fn.LENGTH(MarketSnapshot.news_context) > 10).count()
    with_ob = MarketSnapshot.select().where(fn.LENGTH(MarketSnapshot.order_book_snapshot) > 10).count()
    empty_both = MarketSnapshot.select().where(
        (fn.LENGTH(MarketSnapshot.news_context) <= 10) &
        (fn.LENGTH(MarketSnapshot.order_book_snapshot) <= 10)
    ).count()

    # Snapshots by market active status
    snaps_active = (MarketSnapshot
        .select()
        .join(Market, on=(MarketSnapshot.market == Market.id))
        .where(Market.active == True)
        .count())
    snaps_inactive = total - snaps_active

    print_section("Overview")
    print_kv("Total snapshots", f"{total:,}")
    print_kv("  Active markets", f"{snaps_active:,} ({100*snaps_active/max(total,1):.1f}%)")
    print_kv("  Inactive markets", f"{snaps_inactive:,} ({100*snaps_inactive/max(total,1):.1f}%)")
    print_kv("Ready for analysis", f"{ready:,}")
    print_kv("Analyzed", f"{analyzed:,}")
    print_kv("Pending analysis", f"{pending:,}")

    news_failed = MarketSnapshot.select().where(
        MarketSnapshot.news_context.contains('[Failed to fetch content') |
        MarketSnapshot.news_context.contains('(Content unavailable)')
    ).count()
    news_js_blocked = MarketSnapshot.select().where(
        MarketSnapshot.news_context.contains('enable JS') |
        MarketSnapshot.news_context.contains('JavaScript is not available') |
        MarketSnapshot.news_context.contains('ad blocker') |
        MarketSnapshot.news_context.contains('Enable JavaScript')
    ).count()
    
    print_section("News Fetch Quality")
    print_kv("Snapshots with fetch errors", f"{news_failed:,} ({news_failed/max(total,1):.1%})")
    print_kv("Snapshots with JS blockers", f"{news_js_blocked:,} ({news_js_blocked/max(total,1):.1%})")

    # Breakdown by active status
    snaps_active_news = (MarketSnapshot.select().join(Market, on=(MarketSnapshot.market == Market.id)).where((Market.active == True) & (fn.LENGTH(MarketSnapshot.news_context) > 10)).count())
    snaps_active_ob = (MarketSnapshot.select().join(Market, on=(MarketSnapshot.market == Market.id)).where((Market.active == True) & (fn.LENGTH(MarketSnapshot.order_book_snapshot) > 10)).count())
    
    snaps_inactive_news = (MarketSnapshot.select().join(Market, on=(MarketSnapshot.market == Market.id)).where((Market.active == False) & (fn.LENGTH(MarketSnapshot.news_context) > 10)).count())
    snaps_inactive_ob = (MarketSnapshot.select().join(Market, on=(MarketSnapshot.market == Market.id)).where((Market.active == False) & (fn.LENGTH(MarketSnapshot.order_book_snapshot) > 10)).count())

    print_section("Data Quality")
    print_kv("Active markets", f"{snaps_active:,} total snapshots")
    if snaps_active > 0:
        print_kv("  With news data", f"{snaps_active_news:,} ({100*snaps_active_news/snaps_active:.1f}%)")
        print_kv("  With order book", f"{snaps_active_ob:,} ({100*snaps_active_ob/snaps_active:.1f}%)")
    
    print_kv("Inactive markets", f"{snaps_inactive:,} total snapshots")
    if snaps_inactive > 0:
        print_kv("  With news data", f"{snaps_inactive_news:,} ({100*snaps_inactive_news/snaps_inactive:.1f}%)")
        print_kv("  With order book", f"{snaps_inactive_ob:,} ({100*snaps_inactive_ob/snaps_inactive:.1f}%)")
        
    print_kv("Empty (no data overall)", f"{empty_both:,} ({100*empty_both/max(total,1):.1f}%)")

    pred_ready = Resolution.select().where(
        Resolution.market_id.in_(
            MarketSnapshot.select(MarketSnapshot.market_id).where(fn.LENGTH(MarketSnapshot.order_book_snapshot) > 10)
        )
    ).count()
    print_kv("Prediction-ready markets", f"{pred_ready:,} (Resolved + Order Book)")

    # Markets WITHOUT snapshots
    markets_with_snap = MarketSnapshot.select(MarketSnapshot.market).distinct()
    active_without = (Market
        .select()
        .where(
            (Market.active == True) &
            (Market.id.not_in(markets_with_snap))
        )
        .count()
    )
    inactive_without = (Market
        .select()
        .where(
            (Market.active == False) &
            (Market.id.not_in(markets_with_snap))
        )
        .count()
    )
    print_kv("Markets w/o snapshot", f"{active_without + inactive_without:,} (active: {active_without:,}, inactive: {inactive_without:,})")

    if args.recent:
        print_section(f"Recent Snapshots (last {args.recent})")
        recent = (MarketSnapshot
            .select(MarketSnapshot, Market.question)
            .join(Market, on=(MarketSnapshot.market == Market.id))
            .order_by(MarketSnapshot.timestamp.desc())
            .limit(args.recent)
        )
        for snap in recent:
            news_len = len(snap.news_context) if snap.news_context else 0
            ob_len = len(snap.order_book_snapshot) if snap.order_book_snapshot else 0
            analyzed_str = "DONE" if snap.analyzed else "PENDING"
            
            news_errs = 0
            if snap.news_context:
                news_errs += snap.news_context.count('[Failed to fetch content')
                news_errs += snap.news_context.count('(Content unavailable)')
                news_errs += snap.news_context.count('enable JS')
                news_errs += snap.news_context.count('JavaScript is not available')
                
            err_indicator = "(!)" if news_errs > 0 else "   "
            
            print(f"    #{snap.id:<6}  {analyzed_str:<8}  News: {news_len:>6,}ch {err_indicator} OB: {ob_len:>6,}ch  {truncate(snap.market.question, 35)}")

    print()


def cmd_sql(args):
    """Execute a raw SQL query and display results."""
    query_str = args.query

    print_header(f"SQL QUERY")
    print(f"  > {query_str}")
    print()

    cursor = db.execute_sql(query_str)

    if cursor.description is None:
        print(f"  (query executed, {cursor.rowcount} row(s) affected)")
        print()
        return

    # Get column names
    columns = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()

    if not rows:
        print("  (no results)")
        print()
        return

    # Calculate column widths
    col_widths = [len(c) for c in columns]
    display_rows = []
    for row in rows[:args.limit]:
        display_row = []
        for i, val in enumerate(row):
            s = str(val) if val is not None else "NULL"
            if len(s) > 60:
                s = s[:57] + "..."
            display_row.append(s)
            col_widths[i] = max(col_widths[i], len(s))
        display_rows.append(display_row)

    # Print header
    header = "  " + "  ".join(f"{c:<{col_widths[i]}}" for i, c in enumerate(columns))
    print(header)
    print("  " + "  ".join("─" * w for w in col_widths))

    # Print rows
    for row in display_rows:
        line = "  " + "  ".join(f"{val:<{col_widths[i]}}" for i, val in enumerate(row))
        print(line)

    if len(rows) > args.limit:
        print(f"\n  ... showing {args.limit} of {len(rows)} rows (use --limit N to see more)")

    print(f"\n  {len(rows)} row(s) returned.\n")


# ─── Command Routers ───────────────────────────────────────────────────

def cmd_events_or_event(args):
    if getattr(args, 'event_id', None):
        return cmd_event_detail(args)
    return cmd_events(args)

def cmd_markets_or_market(args):
    if getattr(args, 'market_id', None):
        return cmd_market_detail(args)
    return cmd_markets(args)

def cmd_predictions_or_prediction(args):
    if getattr(args, 'prediction_id', None):
        return cmd_prediction_detail(args)
    return cmd_predictions(args)

def cmd_snapshots_or_snapshot(args):
    if getattr(args, 'snapshot_id', None):
        return cmd_snapshot_detail(args)
    return cmd_snapshots(args)

# ─── Main ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Polymarket Database CLI — Inspect and query the analysis database.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
        Examples:
          python scripts/db_cli.py stats
          python scripts/db_cli.py events --top 20
          python scripts/db_cli.py events 12345
          python scripts/db_cli.py markets --top 10 --all
          python scripts/db_cli.py markets 67890
          python scripts/db_cli.py search "bitcoin"
          python scripts/db_cli.py predictions --top 10
          python scripts/db_cli.py prediction 123
          python scripts/db_cli.py snapshots --recent 10
          python scripts/db_cli.py resolutions --top 15
          python scripts/db_cli.py sql "SELECT COUNT(*) FROM markets WHERE active=1"
        """)
    )
    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    # stats / stat
    sp = subparsers.add_parser('stats', aliases=['stat'], help='Database overview or specific table statistics')
    sp.add_argument('table', type=str, nargs='?', help='Optional table name to view specific stats (events, markets, predictions, snapshots, resolutions, trades)')
    sp.set_defaults(func=cmd_stats)

    # events / event
    sp = subparsers.add_parser('events', aliases=['event'], help='List events by volume or show details')
    sp.add_argument('event_id', type=str, nargs='?', help='Optional Event ID to show details instead of listing')
    sp.add_argument('--top', type=int, default=20, help='Number of events to show (default: 20)')
    sp.add_argument('--all', action='store_true', help='Include closed/inactive events')
    sp.set_defaults(func=cmd_events_or_event)

    # markets / market
    sp = subparsers.add_parser('markets', aliases=['market'], help='List markets by volume or show details')
    sp.add_argument('market_id', type=str, nargs='?', help='Optional Market ID to show details instead of listing')
    sp.add_argument('--top', type=int, default=20, help='Number of markets to show (default: 20)')
    sp.add_argument('--all', action='store_true', help='Include closed/inactive markets')
    sp.set_defaults(func=cmd_markets_or_market)

    # search
    sp = subparsers.add_parser('search', help='Search events and markets by keyword')
    sp.add_argument('keyword', type=str, help='Search keyword')
    sp.add_argument('--top', type=int, default=10, help='Max results per category (default: 10)')
    sp.set_defaults(func=cmd_search)

    # predictions / prediction
    sp = subparsers.add_parser('predictions', aliases=['prediction'], help='List AI predictions or show details')
    sp.add_argument('prediction_id', type=int, nargs='?', help='Optional Prediction ID to show details instead of listing')
    sp.add_argument('--top', type=int, default=20, help='Number of predictions to show (default: 20)')
    sp.add_argument('--reasoning', action='store_true', help='Show AI reasoning for each prediction')
    sp.set_defaults(func=cmd_predictions_or_prediction)

    # snapshots / snapshot
    sp = subparsers.add_parser('snapshots', aliases=['snapshot'], help='Snapshot statistics or detail')
    sp.add_argument('snapshot_id', type=int, nargs='?', help='Optional Snapshot ID to show details instead of listing stats')
    sp.add_argument('--recent', type=int, default=0, help='Show N most recent snapshots')
    sp.set_defaults(func=cmd_snapshots_or_snapshot)

    # resolutions / resolution
    sp = subparsers.add_parser('resolutions', aliases=['resolution'], help='List recent market resolutions')
    sp.add_argument('--top', type=int, default=20, help='Number of resolutions to show (default: 20)')
    sp.set_defaults(func=cmd_resolutions)

    # sql
    sp = subparsers.add_parser('sql', help='Run a raw SQL query')
    sp.add_argument('query', type=str, help='SQL query string')
    sp.add_argument('--limit', type=int, default=50, help='Max rows to display (default: 50)')
    sp.set_defaults(func=cmd_sql)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    try:
        connect()
        args.func(args)
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
    finally:
        close()


if __name__ == "__main__":
    main()
