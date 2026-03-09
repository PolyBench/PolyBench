"""
News Fetcher — Google News RSS + async URL decoding + content scraping.

Uses google_news_api.AsyncGoogleNewsClient for reliable URL decoding
(mirrors the working pipeline from test_full_pipeline_2.py).

Primary API:
    async_fetch_news_for_query(query, limit=5, top_k=3)  -> str
    fetch_news_for_query(query, limit=5, top_k=3)        -> str  (sync wrapper)

Both return a formatted news_context string ready for AI analysis.
"""

import asyncio
import feedparser
import urllib.parse
import logging
import requests
from bs4 import BeautifulSoup
from google_news_api import AsyncGoogleNewsClient

logger = logging.getLogger(__name__)

# ─── Filtering ─────────────────────────────────────────────────────────

# Words in page content that indicate a junk/blocked page
FILTER_WORDS = [
    "enable js", "javascript is not available", "ad blocker",
    "enable javascript", "please subscribe", "captcha",
    "you need to enable javascript", "turn off your ad blocker",
]

# Prediction-market sites/keywords to exclude from results
PREDICTION_MARKET_EXCLUSIONS = [
    "polymarket", "kalshi", "predictit", "metaculus", "manifold",
    "predict.org", "insight prediction", "futuur", "hedgehog",
]

# RSS query suffix to filter out prediction market content
_PM_EXCLUSION_QUERY = " ".join(
    f"-{name}" for name in ["polymarket", "kalshi", "predictit", "metaculus", "manifold"]
)

# User agent for scraping
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

MAX_CONTENT_LEN = 4000  # Truncate individual articles at this length


# ─── Content Scraping ──────────────────────────────────────────────────

def scrape_content(url: str) -> str:
    """
    Fetches and parses text content from a URL.
    Uses multiple strategies (article tag → main tag → class patterns → <p> fallback).
    Returns text content or an error string starting with '['.
    """
    if not url:
        return "[No URL provided]"

    try:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        response = requests.get(url, headers=headers, timeout=12, allow_redirects=True)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, "html.parser")

        # Remove non-content elements
        for element in soup(["script", "style", "nav", "footer", "header", "aside",
                             "noscript", "iframe", "form"]):
            element.extract()

        text = ""

        # Strategy 1: <article> tag
        article = soup.find("article")
        if article:
            text = article.get_text(separator="\n", strip=True)

        # Strategy 2: <main> or role="main"
        if len(text) < 200:
            main = soup.find("main") or soup.find(attrs={"role": "main"})
            if main:
                candidate = main.get_text(separator="\n", strip=True)
                if len(candidate) > len(text):
                    text = candidate

        # Strategy 3: Common content class names
        if len(text) < 200:
            for class_pattern in ["article", "content", "post", "entry", "story", "body"]:
                content_div = soup.find(
                    attrs={"class": lambda x: x and class_pattern in str(x).lower()}
                )
                if content_div:
                    candidate = content_div.get_text(separator="\n", strip=True)
                    if len(candidate) > len(text):
                        text = candidate
                        if len(text) > 200:
                            break

        # Strategy 4: All <p> tags
        if len(text) < 200:
            paragraphs = soup.find_all("p")
            para_text = "\n".join(
                p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 30
            )
            if len(para_text) > len(text):
                text = para_text

        # Clean up lines
        lines = [line.strip() for line in text.splitlines() if line.strip() and len(line.strip()) > 3]
        final_text = "\n".join(lines)

        # Check filter words (junk page detection)
        lower_text = final_text.lower()
        for word in FILTER_WORDS:
            if word in lower_text:
                return f"[FILTERED] Content blocked by keyword: '{word}'"

        # Check prediction market content
        for pm in PREDICTION_MARKET_EXCLUSIONS:
            if pm in lower_text:
                return f"[FILTERED] Prediction market content: '{pm}'"

        if len(final_text) < 50:
            return f"[Too short] Only {len(final_text)} chars from {url[:60]}"

        # Truncate
        if len(final_text) > MAX_CONTENT_LEN:
            final_text = final_text[:MAX_CONTENT_LEN] + "... [truncated]"

        return final_text

    except requests.exceptions.Timeout:
        return f"[Timeout] {url[:60]}"
    except requests.exceptions.HTTPError as e:
        return f"[HTTP {e.response.status_code}] {url[:60]}"
    except Exception as e:
        return f"[Scrape Error] {type(e).__name__}: {e}"


def _is_error(content: str) -> bool:
    """Check if content is an error/filtered string rather than real text."""
    return content.startswith("[")


# ─── Single-article async pipeline ─────────────────────────────────────

async def _process_article(client, article, index: int) -> dict:
    """Decode Google News URL and scrape content for one RSS entry."""
    raw_url = article.link
    title = article.title

    # Decode Google News redirect URL
    try:
        decoded_url = await client.decode_url(raw_url)
    except Exception as e:
        logger.warning(f"URL decode failed for article {index}: {e}")
        return {
            "index": index, "title": title,
            "content": f"[Decode Error] {e}",
            "length": 0, "url": raw_url,
        }

    # Title-level prediction market filter
    title_lower = title.lower()
    for pm in PREDICTION_MARKET_EXCLUSIONS:
        if pm in title_lower:
            return {
                "index": index, "title": title,
                "content": f"[FILTERED] Prediction market in title: '{pm}'",
                "length": 0, "url": decoded_url,
            }

    # Scrape content in a thread (requests is blocking)
    content = await asyncio.to_thread(scrape_content, decoded_url)
    length = len(content) if not _is_error(content) else 0

    return {
        "index": index, "title": title,
        "content": content, "length": length,
        "url": decoded_url,
    }


# ─── Main public API ──────────────────────────────────────────────────

async def async_fetch_news_for_query(
    query: str, limit: int = 5, top_k: int = 3,
    client: AsyncGoogleNewsClient | None = None,
    before_date: str | None = None,
) -> str:
    """
    Fetch news articles for a query, keep the top_k longest.

    1. Fetch RSS feed from Google News
    2. For each article: decode URL + scrape content (concurrently)
    3. Filter out errors/junk, sort by length desc, keep top_k
    4. Return formatted news_context string

    Args:
        query:       Search query (e.g. market question or event title)
        limit:       Number of RSS entries to fetch and try
        top_k:       Number of longest articles to keep in final output
        client:      Optional shared AsyncGoogleNewsClient (avoids creating one per call)
        before_date: Optional date string (YYYY-MM-DD). Only return news before this date.

    Returns:
        Formatted news context string (may be empty string if nothing found)
    """
    # Build RSS URL with prediction-market exclusions and optional date filter
    full_query = f"{query} {_PM_EXCLUSION_QUERY}"
    if before_date:
        full_query += f" before:{before_date}"
    encoded_query = urllib.parse.quote(full_query)
    rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-US&gl=US&ceid=US:en"

    logger.info(f"Fetching news for: {query}")

    # 1. Parse RSS feed (in thread — feedparser does HTTP and blocks)
    feed = await asyncio.to_thread(feedparser.parse, rss_url)
    entries = feed.entries[:limit]

    if not entries:
        logger.info(f"No news entries found for: {query}")
        return ""

    logger.info(f"Found {len(entries)} RSS entries, decoding & scraping...")

    # 2. Decode + scrape all entries concurrently
    # Use shared client if provided, otherwise create a temporary one
    async def _run_with_client(c):
        tasks = [
            _process_article(c, entry, i + 1)
            for i, entry in enumerate(entries)
        ]
        return await asyncio.gather(*tasks)

    if client:
        results = await _run_with_client(client)
    else:
        async with AsyncGoogleNewsClient(requests_per_minute=120) as new_client:
            results = await _run_with_client(new_client)

    # 3. Sort by length descending, take top_k
    sorted_results = sorted(results, key=lambda x: x["length"], reverse=True)
    best = [r for r in sorted_results if r["length"] > 0][:top_k]

    if not best:
        logger.warning(f"No usable articles found for: {query}")
        return ""

    # 4. Format output
    parts = []
    for item in best:
        parts.append(
            f"\n\n--- News Article {item['index']}: {item['title']} ---\n"
            f"{item['content'][:2000]}\n"
        )

    logger.info(f"Returning {len(best)} articles for: {query}")
    return "".join(parts)





# ─── CLI test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("Testing async news fetch...")
    result = asyncio.run(async_fetch_news_for_query("Bitcoin cryptocurrency", limit=5, top_k=3))
    print(f"\nResult length: {len(result)} chars")
    print(result[:2000] if result else "(no results)")
