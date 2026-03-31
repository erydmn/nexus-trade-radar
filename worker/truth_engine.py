"""
NEXUS Trade Radar - Truth Engine
Cross-verification of events from multiple sources.

Supports N kaynak: GDELT, NewsAPI, RSS feeds (Reuters/AP/Lloyd's/TradeWinds),
resmi kaynaklar (OFAC, EU Journal, IMO, WTO, Resmi Gazete) ve gelecekteki
herhangi bir kaynak.

verify_events() imzası geriye dönük uyumlu:
  - Eski: verify_events(gdelt_events, newsapi_events)
  - Yeni: verify_events(source_events={"gdelt": [...], "reuters_rss": [...]})
"""

from typing import List, Dict, Tuple, Optional
from datetime import datetime, timedelta, timezone
import logging

logger = logging.getLogger(__name__)


class TruthEngine:
    """
    N-kaynak cross-verification motoru.

    Kaynak güvenilirlik tablosu (SOURCE_TRUST):
      Resmi/yasal kaynaklar → 0.95–0.98
      Tier-1 haber ajansları → 0.88–0.92
      Sektör yayınları      → 0.78–0.82
      Genel OSINT           → 0.70–0.75

    Doğrulama mantığı:
      2+ kaynak eşleşmesi   → max(bireysel_skorlar) + boost
      Tek kaynak            → kaynak güvenilirlik skoru
      Resmi kaynak tek başına → minimum 0.90 (resmi yayın, doğrulama gereksiz)
    """

    # ─── Kaynak güvenilirlik skoru tablosu ────────────────────────────────────
    # Yeni kaynak eklendiğinde SADECE buraya satır ekle — başka değişiklik gerekmez
    SOURCE_TRUST: Dict[str, float] = {
        # Resmi / yasal kaynaklar (doğrulama gerektirmez)
        "imo_official":        0.98,
        "ofac_official":       0.97,
        "eu_official":         0.97,
        "resmi_gazete":        0.97,
        "wto_official":        0.95,
        "bm_official":         0.95,

        # Tier-1 haber ajansları
        "reuters_rss":         0.90,
        "ap_rss":              0.89,
        "ft_rss":              0.88,
        "bbc_business_rss":    0.85,

        # Denizcilik sektörü yayınları
        "lloyds_list_rss":     0.92,
        "tradewinds_rss":      0.82,
        "splash247_rss":       0.78,
        "hellenicshipping_rss": 0.76,

        # OSINT platformları
        "gdelt":               0.75,
        "newsapi":             0.70,
    }

    # Resmi kaynaklar: tek başına bile yüksek güven, cross-verify zorunlu değil
    OFFICIAL_SOURCES = frozenset({
        "imo_official", "ofac_official", "eu_official",
        "resmi_gazete", "wto_official", "bm_official",
    })

    # Cross-verification boost: 2+ kaynak eşleşince bu değer eklenir
    CROSS_VERIFY_BOOST = 0.05   # max 0.95 ile sınırlandırılır

    # ── Entity / stopword sets for _calculate_match_score (class-level) ───────
    _TRADE_ENTITIES: frozenset = frozenset({
        # Boğazlar / kanallar
        "suez", "panama", "hormuz", "bosphorus", "bab el-mandeb",
        "malacca", "dardanelles", "strait", "canal",
        # Büyük limanlar
        "shanghai", "rotterdam", "hamburg", "singapore", "dubai",
        "istanbul", "izmir", "mersin", "antwerp", "felixstowe",
        # Ticaret terimleri
        "red sea", "black sea", "persian gulf", "mediterranean",
        # Türkiye'ye özgü
        "kapikule", "habur", "gurbulak", "ipsala", "sarp",
    })
    _STOPWORDS: frozenset = frozenset({
        "the", "a", "an", "in", "on", "at", "to", "for", "of", "and", "or",
        "is", "are", "was", "were", "has", "have", "had", "be", "been",
        "reported", "continues", "amid", "over", "after", "due", "new",
        "update", "latest", "breaking", "says", "amid", "as", "its", "by",
    })

    def __init__(
        self,
        cross_verification_boost: float = 0.05,
        time_window_hours: int = 48,
        # Geriye dönük uyum için eski parametreler korundu
        gdelt_only_score: float = 0.75,
        newsapi_only_score: float = 0.70,
    ):
        self.cross_verify_boost = cross_verification_boost
        self.time_window = timedelta(hours=time_window_hours)
        # Eski sabit skorlar artık SOURCE_TRUST'tan geliyor,
        # ama constructor'dan override edilebilir
        self.SOURCE_TRUST = dict(self.SOURCE_TRUST)  # instance copy
        self.SOURCE_TRUST["gdelt"] = gdelt_only_score
        self.SOURCE_TRUST["newsapi"] = newsapi_only_score

    def verify_events(
        self,
        gdelt_events: Optional[List[Dict]] = None,
        newsapi_events: Optional[List[Dict]] = None,
        historical_context: Optional[List[Dict]] = None,
        # Yeni N-kaynak arayüzü
        source_events: Optional[Dict[str, List[Dict]]] = None,
    ) -> List[Dict]:
        """
        N kaynak cross-verification.

        Kullanım — Eski (geriye dönük uyumlu):
            engine.verify_events(gdelt_events=[...], newsapi_events=[...])

        Kullanım — Yeni (N kaynak):
            engine.verify_events(source_events={
                "gdelt":          [...],
                "newsapi":        [...],
                "reuters_rss":    [...],
                "lloyds_list_rss":[...],
                "ofac_official":  [...],
            })

        Truth Score Hiyerarşisi:
            Resmi tek kaynak              : SOURCE_TRUST[kaynak] (≥0.95)
            2+ kaynak eşleşmesi           : max(bireysel) + CROSS_VERIFY_BOOST, ≤0.97
            Tier-1 ajans tek              : 0.88–0.92
            Sektör yayını tek             : 0.76–0.82
            GDELT tek                     : 0.75
            NewsAPI tek                   : 0.70
        """
        # ── Geriye dönük uyum: eski çağrı biçimini normalize et ───────────────
        if source_events is None:
            source_events = {}
        if gdelt_events:
            source_events["gdelt"] = gdelt_events
        if newsapi_events:
            source_events["newsapi"] = newsapi_events

        if not source_events:
            return []

        total_in = sum(len(v) for v in source_events.values())
        logger.info("TruthEngine: cross-verifying %d events from %d sources: %s",
                    total_in, len(source_events), list(source_events.keys()))

        # ── Her kaynak event'ine "source_key" etiketi ekle ────────────────────
        tagged: Dict[str, List[Dict]] = {}
        for src_key, events in source_events.items():
            tagged[src_key] = []
            for ev in events:
                e = ev.copy()
                e.setdefault("_source_key", src_key)
                tagged[src_key].append(e)

        # ── Önce resmi kaynakları direkt ekle (cross-verify gerektirmez) ──────
        verified_events: List[Dict] = []
        skip_ids: set = set()          # Tüm kaynaklarda zaten işlenmiş event_id'ler
        matched_ids: set = set()       # Cross-verify sırasında eşleşen event_id'ler

        for src_key in self.OFFICIAL_SOURCES:
            for ev in tagged.get(src_key, []):
                ev_out = ev.copy()
                ev_out["truth_score"] = self.SOURCE_TRUST.get(src_key, 0.95)
                ev_out["verification_status"] = f"{src_key}_official"
                ev_out["match_confidence"] = 1.0
                ev_out["confirmed_by"] = [src_key]
                verified_events.append(ev_out)
                skip_ids.add(ev["event_id"])

        # ── GDELT birincil kaynak — diğerleriyle cross-verify ─────────────────
        gdelt_events_list = tagged.get("gdelt", [])
        other_sources = {k: v for k, v in tagged.items()
                         if k != "gdelt" and k not in self.OFFICIAL_SOURCES}

        for gdelt_ev in gdelt_events_list:
            if gdelt_ev["event_id"] in skip_ids:
                continue

            confirming_sources = []  # Bu event'i destekleyen diğer kaynaklar
            best_match_ev = None
            best_match_score = 0.0

            for src_key, src_events in other_sources.items():
                match, match_score = self._find_matching_event(
                    gdelt_ev, src_events, matched_ids
                )
                if match:
                    confirming_sources.append((src_key, match, match_score))
                    matched_ids.add(match["event_id"])
                    if match_score > best_match_score:
                        best_match_score = match_score
                        best_match_ev = (src_key, match)

            if confirming_sources:
                # Cross-verified: GDELT + en az 1 başka kaynak
                ev_out = gdelt_ev.copy()
                src_key_best, match_ev = best_match_ev
                ev_out = self._merge_events(ev_out, match_ev, src_key_best)

                base_score = max(
                    self.SOURCE_TRUST.get("gdelt", 0.75),
                    self.SOURCE_TRUST.get(src_key_best, 0.70)
                )
                ev_out["truth_score"] = min(0.97, base_score + self.cross_verify_boost)
                ev_out["verification_status"] = "dual_source"
                ev_out["match_confidence"] = best_match_score
                ev_out["confirmed_by"] = ["gdelt"] + [s for s, _, _ in confirming_sources]
            else:
                # GDELT tek kaynak
                ev_out = gdelt_ev.copy()
                ev_out["truth_score"] = self.SOURCE_TRUST.get("gdelt", 0.75)
                ev_out["verification_status"] = "gdelt_only"
                ev_out["match_confidence"] = 0.0
                ev_out["confirmed_by"] = ["gdelt"]

            verified_events.append(ev_out)
            skip_ids.add(gdelt_ev["event_id"])

        # ── Eşleşmemiş diğer kaynakları ekle (kendi truth_score ile) ─────────
        for src_key, src_events in other_sources.items():
            base_trust = self.SOURCE_TRUST.get(src_key, 0.70)
            for ev in src_events:
                if ev["event_id"] in skip_ids or ev["event_id"] in matched_ids:
                    continue
                ev_out = ev.copy()
                ev_out["truth_score"] = base_trust
                ev_out["verification_status"] = f"{src_key}_only"
                ev_out["match_confidence"] = 0.0
                ev_out["confirmed_by"] = [src_key]
                verified_events.append(ev_out)

        # ── Sırala: truth_score desc, timestamp desc ──────────────────────────
        # ISO 8601 strings sort lexicographically — no parsing needed
        verified_events.sort(
            key=lambda e: (e["truth_score"], e.get("timestamp", "")),
            reverse=True
        )

        # ── İstatistik logu ───────────────────────────────────────────────────
        dual = sum(1 for e in verified_events if e["verification_status"] == "dual_source")
        official = sum(1 for e in verified_events if "official" in e["verification_status"])
        single = len(verified_events) - dual - official
        logger.info("TruthEngine: %d verified (%d dual-source, %d official, %d single-source)",
                    len(verified_events), dual, official, single)

        if historical_context:
            verified_events = self._enrich_with_historical_context(
                verified_events, historical_context
            )

        return verified_events

    def _enrich_with_historical_context(
        self,
        events: List[Dict],
        historical_context: List[Dict]
    ) -> List[Dict]:
        """
        Enrich verified events with historical crisis benchmarks.

        For each event, find the nearest historical crisis by severity score
        and add historical_benchmark field. If a matching historical crisis
        is found for the same chokepoint, apply a minor truth_score boost
        (up to 0.02) as historical corroboration.

        Args:
            events: Verified events from GDELT/NewsAPI
            historical_context: Historical crises from historical_crisis_service

        Returns:
            Events enriched with historical_benchmark field
        """
        if not historical_context:
            return events

        # Build quick lookup by chokepoint
        cp_history: Dict[str, List[Dict]] = {}
        for hc in historical_context:
            cp = hc.get("chokepoint_id")
            if cp:
                cp_history.setdefault(cp, []).append(hc)

        # Pre-slice the fallback list once (used when no chokepoint match)
        fallback_candidates = historical_context[:50]

        enriched = []
        for event in events:
            ev = event.copy()
            ts = ev.get("truth_score", 0.70)
            chokepoint = (
                ev.get("location", {}).get("chokepoint_id") if isinstance(ev.get("location"), dict)
                else ev.get("chokepoint_id")
            )

            # Find nearest historical crisis
            target_score = ts  # Use truth_score as severity proxy
            nearest = None
            best_dist = 999.0

            candidates = cp_history.get(chokepoint, fallback_candidates)
            for hc in candidates:
                dist = abs(hc.get("severity_score", 0) - target_score)
                if dist < best_dist:
                    best_dist = dist
                    nearest = hc

            if nearest:
                bdi = nearest.get("impact_bdi_change_pct")
                ev["historical_benchmark"] = {
                    "event": nearest.get("description", "")[:120],
                    "year": nearest.get("year"),
                    "chokepoint": nearest.get("chokepoint_id"),
                    "severity_score": nearest.get("severity_score"),
                    "bdi_change": f"{bdi:+.1f}%" if bdi is not None else "N/A",
                    "duration_days": nearest.get("duration_days"),
                    "truth_score": nearest.get("truth_score"),
                    "verification_status": nearest.get("verification_status"),
                    "score_distance": round(best_dist, 3),
                }

                # Minor truth_score boost for corroborated events (same chokepoint)
                if chokepoint and chokepoint in cp_history and best_dist < 0.10:
                    boost = min(0.02, (0.10 - best_dist) * 0.2)
                    ev["truth_score"] = min(0.95, ts + boost)
                    ev["historical_corroboration"] = True

            enriched.append(ev)

        historical_enriched = sum(1 for e in enriched if e.get("historical_benchmark"))
        logger.info(f"Historical enrichment: {historical_enriched}/{len(enriched)} events enriched")

        return enriched

    def _find_matching_event(
        self,
        gdelt_event: Dict,
        candidate_events: List[Dict],
        exclude_ids: set
    ) -> Tuple[Dict, float]:
        """
        Find a matching event from any source for a GDELT event.

        Args:
            gdelt_event: GDELT event to match
            candidate_events: List of candidate events from another source
            exclude_ids: Set of already matched event IDs

        Returns:
            Tuple of (matching_event, match_score) or (None, 0.0)
        """
        gdelt_time = self._parse_timestamp(gdelt_event["timestamp"])

        best_match = None
        best_score = 0.0

        for news_event in candidate_events:
            if news_event["event_id"] in exclude_ids:
                continue

            # Time proximity check
            news_time = self._parse_timestamp(news_event["timestamp"])
            time_diff = abs(gdelt_time - news_time)

            if time_diff > self.time_window:
                continue

            # Calculate match score
            match_score = self._calculate_match_score(
                gdelt_event,
                news_event,
                time_diff
            )

            if match_score > best_score and match_score >= 0.6:  # Minimum threshold
                best_score = match_score
                best_match = news_event

        return (best_match, best_score) if best_match else (None, 0.0)

    def _calculate_match_score(
        self,
        event_a: Dict,
        event_b: Dict,
        time_diff: timedelta
    ) -> float:
        """
        Semantik + entity + temporal eşleşme skoru.

        Ağırlıklar:
          Entity match (liman/boğaz/şirket adı)  : 40%
          Title word overlap                       : 40%
          Mode match                               : 10%
          Time proximity                           : 10%
        """
        score = 0.0

        title_a = event_a.get("title", "").lower()
        title_b = event_b.get("title", "").lower()
        desc_a  = (event_a.get("description", "") or "").lower()
        desc_b  = (event_b.get("description", "") or "").lower()

        # ── Entity matching — aynı coğrafi/ticari entity varsa güçlü sinyal ──
        entity_hits = sum(
            1 for ent in self._TRADE_ENTITIES
            if ent in title_a and ent in title_b
        )
        # Aynı entity her iki başlıkta da → güçlü eşleşme
        if entity_hits >= 2:
            score += 0.40
        elif entity_hits == 1:
            score += 0.25

        # ── Title word overlap ─────────────────────────────────────────────────
        words_a = set(title_a.split()) - self._STOPWORDS
        words_b = set(title_b.split()) - self._STOPWORDS

        if words_a and words_b:
            overlap = len(words_a & words_b)
            title_sim = overlap / max(len(words_a), len(words_b))
            score += title_sim * 0.40

        # ── Mode match ────────────────────────────────────────────────────────
        if event_a.get("mode") == event_b.get("mode"):
            score += 0.10

        # ── Time proximity ────────────────────────────────────────────────────
        hours_diff = time_diff.total_seconds() / 3600
        time_score = max(0.0, 1.0 - (hours_diff / 48))
        score += time_score * 0.10

        return min(score, 1.0)

    def _merge_events(self, primary_event: Dict, secondary_event: Dict, secondary_source_key: str = "") -> Dict:
        """
        Primary event üzerine secondary kaynağın bağlamını ekle.
        Primary (GDELT) fiziksel olayı, secondary ekonomik/düzenleyici bağlamı taşır.
        """
        merged = primary_event.copy()

        merged["economic_context"] = {
            "title": secondary_event.get("title", ""),
            "description": secondary_event.get("description", ""),
            "source": secondary_event.get("raw_data", {}).get("source_name")
                      or secondary_event.get("source", secondary_source_key),
            "url": secondary_event.get("url", ""),
            "source_key": secondary_source_key,
        }

        # Severity upgrade: secondary kritikse ve primary değilse yükselt
        if secondary_event.get("severity") == "critical" and merged.get("severity") != "critical":
            merged["severity"] = "high"

        merged["cross_reference"] = {
            "primary_id":   primary_event["event_id"],
            "secondary_id": secondary_event["event_id"],
            "secondary_source": secondary_source_key,
        }

        return merged

    def _parse_timestamp(self, timestamp_str: str) -> datetime:
        """Parse ISO 8601 timestamp — always returns timezone-aware datetime."""
        try:
            return datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
        except Exception as e:
            logger.error("Failed to parse timestamp '%s': %s", timestamp_str, e)
            return datetime.now(timezone.utc)  # timezone-aware fallback
