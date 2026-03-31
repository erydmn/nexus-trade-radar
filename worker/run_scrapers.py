import sys
from pathlib import Path

# Add project root to sys.path
root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

import logging
import httpx
import asyncio
from datetime import datetime, timezone
from core.config import settings
from worker.models.signal import RawEvent
from worker.comtrade_service import run_comtrade_pipeline
from worker.truth_engine import TruthEngine
from worker.scrapers.advanced_news_scraper import fetch_gdelt_doc, fetch_event_registry, fetch_turkish_rss

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ── Source key mapping ────────────────────────────────────────────────────────
# Maps RawEvent.source prefixes to TruthEngine source keys for cross-verification
SOURCE_KEY_MAP = {
    "GDELT":         "gdelt",
    "EventRegistry": "newsapi",       # EventRegistry ≈ news aggregator tier
    "NewsAPI":       "newsapi",
    "The Guardian":  "newsapi",
    "Reuters":       "reuters_rss",
    "AP":            "ap_rss",
    "BBC":           "bbc_business_rss",
    "Lloyd's List":  "lloyds_list_rss",
    "TradeWinds":    "tradewinds_rss",
    "RSS":           "reuters_rss",    # Generic RSS fallback
}


def _classify_source(source_str: str) -> str:
    """Map a RawEvent.source string to a TruthEngine source key."""
    for prefix, key in SOURCE_KEY_MAP.items():
        if source_str.startswith(prefix):
            return key
    return "newsapi"  # conservative default


def _raw_event_to_truth_dict(ev: RawEvent) -> dict:
    """Convert a RawEvent to the dict format TruthEngine expects."""
    return {
        "event_id": ev.event_id,
        "source": ev.source,
        "title": ev.raw_text[:200],
        "description": ev.raw_text,
        "timestamp": ev.timestamp.isoformat(),
        "mode": "maritime",  # default; refined downstream
        "severity": "medium",
        "trust_score": ev.trust_score,
        "metadata": ev.metadata,
    }


def _truth_dict_to_raw_event(verified: dict) -> RawEvent:
    """Convert a TruthEngine-verified dict back to a RawEvent for serialization."""
    return RawEvent(
        source=verified.get("source", "Unknown"),
        raw_text=verified.get("description", verified.get("title", "")),
        timestamp=datetime.fromisoformat(
            verified["timestamp"].replace("Z", "+00:00")
        ) if isinstance(verified.get("timestamp"), str) else datetime.now(timezone.utc),
        trust_score=verified.get("truth_score", 0.70),
        tags=verified.get("confirmed_by", []),
        metadata={
            **verified.get("metadata", {}),
            "verification_status": verified.get("verification_status", "unverified"),
            "match_confidence": verified.get("match_confidence", 0.0),
            "confirmed_by": verified.get("confirmed_by", []),
        },
    )


def fetch_newsapi():
    if not settings.newsapi_key:
        logging.warning("NEWSAPI_KEY not found. Skipping NewsAPI.")
        return []

    # Handle SecretStr if using pydantic
    api_key = settings.newsapi_key.get_secret_value() if hasattr(settings.newsapi_key, 'get_secret_value') else settings.newsapi_key
    
    url = "https://newsapi.org/v2/everything"
    params = {
        "q": '("supply chain" OR "tariffs" OR "export controls" OR "customs" OR "trade agreement" OR "freight")',
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": 20,
        "apiKey": api_key
    }
    
    logging.info("Fetching real-world trade news from NewsAPI...")
    
    events = []
    try:
        response = httpx.get(url, params=params, timeout=15.0)
        response.raise_for_status()
        data = response.json()
        articles = data.get("articles", [])
        for article in articles:
            # Combine title and description for a rich text payload
            title = article.get("title") or ""
            desc = article.get("description") or ""
            raw_text = f"{title}. {desc}".strip().strip(".")
            
            if not raw_text or raw_text == "Removed":
                continue
                
            source_info = article.get("source", {}).get("name", "NewsAPI")
            article_url = article.get("url", "")
            
            event = RawEvent(
                source=f"NewsAPI ({source_info})",
                raw_text=raw_text,
                metadata={"url": article_url}
            )
            events.append(event)
    except Exception as e:
        logging.error(f"Failed to fetch data from NewsAPI: {e}")
        
    return events

def fetch_guardian():
    if not settings.the_guardian_api_key:
        logging.warning("THE_GUARDIAN_API_KEY not found. Skipping The Guardian API.")
        return []
        
    # Handle SecretStr if using pydantic
    api_key = settings.the_guardian_api_key.get_secret_value() if hasattr(settings.the_guardian_api_key, 'get_secret_value') else settings.the_guardian_api_key
    
    url = "https://content.guardianapis.com/search"
    params = {
        "section": "business",
        "q": '"trade" OR "supply chain" OR "exports" OR "tariffs"',
        "api-key": api_key,
        "show-fields": "headline,bodyText",
        "page-size": 20
    }
    
    logging.info("Fetching real-world trade news from The Guardian...")
    
    events = []
    try:
        response = httpx.get(url, params=params, timeout=15.0)
        response.raise_for_status()
        data = response.json()
        results = data.get("response", {}).get("results", [])
        
        for article in results:
            fields = article.get("fields", {})
            title = fields.get("headline") or article.get("webTitle") or ""
            content = fields.get("bodyText", "")[:1000] # Limit to avoid huge payloads
            raw_text = f"{title}. {content}".strip().strip(".")
            
            if not raw_text:
                continue
                
            article_url = article.get("webUrl", "")
            
            event = RawEvent(
                source="The Guardian",
                raw_text=raw_text,
                metadata={"url": article_url}
            )
            events.append(event)
    except Exception as e:
        logging.error(f"Failed to fetch data from The Guardian: {e}")
        
    return events

def fetch_and_store_news():
    # ── Phase 1: Async fetch — ONLY advanced scrapers ─────────────────────────
    # [Phase 12.1 Kill Switch] Legacy scrapers (NewsAPI, Guardian) disabled.
    # They were pulling stale 2023/2024 news that polluted the data lake.
    # Active sources: GDELT Doc API, EventRegistry, Turkish RSS feeds.
    
    loop = asyncio.get_event_loop()
    if loop.is_running():
        # Inside async context like Streamlit or test runners
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(asyncio.run, asyncio.gather(
                fetch_gdelt_doc(),
                fetch_event_registry(),
                fetch_turkish_rss()
            ))
            gdelt_events, event_registry_events, rss_events = future.result()
    else:
        gdelt_events, event_registry_events, rss_events = loop.run_until_complete(asyncio.gather(
            fetch_gdelt_doc(),
            fetch_event_registry(),
            fetch_turkish_rss()
        ))
    
    all_events = gdelt_events + event_registry_events + rss_events
    if not all_events:
        logging.info("No articles found from any source.")
        return

    # ── Phase 2: TruthEngine cross-verification ──────────────────────────────
    # Group RawEvents by source key for N-source verification
    logging.info(f"Running TruthEngine cross-verification on {len(all_events)} events...")
    
    source_groups: dict[str, list[dict]] = {}
    for ev in all_events:
        src_key = _classify_source(ev.source)
        truth_dict = _raw_event_to_truth_dict(ev)
        source_groups.setdefault(src_key, []).append(truth_dict)
    
    logging.info(f"Source groups: {', '.join(f'{k}={len(v)}' for k, v in source_groups.items())}")
    
    engine = TruthEngine()
    verified_dicts = engine.verify_events(source_events=source_groups)
    
    # Convert verified dicts back to RawEvent for serialization
    verified_events = [_truth_dict_to_raw_event(vd) for vd in verified_dicts]
    
    logging.info(
        f"TruthEngine: {len(all_events)} raw → {len(verified_events)} verified events "
        f"(deduplication removed {len(all_events) - len(verified_events)} duplicates)"
    )

    # ── Phase 3: Save only verified events to data lake ──────────────────────
    output_path = root_dir / "nexus_data_lake.jsonl"
    events_saved = 0
    
    with open(output_path, "a", encoding="utf-8") as f:
        for event in verified_events:
            f.write(event.model_dump_json() + "\n")
            events_saved += 1
            
    logging.info(f"Successfully saved {events_saved} verified trade events to {output_path.name}")
    
    # ── Phase 4: Comtrade pipeline ───────────────────────────────────────────
    logging.info("News scraping completed. Handing over to Comtrade Service...")
    asyncio.run(run_comtrade_pipeline())

if __name__ == "__main__":
    fetch_and_store_news()
