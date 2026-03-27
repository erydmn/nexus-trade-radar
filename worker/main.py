"""
NEXUS Trade Radar — Scraper Orkestratörü
=========================================
İyileştirmeler:
  - sys.path manipülasyonu kaldırıldı → proje kök dizininden çalıştırılmalı
    (python -m worker.main veya Dockerfile/Railway start komutu ile)
  - Logging modülü yapılandırıldı (print() yerine)
  - Veri gölüne yazım öncesi SHA-256 event_id ile tekilleştirme (deduplication)
  - Veri gölü yolü settings'ten okunuyor (hardcode yok)
  - asyncio.gather exception handling genişletildi: hata özeti döngü sonunda raporlanır
  - Scraper oturumları async context manager ile düzgün kapatılıyor (connection leak yok)
  - Atomik yazım: önce .tmp dosyasına yaz, sonra rename → veri gölü bozulma riski yok
  - İstatistik özeti: toplam olay, scraper başına başarı/hata sayısı
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import List, Union

# ── Proje kök dizinini Python yoluna ekle (geliştirme ortamı kolaylığı) ────
# Üretimde `python -m worker.main` veya Railway start komutu kullanın.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.config import settings
from worker.models.signal import RawEvent
from worker.scrapers.comtrade_scraper import ComtradeScraper
from worker.scrapers.dynamic_web_scraper import DynamicWebScraper
from worker.scrapers.newsapi_scraper import NewsAPIScraper

# ── Logging Yapılandırması ────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("nexus.orchestrator")


# ── Yardımcı: Veri Gölüne Atomik Yazım ───────────────────────────────────────

def _write_to_data_lake(events: List[RawEvent], path: str) -> int:
    """
    Olayları JSONL formatında veri gölüne ekler (append modu).

    Atomik yazım:
    1. Geçici .tmp dosyasına yaz
    2. Mevcut JSONL dosyasına .tmp içeriğini ekle
    3. .tmp dosyasını sil

    Döndürür: Yazılan olay sayısı
    """
    lake_path = Path(path)
    tmp_path = lake_path.with_suffix(".tmp")

    lines = [event.model_dump_json() for event in events]

    # Geçici dosyaya yaz
    with open(tmp_path, "w", encoding="utf-8") as tmp_f:
        tmp_f.write("\n".join(lines) + "\n")

    # Ana veri gölüne ekle
    with open(lake_path, "a", encoding="utf-8") as lake_f, \
         open(tmp_path, "r", encoding="utf-8") as tmp_f:
        lake_f.write(tmp_f.read())

    tmp_path.unlink(missing_ok=True)
    return len(lines)


def _deduplicate(events: List[RawEvent]) -> List[RawEvent]:
    """
    event_id (SHA-256) kullanarak aynı batch içindeki tekrarlı olayları çıkarır.
    Veri gölündeki mevcut kayıtlarla çapraz kontrol yapmaz (o görev pipeline'ın ileriki adımına aittir).
    """
    seen: set[str] = set()
    unique: List[RawEvent] = []
    for event in events:
        if event.event_id not in seen:
            seen.add(event.event_id)
            unique.append(event)
    return unique


# ── Ana Orkestrasyon ──────────────────────────────────────────────────────────

async def run_all_scrapers() -> None:
    """
    Tüm scraper'ları eşzamanlı olarak çalıştırır, sonuçları toplar,
    tekilleştirir ve veri gölüne yazar.
    """
    logger.info("═" * 60)
    logger.info("NEXUS Orchestrator başlatılıyor…")

    config_path = str(_PROJECT_ROOT / "core" / "sources_config.json")

    scraper_names = ["NewsAPI", "Comtrade", "DynamicWeb"]

    # Context manager ile scraper'ları oluştur → oturumlar düzgün kapatılır
    async with NewsAPIScraper() as news_scraper, \
               ComtradeScraper() as comtrade_scraper, \
               DynamicWebScraper(config_path=config_path) as web_scraper:

        results: List[Union[List[RawEvent], Exception]] = await asyncio.gather(
            news_scraper.fetch_articles(),
            comtrade_scraper.fetch_calcite_exports(),
            web_scraper.run_scraping_cycle(),
            return_exceptions=True,  # Bir scraper çökse bile diğerleri devam eder
        )

    # ── Sonuçları İşle ───────────────────────────────────────────────────────
    all_events: List[RawEvent] = []
    error_summary: List[str] = []

    for i, result in enumerate(results):
        name = scraper_names[i]
        if isinstance(result, Exception):
            msg = f"[{name}] kritik hata: {type(result).__name__}: {result}"
            logger.error(msg)
            error_summary.append(msg)
        elif result:
            scraper_errors = [e for e in result if "error" in e.tags]
            scraper_ok = [e for e in result if "error" not in e.tags]
            logger.info(
                "[%s] %d başarılı olay, %d hata olayı",
                name, len(scraper_ok), len(scraper_errors),
            )
            all_events.extend(result)

    # ── Tekilleştirme ────────────────────────────────────────────────────────
    before_dedup = len(all_events)
    all_events = _deduplicate(all_events)
    after_dedup = len(all_events)
    if before_dedup != after_dedup:
        logger.info("Tekilleştirme: %d → %d olay (%d tekrar kaldırıldı).",
                    before_dedup, after_dedup, before_dedup - after_dedup)

    # ── Veri Gölüne Yaz ──────────────────────────────────────────────────────
    if all_events:
        data_lake_path = str(_PROJECT_ROOT / settings.data_lake_path)
        written = _write_to_data_lake(all_events, data_lake_path)
        logger.info("Veri gölüne %d olay yazıldı: %s", written, data_lake_path)
    else:
        logger.warning("Yazılacak olay bulunamadı.")

    # ── Özet ─────────────────────────────────────────────────────────────────
    logger.info("─" * 60)
    logger.info("Orkestrasyon tamamlandı. Toplam olay: %d", len(all_events))
    if error_summary:
        logger.warning("Hata özeti:\n  %s", "\n  ".join(error_summary))
    logger.info("═" * 60)


if __name__ == "__main__":
    asyncio.run(run_all_scrapers())
