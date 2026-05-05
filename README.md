# Pokemon Center TCG Stock Monitor

A small Python program that polls `pokemoncenter.com/category/tcg-cards` every ~30 seconds and pushes a notification to Discord and your phone (via ntfy.sh) the moment a product flips from out-of-stock to in-stock.

## What it does, in plain terms

1. Every 30 seconds (with a little random jitter), it fetches the TCG cards category page.
2. It parses the embedded Next.js data blob (`__NEXT_DATA__`) to extract every product's name, price, URL, and stock status.
3. It compares against `stock_state.json` from the previous poll.
4. For any product that just transitioned `False -> True` on stock, it fires a Discord webhook + an ntfy push.
5. It does **not** alert on the first poll (so you don't get blasted with 100 notifications when you start it for the first time). It learns the current state, then alerts on changes from there on.

---

## Setup (one-time, ~5 minutes)

### 1. Install Python and the dependency

You probably already have Python 3.9+ on your machine. To check, open PowerShell and run:

```
python --version
```

If that prints something like `Python 3.11.x`, you're set. If not, install it from https://www.python.org/downloads/ and check the "Add Python to PATH" box during install.

Then install the one library this needs:

```
cd path\to\pokemon_stock_monitor
pip install -r requirements.txt
```

### 2. Create your Discord webhook

1. In Discord, create a new server (or pick an existing one — a private personal server works great).
2. Right-click any text channel -> **Edit Channel** -> **Integrations** -> **Webhooks** -> **New Webhook**.
3. Name it whatever you want, then click **Copy Webhook URL**.
4. Make sure Discord push notifications are enabled on your phone for that channel (Discord -> Channel -> ... menu -> Notification Settings -> All Messages).

### 3. Pick an ntfy.sh topic

ntfy is a free push-notification service that doesn't need an account.

1. Install the **ntfy** app on your phone (iOS App Store / Google Play).
2. In the app, tap **+** and subscribe to a topic. Use something **unguessable** — anyone who knows the topic name can send you notifications. Example: `jonathan-pc-tcg-x9q2v8`.
3. That same string goes into the config below.

(You can skip ntfy and use only Discord, or vice versa. Just leave the unused field blank.)

### 4. Fill in `config.json`

```
copy config.example.json config.json
```

Then open `config.json` in any text editor and replace the placeholder webhook URL and ntfy topic with your real values. The defaults for `poll_interval_seconds` (30) and `poll_jitter_seconds` (5) are sensible — leave them unless you have a reason.

### 5. Test your notifications before going live

```
python monitor.py --test
```

You should get a Discord message and a phone push within a couple of seconds, both saying "[TEST] Surging Sparks Booster Box". If either is missing, fix that channel's config before continuing.

### 6. Run it

```
python monitor.py
```

You'll see logs like:

```
2026-05-04 09:12:31 [INFO] Starting monitor — polling every 30s (+/-5s jitter)
2026-05-04 09:12:33 [INFO] Polled 48 products — 12 in stock — 0 new restock(s)
2026-05-04 09:13:05 [INFO] Polled 48 products — 12 in stock — 0 new restock(s)
2026-05-04 09:25:41 [INFO] Polled 48 products — 13 in stock — 1 new restock(s)
2026-05-04 09:25:41 [INFO] RESTOCK: Scarlet & Violet—Prismatic Evolutions Elite Trainer Box (701-91725) — https://www.pokemoncenter.com/product/701-91725/...
```

Press `Ctrl+C` to stop cleanly. The state file persists, so when you restart it picks up where it left off.

For one-click launching on Windows, double-click `run.bat`.

---

## Other useful commands

```
python monitor.py --once   # Poll one time and exit (good for Task Scheduler / cron)
python monitor.py --dump   # Save the raw page + parsed JSON for debugging
python monitor.py --test   # Send a fake notification to all configured channels
```

---

## Troubleshooting

**"Parsed 0 products"**
Pokemon Center changed their data shape. Run `python monitor.py --dump`, open `debug.json`, and look for the product list. Tell me what fields you see (e.g., `quantity_available` instead of `inStock`) and I'll update the field-name lists in `monitor.py` (`_STOCK_KEYS`, `_NAME_KEYS`, etc.).

**"Got HTTP 403" or "Got HTTP 429"**
You're being rate-limited or bot-flagged. The script auto-backs-off exponentially. If it persists:
- Increase `poll_interval_seconds` to 60 or 90.
- Make sure you're on a residential connection, not a VPN/datacenter IP.
- Take a break for an hour and try again.

**Notifications work in `--test` but never fire in real runs**
Most likely everything is currently in stock that's going to be in stock — the script only alerts on **transitions** from OOS -> in stock, not on items that were already in stock when you started monitoring. Wait for an actual restock event.

**My laptop sleeps and the script stops**
Either keep your laptop awake (Settings -> System -> Power & battery -> Screen and sleep -> Never), or upgrade to free 24/7 cloud hosting via GitHub Actions — see `DEPLOY_GITHUB.md` in this folder for the full guide.

**I want it to also auto-buy / add-to-cart**
That's a different (and much more legally fraught) class of tool, and I won't help build that part. Stock alerts so you can buy manually = fine. Auto-purchase bots = scalper territory.

---

## Files in this folder

| File | Purpose |
|---|---|
| `monitor.py` | The main program. |
| `config.example.json` | Template — copy to `config.json` and fill in. |
| `config.json` | **Your secrets.** Not committed to git. |
| `requirements.txt` | Python deps (just `requests`). |
| `stock_state.json` | Auto-created. Remembers each product's last known stock state across runs. |
| `monitor.log` | Auto-created. Rolling log of every poll. |
| `debug.json` / `debug.html` | Auto-created by `--dump`. Inspect when parsing breaks. |
| `run.bat` | Windows one-click launcher. |

---

## Caveats and ethics

- Pokemon Center's Terms of Service technically prohibit automated access. Personal stock alerts at a polite cadence (30+ seconds, single user) are very unlikely to be enforced — but you're using this at your own risk.
- For genuinely hyped drops where Pokemon Center uses Queue-It, items can sell out in <10 seconds. A 30-second poll won't always catch those. This tool is best for slow restocks (booster boxes, ETBs, accessories) rather than chase items.
- Do not run multiple copies in parallel. Do not lower the interval below 30s. That's how this stops being "stock alerts" and starts being "DDoS."
