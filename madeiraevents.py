import requests
import re
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ──── 1. Fetch the events page ────
print("⏳ Fetching events from visitmadeira.com...")
url = "https://visitmadeira.com/pt/a-acontecer/eventos/"
resp = requests.get(url)
resp.raise_for_status()
html = resp.text

# ──── 2. Extract the embedded JSON from loadEvents() ────
# Events are embedded in a JS function: function loadEvents() { return { events: [...] } }
start_marker = "events: ["
marker_pos = html.find(start_marker, 100000)
if marker_pos == -1:
    raise RuntimeError("Could not find events data in page HTML")
arr_start = marker_pos + len("events: [")

# Parse each event object individually (handles escape edge cases)
parsed_events = []
pos = arr_start
depth = 0
obj_start = None

for i in range(arr_start, len(html)):
    ch = html[i]
    if ch == "{" and depth == 0:
        obj_start = i
        depth = 1
    elif ch == "{":
        depth += 1
    elif ch == "}":
        depth -= 1
        if depth == 0 and obj_start is not None:
            obj_str = html[obj_start : i + 1]
            try:
                parsed_events.append(json.loads(obj_str))
            except json.JSONDecodeError:
                pass  # skip malformed entries
            obj_start = None
    elif ch == "]" and depth == 0:
        break

print(f"📦 Found {len(parsed_events)} total events")

# Show which years exist in the source data
from collections import Counter
years_in_data = Counter()
for ev in parsed_events:
    for itv in ev.get("event_date_intervals", []):
        sd = itv.get("start_date", "")
        if sd and len(sd) >= 4:
            try:
                years_in_data[int(sd[:4])] += 1
            except ValueError:
                pass
if years_in_data:
    print(f"📆 Years in source data: {dict(sorted(years_in_data.items()))}")

# ──── 3. Filter to current year + next year ────
current_year = datetime.now().year
next_year = current_year + 1
allowed_years = {current_year, next_year}

selected_events = []
for ev in parsed_events:
    intervals = ev.get("event_date_intervals", [])
    for interval in intervals:
        start_date = interval.get("start_date", "")
        if start_date and len(start_date) >= 4 and int(start_date[:4]) in allowed_years:
            selected_events.append(ev)
            break

print(f"📅 Filtered to {len(selected_events)} events in {current_year}–{next_year}")

# ──── 4. Generate ICS file ────
BASE_URL = "https://visitmadeira.com"
now_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

ics_lines = [
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    "PRODID:-//Visit Madeira Events Scraper//EN",
    "CALSCALE:GREGORIAN",
    "METHOD:PUBLISH",
    f"X-WR-CALNAME:Madeira Events {current_year}-{next_year}",
]


def ics_escape(text):
    """Escape text for ICS format."""
    if not text:
        return ""
    text = text.replace("\\", "\\\\")
    text = text.replace(";", "\\;")
    text = text.replace(",", "\\,")
    text = text.replace("\n", "\\n")
    text = text.replace("\r", "")
    return text


def fold_line(line, max_len=75):
    """Fold long lines per RFC 5545 (max 75 octets per line)."""
    if len(line.encode("utf-8")) <= max_len:
        return line
    result = []
    current = ""
    for char in line:
        test = current + char
        if len(test.encode("utf-8")) > max_len:
            result.append(current)
            current = " " + char
        else:
            current = test
    if current:
        result.append(current)
    return "\r\n".join(result)


event_count = 0
for ev in selected_events:
    title = ev.get("event_title", "Untitled Event").strip()
    location = ev.get("event_location", "").strip()
    description = ev.get("event_description", "").strip()
    event_url = ev.get("event_url", "")
    event_website = ev.get("event_website", "")

    # Build the full URL
    if event_url and not event_url.startswith("http"):
        full_url = BASE_URL + event_url
    else:
        full_url = event_url or ""

    # Add link to description
    desc_parts = []
    if description:
        desc_parts.append(description)
    if full_url:
        desc_parts.append(f"Mais info: {full_url}")
    if event_website:
        desc_parts.append(f"Site oficial: {event_website}")
    full_description = "\\n".join(desc_parts)

    # Get date intervals in current year or next year only
    intervals = ev.get("event_date_intervals", [])
    intervals_in_range = [
        itv for itv in intervals
        if itv.get("start_date", "") and len(itv.get("start_date", "")) >= 4
        and int(itv["start_date"][:4]) in allowed_years
    ]

    # Merge consecutive single-day intervals into ranges
    # (many events list each day separately instead of a date range)
    dates = []
    for itv in intervals_in_range:
        start_str = itv["start_date"][:10]
        end_str = itv.get("end_date", start_str)[:10]
        dt_start = datetime.strptime(start_str, "%Y-%m-%d")
        dt_end = datetime.strptime(end_str, "%Y-%m-%d")
        dates.append((dt_start, dt_end))

    # Sort and merge overlapping/consecutive date ranges
    dates.sort()
    merged = []
    for start, end in dates:
        if merged and start <= merged[-1][1] + timedelta(days=1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    for dt_start, dt_end in merged:
        # For all-day events, DTEND must be the day AFTER the last day (RFC 5545)
        dt_end_exclusive = dt_end + timedelta(days=1)

        uid = f"{ev['event_id']}-{dt_start.strftime('%Y%m%d')}@visitmadeira.com"

        ics_lines.append("BEGIN:VEVENT")
        ics_lines.append(f"UID:{uid}")
        ics_lines.append(f"DTSTAMP:{now_stamp}")
        ics_lines.append(f"DTSTART;VALUE=DATE:{dt_start.strftime('%Y%m%d')}")
        ics_lines.append(f"DTEND;VALUE=DATE:{dt_end_exclusive.strftime('%Y%m%d')}")
        ics_lines.append(fold_line(f"SUMMARY:{ics_escape(title)}"))
        if location:
            ics_lines.append(fold_line(f"LOCATION:{ics_escape(location)}"))
        if full_description:
            ics_lines.append(fold_line(f"DESCRIPTION:{ics_escape(full_description)}"))
        if full_url:
            ics_lines.append(f"URL:{full_url}")
        ics_lines.append("END:VEVENT")
        event_count += 1

ics_lines.append("END:VCALENDAR")

# ──── 5. Save the ICS file ────
# Stable filename so the same URL works every year (content is always current+next year)
output_path = Path(__file__).resolve().parent / "madeira_events.ics"
with open(output_path, "w", encoding="utf-8") as f:
    f.write("\r\n".join(ics_lines))

print(f"\n✅ Successfully created ICS file with {event_count} calendar entries!")
print(f"📁 Saved to: {output_path}")
print(f"\n💡 Double-click the file to import it into Apple Calendar.")

# ──── 6. Print summary ────
print(f"\n{'─' * 70}")
print(f"  {'EVENT':<45} {'DATES'}")
print(f"{'─' * 70}")
for ev in sorted(selected_events, key=lambda e: e["event_date_intervals"][0]["start_date"]):
    title = ev["event_title"].strip()
    intervals = ev.get("event_date_intervals", [])
    dates = []
    for itv in intervals:
        if itv.get("start_date", "") and len(itv.get("start_date", "")) >= 4 and int(itv["start_date"][:4]) in allowed_years:
            start = itv["start_date"][:10]
            end = itv.get("end_date", "")[:10]
            dates.append((start, end))
    dates.sort()
    if dates:
        first_start = dates[0][0]
        last_end = dates[-1][1]
        if first_start == last_end or not last_end:
            date_str = first_start
        else:
            date_str = f"{first_start} → {last_end}"
        loc = ev.get("event_location", "")
        if loc:
            print(f"  {title[:43]:<43}  {date_str}  📍 {loc}")
        else:
            print(f"  {title[:43]:<43}  {date_str}")
