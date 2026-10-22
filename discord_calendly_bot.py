#!/usr/bin/env python3
"""
Discord Calendly / When2Meet Bot (`discord_calendly_bot.py`)
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
       python3 discord_calendly_bot.py
  3. Optional local CLI simulation (no Discord token required):
       python3 discord_calendly_bot.py --demo
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
import re
import sqlite3
from typing import Dict, List, Optional, Sequence, Set, Tuple

# ---------------------------------------------------------------------------
# 1. CORE TIME PARSING & SWEEP-LINE OVERLAP ENGINE
# ---------------------------------------------------------------------------

TIME_TOKEN_RE = re.compile(
    r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, order=True)
class TimeInterval:
    """Represents a half-open continuous time interval [start, end)."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError(
                f"Interval end ({self.end}) must be strictly after start ({self.start})."
            )

    @property
    def duration_minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    def overlaps(self, other: "TimeInterval") -> bool:
        return max(self.start, other.start) < min(self.end, other.end)

    def intersection(self, other: "TimeInterval") -> Optional["TimeInterval"]:
        s = max(self.start, other.start)
        e = min(self.end, other.end)
        return TimeInterval(s, e) if s < e else None

    def format_short(self, include_date: bool = True) -> str:
        """Formats interval concisely (e.g., '2026-10-20 9:00 AM–11:30 AM (2h 30m)')."""
        dur = self.duration_minutes
        hours, mins = divmod(dur, 60)
        dur_parts = []
        if hours:
            dur_parts.append(f"{hours}h")
        if mins or not dur_parts:
            dur_parts.append(f"{mins}m")
        dur_str = " ".join(dur_parts)

        if include_date:
            if self.start.date() == self.end.date():
                return (
                    f"{self.start.strftime('%Y-%m-%d')} "
                    f"{self.start.strftime('%I:%M %p').lstrip('0')}–"
                    f"{self.end.strftime('%I:%M %p').lstrip('0')} ({dur_str})"
                )
            return (
                f"{self.start.strftime('%Y-%m-%d %I:%M %p')}–"
                f"{self.end.strftime('%Y-%m-%d %I:%M %p')} ({dur_str})"
            )
        return (
            f"{self.start.strftime('%I:%M %p').lstrip('0')}–"
            f"{self.end.strftime('%I:%M %p').lstrip('0')} ({dur_str})"
        )


@dataclass
class OverlapWindow:
    """Represents a contiguous window where a specific subset of users are available."""

    interval: TimeInterval
    available_user_ids: Set[int]
    missing_user_ids: Set[int]
    includes_host: bool

    @property
    def count(self) -> int:
        return len(self.available_user_ids)


def merge_intervals(intervals: Sequence[TimeInterval]) -> List[TimeInterval]:
    """Merges overlapping or adjacent TimeIntervals into a minimal sorted list."""
    if not intervals:
        return []
    sorted_iv = sorted(intervals, key=lambda iv: (iv.start, iv.end))
    merged: List[TimeInterval] = [sorted_iv[0]]
    for current in sorted_iv[1:]:
        prev = merged[-1]
        if current.start <= prev.end:
            merged[-1] = TimeInterval(prev.start, max(prev.end, current.end))
        else:
            merged.append(current)
    return merged


def clamp_to_master_span(
    user_intervals: Sequence[TimeInterval],
    master_intervals: Sequence[TimeInterval],
) -> List[TimeInterval]:
    """Intersects user-submitted intervals with the primary user's master timespan."""
    clamped: List[TimeInterval] = []
    for u_iv in user_intervals:
        for m_iv in master_intervals:
            inter = u_iv.intersection(m_iv)
            if inter is not None:
                clamped.append(inter)
    return merge_intervals(clamped)


def parse_clock_time(token: str, base_date: datetime) -> datetime:
    """Parses a time token like '9am', '9:30 AM', '14:00', or '5:15pm' on base_date."""
    m = TIME_TOKEN_RE.match(token)
    if not m:
        raise ValueError(
            f"Could not parse time '{token}'. Use formats like '9am', '2:30pm', or '14:00'."
        )
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    meridiem = (m.group(3) or "").lower().replace(".", "")

    if meridiem:
        if hour < 1 or hour > 12:
            raise ValueError(f"Hour '{hour}' out of range for 12-hour clock in '{token}'.")
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
    else:
        if hour < 0 or hour > 24 or (hour == 24 and minute > 0):
            raise ValueError(f"Hour '{hour}' out of range for 24-hour clock in '{token}'.")

    if minute < 0 or minute >= 60:
        raise ValueError(f"Minute '{minute}' out of range in '{token}'.")

    if hour == 24:
        return base_date.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return base_date.replace(hour=hour, minute=minute, second=0, microsecond=0)


def parse_time_ranges_input(
    raw_input: str,
    default_date: datetime,
    master_intervals: Optional[Sequence[TimeInterval]] = None,
) -> List[TimeInterval]:
    """
    Parses flexible user availability strings into merged TimeIntervals.

    Supported formats (comma, semicolon, or newline separated):
      - 'all' or 'anytime' (selects the entire master_intervals if provided)
      - '9am-12pm, 1:30pm-5pm' (uses default_date)
      - '2026-10-20 09:00-12:30, 2026-10-21 14:00-18:00' (explicit dates)
      - '9-11am' (infers meridiem from end token when omitted on start token)
    """
    cleaned = raw_input.strip()
    if not cleaned:
        raise ValueError("Availability input cannot be empty.")

    if cleaned.lower() in {"all", "anytime", "full", "entire", "whole"}:
        if not master_intervals:
            raise ValueError("Cannot use 'all' before a master timespan is defined.")
        return merge_intervals(master_intervals)

    chunks = [c.strip() for c in re.split(r"[,;\n]+", cleaned) if c.strip()]
    parsed: List[TimeInterval] = []
    current_date = default_date.replace(hour=0, minute=0, second=0, microsecond=0)

    for chunk in chunks:
        date_match = re.match(r"^(\d{4}-\d{2}-\d{2})\s+(.+)$", chunk)
        if date_match:
            try:
                current_date = datetime.strptime(date_match.group(1), "%Y-%m-%d")
            except ValueError as exc:
                raise ValueError(f"Invalid date '{date_match.group(1)}' in '{chunk}'.") from exc
            range_part = date_match.group(2).strip()
        else:
            range_part = chunk

        parts = re.split(r"\s*(?:-|–|—|\bto\b)\s*", range_part, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) != 2:
            raise ValueError(
                f"Invalid range '{chunk}'. Expected start and end time like '9am-12pm' or '14:00-16:30'."
            )
        start_raw, end_raw = parts[0].strip(), parts[1].strip()

        start_has_mer = bool(re.search(r"(am|pm|a\.m\.|p\.m\.)", start_raw, re.IGNORECASE))
        end_mer_match = re.search(r"(am|pm|a\.m\.|p\.m\.)", end_raw, re.IGNORECASE)
        if not start_has_mer and end_mer_match and ":" not in start_raw:
            end_dt = parse_clock_time(end_raw, current_date)
            candidate_same = parse_clock_time(start_raw + end_mer_match.group(1), current_date)
            if candidate_same < end_dt:
                start_dt = candidate_same
            else:
                start_dt = parse_clock_time(start_raw + "am", current_date)
        else:
            start_dt = parse_clock_time(start_raw, current_date)
            end_dt = parse_clock_time(end_raw, current_date)

        if end_dt <= start_dt:
            raise ValueError(
                f"End time must be after start time in '{chunk}' (got {start_dt.strftime('%H:%M')} to {end_dt.strftime('%H:%M')})."
            )
        parsed.append(TimeInterval(start_dt, end_dt))

    merged = merge_intervals(parsed)
    if master_intervals is not None:
        merged = clamp_to_master_span(merged, master_intervals)
    return merged


def generate_discrete_slots(
    master_intervals: Sequence[TimeInterval],
    slot_minutes: int = 30,
    max_options: int = 25,
) -> List[TimeInterval]:
    """
    Subdivides master_intervals into discrete clickable slots for Discord Select menus.
    Discord Select menus allow at most 25 options; if more than 25 slots exist at
    `slot_minutes`, the step size automatically scales up so the entire master window
    remains selectable in a single dropdown.
    """
    if not master_intervals:
        return []

    total_mins = sum(iv.duration_minutes for iv in master_intervals)
    step = max(15, slot_minutes)
    while total_mins // step > max_options:
        step += 30

    slots: List[TimeInterval] = []
    for iv in merge_intervals(master_intervals):
        cursor = iv.start
        while cursor < iv.end and len(slots) < max_options:
            nxt = min(cursor + timedelta(minutes=step), iv.end)
            slots.append(TimeInterval(cursor, nxt))
            cursor = nxt
    return slots


def compute_availability_analysis(
    master_intervals: Sequence[TimeInterval],
    user_availabilities: Dict[int, Sequence[TimeInterval]],
    host_user_id: int,
    min_duration_minutes: int = 15,
) -> Tuple[List[OverlapWindow], List[OverlapWindow]]:
    """
    Runs a sweep-line algorithm over all clamped user intervals.

    Returns:
      - full_overlaps: Contiguous windows where 100% of respondents are available.
      - next_best_options: Contiguous maximal windows when <100% are available, ranked by:
          1. Number of available participants (descending)
          2. Host (primary user) included (True before False)
          3. Duration in minutes (descending)
          4. Earliest start time (ascending)
    """
    active_users = {
        uid: clamp_to_master_span(ivs, master_intervals)
        for uid, ivs in user_availabilities.items()
        if ivs
    }
    active_users = {uid: ivs for uid, ivs in active_users.items() if ivs}
    all_user_ids = set(active_users.keys())
    num_users = len(all_user_ids)

    if num_users == 0:
        return [], []

    boundaries: Set[datetime] = set()
    for m_iv in master_intervals:
        boundaries.add(m_iv.start)
        boundaries.add(m_iv.end)
    for ivs in active_users.values():
        for iv in ivs:
            boundaries.add(iv.start)
            boundaries.add(iv.end)

    sorted_points = sorted(boundaries)
    if len(sorted_points) < 2:
        return [], []

    elementary_segments: List[Tuple[TimeInterval, Set[int]]] = []
    for i in range(len(sorted_points) - 1):
        seg = TimeInterval(sorted_points[i], sorted_points[i + 1])
        if not any(m_iv.start <= seg.start and seg.end <= m_iv.end for m_iv in master_intervals):
            continue
        avail_set: Set[int] = set()
        for uid, ivs in active_users.items():
            if any(iv.start <= seg.start and seg.end <= iv.end for iv in ivs):
                avail_set.add(uid)
        if avail_set:
            elementary_segments.append((seg, avail_set))

    if not elementary_segments:
        return [], []

    merged_segments: List[Tuple[TimeInterval, Set[int]]] = [elementary_segments[0]]
    for seg, avail_set in elementary_segments[1:]:
        prev_seg, prev_set = merged_segments[-1]
        if prev_seg.end == seg.start and prev_set == avail_set:
            merged_segments[-1] = (TimeInterval(prev_seg.start, seg.end), prev_set)
        else:
            merged_segments.append((seg, avail_set))

    candidate_subsets: Set[frozenset[int]] = {
        frozenset(avail_set) for _, avail_set in merged_segments if avail_set
    }

    all_windows: List[OverlapWindow] = []
    for subset in candidate_subsets:
        subset_set = set(subset)
        matching_ivs = [
            seg for seg, avail_set in merged_segments if subset_set.issubset(avail_set)
        ]
        coalesced = merge_intervals(matching_ivs)
        for iv in coalesced:
            if iv.duration_minutes < min_duration_minutes:
                continue
            strictly_dominated = False
            for other_subset in candidate_subsets:
                if len(other_subset) > len(subset) and subset.issubset(other_subset):
                    other_ivs = merge_intervals(
                        [
                            seg
                            for seg, avail_set in merged_segments
                            if set(other_subset).issubset(avail_set)
                        ]
                    )
                    if any(o.start <= iv.start and iv.end <= o.end for o in other_ivs):
                        strictly_dominated = True
                        break
            if not strictly_dominated:
                all_windows.append(
                    OverlapWindow(
                        interval=iv,
                        available_user_ids=subset_set,
                        missing_user_ids=all_user_ids - subset_set,
                        includes_host=(host_user_id in subset_set),
                    )
                )

    full_overlaps = [w for w in all_windows if w.count == num_users]
    full_overlaps.sort(key=lambda w: (w.interval.start, -w.interval.duration_minutes))

    partial_overlaps = [w for w in all_windows if w.count < num_users]
    partial_overlaps.sort(
        key=lambda w: (
            -w.count,
            not w.includes_host,
            -w.interval.duration_minutes,
            w.interval.start,
        )
    )

    return full_overlaps, partial_overlaps


def render_ascii_heatmap(
    master_intervals: Sequence[TimeInterval],
    user_availabilities: Dict[int, Sequence[TimeInterval]],
    step_minutes: int = 30,
) -> str:
    """Renders a compact visual block timeline showing how many users are free per slot."""
    slots = generate_discrete_slots(master_intervals, slot_minutes=step_minutes, max_options=24)
    if not slots:
        return "No slots configured."

    active_users = {
        uid: clamp_to_master_span(ivs, master_intervals)
        for uid, ivs in user_availabilities.items()
        if ivs
    }
    total_users = len(active_users)
    lines: List[str] = []

    for slot in slots:
        free_count = sum(
            1
            for ivs in active_users.values()
            if any(iv.start <= slot.start and slot.end <= iv.end for iv in ivs)
        )
        if total_users == 0:
            bar = "░" * 6
            status = "0/0"
        else:
            filled = round((free_count / total_users) * 6)
            bar = "█" * filled + "░" * (6 - filled)
            star = " ★" if free_count == total_users and total_users > 0 else ""
            status = f"{free_count}/{total_users}{star}"

        time_label = f"{slot.start.strftime('%H:%M')}–{slot.end.strftime('%H:%M')}"
        lines.append(f"{time_label} | {bar} ({status})")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 2. SQLITE STORAGE LAYER
# ---------------------------------------------------------------------------


@dataclass
class ScheduleEvent:
    schedule_id: int
    guild_id: int
    channel_id: int
    message_id: Optional[int]
    title: str
    host_id: int
    host_name: str
    base_date: datetime
    slot_minutes: int
    master_intervals: List[TimeInterval]
    responses: Dict[int, Tuple[str, List[TimeInterval]]] = field(default_factory=dict)


class ScheduleStore:
    """SQLite persistence layer for Discord availability schedules."""

    def __init__(self, db_path: str = "schedules.db") -> None:
        self.db_path = db_path
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schedules (
                    schedule_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER,
                    title TEXT NOT NULL,
                    host_id INTEGER NOT NULL,
                    host_name TEXT NOT NULL,
                    base_date TEXT NOT NULL,
                    slot_minutes INTEGER NOT NULL DEFAULT 30,
                    master_intervals_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS availabilities (
                    schedule_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    user_name TEXT NOT NULL,
                    intervals_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (schedule_id, user_id),
                    FOREIGN KEY (schedule_id) REFERENCES schedules(schedule_id) ON DELETE CASCADE
                )
                """
            )

    @staticmethod
    def _serialize_intervals(intervals: Sequence[TimeInterval]) -> str:
        return json.dumps(
            [{"start": iv.start.isoformat(), "end": iv.end.isoformat()} for iv in intervals]
        )

    @staticmethod
    def _deserialize_intervals(raw_json: str) -> List[TimeInterval]:
        data = json.loads(raw_json)
        return [
            TimeInterval(
                start=datetime.fromisoformat(item["start"]),
                end=datetime.fromisoformat(item["end"]),
            )
            for item in data
        ]

    def create_schedule(
        self,
        guild_id: int,
        channel_id: int,
        title: str,
        host_id: int,
        host_name: str,
        base_date: datetime,
        master_intervals: Sequence[TimeInterval],
        slot_minutes: int = 30,
        host_initial_intervals: Optional[Sequence[TimeInterval]] = None,
    ) -> ScheduleEvent:
        merged_master = merge_intervals(master_intervals)
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO schedules (
                    guild_id, channel_id, title, host_id, host_name,
                    base_date, slot_minutes, master_intervals_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    channel_id,
                    title,
                    host_id,
                    host_name,
                    base_date.strftime("%Y-%m-%d"),
                    slot_minutes,
                    self._serialize_intervals(merged_master),
                    now_iso,
                ),
            )
            schedule_id = int(cur.lastrowid)

        initial_host_ivs = (
            clamp_to_master_span(host_initial_intervals, merged_master)
            if host_initial_intervals is not None
            else merged_master
        )
        self.set_user_availability(schedule_id, host_id, host_name, initial_host_ivs)
        schedule = self.get_schedule(schedule_id)
        assert schedule is not None
        return schedule

    def update_message_id(self, schedule_id: int, message_id: int) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE schedules SET message_id = ? WHERE schedule_id = ?",
                (message_id, schedule_id),
            )

    def set_user_availability(
        self,
        schedule_id: int,
        user_id: int,
        user_name: str,
        intervals: Sequence[TimeInterval],
    ) -> List[TimeInterval]:
        schedule = self.get_schedule(schedule_id)
        if schedule is None:
            raise ValueError(f"Schedule #{schedule_id} does not exist.")

        clamped = clamp_to_master_span(intervals, schedule.master_intervals)
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._conn:
            if not clamped:
                self._conn.execute(
                    "DELETE FROM availabilities WHERE schedule_id = ? AND user_id = ?",
                    (schedule_id, user_id),
                )
            else:
                self._conn.execute(
                    """
                    INSERT INTO availabilities (schedule_id, user_id, user_name, intervals_json, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(schedule_id, user_id) DO UPDATE SET
                        user_name = excluded.user_name,
                        intervals_json = excluded.intervals_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        schedule_id,
                        user_id,
                        user_name,
                        self._serialize_intervals(clamped),
                        now_iso,
                    ),
                )
        return clamped

    def get_schedule(self, schedule_id: int) -> Optional[ScheduleEvent]:
        row = self._conn.execute(
            "SELECT * FROM schedules WHERE schedule_id = ?", (schedule_id,)
        ).fetchone()
        if row is None:
            return None
        avail_rows = self._conn.execute(
            "SELECT * FROM availabilities WHERE schedule_id = ? ORDER BY updated_at ASC",
            (schedule_id,),
        ).fetchall()

        responses: Dict[int, Tuple[str, List[TimeInterval]]] = {}
        for r in avail_rows:
            responses[int(r["user_id"])] = (
                str(r["user_name"]),
                self._deserialize_intervals(str(r["intervals_json"])),
            )

        return ScheduleEvent(
            schedule_id=int(row["schedule_id"]),
            guild_id=int(row["guild_id"]),
            channel_id=int(row["channel_id"]),
            message_id=int(row["message_id"]) if row["message_id"] is not None else None,
            title=str(row["title"]),
            host_id=int(row["host_id"]),
            host_name=str(row["host_name"]),
            base_date=datetime.strptime(str(row["base_date"]), "%Y-%m-%d"),
            slot_minutes=int(row["slot_minutes"]),
            master_intervals=self._deserialize_intervals(str(row["master_intervals_json"])),
            responses=responses,
        )

    def get_latest_schedule_for_channel(self, channel_id: int) -> Optional[ScheduleEvent]:
        row = self._conn.execute(
            "SELECT schedule_id FROM schedules WHERE channel_id = ? ORDER BY schedule_id DESC LIMIT 1",
            (channel_id,),
        ).fetchone()
        if row is None:
            return None
        return self.get_schedule(int(row["schedule_id"]))


# ---------------------------------------------------------------------------
# 3. SUMMARY FORMATTING (SHARED BY DISCORD EMBEDS & CLI DEMO)
# ---------------------------------------------------------------------------


def build_schedule_report_dict(event: ScheduleEvent) -> Dict[str, str]:
    """Builds structured sections for a Discord Embed or CLI report."""
    single_day = len({iv.start.date() for iv in event.master_intervals}) == 1
    master_str = "\n".join(
        f"• `{iv.format_short(include_date=True)}`" for iv in event.master_intervals
    )

    user_intervals_map = {uid: ivs for uid, (_, ivs) in event.responses.items()}
    name_map = {uid: name for uid, (name, _) in event.responses.items()}
    name_map.setdefault(event.host_id, event.host_name)

    if not event.responses:
        indiv_str = "_No availabilities submitted yet. Click **➕ Add / Edit My Availability** below!_"
    else:
        indiv_lines: List[str] = []
        for uid, (uname, ivs) in event.responses.items():
            host_badge = " *(Host)*" if uid == event.host_id else ""
            formatted_ivs = ", ".join(
                f"`{iv.format_short(include_date=not single_day)}`" for iv in ivs
            )
            indiv_lines.append(f"• **{uname}**{host_badge}: {formatted_ivs}")
        indiv_str = "\n".join(indiv_lines)

    full_overlaps, partial_overlaps = compute_availability_analysis(
        master_intervals=event.master_intervals,
        user_availabilities=user_intervals_map,
        host_user_id=event.host_id,
    )

    num_respondents = len(user_intervals_map)
    if num_respondents == 0:
        best_str = "_Waiting for participants to add availability._"
        next_best_str = "_None needed yet._"
    else:
        if full_overlaps:
            best_lines = [
                f"✅ **`{w.interval.format_short(include_date=not single_day)}`** — All **{w.count}/{num_respondents}** participants free!"
                for w in full_overlaps[:5]
            ]
            best_str = "\n".join(best_lines)
        else:
            best_str = (
                f"⚠️ **No single window works for all {num_respondents} participants.** "
                "See the **Next Best Options** below!"
            )

        if partial_overlaps:
            nb_lines: List[str] = []
            for idx, w in enumerate(partial_overlaps[:5], start=1):
                free_names = ", ".join(
                    name_map.get(uid, f"<@{uid}>") for uid in sorted(w.available_user_ids)
                )
                missing_names = ", ".join(
                    name_map.get(uid, f"<@{uid}>") for uid in sorted(w.missing_user_ids)
                )
                host_note = "Host ✓" if w.includes_host else "Host missing"
                nb_lines.append(
                    f"**#{idx}** `{w.interval.format_short(include_date=not single_day)}` — "
                    f"**{w.count}/{num_respondents} free** ({host_note})\n"
                    f"   ↳ *Free:* {free_names} | *Missing:* {missing_names}"
                )
            next_best_str = "\n".join(nb_lines)
        else:
            next_best_str = (
                "_Everyone is free during the 100% match window(s) above!_"
                if full_overlaps
                else "_No overlapping windows found between participants._"
            )

    heatmap_str = (
        "```text\n"
        + render_ascii_heatmap(
            event.master_intervals, user_intervals_map, step_minutes=event.slot_minutes
        )
        + "\n```"
    )

    return {
        "master": master_str,
        "individual": indiv_str,
        "best": best_str,
        "next_best": next_best_str,
        "heatmap": heatmap_str,
    }


# ---------------------------------------------------------------------------
# 4. DISCORD BOT UI (MODALS, MULTI-SELECT DROPDOWNS, PERSISTENT BUTTONS)
# ---------------------------------------------------------------------------

try:
    import discord
    from discord import app_commands
    from discord.ext import commands

    DISCORD_AVAILABLE = True
except ImportError:
    DISCORD_AVAILABLE = False


if DISCORD_AVAILABLE:

    def build_schedule_embed(event: ScheduleEvent) -> discord.Embed:
        sections = build_schedule_report_dict(event)
        embed = discord.Embed(
            title=f"📅 {event.title} (Schedule #{event.schedule_id})",
            description=(
                f"Hosted by <@{event.host_id}> • Base Date: **{event.base_date.strftime('%Y-%m-%d')}**\n"
                "Click **➕ Add / Edit My Availability** to pick time slots or type ranges."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="🕒 Master Timespan (Allowed Window)",
            value=sections["master"][:1024],
            inline=False,
        )
        embed.add_field(
            name=f"👥 Participant Availabilities ({len(event.responses)})",
            value=sections["individual"][:1024],
            inline=False,
        )
        embed.add_field(
            name="🌟 Everyone Available (100% Match)",
            value=sections["best"][:1024],
            inline=False,
        )
        embed.add_field(
            name="🥈 Next Best Options (Ranked Partial Overlap)",
            value=sections["next_best"][:1024],
            inline=False,
        )
        embed.add_field(
            name="📊 Slot Availability Heatmap",
            value=sections["heatmap"][:1024],
            inline=False,
        )
        embed.set_footer(
            text=f"Schedule ID: {event.schedule_id} • Use /availabilities or the buttons below anytime"
        )
        return embed

    async def refresh_public_schedule_card(
        interaction: discord.Interaction,
        store: ScheduleStore,
        schedule_id: int,
    ) -> Optional[ScheduleEvent]:
        """Updates the public schedule embed in the channel after a user edits availability."""
        event = store.get_schedule(schedule_id)
        if event is None:
            return None
        embed = build_schedule_embed(event)
        view = ScheduleCardView(store=store, schedule_id=schedule_id)
        if interaction.channel and event.message_id:
            try:
                msg = await interaction.channel.fetch_message(event.message_id)
                await msg.edit(embed=embed, view=view)
            except discord.HTTPException:
                pass
        return event

    class QuickTextAvailabilityModal(discord.ui.Modal):
        """Lets a user type simple ranges like '9am-11:30am, 2pm-5pm'."""

        def __init__(self, store: ScheduleStore, event: ScheduleEvent) -> None:
            super().__init__(title=f"Availability: {event.title[:28]}")
            self.store = store
            self.event = event

            example_span = event.master_intervals[0]
            placeholder = (
                f"e.g. {example_span.start.strftime('%I:%M%p').lstrip('0').lower()}-"
                f"{example_span.end.strftime('%I:%M%p').lstrip('0').lower()} or 'all'"
            )
            self.ranges_input = discord.ui.TextInput(
                label=f"Your Free Times ({event.base_date.strftime('%Y-%m-%d')})",
                style=discord.TextStyle.paragraph,
                placeholder=placeholder,
                required=True,
                max_length=400,
            )
            self.add_item(self.ranges_input)

        async def on_submit(self, interaction: discord.Interaction) -> None:
            try:
                clamped_intervals = parse_time_ranges_input(
                    raw_input=self.ranges_input.value,
                    default_date=self.event.base_date,
                    master_intervals=self.event.master_intervals,
                )
            except ValueError as exc:
                await interaction.response.send_message(
                    f"❌ **Invalid time format:** {exc}\n"
                    "Try formats like `9am-11:30am, 2pm-4pm` or `13:00-17:00`.",
                    ephemeral=True,
                )
                return

            if not clamped_intervals:
                await interaction.response.send_message(
                    "⚠️ The times you entered fall completely outside the host's master timespan.",
                    ephemeral=True,
                )
                return

            saved = self.store.set_user_availability(
                schedule_id=self.event.schedule_id,
                user_id=interaction.user.id,
                user_name=interaction.user.display_name,
                intervals=clamped_intervals,
            )
            await refresh_public_schedule_card(
                interaction, self.store, self.event.schedule_id
            )
            formatted = ", ".join(f"`{iv.format_short()}`" for iv in saved)
            await interaction.response.send_message(
                f"✅ Saved your availability for **{self.event.title}**:\n{formatted}",
                ephemeral=True,
            )

    class SlotMultiSelect(discord.ui.Select):
        """Dropdown allowing users to check multiple discrete time blocks with zero typing."""

        def __init__(self, store: ScheduleStore, event: ScheduleEvent, user_id: int) -> None:
            self.store = store
            self.event = event
            self.slots = generate_discrete_slots(
                event.master_intervals, slot_minutes=event.slot_minutes, max_options=25
            )
            existing_ivs = event.responses.get(user_id, ("", []))[1]

            options: List[discord.SelectOption] = []
            for idx, slot in enumerate(self.slots):
                is_selected = any(
                    iv.start <= slot.start and slot.end <= iv.end for iv in existing_ivs
                )
                label = (
                    f"{slot.start.strftime('%I:%M %p').lstrip('0')} – "
                    f"{slot.end.strftime('%I:%M %p').lstrip('0')}"
                )
                desc = slot.start.strftime("%Y-%m-%d")
                options.append(
                    discord.SelectOption(
                        label=label,
                        value=str(idx),
                        description=desc,
                        default=is_selected,
                    )
                )

            super().__init__(
                placeholder="Select all time slots when you are FREE...",
                min_values=0,
                max_values=len(options),
                options=options,
            )

        async def callback(self, interaction: discord.Interaction) -> None:
            selected_indices = {int(v) for v in self.values}
            chosen_slots = [self.slots[i] for i in sorted(selected_indices)]
            saved = self.store.set_user_availability(
                schedule_id=self.event.schedule_id,
                user_id=interaction.user.id,
                user_name=interaction.user.display_name,
                intervals=chosen_slots,
            )
            await refresh_public_schedule_card(
                interaction, self.store, self.event.schedule_id
            )
            if not saved:
                await interaction.response.edit_message(
                    content="🗑️ Cleared your availability for this schedule.",
                    view=None,
                )
            else:
                formatted = ", ".join(f"`{iv.format_short()}`" for iv in saved)
                await interaction.response.edit_message(
                    content=f"✅ Updated your availability for **{self.event.title}**:\n{formatted}",
                    view=None,
                )

    class EphemeralSlotPickerView(discord.ui.View):
        """Ephemeral helper view combining the slot multi-select with quick action buttons."""

        def __init__(self, store: ScheduleStore, event: ScheduleEvent, user_id: int) -> None:
            super().__init__(timeout=300)
            self.store = store
            self.event = event
            if event.master_intervals:
                self.add_item(SlotMultiSelect(store, event, user_id))

        @discord.ui.button(
            label="⌨️ Type Custom Ranges Instead",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        async def type_ranges_btn(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ) -> None:
            fresh = self.store.get_schedule(self.event.schedule_id) or self.event
            await interaction.response.send_modal(
                QuickTextAvailabilityModal(self.store, fresh)
            )

        @discord.ui.button(
            label="✅ Free Entire Window",
            style=discord.ButtonStyle.success,
            row=1,
        )
        async def free_all_btn(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ) -> None:
            fresh = self.store.get_schedule(self.event.schedule_id) or self.event
            saved = self.store.set_user_availability(
                schedule_id=fresh.schedule_id,
                user_id=interaction.user.id,
                user_name=interaction.user.display_name,
                intervals=fresh.master_intervals,
            )
            await refresh_public_schedule_card(
                interaction, self.store, fresh.schedule_id
            )
            formatted = ", ".join(f"`{iv.format_short()}`" for iv in saved)
            await interaction.response.edit_message(
                content=f"✅ Marked you as free for the entire window:\n{formatted}",
                view=None,
            )

        @discord.ui.button(
            label="🗑️ Clear My Availability",
            style=discord.ButtonStyle.danger,
            row=1,
        )
        async def clear_btn(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ) -> None:
            self.store.set_user_availability(
                schedule_id=self.event.schedule_id,
                user_id=interaction.user.id,
                user_name=interaction.user.display_name,
                intervals=[],
            )
            await refresh_public_schedule_card(
                interaction, self.store, self.event.schedule_id
            )
            await interaction.response.edit_message(
                content="🗑️ Cleared your availability from this schedule.",
                view=None,
            )

    class ScheduleCardView(discord.ui.View):
        """Main public buttons attached to the schedule message in the channel."""

        def __init__(self, store: ScheduleStore, schedule_id: int) -> None:
            super().__init__(timeout=None)
            self.store = store
            self.schedule_id = schedule_id

        @discord.ui.button(
            label="➕ Add / Edit My Availability",
            style=discord.ButtonStyle.primary,
            custom_id="calendly:add_avail",
        )
        async def add_availability_btn(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ) -> None:
            event = self.store.get_schedule(self.schedule_id)
            if event is None:
                await interaction.response.send_message(
                    "❌ Schedule not found.", ephemeral=True
                )
                return
            picker_view = EphemeralSlotPickerView(
                self.store, event, interaction.user.id
            )
            await interaction.response.send_message(
                f"### 🗓️ Set Your Availability for **{event.title}**\n"
                "Choose your free time slots from the dropdown below, click **✅ Free Entire Window**, "
                "or click **⌨️ Type Custom Ranges** (e.g., `9:30am-11:15am, 2pm-4pm`):",
                view=picker_view,
                ephemeral=True,
            )

        @discord.ui.button(
            label="⌨️ Quick Text Input",
            style=discord.ButtonStyle.secondary,
            custom_id="calendly:quick_modal",
        )
        async def quick_modal_btn(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ) -> None:
            event = self.store.get_schedule(self.schedule_id)
            if event is None:
                await interaction.response.send_message(
                    "❌ Schedule not found.", ephemeral=True
                )
                return
            await interaction.response.send_modal(
                QuickTextAvailabilityModal(self.store, event)
            )

        @discord.ui.button(
            label="📊 Show All & Best Times",
            style=discord.ButtonStyle.success,
            custom_id="calendly:show_best",
        )
        async def show_best_btn(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ) -> None:
            event = self.store.get_schedule(self.schedule_id)
            if event is None:
                await interaction.response.send_message(
                    "❌ Schedule not found.", ephemeral=True
                )
                return
            embed = build_schedule_embed(event)
            await interaction.response.send_message(embed=embed, ephemeral=False)

    # -----------------------------------------------------------------------
    # 5. BOT APPLICATION & SLASH COMMANDS
    # -----------------------------------------------------------------------

    class CalendlyBot(commands.Bot):
        def __init__(self, store: ScheduleStore) -> None:
            intents = discord.Intents.default()
            super().__init__(command_prefix="!", intents=intents)
            self.store = store

        async def setup_hook(self) -> None:
            await self.tree.sync()
            print(f"✅ Synced slash commands for {self.user}.")

    def create_bot_instance(db_path: str = "schedules.db") -> CalendlyBot:
        store = ScheduleStore(db_path=db_path)
        bot = CalendlyBot(store=store)

        @bot.tree.command(
            name="create_schedule",
            description="Primary user: set up a master timespan of availabilities for others to join.",
        )
        @app_commands.describe(
            title="Event name (e.g., 'Project Sync' or 'D&D Session 4')",
            timespan="Allowed master window(s), e.g., '9am-5pm' or '9am-12pm, 2pm-6pm'",
            date="Base date in YYYY-MM-DD format (defaults to today's date)",
            slot_minutes="Slot size in minutes for the dropdown picker (15, 30, or 60; default 30)",
        )
        async def create_schedule_cmd(
            interaction: discord.Interaction,
            title: str,
            timespan: str,
            date: Optional[str] = None,
            slot_minutes: int = 30,
        ) -> None:
            if date:
                try:
                    base_date = datetime.strptime(date.strip(), "%Y-%m-%d")
                except ValueError:
                    await interaction.response.send_message(
                        "❌ Invalid `date`. Please use `YYYY-MM-DD` (e.g. `2026-10-20`).",
                        ephemeral=True,
                    )
                    return
            else:
                now = datetime.now()
                base_date = datetime(now.year, now.month, now.day)

            try:
                master_intervals = parse_time_ranges_input(
                    raw_input=timespan,
                    default_date=base_date,
                    master_intervals=None,
                )
            except ValueError as exc:
                await interaction.response.send_message(
                    f"❌ Could not parse `timespan`: {exc}\n"
                    "Example: `9am-5pm` or `9:30am-12:30pm, 2pm-6pm`",
                    ephemeral=True,
                )
                return

            slot_minutes = max(15, min(120, slot_minutes))
            event = store.create_schedule(
                guild_id=interaction.guild_id or 0,
                channel_id=interaction.channel_id or 0,
                title=title,
                host_id=interaction.user.id,
                host_name=interaction.user.display_name,
                base_date=base_date,
                master_intervals=master_intervals,
                slot_minutes=slot_minutes,
            )

            embed = build_schedule_embed(event)
            view = ScheduleCardView(store=store, schedule_id=event.schedule_id)
            await interaction.response.send_message(embed=embed, view=view)
            sent_msg = await interaction.original_response()
            store.update_message_id(event.schedule_id, sent_msg.id)

        @bot.tree.command(
            name="avail",
            description="Quickly add or update your availability for the active schedule in this channel.",
        )
        @app_commands.describe(
            times="Your available ranges within the timespan (e.g., '10am-12pm, 2pm-4pm' or 'all')",
            schedule_id="Optional Schedule ID (defaults to the latest schedule in this channel)",
        )
        async def avail_cmd(
            interaction: discord.Interaction,
            times: str,
            schedule_id: Optional[int] = None,
        ) -> None:
            event = (
                store.get_schedule(schedule_id)
                if schedule_id is not None
                else store.get_latest_schedule_for_channel(interaction.channel_id or 0)
            )
            if event is None:
                await interaction.response.send_message(
                    "❌ No active schedule found in this channel. Use `/create_schedule` first!",
                    ephemeral=True,
                )
                return

            try:
                clamped = parse_time_ranges_input(
                    raw_input=times,
                    default_date=event.base_date,
                    master_intervals=event.master_intervals,
                )
            except ValueError as exc:
                await interaction.response.send_message(
                    f"❌ Invalid time range: {exc}", ephemeral=True
                )
                return

            if not clamped:
                await interaction.response.send_message(
                    "⚠️ None of the times you entered overlap with the primary user's master timespan.",
                    ephemeral=True,
                )
                return

            saved = store.set_user_availability(
                schedule_id=event.schedule_id,
                user_id=interaction.user.id,
                user_name=interaction.user.display_name,
                intervals=clamped,
            )
            await refresh_public_schedule_card(interaction, store, event.schedule_id)
            formatted = ", ".join(f"`{iv.format_short()}`" for iv in saved)
            await interaction.response.send_message(
                f"✅ Updated your availability for **{event.title}** (#{event.schedule_id}):\n{formatted}",
                ephemeral=True,
            )

        @bot.tree.command(
            name="availabilities",
            description="Show all submitted availabilities, 100% overlap times, or the next best options.",
        )
        @app_commands.describe(
            schedule_id="Optional Schedule ID (defaults to the latest schedule in this channel)"
        )
        async def show_availabilities_cmd(
            interaction: discord.Interaction,
            schedule_id: Optional[int] = None,
        ) -> None:
            event = (
                store.get_schedule(schedule_id)
                if schedule_id is not None
                else store.get_latest_schedule_for_channel(interaction.channel_id or 0)
            )
            if event is None:
                await interaction.response.send_message(
                    "❌ No schedule found in this channel. Create one with `/create_schedule`!",
                    ephemeral=True,
                )
                return

            embed = build_schedule_embed(event)
            view = ScheduleCardView(store=store, schedule_id=event.schedule_id)
            await interaction.response.send_message(embed=embed, view=view)

        return bot


# ---------------------------------------------------------------------------
# 6. LOCAL CLI DEMO MODE (TEST WITHOUT A DISCORD BOT TOKEN)
# ---------------------------------------------------------------------------


def run_cli_demo() -> None:
    """Runs an end-to-end demonstration of the scheduling and overlap engine."""
    print("=" * 72)
    print(" DISCORD CALENDLY BOT — SIMULATION DEMO")
    print("=" * 72)

    store = ScheduleStore(db_path=":memory:")
    base_date = datetime(2026, 10, 20)

    # 1. Primary user sets up master timespan: 9:00 AM - 5:00 PM
    master_ivs = parse_time_ranges_input("9am-5pm", default_date=base_date)
    event = store.create_schedule(
        guild_id=1,
        channel_id=100,
        title="Team Sprint & Product Sync",
        host_id=101,
        host_name="Michael (Host)",
        base_date=base_date,
        master_intervals=master_ivs,
        slot_minutes=30,
    )

    # Primary user refines their own availability inside the master timespan
    store.set_user_availability(
        event.schedule_id,
        101,
        "Michael (Host)",
        parse_time_ranges_input("9am-12:30pm, 2pm-5pm", base_date, master_ivs),
    )
    # Other users add their availability
    store.set_user_availability(
        event.schedule_id,
        102,
        "Alex",
        parse_time_ranges_input("10am-1pm, 2:30pm-4:30pm", base_date, master_ivs),
    )
    store.set_user_availability(
        event.schedule_id,
        103,
        "Sarah",
        parse_time_ranges_input("10:30am-12pm, 1pm-4pm", base_date, master_ivs),
    )
    store.set_user_availability(
        event.schedule_id,
        104,
        "Devon",
        parse_time_ranges_input("10:30am-11:30am, 2:30pm-5pm", base_date, master_ivs),
    )

    updated = store.get_schedule(event.schedule_id)
    assert updated is not None
    report = build_schedule_report_dict(updated)

    print(f"\n📅 Schedule: {updated.title} (Date: {updated.base_date.strftime('%Y-%m-%d')})")
    print("\n[Master Timespan]")
    print(report["master"])
    print("\n[Participant Availabilities]")
    print(report["individual"])
    print("\n[🌟 Everyone Available (100% Overlap)]")
    print(report["best"])
    print("\n[🥈 Next Best Options (Ranked Partial Overlap)]")
    print(report["next_best"])
    print("\n[📊 Availability Heatmap]")
    print(report["heatmap"])
    print("=" * 72)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Discord Calendly / Availability Bot"
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run a local terminal simulation of the availability & overlap engine.",
    )
    parser.add_argument(
        "--db",
        default="schedules.db",
        help="Path to SQLite database file (default: schedules.db).",
    )
    args = parser.parse_args()

    if args.demo:
        run_cli_demo()
        return

    if not DISCORD_AVAILABLE:
        raise SystemExit(
            "discord.py is not installed. Install it via:\n"
            "  pip install -U discord.py\n"
            "Or run `python3 discord_calendly_bot.py --demo` to test the engine locally."
        )

    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit(
            "Missing DISCORD_BOT_TOKEN environment variable.\n"
            "Set it via:\n"
            "  export DISCORD_BOT_TOKEN='your_token_here'\n"
            "  python3 discord_calendly_bot.py\n"
            "Or run with `--demo` to preview the scheduling engine."
        )

    bot = create_bot_instance(db_path=args.db)
    bot.run(token)


if __name__ == "__main__":
    main()