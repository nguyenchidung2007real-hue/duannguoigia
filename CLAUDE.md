# AN TAM AI - PROJECT CONVENTIONS & CODING RULES

## 1. Project Overview
- **Project**: "AnTam AI" (`elderly-care-ai`) - Healthcare Assistant & Companion for Vietnamese Elderly.
- **Tech Stack**: Python FastAPI (Backend), SQLite (Database), WebSockets (Real-time), Vanilla HTML5/TailwindCSS (Frontend), Pytest/Unittest.

## 2. Core Safety & Medical Guardrails (MUST ADHERE)
- **Rule 1 (Hybrid AI Architecture)**: NEVER use LLMs/AI to judge, diagnose, or calculate blood pressure / heart rate numbers. All vital physiological bounds and severity classifications MUST strictly go through deterministic Python code in `backend/services/medical_guardrails.py`.
- **Rule 2 (Zero-Friction UX)**: Elderly interface (`frontend/ElderlyView.html`) must NEVER require passwords, keyboard typing, or tiny font sizes (< 24px). Voice-first & giant 1-touch buttons only.
- **Rule 3 (Two-Phase Commit for Vitals)**: Speech vitals input is parsed first (Step 1), read back aloud, and ONLY committed to DB (Step 2) when the elderly confirms ("ĐÚNG RỒI").
- **Rule 4 (Timezone Unification)**: All timestamps must use naive Vietnam UTC+7 datetime via `models.get_vn_now()`. Never use naive `datetime.now()` or `datetime.utcnow()`.
- **Rule 5 (Caregiver Verification)**: Medications must have `verified_by_caregiver == True` before appearing in the elderly view.
- **Rule 6 (Graceful Fallback)**: If speech recognition or AI fails, immediately fallback to deterministic Vietnamese voice prompts.

## 3. Test Suite Command
Always run unit tests after code modifications:
```bash
python -m unittest backend/tests/test_guardrails.py -v
```
All 14 tests must pass 100% before committing code.
