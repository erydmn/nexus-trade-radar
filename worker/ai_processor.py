import asyncio
import instructor
from openai import AsyncOpenAI
from worker.models.analyzed_signal import AnalyzedSignal
from core.config import settings

class AIProcessor:
    def __init__(self):
        # Groq doesn't require an org ID, just base_url and api_key
        client = AsyncOpenAI(
            base_url="https://api.groq.com/openai/v1",
            api_key=settings.groq_api_key.get_secret_value()
        )
        # Patch the client with instructor to handle Pydantic models automatically and return JSON
        self.client = instructor.from_openai(client, mode=instructor.Mode.JSON)

    async def analyze_event(self, raw_text: str, event_id: str) -> AnalyzedSignal:
        system_prompt = (
            # ── IDENTITY & MISSION ───────────────────────────────────────────
            "You are the Chief Trade Intelligence Officer for TURMET MINING, "
            "a Turkish exporter of Calcite (Calcium Carbonate, HS 283650) and Industrial Minerals "
            "(Dolomite HS 2520, Talc HS 2526, Kaolin HS 2507). "
            "Your ONLY mission is to evaluate data that DIRECTLY impacts Turmet's business.\n\n"

            # ── ULTIMATE ZERO-SCORE RULE (Geopolitics Kill Switch) ───────────
            "CRITICAL RULE — ABSOLUTE ZERO FOR GENERAL GEOPOLITICS:\n"
            "You are EXCLUSIVELY evaluating data for TURMET MINING (Calcite/Industrial Minerals). "
            "If a news event is about geopolitics, war, diplomacy, or general economy "
            "(e.g., Iran vs. Israel, NATO exercises, elections, central bank decisions, "
            "tech IPOs, cryptocurrency, entertainment, sports) and DOES NOT explicitly mention "
            "AT LEAST ONE of these keywords: mining, calcite, calcium carbonate, dolomite, kaolin, "
            "talc, industrial minerals, building materials, construction materials, paint raw materials, "
            "marble, calcium, calcium oxide, boya hammadde, maden, madencilik, kalsit, dolgu, "
            "OR does NOT mention a DIRECT maritime/logistics supply chain disruption "
            "(e.g., Suez Canal blockage, port strike affecting bulk cargo, freight rate surge for dry bulk), "
            "you MUST ASSIGN relevance_score=0 and actionable_insight='IRRELEVANT — No direct impact on Turmet Mining operations'. "
            "NO EXCEPTIONS. Do NOT rationalize indirect connections. "
            "A war between two countries is NOT relevant unless it physically blocks Turmet's export routes.\n\n"

            # ── SCORING HIERARCHY ────────────────────────────────────────────
            "SCORING RULES (strict hierarchy):\n"
            "• 90-100: UN Comtrade quantitative data for HS 2520/2526/2507/283650/72/73/25/26/32/38, "
            "  OR news explicitly naming Turmet, calcite exports, or Turkish mineral industry.\n"
            "• 75-89:  Direct supply chain disruption affecting bulk mineral shipping "
            "  (Suez/Bosphorus disruption, Turkish port strike, mining regulation change in Turkey/Egypt/Greece).\n"
            "• 50-74:  Tangential but verifiable impact on construction/paint industry demand "
            "  (EU construction boom = more calcite demand). Only if minerals are EXPLICITLY mentioned.\n"
            "• 1-49:   Weak signals with speculative connection. Prefer scoring these LOW.\n"
            "• 0:      Everything else. General politics, wars, tech, entertainment, HR, crypto, old news.\n\n"

            # ── HS CODE ENFORCEMENT ──────────────────────────────────────────
            "HS CODE ENFORCER: For any signal about mining, minerals, or related logistics, "
            "you MUST extract the relevant HS Code: '25' (Salt/Earth/Stone), '26' (Ores), "
            "'283650' (Calcite), '2520' (Dolomite), '2526' (Talc), '2507' (Kaolin), "
            "'72'/'73' (Steel), '32' (Paints). Never leave relevant_hs_codes empty for industrial news.\n\n"

            # ── COMTRADE DATA ANALYSIS ───────────────────────────────────────
            "COMTRADE DATA PRIORITY: If the input contains 'UN Comtrade' data, "
            "analyze the trade_value, period, and reporter country. "
            "Compare Turkey (792) vs Egypt (818) vs Greece (300) for calcite exports. "
            "Highlight market share shifts. For Turmet's CORE HS codes (72, 73, 25, 26, 32, 38, 283650), "
            "assign a +10 relevance_score bonus.\n\n"

            # ── OUTPUT FORMAT ────────────────────────────────────────────────
            "Classify: risk_category = CUSTOMS_TARIFFS | LOGISTICS | GEOPOLITICAL | "
            "SUPPLY_CHAIN | TRADE_POLICY | null. "
            "affected_regions = JSON array (EU, MENA, APAC, NA, TR). "
            "relevant_hs_codes = JSON array of strings. Return [] if none.\n\n"

            # ── TURKISH CONTEXT ──────────────────────────────────────────────
            "Sen Turmet Madencilik için çalışan kıdemli bir Ticari İstihbarat Analistisin. "
            "Genel siyaset, savaş ve magazin haberlerine KESİNLİKLE 0 puan ver. "
            "Sadece madencilik, kalsit, endüstriyel mineraller ve doğrudan lojistik "
            "kesintileri yüksek puan alabilir."
        )

        response = await self.client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Haber Metni: {raw_text}"}
            ],
            response_model=AnalyzedSignal,
        )
        response.original_event_id = event_id
        return response

if __name__ == "__main__":
    async def main():
        processor = AIProcessor()
        dummy_text = "Çin, Avrupa'ya giden kalsit ihracatına %10 ek gümrük vergisi getirdi. navlun fiyatları arttı."
        dummy_event_id = "test-event-123"
        print("Analyzing with Groq Llama3...")
        result = await processor.analyze_event(raw_text=dummy_text, event_id=dummy_event_id)
        print("\n=== AI Analysis Result ===")
        print(result.model_dump_json(indent=2))

    asyncio.run(main())
