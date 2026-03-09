import requests
import logging
from typing import Optional, Dict, List, Any, Tuple

logger = logging.getLogger(__name__)

# API Endpoints
GAMMA_API_URL = "https://gamma-api.polymarket.com/events"
CLOB_API_URL = "https://clob.polymarket.com"


def fetch_order_book(token_id: str, max_retries: int = 3) -> Optional[Dict[str, Any]]:
    """
    Fetches order book data from Polymarket CLOB API.
    
    Args:
        token_id: The CLOB token ID for the market outcome.
        max_retries: Maximum number of retry attempts.
        
    Returns:
        Dictionary with bids, asks, and summary, or None if failed.
    """
    if not token_id:
        return None
    
    import time
    
    for attempt in range(max_retries):
        try:
            url = f"{CLOB_API_URL}/book"
            params = {"token_id": token_id}
            
            # Longer timeout, increases with retries
            timeout = 30 + (attempt * 10)
            
            response = requests.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            
            # Parse bids and asks
            bids = data.get('bids', [])
            asks = data.get('asks', [])
            
            # Sort Bids: Descending (Highest Price = Best Bid first)
            bids.sort(key=lambda x: float(x['price']), reverse=True)
            
            # Sort Asks: Ascending (Lowest Price = Best Ask first)
            asks.sort(key=lambda x: float(x['price']))
            
            # Calculate summary
            best_bid = float(bids[0]['price']) if bids else 0.0
            best_ask = float(asks[0]['price']) if asks else 1.0
            spread = best_ask - best_bid if best_bid and best_ask else None
            
            # Calculate depth (sum of sizes at top 3 levels)
            bid_depth = sum(float(b.get('size', 0)) for b in bids[:3])
            ask_depth = sum(float(a.get('size', 0)) for a in asks[:3])
            
            # Only calculate mid_price if spread is reasonable (< 20%)
            # Wide spreads make mid_price meaningless
            mid_price = None
            if spread and spread < 0.2:
                mid_price = (best_bid + best_ask) / 2
            
            return {
                "bids": bids[:5],  # Top 5 bids
                "asks": asks[:5],  # Top 5 asks
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": spread,
                "bid_depth_top3": bid_depth,
                "ask_depth_top3": ask_depth,
                "mid_price": mid_price  # Only set when spread is reasonable
            }
            
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                logger.debug(f"No order book for token {token_id}")
                return None  # No retry for 404
            elif e.response.status_code == 429:
                # Rate limited - wait longer
                wait_time = 5 * (2 ** attempt)
                logger.info(f"Rate limited. Waiting {wait_time}s...")
                time.sleep(wait_time)
            else:
                logger.warning(f"HTTP error fetching order book (attempt {attempt+1}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    
        except requests.exceptions.Timeout:
            logger.warning(f"Timeout fetching order book (attempt {attempt+1}/{max_retries})")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                
        except Exception as e:
            logger.warning(f"Failed to fetch order book for {token_id} (attempt {attempt+1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
    
    return None


def fetch_market_details(condition_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetches detailed market info from CLOB API.
    """
    try:
        url = f"{CLOB_API_URL}/markets/{condition_id}"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.debug(f"Could not fetch market details: {e}")
        return None


def fetch_full_market_description(market_id: str) -> str:
    """
    Fetches the FULL resolution rules (description) from Gamma API.
    The /events endpoint often truncates this.
    """
    try:
        # Note: using string replacement to switch from events/ markets/ base if needed, 
        # or just hardcode the gamma markets endpoint
        url = f"https://gamma-api.polymarket.com/markets/{market_id}"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        return data.get('description', '')
    except Exception as e:
        logger.warning(f"Failed to fetch full description for {market_id}: {e}")
        return ""


def fetch_trending_markets(limit: int = 5, min_volume: float = 0, fetch_obs: bool = True) -> List[Dict[str, Any]]:
    """
    Fetches trending events (grouped markets) from Polymarket Gamma API.
    
    Args:
        limit: Number of events to return.
        min_volume: Minimum trading volume to consider.
        
    Returns:
        List of event dictionaries with nested markets and order book data.
    """
    params = {
        "limit": 30,  # Fetch a larger batch to filter
        "active": "true",
        "closed": "false"
    }
    
    # Use ThreadPoolExecutor for parallel fetching
    import concurrent.futures
    
    events = []
    
    # Function to fetch order book for a single market (helper)
    def process_market(m):
        try:
            # Check closed status first
            if m.get('closed'):
                return None
                
            try:
                vol = float(m.get('volume', 0))
            except (ValueError, TypeError):
                vol = 0
            
            # Simple volume filter for efficiency
            if vol < min_volume:
                return None
                
            # Get CLOB token IDs
            clob_ids = m.get('clobTokenIds', [])
            if isinstance(clob_ids, str):
                try:
                    import json
                    clob_ids = json.loads(clob_ids)
                except:
                    clob_ids = []
            
            # Parse outcomes
            outcomes = m.get('outcomes', [])
            if isinstance(outcomes, str):
                try:
                    import json
                    outcomes = json.loads(outcomes)
                except:
                    outcomes = []

            # Parse outcome prices
            prices = m.get('outcomePrices')
            if isinstance(prices, str):
                try:
                    import json
                    prices = json.loads(prices)
                except:
                    pass
            
            # Fetch order books
            order_books = {}
            if fetch_obs and clob_ids:
                # logger.debug(f"Fetching order book for {len(clob_ids)} token IDs")
                for idx, token_id in enumerate(clob_ids[:2]):  # Max 2 outcomes (Yes/No)
                    if token_id:
                        ob = fetch_order_book(str(token_id))
                        if ob:
                            outcome_name = outcomes[idx] if idx < len(outcomes) else f"Outcome_{idx}"
                            order_books[outcome_name] = ob
            
            # Construct Market Data object
            return {
                "id": m.get("id"),
                "question": m.get("question"),
                "description": m.get("description", ""),
                "volume": vol,
                "clobTokenIds": clob_ids,
                "prices": prices,
                "outcomes": outcomes,
                "order_books": order_books,
                "end_date": m.get("endDate"),
                "liquidity": m.get("liquidity"),
            }
        except Exception as e:
            logger.debug(f"Error processing market {m.get('id')}: {e}")
            return None

    try:
        response = requests.get(GAMMA_API_URL, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        # Collect all active markets from events
        market_tasks = []
        event_map = {} # Map market_id -> event dict buffer
        
        filtered_events = []
        
        for event in data:
            event_markets = event.get('markets', [])
            if not event_markets:
                continue
                
            # Create a placeholder event object
            evt_obj = {
                "id": event.get("id"),
                "title": event.get("title"),
                "description": event.get("description", ""),
                "slug": event.get("slug"),
                "tags": event.get("tags", []),
                "markets": [], # Will fill later
                "total_volume": 0,
                "start_date": event.get("startDate"),
                "end_date": event.get("endDate"),
            }
            
            filtered_events.append(evt_obj)
            
            # Add markets to processing queue
            for m in event_markets:
                market_tasks.append((m, evt_obj))

        # Process markets in parallel
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            future_to_market = {executor.submit(process_market, m): evt_obj for m, evt_obj in market_tasks}
            
            for future in concurrent.futures.as_completed(future_to_market):
                evt_obj = future_to_market[future]
                try:
                    market_data = future.result()
                    if market_data:
                        evt_obj['markets'].append(market_data)
                        if market_data['volume'] > evt_obj['total_volume']:
                            evt_obj['total_volume'] = market_data['volume']
                except Exception as exc:
                    logger.error(f"Market fetch generated an exception: {exc}")

        # Filter out empty events and sort by volume
        final_events = [e for e in filtered_events if e['markets']]
        final_events.sort(key=lambda x: x['total_volume'], reverse=True)
        
        # Apply limit to final list
        final_events = final_events[:limit]
        
        if fetch_obs:
            logger.info(f"Fetched {len(final_events)} active events with order book data.")
        else:
            logger.info(f"Fetched {len(final_events)} active events without order book data.")
        return final_events
        
    except Exception as e:
        logger.error(f"Error fetching events: {e}")
        return []


def get_merged_order_book(yes_ob: Optional[Dict], no_ob: Optional[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """
    Merges YES and NO order books into a single unified view for YES token.
    
    Logic:
    - Merged Bids (Buy YES) = YES Bids + Inverted NO Asks
    - Merged Asks (Sell YES) = YES Asks + Inverted NO Bids
    
    Handles deduplication of mirrored liquidity by taking MAX size at each price level.
    
    Returns:
        Tuple of (merged_bids, merged_asks), both sorted (Bids descending, Asks ascending).
    """
    merged_bids = []  # To buy YES
    merged_asks = []  # To sell YES
    
    # helper to normalize price to tick size (0.1 cent = 0.001)
    def normalize_price(p):
        return round(float(p), 3)

    # 1. Collect all raw orders
    # YES bids -> Merged Bids
    if yes_ob:
        for b in yes_ob.get('bids', []):
            merged_bids.append({'price': normalize_price(b['price']), 'size': float(b['size'])})
            
    # NO asks -> Inverted -> Merged Bids
    if no_ob:
        for a in no_ob.get('asks', []):
            inv_p = normalize_price(1.0 - float(a['price']))
            if inv_p > 0:
                merged_bids.append({'price': inv_p, 'size': float(a['size'])})

    # YES asks -> Merged Asks
    if yes_ob:
        for a in yes_ob.get('asks', []):
            merged_asks.append({'price': normalize_price(a['price']), 'size': float(a['size'])})
            
    # NO bids -> Inverted -> Merged Asks
    if no_ob:
        for b in no_ob.get('bids', []):
            inv_p = normalize_price(1.0 - float(b['price']))
            if inv_p < 1:
                merged_asks.append({'price': inv_p, 'size': float(b['size'])})

    # 2. Aggregate and Deduplicate (Max size for same price)
    def aggregate(orders):
        agg = {}
        for o in orders:
            p = o['price']
            if p in agg:
                agg[p] = max(agg[p], o['size'])
            else:
                agg[p] = o['size']
        return [{'price': p, 'size': s} for p, s in agg.items()]

    merged_bids = aggregate(merged_bids)
    merged_asks = aggregate(merged_asks)

    # 3. Sort
    # Bids: Highest price first (Best Bid)
    merged_bids.sort(key=lambda x: x['price'], reverse=True)
    # Asks: Lowest price first (Best Ask)
    merged_asks.sort(key=lambda x: x['price'])
    
    return merged_bids, merged_asks


def format_order_book_display(order_books: Dict[str, Any], actual_prices: list = None, top_n: int = 8) -> str:
    """
    Formats order book as MERGED view matching Polymarket UI.
    
    Polymarket combines:
    - YES Bids + inverted NO Asks = Merged Bids (to buy YES)
    - YES Asks + inverted NO Bids = Merged Asks (to sell YES)
    
    This creates tight spreads by merging liquidity from both sides.
    """
    if not order_books:
        return "  [No order book data available]"
    
    # Get YES and NO order books
    yes_ob = order_books.get('Yes') or order_books.get('yes')
    no_ob = order_books.get('No') or order_books.get('no')
    
    if not yes_ob and not no_ob:
        outcomes = list(order_books.keys())
        if len(outcomes) >= 1:
            yes_ob = order_books[outcomes[0]]
        if len(outcomes) >= 2:
            no_ob = order_books[outcomes[1]]
    
    # Get actual market prices
    yes_price = float(actual_prices[0]) if actual_prices and len(actual_prices) >= 1 else None
    
    # Generate Merged Book
    merged_bids, merged_asks = get_merged_order_book(yes_ob, no_ob)
    

    
    # Calculate best prices
    best_bid = merged_bids[0]['price'] if merged_bids else 0
    best_ask = merged_asks[0]['price'] if merged_asks else 1
    spread = best_ask - best_bid
    
    lines = []
    
    # Header
    yes_pct = f"{yes_price*100:.0f}%" if yes_price else "?"
    lines.append(f"  =============== Trade YES ({yes_pct}) ===============")
    lines.append(f"  Buy Yes: {best_ask*100:.1f}c  |  Sell Yes: {best_bid*100:.1f}c  |  Spread: {spread*100:.1f}c")
    lines.append("")
    
    # ASKS section
    lines.append("  ------- ASKS (Sell YES) -------")
    asks_reversed = list(reversed(merged_asks[:top_n]))
    for a in asks_reversed:
        lines.append(f"    {a['price']*100:5.1f}c  x  {a['size']:>10,.0f}")
    
    # Spread line
    lines.append(f"  --- Last: {yes_price*100:.1f}c | Spread: {spread*100:.1f}c ---" if yes_price else f"  --- Spread: {spread*100:.1f}c ---")
    
    # BIDS section
    lines.append("  ------- BIDS (Buy YES) --------")
    for b in merged_bids[:top_n]:
        lines.append(f"    {b['price']*100:5.1f}c  x  {b['size']:>10,.0f}")
    
    # Depth summary
    bid_depth = sum(b['size'] for b in merged_bids[:5])
    ask_depth = sum(a['size'] for a in merged_asks[:5])
    lines.append(f"  ---------------------------------")
    lines.append(f"  Depth (5 levels): Bid {bid_depth:,.0f} / Ask {ask_depth:,.0f}")
    
    return '\n'.join(lines)


def format_order_book_for_ai(order_books: Dict[str, Any]) -> str:
    """
    Formats order book data for AI prompt consumption.
    """
    if not order_books:
        return "No order book data available."
    
    lines = []
    for outcome, ob in order_books.items():
        lines.append(f"Outcome: {outcome}")
        lines.append(f"  - Best Bid: {ob['best_bid']:.4f}, Best Ask: {ob['best_ask']:.4f}")
        if ob['spread']:
            lines.append(f"  - Spread: {ob['spread']:.4f} ({ob['spread']*100:.2f}%)")
        if ob['mid_price']:
            lines.append(f"  - Mid Price (implied probability): {ob['mid_price']:.4f}")
        lines.append(f"  - Bid liquidity (top 3): ${ob['bid_depth_top3']:.2f}")
        lines.append(f"  - Ask liquidity (top 3): ${ob['ask_depth_top3']:.2f}")
    
    return '\n'.join(lines)


if __name__ == "__main__":
    # Test
    print("Fetching trending markets with order book data...")
    trending = fetch_trending_markets(limit=2)
    for e in trending:
        print(f"\nEvent: {e['title']}")
        print(f"Open Markets: {len(e['markets'])}")
        for m in e['markets']:
            print(f"\n  Market: {m['question']}")
            print(format_order_book_display(m.get('order_books', {})))
