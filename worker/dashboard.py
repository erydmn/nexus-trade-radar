import sys
from pathlib import Path

# Add project root to Python path so it can find the 'core' module
root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))

import pandas as pd
import streamlit as st
from core.db import get_supabase_client

# Config and Title
st.set_page_config(page_title="NEXUS Trade Radar", layout="wide", page_icon="🌐")

# ── Custom CSS for Truth Engine badges & Comtrade cards ──────────────────────
st.markdown("""
<style>
    .truth-badge {
        display: inline-block;
        padding: 2px 10px;
        border-radius: 12px;
        font-size: 0.8rem;
        font-weight: 600;
        margin-right: 6px;
    }
    .truth-high   { background: #1B5E20; color: #A5D6A7; }
    .truth-medium { background: #E65100; color: #FFE0B2; }
    .truth-low    { background: #B71C1C; color: #FFCDD2; }
    .badge-dual   { background: #0D47A1; color: #BBDEFB; }
    .badge-official { background: #4A148C; color: #E1BEE7; }
    .badge-single { background: #37474F; color: #CFD8DC; }
    .comtrade-card {
        border-left: 4px solid #1565C0 !important;
        background: linear-gradient(90deg, rgba(21,101,192,0.06) 0%, transparent 40%) !important;
    }
    .source-tag {
        display: inline-block;
        padding: 1px 8px;
        border-radius: 8px;
        font-size: 0.72rem;
        font-weight: 500;
    }
    .src-comtrade { background: #0D47A1; color: #BBDEFB; }
    .src-gdelt    { background: #2E7D32; color: #C8E6C9; }
    .src-rss      { background: #F57F17; color: #FFF9C4; }
    .src-default  { background: #455A64; color: #CFD8DC; }
</style>
""", unsafe_allow_html=True)

# Load Data Function
@st.cache_data(ttl=60)
def load_data() -> pd.DataFrame:
    supabase = get_supabase_client()
    try:
        response = supabase.table("analyzed_signals").select("*").order("created_at", desc=True).execute()
        
        if not response.data:
            return pd.DataFrame(columns=[
                "original_event_id", "relevance_score", "sentiment", 
                "entities", "executive_summary", "actionable_insight", "analyzed_at"
            ])
            
        df = pd.DataFrame(response.data)
        # Convert relevance_score to numeric, ignoring and handling potential nulls
        df["relevance_score"] = pd.to_numeric(df["relevance_score"], errors="coerce").fillna(0).astype(int)
        
        # Array filling for new columns safely
        if "affected_regions" in df.columns:
            df["affected_regions"] = df["affected_regions"].apply(lambda x: x if isinstance(x, list) else [])
        if "relevant_hs_codes" in df.columns:
            df["relevant_hs_codes"] = df["relevant_hs_codes"].apply(lambda x: x if isinstance(x, list) else [])
            
        return df
        
    except Exception as e:
        st.error(f"Veritabanı bağlantı hatası: {e}")
        return pd.DataFrame(columns=[
            "original_event_id", "relevance_score", "sentiment", 
            "entities", "executive_summary", "actionable_insight", "analyzed_at"
        ])

df = load_data()

# Header
st.title("🌐 NEXUS Trade Radar - Ticari İstihbarat Paneli")
st.markdown("Yapay Zeka (Llama 3.1) + Truth Engine tarafından işlenmiş küresel ticaret haberleri ve aksiyon tavsiyeleri.")

# Metrics Row
col1, col2, col3, col4 = st.columns(4)
if not df.empty:
    total_signals = len(df)
    high_impact = len(df[df["relevance_score"] >= 70])
    avg_relevance = df["relevance_score"].mean()
    comtrade_count = len(df[df["executive_summary"].str.contains("Comtrade|comtrade|UN Comtrade", case=False, na=False)]) if "executive_summary" in df.columns else 0
else:
    total_signals = 0
    high_impact = 0
    avg_relevance = 0.0
    comtrade_count = 0

col1.metric("Toplam Sinyal", total_signals)
col2.metric("Yüksek Etkili (≥70)", high_impact)
col3.metric("Ortalama Puan", f"{avg_relevance:.1f}")
col4.metric("📊 Comtrade Veri", comtrade_count)

st.divider()

# Sidebar
st.sidebar.header("🔍 Filtreleme Seçenekleri")
min_relevance_score = st.sidebar.slider(
    "Minimum Etki Puanı (Relevance Score)", 
    min_value=70, max_value=100, value=70, step=1
)

selected_sentiments = st.sidebar.multiselect(
    "Duyarlılık (Sentiment) Analizi",
    options=["POSITIVE", "NEGATIVE", "NEUTRAL"],
    default=["POSITIVE", "NEGATIVE", "NEUTRAL"]
)

all_risk_cats = ["CUSTOMS_TARIFFS", "LOGISTICS", "GEOPOLITICAL", "SUPPLY_CHAIN", "TRADE_POLICY"]
selected_risk = st.sidebar.multiselect("🛡️ Risk Kategorisi", options=all_risk_cats, default=all_risk_cats)

# ── Source type filter ───────────────────────────────────────────────────────
source_filter = st.sidebar.multiselect(
    "📡 Kaynak Tipi",
    options=["Tüm Haberler", "📊 UN Comtrade Verisi"],
    default=["Tüm Haberler", "📊 UN Comtrade Verisi"]
)


# ── Helper: render truth/verification badges ────────────────────────────────
def _render_truth_badges(row):
    """Return HTML badges for truth_score and verification_status."""
    badges = []
    
    # Truth Score badge
    trust = row.get("trust_score")
    if trust is not None and pd.notna(trust):
        trust_val = float(trust)
        pct = f"{trust_val * 100:.0f}%" if trust_val <= 1.0 else f"{trust_val:.0f}%"
        if trust_val >= 0.85:
            css = "truth-high"
            icon = "✅"
        elif trust_val >= 0.70:
            css = "truth-medium"
            icon = "🟡"
        else:
            css = "truth-low"
            icon = "🔴"
        badges.append(f'<span class="truth-badge {css}">{icon} Truth Score: {pct}</span>')
    
    # Verification status badge
    vstatus = row.get("verification_status")
    if vstatus and pd.notna(vstatus):
        vstatus = str(vstatus)
        if "dual" in vstatus:
            badges.append('<span class="truth-badge badge-dual">🔗 Dual-Source Verified</span>')
        elif "official" in vstatus:
            badges.append('<span class="truth-badge badge-official">🏛️ Official Source</span>')
        elif vstatus != "unverified":
            badges.append(f'<span class="truth-badge badge-single">📄 {vstatus.replace("_", " ").title()}</span>')
    
    return " ".join(badges) if badges else ""


def _is_comtrade(row) -> bool:
    """Check if signal originates from UN Comtrade data."""
    summary = str(row.get("executive_summary", "")).lower()
    source_url = str(row.get("source_url", "")).lower()
    return any(kw in summary for kw in ["comtrade", "hs 28", "hs 25", "hs 72", "ticaret dengesi", "yoy anomali"]) or "comtradeplus" in source_url


def _source_tag(row) -> str:
    """Return a styled source tag."""
    if _is_comtrade(row):
        return '<span class="source-tag src-comtrade">📊 UN Comtrade</span>'
    summary = str(row.get("executive_summary", "")).lower()
    if "gdelt" in summary:
        return '<span class="source-tag src-gdelt">🌍 GDELT</span>'
    if "rss" in summary or "reuters" in summary or "lloyd" in summary:
        return '<span class="source-tag src-rss">📰 RSS Feed</span>'
    return '<span class="source-tag src-default">📡 Intelligence</span>'


# Main Feed Processing
if df.empty:
    st.info("Sistemde henüz çözümlenmiş bir sinyal bulunmuyor.")
else:
    # ── [Phase 12.1] Hard floor: NEVER render signals below 70 ─────────────
    df = df[df["relevance_score"] >= 70].copy()
    
    # Filter (sidebar controls — already bounded by the hard floor above)
    filtered_df = df[
        (df["relevance_score"] >= min_relevance_score) & 
        (df["sentiment"].isin(selected_sentiments)) &
        (df["risk_category"].isin(selected_risk) | df["risk_category"].isna())
    ].copy()
    
    # ── Source type filter ───────────────────────────────────────────────────
    if source_filter and "Tüm Haberler" not in source_filter:
        # Only Comtrade selected
        filtered_df = filtered_df[filtered_df.apply(_is_comtrade, axis=1)]
    elif source_filter and "📊 UN Comtrade Verisi" not in source_filter:
        # Only news selected
        filtered_df = filtered_df[~filtered_df.apply(_is_comtrade, axis=1)]
    
    # Sort
    filtered_df.sort_values(by="relevance_score", ascending=False, inplace=True)
    
    # Display Results
    st.header(f"📰 Sonuçlar ({len(filtered_df)} kayıt)")
    
    if filtered_df.empty:
        st.warning("Bu filtrelere uygun sonuç bulunamadı.")
    else:
        for index, row in filtered_df.iterrows():
            is_comtrade_signal = _is_comtrade(row)
            
            # Container for each intelligence card
            with st.container(border=True):
                SECTOR_ICONS = {
                    "72": "🏗️", "73": "⚙️", "25": "🧱", 
                    "26": "⛏️", "32": "🎨", "38": "🧪",
                    "2836": "💎",  # Calcite specific
                }
                
                current_hs = row.get("relevant_hs_codes", [])
                icon_prefix = ""
                if isinstance(current_hs, list):
                    for code in current_hs:
                        # Check 4-digit first, then 2-digit fallback
                        if code[:4] in SECTOR_ICONS:
                            icon_prefix += SECTOR_ICONS[code[:4]] + " "
                        elif code[:2] in SECTOR_ICONS:
                            icon_prefix += SECTOR_ICONS[code[:2]] + " "
                
                # ── Card header with source tag ──────────────────────────────
                if is_comtrade_signal:
                    header_prefix = "📊 "
                elif icon_prefix:
                    header_prefix = f"{icon_prefix.strip()} "
                else:
                    header_prefix = ""
                    
                st.subheader(f"{header_prefix}{row['executive_summary']}")
                
                # ── Truth Engine badges + source tag (HTML row) ──────────────
                badge_html = _render_truth_badges(row)
                source_html = _source_tag(row)
                if badge_html or source_html:
                    st.markdown(f"{source_html} {badge_html}", unsafe_allow_html=True)
                
                # Relevance progress display
                score = row["relevance_score"]
                score_col, prog_col = st.columns([1, 6])
                with score_col:
                    st.write(f"**Puan: {score}**")
                with prog_col:
                    st.progress(int(score) / 100.0)
                
                # Display action block based on score and type
                insight = row["actionable_insight"]
                if is_comtrade_signal:
                    st.info(f"📊 **Pazar Verisi:** {insight}")
                elif score >= 70:
                    st.error(f"🚨 **Stratejik Aksiyon:** {insight}")
                elif score >= 40:
                    st.warning(f"⚠️ **Tavsiye:** {insight}")
                else:
                    st.info(f"ℹ️ **Durum:** {insight}")
                
                # Footer with entities and sentiment
                entities_str = ", ".join(row["entities"]) if isinstance(row["entities"], list) and len(row["entities"]) > 0 else "Yok"
                
                risk_cat = row.get("risk_category") if pd.notna(row.get("risk_category")) else "Bilinmiyor"
                regions_list = row.get("affected_regions", [])
                regions_str = ", ".join(regions_list) if isinstance(regions_list, list) and regions_list else "Belirtilmemiş"
                hs_list = row.get("relevant_hs_codes", [])
                hs_codes_str = ", ".join(hs_list) if isinstance(hs_list, list) and hs_list else "Yok"
                
                st.caption(f"**Duyarlılık:** {row['sentiment']} | **Varlıklar:** {entities_str} | **Tarih:** {row['analyzed_at']}")
                st.caption(f"🛡️ **Risk:** {risk_cat} | 🌍 **Bölgeler:** {regions_str} | 🏷️ **HS Kodları:** {hs_codes_str}")
                
                if pd.notna(row.get('source_url')) and row['source_url'] != "Yok":
                    st.link_button("🔗 Haberin Kaynağına Git", str(row['source_url']))
