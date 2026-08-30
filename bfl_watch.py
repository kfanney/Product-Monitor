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
from urllib.parse import urlencode

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

# ---- Scrape.do proxy (lets this run on a server instead of your PC) ----
# Sign up free at https://dashboard.scrape.do/sign-up and put the token in
# the SCRAPEDO_TOKEN environment variable. Without it, requests go direct.
SCRAPEDO_TOKEN = os.environ.get("SCRAPEDO_TOKEN", "").strip()
GEO_CODE = os.environ.get("BFL_GEO", "sa").strip().lower()
PROXY_TIMEOUT = int(os.environ.get("BFL_PROXY_TIMEOUT", "90"))
PROXY_API = "https://api.scrape.do/"

# Tier 0 = datacenter IP (1 credit). Tier 1 = residential/mobile (costs more).
# Failed requests are not charged, so trying the cheap one first is free.
_TIER = {"level": 0, "credits": 0}
_TIER_COST = {0: 1, 1: 10}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", '
                 '"Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
}

# TLS fingerprint to imitate. Only used when curl_cffi is installed.
IMPERSONATE = os.environ.get("BFL_IMPERSONATE", "chrome124")

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

class BlockedError(RuntimeError):
    """Raised when the site refuses us outright."""


def make_session():
    """Prefer curl_cffi (mimics a real Chrome TLS handshake). Fall back to
    plain requests if it isn't installed."""
    try:
        from curl_cffi import requests as cffi  # type: ignore
        s = cffi.Session(impersonate=IMPERSONATE)
        s.headers.update(HEADERS)
        return s, "curl_cffi"
    except Exception:  # noqa: BLE001
        s = requests.Session()
        s.headers.update(HEADERS)
        return s, "requests"


def warm_up(session) -> None:
    """Visit the homepage first so we pick up cookies, like a browser would."""
    if SCRAPEDO_TOKEN:
        return  # proxy rotates IPs per request; a warm-up would just cost credits
    try:
        session.get(f"{BASE}/en-sa/", timeout=TIMEOUT)
        time.sleep(1.0)
    except Exception as exc:  # noqa: BLE001
        print(f"[note] warm-up request failed: {exc}", file=sys.stderr)


def _scrapedo_url(target: str, level: int) -> str:
    params = {"token": SCRAPEDO_TOKEN, "url": target}
    if GEO_CODE:
        params["geoCode"] = GEO_CODE
    if level >= 1:
        params["super"] = "true"
    return PROXY_API + "?" + urlencode(params)


def _fetch_via_proxy(url: str, session, tries: int = 2) -> str:
    """Fetch through scrape.do, escalating from datacenter to residential."""
    last = None
    for level in (_TIER["level"], 1):
        for attempt in range(tries):
            try:
                r = session.get(_scrapedo_url(url, level), timeout=PROXY_TIMEOUT)
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(2 * (attempt + 1))
                continue

            if r.status_code == 200:
                if level != _TIER["level"]:
                    print(f"  (switched to {'residential' if level else 'datacenter'} IPs)")
                    _TIER["level"] = level
                _TIER["credits"] += _TIER_COST[level]
                return r.text

            body = (r.text or "")[:200].replace("\n", " ")
            if r.status_code in (401, 403) and "token" in body.lower():
                raise BlockedError(
                    "Scrape.do rejected the token. Check SCRAPEDO_TOKEN is "
                    f"correct. Response: {body}"
                )
            if r.status_code == 429:
                raise BlockedError(
                    "Scrape.do says you are out of credits or too many "
                    f"concurrent requests. Response: {body}"
                )
            last = f"HTTP {r.status_code}: {body}"
            time.sleep(2 * (attempt + 1))

        if level == 1:
            break

    raise BlockedError(f"Failed to fetch {url} via scrape.do: {last}")


def fetch(url: str, session, tries: int = 3, referer: str | None = None) -> str:
    if SCRAPEDO_TOKEN:
        return _fetch_via_proxy(url, session)

    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
        headers["Sec-Fetch-Site"] = "same-origin"

    last = None
    for attempt in range(tries):
        try:
            r = session.get(url, headers=headers, timeout=TIMEOUT)
            if r.status_code == 403:
                last = f"403 Forbidden (blocked, attempt {attempt + 1})"
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code == 429:
                last = "429 rate limited"
                time.sleep(10 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.text
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2 * (attempt + 1))

    raise BlockedError(f"Failed to fetch {url}: {last}")





# --------------------------------------------------------------------------
# Listing page -> product URLs
# --------------------------------------------------------------------------

def _pretty(slug: str) -> str:
    return slug.replace("-", " ").title()


def _card_sizes(node) -> list[str]:
    """Letter sizes (and 32 x 30 style) inside one product card.

    Bare numbers are deliberately ignored here: a card also contains the price,
    and 'SAR 45' would otherwise be read as size 45.
    """
    out = []
    for _tag, token, _text in _size_candidates(node):
        if re.fullmatch(r"\d{2}(\.5)?", token):
            continue  # bare number - almost certainly a price, not a size
        if token not in out:
            out.append(token)
    return out


def parse_listing(html: str) -> list[dict]:
    """Every product on the search page, with the sizes shown as in stock."""
    soup = BeautifulSoup(html, "html.parser")
    cards: dict[str, dict] = {}

    for a in soup.find_all("a", href=True):
        m = PRODUCT_URL_RE.search(a["href"])
        if not m:
            continue
        slug, pid = m.group(1), m.group(2)

        sizes = _card_sizes(a)
        if not sizes and a.parent is not None:
            sizes = _card_sizes(a.parent)

        img = a.find("img", alt=True)
        title = (img["alt"].strip() if img and img["alt"].strip()
                 else _pretty(slug))

        price = ""
        pm = re.search(r"SAR\s*[\d,]+", a.get_text(" ", strip=True))
        if pm:
            price = pm.group(0)

        if pid in cards:
            for s in sizes:
                if s not in cards[pid]["sizes"]:
                    cards[pid]["sizes"].append(s)
            continue

        cards[pid] = {
            "id": pid,
            "slug": slug,
            "url": f"{BASE}/en-sa/{slug}/{pid}/p/",
            "title": title,
            "price": price,
            "sizes": sizes,
        }

    return list(cards.values())


def more_pages(html: str) -> str | None:
    """Return e.g. '1/36' if the listing is paginated beyond page one."""
    m = re.search(r"Page\s*(\d+)\s*/\s*(\d+)", html)
    if m and m.group(2) != "1":
        return f"{m.group(1)}/{m.group(2)}"
    return None


def find_products(html: str) -> list[dict]:
    """Kept for the --calibrate path."""
    return parse_listing(html)


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
    ap.add_argument("--test-webhook", action="store_true",
                    help="send a message even when nothing matches, to prove "
                         "the notifier works")
    args = ap.parse_args()

    session, kind = make_session()

    if args.calibrate:
        warm_up(session)
        calibrate(args.calibrate, session)
        return 0

    route = f"scrape.do proxy (geo={GEO_CODE or 'auto'})" if SCRAPEDO_TOKEN \
        else f"direct - {kind} (impersonate={IMPERSONATE})"
    print(f"Route  : {route}")
    if not SCRAPEDO_TOKEN:
        print("[warn] SCRAPEDO_TOKEN is not set. Going direct, which Brands "
              "For Less blocks from servers. Expect a 403.")
    print(f"Search : {SEARCH_URL}")
    print(f"Sizes  : {', '.join(TARGET_SIZES)}")

    warm_up(session)
    try:
        listing = fetch(SEARCH_URL, session, referer=f"{BASE}/en-sa/")
    except BlockedError as exc:
        print(f"\n[BLOCKED] {exc}", file=sys.stderr)
        if SCRAPEDO_TOKEN:
            print(
                "\nEven the proxy could not fetch the page. Check your credit\n"
                "balance at https://dashboard.scrape.do and that the token is\n"
                "still valid.\n",
                file=sys.stderr,
            )
        else:
            print(
                "\n>>> SCRAPEDO_TOKEN is NOT SET, so the request went direct <<<\n"
                "\nThat is why it failed. Brands For Less blocks datacenter IPs\n"
                "and GitHub Actions runs on Microsoft Azure. No amount of header\n"
                "or TLS faking gets around it.\n\n"
                "Fix:\n"
                "  1. Sign up free at https://dashboard.scrape.do/sign-up\n"
                "  2. Copy your API token from the dashboard\n"
                "  3. In this repo: Settings > Secrets and variables > Actions\n"
                "     > New repository secret\n"
                "       Name:   SCRAPEDO_TOKEN\n"
                "       Secret: <paste the token>\n"
                "  4. Run this workflow again. The first line should say\n"
                "     'Route  : scrape.do proxy', not 'direct'.\n",
                file=sys.stderr,
            )
        return 2
    products = parse_listing(listing)
    print(f"Found  : {len(products)} product(s)")

    pages = more_pages(listing)
    if pages:
        print(f"[note] this search has multiple pages ({pages}); only page 1 "
              f"is checked. Narrow the search URL if that matters.")

    if not products:
        print("\n[warn] no products parsed - the listing markup may have "
              "changed. Run with --debug and send the output to Claude.",
              file=sys.stderr)
        return 1

    print()
    matches = []
    for p in products:
        hit = [s for s in TARGET_SIZES if s in p["sizes"]]
        flag = "  <<< MATCH" if hit else ""
        print(f"- {p['title'][:58]}")
        print(f"    in stock: {', '.join(p['sizes']) or '(none shown)'}{flag}")
        if hit:
            p["hit"] = hit
            matches.append(p)

    # Confirm each match on its own product page. Only runs when something
    # actually matched, so a normal day costs a single request.
    confirmed = []
    for p in matches:
        time.sleep(REQUEST_DELAY)
        try:
            html = fetch(p["url"], session, referer=SEARCH_URL)
        except BlockedError as exc:
            print(f"[note] could not double-check {p['id']}: {exc}",
                  file=sys.stderr)
            confirmed.append(p)      # trust the listing rather than stay silent
            continue

        available, seen = parse_sizes(html)
        if "__NO_STOCK_SIGNAL__" in seen:
            confirmed.append(p)
            continue

        still = [s for s in p["hit"] if s in available]
        if still:
            p["hit"] = still
            p["price"] = product_price(html) or p["price"]
            confirmed.append(p)
        else:
            print(f"[note] {p['title'][:40]} showed "
                  f"{'/'.join(p['hit'])} on the listing but not on the "
                  f"product page - skipping.")

    matches = confirmed
    stock_signal_broken = False

    if SCRAPEDO_TOKEN:
        print(f"\nScrape.do credits used this run: ~{_TIER['credits']}")

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
        if args.test_webhook:
            lines = [
                "✅ Test message — your Brands For Less watcher is working.",
                "",
                f"Watching for size {'/'.join(TARGET_SIZES)}. "
                f"Nothing matches right now. Currently in stock:",
                "",
            ]
            for p in products:
                lines.append(f"• {p['title']} — {', '.join(p['sizes']) or 'none'}")
            lines += ["", "You'll get a message here when something appears in "
                          f"{'/'.join(TARGET_SIZES)}."]
            body = "\n".join(lines)
            print("\n" + body)

            sent = any([
                notify_discord(body),
                notify_telegram(body),
                notify_email("Brands For Less watcher test", body),
            ])
            if not sent:
                print(
                    "\n[FAIL] Nothing was sent. No notifier is configured, or "
                    "the webhook was rejected.\n"
                    "  - Is there a secret named exactly DISCORD_WEBHOOK_URL?\n"
                    "  - Does check.yml have the line\n"
                    "      DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}\n"
                    "  - Is the webhook still alive in Discord?",
                    file=sys.stderr,
                )
                save_state(state)
                return 3
            print("\n[OK] Test message sent. Check your Discord channel.")
            save_state(state)
            return 0

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
