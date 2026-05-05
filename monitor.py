#!/usr/bin/env python3
"""
Best Buy Pokemon TCG Stock Monitor

Polls Best Buy's Pokemon TCG search page on a fixed interval, detects when
a product flips from out-of-stock -> in-stock, and fires notifications via
Discord webhook and/or ntfy.sh push.

Usage:
    python monitor.py             # run the monitor loop
    python monitor.py --once      # poll one time and exit (good for cron)
    python monitor.py --dump      # save raw HTML + parsed data for debugging
    python monitor.py --test      # send test notifications to your configured channels

Config lives in config.json (copy from config.example.json).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import requests
# curl_cffi (optional) mimics a real Chrome TLS fingerprint. Best Buy doesn't
# fingerprint heavily, so plain requests works, but we use curl_cffi when it's
# installed for defense-in-depth in case anti-bot ever ramps up.
try:
    from curl_cffi import requests as cffi_requests  # type: ignore
    _HAS_CFFI = True
except ImportError:
    cffi_requests = None  # type: ignore
    _HAS_CFFI = False

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
STATE_FILE = SCRIPT_DIR / "stock_state.json"
LOG_FILE = SCRIPT_DIR / "monitor.log"
DEBUG_DUMP_FILE = SCRIPT_DIR / "debug.json"

CATEGORY_URL = "https://www.bestbuy.com/site/searchpage.jsp?st=pokemon+trading+cards&_dyncharset=UTF-8&id=pcat17071&type=page&sc=Global&cp=1&nrp=24&list=n&iht=y&keys=keys"
SITE_ROOT = "https://www.bestbuy.com"

# Realistic Chrome-on-Windows headers. Anti-bot systems primarily flag missing
# or python-default User-Agents, so this matters more than people realize.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("pc_stock_monitor")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_console = logging.StreamHandler(sys.stdout)
_console.setFormatter(_fmt)
logger.addHandler(_console)
_file = logging.FileHandler(LOG_FILE, encoding="utf-8")
_file.setFormatter(_fmt)
logger.addHandler(_file)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Product:
    sku: str
    name: str
    url: str
    price: Optional[str]
    in_stock: bool
    image: Optional[str] = None


# ---------------------------------------------------------------------------
# Config & state
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Load config. Order of precedence:

    1. Environment variables (used by GitHub Actions so secrets live in GitHub
       Secrets instead of a checked-in JSON file).
    2. config.json on disk (used for local runs).

    Env vars: DISCORD_WEBHOOK, NTFY_TOPIC, NTFY_SERVER, POLL_INTERVAL_SECONDS,
    POLL_JITTER_SECONDS. Any env var present overrides the file value.
    """
    cfg: dict = {}
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)

    if os.environ.get("DISCORD_WEBHOOK"):
        cfg["discord_webhook"] = os.environ["DISCORD_WEBHOOK"]
    if os.environ.get("NTFY_TOPIC") or os.environ.get("NTFY_SERVER"):
        ntfy = dict(cfg.get("ntfy") or {})
        if os.environ.get("NTFY_TOPIC"):
            ntfy["topic"] = os.environ["NTFY_TOPIC"]
        if os.environ.get("NTFY_SERVER"):
            ntfy["server"] = os.environ["NTFY_SERVER"]
        cfg["ntfy"] = ntfy
    if os.environ.get("POLL_INTERVAL_SECONDS"):
        cfg["poll_interval_seconds"] = int(os.environ["POLL_INTERVAL_SECONDS"])
    if os.environ.get("POLL_JITTER_SECONDS"):
        cfg["poll_jitter_seconds"] = int(os.environ["POLL_JITTER_SECONDS"])

    has_discord = bool((cfg.get("discord_webhook") or "").strip())
    has_ntfy = bool(((cfg.get("ntfy") or {}).get("topic") or "").strip())
    if not (has_discord or has_ntfy):
        logger.error(
            "No notification channels configured. Either copy config.example.json "
            "to config.json and fill it in, or set DISCORD_WEBHOOK / NTFY_TOPIC "
            "environment variables."
        )
        sys.exit(1)
    return cfg


def load_state() -> Dict[str, bool]:
    """Returns sku -> last_known_in_stock."""
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        logger.warning("State file corrupt, starting fresh.")
        return {}


def save_state(state: Dict[str, bool]) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    tmp.replace(STATE_FILE)


# ---------------------------------------------------------------------------
# Fetch + parse
# ---------------------------------------------------------------------------

def make_browser_session():
    """Return a session for hitting Best Buy. We try curl_cffi first (in case
    Best Buy ever ramps up TLS fingerprinting) and fall back to regular requests.
    """
    if _HAS_CFFI:
        try:
            return cffi_requests.Session(impersonate="chrome")
        except Exception:
            pass
    return requests.Session()


def fetch_page(session) -> Optional[str]:
    try:
        resp = session.get(CATEGORY_URL, headers=DEFAULT_HEADERS, timeout=20)
    except Exception as e:
        logger.warning("Fetch error: %s", e)
        return None
    if resp.status_code == 200:
        body = resp.text
        # Best Buy occasionally serves a "robot or human?" interstitial. Detect it.
        low = body[:4000].lower()
        if "are you a robot" in low or "captcha" in low or "blocked" in low:
            logger.warning("Got Best Buy bot-challenge interstitial.")
            return None
        return body
    if resp.status_code in (403, 429):
        logger.warning("Got HTTP %s — likely rate-limited.", resp.status_code)
    else:
        logger.warning("Got HTTP %s fetching category page", resp.status_code)
    return None


_LD_JSON_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_NEXT_DATA_RE = re.compile(
    r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
    re.DOTALL,
)


def extract_next_data(html: str) -> Optional[dict]:
    """Pull every JSON-bearing script block from the page and bundle into one
    dict the walker can iterate. Best Buy spreads product data across multiple
    <script type=\"application/ld+json\"> blocks (one per product), so we
    collect them all into a list under \"ld_blocks\" plus include the Next.js
    blob if present.
    """
    out: dict = {"ld_blocks": []}
    for raw in _LD_JSON_RE.findall(html):
        raw = raw.strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        out["ld_blocks"].append(parsed)
    nm = _NEXT_DATA_RE.search(html)
    if nm:
        try:
            out["next_data"] = json.loads(nm.group(1))
        except json.JSONDecodeError:
            pass
    if not out["ld_blocks"] and "next_data" not in out:
        return None
    return out


_NAME_KEYS = ("name", "displayName", "title", "productName")
_SKU_KEYS = ("skuId", "sku", "productId", "id")
_STOCK_KEYS = (
    "inStoreAvailability", "onlineAvailability",
    "inStock", "in_stock", "orderable", "available", "isAvailable",
)
_PRICE_KEYS = ("price", "regularPrice", "currentPrice", "salePrice", "listPrice")
# JSON-LD availability strings map to in-stock truthiness.
_LD_AVAIL_INSTOCK = (
    "instock", "in_stock",
    "https://schema.org/instock", "http://schema.org/instock",
    "https://schema.org/limitedavailability", "http://schema.org/limitedavailability",
    "https://schema.org/onlineonly", "http://schema.org/onlineonly",
    "https://schema.org/preorder", "http://schema.org/preorder",
)


def _looks_like_product(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    # JSON-LD Product schema
    if obj.get("@type") == "Product" and obj.get("name"):
        return True
    has_sku = any(k in obj for k in _SKU_KEYS)
    has_name = any(k in obj for k in _NAME_KEYS)
    has_stock_signal = (
        any(k in obj for k in _STOCK_KEYS)
        or "availability" in obj
        or "offers" in obj
        or "inventory" in obj
        or any(k in obj for k in _PRICE_KEYS)
    )
    return has_sku and has_name and has_stock_signal


def _first(obj: dict, keys) -> Any:
    for k in keys:
        if k in obj and obj[k] is not None:
            return obj[k]
    return None


def _extract_price(raw) -> Optional[str]:
    if raw is None:
        return None
    if isinstance(raw, str):
        s = raw.strip()
        # Format bare numeric strings as currency for cleaner alerts.
        try:
            return f"${float(s):.2f}"
        except (TypeError, ValueError):
            return s
    if isinstance(raw, (int, float)):
        return f"${raw:.2f}"
    if isinstance(raw, dict):
        if "formatted" in raw:
            return raw["formatted"]
        val = raw.get("value") or raw.get("amount") or raw.get("min")
        cur = raw.get("currency") or raw.get("currencyCode") or "USD"
        if val is not None:
            try:
                return f"${float(val):.2f}" if cur == "USD" else f"{val} {cur}"
            except (TypeError, ValueError):
                return str(val)
    return None


def _extract_stock(obj: dict) -> bool:
    # JSON-LD style: { "offers": { "availability": "https://schema.org/InStock" } }
    offers = obj.get("offers")
    if isinstance(offers, dict):
        av = offers.get("availability")
        if isinstance(av, str):
            return av.strip().lower().lstrip("/") in _LD_AVAIL_INSTOCK or av.lower().endswith("instock")
    if isinstance(offers, list):
        for o in offers:
            if isinstance(o, dict):
                av = o.get("availability")
                if isinstance(av, str) and (av.strip().lower().lstrip("/") in _LD_AVAIL_INSTOCK or av.lower().endswith("instock")):
                    return True

    # Best Buy / generic style flat fields
    for k in _STOCK_KEYS:
        if k in obj:
            v = obj[k]
            if isinstance(v, bool):
                return v
            if isinstance(v, str):
                return v.lower() in ("yes", "true", "instock", "in_stock", "available", "orderable")

    avail = obj.get("availability") or obj.get("inventory")
    if isinstance(avail, dict):
        for k in _STOCK_KEYS:
            if k in avail:
                v = avail[k]
                if isinstance(v, bool):
                    return v
                if isinstance(v, str):
                    return v.lower() in ("yes", "true", "instock", "in_stock", "available", "orderable")
        status = avail.get("status") or avail.get("availabilityStatus")
        if isinstance(status, str):
            return status.lower() in ("instock", "in_stock", "available", "orderable")
    if isinstance(avail, str):
        return avail.strip().lower().lstrip("/") in _LD_AVAIL_INSTOCK or avail.lower() in ("instock", "in_stock", "available", "orderable")
    return False


def _extract_url(obj: dict, sku: str) -> str:
    raw = obj.get("url") or obj.get("link") or obj.get("slug") or obj.get("href")
    if isinstance(raw, dict):
        raw = raw.get("href") or raw.get("url")
    if not raw:
        return f"{SITE_ROOT}/product/{sku}"
    if raw.startswith("http"):
        return raw
    return SITE_ROOT + (raw if raw.startswith("/") else "/" + raw)


def _extract_image(obj: dict) -> Optional[str]:
    img = obj.get("image") or obj.get("primaryImage") or obj.get("imageUrl")
    if isinstance(img, dict):
        return img.get("url") or img.get("src") or img.get("href")
    if isinstance(img, list) and img:
        first = img[0]
        if isinstance(first, dict):
            return first.get("url") or first.get("src")
        if isinstance(first, str):
            return first
    if isinstance(img, str):
        return img
    return None


def _build_product(obj: dict) -> Optional[Product]:
    sku_raw = _first(obj, _SKU_KEYS)
    name_raw = _first(obj, _NAME_KEYS)
    if not sku_raw or not name_raw:
        # JSON-LD products can have name without sku — synthesize from URL.
        if name_raw and obj.get("url"):
            sku_raw = obj["url"].rstrip("/").split("/")[-1].split(".")[0]
        else:
            return None
    sku = str(sku_raw)
    name = str(name_raw)
    # Price from flat fields OR from offers.price (JSON-LD)
    price = _extract_price(_first(obj, _PRICE_KEYS))
    if price is None:
        offers = obj.get("offers")
        if isinstance(offers, dict):
            price = _extract_price(offers.get("price") or offers.get("lowPrice"))
        elif isinstance(offers, list) and offers:
            for o in offers:
                if isinstance(o, dict):
                    price = _extract_price(o.get("price") or o.get("lowPrice"))
                    if price:
                        break
    return Product(
        sku=sku,
        name=name,
        url=_extract_url(obj, sku),
        price=price,
        in_stock=_extract_stock(obj),
        image=_extract_image(obj),
    )


def parse_products(next_data: dict) -> List[Product]:
    """Walk the entire Next.js data tree and yank anything that looks like a product.

    Best Buy embeds product data in JSON-LD blocks plus internal Next.js props.
    Rather than binding to a specific JSON path (which breaks on every redesign)
    we walk the tree and identify product-shaped objects by their fields. This
    survives most refactors.
    """
    products: List[Product] = []
    seen: Set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if _looks_like_product(node):
                p = _build_product(node)
                if p and p.sku not in seen:
                    seen.add(p.sku)
                    products.append(p)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(next_data)
    return products


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def send_discord(webhook: str, product: Product) -> bool:
    embed: Dict[str, Any] = {
        "title": f"In stock: {product.name}"[:256],
        "url": product.url,
        "color": 0xEE1515,
        "fields": [],
        "footer": {"text": "Best Buy TCG stock monitor"},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if product.price:
        embed["fields"].append({"name": "Price", "value": product.price, "inline": True})
    embed["fields"].append({"name": "SKU", "value": product.sku, "inline": True})
    if product.image:
        embed["thumbnail"] = {"url": product.image}

    payload = {"username": "PC Stock Bot", "embeds": [embed]}
    try:
        r = requests.post(webhook, json=payload, timeout=10)
    except requests.RequestException as e:
        logger.warning("Discord error: %s", e)
        return False
    if r.status_code >= 300:
        logger.warning("Discord webhook returned %s: %s", r.status_code, r.text[:200])
        return False
    return True


def send_ntfy(server: str, topic: str, product: Product) -> bool:
    url = f"{server.rstrip('/')}/{topic}"
    headers = {
        "Title": f"In stock: {product.name}"[:200],
        "Priority": "high",
        "Tags": "shopping_cart,pokemon",
        "Click": product.url,
    }
    body = f"{product.name}\nPrice: {product.price or 'unknown'}\n{product.url}"
    try:
        r = requests.post(url, data=body.encode("utf-8"), headers=headers, timeout=10)
    except requests.RequestException as e:
        logger.warning("ntfy error: %s", e)
        return False
    if r.status_code >= 300:
        logger.warning("ntfy returned %s: %s", r.status_code, r.text[:200])
        return False
    return True


def notify(config: dict, product: Product) -> None:
    discord = (config.get("discord_webhook") or "").strip()
    if discord:
        send_discord(discord, product)
    ntfy_cfg = config.get("ntfy") or {}
    topic = (ntfy_cfg.get("topic") or "").strip()
    if topic:
        server = ntfy_cfg.get("server") or "https://ntfy.sh"
        send_ntfy(server, topic, product)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

_stop = False


def _handle_signal(signum, frame):
    global _stop
    _stop = True
    logger.info("Stop signal received; shutting down after current poll.")


signal.signal(signal.SIGINT, _handle_signal)
try:
    signal.signal(signal.SIGTERM, _handle_signal)
except (AttributeError, ValueError):
    pass  # SIGTERM not available on Windows in some contexts


def _interruptible_sleep(seconds: float) -> None:
    end = time.time() + max(0.5, seconds)
    while time.time() < end and not _stop:
        time.sleep(min(0.5, end - time.time()))


def poll_once(session: requests.Session, state: Dict[str, bool], config: dict,
              alert_first_run: bool = False) -> Optional[List[Product]]:
    html = fetch_page(session)
    if not html:
        return None
    next_data = extract_next_data(html)
    if not next_data:
        logger.warning("Could not find __NEXT_DATA__ in page — site shape may have changed.")
        return None
    products = parse_products(next_data)
    if not products:
        logger.warning("Parsed 0 products. Run `python monitor.py --dump` to debug.")
        return []

    new_in_stock: List[Product] = []
    for p in products:
        previous = state.get(p.sku)
        was_oos = previous is False
        first_seen = previous is None
        if p.in_stock and (was_oos or (first_seen and alert_first_run)):
            new_in_stock.append(p)
        state[p.sku] = p.in_stock

    save_state(state)
    in_stock_count = sum(1 for p in products if p.in_stock)
    logger.info(
        "Polled %d products — %d in stock — %d new restock(s)",
        len(products), in_stock_count, len(new_in_stock),
    )
    for p in new_in_stock:
        logger.info("RESTOCK: %s (%s) — %s", p.name, p.sku, p.url)
        notify(config, p)
    return new_in_stock


def run_loop():
    config = load_config()
    state = load_state()

    interval = int(config.get("poll_interval_seconds", 30))
    jitter = int(config.get("poll_jitter_seconds", 5))

    session = make_browser_session()
    logger.info("Starting monitor — polling every %ds (+/-%ds jitter)", interval, jitter)
    logger.info("Tracking %d previously-seen products", len(state))

    consecutive_failures = 0
    while not _stop:
        result = poll_once(session, state, config)
        if result is None:
            consecutive_failures += 1
            backoff = min(600, interval * (2 ** min(consecutive_failures, 5)))
            logger.info("Backoff %ds after failure (#%d)", backoff, consecutive_failures)
            _interruptible_sleep(backoff)
            continue
        consecutive_failures = 0
        wait = max(5, interval + random.randint(-jitter, jitter))
        _interruptible_sleep(wait)

    logger.info("Monitor stopped cleanly.")


def cmd_dump():
    """Fetch the page once and write the parsed __NEXT_DATA__ to debug.json
    plus a list of detected products. Useful when parse_products() returns 0
    and we need to retune the field names."""
    session = make_browser_session()
    html = fetch_page(session)
    if not html:
        print("Fetch failed.")
        sys.exit(1)
    raw_html_file = SCRIPT_DIR / "debug.html"
    raw_html_file.write_text(html, encoding="utf-8")
    print(f"Wrote raw HTML to {raw_html_file}")

    next_data = extract_next_data(html)
    if not next_data:
        print("No __NEXT_DATA__ blob found. Inspect debug.html manually.")
        sys.exit(1)
    DEBUG_DUMP_FILE.write_text(json.dumps(next_data, indent=2), encoding="utf-8")
    print(f"Wrote __NEXT_DATA__ to {DEBUG_DUMP_FILE}")

    products = parse_products(next_data)
    print(f"Parsed {len(products)} product(s):")
    for p in products[:20]:
        print(f"  - [{('IN' if p.in_stock else 'OOS')}] {p.name} ({p.sku}) {p.price or ''}")
    if len(products) > 20:
        print(f"  ... and {len(products) - 20} more")


def cmd_test():
    """Send test notifications using your configured channels."""
    config = load_config()
    fake = Product(
        sku="TEST-0001",
        name="[TEST] Surging Sparks Booster Box",
        url="https://www.bestbuy.com/site/searchpage.jsp?st=pokemon+trading+cards",
        price="$161.64",
        in_stock=True,
        image=None,
    )
    print("Sending test notifications...")
    notify(config, fake)
    print("Done. Check Discord/your phone.")


def main():
    parser = argparse.ArgumentParser(description="Best Buy Pokemon TCG stock monitor")
    parser.add_argument("--once", action="store_true", help="Poll once and exit")
    parser.add_argument("--dump", action="store_true",
                        help="Fetch and dump __NEXT_DATA__ for debugging, then exit")
    parser.add_argument("--test", action="store_true",
                        help="Send a test notification to all configured channels")
    args = parser.parse_args()

    if args.dump:
        cmd_dump()
        return
    if args.test:
        cmd_test()
        return
    if args.once:
        config = load_config()
        state = load_state()
        session = make_browser_session()
        poll_once(session, state, config)
        return

    try:
        run_loop()
    except Exception:
        logger.exception("Fatal error")
        sys.exit(1)


if __name__ == "__main__":
    main()
