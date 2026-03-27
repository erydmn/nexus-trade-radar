import json
import os
import sys
import asyncio
import logging
from pathlib import Path

# Early sys.path check to easily support direct running vs -m running
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from core.db import get_supabase_client
from worker.ai_processor import AIProcessor
from worker.models.signal import RawEvent
from worker.models.analyzed_signal import AnalyzedSignal

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def _get_processed_ids(supabase) -> set:
    try:
        response = supabase.table("analyzed_signals").select("original_event_id").execute()
        return {row["original_event_id"] for row in response.data}
    except Exception as e:
        logging.error(f"Failed to fetch processed IDs from Supabase: {e}")
        return set()

async def process_batch():
    input_path = project_root / "nexus_data_lake.jsonl"
    
    ai_processor = AIProcessor()
    supabase = get_supabase_client()
    processed_ids = _get_processed_ids(supabase)
    
    if not input_path.exists():
        logging.error(f"Input file not found at {input_path}")
        return

    MAX_BATCH_SIZE = 5
    processed_count = 0

    with open(input_path, "r", encoding="utf-8") as f:
        for line in f:
            if processed_count >= MAX_BATCH_SIZE:
                logging.info(f"Reached MAX_BATCH_SIZE limit of {MAX_BATCH_SIZE}.")
                break
                
            line = line.strip()
            if not line:
                continue
            
            try:
                event = RawEvent.model_validate_json(line)
            except Exception as e:
                logging.warning(f"Failed to parse event: {e}")
                continue
            
            if event.event_id in processed_ids:
                continue
            
            if "error" in event.tags:
                logging.info(f"Skipping error event: {event.event_id}")
                continue
                
            logging.info(f"Processing event: {event.event_id}")
            try:
                analyzed_signal = await ai_processor.analyze_event(
                    raw_text=event.raw_text,
                    event_id=event.event_id
                )
                
                # Insert directly into Supabase instead of JSONL file
                supabase.table("analyzed_signals").insert({
                    "original_event_id": event.event_id,
                    "relevance_score": analyzed_signal.relevance_score,
                    "sentiment": analyzed_signal.sentiment,
                    "entities": analyzed_signal.entities,
                    "executive_summary": analyzed_signal.executive_summary,
                    "actionable_insight": analyzed_signal.actionable_insight,
                    "analyzed_at": analyzed_signal.analyzed_at.isoformat(),
                    "source_url": event.metadata.get("url", "Yok"),
                    "risk_category": analyzed_signal.risk_category,
                    "affected_regions": analyzed_signal.affected_regions or [],
                    "relevant_hs_codes": analyzed_signal.relevant_hs_codes or []
                }).execute()
                
                processed_ids.add(event.event_id)
                processed_count += 1
                logging.info(f"Successfully processed and saved event to Supabase: {event.event_id}")
                
            except Exception as e:
                logging.error(f"Failed to process event {event.event_id}: {e}")
            
            # Rate limiting wrapper
            await asyncio.sleep(2.5)

if __name__ == "__main__":
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    asyncio.run(process_batch())
