Discord Scheduler Bot (`discord_schedule_bot.py`)
============================================================
A Discord bot built with `discord.py` (v2.x) that lets a primary user (host)
create an availability poll within a master timespan and allows any server
member (including the host) to add or edit their availability with minimal
friction via interactive buttons, multi-select slot menus, or shorthand text.

Key Features:
1. Master Timespan Enforcement:
   - The primary user creates a schedule with one or more master time windows
     (e.g., `2026-10-20 09:00-17:00` or `9am-12pm, 2pm-6pm`).
   - All participant submissions are validated and automatically clamped to
     fit strictly within the host's master timespan.
2. Frictionless Availability Input (3 One-Click Methods):
   - Interactive Slot Multi-Select Dropdown (`discord.ui.Select`): Tap time
     slots directly inside Discord without typing.
   - Quick Shorthand Modal (`discord.ui.Modal`): Type natural ranges like
     `9am-11:30am, 2pm-4:30pm` or `14:00-17:30`.
   - One-Click Buttons: "✅ Free Entire Window" or "🗑️ Clear My Availability".
3. Exact Overlap & Next-Best-Option Engine (Sweep-Line Algorithm):
   - Computes all contiguous time windows where 100% of participants are free.
   - Automatically computes ranked "Next Best Options" (maximal participant
     overlap, prioritizing windows where the primary host is available, then
     by longest duration) and lists who is free vs. missing for each slot.
4. SQLite Persistence & Persistent Views:
   - Stores schedules and user intervals in `schedules.db` (`sqlite3`) so data
     and interactive message buttons survive bot restarts.

Usage:
  1. Install dependencies:
       pip install -U discord.py
  2. Set your bot token and run:
       export DISCORD_BOT_TOKEN="your-bot-token-here"
       python3 discord_schedule_bot.py
  3. Optional local CLI simulation (no Discord token required):
       python3 discord_schedule_bot.py --demo
