# EP2026 Ticket Watcher

Watches [Electric Picnic 2026 — Weekend Camping](https://www.ticketmaster.ie/electric-picnic-2026-weekend-camping-co-laois-28-08-2026/event/18006314BD813D3E)
and emails `davidcoyne73@gmail.com` when a ticket is actually buyable.

It works by doing what you do by hand: setting the quantity, pressing
**Find Tickets**, and reading the answer — including the **Verified Resale**
panel that the search reveals.

---

## Why the old one never worked

The previous `ticket_checker.py` fetched the page with `cloudscraper` and
parsed `__NEXT_DATA__`. It logged **657 consecutive failures over 44 days** and
never once alerted. Three separate reasons, each fatal on its own:

1. **`cloudscraper` cannot get in.** ticketmaster.ie answers plain HTTP clients
   with `401` and an identity-verification page. `cloudscraper` solves legacy
   Cloudflare challenges; this is a different, behavioural system.
2. **The signal it watched is not the answer.** `hasEnabledTicketTypes` and the
   rendered tier list describe a *menu*, not *stock*. The page will happily show
   you "General Admission Tier 2 — €310.50" and then refuse to sell you one.
3. **Nobody was told.** The watchdog latched a single "already alerted" flag, so
   one email went out on 1 July and then silence for 44 days — while the Actions
   runs stayed green, because a failed fetch exited 0.

---

## What actually works, and how it was established

Every claim here was measured against the live page on 2026-08-13.

| Thing tried | Result |
| --- | --- |
| `cloudscraper` / plain HTTP | `401` identity wall, every time |
| Playwright **headless** Chrome | `403`, every time |
| Playwright **headed** Chrome | `401` on first load → **reload → `200`, real page** |

So the working recipe is a **real, visible Chrome, from a home IP**:

1. Load the page — expect the first response to fail. Its content lives inside
   `<noscript>`, so a walled page looks *blank* rather than obviously blocked.
2. Accept the cookie dialog. While it is up it blocks the bot check from
   completing, and the page stays empty.
3. Reload. Now you get the real page.
4. Set the quantity, press **Find Tickets**, read the result.

Two findings that invert the obvious implementation:

- **The resale panel does not exist until you search.** A fresh page load ends
  at the Find Tickets button. "Other Options → Verified Resale Tickets" is
  rendered by the *search response*. There is no passive way to watch resale —
  which is why the search is the whole design, not an optional extra.
- **"Resale Tickets will appear below when they are available." is a static
  caption.** It sits there permanently, including directly above a real
  listing. Reading it as an empty-state marker inverts your result.

To answer your question directly: **yes, it is possible, and pressing the
button is not just possible but required.** There is no read-only version of
this that works.

---

## Setup

```bash
cd ~/SideProjects/EPTicketRefresher

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
chmod +x run_watcher.sh          # blocked from doing this for you

# Credentials (chmod 600 — it holds your Gmail app password)
mkdir -p ~/.ep2026-watcher
cat > ~/.ep2026-watcher/env <<'EOF'
GMAIL_ADDRESS=davidcoyne73@gmail.com
GMAIL_APP_PASSWORD=your_16_char_app_password
NTFY_TOPIC=some-hard-to-guess-topic      # optional phone push
EOF
chmod 600 ~/.ep2026-watcher/env
```

Then confirm it works:

```bash
.venv/bin/python -m ep_watcher check    # read once, print, send nothing
.venv/bin/python -m ep_watcher test     # send a test email + push
```

`check` opens a Chrome window briefly. That is expected and load-bearing.

### Run it continuously

```bash
cp launchd/com.davidcoyne.ep2026watcher.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.davidcoyne.ep2026watcher.plist
tail -f ~/.ep2026-watcher/logs/watcher.log
```

**Your Mac must be awake and logged in.** A sleeping Mac is a stopped watcher.
For the fortnight before the festival:

```bash
sudo pmset -a sleep 0 disablesleep 1     # undo with disablesleep 0
```

---

## Commands

| Command | What it does |
| --- | --- |
| `check` | One search, print the result, notify nobody |
| `run` | One search **with** alerts — for a scheduler |
| `watch` | Long-running loop, one warm browser held open |
| `test` | Send one real example of **every** email to your inbox |
| `selftest` | Offline checks — no network, no credentials, nothing sent |
| `login` | Open Chrome to sign in by hand (only needed for *buying*) |
| `calibrate` | Dump screenshot + text + HTML after a search |
| `status` | Print config and health |
| `resolve-id` | Look up the Discovery event id for the API source |

---

## Configuration

Environment variables, all optional:

| Variable | Default | Notes |
| --- | --- | --- |
| `WANTED_QUANTITIES` | `1` | Quantities to search per poll |
| `EP_POLL_SECONDS` | `180` | Seconds between polls, jittered ±25% |
| `EP_HEARTBEAT_HOURS` | `1` | How often to send the "still nothing" report |
| `EP_OFFSCREEN` | `1` | Park the Chrome window off-desktop |
| `EP_HEADLESS` | `0` | **Leave this alone.** Headless is always blocked |
| `PRESS_THE_BUTTON` | `1` | Set `0` and it can no longer answer the question |
| `EP_USE_BROWSER` | `1` | Set `0` for API-only mode (no Chrome needed) |
| `TM_DISCOVERY_KEY` | — | Free Discovery API key — enables the browser-free source |
| `TM_API_KEY` | — | Inventory Status API key (needs an access grant) |

### Why quantity matters

"There aren't enough tickets" is an answer about *the number you asked for*,
and the page defaults to **2**. Asking for 2 when one would do manufactures its
own refusal, so the watcher explicitly drives the stepper down to 1 first.

Searching for 1 is also the most sensitive probe available: if anything exists
at all, it shows up here. It matches what resale actually produces, too — the
listing seen during testing was a single ticket at €366.39.

Set `WANTED_QUANTITIES=1,2,3` to sweep several per poll instead.

---

## The emails

Four kinds, all to `davidcoyne73@gmail.com`, all carrying a link to the event
page:

| Email | When |
| --- | --- |
| **Tickets available** | A listing appears on the box office or verified resale |
| **In the basket** | A reserve actually succeeded — there is a live hold, with a countdown |
| **No luck yet** | Hourly while nothing has turned up |
| **Watcher is broken** | 4 consecutive failed checks, then every 6h until fixed |

The hourly "no luck yet" email is a liveness proof, not a status update. A
silent watcher and a dead watcher look identical from the inbox — that
ambiguity is exactly what the previous version hid inside for 44 days. So the
email carries the numbers: how many checks ran, and how many of them failed. If
every check in an hour failed, it says so in those words rather than reporting
a calm "no tickets found".

Its clock resets whenever a real availability alert goes out, so good news is
never followed by "no success this hour".

### Testing the emails

```bash
.venv/bin/python -m ep_watcher selftest   # offline: nothing sent, no credentials
.venv/bin/python -m ep_watcher test       # sends one real example of all four
```

`selftest` checks that each email goes to the right address and contains the
link and the listing details, and that a dead SMTP server can never take down a
run. `test` puts the real thing in your inbox.

**Run `test` once and check none of them landed in spam.** The alert that
matters arrives exactly once, under time pressure — mark them "not spam" now,
not on the day.

---

## The Inventory Status API (worth doing, not yet available)

Ticketmaster has an endpoint built for exactly this question, which reports
primary and resale status separately, near-real-time, with **Ireland
supported** and no bot detection, no browser, and no session:

```text
GET https://app.ticketmaster.com/inventory-status/v1/availability?events=<id>&apikey=<key>
→ { "status": "TICKETS_AVAILABLE", "resaleStatus": "TICKETS_NOT_AVAILABLE", ... }
```

It is strictly better than driving a browser in every respect except one: it
needs an access grant. It is **not** covered by the free Discovery signup —
you request it by mailing `devportalinquiry@ticketmaster.com`.

Worth sending that email today. It probably will not land before 28 August, so
the browser carries the watch in the meantime — but `sources/inventory_api.py`
is already written and wired in. Set `TM_API_KEY` and it activates itself,
running alongside the browser and taking priority when both have an opinion.

Get the event id it needs with `resolve-id` (the `18006314BD813D3E` in the URL
is a legacy host id, which is not always the same string).

---

## Running it somewhere other than the MacBook

The constraint that drives everything here: **no browser, no data.** Every
ticketmaster.ie endpoint is behind PerimeterX, including the resale API the
page itself calls:

```text
GET https://www.ticketmaster.ie/api/quickpicks/18006314BD813D3E/resale?qty=1
  → plain HTTP, no headers:      403 {"response": "block"}
  → plain HTTP, browser headers: 403 {"response": "dynamic_block"}
```

So there is no "just call the API" shortcut. The only browser-free source is
the **Discovery API**, which is a genuinely different, public, documented API —
and much coarser (see below).

The second constraint: **Chrome's headless mode is rejected (403); headed
Chrome is not (200).** On a server that means running headed Chrome under
`xvfb`, a virtual display. That is what [deploy/Dockerfile](deploy/Dockerfile)
and [deploy/ep-watcher.service](deploy/ep-watcher.service) do.

The remaining variable is the **IP address**. The working result was measured
from a residential connection. Datacentre IPs are what bot detection weights
most heavily, and GitHub Actions runners are the case we already know fails.

| Where | Cost | Browser? | Confidence |
| --- | --- | --- | --- |
| The MacBook | free | yes | **Proven** — measured working |
| Pi / old laptop at home | ~free | yes | **High** — same residential IP, different box |
| VPS + Xvfb (Docker) | ~€5/mo | yes | **Unknown** — real Chrome, datacentre IP |
| VPS + residential proxy | €5 + €15–30/mo | yes | High, but the priciest option |
| GitHub Actions | free | no | Works, but weak signal and ~hourly |

**An always-on box at home is the recommendation.** A Raspberry Pi or any old
laptop keeps the one variable proven to work (the residential IP) and removes
the one you object to (the MacBook being on).

### Cloud providers: they are all the same bet

AWS, Oracle, Hetzner, DigitalOcean, Fly.io — for this problem the choice barely
matters, because **the risk is the datacentre IP, and they all have one.** AWS
is not safer than a €4 Hetzner box; if anything its ranges are more heavily
used by scrapers and so more likely to be pre-judged. Pick on price:

| Provider | Cost | Note |
| --- | --- | --- |
| Oracle Cloud Always Free | **free** | 4 ARM cores / 24 GB, free indefinitely |
| Hetzner CX22 | ~€4/mo | Cheapest reliable x86 |
| AWS Lightsail | ~$5/mo | Simpler than EC2; EC2 free tier is 12-month only |
| Fly.io / DigitalOcean | ~$5/mo | Fine, no advantage here |

Oracle's always-free tier is the one worth trying first: it costs nothing, so
the datacentre-IP gamble is free to take.

**Whichever you pick, it is a 60-second experiment**, and the code supports it
today:

```bash
docker build -t ep-watcher -f deploy/Dockerfile .
docker run --rm --env-file ~/.ep2026-watcher/env ep-watcher check
```

If that prints a real reading, the datacentre IP is fine and you are done. If
it reports the bot check, that provider is out — try another, or fall back to a
box at home. Either way you know in about a minute, without committing.

### Keeping the MacBook going instead

Perfectly reasonable, and it is the only option measured working. Plugged in,
lid open, and:

```bash
sudo pmset -a disablesleep 1     # undo: sudo pmset -a disablesleep 0
caffeinate -dimsu &              # belt and braces
```

The watcher window is parked off-screen (`EP_OFFSCREEN=1`), so it will not
interrupt you while you use the machine normally.

### The free GitHub Actions fallback

[.github/workflows/api_watch.yml](.github/workflows/api_watch.yml) runs the
Discovery API source hourly with `EP_USE_BROWSER=0`. Set `TM_DISCOVERY_KEY`
and the Gmail secrets in the repo and it works with no hardware at all.

Be clear about what it buys you. It can catch a coarse re-release. It cannot
see a single Verified Resale listing appearing and vanishing inside five
minutes — which is the behaviour actually observed on this event. Treat it as
a safety net, not as coverage, and do not read a quiet inbox from it as "nothing
happened".

Whether it can see resale at all is an open question with a cheap answer: get
a free Discovery key, and next time the browser reports a live resale listing,
run `resolve-id`. If the `source=tmr` search shows the same listing at that
moment, free browser-free resale watching works. If it shows nothing, it does
not, and hosting needs a browser.

### Run both at once — they fail differently

Nothing stops the GitHub Actions watch and a browser watcher running together,
and it is a good idea: one has coarse vision but perfect uptime, the other has
sharp vision but depends on a machine staying up. They keep separate state, so
if both spot the same thing you get two emails. That is the right trade.

---

## Honest limits

- **It will not buy the ticket.** It finds and alerts; you buy. Automating the
  purchase is a different thing with a different risk profile, and a wrong
  automated checkout is expensive in a way a missed email is not.
- **Each poll is a real search** against Ticketmaster, not a page read. The
  default cadence is deliberately human-paced, and `watch` jitters it so the
  traffic is not a metronome. Ticketmaster's terms prohibit automated access;
  the realistic risk of running it hard is your account getting flagged, which
  costs you the ability to buy at all. That is the reason for the pacing, and
  the reason it does not hammer.
- **The bot check may change.** If the anchors stop matching, `calibrate` dumps
  exactly what the browser saw, post-search, which is where every signal lives.
- **Resale listings flicker.** One appeared, vanished, and reappeared within
  ten minutes during testing. That is the behaviour the watcher exists for, and
  the reason alerts are edge-triggered per market with an hourly re-nag rather
  than fired once and latched.

---

## Superseded

`ticket_checker.py` and `.github/workflows/check_tickets.yml` are the old
version. The workflow's schedule is disabled — it had been failing every run
since 30 June while committing a state file on each one, which is where the
~1000 `Update watcher state` commits came from. Left in place, not deleted, so
the history stays readable.
