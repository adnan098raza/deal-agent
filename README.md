# Deal Agent

A personal AI shopping deal agent for Germany. It runs by itself on your PC, with an optional GitHub Actions fallback for when the PC is off.

It collects deals from deal communities, price-comparison sites and shop sale pages, then for each deal:
- labels the seller's trustworthiness and warns about suspicious offers
- drops women's and kids' items in the men's-only categories
- checks whether a student discount stacks
- de-duplicates the same product across shops
- has an LLM (Groq or Gemini) score it

You get alerts and summaries through:
- **ntfy pushes with action buttons**: digests at 11:00, 14:30, 17:30 and 22:30, plus instant pushes only for great deals (9+), watchlist hits and deals ending soon
- an **end-of-day email** at 21:00
- an **Excel log** in this OneDrive folder, so it syncs to your phone
- a small **local dashboard**

```
 sources ──► robots.txt + rate limit ──► SQLite (%LOCALAPPDATA%\DealAgent\deals.db)
 (mydealz feeds + deal pages, idealo,         │
  eBay, MediaMarkt, Saturn, Flaconi)          ▼
                       enrich: price · discount · expiry · coupon · shipping · market price
                       annotate: trust label + warnings · men's-only filter · student discount · product key
                       score: LLM (relevance × quality) − "not interested" penalty
                                              │
     ┌──────────────┬──────────────┬──────────┴──────┬───────────────┬──────────────┐
  watchlist 1h   discovery 3h    digest 11:00     reminders 30m   recheck 12:00   eod 21:00
  instant hits   instant 9+ /    14:30 17:30      6 h before end, live/price/     HTML email
                 ending < 3 h    22:30: top 3 new snoozed resend  expired/stock   + deals.xlsx
     └──────────── ntfy push: [Open deal] [Snooze] [Not interested] ◄── feedback topic (5 min)
                                              │
                         data/deals.xlsx (Active / Expired / History) · dashboard :8765
```

## Quick start

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env          # add GROQ_API_KEY and NTFY_TOPIC (see below)

.venv\Scripts\python -m deal_agent test-notify               # push arrives on your phone?
.venv\Scripts\python -m deal_agent run discovery --dry-run   # what it would send (nothing is sent)
powershell -ExecutionPolicy Bypass -File .\setup_autostart.ps1  # run forever, from now on
```

After that, nothing needs to be run by hand. Open **http://127.0.0.1:8765** for the dashboard.

| Key | Where | Notes |
|---|---|---|
| Groq | https://console.groq.com/keys | Free tier. Default model `openai/gpt-oss-120b` (Groq retires models; list yours at `/openai/v1/models`) |
| Gemini | https://aistudio.google.com/apikey | Optional alternative (`LLM_PROVIDER=gemini`) |
| ntfy | ntfy app → subscribe to your `NTFY_TOPIC` | Anyone who knows the topic name can read it, so use a long random name |
| Gmail SMTP | Google Account → Security → App passwords | For the end-of-day email (`EMAIL_ENABLED=true`) |

Without an LLM key a heuristic scorer is used (category + brand + discount + community heat + trust).

## Sources

All sources are configured in `sources.yaml`. Each request is checked against the site's robots.txt and rate-limited to at least 2 s per host (longer if the site sets a `Crawl-delay`). A source that blocks automated access (HTTP 401/403/429, captcha or bot-protection pages) is **skipped and logged**, then backed off for 6 h, doubling on each repeat failure up to 48 h. You can see this under *Dashboard → Status* and in `python -m deal_agent sources`.

Status as checked on 2026-11-08 with the agent's own user agent:

| Source | Type | Status |
|---|---|---|
| mydealz.de: all, trending, and the electronics, clothing, fashion, shoes, sneaker, watches, jewelry, accessories, beauty, perfume and cosmetics groups | `mydealz` | ✅ RSS, plus the **deal page** for promising deals (40 per run): shop domain, coupon code, shipping, idealo comparison price, end date, expired flag |
| idealo.de deals | `html` | ✅ 60 tiles (price "ab …", discount vs. average price) |
| eBay.de deals | `html` | ✅ ~100 tiles (price, UVP, discount), labelled marketplace |
| MediaMarkt / Saturn offers | `mediamarkt` | ✅ embedded data: price, discount, EAN, marketplace seller and rating |
| Flaconi sale | `jsonld` | ✅ schema.org products (no original price, so the LLM judges value) |
| geizhals.de | `html` | ❌ HTTP 403, disabled |
| Douglas | `jsonld` | ❌ "Access Denied" bot protection, disabled |
| Amazon.de, Zalando, About You, Notino, Otto | | ❌ rendered by JavaScript or 404 for non-browser clients. Deals from these shops still come in through **mydealz**, with merchant and shop domain. |

mydealz's robots.txt disallows `/search` and the `/visit/` redirect links, so the agent never uses either. The deal page already gives the shop domain.

**Adding sources:**
- An RSS feed is a config entry (`type: rss` or `mydealz`).
- A server-rendered sale page uses `type: html` with CSS selectors, or `type: jsonld` if the page embeds schema.org products.
- Custom code is a `Source` subclass, referenced as `type: "my_module:MySource"`:

```python
from deal_agent.sources import Source
from deal_agent.models import Deal

class MySource(Source):
    def fetch(self) -> list[Deal]:
        data = self.get("https://api.example/deals").json()   # robots-checked, throttled, block-aware
        return [Deal(title=x["name"], link=x["url"], source=self.name, price=x["price"]) for x in data]
```

## Preferences (`preferences.yaml`)

Edit the file by hand, in the dashboard (*Preferences*), or with the CLI.

- **Categories:** electronics, clothing, shoes, jewelry_accessories (watches, chains, rings, bracelets, …) and cosmetics, each with German and English keywords. Keywords of 5+ letters also match inside compound words ("creme" matches "Feuchtigkeitscreme").
- **Men's only:** `gender.men_only_categories` lists clothing, shoes and jewelry_accessories. Women's and kids' items are filtered three ways:
  1. title keywords: Damen, Frauen, Women, Mädchen, Kinder, Kids, GS/PS sizing, Kleid, Bluse, …
  2. the source category
  3. the LLM's `audience` verdict

  Unisex items ("Damen & Herren") are kept. Cosmetics have no restriction.
- **Brands:** `budget` and `premium` lists. Both are welcome: budget brands must be genuinely cheap, premium brands need a real discount.
- **Student:** `student.is_student: true` plus the programs you can use (see below).
- **Thresholds:** `scoring.notify_threshold` (digest, 7), `notifications.instant_score` (9), `watchlist_threshold` (5).

```powershell
python -m deal_agent prefs show
python -m deal_agent prefs add-brand premium Garmin
python -m deal_agent prefs student on
python -m deal_agent prefs set notifications.digest_times "11:00, 14:30, 17:30, 22:30"
python -m deal_agent prefs set notifications.quiet_hours 23-7     # or "off"
```

## What an alert contains

```
Top deal: Nike Air Force 1 '07 Black/White – 47,89 €
47,89 €, was 95,95 € −50% · ⬇ Lowest in 90 days
Code SD26 · +5,00 € shipping · final ≈ 52,89 €
🎓 UNiDAYS ≈10% (no): only valid in the brand's own shop (nike.com)
🏪 Best Secret (bestsecret.com) · Major retailer
⏳ Time left: 1d 20h
Also at 2 other shop(s) from 54,99 €
Score 9.1/10 – Premium brand, very low price for Air Force 1
[Open deal]  [Snooze]  [Not interested]
```

### Seller trust

Each deal gets one of four labels, based on the shop domain (from the mydealz deal page or the shop URL), the merchant name and marketplace signals. The lists live in `deal_agent/data/retailers.yaml`.

| Label | Meaning |
|---|---|
| Official brand store | The shop is the brand itself (nike.com, store.sony.de, …) |
| Major retailer | Amazon, Otto, MediaMarkt, Zalando, Douglas, Best Secret, … (editable list) |
| Marketplace (3rd-party seller) | eBay, AliExpress, Temu, "Amazon Mp", MediaMarkt or Saturn marketplace sellers, … |
| Unknown shop | Everything else |

**Warnings** are heuristics, not verdicts. One is added when:
- an untrusted shop's price is far below the market price (the idealo price from mydealz, or the comparison price)
- any shop's price is less than 30% of the market price (possible price error or scam)
- an unknown or marketplace shop offers 70%+ off
- the seller rating is below 4.0
- the community voted the deal cold
- the shop is unknown, especially with no reviews or community feedback

Unknown shops get a link to the **Verbraucherzentrale Fakeshop-Finder**. It has no public API and its form is built for people, with anti-automation fields, so the agent links to it rather than automating it.

### Student discounts

UNiDAYS and Student Beans codes sit behind a login, and the education stores render with JavaScript, so live checking isn't possible. Instead, `deal_agent/data/student_programs.yaml` holds a curated, **editable** list: Apple Education, Samsung Education, Amazon Prime Student, UNiDAYS and Student Beans partner brands, and Lenovo Education. Each entry has a typical percentage, where it applies and whether it stacks.

Every alert says whether a program stacks:
- **yes / maybe:** shown with an estimated final price, including shipping
- **no:** for example "only valid in the brand's own shop", or "in-store deal"
- **unlikely:** for example the deal already uses code X, and usually only one code works per order

Percentages are marked "≈" because they're typical values, so check at checkout. Prime Student is shown as a shipping perk with no extra %.

### Buttons and feedback

- **Snooze** hides the deal for `snooze_hours` (24) and re-sends it once afterwards, if it's still live.
- **Not interested** removes the deal and lowers the score of **similar deals**:
  - same product: −4
  - similar title: −1.5 to −3
  - plus −0.5 per dismissal of the same brand and category, at most −2 extra

The buttons post to a private ntfy topic (`<NTFY_TOPIC>-fb`) that the agent checks every 5 minutes. They work from anywhere and don't need your PC to be reachable. The dashboard has the same actions.

## Notifications schedule

| What | When | Rule |
|---|---|---|
| Digest (ntfy) | **11:00, 14:30, 17:30, 22:30** (`digest_times`, Europe/Berlin) | Top 3 (`digest_size`) deals that became candidates (seen or scored) **since the previous digest**, at or above `notify_threshold`. The same product is collapsed to the cheapest trustworthy offer. Feeds are refreshed right before each digest. **Skipped if nothing new.** Sent as up to 3 pushes, because ntfy allows only 3 buttons per notification. If the PC starts within 90 min after a missed digest time, it catches up once. |
| Instant (ntfy) | between digests | Great deals only: score ≥ `instant_score` (9), **or** at or above threshold and ending within `instant_expiry_hours` (3) |
| Watchlist hit | hourly | Instant (queued for the next digest during quiet hours), after the LLM confirms it's really the product (and a men's or unisex version) |
| Reminder | every 30 min | Alerted deals ~6 h before they expire, and snoozed deals when the snooze ends |
| Quiet hours | 23:00–07:00 | No instant pushes. Instant-worthy deals and watchlist hits found at night are **queued and lead the next digest (11:00)**. They always go out, even beyond the top 3. Reminders and snoozed deals are held until 07:00. |
| End-of-day email | 21:00 | Every deal shown today, grouped by category: link, price, discount, shop, trust, student info, status, score and alert type. **deals.xlsx attached.** |

## Excel log (`data/deals.xlsx`)

The log is rewritten after every run. This folder is inside OneDrive, so it syncs to your phone (open it with the Excel or OneDrive app). Each sheet has filters and a frozen header row.

| Sheet | Rows | Columns |
|---|---|---|
| **Active** | Relevant deals (score ≥ `excel.min_score`, alerted, or a watchlist hit) that are live and seen or checked in the last 30 days | Title (link), brand, category, price, original price, discount %, shop, trust, student discount, final est., score, expiry, status, last checked, current price, lowest seen, warnings, link |
| **Expired** | Relevant deals that ended (re-check, stated expiry, or not seen for 30 days) | Same as Active |
| **History** | Every deal ever seen | First and last seen, first, lowest and current price, status, source, link |

If the file is **open in Excel**, Windows locks it. The agent then keeps the new version in `%LOCALAPPDATA%\DealAgent\deals.pending.xlsx` and retries every 10 minutes instead of crashing. The end-of-day email attaches the newest copy.

## Daily re-check and price history

Every day at 12:00 the agent revisits up to 150 Active and recent History deals and updates their status, current price and last-checked time:
- **mydealz links:** the deal page's expired flag and current price
- **shop links:** JSON-LD price and availability
- **404 / 410:** Expired

The possible statuses are Live, Price changed, Expired and Out of stock.

Every price observation goes into the price history, keyed by product: the EAN when known, otherwise the LLM's canonical product name, otherwise a cleaned title. Alerts show **"Lowest in 90 days"** only when there's enough history: at least 2 other observations spanning 3+ days. The dashboard's deal page shows the price chart and the other shops offering the same product.

## Dashboard

`python -m deal_agent start` also serves **http://127.0.0.1:8765** (or run `python -m deal_agent dashboard` on its own):
- **Deals:** search in plain words ("last 7 days matching sony", "today with nike", "letzte 2 wochen mit levi's"), filter by category, status and score, and snooze or dismiss
- **Deal page:** price history, the same product in other shops, trust details, student details
- **Watchlist:** add, pause and remove items
- **Preferences:** quick settings (thresholds, digest interval, quiet hours, student status and programs) plus the full YAML, validated before saving
- **Status:** jobs and their last runs, "run now", per-source health and blocks, Excel state

It only listens on localhost. Form posts are checked for same-origin requests. To open it from your phone on Wi-Fi, set `DASHBOARD_HOST=0.0.0.0` **and** `DASHBOARD_TOKEN=<secret>`, then open `http://<pc-ip>:8765/?token=<secret>` once.

## Fully automatic

### On your PC (`setup_autostart.ps1`)

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_autostart.ps1          # install + start now
powershell -ExecutionPolicy Bypass -File .\setup_autostart.ps1 -Remove  # uninstall
```

This registers the Task Scheduler task **DealAgent**:
- starts at log-on with `pythonw.exe` (no window)
- restarts on failure every minute, up to 999 times
- has a 15-minute watchdog trigger
- never runs twice: the task uses IgnoreNew, and the agent holds a lock file

Logs go to `%LOCALAPPDATA%\DealAgent\agent.log`.

### Cloud fallback (`.github/workflows/deal-agent.yml`, optional)

This runs `python -m deal_agent cron --skip-if-pc-online` hourly on GitHub Actions. Each run does whatever is due, using the same schedule as the PC.

1. Push the project to a **private** repository. `.env` and `data/` are git-ignored.
2. Add the repository secrets `GROQ_API_KEY` and `NTFY_TOPIC`, plus optional `NTFY_TOKEN` and the `SMTP_*`, `EMAIL_*` values.
3. Set `HEARTBEAT_ENABLED=true` in the PC's `.env`. The PC then posts an hourly heartbeat to `<topic>-hb`, and the cloud job skips while it's recent.
4. Commit `watchlist.yaml` when the watchlist changes. The agent rewrites it after every change, and the cloud runner syncs its database from it.

The cloud database is cached between runs with `actions/cache`, and `deals.xlsx` is uploaded as a 7-day artifact. Before sending, both sides read the last 12 h of the ntfy topic, so a deal pushed by one isn't pushed again by the other. GitHub may delay scheduled runs at busy times.

## CLI reference

```powershell
python -m deal_agent start [--no-dashboard]
python -m deal_agent run discovery|watchlist|digest|reminders|recheck|eod|feedback|excel|all [--dry-run]
python -m deal_agent cron [--skip-if-pc-online]
python -m deal_agent watch add "Air Force 1" --brand Nike --max-price 90 --category shoes
python -m deal_agent watch list | pause 2 | resume 2 | remove 2
python -m deal_agent prefs show | edit | set KEY VALUE | student on|off | add-brand TIER NAME | exclude WORD
python -m deal_agent deals --limit 30 -l
python -m deal_agent sources --check
python -m deal_agent test-notify [--email]
python -m deal_agent -v run all --dry-run        # debug logging
```

## Project layout

```
deal_agent/
  cli.py            CLI                      pipeline.py   jobs: collect → details → annotate → score → alert
  config.py         .env + preferences       scheduler.py  APScheduler + dashboard thread + single-instance lock
  db.py             SQLite + migrations      notifier.py   ntfy (buttons) + SMTP
  http.py           robots.txt, throttling   feedback.py   ntfy feedback topic, heartbeat, sent-sync, penalties
  parsing.py        prices/discount/expiry   report.py     end-of-day HTML email
  filters.py        men's-only audience      excel.py      deals.xlsx (lock-safe)
  trust.py          seller labels, warnings  recheck.py    daily status / price re-check
  students.py       student programs         watchfile.py  watchlist.yaml mirror (cloud sync)
  products.py       product keys, best offer scorer.py     Groq / Gemini prompts + heuristic fallback
  sources/          mydealz, rss, html, jsonld, mediamarkt, plugin registry
  dashboard/        FastAPI app + Jinja templates
  data/             retailers.yaml, student_programs.yaml (editable)
setup_autostart.ps1   .github/workflows/deal-agent.yml   preferences.yaml   sources.yaml   .env.example
tests/              offline tests (fake sources, LLM, ntfy, HTTP)
```

The database lives in `%LOCALAPPDATA%\DealAgent\deals.db`, deliberately **outside OneDrive**, because sync clients can corrupt SQLite files. A v1 database in `data/` is moved there automatically on first start.

## Tests

```powershell
.venv\Scripts\python -m pytest -q
```



# cmd to use 
.venv\Scripts\python -m deal_agent watch add "AirPods Pro" --max-price 180   # add a watchlist item
Stop-ScheduledTask DealAgent; Start-ScheduledTask DealAgent                  # restart after editing preferences.yaml times
.\setup_autostart.ps1 -Remove 