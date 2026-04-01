"""
ForexFactory Breaking News Scraper + Pine Seeds Encoder
========================================================
Scrapes forexfactory.com/news for red-folder (breaking) headlines,
encodes them into OHLCV numeric format compatible with TradingView
Pine Seeds (request.seed), and writes the output CSV.

Uses Playwright (headless Chromium) because FF /news is fully
JavaScript-rendered — BeautifulSoup alone gets an empty page.

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

from datetime import datetime, timezone, timedelta
import csv
import os
import re

# ── Character encoding table ──

CHAR_TO_CODE = {' ': '01'}

for i, c in enumerate('ABCDEFGHIJKLMNOPQRSTUVWXYZ', start=2):
    CHAR_TO_CODE[c] = f'{i:02d}'

for i, c in enumerate('abcdefghijklmnopqrstuvwxyz', start=28):
    CHAR_TO_CODE[c] = f'{i:02d}'

for i, c in enumerate('0123456789', start=54):
    CHAR_TO_CODE[c] = f'{i:02d}'

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
            codes.append('00')
    return int(''.join(codes)) if codes else 0


def encode_headline(headline):
    """Encode a headline string into 5 OHLCV integer values."""
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


# ── ForexFactory Scraper (Playwright) ──

FF_NEWS_URL = "https://www.forexfactory.com/news"


def scrape_ff_breaking_news():
    """
    Scrape ForexFactory /news page for breaking headlines using Playwright.
    Returns list of dicts: [{"timestamp": datetime, "headline": str, "impact": str}, ...]

    FF impact CSS classes (from page source):
      - universal-impact__impact-high--ff   (red)
      - universal-impact__impact-medium--ff (orange)
      - universal-impact__impact-low--ff    (yellow)
    """
    from playwright.sync_api import sync_playwright

    items = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        print(f"[INFO] Loading {FF_NEWS_URL} ...")
        try:
            page.goto(FF_NEWS_URL, wait_until="networkidle", timeout=60000)
        except Exception as e:
            print(f"[WARN] Navigation timeout/error (may still have content): {e}")

        # Wait for news stories to render
        try:
            page.wait_for_selector(".news__story, .flexposts__story, .story", timeout=15000)
            print("[INFO] News stories loaded")
        except Exception:
            print("[WARN] Could not find news story elements, trying broader search...")

        # Get the full rendered HTML
        html = page.content()
        browser.close()

    # Parse the rendered HTML with BeautifulSoup
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    # ── Strategy 1: Look for story elements with impact indicators ──
    # FF news stories typically have an impact dot/icon
    story_selectors = [
        ".news__story",
        ".flexposts__story",
        ".story",
        "tr[data-story-id]",
        "div[data-story-id]",
        "[class*='story']",
    ]

    stories = []
    for sel in story_selectors:
        stories = soup.select(sel)
        if stories:
            print(f"[INFO] Found {len(stories)} stories with selector: {sel}")
            break

    for story in stories:
        # Check impact level — we want HIGH (red) only
        # Actual FF HTML uses: <img class='svg-img svg-img--impact-ff-high'>
        impact_el = story.select_one(
            "img[class*='impact-ff-high'], "
            ".svg-img--impact-ff-high, "
            ".universal-impact__impact-high--ff, "
            "[class*='impact-high'], "
            "[class*='impact--red']"
        )
        if not impact_el:
            continue

        # Extract headline text
        title_el = story.select_one(
            ".news__title, .flexposts__title, .story__title, "
            ".news__story-title, a[class*='title'], "
            "a[data-story-url], .title, a"
        )
        if not title_el:
            # Try getting any link text
            title_el = story.select_one("a")
        if not title_el:
            continue

        headline = title_el.get_text(strip=True)
        if not headline or len(headline) < 3:
            continue

        # Extract timestamp
        time_el = story.select_one(
            ".news__time, .flexposts__time, .story__time, "
            "time, [class*='time'], [class*='date']"
        )
        ts = datetime.now(timezone.utc)
        if time_el:
            time_text = time_el.get_text(strip=True)
            ts = _parse_ff_time(time_text, ts)
            # Also check datetime attribute
            if time_el.get("datetime"):
                try:
                    ts = datetime.fromisoformat(time_el["datetime"].replace("Z", "+00:00"))
                except Exception:
                    pass

        items.append({
            "timestamp": ts,
            "headline": headline[:MAX_HEADLINE_LEN],
            "impact": "high",
        })

    # ── Strategy 2: Broader fallback — find impact-high images and walk up ──
    if not items:
        print("[INFO] Strategy 1 found nothing, trying broader search...")
        all_impacts = soup.select(
            "img[class*='impact-ff-high'], "
            ".svg-img--impact-ff-high, "
            ".universal-impact__impact-high--ff, "
            "[class*='impact-high'], [class*='impact--red']"
        )
        seen_headlines = set()
        for el in all_impacts:
            # Walk up to the story content container
            parent = el.find_parent("div", class_=lambda c: c and "news-block" in " ".join(c) if isinstance(c, list) else c and "news-block" in c)
            if not parent:
                parent = el.find_parent(["div", "article", "li", "section"])
            if not parent:
                continue

            # The headline is plain text after the links, inside the details div
            details = parent.select_one(".news-block__details, .darktext, [class*='details']")
            if not details:
                details = parent

            # Get the full text and strip out link text to isolate the headline
            full_text = details.get_text(strip=False)
            # Remove link texts from the full text
            for link in details.select("a"):
                link_text = link.get_text(strip=False)
                full_text = full_text.replace(link_text, "", 1)

            # Clean up: remove pipes, timestamps, whitespace
            headline = re.sub(r'\|[^|]*?\|', ' ', full_text)  # remove |time|comments|
            headline = re.sub(r'\|', ' ', headline)
            headline = re.sub(r'\s+', ' ', headline).strip()

            # Also try extracting from the <a> href slug as fallback
            story_link = details.select_one("a.darklink, a[href*='/news/']")
            slug_headline = ""
            if story_link and story_link.get("href"):
                href = story_link["href"]
                # Extract slug: /news/1391546-trump-irans-new-regime-president...
                slug_match = re.search(r'/news/\d+-(.+)', href)
                if slug_match:
                    slug_headline = slug_match.group(1).replace("-", " ").strip()

            # Use the longer of the two (text content vs slug)
            if len(slug_headline) > len(headline) or len(headline) < 10:
                headline = slug_headline

            if not headline or len(headline) < 5:
                print(f"  [DEBUG] Skipping: no usable headline from: {full_text[:80]}")
                continue

            truncated = headline[:MAX_HEADLINE_LEN]
            if truncated in seen_headlines:
                continue
            seen_headlines.add(truncated)

            # Try to parse timestamp
            time_match = re.search(r'(\d+)\s*(min|hr|hour|sec)\s*ago', details.get_text())
            ts = datetime.now(timezone.utc)
            if time_match:
                val = int(time_match.group(1))
                unit = time_match.group(2)
                if 'min' in unit:
                    ts -= timedelta(minutes=val)
                elif 'hr' in unit or 'hour' in unit:
                    ts -= timedelta(hours=val)

            print(f"  [DEBUG] Found headline: {truncated}")
            items.append({
                "timestamp": ts,
                "headline": truncated,
                "impact": "high",
            })

    # ── Strategy 3: Debug — dump what we see ──
    if not items:
        print("[INFO] No red-impact stories found. Dumping page structure for debugging...")
        # Print all class names that contain 'impact' or 'story' or 'news'
        for tag in soup.find_all(True):
            classes = tag.get("class", [])
            class_str = " ".join(classes)
            if any(kw in class_str.lower() for kw in ["impact", "story", "news__"]):
                text = tag.get_text(strip=True)[:80]
                print(f"  <{tag.name} class='{class_str}'> {text}")

    print(f"[INFO] Found {len(items)} red-folder breaking news items")
    return items


def _parse_ff_time(time_text, default):
    """Best-effort parse of ForexFactory time strings."""
    t = time_text.lower().strip()
    now = default

    # "X min ago", "X hrs ago", "X sec ago"
    if "ago" in t:
        m = re.search(r'(\d+)\s*(min|hr|hour|sec)', t)
        if m:
            val = int(m.group(1))
            unit = m.group(2)
            if 'min' in unit:
                return now - timedelta(minutes=val)
            elif 'hr' in unit or 'hour' in unit:
                return now - timedelta(hours=val)
            elif 'sec' in unit:
                return now - timedelta(seconds=val)

    # "Jan 15 at 2:30pm", "Mar 3, 2025 10:15am"
    for fmt in ["%b %d at %I:%M%p", "%b %d, %Y %I:%M%p", "%b %d %I:%M%p",
                "%b %d at %I:%M %p", "%b %d, %Y %I:%M %p"]:
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
    existing = set()
    if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        with open(output_path, 'r') as f:
            reader = csv.reader(f)
            next(reader, None)  # skip header
            for row in reader:
                if row:
                    existing.add(row[0])

    rows = []
    for item in items:
        ts = item["timestamp"]
        date_str = ts.strftime("%Y%m%dT%H%M%S")
        if date_str in existing:
            continue
        o, h, l, c, v = encode_headline(item["headline"])
        rows.append([date_str, o, h, l, c, v])

    if not rows and not existing:
        with open(output_path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(["date", "open", "high", "low", "close", "volume"])
        print("[INFO] Created empty CSV with header")
        return

    if rows:
        # Read existing rows, merge, and rewrite
        all_rows = []
        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            with open(output_path, 'r') as f:
                reader = csv.reader(f)
                next(reader, None)
                for row in reader:
                    if row:
                        all_rows.append(row)
        all_rows.extend(rows)

        with open(output_path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(["date", "open", "high", "low", "close", "volume"])
            for row in all_rows:
                writer.writerow(row)

        print(f"[INFO] Wrote {len(rows)} new breaking news entries to {output_path}")
    else:
        print("[INFO] No new entries to write")


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
