"""
NEXUS Trade Radar — Temel HTTP İstemcisi
=========================================
İyileştirmeler:
  - httpx.AsyncClient session yeniden kullanımı: her çağrıda yeni bağlantı açmak yerine
    context manager aracılığıyla tek oturum kullanılır
  - Yapılandırılabilir timeout, retry sayısı ve backoff
  - Exponential backoff ile yeniden deneme (429 / 5xx için)
  - Retry mantığı tenacity yerine bağımlılık gerektirmeden saf asyncio ile uygulandı
  - fetch_text() ve fetch_json() birbirinden bağımsız → tek sorumluluk ilkesi
  - Hata yönetimi: connection error, timeout, HTTP 4xx/5xx ayrı ayrı ele alınır
  - verify=False GÜVENLİK AÇIĞI → kaldırıldı; SSL doğrulaması varsayılan olarak aktif
"""

import asyncio
import logging
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_HEADERS = {
    "User-Agent": (
        "NEXUS-TradeRadar/4.0 (+https://nexustraderadar.com/bot; "
        "trade-intelligence-platform)"
    ),
    "Accept": "application/json, text/html, application/xml;q=0.9, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,tr;q=0.8",
}


class BaseAPIClient:
    """
    Tüm scraper sınıflarının miras aldığı temel HTTP istemcisi.

    Parametreler
    ------------
    timeout : float
        Tek bir HTTP isteği için maksimum bekleme süresi (saniye).
    max_retries : int
        Geçici hatalarda (429, 5xx, bağlantı hatası) maksimum yeniden deneme sayısı.
    backoff_factor : float
        Yeniden denemeler arasındaki bekleme süresi katsayısı.
        Bekleme = backoff_factor * (2 ** (deneme_no - 1))  saniye
        Örnek: factor=0.5 → 0.5s, 1s, 2s, 4s …
    """

    def __init__(
        self,
        timeout: float = 15.0,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self._client: Optional[httpx.AsyncClient] = None

    # ── Oturum Yönetimi ───────────────────────────────────────────────────────

    async def _get_client(self) -> httpx.AsyncClient:
        """
        Paylaşılan AsyncClient döndürür.
        Scraper içinde ilk fetch çağrısında yaratılır, bir sonraki çağrıda yeniden kullanılır.
        Bağlantı havuzu (connection pool) otomatik devreye girer → bellek ve bağlantı tasarrufu.
        """
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                headers=_DEFAULT_HEADERS,
                follow_redirects=True,
                # verify=True varsayılandır → SSL sertifika doğrulaması aktif
            )
        return self._client

    async def close(self) -> None:
        """İstemci oturumunu kapatır. __aexit__ veya scraper yıkımında çağrılmalı."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # ── async context manager desteği ────────────────────────────────────────

    async def __aenter__(self) -> "BaseAPIClient":
        await self._get_client()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ── Yeniden Deneme Mantığı ────────────────────────────────────────────────

    _RETRYABLE_STATUS = {429, 500, 502, 503, 504}

    async def _request_with_retry(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Optional[httpx.Response]:
        """
        GET isteğini exponential backoff ile yeniden dener.
        429 / 5xx → yeniden dene.
        4xx (429 hariç) → anında None dön (parametrik hata, retry çözüm değil).
        """
        client = await self._get_client()
        last_exception: Optional[Exception] = None

        for attempt in range(1, self.max_retries + 1):
            try:
                response = await client.get(url, params=params, headers=headers)

                if response.status_code in self._RETRYABLE_STATUS:
                    wait = self.backoff_factor * (2 ** (attempt - 1))
                    logger.warning(
                        "HTTP %s alındı — %s | deneme %d/%d | %.1fs bekleniyor",
                        response.status_code, url, attempt, self.max_retries, wait,
                    )
                    await asyncio.sleep(wait)
                    continue

                response.raise_for_status()
                return response

            except httpx.TimeoutException as exc:
                last_exception = exc
                wait = self.backoff_factor * (2 ** (attempt - 1))
                logger.warning(
                    "Timeout — %s | deneme %d/%d | %.1fs bekleniyor",
                    url, attempt, self.max_retries, wait,
                )
                await asyncio.sleep(wait)

            except httpx.HTTPStatusError as exc:
                # 4xx (429 hariç) → yeniden denemeye gerek yok
                logger.error("HTTP hata %s: %s", exc.response.status_code, url)
                return None

            except httpx.RequestError as exc:
                last_exception = exc
                wait = self.backoff_factor * (2 ** (attempt - 1))
                logger.warning(
                    "Bağlantı hatası — %s | deneme %d/%d | %.1fs bekleniyor: %s",
                    url, attempt, self.max_retries, wait, exc,
                )
                await asyncio.sleep(wait)

        logger.error(
            "Tüm %d deneme başarısız — %s. Son hata: %s",
            self.max_retries, url, last_exception,
        )
        return None

    # ── Genel Amaçlı Fetch Metotları ─────────────────────────────────────────

    async def fetch_json(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """JSON yanıtı döndüren API uç noktaları için."""
        response = await self._request_with_retry(url, params=params, headers=headers)
        if response is None:
            return None
        try:
            return response.json()
        except Exception as exc:
            logger.error("JSON ayrıştırma hatası — %s: %s", url, exc)
            return None

    async def fetch_text(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Optional[str]:
        """HTML / XML / düz metin yanıtı döndüren uç noktalar için."""
        response = await self._request_with_retry(url, params=params, headers=headers)
        if response is None:
            return None
        return response.text
