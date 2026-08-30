#!/usr/bin/env python3
"""
Brands For Less size watcher.

Checks a BFL search results page, opens each product, and reports any product
that has one of your target sizes in stock. Sends a Telegram message and/or an
email when a NEW match appears (it remembers what it already told you about).

Usage:
    python bfl_watch.py                 # normal run
    python bfl_watch.py --debug         # print what it parsed, send nothing
    python bfl_watch.py --calibrate URL # dump the size markup of one product
    python bfl_watch.py --force-notify  # re-send even if already alerted

Config comes from environment variables (see README).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
import time
from email.message import EmailMessage
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

SEARCH_URL = os.environ.get(
    "BFL_SEARCH_URL",
    "https://www.brandsforless.com/en-sa/search/?p_cat=men&q=ralph+lauren",
)

# Sizes you care about. Comma separated. Matching is exact + case-insensitive.
TARGET_SIZES = [
    s.strip().upper()
    for s in os.environ.get("BFL_TARGET_SIZES", "M").split(",")
    if s.strip()
]

STATE_FILE = Path(os.environ.get("BFL_STATE_FILE", "state.json"))
REQUEST_DELAY = float(os.environ.get("BFL_REQUEST_DELAY", "1.5"))
TIMEOUT = 30
BASE = "https://www.brandsforless.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Class-name fragments that mark a size chip as NOT buyable.
# Calibrate these once with --calibrate (see README).
UNAVAILABLE_HINTS = [
    h.strip().lower()
    for h in os.environ.get(
        "BFL_UNAVAILABLE_HINTS",
        "disable,disabled,sold,soldout,sold-out,out-of-stock,outofstock,"
        "unavailable,not-available,inactive,strike,crossed",
    ).split(",")
    if h.strip()
]

# Tokens that look like clothing sizes.
# Numerics are restricted to 20-60 so stock counts ("1 left") aren't mistaken
# for sizes.
SIZE_RE = re.compile(
    r"^(?:"
    r"XXS|XS|S|M|L|XL|XXL|XXXL|XXXXL|[2-6]XL|"       # letter sizes
    r"\d{2}\s*[xX]\s*\d{2}|"                          # 32 x 30 waist/length
    r"(?:[2-5]\d|60)(?:\.5)?"                         # numeric (EU / waist)
    r")$"
)

# Noise that sits inside a size chip alongside the size itself.
STOCK_NOTE_RE = re.compile(
    r"\b\d+\s*(?:piece|pieces|item|items|pc|pcs)?\s*left\b|\bonly\b|\bleft\b",
    re.I,
)

PRODUCT_URL_RE = re.compile(r"/en-sa/([a-z0-9\-]+)/(\d+)/p/", re.I)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def fetch(url: str, session: requests.Session, tries: int = 3) -> str:
    last = None
    for attempt in range(tries):
        try:
            r = session.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r.text
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last}")


# --------------------------------------------------------------------------
# Listing page -> product URLs
# --------------------------------------------------------------------------

def find_products(html: str) -> list[dict]:
    """Return [{'id', 'slug', 'url'}] for every product linked on the page."""
    seen: dict[str, dict] = {}
    soup = BeautifulSoup(html, "html.parser")

    for a in soup.find_all("a", href=True):
        m = PRODUCT_URL_RE.search(a["href"])
        if not m:
            continue
        slug, pid = m.group(1), m.group(2)
        if pid in seen:
            continue
        seen[pid] = {
            "id": pid,
            "slug": slug,
            "url": f"{BASE}/en-sa/{slug}/{pid}/p/",
        }

    # Fallback: some markup puts the link in an onclick / data attribute.
    if not seen:
        for m in PRODUCT_URL_RE.finditer(html):
            slug, pid = m.group(1), m.group(2)
            seen.setdefault(pid, {
                "id": pid,
                "slug": slug,
                "url": f"{BASE}/en-sa/{slug}/{pid}/p/",
            })

    return list(seen.values())


# --------------------------------------------------------------------------
# Product page -> available sizes
# --------------------------------------------------------------------------

def _classes(tag) -> str:
    cls = tag.get("class") or []
    if isinstance(cls, str):
        cls = [cls]
    bits = list(cls)
    if tag.get("data-testid"):
        bits.append(str(tag["data-testid"]))
    return " ".join(bits).lower()


def _is_unavailable(tag) -> bool:
    """Walk up a few levels looking for an out-of-stock marker."""
    node = tag
    for _ in range(4):
        if node is None or node.name is None:
            break
        blob = _classes(node)
        if any(h in blob for h in UNAVAILABLE_HINTS):
            return True
        if node.get("disabled") is not None or node.get("aria-disabled") == "true":
            return True
        node = node.parent
    return False


def _size_token(text: str) -> str | None:
    """Turn a chip's text into a bare size token, or None."""
    cleaned = STOCK_NOTE_RE.sub(" ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned or len(cleaned) > 12:
        return None

    upper = cleaned.upper()

    def normalize(tok: str) -> str:
        # "32x30" / "32 X 30" -> "32 x 30". Letter sizes (XL, 2XL) untouched.
        if re.fullmatch(r"\d{2}\s*X\s*\d{2}", tok):
            return re.sub(r"\s*X\s*", " x ", tok)
        return tok

    if SIZE_RE.match(upper):
        return normalize(upper)
    first = upper.split(" ")[0]
    if SIZE_RE.match(first):
        return normalize(first)
    return None


def _size_candidates(soup: BeautifulSoup) -> list:
    """Elements that represent one selectable size."""
    out = []
    for tag in soup.find_all(["li", "button", "span", "div", "label", "a"]):
        # skip containers that hold other chips
        if tag.find(["li", "button", "ul"]):
            continue
        text = tag.get_text(" ", strip=True)
        if not text or len(text) > 40:
            continue
        token = _size_token(text)
        if token:
            out.append((tag, token, text))
    return out


def parse_sizes(html: str) -> tuple[list[str], list[str]]:
    """Return (available_sizes, all_sizes_seen)."""
    soup = BeautifulSoup(html, "html.parser")

    available: list[str] = []
    everything: list[str] = []

    for tag, token, _full in _size_candidates(soup):
        if token not in everything:
            everything.append(token)
        if not _is_unavailable(tag):
            if token not in available:
                available.append(token)

    # If nothing was flagged unavailable, the markup hints are probably wrong.
    # Signal that so the caller can warn instead of silently over-reporting.
    if everything and len(available) == len(everything) and len(everything) > 2:
        return available, everything + ["__NO_STOCK_SIGNAL__"]

    return available, everything


def product_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        t = og["content"].replace(" | Brands For Less", "")
        t = re.sub(r"^Buy\s+", "", t)
        t = re.sub(r"\s+Online$", "", t)
        return t.strip()
    h1 = soup.find("h1")
    return h1.get_text(" ", strip=True) if h1 else "(unknown product)"


def product_price(html: str) -> str:
    m = re.search(r"SAR\s*[\d,]+", html)
    return m.group(0) if m else ""


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------

def _chunk(text: str, limit: int = 1900) -> list[str]:
    """Split on blank lines so a message never exceeds Discord's 2000 chars."""
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > limit and current:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    # Hard-split anything still oversized (one absurdly long product name).
    out = []
    for c in chunks:
        while len(c) > limit:
            out.append(c[:limit])
            c = c[limit:]
        out.append(c)
    return out


def notify_discord(text: str) -> bool:
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return False

    ok = True
    for part in _chunk(text):
        for attempt in range(4):
            r = requests.post(
                url,
                json={
                    "content": part,
                    # never ping @everyone even if a product name contains it
                    "allowed_mentions": {"parse": []},
                },
                timeout=TIMEOUT,
            )
            if r.status_code == 429:  # rate limited, obey retry_after
                wait = 2.0
                try:
                    wait = float(r.json().get("retry_after", 2.0))
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(min(wait, 30) + 0.5)
                continue
            if r.status_code in (200, 204):
                break
            print(
                f"[warn] discord failed: {r.status_code} {r.text[:200]}",
                file=sys.stderr,
            )
            ok = False
            break
        else:
            print("[warn] discord: gave up after rate limits", file=sys.stderr)
            ok = False
        time.sleep(0.4)
    return ok


def notify_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return False
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        print(f"[warn] telegram failed: {r.status_code} {r.text[:200]}", file=sys.stderr)
        return False
    return True


def notify_email(subject: str, body: str) -> bool:
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    to_addr = os.environ.get("ALERT_EMAIL_TO")
    if not all([host, user, password, to_addr]):
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    msg.set_content(body)

    port = int(os.environ.get("SMTP_PORT", "587"))
    try:
        with smtplib.SMTP(host, port, timeout=TIMEOUT) as s:
            s.starttls()
            s.login(user, password)
            s.send_message(msg)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] email failed: {exc}", file=sys.stderr)
        return False


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def calibrate(url: str, session: requests.Session) -> None:
    """Dump every size-like element with its classes, so selectors can be fixed."""
    html = fetch(url, session)
    soup = BeautifulSoup(html, "html.parser")
    print(f"Product: {product_title(html)}\n")
    print(f"{'TOKEN':<10} {'UNAVAIL?':<9} TAG / CLASSES")
    print("-" * 78)
    for tag, token, full in _size_candidates(soup):
        print(
            f"{token:<10} {str(_is_unavailable(tag)):<9} "
            f"<{tag.name} class='{_classes(tag)}'>  text={full!r}"
        )
    print("\nParent chain classes of the first size chip:")
    cands = _size_candidates(soup)
    if cands:
        node = cands[0][0]
        for _ in range(5):
            if node is None or node.name is None:
                break
            print(f"   <{node.name} class='{_classes(node)}'>")
            node = node.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true", help="print detail, send nothing")
    ap.add_argument("--calibrate", metavar="URL", help="dump size markup for one product")
    ap.add_argument("--force-notify", action="store_true", help="ignore saved state")
    args = ap.parse_args()

    session = requests.Session()

    if args.calibrate:
        calibrate(args.calibrate, session)
        return 0

    print(f"Search : {SEARCH_URL}")
    print(f"Sizes  : {', '.join(TARGET_SIZES)}")

    listing = fetch(SEARCH_URL, session)
    products = find_products(listing)
    print(f"Found  : {len(products)} product(s)\n")

    if not products:
        print("[warn] no products parsed - the listing markup may have changed.",
              file=sys.stderr)
        return 1

    matches = []
    stock_signal_broken = False

    for p in products:
        time.sleep(REQUEST_DELAY)
        try:
            html = fetch(p["url"], session)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] {p['id']}: {exc}", file=sys.stderr)
            continue

        available, seen = parse_sizes(html)
        if "__NO_STOCK_SIGNAL__" in seen:
            stock_signal_broken = True
            seen = [s for s in seen if s != "__NO_STOCK_SIGNAL__"]

        p["title"] = product_title(html)
        p["price"] = product_price(html)
        p["available"] = available
        p["all_sizes"] = seen

        hit = [s for s in TARGET_SIZES if s in available]
        if hit:
            p["hit"] = hit
            matches.append(p)

        flag = "  <<< MATCH" if hit else ""
        print(f"- {p['title'][:60]}")
        print(f"    all: {', '.join(seen) or '-'}")
        print(f"    in stock: {', '.join(available) or '-'}{flag}")

    if stock_signal_broken:
        print(
            "\n[warn] Every size looked available on at least one product. "
            "The out-of-stock CSS class has probably changed. "
            "Run: python bfl_watch.py --calibrate <product-url>  and update "
            "BFL_UNAVAILABLE_HINTS.",
            file=sys.stderr,
        )

    if args.debug:
        print("\n[debug] no notifications sent.")
        return 0

    # Only alert on things we have not already reported.
    state = load_state()
    fresh = []
    for p in matches:
        key = p["id"]
        already = set(state.get(key, {}).get("sizes", []))
        if args.force_notify or not set(p["hit"]).issubset(already):
            fresh.append(p)
        state[key] = {"sizes": sorted(set(p["hit"]) | already), "title": p["title"]}

    # Forget products that no longer match, so a restock re-alerts later.
    live_ids = {p["id"] for p in matches}
    for key in list(state):
        if key not in live_ids:
            del state[key]

    if not fresh:
        print("\nNo new matches.")
        save_state(state)
        return 0

    lines = [f"👕 {len(fresh)} Ralph Lauren item(s) available in "
             f"{'/'.join(TARGET_SIZES)}:", ""]
    for p in fresh:
        lines.append(f"• {p['title']}")
        lines.append(f"  {p['price']} — size {', '.join(p['hit'])}")
        lines.append(f"  {p['url']}")
        lines.append("")
    body = "\n".join(lines)

    print("\n" + body)

    sent_discord = notify_discord(body)
    sent_tg = notify_telegram(body)
    sent_mail = notify_email(
        f"Ralph Lauren in {'/'.join(TARGET_SIZES)} at Brands For Less", body
    )
    if not (sent_discord or sent_tg or sent_mail):
        print("[warn] no notifier configured - nothing was sent.", file=sys.stderr)

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
