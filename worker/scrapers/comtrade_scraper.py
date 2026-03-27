"""
NEXUS Trade Radar — UN Comtrade Scraper
========================================
İyileştirmeler:
  - Hardcoded parametreler (cmdCode, period, reporterCode) → yapılandırılabilir argümanlara taşındı
  - İkincil API anahtarına otomatik geçiş: birincil anahtar 429 aldığında devreye girer
  - Tüm veri satırları işleniyor (sadece [0] değil) → tek çağrıda daha fazla olay üretir
  - timestamp oluşturma .replace('+00:00', 'Z') yerine datetime.now(timezone.utc) ile doğrudan yapılıyor
  - Hata durumunda RawEvent.error_event() kullanılıyor
  - Reporter ve partner kodu → UN LOCODE ülke adlarına çözümleniyor (basit eşleme)
  - Logging modülü kullanıldı
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional

from core.config import settings
from worker.models.signal import RawEvent
from worker.scrapers.base_scraper import BaseAPIClient

logger = logging.getLogger(__name__)

# Sık kullanılan Comtrade reporter kodları → ülke adı eşlemesi
_REPORTER_NAMES: dict[str, str] = {
    "792": "Türkiye",
    "842": "Amerika Birleşik Devletleri",
    "276": "Almanya",
    "156": "Çin",
    "380": "İtalya",
    "528": "Hollanda",
}

_COMTRADE_BASE_URL = "https://comtradeapi.un.org/data/v1/get/C/A/HS"


class ComtradeScraper(BaseAPIClient):
    """
    UN Comtrade API'den ticaret akış verisi çeker.

    Birincil API anahtarı 429 döndürürse ikincil anahtara otomatik geçiş yapar.
    Comtrade aylık 100 istek kotasına sahiptir; dönem ve parametre yönetimi dışarıdan yapılmalıdır.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._primary_key = settings.comtrade_api_key_primary.get_secret_value()
        self._secondary_key = (
            settings.comtrade_api_key_secondary.get_secret_value()
            if settings.comtrade_api_key_secondary
            else None
        )

    def _make_headers(self, use_secondary: bool = False) -> dict:
        key = self._secondary_key if (use_secondary and self._secondary_key) else self._primary_key
        return {"Ocp-Apim-Subscription-Key": key}

    async def fetch_trade_flows(
        self,
        cmd_code: str = "283650",          # Kalsit / Kalsiyum Karbonat (HS 2836.50)
        reporter_code: str = "792",         # Türkiye
        partner_code: Optional[str] = None, # None = tüm partnerlar
        period: str = "2023",
        flow_code: str = "X",              # X=İhracat, M=İthalat
        max_records: int = 20,
    ) -> List[RawEvent]:
        """
        Belirtilen HS kodu ve yöne göre ticaret akışlarını çeker.

        Parametreler
        ------------
        cmd_code     : HS kodu (örn. "283650" = kalsit)
        reporter_code: Raporlayan ülke (örn. "792" = Türkiye)
        partner_code : Partner ülke kodu; None → tüm partnerlar
        period       : Yıl veya Yıl-Ay (örn. "2023" veya "202301")
        flow_code    : "X"=ihracat, "M"=ithalat, "RX"=re-ihracat
        max_records  : Döndürülecek maksimum kayıt sayısı
        """
        params: dict = {
            "cmdCode": cmd_code,
            "reporterCode": reporter_code,
            "period": period,
            "flowCode": flow_code,
            "maxRecords": max_records,
        }
        if partner_code:
            params["partnerCode"] = partner_code

        reporter_name = _REPORTER_NAMES.get(reporter_code, f"Ülke-{reporter_code}")

        # 1. Deneme: Birincil anahtar
        response_data = await self.fetch_json(
            _COMTRADE_BASE_URL, params=params, headers=self._make_headers(use_secondary=False)
        )

        # Birincil başarısız veya kota aşıldıysa ikincil anahtarı dene
        if response_data is None and self._secondary_key:
            logger.warning("Birincil Comtrade anahtarı başarısız, ikincil anahtar deneniyor.")
            response_data = await self.fetch_json(
                _COMTRADE_BASE_URL, params=params, headers=self._make_headers(use_secondary=True)
            )

        if not response_data:
            return [
                RawEvent.error_event(
                    source="UN Comtrade",
                    reason="API yanıt vermedi veya boş döndü",
                    metadata={"params": params},
                )
            ]

        records = response_data.get("data", [])
        if not records:
            logger.warning("Comtrade: Veri bulunamadı. Parametreler: %s", params)
            return [
                RawEvent.error_event(
                    source="UN Comtrade",
                    reason="Sonuç kümesi boş. Parametre veya kota sorunu olabilir.",
                    metadata={"params": params, "response_keys": list(response_data.keys())},
                )
            ]

        events: List[RawEvent] = []
        now = datetime.now(timezone.utc)
        flow_label = {"X": "ihracat", "M": "ithalat", "RX": "re-ihracat"}.get(flow_code, flow_code)

        for record in records:
            trade_value = record.get("primaryValue") or record.get("tradeValue", 0)
            net_weight = record.get("netWgt", "?")
            partner = record.get("partnerDesc", "Bilinmeyen Partner")
            hs_desc = record.get("cmdDesc", f"HS {cmd_code}")

            raw_text = (
                f"{reporter_name} → {partner} | {period} | {flow_label.upper()} | "
                f"{hs_desc} (HS {cmd_code}) | "
                f"Değer: ${trade_value:,.0f} | Net Ağırlık: {net_weight} kg"
            )

            events.append(
                RawEvent(
                    source="UN Comtrade",
                    raw_text=raw_text,
                    timestamp=now,
                    trust_score=0.97,  # Resmi istatistik kaynağı
                    language="tr",
                    tags=["trade_flow", "comtrade", "official", f"hs_{cmd_code}"],
                    metadata={
                        "comtrade_record": record,
                        "reporter_code": reporter_code,
                        "flow_code": flow_code,
                        "period": period,
                    },
                )
            )

        logger.info("Comtrade: %d ticaret akışı olayı oluşturuldu.", len(events))
        return events

    # Geriye uyumluluk alias'ı — mevcut main.py çağrılarını kırmaz
    async def fetch_calcite_exports(self) -> List[RawEvent]:
        """Türkiye kalsit ihracatı (HS 283650) için önceden yapılandırılmış kısayol."""
        return await self.fetch_trade_flows(
            cmd_code="283650",
            reporter_code="792",
            period="2023",
            flow_code="X",
        )
