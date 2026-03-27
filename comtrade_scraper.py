"""
NEXUS Trade Radar — UN Comtrade Scraper v2.0
=============================================
comtrade_service.py ile entegre çalışan, BaseAPIClient tabanlı scraper katmanı.

v2.0 İyileştirmeleri:
  • ComtradeClient (async, paginated, rate-limited) doğrudan kullanılır
  • Birincil → ikincil API anahtarı geçişi anahtar düzeyinde değil,
    istek düzeyinde yönetilir (per-request failover)
  • Çok-dönemli toplu sorgu: fetch_bulk_flows() ile tek çağrıda
    birden fazla HS kodu + dönem + yön
  • Minerals vertical için önceden yapılandırılmış kısayollar
    (calcite, dolomite, talc, kaolin, paint/coatings)
  • Anomali ve denge sinyalleri doğrudan RawEvent'e dönüştürülür
  • trust_score, tags, language alanları doldurulur
  • Geriye uyumluluk alias'ları korunur (mevcut main.py bozulmaz)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from core.config import settings
from worker.models.signal import RawEvent
from worker.scrapers.base_scraper import BaseAPIClient

# comtrade_service.py → tek gerçek kaynak (tek sorumluluk)
from worker.scrapers.comtrade_service import (
    ComtradeClient,
    ComtradeAnalytics,
    ComtradeRecord,
    HS_CODES,
    REPORTERS,
    KEY_PARTNERS,
    records_to_raw_events,
    anomalies_to_raw_events,
    balances_to_raw_events,
    save_structured_records,
)
from pathlib import Path
import asyncio

logger = logging.getLogger(__name__)
root_dir = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# HS Kod grupları — Minerals vertical + genel Trade Radar
# ---------------------------------------------------------------------------
HS_GROUPS: dict[str, list[str]] = {
    "minerals": ["252010", "252020", "252610", "252620", "250700", "283650"],
    "paint_coatings": ["320810", "320890", "320910", "320990", "321490"],
    "steel": ["7208", "7210", "7225"],
    "automotive": ["8411", "8708"],
    "textile": ["6203", "6204"],
    "plastics": ["3901", "3902"],
    "agri": ["0702", "0805"],
}

# Etiket → HS grubu eşlemesi (RawEvent.tags için)
_GROUP_TAGS: dict[str, list[str]] = {
    "minerals":      ["minerals", "industrial_minerals", "turmet"],
    "paint_coatings":["paint", "coatings", "b2b_customer"],
    "steel":         ["steel", "metals"],
    "automotive":    ["automotive", "machinery"],
    "textile":       ["textile", "apparel"],
    "plastics":      ["plastics", "chemicals"],
    "agri":          ["agriculture", "fresh_produce"],
}

# trust_score: resmi UN istatistiği → 0.97
_COMTRADE_TRUST = 0.97


# ---------------------------------------------------------------------------
# Yardımcı: API anahtarını al (birincil / ikincil failover)
# ---------------------------------------------------------------------------
def _resolve_api_key(use_secondary: bool = False) -> Optional[str]:
    """
    Birincil veya ikincil Comtrade API anahtarını döndürür.
    use_secondary=True iken ikincil anahtar yoksa birincile düşer.
    """
    primary = settings.comtrade_api_key_primary
    secondary = getattr(settings, "comtrade_api_key_secondary", None)

    def _get(secret) -> Optional[str]:
        if secret is None:
            return None
        return secret.get_secret_value() if hasattr(secret, "get_secret_value") else secret

    if use_secondary and secondary:
        return _get(secondary)
    return _get(primary)


# ---------------------------------------------------------------------------
# ComtradeScraper
# ---------------------------------------------------------------------------
class ComtradeScraper(BaseAPIClient):
    """
    UN Comtrade API'den ticaret akış verisi çeken scraper sınıfı.

    Mimari not:
    -----------
    Ham HTTP işleri ComtradeClient (comtrade_service.py) üstlenir.
    Bu sınıf:
      1. NEXUS domain parametrelerini (HS grubu, reporter, partner, dönem) yönetir,
      2. ComtradeRecord → RawEvent dönüşümünü yapar,
      3. Analitik sinyalleri (denge, YoY anomali) ekler,
      4. BaseAPIClient miras hiyerarşisini korur.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        primary_key = _resolve_api_key(use_secondary=False)
        if not primary_key:
            raise ValueError("COMTRADE_API_KEY_PRIMARY yapılandırılmamış.")
        self._primary_key = primary_key
        self._secondary_key = _resolve_api_key(use_secondary=True)

    # ------------------------------------------------------------------
    # Düşük seviye: tek HS grubu + dönem + yön
    # ------------------------------------------------------------------

    async def fetch_trade_flows(
        self,
        cmd_code: str = "283650",
        reporter_code: str = "792",
        partner_code: Optional[str] = None,
        period: str = "2023",
        flow_code: str = "X",
        frequency: str = "A",
        max_records: int = 500,
        use_secondary_key: bool = False,
    ) -> list[RawEvent]:
        """
        Tek bir HS kodu için ticaret akışlarını çeker.

        Parametreler
        ------------
        cmd_code        : HS kodu (örn. "283650" = kalsit)
        reporter_code   : Raporlayan ülke kodu (örn. "792" = Türkiye)
        partner_code    : Partner ülke kodu; None → Dünya toplamı (0)
        period          : "2023" (yıllık) veya "202301" (aylık)
        flow_code       : "X"=ihracat | "M"=ithalat | "RX"=re-ihracat
        frequency       : "A"=yıllık | "M"=aylık
        max_records     : Sayfa başına maksimum kayıt (≤500)
        use_secondary_key: True → ikincil API anahtarını kullan
        """
        api_key = self._secondary_key if use_secondary_key else self._primary_key

        reporter_int = int(reporter_code)
        partner_int = int(partner_code) if partner_code else 0

        records: list[ComtradeRecord] = []

        try:
            async with ComtradeClient(api_key) as client:
                records, _ = await client._fetch_page(
                    frequency=frequency,
                    reporter=reporter_int,
                    partner=partner_int,
                    cmd_codes=[cmd_code],
                    period=period,
                    flow=flow_code,
                    start_record=0,
                )
        except Exception as exc:
            # Birincil başarısız → ikincil anahtar ile otomatik fallback
            if not use_secondary_key and self._secondary_key:
                logger.warning(
                    "Birincil Comtrade anahtarı başarısız (%s), "
                    "ikincil anahtar devreye giriyor…",
                    exc,
                )
                return await self.fetch_trade_flows(
                    cmd_code=cmd_code,
                    reporter_code=reporter_code,
                    partner_code=partner_code,
                    period=period,
                    flow_code=flow_code,
                    frequency=frequency,
                    max_records=max_records,
                    use_secondary_key=True,
                )
            logger.error("Comtrade fetch_trade_flows hatası: %s", exc)
            return [
                RawEvent.error_event(
                    source="UN Comtrade",
                    reason=str(exc),
                    metadata={
                        "cmd_code": cmd_code,
                        "reporter_code": reporter_code,
                        "period": period,
                        "flow_code": flow_code,
                    },
                )
            ]

        if not records:
            logger.warning(
                "Comtrade: Sonuç boş — HS=%s, reporter=%s, period=%s, flow=%s",
                cmd_code, reporter_code, period, flow_code,
            )
            return [
                RawEvent.error_event(
                    source="UN Comtrade",
                    reason="Sonuç kümesi boş. Parametre veya kota sorunu olabilir.",
                    metadata={
                        "cmd_code": cmd_code,
                        "reporter_code": reporter_code,
                        "period": period,
                    },
                )
            ]

        # HS grubunu bul → tag listesi
        group_name = next(
            (g for g, codes in HS_GROUPS.items() if cmd_code in codes), "general"
        )
        flow_label = {"X": "ihracat", "M": "ithalat", "RX": "re-ihracat"}.get(
            flow_code, flow_code
        )
        base_tags = ["trade_flow", "comtrade", "official", f"hs_{cmd_code}", flow_label]
        base_tags.extend(_GROUP_TAGS.get(group_name, []))

        now = datetime.now(timezone.utc)
        events: list[RawEvent] = []
        for rec in records:
            ev = RawEvent(
                source="UN Comtrade",
                raw_text=rec.to_signal_text(),
                timestamp=now,
                trust_score=_COMTRADE_TRUST,
                language="tr",
                tags=base_tags + [f"group_{group_name}"],
                metadata={
                    "comtrade_record": {
                        "reporter_code":      rec.reporter_code,
                        "reporter_desc":      rec.reporter_desc,
                        "partner_code":       rec.partner_code,
                        "partner_desc":       rec.partner_desc,
                        "flow_code":          rec.flow_code,
                        "cmd_code":           rec.cmd_code,
                        "cmd_desc":           rec.cmd_desc,
                        "period":             rec.period,
                        "primary_value_usd":  rec.primary_value_usd,
                        "net_weight_kg":      rec.net_weight_kg,
                        "unit_value_usd_per_kg": rec.unit_value,
                    },
                    "reporter_code": reporter_code,
                    "flow_code":     flow_code,
                    "period":        period,
                    "hs_group":      group_name,
                },
            )
            events.append(ev)

        logger.info(
            "Comtrade fetch_trade_flows: %d olay — HS=%s, flow=%s, period=%s",
            len(events), cmd_code, flow_code, period,
        )
        return events

    # ------------------------------------------------------------------
    # Yüksek seviye: toplu çok-dönemli sorgu + analitik sinyaller
    # ------------------------------------------------------------------

    async def fetch_bulk_flows(
        self,
        hs_groups: list[str] = ("minerals", "paint_coatings"),
        reporters: list[int] = (792,),
        partners: list[int] = (0,),
        annual_periods: list[str] = ("2021", "2022", "2023"),
        monthly_periods: Optional[list[str]] = None,
        include_analytics: bool = True,
        concurrency: int = 3,
    ) -> list[RawEvent]:
        """
        Birden fazla HS grubu, dönem ve reporter için toplu async sorgu.

        Ek olarak include_analytics=True iken ticaret dengesi ve YoY
        anomali sinyalleri de üretilir.

        Parametreler
        ------------
        hs_groups       : HS_GROUPS anahtarları (örn. ["minerals", "paint_coatings"])
        reporters       : Raporlayan ülke kodları
        partners        : Partner ülke kodları (0 = Dünya toplamı)
        annual_periods  : Yıllık sorgu dönemleri
        monthly_periods : Aylık sorgu dönemleri; None → otomatik son 12 ay
        include_analytics: True → denge + YoY anomali sinyalleri ekle
        concurrency     : Eş zamanlı HTTP isteği sayısı
        """
        if monthly_periods is None:
            from datetime import date
            today = date.today()
            monthly_periods = [
                f"{today.year - (1 if m > today.month else 0)}{m:02d}"
                for m in range(1, 13)
            ]

        # Seçilen HS gruplarını flat liste halinde birleştir, 10'luk gruplara böl
        all_codes: list[str] = []
        for g in hs_groups:
            all_codes.extend(HS_GROUPS.get(g, []))
        # API limiti: istek başına ≤10 HS kodu
        cmd_groups = [all_codes[i:i+10] for i in range(0, len(all_codes), 10)]

        all_records: list[ComtradeRecord] = []

        try:
            async with ComtradeClient(self._primary_key) as client:
                # Yıllık veri
                annual = await client.fetch_bulk(
                    frequency="A",
                    reporters=list(reporters),
                    partners=list(partners),
                    cmd_groups=cmd_groups,
                    periods=list(annual_periods),
                    flows=["M", "X"],
                    concurrency=concurrency,
                )
                all_records.extend(annual)

                # Aylık veri
                monthly = await client.fetch_bulk(
                    frequency="M",
                    reporters=list(reporters),
                    partners=[0],           # aylık: sadece Dünya toplamı
                    cmd_groups=cmd_groups,
                    periods=list(monthly_periods),
                    flows=["M", "X"],
                    concurrency=concurrency,
                )
                all_records.extend(monthly)

        except Exception as exc:
            logger.error("fetch_bulk_flows ComtradeClient hatası: %s", exc)

        if not all_records:
            logger.warning("fetch_bulk_flows: hiç kayıt alınamadı.")
            return []

        # Yapısal kayıtları diske yaz (Neo4j / TimescaleDB import için)
        struct_path = root_dir / "comtrade_structured_records.jsonl"
        save_structured_records(all_records, struct_path)

        # Temel RawEvent'ler
        events: list[RawEvent] = []
        base_events = records_to_raw_events(all_records)

        # trust_score + tags ekle
        now = datetime.now(timezone.utc)
        for ev in base_events:
            cmd = ev.metadata.get("cmd_code", "")
            group = next((g for g, c in HS_GROUPS.items() if cmd in c), "general")
            ev.timestamp = now
            ev.trust_score = _COMTRADE_TRUST
            ev.language = "tr"
            ev.tags = ["trade_flow", "comtrade", "official"] + _GROUP_TAGS.get(group, [])
        events.extend(base_events)

        # Analitik sinyaller
        if include_analytics:
            analytics = ComtradeAnalytics()

            world_records = [r for r in all_records if r.partner_code == 0]
            balances = analytics.compute_trade_balances(world_records)
            balance_events = balances_to_raw_events(balances)
            for ev in balance_events:
                ev.timestamp = now
                ev.trust_score = _COMTRADE_TRUST
                ev.language = "tr"
                ev.tags = ["trade_balance", "comtrade", "official", "analytics"]
            events.extend(balance_events)

            anomalies = analytics.detect_yoy_anomalies(all_records)
            anomaly_events = anomalies_to_raw_events(anomalies)
            for ev in anomaly_events:
                ev.timestamp = now
                ev.trust_score = _COMTRADE_TRUST
                ev.language = "tr"
                ev.tags = ["yoy_anomaly", "comtrade", "official", "analytics", "alert"]
            events.extend(anomaly_events)

            logger.info(
                "Analitik: %d denge özeti, %d YoY anomali üretildi.",
                len(balance_events), len(anomaly_events),
            )

        logger.info(
            "fetch_bulk_flows tamamlandı: %d kayıt → %d sinyal",
            len(all_records), len(events),
        )
        return events

    # ------------------------------------------------------------------
    # Minerals vertical kısayolları
    # ------------------------------------------------------------------

    async def fetch_calcite_exports(
        self,
        period: str = "2023",
        reporter_code: str = "792",
    ) -> list[RawEvent]:
        """Türkiye kalsit ihracatı (HS 283650) için önceden yapılandırılmış kısayol."""
        return await self.fetch_trade_flows(
            cmd_code="283650",
            reporter_code=reporter_code,
            period=period,
            flow_code="X",
            frequency="A",
        )

    async def fetch_dolomite_exports(
        self,
        period: str = "2023",
        reporter_code: str = "792",
    ) -> list[RawEvent]:
        """Türkiye dolomit ihracatı (HS 252010 + 252020)."""
        events: list[RawEvent] = []
        for hs in ["252010", "252020"]:
            events.extend(
                await self.fetch_trade_flows(
                    cmd_code=hs,
                    reporter_code=reporter_code,
                    period=period,
                    flow_code="X",
                )
            )
        return events

    async def fetch_talc_exports(
        self,
        period: str = "2023",
        reporter_code: str = "792",
    ) -> list[RawEvent]:
        """Türkiye talk ihracatı (HS 252610 + 252620)."""
        events: list[RawEvent] = []
        for hs in ["252610", "252620"]:
            events.extend(
                await self.fetch_trade_flows(
                    cmd_code=hs,
                    reporter_code=reporter_code,
                    period=period,
                    flow_code="X",
                )
            )
        return events

    async def fetch_kaolin_exports(
        self,
        period: str = "2023",
        reporter_code: str = "792",
    ) -> list[RawEvent]:
        """Türkiye kaolin ihracatı (HS 250700)."""
        return await self.fetch_trade_flows(
            cmd_code="250700",
            reporter_code=reporter_code,
            period=period,
            flow_code="X",
        )

    async def fetch_paint_imports(
        self,
        period: str = "2023",
        reporter_code: str = "792",
    ) -> list[RawEvent]:
        """
        Türkiye'ye boya/kaplama ithalatı — Turmet'in müşteri segmenti (Jotun, Akzo Nobel…).
        Buradaki ithalat artışı → boyacı segmentinin büyümesi → kalsit/dolomit talebinin
        artışına dair dolaylı sinyal.
        """
        events: list[RawEvent] = []
        for hs in ["320810", "320890", "320910", "320990", "321490"]:
            events.extend(
                await self.fetch_trade_flows(
                    cmd_code=hs,
                    reporter_code=reporter_code,
                    period=period,
                    flow_code="M",
                )
            )
        return events

    async def fetch_minerals_full_picture(
        self,
        periods: list[str] = ("2021", "2022", "2023"),
    ) -> list[RawEvent]:
        """
        Minerals vertical için eksiksiz görünüm:
          • Tüm minerals + paint/coatings HS kodları
          • 3 yıllık trend + son 12 ay
          • Denge ve YoY anomali sinyalleri dahil
        """
        return await self.fetch_bulk_flows(
            hs_groups=["minerals", "paint_coatings"],
            reporters=[792],
            partners=[0, 276, 840, 156, 826],
            annual_periods=list(periods),
            include_analytics=True,
            concurrency=3,
        )

    # ------------------------------------------------------------------
    # Geriye uyumluluk: eski imza korunur
    # ------------------------------------------------------------------

    async def scrape(self) -> list[RawEvent]:
        """
        BaseAPIClient.scrape() abstract metodunu karşılar.
        Varsayılan olarak minerals full picture çeker.
        """
        return await self.fetch_minerals_full_picture()
