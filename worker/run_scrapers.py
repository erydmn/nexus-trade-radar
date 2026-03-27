import sys
from pathlib import Path

# Add project root to sys.path
root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

import logging
import httpx
from core.config import settings
from worker.models.signal import RawEvent

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def fetch_and_store_news():
    if not settings.newsapi_key:
        logging.warning("NEWSAPI_KEY not found in configuration. Exiting scraper gracefully.")
        return

    # Handle SecretStr if using pydantic
    api_key = settings.newsapi_key.get_secret_value() if hasattr(settings.newsapi_key, 'get_secret_value') else settings.newsapi_key
    
    url = "https://newsapi.org/v2/top-headlines"
    params = {
        "category": "business",
        "language": "en",
        "pageSize": 20,
        "apiKey": api_key
    }
    
    logging.info("Fetching real-world news from NewsAPI...")
    
    try:
        response = httpx.get(url, params=params, timeout=15.0)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        logging.error(f"Failed to fetch data from NewsAPI: {e}")
        return

    articles = data.get("articles", [])
    if not articles:
        logging.info("No articles found in NewsAPI response.")
        return
        
    output_path = root_dir / "nexus_data_lake.jsonl"
    events_saved = 0
    
    with open(output_path, "a", encoding="utf-8") as f:
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
            
            f.write(event.model_dump_json() + "\n")
            events_saved += 1
            
    logging.info(f"Successfully saved {events_saved} new business events to {output_path.name}")

if __name__ == "__main__":
    fetch_and_store_news()
