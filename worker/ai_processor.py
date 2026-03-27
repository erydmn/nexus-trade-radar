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
            "You are an elite Chief Trade Intelligence Officer. Read the text. FIRST, evaluate if it is STRICTLY "
            "related to international trade, macroeconomics, supply chains, tariffs, maritime/aviation logistics, or global sanctions. "
            "If it is about entertainment (e.g., Marvel, movies), general tech gadgets, home decor, sports, or unrelated local politics, "
            "you MUST immediately return relevance_score=0, actionable_insight='IRRELEVANT NOISE', and stop analysis. "
            "Only analyze true global trade and economic signals.\n\n"
            "Sen kıdemli bir Ticari İstihbarat Analistisin. Amacın ticaret haberlerini okuyup, "
            "bir CEO'ya JSON formatında stratejik tavsiyeler çıkarmaktır. Önemsiz veya magazin "
            "haberlerine çok düşük relevance_score (0-20) ver ve actionable_insight kısmına 'Aksiyon gerekmiyor' yaz."
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
