# Learnings

Corrections, insights, and knowledge gaps captured during development.

**Categories**: correction | insight | knowledge_gap | best_practice

---

## [LRN-20260508-001] correction

**Logged**: 2026-05-08T08:38:00+02:00
**Priority**: medium
**Status**: pending
**Area**: frontend

### Summary
Dashboard open-operations widget must use the same real unclosed trade source as the donut "Sin cerrar" count.

### Details
User noticed Fabian Python had 8 unclosed trades shown in the donut but none in "Operaciones abiertas" because the frontend only read `positions_open`, which was empty. Correct behavior: trades with empty/null `result` are real open operations and must appear until closed.

### Suggested Action
For trading dashboards, keep metric widgets and tables backed by the same source/query whenever they describe the same concept.

### Metadata
- Source: user_feedback
- Related Files: backend/main.py, frontend/index.html
- Tags: dashboard, open_trades, data_consistency

---
