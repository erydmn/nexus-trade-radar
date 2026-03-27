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

# Load Data Function
@st.cache_data(ttl=60) # Caches data for 60 seconds to auto-refresh nicely
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
st.markdown("Yapay Zeka (Llama 3.1) tarafından işlenmiş küresel ticaret haberleri ve aksiyon tavsiyeleri.")

# Metrics Row
col1, col2, col3 = st.columns(3)
if not df.empty:
    total_signals = len(df)
    high_impact = len(df[df["relevance_score"] >= 70])
    avg_relevance = df["relevance_score"].mean()
else:
    total_signals = 0
    high_impact = 0
    avg_relevance = 0.0

col1.metric("Toplam İstihbarat Sinyali", total_signals)
col2.metric("Yüksek Etkili (Puan >= 70)", high_impact)
col3.metric("Ortalama Etki Puanı", f"{avg_relevance:.1f}")

st.divider()

# Sidebar
st.sidebar.header("🔍 Filtreleme Seçenekleri")
min_relevance_score = st.sidebar.slider(
    "Minimum Etki Puanı (Relevance Score)", 
    min_value=0, max_value=100, value=50, step=1
)

selected_sentiments = st.sidebar.multiselect(
    "Duyarlılık (Sentiment) Analizi",
    options=["POSITIVE", "NEGATIVE", "NEUTRAL"],
    default=["POSITIVE", "NEGATIVE", "NEUTRAL"]
)

all_risk_cats = ["CUSTOMS_TARIFFS", "LOGISTICS", "GEOPOLITICAL", "SUPPLY_CHAIN", "TRADE_POLICY"]
selected_risk = st.sidebar.multiselect("🛡️ Risk Kategorisi", options=all_risk_cats, default=all_risk_cats)

# Main Feed Processing
if df.empty:
    st.info("Sistemde henüz çözümlenmiş bir sinyal bulunmuyor.")
else:
    # Filter
    filtered_df = df[
        (df["relevance_score"] >= min_relevance_score) & 
        (df["sentiment"].isin(selected_sentiments)) &
        (df["risk_category"].isin(selected_risk) | df["risk_category"].isna())
    ].copy()
    
    # Sort
    filtered_df.sort_values(by="relevance_score", ascending=False, inplace=True)
    
    # Display Results
    st.header(f"📰 Sonuçlar ({len(filtered_df)} kayıt)")
    
    if filtered_df.empty:
        st.warning("Bu filtrelere uygun sonuç bulunamadı.")
    else:
        for index, row in filtered_df.iterrows():
            # Container for each intelligence card
            with st.container(border=True):
                st.subheader(row["executive_summary"])
                
                # Relevance progress display
                score = row["relevance_score"]
                score_col, prog_col = st.columns([1, 6])
                with score_col:
                    st.write(f"**Puan: {score}**")
                with prog_col:
                    st.progress(int(score) / 100.0)
                
                # Display action block based on score
                insight = row["actionable_insight"]
                if score >= 70:
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
