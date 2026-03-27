"""
NEXUS Trade Radar — Dinamik Web Scraper (RSS + HTML)
=====================================================
İyileştirmeler:
  - verify=False GÜVENLİK AÇIĞI kaldırıldı → SSL doğrulaması BaseAPIClient'tan devralınır
  - sources_config.json'daki "enabled" ve "max_items" alanları artık dikkate alınıyor
  - trust_score ve tags kaynak config'ten RawEvent'e aktarılıyor
  - RSS Atom formatı desteği iyileştirildi (link[href] çözümlemesi)
  - HTML scraper sadece sayfa başlığı çekmek yerine meta description da topluyor
  - Hata durumunda crash yerine RawEvent.error_event() döndürülüyor
  - Config yüklemesi başarısız olursa açıklayıcı exception fırlatılıyor
  - Logging modülü print() yerine kullanıldı
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

from bs4 import BeautifulSoup

from worker.models.signal import RawEvent
from worker.scrapers.base_scraper import BaseAPIClient

logger = logging.getLogger(__name__)

# Proje kök dizinine göre varsayılan config yolu
_DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "core",
    "sources_config.json",
)


class DynamicWebScraper(BaseAPIClient):
    """
    sources_config.json dosyasında tanımlı RSS ve HTML kaynaklarını tarar.

    Kaynak Config Şeması
    --------------------
    source_name : str   — İnsan-okunabilir kaynak adı
    url         : str   — Hedef URL
    type        : str   — "rss" | "html"
    enabled     : bool  — False ise kaynak atlanır (varsayılan: True)
    trust_score : float — Kaynak güvenilirliği [0.0–1.0] (varsayılan: 0.70)
    max_items   : int   — RSS'te kaç öğe çekilsin (varsayılan: 5)
    language    : str   — İçerik dili ISO 639-1 (varsayılan: "en")
    tags        : list  — Sınıflandırma etiketleri (varsayılan: [])
    """

    def __init__(self, config_path: str = _DEFAULT_CONFIG_PATH, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.config_path = config_path
        self.sources: List[Dict[str, Any]] = self._load_config()

    def _load_config(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.config_path):
            raise FileNotFoundError(
                f"Kaynak konfigürasyonu bulunamadı: {self.config_path}\n"
                "core/sources_config.json dosyasının var olduğunu doğrulayın."
            )
        with open(self.config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Yalnızca enabled=True (veya enabled alanı eksik) olan kaynaklar yüklenir
        active = [s for s in data if s.get("enabled", True)]
        logger.info("%d/%d kaynak yüklendi (enabled=True).", len(active), len(data))
        return active

    # ── RSS Ayrıştırıcı ───────────────────────────────────────────────────────

    def _parse_rss(self, content: str, source: Dict[str, Any]) -> List[RawEvent]:
        soup = BeautifulSoup(content, "xml")
        max_items: int = source.get("max_items", 5)

        # RSS <item> veya Atom <entry> etiketleri
        items = soup.find_all("item") or soup.find_all("entry")
        events: List[RawEvent] = []

        for item in items[:max_items]:
            title_elem = item.find("title")
            desc_elem = item.find("description") or item.find("summary") or item.find("content")
            link_elem = item.find("link")
            pub_date_elem = item.find("pubDate") or item.find("published") or item.find("updated")

            title = title_elem.get_text(strip=True) if title_elem else "Başlık Yok"
            description = desc_elem.get_text(strip=True) if desc_elem else ""

            # Atom feed'lerde <link href="..."/> olarak gelir
            link = ""
            if link_elem:
                link = link_elem.get_text(strip=True) or link_elem.get("href", "")

            # Yayın tarihini çözümle; hata varsa şimdiki zamanı kullan
            timestamp = datetime.now(timezone.utc)
            if pub_date_elem:
                try:
                    from email.utils import parsedate_to_datetime
                    timestamp = parsedate_to_datetime(pub_date_elem.get_text(strip=True))
                except Exception:
                    pass  # Ayrıştırma başarısız → varsayılan (şimdi) kullanılır

            events.append(
                RawEvent(
                    source=source["source_name"],
                    raw_text=f"{title}\n{description}".strip(),
                    timestamp=timestamp,
                    trust_score=source.get("trust_score", 0.70),
                    language=source.get("language", "en"),
                    tags=source.get("tags", []),
                    metadata={"link": link, "type": "rss"},
                )
            )
        logger.debug("%s → %d RSS öğesi çekildi.", source["source_name"], len(events))
        return events

    # ── HTML Ayrıştırıcı ──────────────────────────────────────────────────────

    def _parse_html(self, content: str, source: Dict[str, Any]) -> List[RawEvent]:
        soup = BeautifulSoup(content, "html.parser")

        title = soup.title.get_text(strip=True) if soup.title else "Başlık Yok"

        # <meta name="description"> varsa zenginleştir
        meta_desc = ""
        meta_elem = soup.find("meta", attrs={"name": "description"})
        if meta_elem and meta_elem.get("content"):
            meta_desc = meta_elem["content"].strip()

        raw_text = f"{title}\n{meta_desc}".strip() if meta_desc else title

        return [
            RawEvent(
                source=source["source_name"],
                raw_text=raw_text,
                trust_score=source.get("trust_score", 0.70),
                language=source.get("language", "en"),
                tags=source.get("tags", []),
                metadata={"url": source["url"], "type": "html"},
            )
        ]

    # ── Ana Döngü ─────────────────────────────────────────────────────────────

    async def run_scraping_cycle(self) -> List[RawEvent]:
        """
        Tüm aktif kaynakları sırayla tarar ve RawEvent listesi döndürür.
        Tek bir kaynakta hata olursa diğerleri etkilenmez.
        """
        all_events: List[RawEvent] = []

        for source in self.sources:
            source_name = source.get("source_name", "Bilinmeyen Kaynak")
            url = source.get("url")
            source_type = source.get("type", "").lower()

            if not url:
                logger.warning("%s için URL tanımlı değil, atlanıyor.", source_name)
                continue

            logger.info("Taranıyor: %s (%s)", source_name, source_type)
            content = await self.fetch_text(url)

            if not content:
                all_events.append(
                    RawEvent.error_event(
                        source=source_name,
                        reason=f"İçerik alınamadı: {url}",
                        metadata={"url": url},
                    )
                )
                continue

            try:
                if source_type == "rss":
                    events = self._parse_rss(content, source)
                elif source_type == "html":
                    events = self._parse_html(content, source)
                else:
                    logger.warning("Desteklenmeyen kaynak tipi: '%s' (%s)", source_type, source_name)
                    continue

                all_events.extend(events)

            except Exception as exc:
                logger.exception("Ayrıştırma hatası — %s: %s", source_name, exc)
                all_events.append(
                    RawEvent.error_event(
                        source=source_name,
                        reason=f"Ayrıştırma hatası: {exc}",
                        metadata={"url": url},
                    )
                )

        logger.info("DynamicWebScraper tamamlandı. Toplam %d olay.", len(all_events))
        return all_events
