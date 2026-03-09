#!/usr/bin/env python3
import sys
import os
import time
import logging

# Add the release directory to the python path to allow absolute imports within release/
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import json
from database.models import set_db_path as set_sqlite_db_path, init_db, get_db_connection, save_event, save_market
from database.peewee_models import set_db_path as set_peewee_db_path
from core.market_data import fetch_trending_markets, format_order_book_display, fetch_full_market_description, fetch_order_book
from core.analysis import analyze_market_prediction
from core.trading_engine import execute_trade, get_clob_client
from batch.unified_batch import run_auto_pipeline, run_resolve_check, run_integrity_recover, run_integrity_check_only, run_error_recover
from evaluation.archive_errors import archive_and_delete_errors

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.WARNING if config.VERBOSE_MODE else logging.INFO
)
logger = logging.getLogger(__name__)

def get_float_input(prompt: str, min_val: float = None, max_val: float = None) -> float:
    while True:
        try:
            value = float(input(prompt).strip())
            if min_val is not None and value < min_val:
                print(f"Value must be at least {min_val}")
                continue
            if max_val is not None and value > max_val:
                print(f"Value must be at most {max_val}")
                continue
            return value
        except ValueError:
            print("Invalid number. Please enter a valid decimal number.")

def get_choice_input(prompt: str, valid_choices: list) -> str:
    while True:
        choice = input(prompt).strip().lower()
        if choice in valid_choices:
            return choice
        print(f"Invalid choice. Please enter one of: {', '.join(valid_choices)}")

def change_database():
    print(f"\nCurrent Database: {config.DB_PATH}")
    new_path = input("Enter new SQLite database name or path (or press Enter to keep current): ").strip()
    if new_path:
        # Enforce .db suffix
        if not new_path.endswith('.db'):
            new_path += '.db'
            
        # If the user just typed a name (no slashes), default to the ./database/ directory
        if os.sep not in new_path and '/' not in new_path:
            abs_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'database', new_path)
        else:
            # Resolve absolute path relative to current working directory
            abs_path = os.path.abspath(new_path)
        
        # Check if DB exists
        if not os.path.exists(abs_path):
            confirm = get_choice_input(f"Database '{abs_path}' does not exist. Create it? (y/n): ", ['y', 'n'])
            if confirm == 'n':
                print("Database change aborted.")
                return
                
        # Ensure the directory exists
        db_dir = os.path.dirname(abs_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
            print(f"Created directory: {db_dir}")
            
        set_sqlite_db_path(abs_path)
        set_peewee_db_path(abs_path)
        
        # Initialize DB tables if they don't exist
        init_db()
        print(f"Database switched to: {abs_path}")
    else:
        print("Database unchanged.")

def sequence_mode():
    print("\n--- Sequence Mode ---")
    client = get_clob_client()
    if not client:
        print("Warning: CLOB Client could not be initialized. Trading disabled.")
        
    try:
        limit_events = int(input("Enter max trending events to fetch (default: 20): ").strip() or "20")
        print(f"Fetching {limit_events} trending events list (Live context loads sequentially)...")
        events = fetch_trending_markets(limit=limit_events, min_volume=0, fetch_obs=False)
        
        if not events:
            print("No active events found.")
            return
            
        for event in events:
            print("\n" + "="*70)
            print(f"Loading live context for: {event.get('title', 'Unknown')}...")
            
            # Sequentially fetch order books for this single event ONLY
            for m in event['markets']:
                clob_ids = m.get('clobTokenIds', [])
                outcomes = m.get('outcomes', [])
                order_books = {}
                if clob_ids:
                    for idx, token_id in enumerate(clob_ids[:2]):
                        if token_id:
                            ob = fetch_order_book(str(token_id))
                            if ob:
                                outcome_name = outcomes[idx] if idx < len(outcomes) else f"Outcome_{idx}"
                                order_books[outcome_name] = ob
                m['order_books'] = order_books
                
            print(f"Total Volume: ${event.get('total_volume', 0):,.2f}")
            tags_data = event.get('tags', [])
            tags_list = [t.get('label', '') if isinstance(t, dict) else str(t) for t in tags_data]
            print(f"Tags: {', '.join(tags_list)}")
            
            full_desc = event['description']
            if event['markets']:
                try:
                    first_market_id = event['markets'][0]['id']
                    fetched_desc = fetch_full_market_description(first_market_id)
                    if len(fetched_desc) > len(full_desc):
                        full_desc = fetched_desc
                except Exception as e:
                    logger.warning(f"Error fetching full desc: {e}")

            print("\nResolution Rules:")
            print(f"{full_desc}")
            print("-" * 70)
            
            event['description'] = full_desc
            
            print("\nMarket Options:")
            market_map = {}
            for i, m in enumerate(event['markets']):
                print(f"\n  [{i+1}] {m['question']}")
                print(f"      Volume: ${m['volume']:,.2f} | Prices: {m.get('prices')}")
                market_map[str(i+1)] = m
                
                if m.get('order_books'):
                    print("\n      📊 ORDER BOOK:")
                    actual_prices = m.get('prices', [])
                    ob_display = format_order_book_display(m['order_books'], actual_prices=actual_prices, top_n=3)
                    for line in ob_display.split('\n'):
                        print(f"      {line}")
            
            print("Analyzing Event (Multi-Provider)...")
            analysis = analyze_market_prediction(event)
            
            print("\n" + "#"*70)
            print(f"RECAP: {event['title']}")
            if 'news_context' in analysis:
                print("\n--- Context & Evidence (Truncated) ---")
                news_ctx = analysis['news_context']
                print(news_ctx[:1000] + "..." if len(news_ctx) > 1000 else news_ctx)
                print("-" * 40)

            decisions = analysis.get('decisions', [])
            if not decisions:
                print(f"\nNo confident trades identified.")
            else:
                print(f"\nFound {len(decisions)} potential trades:")
                for decision_obj in decisions:
                    print(f"Option ID: {decision_obj.get('decision')} | Action: TRADE (Side: {decision_obj.get('outcome', 'N/A')})")
                    print(f"Confidence: {decision_obj.get('confidence', 0.0)} | Strategy: {decision_obj.get('strategy')}")
                    print(f"Reasoning: {decision_obj.get('reasoning', '')}\n")
            
            user_input = get_choice_input("\n> Do you want to trade on this event? (y/n/skip) [Enter to skip]: ", ['y', 'n', 'skip', 's', ''])
            if user_input == 'y':
                option_idx = input(f"> Select Option Number (1-{len(market_map)}): ").strip()
                selected_market = market_map.get(option_idx)
                if not selected_market:
                    print("Invalid selection.")
                    continue
                
                side = get_choice_input("> Enter side (BUY/SELL): ", ['buy', 'sell']).upper()
                token_id = None
                clob_ids = selected_market.get('clobTokenIds')
                if clob_ids and len(clob_ids) >= 2:
                    choice = get_choice_input("> Select outcome (0 for first/No, 1 for second/Yes): ", ['0', '1'])
                    token_id = clob_ids[int(choice)]
                
                if not token_id:
                    print("Could not determine Token ID. Aborting.")
                    continue

                price = get_float_input("> Enter Limit Price (0.01 - 0.99): ", min_val=0.01, max_val=0.99)
                size = get_float_input(f"> Enter Size (max {config.MAX_TRADE_AMOUNT_USDC}): ", min_val=0.1, max_val=config.MAX_TRADE_AMOUNT_USDC)
                    
                print(f"Executing {side} order for {size} units at ${price}...")
                result = execute_trade(token_id, side, size, price)
                print(f"Trade Result: {result}")
            
            # --- Store Result to Database sequentially ---
            try:
                # 1. Save Base Event and Markets before keeping a connection open to prevent locking
                save_event(event)
                for m in event['markets']:
                    save_market(m, event['id'])

                # 2. Insert AI Predictions sequentially
                conn = get_db_connection()
                c = conn.cursor()
                
                # Format order book snapshot
                ob_snapshot = ""
                if event['markets'] and 'order_books' in event['markets'][0]:
                    ob_snapshot = json.dumps(event['markets'][0]['order_books'])
                
                model_name = analysis.get('provider', 'Unknown')
                raw_resp = json.dumps(analysis)
                news_ctx = analysis.get('news_context', '')
                prompt_ctx = analysis.get('prompt_used', '')

                for m in event['markets']:
                    # 1. Store the Market Snapshot
                    m_ob_snapshot = json.dumps(m.get('order_books', {}))
                    market_prices = json.dumps(m.get('prices', []))
                    
                    c.execute("""
                    INSERT INTO market_snapshots (market_id, event_id, news_context, order_book_snapshot, market_prices, ready_for_analysis, analyzed)
                    VALUES (?, ?, ?, ?, ?, 1, 1)
                    """, (m['id'], event['id'], news_ctx, m_ob_snapshot, market_prices))

                    # 2. Find decisions specifically targeting this market
                    market_decisions = [d for d in decisions if str(d.get('decision')) == str(m['id'])]
                    
                    if not market_decisions:
                        final_decision = "ERROR" if analysis.get('error') or analysis.get('decision') == 'ERROR' else "SKIP"
                        # Store a SKIP or ERROR for this market
                        c.execute("""
                        INSERT INTO predictions (market_id, event_id, model_name, decision, confidence, reasoning, raw_response, order_book_snapshot, news_context, prompt_used)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (m['id'], event['id'], model_name, final_decision, 0.0, analysis.get('reasoning', 'No trade suggested manually.'), raw_resp, ob_snapshot, news_ctx, prompt_ctx))
                    else:
                        for d in market_decisions:
                            # Instead of generic TRADE, ensure it logs correctly. The AI doesn't output an 'action'.
                            c.execute("""
                            INSERT INTO predictions (market_id, event_id, model_name, decision, side, confidence, reasoning, raw_response, order_book_snapshot, news_context, prompt_used)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, (m['id'], event['id'], model_name, 'TRADE', d.get('outcome', ''), d.get('confidence', 0), d.get('reasoning', ''), raw_resp, ob_snapshot, news_ctx, prompt_ctx))
                
                conn.commit()
                conn.close()
                print(f"\n[+] Stored predictions and snapshot for '{event['title']}' to database.")
            except Exception as e:
                logger.error(f"Failed to store sequence mode results: {e}")
                print(f"\nWarning: Failed to save loop state to database: {e}")
            
            cmd = input("\nPress Enter to continue to next event, or 'q' to quit Sequence Mode... ")
            if cmd.lower() == 'q':
                break
                
    except KeyboardInterrupt:
        print("\nStopping Sequence Mode.")
    except Exception as e:
        print(f"Unexpected error: {e}")

def batch_mode():
    print("\n--- Unified Batch Mode ---")
    print("1. Auto-pipeline (Event -> Market Order Books & News -> AI prediction)")
    print("2. Resolve check (Iterate through each market to check whether resolved)")
    print("3. Integrity check (Calculate missing/error items needed to be updated)")
    print("4. Integrity recover (Recover Market OBs, News, and AI prediction integrity)")
    print("5. Error recover (Clear all ERROR predictions and re-predict)")
    print("6. Return to Main Menu")
    
    choice = input("> Select batch pipeline (1-6): ").strip()
    if choice == '1':
        print("\n--- Auto-Pipeline ---")
        try:
            limit_events = int(input("Enter max fresh events to pull (e.g. 30): ").strip() or "30")
            print(f"\nLaunching Auto-Pipeline for {limit_events} events...")
            run_auto_pipeline(limit=limit_events, max_workers=5)
            
            if get_choice_input("\n> Do you want to auto-archive ERROR predictions? (y/n): ", ['y', 'n']) == 'y':
                print("Archiving errors...")
                archive_and_delete_errors()
        except Exception as e:
            print(f"Pipeline error: {e}")
            
    elif choice == '2':
        print("\n--- Resolve Check ---")
        try:
            run_resolve_check(max_workers=10)
        except Exception as e:
            print(f"Resolve check error: {e}")
            
    elif choice == '3':
        try:
            run_integrity_check_only()
        except Exception as e:
            print(f"Integrity calculation error: {e}")
            
    elif choice == '4':
        print("\n--- Integrity Recover ---")
        try:
            run_integrity_recover()
        except Exception as e:
            print(f"Integrity recover error: {e}")

    elif choice == '5':
        print("\n--- Error Recover ---")
        try:
            run_error_recover()
        except Exception as e:
            print(f"Error recovery failed: {e}")

    elif choice == '6':
        print("\nReturning to Main Menu.")
        return
        
    else:
        print("Invalid choice. Returning to Main Menu.")

def evaluate_mode():
    print("\n--- Evaluation Metrics & Plotting ---")
    try:
        from evaluation.evaluate_mimo import analyze
        print("Calculating metrics and generating markdown report...")
        analyze(lot_size=10.0)
        print(f"Done! Report saved to evaluation/mimo_evaluation_report.md")
        
        if get_choice_input("\n> Do you want to generate the Portfolio Value plot? (y/n): ", ['y', 'n']) == 'y':
            from evaluation.plot_portfolio import plot_portfolio
            plot_portfolio()
            
    except Exception as e:
        print(f"Evaluation error: {e}")

def inspect_db_mode():
    print("\n--- Database Inspection ---")
    print("This mode wraps the DB CLI tool. Type 'help' for commands, 'q' to quit.")
    import subprocess
    db_cli_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "db_cli.py")
    
    while True:
        cmd = input("db> ").strip()
        if cmd.lower() in ['q', 'quit', 'exit']:
            break
        if not cmd:
            continue
            
        args = cmd.split()
        try:
            # Pass the currently active DB path to the subprocess
            env = os.environ.copy()
            env["POLY_DB_PATH"] = config.DB_PATH
            subprocess.run([sys.executable, db_cli_path] + args, env=env)
        except Exception as e:
            print(f"Error running db_cli: {e}")

def archive_errors_mode():
    print("\n--- Archive Errors ---")
    archive_and_delete_errors()

def main_menu():
    init_db()
    while True:
        print("\n" + "="*50)
        print("  Polymarket AI Analysis - Standalone CLI")
        print("="*50)
        print(f"Active DB: {config.DB_PATH}")
        print("1. Select / Reselect Database")
        print("2. Sequence Mode (Fetch -> Analyze -> Trade)")
        print("3. Unified Batch Mode (Automated Pipeline)")
        print("4. Evaluate Metrics & Export Plots")
        print("5. Database Inspection CLI")
        print("6. Archive ERROR Predictions")
        print("0. Exit")
        
        choice = input("\n> Select an option (0-6): ").strip()
        
        if choice == '1':
            change_database()
        elif choice == '2':
            sequence_mode()
        elif choice == '3':
            batch_mode()
        elif choice == '4':
            evaluate_mode()
        elif choice == '5':
            inspect_db_mode()
        elif choice == '6':
            archive_errors_mode()
        elif choice == '0':
            print("Exiting...")
            break
        else:
            print("Invalid choice. Try again.")

if __name__ == "__main__":
    main_menu()
