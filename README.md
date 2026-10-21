# Brands For Less size watcher

Checks a BFL search page once a day (whenever you desire) and messages you on Discord when a product
appears in your size. Runs on GitHub's free servers. Your computer stays off.

---

## Why there's a proxy in here

Brands For Less blocks requests coming from datacenter IP addresses, and GitHub
Actions runs on Microsoft Azure. A plain request gets 403 Forbidden no matter
what headers it sends. So the request is routed through Scrape.do, which
forwards it from a Saudi residential IP that the site accepts.

Scrape.do's free plan is 1,000 credits a month, no credit card. This script
uses about **30 a month**, because it reads every product's sizes straight off
the search results page in a single request. It only opens an individual
product page to double-check when something actually matches your size.

---

## Setup

### 1. Get a Scrape.do token
Sign up at https://dashboard.scrape.do/sign-up (free, no card). Copy the API
token from your dashboard.

### 2. Get a Discord webhook
In Discord: Server Settings > Integrations > Webhooks > New Webhook. Pick a
channel, then Copy Webhook URL. Webhooks can't post to DMs, so if you want this
private, make a server with only yourself in it.

### 3. Add both to GitHub
In your repo: Settings > Secrets and variables > Actions > New repository
secret. Add these two, exactly as named:

| Name | Value |
|---|---|
| `SCRAPEDO_TOKEN` | your Scrape.do API token |
| `DISCORD_WEBHOOK_URL` | your Discord webhook URL |

The workflow file refers to these by name. Never paste the values into
`check.yml` - anything in that file is stored in plain text.

### 4. Test it
Actions tab > BFL size watch > Run workflow > leave mode on **debug** > green
Run workflow button. Open the run, then the "Check stock" step.

You want to see:

```
Route  : scrape.do proxy (geo=sa)
Found  : 5 product(s)

- Polo Ralph Lauren Men Regular Fit Drawstring Jogger Pants, Grey
    in stock: XS
...
Scrape.do credits used this run: ~1
```

No M anywhere right now, so it should report no matches. Then run it again on
**test alert** to confirm Discord works.

After that it runs itself, daily at 03:00 UTC (06:00 Riyadh).

---

## Settings

Edit the `env:` block in `.github/workflows/check.yml`:

| Variable | Default | What it does |
|---|---|---|
| `BFL_SEARCH_URL` | men + ralph lauren | any BFL search or category URL |
| `BFL_TARGET_SIZES` | `M` | comma separated, e.g. `M,L` |
| `BFL_GEO` | `sa` | proxy country |
| `BFL_UNAVAILABLE_HINTS` | disable,sold,... | class names meaning sold out |

To watch something else, search on the site in your browser and paste the
address bar into `BFL_SEARCH_URL`.

Only the first page of results is checked. If the run warns that your search
has multiple pages, narrow it with filters until it's one page.

---

## How it decides

The search results page lists each product's in-stock sizes on the card. That
one page answers the whole question, so a normal day is a single request. When
a size matches, it opens that product's page to confirm before messaging you.

`state.json` remembers what you've already been told, so you get one message
per item rather than the same one every morning. If an item stops matching it's
dropped from state, so a later restock alerts again.

---

## Credits

Datacenter requests cost 1 credit, residential cost 10. The script tries
datacenter first and only escalates if blocked; failed attempts are free. Each
run prints what it used. Watch your balance at https://dashboard.scrape.do
for the first week.

At one run a day you should be at roughly 30-300 credits a month against a
1,000 free allowance. If you ever run short, change the `cron` line to run
every other day: `0 3 */2 * *`.

---

## When it breaks

**"no products parsed"** - BFL changed its layout. Run in debug and send the
output to Claude.

**Every product shows every size** - the sold-out marker changed. Run
`python bfl_watch.py --calibrate "<a product URL>"` and update
`BFL_UNAVAILABLE_HINTS`.

**"Scrape.do rejected the token"** - `SCRAPEDO_TOKEN` is wrong or missing.

**"out of credits"** - check the dashboard, or reduce the schedule.

**403 Forbidden** - the proxy isn't being used. Confirm the first output line
says `Route : scrape.do proxy`, not `direct`.

---

## Running it on your own PC instead

If you'd rather not use a proxy service, the same script works from home,
where the site isn't blocked. Leave `SCRAPEDO_TOKEN` unset and it goes direct.
The Windows version with Task Scheduler is in the other folder.
