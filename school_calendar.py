#!/usr/bin/env python3
"""
Build an iCalendar (.ics) feed from the Highworth Warneford School website calendar.

The school website (e4education/Juniper platform) renders its calendar client-side
from a JSON endpoint:

    /calendar/api.asp?pid=9&viewid=1&calid=1&bgedit=false&start=YYYY-MM-DD&end=YYYY-MM-DD&_=<epoch_ms>

No cookies or authentication are required. This script walks a rolling window of
months, collects the events, and writes a deterministic .ics file so that git only
records a commit when the school actually changes something.

Standard library only - no dependencies.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

API_URL = "https://www.warnefordschool.org/calendar/api.asp"
PAGE_URL = "https://www.warnefordschool.org/calendar/"
EVENT_TZ = ZoneInfo("Europe/London")
UID_DOMAIN = "warnefordschool.org"
PRODID = "-//dave//warneford-school-calendar//EN"

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def add_months(d: date, n: int) -> date:
    """First day of the month n months from d's month."""
    total = d.year * 12 + (d.month - 1) + n
    return date(total // 12, total % 12 + 1, 1)


def windows(months_back: int, months_ahead: int) -> list[tuple[date, date]]:
    """Month-sized fetch windows, padded a week either side (overlaps are deduped)."""
    this_month = date.today().replace(day=1)
    out = []
    for i in range(-months_back, months_ahead + 1):
        start = add_months(this_month, i) - timedelta(days=7)
        end = add_months(this_month, i + 1) + timedelta(days=7)
        out.append((start, end))
    return out


def fetch_window(start: date, end: date, calid: int, pid: int, viewid: int,
                 timeout: int = 30, retries: int = 3) -> list[dict]:
    params = {
        "pid": pid,
        "viewid": viewid,
        "calid": calid,
        "bgedit": "false",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "_": str(int(time.time() * 1000)),
    }
    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    referer = f"{PAGE_URL}?calid={calid}&pid={pid}&viewid={viewid}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": referer,
        "User-Agent": "warneford-calendar-feed/1.0 (personal calendar mirror)",
    })

    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            if not isinstance(payload, list):
                raise ValueError(f"expected a JSON array, got {type(payload).__name__}")
            return payload
        except (urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"failed to fetch {start}..{end}: {last_error}")


def fetch_all(calid: int, pid: int, viewid: int,
              months_back: int, months_ahead: int) -> list[dict]:
    """Fetch every window and dedupe by event id, preserving first-seen record."""
    seen: dict[str, dict] = {}
    for start, end in windows(months_back, months_ahead):
        for ev in fetch_window(start, end, calid, pid, viewid):
            key = str(ev.get("id") or hashlib.sha1(
                json.dumps(ev, sort_keys=True).encode()).hexdigest())
            seen.setdefault(key, ev)
        time.sleep(0.5)  # be a polite guest on someone else's server
    return list(seen.values())


# --------------------------------------------------------------------------- #
# Normalising
# --------------------------------------------------------------------------- #

def clean_text(value: str | None) -> str:
    """Strip HTML tags, decode entities (&ndash; etc), collapse whitespace."""
    if not value:
        return ""
    text = TAG_RE.sub(" ", value)
    text = html.unescape(text)
    return WS_RE.sub(" ", text).strip()


def parse_point(value: str):
    """'2026-09-03' -> date; '2026-09-17T17:00:00' -> naive local datetime."""
    if "T" in value:
        return datetime.fromisoformat(value)
    return date.fromisoformat(value)


def normalise(raw: dict, expand_daily: bool = True) -> dict | None:
    """Turn one API event into a flat dict, or None if it is unusable."""
    if not raw.get("start"):
        return None

    start = parse_point(raw["start"])
    # datetime subclasses date, so test datetime first.
    timed = isinstance(start, datetime)
    all_day = bool(raw.get("allDay")) or not timed

    raw_end = raw.get("end")
    if all_day:
        if timed:  # allDay flag but a timestamp given - take the date part
            start = start.date()
        if raw_end:
            end = date.fromisoformat(str(raw_end)[:10])
        else:
            end = start + timedelta(days=1)
        if end <= start:  # defensive: DTEND for all-day events is exclusive
            end = start + timedelta(days=1)
    else:
        end = datetime.fromisoformat(raw_end) if raw_end else start + timedelta(hours=1)
        if end <= start:
            end = start + timedelta(hours=1)

    # A timed event spanning several days means "this time slot, each day",
    # not one continuous block. The API's own `recurrence` text spells it out:
    #   "This event will take place between 5:00pm and 7:30pm on 18/09/2026
    #    until 23/09/2026"
    # so turn it into a daily recurrence with the end clamped to day one.
    daily_count = 0
    if expand_daily and not all_day and end.date() > start.date() \
            and end.time() > start.time():
        daily_count = (end.date() - start.date()).days + 1
        end = datetime.combine(start.date(), end.time())

    title = clean_text(raw.get("title")) or "(untitled event)"
    description = clean_text(raw.get("desc"))
    url = raw.get("url") or ""
    if raw.get("hasAttachment"):
        description = (description + " " if description else "") + \
                      "[This event has an attachment on the school website.]"
    if url:
        description = (description + "\n\n" if description else "") + url

    calendars = [clean_text(c.get("title")) for c in (raw.get("cals") or [])]
    categories = [c for c in calendars if c]

    uid_seed = str(raw.get("id") or f"{title}|{raw['start']}")
    return {
        "uid": f"{uid_seed}@{UID_DOMAIN}",
        "title": title,
        "description": description,
        "url": url,
        "start": start,
        "end": end,
        "all_day": all_day,
        "daily_count": daily_count,
        "categories": categories,
    }


def content_hash(ev: dict) -> str:
    payload = "|".join([
        ev["title"], ev["description"], ev["url"],
        ev["start"].isoformat(), ev["end"].isoformat(),
        str(ev["all_day"]), str(ev["daily_count"]),
        ",".join(ev["categories"]),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# ICS output
# --------------------------------------------------------------------------- #

def esc(text: str) -> str:
    return (text.replace("\\", "\\\\")
                .replace(";", "\\;")
                .replace(",", "\\,")
                .replace("\r\n", "\\n")
                .replace("\n", "\\n"))


def fold(line: str) -> str:
    """RFC 5545 line folding at 75 octets."""
    if len(line.encode("utf-8")) <= 75:
        return line
    parts: list[str] = []
    current = ""
    length = 0
    for ch in line:
        size = len(ch.encode("utf-8"))
        if length + size > 75:
            parts.append(current)
            current, length = " ", 1
        current += ch
        length += size
    parts.append(current)
    return "\r\n".join(parts)


def utc_stamp(dt: datetime) -> str:
    """Naive local (Europe/London) datetime -> UTC ICS timestamp."""
    return dt.replace(tzinfo=EVENT_TZ).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def local_stamp(dt: datetime) -> str:
    """Naive local datetime -> floating local ICS timestamp (used with TZID)."""
    return dt.strftime("%Y%m%dT%H%M%S")


# Timed events are written as TZID=Europe/London rather than converted to UTC.
# That matters for daily recurrences crossing a clock change: with a UTC DTSTART,
# FREQ=DAILY would keep the UTC time fixed and drift the local time by an hour.
VTIMEZONE = [
    "BEGIN:VTIMEZONE",
    "TZID:Europe/London",
    "BEGIN:DAYLIGHT",
    "TZOFFSETFROM:+0000",
    "TZOFFSETTO:+0100",
    "TZNAME:BST",
    "DTSTART:19810329T010000",
    "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU",
    "END:DAYLIGHT",
    "BEGIN:STANDARD",
    "TZOFFSETFROM:+0100",
    "TZOFFSETTO:+0000",
    "TZNAME:GMT",
    "DTSTART:19961027T020000",
    "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU",
    "END:STANDARD",
    "END:VTIMEZONE",
]


def build_ics(events: list[dict], state: dict, calendar_name: str) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{esc(calendar_name)}",
        "X-WR-TIMEZONE:Europe/London",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
        "X-PUBLISHED-TTL:PT12H",
    ]
    lines += VTIMEZONE

    for ev in sorted(events, key=lambda e: (e["start"].isoformat(), e["uid"])):
        entry = state[ev["uid"]]
        lines += ["BEGIN:VEVENT", f"UID:{ev['uid']}"]
        lines.append(f"SEQUENCE:{entry['seq']}")
        lines.append(f"DTSTAMP:{entry['stamp']}")
        lines.append(f"LAST-MODIFIED:{entry['stamp']}")

        if ev["all_day"]:
            lines.append(f"DTSTART;VALUE=DATE:{ev['start'].strftime('%Y%m%d')}")
            lines.append(f"DTEND;VALUE=DATE:{ev['end'].strftime('%Y%m%d')}")
        else:
            lines.append(f"DTSTART;TZID=Europe/London:{local_stamp(ev['start'])}")
            lines.append(f"DTEND;TZID=Europe/London:{local_stamp(ev['end'])}")
            if ev["daily_count"] > 1:
                # UNTIL must be UTC when DTSTART carries a TZID (RFC 5545 3.3.10).
                last = ev["start"] + timedelta(days=ev["daily_count"] - 1)
                lines.append(f"RRULE:FREQ=DAILY;UNTIL={utc_stamp(last)}")

        lines.append(f"SUMMARY:{esc(ev['title'])}")
        if ev["description"]:
            lines.append(f"DESCRIPTION:{esc(ev['description'])}")
        if ev["url"]:
            lines.append(f"URL:{ev['url']}")
        if ev["categories"]:
            lines.append(f"CATEGORIES:{esc(','.join(ev['categories']))}")
        lines.append("TRANSP:TRANSPARENT")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(line) for line in lines) + "\r\n"


# --------------------------------------------------------------------------- #
# State (stable SEQUENCE / LAST-MODIFIED so the file only churns on real change)
# --------------------------------------------------------------------------- #

def update_state(events: list[dict], state_path: Path) -> dict:
    try:
        state = json.loads(state_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}

    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fresh: dict = {}
    for ev in events:
        digest = content_hash(ev)
        previous = state.get(ev["uid"])
        if previous and previous.get("hash") == digest:
            fresh[ev["uid"]] = previous
        elif previous:
            fresh[ev["uid"]] = {"hash": digest,
                                "seq": int(previous.get("seq", 0)) + 1,
                                "stamp": now}
        else:
            fresh[ev["uid"]] = {"hash": digest, "seq": 0, "stamp": now}

    state_path.write_text(json.dumps(fresh, indent=1, sort_keys=True) + "\n")
    return fresh


# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="calendar.ics", help="output .ics path")
    ap.add_argument("--state", default="state.json", help="sequence-tracking state file")
    ap.add_argument("--calid", type=int, default=1)
    ap.add_argument("--pid", type=int, default=9)
    ap.add_argument("--viewid", type=int, default=1)
    ap.add_argument("--months-back", type=int, default=1)
    ap.add_argument("--months-ahead", type=int, default=13)
    ap.add_argument("--name", default="Highworth Warneford School")
    ap.add_argument("--no-daily-expand", action="store_true",
                    help="treat a multi-day timed event as one continuous block "
                         "instead of a daily recurrence")
    ap.add_argument("--min-events", type=int, default=1,
                    help="abort without writing if fewer events than this are found, "
                         "so a broken scrape never wipes a working feed")
    ap.add_argument("--json-file", help="read events from a local JSON file instead "
                                        "of the network (for testing)")
    args = ap.parse_args(argv)

    if args.json_file:
        raw_events = json.loads(Path(args.json_file).read_text())
    else:
        raw_events = fetch_all(args.calid, args.pid, args.viewid,
                               args.months_back, args.months_ahead)

    events = [e for e in (normalise(r, not args.no_daily_expand)
                          for r in raw_events) if e]
    print(f"fetched {len(raw_events)} raw records -> {len(events)} usable events",
          file=sys.stderr)

    if len(events) < args.min_events:
        print(f"ERROR: only {len(events)} events found (min {args.min_events}); "
              f"refusing to overwrite {args.out}", file=sys.stderr)
        return 1

    state = update_state(events, Path(args.state))
    Path(args.out).write_text(build_ics(events, state, args.name), newline="")
    print(f"wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
