# PHASE 16 - COMTRADE CALIBRATION & AI SCORING FIX
## Status: [IN PROGRESS]

### ✅ PLAN CONFIRMED
- [x] Remove legacy HS codes (72,73,25,26,32,38) from TARGETS → ONLY 283650 Calcite
- [x] Update annual_periods → ["2022","2023","2024"] (drop 2021)
- [x] Add CRITICAL SCORING RULE to ai_processor.py system_prompt
- [ ] Test Comtrade output
- [ ] Git commit & push

### ✅ IMPLEMENTATION COMPLETE
```
1. [x] Edit worker/comtrade_service.py → TARGETS + annual_periods
2. [x] Edit worker/ai_processor.py → Add scoring rule  

### 🧪 TESTING
3. [ ] Test: python worker/comtrade_service.py
   → Expect: ONLY HS283650 data, 2022-2024 periods

### 🚀 DEPLOY
4. [ ] git commit -m "fix: update Comtrade to recent Calcite-only (283650) + AI scoring fix"
5. [ ] git push origin main
```

**Next Step:** Execute file edits per approved plan
