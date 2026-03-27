"""
NEXUS Trade Radar — NewsAPI Scraper
=====================================
İyileştirmeler:
  - API anahtarı query string'den çıkarıldı → Authorization header'a taşındı
    (query string'deki anahtarlar server loglarına düşer → güvenlik riski)
  - run_dry_test() → fetch_articles() olarak yeniden adlandırıldı (anlamlı isim)
  - Arama terimi ve makale sayısı parametreleştirilerek yeniden kullanılabilir hale getirildi
  - Yayın tarihi metadata'ya eklendi; timestamp alanı doğru tipte (datetime) set ediliyor
  - trust_score ve tags RawEvent'e eklendi
  - pageSize parametresi eklendi (API destekler, varsayılan 5)
  - Hata olayı için RawEvent.error_event() kullanıldı
  - Logging modülü kullanıldı
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional

from core.config import settings
from worker.models.signal import RawEvent
from worker.scrapers.base_scraper import BaseAPIClient

logger = logging.getLogger(__name__)

_NEWSAPI_BASE_URL = "https://newsapi.org/v2/everything"
_NEWSAPI_TRUST_SCORE = 0.70  # NEXUS güven hiyerarşisinde NewsAPI seviyesi


class NewsAPIScraper(BaseAPIClient):
    """
    NewsAPI.org /v2/everything uç noktasından makale çeker.

    API anahtarı Authorization header üzerinden iletilir (güvenli yöntem).
    Ücretsiz planda son 30 günlük içerik döner; geliştirici planında daha geniş erişim vardır.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # Fix applied: Used flattened configuration `newsapi_key` instead of legacy `news.newsapi_key`
        self._api_key = settings.newsapi_key.get_secret_value()

    @property
    def _auth_headers(self) -> dict:
        """API anahtarını Authorization header olarak döndürür."""
        return {"Authorization": f"Bearer {self._api_key}"}

    async def fetch_articles(
        self,
        query: str = "Jotun OR Kalekim OR Marshall Boya OR calcite supply",
        page_size: int = 5,
        language: str = "en",
        sort_by: str = "publishedAt",
        from_date: Optional[str] = None,  # ISO 8601, ör. "2024-01-01"
    ) -> List[RawEvent]:
        """
        Verilen arama sorgusuna göre makale çeker.

        Parametreler
        ------------
        query     : NewsAPI q parametresi (Boolean operatörler desteklenir)
        page_size : Çekilecek makale sayısı (max 100, ücretsiz planda 20)
        language  : İçerik dil filtresi (ISO 639-1)
        sort_by   : "publishedAt" | "relevancy" | "popularity"
        from_date : Başlangıç tarihi filtresi (YYYY-MM-DD)
        """
        params: dict = {
            "q": query,
            "pageSize": page_size,
            "language": language,
            "sortBy": sort_by,
        }
        if from_date:
            params["from"] = from_date

        response_data = await self.fetch_json(
            _NEWSAPI_BASE_URL,
            params=params,
            headers=self._auth_headers,
        )

        if not response_data or "articles" not in response_data:
            logger.error(
                "NewsAPI yanıt alınamadı. Status: %s",
                response_data.get("status") if response_data else "N/A",
            )
            return [
                RawEvent.error_event(
                    source="NewsAPI",
                    reason=f"Yanıt alınamadı veya 'articles' alanı yok. Query: {query}",
                    metadata={"query": query, "response": response_data},
                )
            ]

        articles = response_data.get("articles", [])
        if not articles:
            logger.warning("NewsAPI: Sonuç kümesi boş. Query: %s", query)
            return []

        events: List[RawEvent] = []
        for article in articles:
            title = article.get("title") or "Başlık Yok"
            description = article.get("description") or ""
            url = article.get("url", "")
            source_name = article.get("source", {}).get("name", "Bilinmeyen Kaynak")

            # Yayın tarihini datetime'a çevir; hata varsa şimdiyi kullan
            published_at_raw = article.get("publishedAt")
            try:
                timestamp = datetime.fromisoformat(
                    published_at_raw.replace("Z", "+00:00")
                ) if published_at_raw else datetime.now(timezone.utc)
            except (ValueError, AttributeError):
                timestamp = datetime.now(timezone.utc)

            raw_text = f"{title}\n{description}".strip()

            events.append(
                RawEvent(
                    source="NewsAPI",
                    raw_text=raw_text,
                    timestamp=timestamp,
                    trust_score=_NEWSAPI_TRUST_SCORE,
                    language=language,
                    tags=["news", "newsapi"],
                    metadata={
                        "article_source": source_name,
                        "url": url,
                        "author": article.get("author"),
                        "published_at": published_at_raw,
                        "query": query,
                    },
                )
            )

        logger.info("NewsAPI: %d makale olayı oluşturuldu. Query: %s", len(events), query)
        return events
