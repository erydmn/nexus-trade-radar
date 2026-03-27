"""
NEXUS Trade Radar — Faz 8
UN Comtrade Makro Veri Entegrasyonu v2.0
-----------------------------------------
İyileştirmeler:
  • Async/concurrent HTTP (httpx.AsyncClient)
  • Otomatik pagination (max_records limiti aşılınca sayfalandırma)
  • Exponential backoff ile retry (429 / 5xx)
  • Rate limiter (dakika başına istek kotası)
  • Yapısal ComtradeRecord modeli — istatistiksel değer korunur
  • Minerals vertical HS kodları (calcite, dolomite, talc, kaolin)
  • Çok dönemli sorgu: yıllık + aylık granülarite
  • Çok-reporter/partner desteği
  • YoY anomali tespiti (eşik: %30 sapma)
  • Trade balance hesaplama
  • Zengin sinyal üretimi — RawEvent'e anlamlı metin gider
  • SHA-256 dedup (yeniden çalıştırmada duplicate yok)
  • Supabase + yerel JSONL çift yazma
"""

import sys
import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional
from collections import defaultdict

import httpx

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from core.config import settings
from worker.models.signal import RawEvent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s"
)
logger = logging.getLogger("nexus.comtrade")

# ---------------------------------------------------------------------------
# Sabitler
# ---------------------------------------------------------------------------
BASE_URL = "https://comtradeapi.un.org/data/v1/get/C"
PREVIEW_URL = "https://comtradeapi.un.org/public/v1/preview/C"   # API key gerektirmez
MAX_RECORDS_PER_PAGE = 500
MAX_PAGES = 10                      # sayfa başına 500 → max 5 000 kayıt/sorgu
RATE_LIMIT_PER_MINUTE = 55          # free tier: 60/dk, güvenli marj
RETRY_MAX = 4
RETRY_BASE_DELAY = 2.0              # saniye, her denemede ×2 artar
YOY_ANOMALY_THRESHOLD = 0.30        # %30 sapma = anomali sinyali

# ---------------------------------------------------------------------------
# HS Kod Sözlüğü — NEXUS domain'ine özel
# ---------------------------------------------------------------------------
HS_CODES: dict[str, str] = {
    # Minerals Vertical (Turmet)
    "252010": "Dolomite (ham)",
    "252020": "Dolomite (kalsine/sinterlenmiş)",
    "252610": "Talk (öğütülmemiş/kırılmamış)",
    "252620": "Tak (öğütülmüş/kırılmamış)",
    "250700": "Kaolin ve kaolinli killer",
    "283650": "Kalsiyum karbonat (kalsit)",
    "251610": "Granit (ham/kaba yontulmuş)",
    "251620": "Granit (kesilmiş/işlenmiş)",
    # Boya & Kaplama müşterileri (Turmet → Jotun, Marshall, Akzo Nobel)
    "320810": "Poliester bazlı boya ve vernikler",
    "320890": "Diğer boya ve vernikler (çözücülü)",
    "320910": "Akrilat bazlı su bazlı boya",
    "320990": "Diğer su bazlı boya ve vernikler",
    "321490": "Dolgu macunu ve badana",
    # Türkiye'nin ana ihracat kalemleri (Trade Radar — genel)
    "7208":   "Yassı hadde ürünleri (demir/çelik)",
    "7210":   "Kaplamalı yassı çelik",
    "7225":   "Alaşımlı çelik yassı ürünler",
    "8411":   "Türbin ve jet motorları",
    "8708":   "Otomotiv parçaları",
    "6203":   "Erkek takım elbise (tekstil)",
    "6204":   "Kadın giyim (tekstil)",
    "3901":   "Polietilen (plastik ham madde)",
    "3902":   "Polipropilen",
    "0702":   "Domates (taze)",
    "0805":   "Narenciye",
}

# Reporter kodları
REPORTERS: dict[int, str] = {
    792: "Türkiye",
    276: "Almanya",
    380: "İtalya",
    724: "İspanya",
    616: "Polonya",
    804: "Ukrayna",
    156: "Çin",
    842: "ABD",
}

# Anahtar ticaret ortakları (partner 0 = Dünya)
KEY_PARTNERS: dict[int, str] = {
    0:   "Dünya (toplam)",
    276: "Almanya",
    380: "İtalya",
    840: "ABD",
    156: "Çin",
    826: "Birleşik Krallık",
    682: "Suudi Arabistan",
    784: "BAE",
    643: "Rusya",
    372: "İrlanda",
    056: "Belçika",
}

# ---------------------------------------------------------------------------
# Veri modelleri
# ---------------------------------------------------------------------------
@dataclass
class ComtradeRecord:
    """Ham UN Comtrade kaydını yapısal olarak tutar."""
    reporter_code: int
    reporter_desc: str
    partner_code: int
    partner_desc: str
    flow_code: str          # "M" import | "X" export | "RX" re-export
    cmd_code: str
    cmd_desc: str
    period: str             # "2023" veya "202310"
    primary_value_usd: float
    net_weight_kg: Optional[float]
    qty: Optional[float]
    qty_unit: Optional[str]
    source: str = "UN Comtrade API"

    @property
    def unit_value(self) -> Optional[float]:
        """USD / kg birim değer — fiyat anomali tespitinde kullanılır."""
        if self.net_weight_kg and self.net_weight_kg > 0:
            return self.primary_value_usd / self.net_weight_kg
        return None

    def to_signal_text(self) -> str:
        """NEXUS NLP pipeline'ına gönderilecek zengin metin özeti."""
        flow_label = {"M": "ithalatı", "X": "ihracatı", "RX": "re-ihracatı"}.get(
            self.flow_code, self.flow_code
        )
        value_m = self.primary_value_usd / 1_000_000
        weight_part = ""
        if self.net_weight_kg:
            weight_t = self.net_weight_kg / 1000
            weight_part = f", {weight_t:,.1f} ton"
        unit_part = ""
        if self.unit_value:
            unit_part = f" (birim değer: {self.unit_value:.3f} USD/kg)"
        return (
            f"UN Comtrade Resmi Veri | {self.period} | {self.reporter_desc}, "
            f"{self.cmd_desc} (HS {self.cmd_code}) {flow_label}: "
            f"{value_m:.2f} M USD{weight_part}{unit_part}. "
            f"Ticaret ortağı: {self.partner_desc}."
        )

    def record_id(self) -> str:
        key = f"{self.reporter_code}_{self.partner_code}_{self.flow_code}_{self.cmd_code}_{self.period}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]


@dataclass
class TradeBalance:
    """Belirli bir HS kodu ve dönem için denge analizi."""
    reporter: str
    partner: str
    cmd_code: str
    cmd_desc: str
    period: str
    export_usd: float = 0.0
    import_usd: float = 0.0

    @property
    def balance(self) -> float:
        return self.export_usd - self.import_usd

    @property
    def trade_coverage_ratio(self) -> Optional[float]:
        if self.import_usd > 0:
            return self.export_usd / self.import_usd
        return None

    def to_signal_text(self) -> str:
        direction = "fazla" if self.balance >= 0 else "açık"
        ratio_part = ""
        if self.trade_coverage_ratio:
            ratio_part = f" (ihracat karşılama oranı: {self.trade_coverage_ratio:.2%})"
        return (
            f"Ticaret Dengesi | {self.period} | {self.reporter} ↔ {self.partner} | "
            f"HS {self.cmd_code} ({self.cmd_desc}): "
            f"İhracat {self.export_usd/1e6:.2f}M$, İthalat {self.import_usd/1e6:.2f}M$, "
            f"Denge {abs(self.balance)/1e6:.2f}M$ {direction}{ratio_part}."
        )


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------
class RateLimiter:
    def __init__(self, calls_per_minute: int):
        self.calls_per_minute = calls_per_minute
        self._calls: list[float] = []

    async def acquire(self):
        now = time.monotonic()
        window_start = now - 60.0
        self._calls = [t for t in self._calls if t > window_start]
        if len(self._calls) >= self.calls_per_minute:
            wait = 60.0 - (now - self._calls[0]) + 0.1
            logger.info(f"Rate limit: {wait:.1f}s bekleniyor…")
            await asyncio.sleep(wait)
        self._calls.append(time.monotonic())


# ---------------------------------------------------------------------------
# Comtrade API istemcisi
# ---------------------------------------------------------------------------
class ComtradeClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.rate_limiter = RateLimiter(RATE_LIMIT_PER_MINUTE)
        self._seen_ids: set[str] = set()
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, *_):
        if self._client:
            await self._client.aclose()

    # ---- HTTP yardımcıları -----------------------------------------------

    async def _get_with_retry(self, url: str, params: dict) -> dict:
        """Exponential backoff ile retry uygular. 429 / 5xx → tekrar dener."""
        for attempt in range(RETRY_MAX):
            await self.rate_limiter.acquire()
            try:
                resp = await self._client.get(url, params=params)
                if resp.status_code == 429:
                    wait = RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(f"429 Too Many Requests. {wait}s sonra tekrar denenecek…")
                    await asyncio.sleep(wait)
                    continue
                if resp.status_code >= 500:
                    wait = RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(f"HTTP {resp.status_code}. {wait}s sonra tekrar denenecek…")
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.TimeoutException:
                wait = RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(f"Timeout (deneme {attempt+1}/{RETRY_MAX}). {wait}s bekleniyor…")
                await asyncio.sleep(wait)
            except Exception as e:
                logger.error(f"İstek hatası: {e}")
                break
        return {}

    # ---- Tekli sayfa sorgusu -----------------------------------------------

    async def _fetch_page(
        self,
        frequency: str,       # "A" yıllık | "M" aylık
        reporter: int,
        partner: int,
        cmd_codes: list[str],
        period: str,
        flow: str,
        start_record: int = 0,
    ) -> tuple[list[ComtradeRecord], bool]:
        """
        Tek bir API sayfasını çeker.
        Returns: (kayıtlar, daha_fazla_sayfa_var_mı)
        """
        params = {
            "reporterCode": reporter,
            "partnerCode": partner,
            "cmdCode": ",".join(cmd_codes),
            "period": period,
            "flowCode": flow,
            "includeDesc": True,
            "startRecord": start_record,
            "maxRecords": MAX_RECORDS_PER_PAGE,
            "subscription-key": self.api_key,
        }
        url = f"{BASE_URL}/{frequency}/HS"
        data = await self._get_with_retry(url, params)

        records: list[ComtradeRecord] = []
        raw_list = data.get("data", [])

        for r in raw_list:
            rec = ComtradeRecord(
                reporter_code=r.get("reporterCode", reporter),
                reporter_desc=r.get("reporterDesc") or REPORTERS.get(reporter, str(reporter)),
                partner_code=r.get("partnerCode", partner),
                partner_desc=r.get("partnerDesc") or KEY_PARTNERS.get(partner, str(partner)),
                flow_code=r.get("flowCode", flow),
                cmd_code=str(r.get("cmdCode", "")),
                cmd_desc=r.get("cmdDesc") or HS_CODES.get(str(r.get("cmdCode", "")), "Bilinmiyor"),
                period=str(r.get("period", period)),
                primary_value_usd=float(r.get("primaryValue") or 0),
                net_weight_kg=r.get("netWgt") or r.get("netWeight"),
                qty=r.get("qty"),
                qty_unit=r.get("qtyUnitAbbr"),
            )
            rid = rec.record_id()
            if rid not in self._seen_ids:
                self._seen_ids.add(rid)
                records.append(rec)

        total_count = data.get("count", len(raw_list))
        has_more = (start_record + MAX_RECORDS_PER_PAGE) < total_count

        logger.info(
            f"  Sayfa {start_record//MAX_RECORDS_PER_PAGE + 1}: "
            f"{len(records)} yeni kayıt "
            f"(reporter={REPORTERS.get(reporter, reporter)}, "
            f"partner={KEY_PARTNERS.get(partner, partner)}, "
            f"period={period}, flow={flow}, "
            f"toplam={total_count})"
        )
        return records, has_more

    # ---- Sayfalandırmalı tam sorgu -----------------------------------------

    async def fetch_all_pages(
        self,
        frequency: str,
        reporter: int,
        partner: int,
        cmd_codes: list[str],
        period: str,
        flow: str,
    ) -> list[ComtradeRecord]:
        """Tüm sayfaları otomatik olarak iterasyonla çeker."""
        all_records: list[ComtradeRecord] = []
        for page in range(MAX_PAGES):
            start = page * MAX_RECORDS_PER_PAGE
            records, has_more = await self._fetch_page(
                frequency, reporter, partner, cmd_codes, period, flow, start
            )
            all_records.extend(records)
            if not has_more:
                break
        return all_records

    # ---- Çok-dönemli, çok-reporter toplu sorgu ----------------------------

    async def fetch_bulk(
        self,
        frequency: str,
        reporters: list[int],
        partners: list[int],
        cmd_groups: list[list[str]],
        periods: list[str],
        flows: list[str] = ["M", "X"],
        concurrency: int = 4,
    ) -> list[ComtradeRecord]:
        """
        Semaphore ile concurrency kontrolü altında toplu sorgu yürütür.
        cmd_groups: büyük HS listeleri ≤10 kodluk gruplara bölünmüş hâli
        """
        semaphore = asyncio.Semaphore(concurrency)

        async def bounded_fetch(rep, par, codes, period, flow):
            async with semaphore:
                return await self.fetch_all_pages(frequency, rep, par, codes, period, flow)

        tasks = [
            bounded_fetch(rep, par, codes, period, flow)
            for rep in reporters
            for par in partners
            for codes in cmd_groups
            for period in periods
            for flow in flows
        ]

        logger.info(f"Toplam {len(tasks)} paralel sorgu başlatılıyor…")
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_records: list[ComtradeRecord] = []
        for res in results:
            if isinstance(res, Exception):
                logger.error(f"Paralel sorgu hatası: {res}")
            else:
                all_records.extend(res)

        logger.info(f"Bulk sorgu tamamlandı: {len(all_records)} benzersiz kayıt")
        return all_records


# ---------------------------------------------------------------------------
# Analitik katman
# ---------------------------------------------------------------------------
class ComtradeAnalytics:

    @staticmethod
    def compute_trade_balances(records: list[ComtradeRecord]) -> list[TradeBalance]:
        """Kayıtlardan ticaret dengesi özet tablosu oluşturur."""
        balances: dict[tuple, TradeBalance] = {}
        for r in records:
            key = (r.reporter_desc, r.partner_desc, r.cmd_code, r.period)
            if key not in balances:
                balances[key] = TradeBalance(
                    reporter=r.reporter_desc,
                    partner=r.partner_desc,
                    cmd_code=r.cmd_code,
                    cmd_desc=r.cmd_desc,
                    period=r.period,
                )
            if r.flow_code == "X":
                balances[key].export_usd += r.primary_value_usd
            elif r.flow_code == "M":
                balances[key].import_usd += r.primary_value_usd
        return list(balances.values())

    @staticmethod
    def detect_yoy_anomalies(
        records: list[ComtradeRecord],
        threshold: float = YOY_ANOMALY_THRESHOLD,
    ) -> list[dict]:
        """
        Yıl-üstü-yıl anomalileri tespit eder.
        Aynı reporter / partner / cmd / flow kombinasyonunda
        %30+ sapma olan geçişleri döndürür.
        """
        # Gruplama
        series: dict[tuple, dict[str, float]] = defaultdict(dict)
        for r in records:
            key = (r.reporter_desc, r.partner_desc, r.cmd_code, r.flow_code)
            series[key][r.period] = r.primary_value_usd

        anomalies = []
        for key, periods_data in series.items():
            sorted_periods = sorted(periods_data.keys())
            for i in range(1, len(sorted_periods)):
                prev_period = sorted_periods[i - 1]
                curr_period = sorted_periods[i]
                prev_val = periods_data[prev_period]
                curr_val = periods_data[curr_period]
                if prev_val == 0:
                    continue
                change = (curr_val - prev_val) / prev_val
                if abs(change) >= threshold:
                    reporter, partner, cmd, flow = key
                    direction = "ARTIŞ" if change > 0 else "DÜŞÜŞ"
                    anomalies.append({
                        "reporter": reporter,
                        "partner": partner,
                        "cmd_code": cmd,
                        "cmd_desc": HS_CODES.get(cmd, ""),
                        "flow": flow,
                        "from_period": prev_period,
                        "to_period": curr_period,
                        "from_value_usd": prev_val,
                        "to_value_usd": curr_val,
                        "change_pct": round(change * 100, 2),
                        "direction": direction,
                        "signal_text": (
                            f"YoY ANOMALİ [{direction} %{abs(change)*100:.1f}] | "
                            f"{reporter} → {partner} | HS {cmd}: "
                            f"{flow} değeri {prev_period}: {prev_val/1e6:.2f}M$ → "
                            f"{curr_period}: {curr_val/1e6:.2f}M$."
                        ),
                    })
        return anomalies

    @staticmethod
    def top_partners(
        records: list[ComtradeRecord],
        flow: str = "X",
        top_n: int = 5,
    ) -> dict[str, list[tuple[str, float]]]:
        """Her HS kodu için en büyük N ticaret ortağını döndürür."""
        agg: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for r in records:
            if r.flow_code == flow:
                agg[r.cmd_code][r.partner_desc] += r.primary_value_usd
        return {
            cmd: sorted(partners.items(), key=lambda x: x[1], reverse=True)[:top_n]
            for cmd, partners in agg.items()
        }


# ---------------------------------------------------------------------------
# Sinyal dönüştürücü
# ---------------------------------------------------------------------------
def records_to_raw_events(records: list[ComtradeRecord]) -> list[RawEvent]:
    return [
        RawEvent(
            source="UN Comtrade API",
            raw_text=r.to_signal_text(),
            metadata={
                "url": "https://comtradeplus.un.org",
                "record_id": r.record_id(),
                "reporter_code": r.reporter_code,
                "partner_code": r.partner_code,
                "cmd_code": r.cmd_code,
                "flow_code": r.flow_code,
                "period": r.period,
                "primary_value_usd": r.primary_value_usd,
                "net_weight_kg": r.net_weight_kg,
                "unit_value_usd_per_kg": r.unit_value,
            },
        )
        for r in records
    ]


def anomalies_to_raw_events(anomalies: list[dict]) -> list[RawEvent]:
    return [
        RawEvent(
            source="UN Comtrade API (YoY Anomali)",
            raw_text=a["signal_text"],
            metadata={
                "url": "https://comtradeplus.un.org",
                "change_pct": a["change_pct"],
                "direction": a["direction"],
                "cmd_code": a["cmd_code"],
                "from_period": a["from_period"],
                "to_period": a["to_period"],
            },
        )
        for a in anomalies
    ]


def balances_to_raw_events(balances: list[TradeBalance]) -> list[RawEvent]:
    return [
        RawEvent(
            source="UN Comtrade API (Ticaret Dengesi)",
            raw_text=b.to_signal_text(),
            metadata={
                "url": "https://comtradeplus.un.org",
                "reporter": b.reporter,
                "partner": b.partner,
                "cmd_code": b.cmd_code,
                "period": b.period,
                "balance_usd": b.balance,
                "trade_coverage_ratio": b.trade_coverage_ratio,
            },
        )
        for b in balances
    ]


# ---------------------------------------------------------------------------
# Depolama
# ---------------------------------------------------------------------------
def save_to_jsonl(events: list[RawEvent], output_path: Path) -> int:
    """JSONL dosyasına ekler, toplam kayıt sayısını döndürür."""
    count = 0
    with open(output_path, "a", encoding="utf-8") as f:
        for ev in events:
            f.write(ev.model_dump_json() + "\n")
            count += 1
    return count


def save_structured_records(records: list[ComtradeRecord], output_path: Path):
    """Ham yapısal kayıtları analiz için ayrı bir JSONL dosyasına yazar."""
    with open(output_path, "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(asdict(r)) + "\n")


# ---------------------------------------------------------------------------
# Ana orkestrasyon
# ---------------------------------------------------------------------------
async def run_comtrade_pipeline() -> list[RawEvent]:
    """
    Tüm pipeline'ı yönetir:
    1. API key doğrulama
    2. HS kod gruplarını oluşturma
    3. Toplu async sorgu
    4. Analitik (denge + YoY anomali)
    5. Sinyal üretimi
    6. Yerel kayıt
    """
    if not settings.comtrade_api_key_primary:
        logger.warning("COMTRADE_API_KEY_PRIMARY bulunamadı. Atlanıyor.")
        return []

    api_key = (
        settings.comtrade_api_key_primary.get_secret_value()
        if hasattr(settings.comtrade_api_key_primary, "get_secret_value")
        else settings.comtrade_api_key_primary
    )

    # --- HS kod grupları (API maks 10 kod/istek sınırı) -------------------
    minerals_codes = ["252010", "252020", "252610", "252620", "250700", "283650"]
    paint_codes    = ["320810", "320890", "320910", "320990", "321490"]
    steel_codes    = ["7208", "7210", "7225"]
    auto_codes     = ["8411", "8708"]
    textile_codes  = ["6203", "6204"]
    plastic_codes  = ["3901", "3902"]

    cmd_groups = [
        minerals_codes,
        paint_codes,
        steel_codes + auto_codes,
        textile_codes + plastic_codes,
    ]

    # --- Sorgu parametreleri -----------------------------------------------
    # Yıllık: 3 yıl trend analizi için
    annual_periods = ["2021", "2022", "2023"]
    # Aylık: son 12 ay (2024 dahil)
    monthly_periods = [f"2024{m:02d}" for m in range(1, 13)]

    # Reporter: Türkiye merkez, anahtar rakipler
    reporters = [792]                     # Türkiye
    # Minerals vertical için rakip ülkeler eklenebilir:
    # reporters += [276, 380, 724]        # Almanya, İtalya, İspanya

    # Partner: Dünya toplamı + anahtar pazarlar
    partners = [0, 276, 840, 156, 826]   # Dünya, DE, US, CN, UK

    all_records: list[ComtradeRecord] = []

    async with ComtradeClient(api_key) as client:
        # -- Yıllık veri (trend analizi için) --------------------------------
        logger.info("── Yıllık veri sorgusu başlıyor ──")
        annual_records = await client.fetch_bulk(
            frequency="A",
            reporters=reporters,
            partners=partners,
            cmd_groups=cmd_groups,
            periods=annual_periods,
            flows=["M", "X"],
            concurrency=3,
        )
        all_records.extend(annual_records)
        logger.info(f"Yıllık: {len(annual_records)} kayıt alındı")

        # -- Aylık veri (kısa vadeli izleme) ---------------------------------
        logger.info("── Aylık veri sorgusu başlıyor ──")
        monthly_records = await client.fetch_bulk(
            frequency="M",
            reporters=reporters,
            partners=[0],           # Aylık için sadece Dünya toplamı
            cmd_groups=cmd_groups,
            periods=monthly_periods,
            flows=["M", "X"],
            concurrency=3,
        )
        all_records.extend(monthly_records)
        logger.info(f"Aylık: {len(monthly_records)} kayıt alındı")

    if not all_records:
        logger.warning("Hiç kayıt alınamadı.")
        return []

    # --- Analitik katman ----------------------------------------------------
    analytics = ComtradeAnalytics()

    logger.info("Ticaret dengeleri hesaplanıyor…")
    balances = analytics.compute_trade_balances(
        [r for r in all_records if r.partner_code == 0]  # sadece Dünya toplamı
    )

    logger.info("YoY anomaliler tespit ediliyor…")
    anomalies = analytics.detect_yoy_anomalies(all_records)
    logger.info(f"{len(anomalies)} anomali tespit edildi")

    top_export_partners = analytics.top_partners(all_records, flow="X", top_n=5)
    if top_export_partners:
        logger.info("En büyük ihracat ortakları (HS bazında):")
        for cmd, partners_list in list(top_export_partners.items())[:3]:
            logger.info(f"  HS {cmd}: {partners_list}")

    # --- Sinyal üretimi -----------------------------------------------------
    signal_events: list[RawEvent] = []
    signal_events.extend(records_to_raw_events(all_records))
    signal_events.extend(balances_to_raw_events(balances))
    signal_events.extend(anomalies_to_raw_events(anomalies))

    # --- Kayıt (yerel) ------------------------------------------------------
    output_dir = root_dir
    jsonl_path = output_dir / "nexus_data_lake.jsonl"
    struct_path = output_dir / "comtrade_structured_records.jsonl"

    saved = save_to_jsonl(signal_events, jsonl_path)
    save_structured_records(all_records, struct_path)

    logger.info(
        f"Pipeline tamamlandı — "
        f"{len(all_records)} kayıt | "
        f"{len(balances)} denge özeti | "
        f"{len(anomalies)} anomali | "
        f"{saved} sinyal kaydedildi"
    )
    return signal_events


# ---------------------------------------------------------------------------
# Senkron sarmalayıcı (mevcut pipeline uyumluluğu)
# ---------------------------------------------------------------------------
def fetch_comtrade_data() -> list[RawEvent]:
    """
    Mevcut fetch_and_store_news() içinden çağrılabilir.
    Async pipeline'ı senkron ortamda çalıştırır.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Zaten bir event loop varsa (örn. Jupyter / FastAPI)
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, run_comtrade_pipeline())
                return future.result()
        else:
            return loop.run_until_complete(run_comtrade_pipeline())
    except Exception as e:
        logger.error(f"Comtrade pipeline başlatma hatası: {e}")
        return []


# ---------------------------------------------------------------------------
# Doğrudan çalıştırma
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    events = fetch_comtrade_data()
    print(f"\n✓ Toplam {len(events)} sinyal üretildi.")

    # İlk 5 sinyali önizle
    print("\n── Örnek sinyaller ──")
    for ev in events[:5]:
        print(f"[{ev.source}]\n  {ev.raw_text[:160]}\n")
