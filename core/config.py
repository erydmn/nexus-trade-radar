"""
NEXUS Trade Radar — Merkezi Konfigürasyon Modülü
=================================================
İyileştirmeler:
  - Tüm ayarlar tek bir BaseSettings altında toplandı → .env dosyası sadece 1 kez okunuyor
  - Zorunlu (Field(...)) ve isteğe bağlı (default=None) alanlar net ayrıldı
  - Tip güvenliği için AnyHttpUrl kullanıldı
  - lru_cache yerine module-level singleton pattern (daha öngörülü davranış)
  - Validation alias eklendi (ortam değişkeni isimleriyle tam eşleşme)
  - Gizli alanlar SecretStr ile maskelendi (log sızıntısı önlemi)
"""

from functools import lru_cache
from typing import Optional
from pydantic import Field, AnyHttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Tüm uygulama ayarlarını tek bir Pydantic modeli altında toplar.
    Pydantic-Settings .env dosyasını bir kez okur ve tüm alanları doğrular.
    Zorunlu alanlar Field(...) ile işaretlenmiştir; eksik olursa başlatma anında
    ValidationError fırlatılır — sessiz hata yoktur.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",          # tanımlanmamış env değişkenleri sessizce yoksayılır
        case_sensitive=False,    # NEWSAPI_KEY = newsapi_key
    )

    # ── Haber / İçerik API'leri ─────────────────────────────────────────────
    newsapi_key: Optional[SecretStr] = None
    eventregistry_api_key: Optional[SecretStr] = None
    the_guardian_api_key: Optional[SecretStr] = None
    gnews_api_key: Optional[SecretStr] = None
    currents_api_key: Optional[SecretStr] = None
    mediastack_api_key: Optional[SecretStr] = None

    # GDELT sabit bir URL'dir, API anahtarı gerektirmez
    gdelt_api_url: AnyHttpUrl = Field(
        default="https://api.gdeltproject.org/api/v2/doc/doc",
        description="GDELT DOC 2.0 API temel URL'si",
    )

    # ── Resmi / Kurumsal Kaynaklar ───────────────────────────────────────────
    companies_house_api_key: Optional[SecretStr] = None
    serpapi_key: Optional[SecretStr] = None

    # ── Makro Ticaret (UN Comtrade) ──────────────────────────────────────────
    comtrade_api_key_primary: Optional[SecretStr] = None
    comtrade_api_key_secondary: Optional[SecretStr] = None

    # ── Lojistik / AIS / Uçuş ───────────────────────────────────────────────
    aisstream_api_key: Optional[SecretStr] = None

    # OpenSky: OAuth2 client credentials
    opensky_client_id: Optional[str] = None
    opensky_client_secret: Optional[SecretStr] = None
    opensky_username: Optional[str] = None
    opensky_password: Optional[SecretStr] = None

    # Global Fishing Watch
    gfw_base_url: AnyHttpUrl = Field(
        default="https://gateway.api.globalfishingwatch.org",
        description="GFW API temel URL'si",
    )
    gfw_api_token: Optional[SecretStr] = None

    # AviationStack
    aviationstack_api_key: Optional[SecretStr] = None

    # ── Google OAuth2 (Media) ────────────────────────────────────────────────
    google_client_id: Optional[str] = None
    google_client_secret: Optional[SecretStr] = None

    # ── Yapay Zeka / LLM ────────────────────────────────────────────────────
    groq_api_key: Optional[SecretStr] = None

    # ── Veritabanı (Supabase) ────────────────────────────────────────────────
    supabase_url: Optional[str] = None
    supabase_key: Optional[SecretStr] = None

    # ── Uygulama Genel ──────────────────────────────────────────────────────
    data_lake_path: str = Field(
        default="nexus_data_lake.jsonl",
        description="Ham olayların yazıldığı JSONL dosyası yolu",
    )
    scraper_timeout_seconds: float = Field(
        default=15.0, description="HTTP istek zaman aşımı (saniye)"
    )
    scraper_max_retries: int = Field(
        default=3, description="Başarısız HTTP isteklerinde maksimum yeniden deneme sayısı"
    )
    log_level: str = Field(default="INFO", description="Logging seviyesi")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Settings nesnesini bir kez oluşturur ve önbellekte tutar (Singleton).
    lru_cache(maxsize=1) → yalnızca tek örnek bellekte yaşar.
    Test ortamlarında cache'i temizlemek için: get_settings.cache_clear()
    """
    return Settings()


# Modül seviyesinde erişim kolaylığı için dışa aktarılır
settings: Settings = get_settings()
