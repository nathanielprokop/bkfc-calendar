import os, re, hashlib, datetime
from typing import List, Dict, Optional
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from dateutil import tz
from ics import Calendar, Event

# Sources
BKFC_EVENTS_URL = "https://www.bkfc.com/events"   # official upcoming events
FIGHTMAG_URL     = "https://schedule.fightmag.com/events/categories/bkfc/"  # cross-check

UA = {"User-Agent": "Mozilla/5.0 (BKFC iCal Generator)"}

# Common timezone abbreviations -> IANA zones (for stable UTC conversion)
TZ_ABBREV = {
    "PDT": "America/Los_Angeles",
    "PST": "America/Los_Angeles",
    "MDT": "America/Denver",
    "MST": "America/Denver",
    "CDT": "America/Chicago",
    "CST": "America/Chicago",
    "EDT": "America/New_York",
    "EST": "America/New_York",
    "BST": "Europe/London",
    "GMT": "Etc/GMT",
    "CEST": "Europe/Berlin",
    "CET": "Europe/Berlin",
    "EEST": "Europe/Sofia",
    "EET": "Europe/Sofia",
    "IST": "Europe/Dublin",  # rarely used here; adjust if needed
}

def tzinfos(tzname, offset):
    if tzname and tzname.upper() in TZ_ABBREV:
        return tz.gettz(TZ_ABBREV[tzname.upper()])
    return None

def fetch(url: str) -> str:
    r = requests.get(url, headers=UA, timeout=25)
    r.raise_for_status()
    return r.text

def find_datetime_strings(text: str) -> List[str]:
    # Find things like "October 4, 2025 2:00 PM PDT" or "Oct 4, 2025 2:00 PM PST"
    pattern_long = r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s+\d{4}\s+\d{1,2}:\d{2}\s*(AM|PM)\s*[A-Za-z]{2,4}"
    return re.findall(pattern_long, text, flags=re.IGNORECASE)

def search_datetime_full(text: str) -> Optional[str]:
    # return first full "Month Day, Year HH:MM AM/PM TZN" match as string
    m = re.search(r"([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4}\s+\d{1,2}:\d{2}\s*(AM|PM)\s*[A-Za-z]{2,4})", text, re.IGNORECASE)
    return m.group(1) if m else None

def search_datetime_lenient(text: str) -> Optional[str]:
    # fallback: "Month Day, Year HH:MM AM/PM" (no TZ)
    m = re.search(r"([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4}\s+\d{1,2}:\d{2}\s*(AM|PM))", text, re.IGNORECASE)
    return m.group(1) if m else None

def normalize_title(t: str) -> str:
    t = re.sub(r"\s+", " ", t).strip().lower()
    # drop common boilerplate
    t = t.replace("bkfc", "")
    t = t.replace("event", "")
    t = t.replace("fight night", "")
    # keep fighter names and location keywords
    t = re.sub(r"[^a-z0-9 :,&-]", "", t)
    return t.strip()

def parse_bkfc_listing() -> List[str]:
    """Return unique event detail URLs from the BKFC events listing"""
    html = fetch(BKFC_EVENTS_URL)
    soup = BeautifulSoup(html, "lxml")
    urls = set()

    # Collect all event links under /events/ (detail pages)
    for a in soup.select("a[href*='/events/']"):
        href = a.get("href", "")
        if "/events/" in href and "http" not in href:
            urls.add("https://www.bkfc.com" + href)
        elif href.startswith("https://www.bkfc.com/events/"):
            urls.add(href)

    # Light filter: prefer pages that are not pure ticket links
    urls = {u for u in urls if len(u.split("/")) >= 5}
    return sorted(urls)

def parse_event_detail(url: str) -> Optional[Dict]:
    """Parse a single BKFC event page for title, datetime, location."""
    try:
        html = fetch(url)
    except Exception:
        return None

    soup = BeautifulSoup(html, "lxml")

    # Title: prefer <h1>, else og:title
    title = None
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        title = h1.get_text(strip=True)
    if not title:
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            title = og["content"]

    # Location: try common patterns
    location = ""
    for sel in ["[class*='venue']", "[class*='location']", "address", "li", "p"]:
        el = soup.select_one(sel)
        if el and ("," in el.get_text(" ", strip=True)):
            location = el.get_text(" ", strip=True)
            break
    # If still empty, search for "City, ST" in whole page text
    if not location:
        m = re.search(r"([A-Za-z .'-]+,\s*[A-Za-z]{2,}(?:,\s*[A-Za-z .'-]+)?)", soup.get_text(" ", strip=True))
        if m:
            location = m.group(1)

    # Date/time: prefer a full string with timezone on the page
    page_text = soup.get_text(" ", strip=True)
    dt_text = search_datetime_full(page_text)

    # Fallbacks:
    if not dt_text:
        # look inside obvious time containers
        for sel in ["time", "[class*='time']", "[class*='date']"]:
            el = soup.select_one(sel)
            if el:
                maybe = search_datetime_full(el.get_text(" ", strip=True))
                if maybe:
                    dt_text = maybe
                    break
        if not dt_text:
            # very last resort: accept no-timezone (we'll assume US Eastern if venue is on East coast)
            dt_text = search_datetime_lenient(page_text)

    # Parse into aware datetime (UTC)
    start_utc = None
    if dt_text:
        try:
            # dateutil will use tzinfos to convert PDT/EDT/etc to real zones
            parsed = dateparser.parse(dt_text, fuzzy=True, tzinfos=tzinfos)
            if parsed.tzinfo is None:
                # try to infer by location keywords
                loc = (location or "").lower()
                guess = None
                if any(k in loc for k in ["fl", "florida", "nj", "new jersey", "newark", "miami", "hollywood, fl"]):
                    guess = tz.gettz("America/New_York")
                elif any(k in loc for k in ["in", "indiana", "hammond"]):
                    guess = tz.gettz("America/Chicago")
                elif any(k in loc for k in ["uk", "manchester", "england", "london"]):
                    guess = tz.gettz("Europe/London")
                elif any(k in loc for k in ["italy", "rome"]):
                    guess = tz.gettz("Europe/Rome")
                elif any(k in loc for k in ["bulgaria", "burgas"]):
                    guess = tz.gettz("Europe/Sofia")
                if guess is None:
                    # site often lists PDT for all—fallback to Los Angeles
                    guess = tz.gettz("America/Los_Angeles")
                parsed = parsed.replace(tzinfo=guess)
            start_utc = parsed.astimezone(tz.UTC)
        except Exception:
            start_utc = None

    # If we still don't have a datetime, skip this event
    if not start_utc:
        return None

    # Clean title
    title = title or "BKFC Event"
    title = re.sub(r"\s+", " ", title).strip()

    return {
        "title": title,
        "start_utc": start_utc,
        "location": location or "",
        "url": url,
    }

def parse_fightmag_events() -> List[Dict]:
    """Lightweight cross-check: title + date (no times on some posts)."""
    try:
        html = fetch(FIGHTMAG_URL)
    except Exception:
        return []
    soup = BeautifulSoup(html, "lxml")
    events = []
    # headings (e.g., h2 entries with date nearby)
    for block in soup.select("h2, h3"):
        title = block.get_text(" ", strip=True)
        if not title or "BKFC" not in title.upper():
            continue
        # look for a date in same section
        section_text = " ".join([title, block.find_next().get_text(" ", strip=True) if block.find_next() else ""])
        m = re.search(r"([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})", section_text)
        dt_date = None
        if m:
            try:
                d = dateparser.parse(m.group(1), fuzzy=True)
                dt_date = d.date()
            except Exception:
                pass
        events.append({"title_norm": normalize_title(title), "date": dt_date})
    return events

def cross_check(primary: Dict, fm: List[Dict]) -> Dict:
    """If FightMag has the same event name/date, we keep BKFC time; this just acts as sanity."""
    pn = normalize_title(primary["title"])
    pd = primary["start_utc"].astimezone(tz.UTC).date()
    confidence = "ok"
    for e in fm:
        if e["date"] and abs((e["date"] - pd).days) <= 1:
            # title fuzzy match
            if any(token and token in pn for token in e["title_norm"].split()[:4]):
                confidence = "verified"
                break
    return {**primary, "confidence": confidence}

def build_calendar(events: List[Dict]) -> Calendar:
    cal = Calendar()
    for e in events:
        ev = Event()
        ev.name = e["title"]
        ev.begin = e["start_utc"]  # aware dt -> ics will write UTC
        ev.duration = datetime.timedelta(hours=3)
        ev.location = e.get("location", "")
        ev.url = e.get("url", "")
        uid_src = f"{e['url']}|{e['start_utc'].isoformat()}"
        ev.uid = hashlib.md5(uid_src.encode()).hexdigest() + "@bkfc"
        cal.events.add(ev)
    return cal

def main():
    # 1) collect BKFC detail pages
    detail_urls = parse_bkfc_listing()

    # 2) parse each event page
    primary_events = []
    for url in detail_urls:
        try:
            e = parse_event_detail(url)
            if e:
                primary_events.append(e)
        except Exception:
            continue

    # keep only future (and recent) events
    cutoff = datetime.datetime.now(tz.UTC) - datetime.timedelta(days=7)
    primary_events = [e for e in primary_events if e["start_utc"] and e["start_utc"] > cutoff]
    primary_events.sort(key=lambda x: x["start_utc"])

    # 3) light cross-check
    fm_events = parse_fightmag_events()
    checked = [cross_check(e, fm_events) for e in primary_events]

    # 4) build & write ICS
    cal = build_calendar(checked)
    out_path = "bkfc.ics"
    with open(out_path, "w", encoding="utf-8") as f:
        f.writelines(cal.serialize_iter())
    print(f"Generated {out_path} with {len(cal.events)} events")

if __name__ == "__main__":
    main()
