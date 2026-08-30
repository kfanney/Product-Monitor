# Brands For Less — size watcher

Checks the BFL men's Ralph Lauren search page once a day, opens every product,
and messages you when any of them has size **M** in stock.

Runs free on GitHub Actions. Nothing to leave switched on.

---

## 1. Local test first (5 minutes)

```bash
pip install -r requirements.txt
python bfl_watch.py --debug
```

`--debug` prints what it found and sends nothing. Expected output today, since
none of the 5 items has M:

```
- Polo Ralph Lauren Men Crew Neck Cable Knitted Sweaters, Red
    all: S, M, L, XL, 2XL
    in stock: 2XL
```

**This is the important check.** If `in stock` lists *every* size for a product,
the script isn't seeing the out-of-stock markers, and you'd get false alarms.
The script warns you when it detects this. Fix it with:

```bash
python bfl_watch.py --calibrate "https://www.brandsforless.com/en-sa/polo-ralph-lauren-men-crew-neck-cable-knitted-sweaters-red/1384415/p/"
```

That prints each size chip with its CSS classes. This product is known to have
only 2XL in stock, so it's a good reference. Whatever class marks S/M/L/XL,
add it to `BFL_UNAVAILABLE_HINTS` (comma separated, matched as substrings).

## 2. Discord alerts (easiest)

1. In Discord: **Server Settings → Integrations → Webhooks → New Webhook**.
   Pick the channel you want the alerts in.
   (For a DM-style ping, make a private server with just you — webhooks can't
   post to DMs.)
2. Click **Copy Webhook URL**.

```bash
export DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/...'
python bfl_watch.py --force-notify   # should post in the channel
```

That's it — no bot, no OAuth. Treat the URL like a password: anyone holding it
can post to that channel, which is why it goes in GitHub Secrets below and not
in the workflow file.

To get a phone notification, right-click the channel → **Notification
Settings → All Messages**.

### Telegram instead

1. Message **@BotFather**, send `/newbot`, copy the token.
2. Send your new bot any message.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id`.

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

### Email instead
Set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `ALERT_EMAIL_TO`.
For Gmail use an App Password, not your normal password.

Any combination works — set all three and it sends to all three.

## 3. Put it on GitHub

1. Create a **private** repo, push these files.
2. Settings → Secrets and variables → Actions → add `DISCORD_WEBHOOK_URL`
   (and/or the Telegram / SMTP ones).
3. Actions tab → *BFL size watch* → **Run workflow** to test it now.

It then runs daily at 03:00 UTC (06:00 Riyadh). Change the `cron:` line in
`.github/workflows/check.yml` to move it.

## Settings

| Variable | Default | Notes |
|---|---|---|
| `BFL_SEARCH_URL` | men + ralph lauren search | any BFL search/category URL works |
| `BFL_TARGET_SIZES` | `M` | comma separated, e.g. `M,L` or `32 x 30` |
| `BFL_UNAVAILABLE_HINTS` | `disable,sold,out-of-stock,...` | class fragments meaning sold out |
| `BFL_REQUEST_DELAY` | `1.5` | seconds between product pages |

## How it decides

The search listing shows only a *last-piece* size on each card, so it isn't a
reliable list of what's available. The script instead opens each product page,
reads the size selector, and treats a size as available unless its chip is
marked disabled. `state.json` remembers what it already told you, so you get one
alert per item, not one every morning. If an item stops matching, it's dropped
from state, so a later restock alerts again.

## When it breaks

BFL will redesign eventually. Symptoms and fixes:

- **"no products parsed"** — the product URL format changed. Check `PRODUCT_URL_RE`.
- **"Every size looked available"** — run `--calibrate` and update the hints.
- **Silent, but you know M exists** — run `--debug` and compare against the site.

Keep the delay at 1.5s or higher and don't lower the schedule below daily;
this is a handful of polite requests, and there's no reason to make it more.
