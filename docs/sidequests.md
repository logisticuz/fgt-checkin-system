# Sidequests & Feature Ideas

Ideas captured during development sessions. Review periodically and promote to proper issues/tasks when ready.

---

## 2026-01-04 - Activity Log/Feed in Dashboard

**Context:** Currently feedback messages are shown as plain text in a single row. Came up during dashboard development work.

**Idea:** Create a more polished activity log/feed section in the dashboard that shows recent events with timestamps, including:
- Deletions ("Deleted 3 players")
- Status changes ("viktor: Payment ✓, status=Ready")
- Other TO actions (manual game selection, bulk operations, etc.)

This would provide better visibility into what's happening during the tournament and create a historical record of actions taken.

**System area:** fgt_dashboard (dashboard UI and callbacks)

**Priority hint:** Would improve UX - helps TO track what they've done and when

**Status:** 🆕 New

---

## 2026-01-04 - Tabbed View for "Needs Attention" and "Player List"

**Context:** Came up during dashboard development - scrolling between sections becomes cumbersome when player list grows

**Idea:** Replace the stacked layout of "Needs Attention" and "Player List" with a tabbed interface:
- Tabs at the top to switch between the two views
- Both sections share the same screen space (no scrolling needed)
- "Needs Attention" tab should display a pulsating red badge with the count of players needing attention
- Cleaner UI when managing many players during an event

**System area:** fgt_dashboard (layout.py, callbacks.py for badge count)

**Priority hint:** Next session (tomorrow) - significant UX improvement for tournament operations

**Status:** 🆕 New

---
