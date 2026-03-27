"""
NEXUS Trade Radar — Ham Olay (RawEvent) Veri Modeli
====================================================
İyileştirmeler:
  - timestamp str yerine datetime tipinde → otomatik ISO 8601 doğrulaması
  - trust_score alanı eklendi (pipeline genelinde kaynak güvenilirliği taşınır)
  - event_id alanı eklendi: kaynak + timestamp SHA-256 hash'i → veri gölünde tekrar yazımı önler
  - language alanı eklendi: çok dilli pipeline için zorunlu
  - tags alanı eklendi: kaynak konfigürasyonundan aktarılan etiketler
  - model_config: json_encoders ile datetime → ISO string dönüşümü standardize edildi
  - Yardımcı sınıf method: from_source_config() → kaynak config dict'ten hızlı oluşturma
"""

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, computed_field, model_validator


class RawEvent(BaseModel):
    """
    Pipeline boyunca taşınan ham istihbarat olayı.
    Tüm scraperlar bu modeli döndürür; downstream işlemciler bu modeli tüketir.
    """

    source: str = Field(..., description="Kaynak sistem adı (ör. 'NewsAPI', 'UN Comtrade')")
    raw_text: str = Field(..., description="Ham, işlenmemiş metin içeriği")
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Olayın UTC zaman damgası",
    )
    trust_score: float = Field(
        default=0.70,
        ge=0.0,
        le=1.0,
        description="Kaynak güvenilirlik skoru [0.0 – 1.0]",
    )
    language: str = Field(default="en", description="İçerik dili (ISO 639-1)")
    tags: List[str] = Field(default_factory=list, description="Sınıflandırma etiketleri")
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Kaynağa özgü ek veriler (link, HS kodu, IMO numarası vb.)",
    )

    # ── Hesaplanan Alan: Tekil ID ─────────────────────────────────────────────
    @computed_field  # type: ignore[misc]
    @property
    def event_id(self) -> str:
        """
        source + raw_text + timestamp → SHA-256 → 16 hex karakter
        Veri gölüne aynı olayın iki kez yazılmasını engeller.
        Downstream'de upsert / deduplication için primary key görevi görür.
        """
        payload = f"{self.source}|{self.raw_text}|{self.timestamp.isoformat()}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    # ── Doğrulama ─────────────────────────────────────────────────────────────
    @model_validator(mode="after")
    def ensure_utc_timestamp(self) -> "RawEvent":
        """Zaman dilimi bilgisi eksik timestamp'leri UTC olarak işaretle."""
        if self.timestamp.tzinfo is None:
            object.__setattr__(
                self, "timestamp", self.timestamp.replace(tzinfo=timezone.utc)
            )
        return self

    # ── Serileştirme ──────────────────────────────────────────────────────────
    model_config = {
        "json_encoders": {datetime: lambda v: v.isoformat()},
        "populate_by_name": True,
    }

    # ── Fabrika Yardımcıları ──────────────────────────────────────────────────
    @classmethod
    def error_event(cls, source: str, reason: str, metadata: Optional[Dict[str, Any]] = None) -> "RawEvent":
        """
        Başarısız scraper çalışmaları için standart hata olayı üretir.
        downstream'de 'error' tag'i ile filtrelenebilir.
        """
        return cls(
            source=source,
            raw_text=f"[SCRAPER_ERROR] {reason}",
            trust_score=0.0,
            tags=["error"],
            metadata=metadata or {},
        )
