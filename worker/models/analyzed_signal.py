from datetime import datetime, timezone
from typing import List, Literal, Optional
import re

from pydantic import BaseModel, Field, field_validator


class AnalyzedSignal(BaseModel):
    """
    NEXUS Trade Radar — Analiz Edilmiş Sinyal (AnalyzedSignal)
    
    LLM (Yapay Zeka) tarafından ham olay metinlerinin (RawEvent) 
    Structured Output (Yapılandırılmış Çıktı) metodu ile 
    'Aksiyon Alınabilir İstihbarata' dönüştürülmüş halidir.
    """
    
    original_event_id: str = Field(
        ..., 
        description="Faz 1'den (RawEvent) gelen SHA-256 ID'si (İlişki kurmak için)."
    )
    
    relevance_score: int = Field(
        ..., 
        ge=0, 
        le=100, 
        description="0-100 arası B2B ticari istihbarat değer skoru."
    )
    
    sentiment: Literal["POSITIVE", "NEGATIVE", "NEUTRAL"] = Field(
        ..., 
        description="Haberin veya gelişmenin ticari duygu durumu (Sentiment)."
    )
    
    entities: List[str] = Field(
        default_factory=list, 
        description="Metinde geçen şirket isimleri, lokasyonlar, ürünler veya HS kodları."
    )
    
    executive_summary: str = Field(
        ..., 
        description="CEO'nun okuyacağı seviyede, konuyu özetleyen en fazla 2 cümlelik yönetici özeti."
    )
    
    actionable_insight: str = Field(
        ..., 
        description="'Bu durumda şirket olarak ne yapılmalı?' sorusunun tek cümlelik cevabı."
    )
    
    analyzed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Analizin yapıldığı UTC tabanlı zaman damgası."
    )
    
    source_url: str = Field(
        default="Yok",
        description="Haberin orijinal kaynağına ait URL adresi."
    )

    risk_category: Optional[str] = None
    affected_regions: Optional[List[str]] = Field(default_factory=list)
    relevant_hs_codes: Optional[List[str]] = Field(default_factory=list)

    @field_validator("risk_category", mode="before")
    @classmethod
    def validate_risk(cls, v):
        VALID = {"CUSTOMS_TARIFFS", "LOGISTICS", "GEOPOLITICAL", "SUPPLY_CHAIN", "TRADE_POLICY"}
        if v and isinstance(v, str) and v.strip().upper() in VALID:
            return v.strip().upper()
        return None

    @field_validator("affected_regions", "relevant_hs_codes", mode="before")
    @classmethod
    def coerce_to_list(cls, v):
        if isinstance(v, str):
            return [item.strip() for item in re.split(r"[,;]+", v) if item.strip()]
        return v if v else []
