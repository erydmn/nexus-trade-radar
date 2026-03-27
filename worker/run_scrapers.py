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

def fetch_comtrade_data():
    if not settings.comtrade_api_key_primary:
        logging.warning("COMTRADE_API_KEY_PRIMARY not found. Skipping UN Comtrade.")
        return []
        
    # Handle SecretStr if using pydantic
    api_key = settings.comtrade_api_key_primary.get_secret_value() if hasattr(settings.comtrade_api_key_primary, 'get_secret_value') else settings.comtrade_api_key_primary
    
    url = "https://comtradeapi.un.org/data/v1/get/C/A/HS"
    headers = {"Ocp-Apim-Subscription-Key": api_key}
    params = {
        "reporterCode": 792,
        "partnerCode": 0,
        "cmdCode": "72,84,87",
        "period": 2023,
        "flowCode": "M,X"
    }
    
    logging.info("Fetching real-world macro trade data from UN Comtrade...")
    
    events = []
    try:
        response = httpx.get(url, headers=headers, params=params, timeout=20.0)
        response.raise_for_status()
        data = response.json()
        
        records = data.get("data", [])
        if not records:
            logging.info("No records found in UN Comtrade response.")
            return events
            
        for record in records:
            cmdCode = record.get("cmdCode", "Unknown")
            flowCode = record.get("flowCode", "Unknown")
            primaryValue = record.get("primaryValue", 0)
            period = record.get("period", "Unknown")
            reporterDesc = record.get("reporterDesc", "Turkey")
            partnerDesc = record.get("partnerDesc", "World")
            
            summary = (f"UN Comtrade Official Data: Reporter: {reporterDesc}, "
                       f"Partner: {partnerDesc}, Flow: {flowCode}, "
                       f"HS Code: {cmdCode}, Value: {primaryValue} USD, Period: {period}.")
            
            event = RawEvent(
                source="UN Comtrade API",
                raw_text=summary,
                metadata={"url": "https://comtradeplus.un.org"}
            )
            events.append(event)
    except Exception as e:
        logging.error(f"Failed to fetch data from UN Comtrade: {e}")
        
    return events

def fetch_and_store_news():
    newsapi_events = fetch_newsapi()
    guardian_events = fetch_guardian()
    comtrade_events = fetch_comtrade_data()
    
    all_events = newsapi_events + guardian_events + comtrade_events
    if not all_events:
        logging.info("No articles found from any source.")
        return
        
    output_path = root_dir / "nexus_data_lake.jsonl"
    events_saved = 0
    
    with open(output_path, "a", encoding="utf-8") as f:
        for event in all_events:
            f.write(event.model_dump_json() + "\n")
            events_saved += 1
            
    logging.info(f"Successfully saved {events_saved} new highly targeted trade events to {output_path.name}")

if __name__ == "__main__":
    fetch_and_store_news()
