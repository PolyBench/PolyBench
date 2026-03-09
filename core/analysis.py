import logging
import json
import re
import sys
from config import (
    OPENROUTER_API_KEY, OPENROUTER_MODEL, OPENROUTER_PROVIDER_ORDER,
    DEBUG_MODE, VERBOSE_MODE, NEWS_LIMIT
)
from core.news_fetcher import async_fetch_news_for_query, scrape_content
from google_news_api.client import AsyncGoogleNewsClient
import asyncio
from core.market_data import format_order_book_for_ai

logger = logging.getLogger(__name__)


def vprint(*args, **kwargs):
    """Print with flush for verbose output."""
    if VERBOSE_MODE:
        print(*args, **kwargs)
        sys.stdout.flush()





def extract_urls(text):
    """Finds URLs in text."""
    url_pattern = re.compile(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+')
    return url_pattern.findall(text)


def analyze_market_prediction(event_data):
    """
    Analyzes an event using Multi-Provider Strategy (Google -> Xiaomi).
    Includes order book data for better market analysis.
    """
    title = event_data.get('title')
    description = event_data.get('description', "")
    markets = event_data.get('markets', [])
    
    vprint("\n" + "~"*70)
    vprint("📰 GATHERING NEWS & CONTEXT...")
    vprint("~"*70)
    
    # 1. Gather Context
    # A. Scrape links from description
    scraped_content = ""
    found_urls = extract_urls(description)
    if found_urls:
        logger.info(f"Found {len(found_urls)} source URLs. Scraping...")
        vprint(f"  Found {len(found_urls)} source URLs in description")
        for url in found_urls[:2]: 
            text = scrape_content(url)
            if text and not text.startswith("[Failed"):
                scraped_content += f"\n--- Source Content ({url[:50]}...) ---\n{text}\n"
                vprint(f"  ✓ Scraped {len(text)} chars from: {url[:60]}...")

    # B. Fetch News Context
    
    deep_news_context = ""
    successful_scrapes = 0

    # Check if news_context was already passed in via the first market's data
    pre_fetched_news = markets[0].get('news_context') if markets else None

    if pre_fetched_news and len(pre_fetched_news.strip()) > 10:
        news_context = pre_fetched_news
        logger.info(f"Using pre-fetched news from database for: {title}")
        vprint(f"  ✓ Using precisely pre-fetched news context from DB")
    else:
        logger.info(f"Fetching news for event: {title}")
        # Execute async fetch with shared client logic as done in update_snapshots
        async def _fetch_news():
            async with AsyncGoogleNewsClient(requests_per_minute=1000) as client:
                return await async_fetch_news_for_query(title, limit=NEWS_LIMIT, top_k=3, client=client)
    
        news_context = asyncio.run(_fetch_news())
    
    if news_context:
        deep_news_context += news_context
        successful_scrapes = news_context.count("--- News Article")
        vprint(f"  ✓ Successfully loaded news context")
    else:
        vprint("  ⚠ No news articles found")

    rss_summary = "No recent news found." if not news_context else "News context aggregated above."
    
    vprint(f"  📊 Summary: loaded {successful_scrapes} articles")
    total_context = len(scraped_content) + len(deep_news_context)
    vprint(f"  📊 Total context size: {total_context:,} chars")
    
    full_news_context = f"{scraped_content}\n{deep_news_context}\n\n--- Recent Headlines ---\n{rss_summary}"

    # C. Format Markets with Order Book Data
    markets_context = ""
    for m in markets:
        m_id = str(m.get('id'))
        prices_str = str(m.get('prices'))
        
        markets_context += f"\n## Option ID: {m_id}\n"
        markets_context += f"Question: {m['question']}\n"
        markets_context += f"Current Prices/Probabilities: {prices_str}\n"
        
        # Add order book data if available
        order_books = m.get('order_books', {})
        if order_books:
            markets_context += "\nOrder Book Analysis:\n"
            markets_context += format_order_book_for_ai(order_books)
            markets_context += "\n"
        
        # Add other market metadata
        if m.get('volume'):
            markets_context += f"Trading Volume: ${m['volume']:,.2f}\n"
        if m.get('liquidity'):
            markets_context += f"Liquidity: ${m['liquidity']}\n"

    # 2. Construct Prompt
    import datetime
    current_time_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M UTC")

    system_prompt = """You are an expert prediction market trader using Bayesian reasoning.
Your goal is to identify mispriced markets based on provided evidence and order book data.

RULES:
1. You may select MULTIPLE Option IDs if you identify multiple mispriced assets.
2. If NO option offers good value (EV+), return an empty list for "decisions".
3. STRICT JSON OUTPUT ONLY. No markdown, no text.
4. Confidence > 0.8 requires DIRECT quotes/stats from the context.
5. If the resolution source (Source Content) gives a definitive answer, trade with high confidence.
6. Consider ORDER BOOK data: wide spreads indicate uncertainty, large bid/ask imbalances may signal informed trading.
7. If mid_price differs significantly from the stated probability, investigate further.
8. CRITICAL: PAY CLOSE ATTENTION to the 'Description & Rules' field. It contains the official Resolution Rules. These are strict conditions (dates, definitions) that determine the outcome. Ignore general knowledge if it conflicts with these specific rules.
9. CHECK FOR "50-50" or "SPLIT" RESOLUTION: If the rules state the market resolves 50-50 if a condition isn't met by a date, this sets a price floor/ceiling. If the deadline is likely to be reached without the event, the true value is 0.50, NOT 0.0 or 1.0. Factor this heavily into your EV calculation.
10. DATE AWARENESS: Compare the 'Current Date' provided in the prompt against any dates/deadlines in the Rules.
11. ESTIMATE RESOLUTION: Provide an 'est_resolution_date' (YYYY-MM-DD) based on the event name, description or typical resolution timelines. This is crucial for calculating annualized yield.
"""

    user_prompt = f"""
Current Date: {current_time_str}
Event: {title}
Description & Rules: {description}

=== MARKET OPTIONS WITH LIVE ORDER BOOK DATA ===
{markets_context}

=== EVIDENCE & CONTEXT ({successful_scrapes} NEWS ARTICLES) ===
{full_news_context}

TASK:
Analyze the evidence AND order book data. Identify ALL options that are mispriced.
The current prices imply probabilities. Order book mid-prices show actual market state.
Signal a trade for ANY option where your estimated probability differs significantly from price.

Output JSON:
{{
    "decisions": [
        {{
            "decision": "<Option ID>",
            "outcome": "<Exact Outcome Name>",
            "strategy": ["value_bet", "news_catalyst", "arbitrage", "stable_yield"], 
            "est_resolution_date": "YYYY-MM-DD" or null,
            "confidence": <float 0.0-1.0>,
            "reasoning": "<concise explanation>"
        }},
        ... (more trades if applicable)
    ]
}}
"""

    # 3. Provider Logic
    from config import PRIMARY_PROVIDER
    if DEBUG_MODE:
        print("\n" + "!"*30 + " DEBUG MODE " + "!"*30)
        print(f"Primary Provider: {PRIMARY_PROVIDER}")
        print("--- System Prompt ---")
        print(system_prompt)
        print("--- User Prompt (Full) ---")
        print(user_prompt)
        print("!"*72 + "\n")
    else:
        vprint("\n" + "~"*70)
        vprint("🤖 SENDING TO AI FOR ANALYSIS...")
        vprint("~"*70)
        vprint(f"  Prompt size: {len(user_prompt):,} chars")
        vprint(f"  News articles in prompt: {successful_scrapes}")
        vprint(f"  Order books in prompt: {sum(1 for m in markets if m.get('order_books'))}")
    
    
    analysis_result = {
        "decision": "SKIP", 
        "reasoning": "Analysis failed.",
        "news_context": full_news_context[:500] + "..." 
    }



    def try_openrouter():
        if not OPENROUTER_API_KEY: 
            return None
        try:
            vprint(f"  Trying OpenRouter ({OPENROUTER_MODEL})...")
            
            model_name = OPENROUTER_MODEL
            
            # Strip openrouter/ prefix if using raw requests
            if model_name.startswith("openrouter/"):
                model_name = model_name[11:]
                
            headers = {
              'Authorization': f'Bearer {OPENROUTER_API_KEY}',
              'HTTP-Referer': 'http://localhost:8000', # required by OpenRouter
              'X-Title': 'Polymarket Analysis Bot',
              'Content-Type': 'application/json',
            }
            
            import requests
            
            payload = {
                'model': model_name,
                'messages': [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ]
            }
            
            if OPENROUTER_PROVIDER_ORDER:
                payload['provider'] = {
                    'order': [OPENROUTER_PROVIDER_ORDER],
                    'allow_fallbacks': False,
                }
                
            response = requests.post(
                'https://openrouter.ai/api/v1/chat/completions', 
                headers=headers, 
                json=payload
            )
            
            response.raise_for_status()
            response_data = response.json()
            content = response_data['choices'][0]['message']['content']
            
            # Log provider info for debugging
            provider_info = response_data.get('provider', 'Unknown')
            vprint(f"  ✓ OpenRouter Response. Provider used: {provider_info}")

            if DEBUG_MODE: 
                print(f"--- RAW OPENROUTER RESPONSE ---\n{content}\n---------------------------")
            
            content = content.replace("```json", "").replace("```", "").strip()
            
            # Intelligent JSON extraction (bypasses <think> tags from reasoning models)
            import re
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if json_match:
                content = json_match.group(0)
                
            data = json.loads(content)
            data['provider'] = f"OPENROUTER ({OPENROUTER_MODEL} via {OPENROUTER_PROVIDER_ORDER})"
            
            return data
        except json.JSONDecodeError as e:
            err_msg = f"JSON Parsing Error: {e}\n\nRaw LLM Output Snippet:\n{content[:800]}"
            logger.error(err_msg)
            vprint(f"  ✗ OpenRouter JSON parsing failed.")
            return {
                "decision": "ERROR",
                "decisions": [],
                "reasoning": err_msg,
                "error": "JSON_PARSE_ERROR",
                "provider": f"OPENROUTER ({OPENROUTER_MODEL} via {OPENROUTER_PROVIDER_ORDER})"
            }
        except requests.exceptions.RequestException as e:
            if hasattr(e, 'response') and e.response is not None:
                err_text = e.response.text
                logger.error(f"OpenRouter HTTP Error: {e}\nResponse: {err_text}")
                vprint(f"  ✗ OpenRouter failed: HTTP {e.response.status_code} - {err_text}")
                err_msg = f"HTTP {e.response.status_code}: {err_text}"
            else:
                logger.error(f"OpenRouter network error: {e}")
                vprint(f"  ✗ OpenRouter failed: {e}")
                err_msg = f"Network Error: {e}"
            return {
                "decision": "ERROR",
                "decisions": [],
                "reasoning": f"RequestException: {err_msg}",
                "error": "NETWORK_ERROR",
                "provider": f"OPENROUTER ({OPENROUTER_MODEL} via {OPENROUTER_PROVIDER_ORDER})"
            }
        except Exception as e:
            logger.error(f"OpenRouter failed: {e}")
            vprint(f"  ✗ OpenRouter failed: {e}")
            import traceback
            tb = traceback.format_exc()
            return {
                "decision": "ERROR",
                "decisions": [],
                "reasoning": f"Exception: {e}\nTraceback:\n{tb}",
                "error": "SYSTEM_ERROR",
                "provider": f"OPENROUTER ({OPENROUTER_MODEL} via {OPENROUTER_PROVIDER_ORDER})"
            }

    # Dispatch based on Config
    data = None
    provider_error = None
    
    if PRIMARY_PROVIDER == "OPENROUTER":
        data = try_openrouter()
        if not data:
            provider_error = "OPENROUTER_FAILED"
    else:
        raise ValueError(f"Unknown PRIMARY_PROVIDER: {PRIMARY_PROVIDER}")
    
    # If provider failed, return error result for storage/retry
    if provider_error:
        provider_label = PRIMARY_PROVIDER
        if PRIMARY_PROVIDER == "OPENROUTER":
            provider_label = f"OPENROUTER ({OPENROUTER_MODEL} via {OPENROUTER_PROVIDER_ORDER})"
            
        return {
            "decision": "ERROR",
            "decisions": [],
            "reasoning": f"Provider failed: {provider_error}",
            "provider": provider_label,
            "error": provider_error,
            "news_context": full_news_context,
            "prompt_used": user_prompt
        }

    if data:
        # Standardize Output: Convert old single-decision format to new list format
        if 'decisions' not in data:
            # Legacy/Fallback handling
            if data.get('decision') and data.get('decision') != "SKIP":
                data['decisions'] = [data] # Wrap as single item list
            else:
                data['decisions'] = []
        
        # Low confidence logic per decision
        valid_decisions = []
        for d in data.get('decisions', []):
            conf = d.get('confidence', 0.0)
            if conf < 0.6:
                logger.warning(f"Confidence {conf} too low for {d.get('decision')}. SKIP.")
                continue
            valid_decisions.append(d)
        
        data['decisions'] = valid_decisions
             
        data['news_context'] = full_news_context 
        data['news_count'] = successful_scrapes
        data['prompt_used'] = user_prompt
        return data

    return analysis_result


def analyze_backtest_prediction(event_data):
    """
    Analyzes a closed market using A HISTORICAL SNAPSHOT to test AI accuracy.
    Uses pre-fetched news context and order book data instead of live data.
    """
    title = event_data.get('title')
    description = event_data.get('description', "")
    markets = event_data.get('markets', [])
    
    # We expect these to be passed in from the DB snapshots
    pre_fetched_news = event_data.get('news_context', "No historical news context provided.")
    snapshot_time = event_data.get('timestamp', "Unknown Date")
    
    vprint("\n" + "~"*70)
    vprint(f"🕰️ RUNNING BACKTEST ANALYSIS (Snapshot: {snapshot_time})")
    vprint("~"*70)

    # C. Format Markets with Order Book Data
    markets_context = ""
    for m in markets:
        m_id = str(m.get('id'))
        prices_str = str(m.get('prices'))
        
        markets_context += f"\n## Option ID: {m_id}\n"
        markets_context += f"Question: {m['question']}\n"
        markets_context += f"Last Known Prices: {prices_str}\n"
        
        order_books = m.get('order_books', {})
        if order_books:
            markets_context += "\nHistorical Order Book Analysis:\n"
            markets_context += format_order_book_for_ai(order_books)
            markets_context += "\n"
        
        if m.get('volume'):
            markets_context += f"Final Trading Volume: ${m['volume']:,.2f}\n"

    # 2. Construct Backtest Prompt
    system_prompt = """You are an expert prediction market trader using Bayesian reasoning.
Your goal is to identify mispriced markets based on provided evidence and order book data.

RULES:
1. You may select MULTIPLE Option IDs if you identify multiple mispriced assets.
2. If NO option offers good value (EV+), return an empty list for "decisions".
3. STRICT JSON OUTPUT ONLY. No markdown, no text.
4. Confidence > 0.8 requires DIRECT quotes/stats from the context.
5. If the resolution source (Source Content) gives a definitive answer, trade with high confidence.
6. Consider ORDER BOOK data: wide spreads indicate uncertainty, large bid/ask imbalances may signal informed trading.
7. If mid_price differs significantly from the stated probability, investigate further.
8. CRITICAL: PAY CLOSE ATTENTION to the 'Description & Rules' field. It contains the official Resolution Rules. These are strict conditions (dates, definitions) that determine the outcome. Ignore general knowledge if it conflicts with these specific rules.
9. CHECK FOR "50-50" or "SPLIT" RESOLUTION: If the rules state the market resolves 50-50 if a condition isn't met by a date, this sets a price floor/ceiling. If the deadline is likely to be reached without the event, the true value is 0.50, NOT 0.0 or 1.0. Factor this heavily into your EV calculation.
10. DATE AWARENESS: Compare the 'Current Date' provided in the prompt against any dates/deadlines in the Rules.
11. ESTIMATE RESOLUTION: Provide an 'est_resolution_date' (YYYY-MM-DD) based on the event name, description or typical resolution timelines. This is crucial for calculating annualized yield.

NOTE: This is a historical evaluation. The 'Current Date' provided represents the time the market snapshot was taken. Make your decisions as if you were trading precisely at that moment.
"""

    user_prompt = f"""
Current Date: {snapshot_time}
Event: {title}
Description & Rules: {description}

=== MARKET OPTIONS WITH HISTORICAL ORDER BOOK DATA ===
{markets_context}

=== EVIDENCE & CONTEXT AT SNAPSHOT TIME ===
{pre_fetched_news}

TASK:
Analyze the historical evidence AND historical order book data. Identify ALL options that are mispriced.
The stated prices imply probabilities. Order book mid-prices show actual market state.
Signal a trade for ANY option where your estimated probability differs significantly from price.

Output JSON:
{{
    "reasoning": "<Explanation of your overall market view, and why you are skipping if decisions is empty.>",
    "decisions": [
        {{
            "decision": "<Option ID>",
            "outcome": "<Exact Outcome Name>",
            "strategy": ["value_bet", "news_catalyst", "arbitrage", "stable_yield"], 
            "est_resolution_date": "YYYY-MM-DD" or null,
            "confidence": <float 0.0-1.0>,
            "reasoning": "<concise explanation>"
        }},
        ... (more trades if applicable)
    ]
}}
"""

    # 3. Provider Logic
    from config import PRIMARY_PROVIDER
    if DEBUG_MODE:
        print("\n" + "!"*30 + " BACKTEST DEBUG " + "!"*30)
        print("--- System Prompt ---")
        print(system_prompt)
        print("--- User Prompt ---")
        print(user_prompt[:1500] + "...\n[CONTINUED]")
        print("!"*74 + "\n")
        
    analysis_result = {
        "decision": "ERROR", 
        "decisions": [],
        "reasoning": "Analysis failed.",
        "news_context": pre_fetched_news[:500] + "...",
        "prompt_used": user_prompt
    }



    def try_openrouter():
        if not OPENROUTER_API_KEY: return None
        try:
            vprint(f"  Trying OpenRouter ({OPENROUTER_MODEL})...")
            model_name = OPENROUTER_MODEL
            
            # Strip openrouter/ prefix if using raw requests
            if model_name.startswith("openrouter/"):
                model_name = model_name[11:]
                
            import requests
            headers = {
              'Authorization': f'Bearer {OPENROUTER_API_KEY}',
              'HTTP-Referer': 'http://localhost:8000',
              'Content-Type': 'application/json',
            }
            
            payload = {
                'model': model_name,
                'messages': [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ]
            }
            
            if OPENROUTER_PROVIDER_ORDER:
                payload['provider'] = {
                    'order': OPENROUTER_PROVIDER_ORDER.split(',') if isinstance(OPENROUTER_PROVIDER_ORDER, str) else OPENROUTER_PROVIDER_ORDER,
                    'allow_fallbacks': False,
                }
                
            response = requests.post(
                url='https://openrouter.ai/api/v1/chat/completions', 
                headers=headers, 
                json=payload
            )
            
            response.raise_for_status()
            response_data = response.json()
            content = response_data['choices'][0]['message']['content']
            
            # Log provider info
            provider_info = response_data.get('provider', 'Unknown')
            vprint(f"  ✓ OpenRouter Response. Provider used: {provider_info}")
            
            content = content.replace("```json", "").replace("```", "").strip()
            
            # Intelligent JSON extraction (bypasses <think> tags from reasoning models)
            import re
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if json_match:
                content = json_match.group(0)
                
            data = json.loads(content)
            data['provider'] = f"OPENROUTER ({OPENROUTER_MODEL} via {OPENROUTER_PROVIDER_ORDER})"
            return data
        except json.JSONDecodeError as e:
            err_msg = f"JSON Parsing Error: {e}\n\nRaw LLM Output Snippet:\n{content[:800]}"
            print(f"  [!] OpenRouter JSON JSONDecodeError: {e}")
            return {
                "decision": "ERROR",
                "decisions": [],
                "reasoning": err_msg,
                "error": "JSON_PARSE_ERROR",
                "provider": f"OPENROUTER ({OPENROUTER_MODEL} via {OPENROUTER_PROVIDER_ORDER})"
            }
        except requests.exceptions.RequestException as e:
            if hasattr(e, 'response') and e.response is not None:
                err_text = e.response.text
                print(f"  [!] OpenRouter HTTP Error {e.response.status_code}: {err_text}")
                err_msg = f"HTTP {e.response.status_code}: {err_text}"
            else:
                print(f"  [!] OpenRouter network error: {e}")
                err_msg = f"Network Error: {e}"
            return {
                "decision": "ERROR",
                "decisions": [],
                "reasoning": f"RequestException: {err_msg}",
                "error": "NETWORK_ERROR",
                "provider": f"OPENROUTER ({OPENROUTER_MODEL} via {OPENROUTER_PROVIDER_ORDER})"
            }
        except Exception as e:
            print(f"  [!] OpenRouter failed: {e}")
            import traceback
            tb = traceback.format_exc()
            return {
                "decision": "ERROR",
                "decisions": [],
                "reasoning": f"Exception: {e}\nTraceback:\n{tb}",
                "error": "SYSTEM_ERROR",
                "provider": f"OPENROUTER ({OPENROUTER_MODEL} via {OPENROUTER_PROVIDER_ORDER})"
            }

    data = None
    provider_error = None
    
    if PRIMARY_PROVIDER == "OPENROUTER":
        data = try_openrouter()
        if not data:
            provider_error = "OPENROUTER_FAILED"
    else:
        raise ValueError(f"Unknown PRIMARY_PROVIDER: {PRIMARY_PROVIDER}")
    
    if provider_error:
        provider_label = f"{PRIMARY_PROVIDER}"
        if PRIMARY_PROVIDER == "OPENROUTER":
            provider_label = f"OPENROUTER ({OPENROUTER_MODEL} via {OPENROUTER_PROVIDER_ORDER})"
            
        analysis_result["reasoning"] = f"Provider failed: {provider_error}"
        analysis_result["error"] = provider_error
        analysis_result["provider"] = provider_label
        return analysis_result

    if data:
        data['news_context'] = pre_fetched_news 
        data['prompt_used'] = user_prompt
        
        # Standardize Output: Convert old single-decision format to new list format
        if 'decisions' not in data:
            if data.get('decision') and data.get('decision') != "SKIP":
                data['decisions'] = [data]
            else:
                data['decisions'] = []
                
        # Confidence filtering
        valid_decisions = []
        for d in data.get('decisions', []):
            conf = d.get('confidence', 0.0)
            if conf < 0.6:
                logger.warning(f"Confidence {conf} too low for {d.get('decision')}. SKIP.")
                continue
            valid_decisions.append(d)
            
        data['decisions'] = valid_decisions
        return data

    return analysis_result

