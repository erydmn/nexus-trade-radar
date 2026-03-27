import sys
import asyncio
from pathlib import Path
import logging
import httpx
import feedparser
from core.config import settings
from worker.models.signal import RawEvent
from datetime import datetime, timezone, timedelta
from time import mktime

logger = logging.getLogger("nexus.advanced")

async def fetch_gdelt_doc():
    url = "https://api.gdeltproject.org/api/v2/doc/doc"
    params = {
        "query": "(calcite OR 'calcium carbonate' OR mining OR 'industrial minerals' OR 'port strike' OR 'supply chain')",
        "mode": "artlist",
        "maxrecords": 15,
        "format": "json",
        "sort": "DateDesc",
        "timespan": "7d"
    }
    
    events = []
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            articles = data.get("articles", [])
            for art in articles:
                title = art.get('title', '')
                domain = art.get('domain', '')
                if title:
                    raw_text = f"{title}. Domain: {domain}".strip()
                    events.append(RawEvent(
                        source="GDELT",
                        raw_text=raw_text,
                        metadata={"url": art.get("url", ""), "date": art.get("seendate")},
                        tags=["macro", "global_event"]
                    ))
    except Exception as e:
        logger.error(f"GDELT fetch failed: {e}")
    return events

async def fetch_event_registry():
    if not settings.eventregistry_api_key:
        logger.warning("EventRegistry API key omitted. Skipping.")
        return []
        
    api_key = settings.eventregistry_api_key.get_secret_value() if hasattr(settings.eventregistry_api_key, 'get_secret_value') else settings.eventregistry_api_key
    
    url = "https://eventregistry.org/api/v1/article/getArticles"
    # 7 days ago string for Event Registry
    cutoff_str = (datetime.now(timezone.utc) - timedelta(days=7)).strftime('%Y-%m-%d')
    
    payload = {
        "keyword": ["calcite", "paint industry", "coating", "mineral"],
        "keywordOper": "or",
        "lang": ["eng", "tur"],
        "articlesCount": 10,
        "apiKey": api_key,
        "dateStart": cutoff_str
    }
    
    events = []
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            articles = data.get("articles", {}).get("results", [])
            for art in articles:
                title = art.get('title', '')
                body = art.get('body', '')[:1000]
                if title:
                    raw_text = f"{title}. {body}".strip()
                    events.append(RawEvent(
                        source="EventRegistry",
                        raw_text=raw_text,
                        metadata={"url": art.get("url", "")},
                        tags=["industry_news", "competitor_intel"]
                    ))
    except Exception as e:
        logger.error(f"EventRegistry fetch failed: {e}")
    return events

async def fetch_turkish_rss():
    url = "https://madencilikTurkiye.com/feed/"
    events = []
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            
            # Run feedparser parsing in thread to avoid blocking loop
            feed = await asyncio.to_thread(feedparser.parse, resp.text)
            cutoff = datetime.now(timezone.utc) - timedelta(days=7)
            
            for entry in feed.entries[:10]:
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    dt = datetime.fromtimestamp(mktime(entry.published_parsed), timezone.utc)
                    if dt < cutoff:
                        continue
                        
                title = entry.get('title', '')
                desc = entry.get('description', '')
                if title:
                    raw_text = f"{title}. {desc}".strip()
                    events.append(RawEvent(
                        source="Local RSS",
                        raw_text=raw_text,
                        metadata={"url": entry.get("link", "")},
                        tags=["local_regulation", "turkey"]
                    ))
    except Exception as e:
        logger.error(f"Turkish RSS fetch failed: {e}")
    return events
