"""
ForexFactory Breaking News Scraper + Pine Seeds Encoder
========================================================
Scrapes forexfactory.com/news for red-folder (breaking) headlines,
encodes them into OHLCV numeric format compatible with TradingView
Pine Seeds (request.seed), and writes the output CSV.

Encoding scheme
---------------
Each breaking news item is encoded into ONE candle row:
  date  = YYYYMMDDTHHMMSS  (Pine Seeds intraday timestamp)
  open  = encoded chars  1-7  of headline
  high  = encoded chars  8-14 of headline
  low   = encoded chars 15-21 of headline
  close = encoded chars 22-28 of headline
  volume= encoded chars 29-35 of headline

Character mapping: each ASCII char -> 2-digit code (01-99).
  space=01, A-Z=02-27, a-z=28-53, 0-9=54-63,
  common punctuation mapped 64+.
7 chars per field * 2 digits = 14-digit number per OHLCV field.
Max ~35 chars per candle. Longer headlines are truncated.
"""

import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone
import csv
import os
import json
import time as time_mod

# ── Character encoding table ──

CHAR_TO_CODE = {' ': '01'}

# A-Z -> 02-27
for i, c in enumerate('ABCDEFGHIJKLMNOPQRSTUVWXYZ', start=2):
    CHAR_TO_CODE[c] = f'{i:02d}'

# a-z -> 28-53
for i, c in enumerate('abcdefghijklmnopqrstuvwxyz', start=28):
    CHAR_TO_CODE[c] = f'{i:02d}'

# 0-9 -> 54-63
for i, c in enumerate('0123456789', start=54):
    CHAR_TO_CODE[c] = f'{i:02d}'

# Punctuation -> 64+
PUNCT = ".,;:!?'-/()+=%$&#@\"\\[]{}|<>~`^_*"
for i, c in enumerate(PUNCT, start=64):
    CHAR_TO_CODE[c] = f'{i:02d}'

CODE_TO_CHAR = {v: k for k, v in CHAR_TO_CODE.items()}

CHARS_PER_FIELD = 7
NUM_FIELDS = 5  # open, high, low, close, volume
MAX_HEADLINE_LEN = CHARS_PER_FIELD * NUM_FIELDS  # 35


def encode_chars(text, start, count):
    """Encode `count` characters from `text[start:]` into one numeric value."""
    codes = []
    for i in range(count):
        idx = start + i
        if idx < len(text):
            ch = text[idx]
            codes.append(CHAR_TO_CODE.get(ch, '00'))
        else:
            codes.append('00')  # padding
    return int(''.join(codes)) if codes else 0


def encode_headline(headline):
    """Encode a headline string into 5 OHLCV integer values."""
    # Truncate if needed
    h = headline[:MAX_HEADLINE_LEN]
    o = encode_chars(h, 0 * CHARS_PER_FIELD, CHARS_PER_FIELD)
    hi = encode_chars(h, 1 * CHARS_PER_FIELD, CHARS_PER_FIELD)
    lo = encode_chars(h, 2 * CHARS_PER_FIELD, CHARS_PER_FIELD)
    cl = encode_chars(h, 3 * CHARS_PER_FIELD, CHARS_PER_FIELD)
    vo = encode_chars(h, 4 * CHARS_PER_FIELD, CHARS_PER_FIELD)
    return o, hi, lo, cl, vo


def decode_field(value, count=CHARS_PER_FIELD):
    """Decode one OHLCV numeric value back into characters."""
    s = str(int(value)).zfill(count * 2)
    chars = []
    for i in range(0, len(s), 2):
        code = s[i:i+2]
        if code == '00':
            break
        chars.append(CODE_TO_CHAR.get(code, '?'))
    return ''.join(chars)


def decode_headline(o, h, l, c, v):
    """Decode 5 OHLCV values back into a headline string."""
    parts = [decode_field(x) for x in [o, h, l, c, v]]
    return ''.join(parts).rstrip()


# ── ForexFactory Scraper ──

FF_NEWS_URL = "https://www.forexfactory.com/news"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def scrape_ff_breaking_news():
    """
    Scrape ForexFactory /news page for red-folder breaking headlines.
    Returns list of dicts: [{"timestamp": datetime, "headline": str}, ...]
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    try:
        resp = session.get(FF_NEWS_URL, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[ERROR] Failed to fetch FF /news: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    items = []

    # FF news page uses table rows or divs with news items.
    # Red-folder items have a specific class/icon indicator.
    # The structure may vary; we look for common patterns.

    # Strategy 1: Look for news rows with red impact indicator
    for row in soup.select("tr.news__row, tr.news_item, div.news__item"):
        # Check for red folder / breaking indicator
        impact_el = row.select_one(
            ".news__impact--red, .impact--red, "
            "td.news__impact span.icon--red, "
            "img[src*='red'], img[alt*='High']"
        )
        if not impact_el:
            continue

        # Extract headline
        title_el = row.select_one(
            ".news__title, .news_title, "
            "td.news__title a, a.news__title"
        )
        if not title_el:
            continue
        headline = title_el.get_text(strip=True)
        if not headline:
            continue

        # Extract timestamp
        time_el = row.select_one(
            ".news__time, .news_date, "
            "td.news__time, time"
        )
        ts = datetime.now(timezone.utc)
        if time_el:
            time_text = time_el.get_text(strip=True)
            # Try to parse relative or absolute time
            ts = _parse_ff_time(time_text, ts)

        items.append({
            "timestamp": ts,
            "headline": headline[:MAX_HEADLINE_LEN],
        })

    # Strategy 2: Fallback — broader search for any red-flagged news
    if not items:
        for link in soup.select("a[href*='/news/']"):
            parent = link.find_parent("tr") or link.find_parent("div")
            if not parent:
                continue
            red_indicator = parent.select_one(
                "[class*='red'], [class*='breaking'], "
                "img[src*='red']"
            )
            if not red_indicator:
                continue
            headline = link.get_text(strip=True)
            if headline and len(headline) > 5:
                items.append({
                    "timestamp": datetime.now(timezone.utc),
                    "headline": headline[:MAX_HEADLINE_LEN],
                })

    print(f"[INFO] Found {len(items)} red-folder breaking news items")
    return items


def _parse_ff_time(time_text, default):
    """Best-effort parse of ForexFactory time strings."""
    t = time_text.lower().strip()
    now = default

    # "X min ago", "X hrs ago"
    if "ago" in t:
        import re
        m = re.search(r'(\d+)\s*(min|hr|hour|sec)', t)
        if m:
            val = int(m.group(1))
            unit = m.group(2)
            from datetime import timedelta
            if 'min' in unit:
                return now - timedelta(minutes=val)
            elif 'hr' in unit or 'hour' in unit:
                return now - timedelta(hours=val)
            elif 'sec' in unit:
                return now - timedelta(seconds=val)
    # "Jan 15 at 2:30pm"
    for fmt in ["%b %d at %I:%M%p", "%b %d, %Y %I:%M%p", "%b %d %I:%M%p"]:
        try:
            parsed = datetime.strptime(t, fmt)
            return parsed.replace(year=now.year, tzinfo=timezone.utc)
        except ValueError:
            continue

    return default


# ── CSV Writer (Pine Seeds format) ──

def write_pine_seeds_csv(items, output_path):
    """
    Write breaking news items as Pine Seeds CSV.
    Format: date,open,high,low,close,volume
    Date format for intraday: YYYYMMDDTHHMMSS
    """
    # Load existing data to avoid duplicates
    existing = set()
    if os.path.exists(output_path):
        with open(output_path, 'r') as f:
            reader = csv.reader(f)
            next(reader, None)  # skip header
            for row in reader:
                if row:
                    existing.add(row[0])  # date key

    rows = []
    for item in items:
        ts = item["timestamp"]
        date_str = ts.strftime("%Y%m%dT%H%M%S")

        if date_str in existing:
            continue

        o, h, l, c, v = encode_headline(item["headline"])
        rows.append([date_str, o, h, l, c, v])

    if not rows and not existing:
        # Write header only
        with open(output_path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(["date", "open", "high", "low", "close", "volume"])
        print("[INFO] Created empty CSV with header")
        return

    # Append new rows (or create file)
    write_header = not os.path.exists(output_path) or os.path.getsize(output_path) == 0
    mode = 'a' if os.path.exists(output_path) and not write_header else 'w'

    with open(output_path, mode, newline='') as f:
        writer = csv.writer(f)
        if write_header or mode == 'w':
            # Re-read existing and merge
            all_rows = []
            if os.path.exists(output_path) and mode == 'a':
                pass  # appending
            else:
                writer.writerow(["date", "open", "high", "low", "close", "volume"])
        for row in rows:
            writer.writerow(row)

    print(f"[INFO] Wrote {len(rows)} new breaking news entries to {output_path}")


# ── Main ──

def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(script_dir)
    csv_path = os.path.join(repo_root, "FF_BREAKING_NEWS_SLOT_1", "data.csv")

    os.makedirs(os.path.dirname(csv_path), exist_ok=True)

    print(f"[INFO] Scraping ForexFactory /news at {datetime.now(timezone.utc).isoformat()}")
    items = scrape_ff_breaking_news()

    write_pine_seeds_csv(items, csv_path)

    # Verify round-trip encoding
    for item in items[:3]:
        o, h, l, c, v = encode_headline(item["headline"])
        decoded = decode_headline(o, h, l, c, v)
        print(f"  Original:  {item['headline']}")
        print(f"  Decoded:   {decoded}")
        print(f"  Match: {item['headline'][:MAX_HEADLINE_LEN].rstrip() == decoded.rstrip()}")


if __name__ == "__main__":
    main()
