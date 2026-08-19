# EP2026 Ticket Watcher

Watches Electric Picnic 2026 and emails `davidcoyne73@gmail.com` when a ticket
is actually buyable. Two pages, not one:

- [Weekend Camping](https://www.ticketmaster.ie/electric-picnic-2026-weekend-camping-co-laois-28-08-2026/event/18006314BD813D3E)
  — the standard, pay-in-full listing
- [Weekend Camping Instalment Plan](https://www.ticketmaster.ie/electric-picnic-2026-weekend-camping-instalment-co-laois-28-08-2026/event/18006314CFB4A99E)
  — the same weekend, paid in stages

They are separate products with separate inventory and separate resale panels.
A ticket can appear on one and not the other, so both are watched and every
alert says **which**. See [Two pages, watched separately](#two-pages-watched-separately).

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

So the working recipe is a **real, visible Chrome**. It was first proven from
a residential connection — but do not run it from yours. See the rate-limiting
section: polling hard from home got that IP flagged for ordinary browsing too,
and your home IP is the one you need in order to actually buy.

1. Load the page — expect the first response to fail. Its content lives inside
   `<noscript>`, so a walled page looks *blank* rather than obviously blocked.
2. Accept the cookie dialog. While it is up it blocks the bot check from
   completing, and the page stays empty.
3. Reload. Now you get the real page.
4. Set the quantity, press **Find Tickets**, read the result.

Four findings that invert the obvious implementation:

- **The resale panel does not exist until you search.** A fresh page load ends
  at the Find Tickets button. "Other Options → Verified Resale Tickets" is
  rendered by the *search response*. There is no passive way to watch resale —
  which is why the search is the whole design, not an optional extra.
- **"Resale Tickets will appear below when they are available." is a static
  caption.** It sits there permanently, including directly above a real
  listing. Reading it as an empty-state marker inverts your result.
- **The panel arrives in three stages, and they are not the same event.** The
  search resolves; then a *separate* call, `GET /api/quickpicks/{id}/resale`,
  answers; then the panel paints; then the listing rows paint under its
  heading. Treating any earlier stage as the later one costs you readings —
  see [Watching the response is not watching the panel](#watching-the-response-is-not-watching-the-panel).
- **"Verified Resale Tickets" contains "Verified Resale Ticket".** The panel
  heading is the plural; one listing row is the singular. A substring test
  therefore finds a listing in a panel that is rendered and completely empty.
  Match whole lines. Everything in the codebase that asks "are there
  listings?" goes through one function for exactly this reason.

To answer your question directly: **yes, it is possible, and pressing the
button is not just possible but required.** There is no read-only version of
this that works.

---

## What it has actually caught

Two Verified Resale listings on 2026-08-17, both on the standard Weekend
Camping page, both alerted by email and push inside the same minute:

| Seen | Listing | Price | Gone by |
| --- | --- | --- | --- |
| 07:49 UTC | Section STNDN1 | €366.39 | 08:10 (≤ 20 min) |
| 16:02 UTC | Section STNDN2 | €366.39 | 16:14 (≤ 12 min) |

Three things worth taking from that.

**€366.39 twice** — same price, adjacent sections, hours apart. That reads as
face value plus fees rather than opportunistic reselling, which means you can
decide in advance whether it is worth paying instead of working it out in the
ninety seconds you will have.

**Both lived 12–20 minutes**, not the five minutes seen during testing. Still
comfortably inside a 10-minute cycle, but only just, and a blind poll in that
window would have missed one outright.

**Both were on the page whose resale panel was going blind 22% of the time**
before the fix described in
[Watching the response is not watching the panel](#watching-the-response-is-not-watching-the-panel).

The 07:49 find is recorded in the log as a bare count, because listing details
were not logged until that afternoon. The 16:02 one is preserved in full. That
is the whole argument for logging what a thing *was* rather than that it
happened.

---

## Two pages, watched separately

Electric Picnic sells the same weekend twice — an ordinary ticket and an
instalment plan — on two pages with separate inventory. Each is searched on
its own clock, and that has consequences worth knowing:

**Each page has its own interval, and it is a range rather than a number.**
Since 2026-08-19 the gap between searches is drawn fresh after every search:
3–6 minutes on the standard page, 20–40 on the instalment plan. Two reasons,
and the second matters more than the first:

- A fixed cadence is a signature. A page hit at 12:00:03, 12:06:03, 12:12:04
  is describing itself. The ±25% jitter already on the loop's sleep never
  fixed that, because the page was still searched the instant it came due.
- The mean gap on the busy page drops from 360s to 270s, so a listing with a
  ~4.6 minute life is likelier to be seen at all.

**Request volume is the sum of the per-page rates**, not a function of the
cycle — which is what lets the pages be weighted by yield without spending
more. The split is deliberate: of the nine resale sightings between 13 and 18
August, eight were on the standard page and one on the instalment plan, so
searching both equally spent half the budget for an eighth of the return.

| Page | Gap | Searches/hour |
| --- | --- | --- |
| Weekend Camping | 3–6 min | 13.3 |
| Weekend Camping Instalment Plan | 20–40 min | 2.0 |
| | **total** | **15.3** |

That total is the number to watch. Roughly 20/hour is what got the home
connection flagged in development, so this sits under the line with less room
than the 12/hour it replaced. A test fails if it ever creeps past 16/hour, so
a later tweak cannot drift there quietly.

**The drawn gap is stored, not recomputed.** It lives in `state.json` and
survives a restart. Re-drawing while waiting would collapse the range to its
floor — the page becomes due the first time any draw lands low, so the
effective interval is the minimum of many draws rather than a sample from the
range. For the same reason the watch loop ticks at the shortest gap any page
can draw (180s) and legitimately does nothing on most ticks; a tick that finds
nothing due sleeps again rather than forcing a search.

**Each page keeps its own availability history.** Sharing one set of "last
seen" values across two pages means the quiet page's poll overwrites the busy
one's history — so either a listing re-alerts forever, or the next real one is
silent. Failures are counted per page too: a single shared counter let a
healthy page reset the count a broken one had just incremented, so one page
could fail every cycle for a fortnight while the watchdog never fired.

**Every alert names and links the page it is about**, and says which *kind* of
ticket in its own words:

```text
This is the INSTALMENT PLAN page — the pay-in-stages listing, not the
standard one.
```

That sentence exists because the two names differ only by a trailing
suffix, which is easy to skim past on a phone in the ninety seconds a resale
listing survives — and the two are paid for differently. The hourly report
lists **both** pages, each with its own statuses and its own link.

This was a real bug, and an instructive one. `available()` took its name and
link from the reading; the basket alert did not, so the loudest message the
watcher can send — the one arriving with a checkout countdown already
running — always described the standard page whichever page had actually
reserved. The hourly report was worse than wrong, it was *undetectable*: it
printed one page's statuses beneath the other page's URL, which looks exactly
like a correct email. The only real find so far was on the instalment page.

The tests for this run against every configured event rather than a fixed
one, so adding a third page cannot quietly reintroduce it.

---

## Securing a ticket automatically (opt-in, off by default)

For most of this project's life the scope was explicitly notification only.
That changed on 2026-08-19, after several days in which real resale listings
were found and alerted on and still sold before the buy screen could be
reached. The watcher can now click into a listing and hold it. It never pays.

**This is off unless you turn it on.** `EP_SECURE_ON_FIND=1`.

### How it runs

Two browsers, and the separation is the point:

| Browser | Profile | Signed in? | Does what |
| --- | --- | --- | --- |
| Watcher | `chrome-profile` | **no** | Every poll, all day |
| Buyer | `chrome-profile-buy` | **yes** | Only when a listing is found |

Keeping the polling anonymous means the account is exposed roughly six times a
day rather than a hundred and forty. It also means a block on the watcher
costs a profile reset, as it always has, rather than landing on the account
you need working at checkout.

Sign the buying profile in once, by hand:

```bash
python -m ep_watcher login-buy
```

Your password is never asked for, stored, or handled. Chrome opens, you sign
in, the cookies persist in that profile. Same mechanism as `login`, separate
profile — Chrome takes an exclusive lock on a user-data-dir, so the buyer
cannot share the watcher's.

**There is deliberately no automated sign-in, and there will not be.** Not
squeamishness — three concrete reasons. It would require storing the password
on disk. Scripted logins are what Ticketmaster's account security is built to
catch, so *testing* one repeatedly is a good way to get the account locked
before the event. And a cookie session obtained by hand is indistinguishable
from one obtained by script once it exists, so automation buys nothing except
the risk.

What replaces it is verification. The session is the thing that rots — cookies
expire, Ticketmaster invalidates them, a profile reset wipes them — and all
three fail silently, first showing up as a listing appearing and not being
held:

```bash
python -m ep_watcher check-buy
```

Read-only. It opens the page, reads whether the account is present, and
closes. It types nothing and baskets nothing, so it cannot itself trip
anything. `doctor` runs the cheap half of this check (does the profile exist,
does it hold cookies) on every run.

### What happens when a listing appears

1. The availability alert goes out **first**, always, before any securing is
   attempted. If everything below fails you are no worse off than before.
2. The buying browser opens, sets the quantity to 1, searches, and clicks the
   listing.
3. It presses only buttons on an allowlist — Continue, Next, Get tickets,
   Select — and refuses anything matching pay, buy, purchase, checkout,
   confirm order, or place order.
4. It gives up after 45 seconds.
5. **If a basket appears**: a second, louder alert, and the window is left
   open and frontmost on the checkout page. You have roughly four minutes.
6. **If not**: an email that says plainly there is *no* hold, and names the
   step that failed.

### The hold cannot travel

A Ticketmaster basket lives in the session cookies of the browser that created
it. Opening a link on your phone gets you a different session and an empty
basket while the hold expires. This is why the buyer must run on the machine
you will finish payment on, and why the "held" email contains **no link** —
only an instruction to go to that Mac.

### What is proven and what is not

Tested offline, no network: it will not press a payment button; it will not
claim a hold it cannot see on the page; the availability alert fires
regardless; the failure email always sends; it stays off until enabled; it
refuses to act on an event it was not given.

**Not proven:** the click-through itself. The button labels between a resale
listing and a basket were inferred, not observed — nobody has walked that flow
on this event. Expect the first live attempt to fail and to name the step that
broke. Walking the flow by hand on any event that currently has a resale
listing, and reading off the real button labels, is worth more than any amount
of further guessing.

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
./restart.sh
```

That installs and starts both LaunchAgents, and then tells you whether they
actually came up. It is safe to run at any time, from any state — it is the
one command to reach for whenever something looks wrong.

---

## Running is not the same as seeing

The distinction the watcher now makes explicitly, because getting it wrong
was a live bug on 14 August 2026.

Several sources answer each poll. The free Discovery API works from anywhere
and effectively never fails; the browser is fragile and is the **only** source
that can see a Verified Resale listing — which is how a ticket has actually
appeared on this event. Originally a poll counted as failed only if *every*
source failed, so once Discovery was configured, a browser blocked by HTTP 403
produced a perfectly healthy-looking poll. The failure counter reset every
time, the watchdog could never reach its threshold, and the hourly email
reported `0 failed` throughout. The same 403, before and after Discovery was
added:

```text
13 Aug 20:37  (browser only)   hourly report: 5 checks, 3 failed
14 Aug 04:40  (+ discovery)    hourly report: 3 checks, 0 failed
```

So there are now three outcomes per poll, not two:

| Outcome | Meaning | What happens |
| --- | --- | --- |
| **clean** | every source answered | failure counter cleared |
| **partial** | some answered, at least one failed | data kept and alerted on normally, **and** counted as unhealthy so it escalates |
| **failed** | nothing answered | as before |

A partial poll still alerts. That half matters as much as the other: counting
it as a plain failure would suppress a find, which is the worse of the two
bugs. What changes is only that it no longer *clears* the failure counter, so
a browser blocked for four polls now raises the watchdog instead of hiding
behind the API covering for it — and the email says plainly that resale has
gone dark rather than just "rate-limited".

Two related rules follow from the same principle:

- **Discovery reports resale as UNKNOWN, never UNAVAILABLE.** It cannot see an
  individual resale listing at all, so "no resale events" is *I could not
  look*, not *there is nothing*. Since UNAVAILABLE outranks UNKNOWN when
  readings are merged, answering UNAVAILABLE meant the merged reading claimed
  a confident "no resale" that no source had established.
- **A search that resolves without learning anything is a failed read.** If
  both primary and resale come back UNKNOWN, that is not a quiet "no tickets".

`doctor` reports resale visibility as its own line, with its own denominator,
so an old state file cannot read as a flawless 0% before anything is measured.
Over the first day of running, resale was unreadable on about one poll in six.

### When the page will not ask, ask the endpoint yourself

Measured to 2026-08-19: of 699 readings, 80 came away knowing nothing about
resale, and **78 of those never saw the resale API call at all**. Two distinct
causes hide in that number:

| Cause | Polls | Can a direct fetch help? |
| --- | --- | --- |
| Bot check / HTTP 403 / no internet | ~52 | No — there is no live page |
| Searched fine, but no resale call inside 25s | 26 | **Yes** |

For that second group nothing was wrong with the session. The watcher was
waiting for the page to ask a question it could have asked itself. So when a
poll would otherwise be recorded as resale-blind, it now calls the endpoint
directly from inside the live page:

```js
await fetch('/api/quickpicks/{eventId}/resale?qty=1&offset=0&limit=20',
            {credentials: 'include', cache: 'no-store'})
```

`page.evaluate` runs this in the page's own context, so it carries that
session's cookies, TLS fingerprint and origin. That is the whole reason it
works where `requests` cannot: the endpoint returns 403 to anything that is
not a real browser session, and this *is* the real browser session.

**Verified live on 2026-08-19**, not assumed:

```text
status : 200
body   : {"quantity": 0, "total": 0, "picks": [], "descriptions": []}
took   : 143 ms
```

143 milliseconds, against up to 25 seconds spent waiting for a panel to paint.

Two deliberate restraints. It runs **only** when the reading would otherwise
be UNKNOWN, so it cannot disturb the 88% of polls that already work. And it
uses `cache: no-store` rather than a cache-busting query parameter — a novel
URL misses Fastly's edge cache and hits origin, which is heavier and more
conspicuous than the call the page makes for itself. The response is
edge-cached for 15 seconds with 30 seconds of stale-while-revalidate, so
asking more often than that returns byte-identical data.

### Watching the response is not watching the panel

The correction to that one-in-six, and a caution about how it was fixed the
first time.

The panel is filled by its own API call, so waiting for it by polling the
rendered text is guesswork: while that call is in flight, "still loading" and
"arrived and empty" look identical. Listening for the network response
instead is the right instinct — it fires whether or not the panel has
anything in it.

But a Playwright `response` event fires when the response **headers** arrive,
and the panel is painted some way after that. Returning there meant the page
was read in the gap, and a perfectly good poll was recorded as resale-blind.
Measured on the logs for 2026-08-16, split at the restart that deployed it:

```text
before   resale unreadable on 10/80 polls   12%
after    resale unreadable on 22/74 polls   30%
```

The fix keeps the response for the thing it is genuinely good at — knowing
**how long to stay patient** — and lets the DOM decide. Once the call
answers, the panel gets a few seconds to paint; if it does not, that is
reported rather than guessed at, instead of spending the whole timeout on a
page that was never going to show one. The two failures are distinguishable
in the log, because "the call never came back" and "the call came back and
nothing painted" have different fixes.

Two rules came out of it, both worth keeping:

- **Whatever decides the panel is readable must agree with whatever parses
  it.** While those disagreed — the wait accepted a bare "Other Options", the
  parser demanded the heading — a poll could be declared readable and then
  parsed as blind, with nothing in the log to explain the gap. A test asserts
  that agreement directly rather than trusting it.
- **Wait for the listing rows, not just the heading.** The heading paints
  first, so parsing on it alone can report a confident `UNAVAILABLE` for a
  page about to show a listing. That is worse than being blind: `UNAVAILABLE`
  outranks `UNKNOWN` when readings are merged, so the wrong answer wins.

---

## Keeping it running

Three layers, because they fail differently.

**1. Reboots and logins.** The LaunchAgent lives in `~/Library/LaunchAgents/`
with `RunAtLoad`, so macOS starts it automatically whenever you log in.
Nothing to do after a restart except log in.

**2. Crashes.** `KeepAlive: {SuccessfulExit: false}` restarts the watcher if
it dies — but deliberately *not* when it exits cleanly on its stop date.

**3. Hangs — the one the first two miss.** A wedged Chrome keeps its PID and
looks perfectly healthy to launchd while doing nothing whatsoever. So the
watcher writes `last_check_at` on every poll, and a second LaunchAgent runs
[watchdog.sh](watchdog.sh) every 15 minutes to check that it is advancing. It
stays silent when things are fine, and won't "repair" a watcher that is merely
starting up or has correctly stopped after the event.

### Three states that look identical from outside

A still `last_check_at` has three quite different causes, and telling them
apart is the difference between fixing a hang and making a rate limit worse.

| State | Looks like | Right response |
| --- | --- | --- |
| Sleeping between polls | clock still | leave alone |
| Backing off from a 403 | clock still | **leave alone** |
| Wedged Chrome | clock still | restart |

So the watcher writes down which it is, rather than leaving the watchdog to
guess from a fixed threshold:

- **`next_poll_due`**, written before every sleep, is when the next poll is
  actually expected. The watchdog measures lateness against that plus a
  15-minute grace. A flat 45-minute limit only ever matched the daytime
  cadence — overnight, where the interval is 30 minutes jittered to 37.5 and
  the gap includes the poll itself, a real 38-minute gap was observed with
  seven minutes to spare. This also makes it *stricter* by day: a wedge at
  noon is caught in ~25 minutes rather than 45.
- **`backoff_until`**, written before a deliberate 403 backoff, marks the
  watcher as resting on purpose. That backoff doubles to a three-hour cap, so
  past 45 minutes the watchdog would otherwise restart it — and each restart
  polls the rate-limited connection again immediately, unattended, turning a
  short block into a long one. Both the watchdog and `doctor` leave it alone.

`doctor`'s own staleness limit follows the cadence in force and says which it
used (`last check 3 min ago (30 min cadence overnight)`). Deriving it from the
daytime cycle alone meant it reported a perfectly healthy watcher as wedged
every night between midnight and seven.

### When something looks wrong

```bash
./run_watcher.sh doctor    # what is broken, and the exact command to fix it
./restart.sh               # put everything back, from any state
```

`doctor` checks the agent is installed, the process is running, polling is
actually advancing, **resale is actually readable**, email and push work, the
connection isn't blocked, and the Mac isn't set to sleep. Every failure prints
the command that repairs it.
Both commands also appear in the "watcher is broken" email, so you never have
to come back here to find them.

**Read the closing summary, and note that `[WARN]` counts.** It used to tally
only hard failures, so it printed "Everything is working. Nothing to do."
directly beneath two warnings — resale unreadable on 28% of polls, and the
connection being rate-limited. The summary is the line you actually read; one
that disagrees with its own body teaches you to stop reading either. Warnings
now appear in it, while still leaving the exit code at 0, because nothing
there needs a command run:

```text
  Nothing is broken, but 2 thing(s) worth an eye:

    · Resale visibility: resale readable on 78/102 polls (76%)
    · Connection: 4 block(s) in the last 24h, none in the last hour — recovered.
```

```bash
tail -f ~/.ep2026-watcher/logs/watcher.log     # what it is doing
tail -f ~/.ep2026-watcher/logs/watchdog.log    # only written when it acts
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
| `doctor` | Is it healthy? Prints the exact fix for anything that isn't |
| `login` | Open Chrome to sign in by hand (only needed for *buying*) |
| `login-buy` | The same, for the **buying** profile — needed for `EP_SECURE_ON_FIND` |
| `check-buy` | Is the buying profile still signed in? Read-only, types nothing |
| `calibrate` | Dump screenshot + text + HTML after a search |
| `networks` | List every connection the watcher has seen, with blocks against each |
| `status` | Print config and health |
| `resolve-id` | Look up the Discovery event id for the API source |

---

## Configuration

> **Nothing is assumed delivered.** Every alert reports whether it actually
> reached you, and the clocks that stop it repeating are only started when it
> did. If the email and the push both fail — which is exactly what happens when
> the fault is your network — the watcher keeps trying on the next poll instead
> of going quiet for six hours. See "When the alert itself cannot get out".

Environment variables, all optional:

| Variable | Default | Notes |
| --- | --- | --- |
| `WANTED_QUANTITIES` | `1` | Quantities to search per poll |
| `EP_STANDARD_POLL_MIN` | `180` | Shortest gap between searches of the standard page |
| `EP_STANDARD_POLL_MAX` | `360` | Longest. The gap is drawn fresh from this range after each search |
| `EP_INSTALMENT_POLL_MIN` | `1200` | The same, for the instalment plan |
| `EP_INSTALMENT_POLL_MAX` | `2400` | |
| `EP_SECURE_ON_FIND` | `0` | Set `1` to let the buying browser hold a resale listing. Needs `login-buy` first |
| `EP_SECURE_TIMEOUT_SECONDS` | `45` | Seconds to spend trying to secure before giving up |
| `EP_BUY_PROFILE_DIR` | `~/.ep2026-watcher/chrome-profile-buy` | Where the signed-in buying profile lives |
| `EP_POLL_SECONDS` | `300` | Fallback per-page gap for a page configured without a range |
| `EP_WATCH_LABEL` | `Electric Picnic 2026` | What to call the watch in emails covering every page |
| `EP_HEARTBEAT_HOURS` | `1` | How often to send the "still nothing" report |
| `EP_NIGHT_POLL_SECONDS` | `1800` | Overnight cycle. `0` disables the slowdown |
| `EP_SEARCH_TIMEOUT` | `90` | Seconds to wait for a search to resolve. Raised from 45 after daytime timeouts on a tethered connection |
| `EP_NIGHT_SEARCH_TIMEOUT` | `90` | The same, overnight. Equal to the daytime value now; it may never be lower |
| `EP_GRACE_MINUTES` | `15` | How late a poll may be before the watchdog restarts it |
| `EP_NETWORK_NAMES` | — | `key=Label` pairs naming your connections, comma separated. Key may be a gateway MAC, gateway IP or public IP |
| `EP_HOTSPOT_LABEL` | `David's hotspot` | What to call a detected phone hotspot |
| `EP_HOME_LABEL` | `home Wi-Fi` | What to call the home connection |
| `EP_PROFILE_MAX_AGE` | `90` | Minutes before the browser identity is rebuilt pre-emptively. `0` waits for the block instead |
| `EP_SEND_TIMEOUT` | `20` | Seconds any one email or push may take before it is treated as undelivered |
| `EP_OFFSCREEN` | `1` | Park the Chrome window off-desktop |
| `EP_HEADLESS` | `0` | **Leave this alone.** Headless is always blocked |
| `PRESS_THE_BUTTON` | `1` | Set `0` and it can no longer answer the question |
| `EP_USE_BROWSER` | `1` | Set `0` for API-only mode (no Chrome needed) |
| `TM_DISCOVERY_KEY` | — | Free Discovery API key — enables the browser-free source |
| `TM_API_KEY` | — | Inventory Status API key (needs an access grant) |

### When the alert itself cannot get out

On 18 August 2026 a power cut took the house network down for 69 minutes. At
09:39 four consecutive failures tripped the watchdog, which tried both channels
and lost both — the outage *was* the network, so the Gmail send and the ntfy
push each died on DNS resolution:

```
[09:39] WARNING: watchdog-email notification failed: nodename nor servname ...
[09:39] WARNING: watchdog-push notification failed: ... ntfy.sh ...
```

The alert was then recorded as sent, which started the six-hour re-nag clock,
so the failures at 09:48 and 09:57 raised nothing at all. Power came back and
it recovered by itself — but had the cut lasted, the watcher would have sat
silent for six hours believing it had already raised the alarm. That is this
project's founding failure, one layer below the alerting logic.

So:

- `notify.watchdog()` and `notify.heartbeat()` return whether anything was
  delivered, and `_safe()` reports success rather than swallowing it.
- The re-nag clock and the hourly clock are only advanced on a real delivery.
  An undelivered report keeps its counters and retries on the next poll, with
  an honest, longer window.
- Every send has a 20-second ceiling, so a dead network cannot stall the poll
  that is trying to find a ticket.
- `doctor` now *signs in to Gmail* rather than checking that a password is set.
  A revoked app password looks identical from the outside, and the first thing
  to discover it would be the one alert that mattered failing to arrive.

### Any number of connections, not two

The watcher was built when there were two — a home Wi-Fi and a phone hotspot —
and its labelling said so: an address either equalled `EP_HOME_IP` or it was
"the hotspot". On 18 August there were three in one morning. A power cut moved
the MacBook onto a tethered eir connection and then onto a Sky line, and the
second switch was announced as *"new address, same connection"*, because with
only two names available both non-home connections were called the same thing
and comparing labels could not tell them apart.

**A connection is now identified by its default gateway's MAC address** — the
router itself. Read from the ARP table, so it costs two cheap subprocess calls
and no permission at all, and it is stable in exactly the case that broke the
old scheme: a carrier handing a tether a new public address every twenty
minutes does not change the router.

The obvious identity would be the Wi-Fi network's name, and it cannot be used.
Measured on this Mac:

```
networksetup -getairportnetwork en0   ->  "You are not associated with an
                                           AirPort network."  (while associated)
ipconfig getsummary en0 | grep SSID   ->  "SSID : <redacted>"
```

macOS withholds the SSID from any process without Location Services
permission. That is a GUI grant which does not survive launchd reliably, and a
watcher that silently loses the ability to tell two networks apart is worse
than one that never had it.

What follows from the change:

- **A re-address is distinguished from a switch by fact, not by guess.** Same
  router, new public address is reported as a re-address; a different router
  is reported as a switch.
- **Blocks follow the connection**, not whichever address it held at the time,
  so a tether re-addressed six times in an afternoon is one connection with
  one block history rather than six strangers.
- **Naming is optional.** An unnamed connection is tracked, counted and blamed
  correctly; it is described by its address range ("the 192.168.0.x network
  via Wi-Fi") instead of named. A phone hotspot names itself, by its gateway;
  the first connection ever seen is assumed to be home.
- **`./run_watcher.sh networks`** lists everything it has seen, with searches
  and blocks against each and the key to name it by. Every "you are on a
  different connection" email carries the same key.
- Connections that never caused trouble are forgotten three days after they
  were last used, so the list stays readable.

### Two faults that get named in their own words

Neither is "Ticketmaster is busy", and neither is fixed by retrying, so both
say what they are:

- **This Mac has no internet.** Every source fails to resolve or connect. The
  alert leads with that, points at the Wi-Fi or hotspot, and promises a second
  email when the connection returns — which arrives carrying how long the
  watcher was dark and what it might have cost.
- **The event page is gone.** Ticketmaster answers `404`. No amount of
  retrying, backing off or resetting the profile fixes a URL that has changed,
  so this escalates immediately and names the page and the file to edit. Left
  undetected it is the quietest possible death: a watcher running faithfully
  forever against a page that no longer exists.

### The browser identity is rebuilt before it is refused

Across 28 blocks in six days, **every single one was cleared by a fresh browser
profile on the first attempt**, and the exponential backoff behind that reset
was never once reached. The wall is carried in the bot-check cookies, not in
the IP — which the watcher had already recorded the other way round: after a
block, moving to a completely different network did *not* clear it, while a
fresh profile on the same network worked first try.

Waiting for the wall costs two resale-blind readings and a wasted cycle, four
to ten times a day. So the profile is now thrown away and rebuilt every
`EP_PROFILE_MAX_AGE` minutes, during a sleep window, and the reactive reset
stays as the backstop for the ones that beat the timer.

A 403 also ends the cycle now. A refusal is a verdict on this client, not on
this page, so polling the next page merely earns a second refusal — and one
wall is recorded as one block however many pages saw it.

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

All to `davidcoyne73@gmail.com`, and every one that concerns a ticket names
and links the page it is about — never "the event page" in the abstract:

| Email | When | Push? |
| --- | --- | --- |
| **Tickets available** | A listing appears on the box office or verified resale | yes, urgent |
| **In the basket** | A reserve actually succeeded — a live hold, with a countdown | yes, urgent |
| **HELD — go to the laptop** | The buying browser secured a resale listing (opt-in) | yes, urgent |
| **Could not hold it** | A securing attempt was made and failed, and why | no |
| **No luck yet** | Hourly while nothing has turned up — reports **both** pages | only if failing |
| **Watcher is broken** | 4 consecutive failed checks, then every 6h until fixed | yes |
| **Working again** | It recovered from a run of failures | low |
| **Session summary** | Whenever day/night settings change — twice a day | no |
| **Connection changed** | The MacBook moved to a different network | no |
| **Watcher stopped** | Once, on the stop date | low |
| **Mac has gone quiet** | Sent *from GitHub* when the laptop stops checking in | yes |

The push column is deliberate. Three of these are scheduled or
self-inflicted — a session summary, a network switch you just made, the
retirement notice — and buzzing a phone for them trains you to swipe away the
channel that carries the ticket alert. That channel has to stay worth looking
at.

The "watcher is broken" email names the worst-affected page *and its URL*: the
likeliest cause of one page failing while the other is fine is that page's URL
having changed, which takes seconds to check once you have the link.

### The availability alert leads with a link, not a recipe

The alert used to hand over the bare event URL followed by four numbered steps
— open in a browser, set the quantity, press Find Tickets, scroll to Other
Options. On 2026-08-19 David reported where the ticket is actually lost: not in
noticing the email, but in the seconds spent working through those steps while
a listing that lives about 4.6 minutes sells to somebody already on the page.

So the alert now leads with a link carrying the quantity, and the push title
carries the section and price:

```text
PUSH:  EP2026 Section STNDN1 · €366.39
LINK:  …/event/18006314BD813D3E?quantity=1#resale-ly7vs38jkx
```

The lock screen alone is now enough to decide whether to move. The four steps
are still in the email, demoted to a fallback under "IF THE LISTING IS NOT
THERE", because the link is a **hypothesis, not an observation**: the find
recorded on 2026-08-18 shows Ticketmaster's search changes page state without
changing the address, so `?quantity=1` may not be honoured. Every find now
records both the live URL and the link the alert sent, so the next real
listing settles it.

The listing id is carried on the `Listing` object but deliberately kept out of
`describe()`. That string drives the new-listing diff, and if Ticketmaster
regenerates ids per poll, leaking one in would make the same ticket look new
on every check and re-alert on a four-minute clock.

### Session summaries, at each change of settings

The watcher runs in two modes with different settings and used to cross
between them silently. Now each crossing sends one email: what changed, why,
and what the finished session did.

```text
SETTINGS CHANGED
  Poll cycle                : every 10 min  →  every 30 min
  Search timeout            : 45s  →  90s
  Next change               : 07:00 local (or the first poll after), back to daytime

DAYTIME SESSION JUST ENDED
  Ran for                   : 16.5 hours
  Page checks               : 200
  Of those, unhealthy       : 0
  Resale readable           : 198/200 (99%)
  Rate-limit blocks         : 0
  Tickets found             : 1

  What turned up:
    • 07:49 UTC — Weekend Camping: Verified Resale — Section STNDN1 — €366.39
```

Two reasons it exists. A watcher that quietly starts polling three times more
slowly is one you cannot reason about from the inbox, and every ambiguity of
that kind here has eventually cost something. And the hourly report can only
ever show an hour, so "how did the night go" previously had no answer short of
reading the log by hand — which is how a resale regression went unnoticed for
six hours.

**It reports what a listing *was*, not just that there was one.** A listing
lives minutes, so by the time the summary arrives it has sold; a bare count
teaches you nothing about what these go for. Kept per session, capped at 20 so
a fortnight cannot grow `state.json` without limit.

It is found by comparing the *stored* mode against the current one rather than
by catching the instant of the change, so a restart across the boundary still
reports the finished session instead of swallowing it.

The "watcher is broken" email names the worst-affected page *and its URL*: the
likeliest cause of one page failing while the other is fine is that page's URL
having changed, which takes seconds to check once you have the link.

The status emails carry a **Connection health** block — `[OK]`, `[WATCH]` or
`[BLOCKED]` — showing how many times Ticketmaster has rate-limited **the
connection you are currently on** in the last hour and day, and what to do
about it. At `[BLOCKED]` it spells out the steps: stop the watcher, browse
over mobile data, sign in, wait, and slow the cadence before restarting.

That block exists because of what happened on 2026-08-13: the watcher polled
too fast, the block escalated to the home IP, and ordinary manual browsing
stopped working. The failure wasn't getting blocked — it was that nothing
said so, because a run of quiet failures looks exactly like a quiet
Ticketmaster.

**Blocks follow the connection, not the clock.** Each 403 records which
connection it happened on, and the verdict is computed for the connection in
use — with any *other* connection that is in trouble named alongside it:

```text
Connection health [OK]
  No blocks on phone hotspot in the last 24 hours — it looks healthy.

  Note: home Wi-Fi (86.44.208.194) took 4 block(s) in the last 24h — that is
  the connection in trouble, not this one.
```

Counting blocks by time alone meant that switching networks — the exact thing
the watcher had just asked for — produced an email saying the *fresh*
connection was rate-limited, on the strength of blocks the old one collected.
Advice that punishes you for following it is worse than no advice. Blocks
recorded before this existed still count, but no longer get a connection
*named*: nothing recorded which one they were, and a confident wrong name
points you away from the one that is really burnt.

### Alternating home Wi-Fi and the phone hotspot

The status emails also carry a **Network** block, and every ~3 hours (or 30
searches, whichever comes first) it asks you to switch the MacBook between
your home Wi-Fi and your phone's Personal Hotspot. When it does, that ask
goes in the **subject line** — an instruction buried three paragraphs into an
hourly "no luck yet" email is one nobody reads.

The point is to halve the volume either connection sees, and to keep a second
working connection in reserve: if one gets flagged, you can still buy on the
other.

**Switching the network is the only thing you have to do.** The watcher
detects its own public IP, so it notices the change by itself, resets its
counters, attributes any blocks to the connection they happened on, and tells
you when to switch back. There is nothing to confirm and no setting to change.
The IP is looked up once per cycle rather than once per page, and a momentary
failure to reach the IP-echo service is not treated as a network change.

It also **emails you the moment it moves**, rather than waiting up to an hour
to mention it in a report headed "no luck yet". Which connection is in use
decides where blocks land, and the burnt one is the one you must not try to
buy on, so the change is worth saying at the time. The email names what was
left, what is now in use, the health of each, and what the old connection
collected while it was in service.

Two cases are told apart, because they look identical in the state file:

- **A switch you made** (home ↔ hotspot). Always emailed.
- **A new address on the same connection.** A tethered phone is usually given
  a fresh address each time it reconnects — observed twice on 2026-08-17 —
  and calling that "you switched networks" would be wrong. Reported as a
  re-addressing, and limited to one email per ten minutes so a flapping
  tether cannot fill the inbox with mail about something you did not do. A
  genuine switch is never suppressed, however recent the last email.

Switching **onto** an already rate-limited connection puts `CAUTION` in the
subject. Switching is meant to buy a clean connection; landing on a burnt one
silently would defeat the whole scheme.

### Ask only for the IPv4 address

The IP lookups are pinned to the IPv4-only form of each service, and that is
load-bearing rather than tidy. Measured from the home connection on
2026-08-17, the unpinned hostnames disagreed with each other:

```text
api.ipify.org    -> 86.44.208.194
ifconfig.me/ip   -> 2001:bb6:4cb5:f000:81f0:2eb3:1625:7556
icanhazip.com    -> 2001:bb6:4cb5:f000:81f0:2eb3:1625:7556
```

A dual-stack connection has both addresses, so which one comes back depends on
which service answered rather than on which network you are using. The watcher
treats a different address as a different connection, so a v6 answer looks like
a switch that never happened: counters reset, a switch email sent, and —
because the v6 address is not `EP_HOME_IP` — **the home connection labelled
"phone hotspot"**. Blocks on home would then be recorded against a connection
that does not exist, and the health line would call the connection you need
for buying clean while it was being throttled.

If no service returns a v4 address the answer is "don't know", which leaves
the known connection untouched — better than inventing a switch.

| Variable | Default | Notes |
| --- | --- | --- |
| `EP_ROTATE_HOURS` | `3` | Ask to switch after this long on one connection |
| `EP_ROTATE_SEARCHES` | `30` | ...or after this many searches, whichever first |
| `EP_HOME_IP` | — | Optional. Labels this IP "home Wi-Fi" instead of guessing |

Switching more often does **not** reduce the daily total per connection —
that is set by the poll rate, and ~230 searches a day split two ways is ~115
each however often you alternate. What it reduces is how many land on one IP
inside any given hour, which is what a rate limit actually measures.

**Set `EP_HOME_IP`.** Without it the watcher assumes the first connection it
ever saw is home, which is only reliable if you started it at home *and*
there are exactly two connections. A phone hotspot is usually issued a
different IP each time you tether, so on the third or fourth address the
guess quietly stops meaning anything — and the label is what tells you which
connection is safe to buy on.

The scheme only helps if you act on the ask. On 2026-08-16 the watcher sat on
home Wi-Fi from 11:18 to 18:23 — 82 of that day's 86 searches, about 2.7× the
30-search budget — and every one of the day's four blocks landed on it. That
is the connection you need working in order to buy.

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
.venv/bin/python -m ep_watcher test       # sends one real example of each
```

`selftest` runs every suite in `tests/` — offline, no credentials, safe to run
while the watcher is running. It checks that each email goes to the right
address, names and links the right page, carries the listing details, and that
a dead SMTP server can never take down a run.

`test` puts five real samples in your inbox, including the basket alert, which
was missing from the drill for a while — the one message you least want to see
for the first time under a countdown. The find and basket samples are built on
the **instalment** page on purpose: a drill built on the first page proves
nothing about an alert that always named the first page.

**Run `test` once and check two things**: that none landed in spam, and that
the samples name the page they claim to. The alert that matters arrives
exactly once, under time pressure — mark them "not spam" now, not on the day.

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

### Cloud providers: two hard requirements

Providers differ less than you would think — the risk is the datacentre IP and
they all have one. But two requirements rule most cheap tiers out:

**x86-64, not ARM.** Google ships Chrome for Linux on `amd64` only. On ARM you
get Chromium, which is a different fingerprint from the browser this was
verified against, and Chromium-flavoured browsers are what this site rejects.

**At least 2 GB RAM.** Chrome will thrash or get OOM-killed on 1 GB.

Together those disqualify the free tiers. Oracle Always Free is ARM (Ampere);
its x86 alternative is 1 GB. AWS's always-free x86 is 1 GB too.

| Provider | Cost | Verdict |
| --- | --- | --- |
| **Hetzner CX22** | ~€4/mo | **Best fit** — x86, 4 GB, EU, hourly billing |
| DigitalOcean 2 GB | ~$12/mo | Fine, three times the price |
| AWS Lightsail 2 GB | ~$12/mo | Fine, no advantage |
| Oracle Always Free | free | ARM → Chromium → expect blocking |
| Any 1 GB tier | ~$5/mo | Chrome will not run reliably |

Hetzner bills by the hour, so **testing whether a datacentre IP works at all
costs about one cent** — spin one up, run the check, destroy it if it fails.
That test is the first thing to do, before committing to anything.

### Setting one up

```bash
curl -fsSL https://raw.githubusercontent.com/theMightyGit73/ep2026-ticket-watcher/main/deploy/bootstrap.sh | bash
```

Installs Docker, clones, builds, and runs a single `check` — deliberately
stopping before it starts the service, because that check answers the only
question that matters: does this host get the real page, or a 403?

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

## Rate limiting — read this before speeding it up

This is the failure mode most likely to cost you a ticket, and it was
triggered for real during development.

Polling every 3 minutes, roughly 30 searches in an afternoon was enough for
ticketmaster.ie to start answering **HTTP 403** to the same headed Chrome that
had been getting 200 all day. Verified it was the rate and not the setup: the
identical command that worked fifteen minutes earlier also got 403.

What the watcher does about it:

- **403 is not 401.** A 401 is the ordinary challenge and a reload fixes it.
  A 403 means blocked, where retrying has never once helped and merely triples
  the request volume at the worst moment. The watcher bails immediately on 403.
- **It backs off exponentially** — 30 minutes, doubling to a 3-hour cap —
  and resets the moment a real reading comes back. Being blocked and carrying
  on at the normal cadence is how a short rate-limit becomes a long one.
- **The default cycle is 10 minutes** — `EP_POLL_SECONDS=300` per page, two
  pages. That is 12 searches an hour, against the ~20/hour that got the home
  IP flagged — roughly 3,250 over a fortnight once the overnight slowdown is
  counted, rather than the ~13,000 a 3-minute cycle would send. Adding the
  second page did not raise the hourly rate: the cycle scales with the page
  count instead.
- **Overnight it drops to 30 minutes** (`EP_NIGHT_POLL_SECONDS`, midnight to
  07:00 local). A headstart is worth nothing while you are asleep, and those
  hours otherwise accumulate volume on the connection unattended, with nobody
  awake to notice a block.

The tension is real and worth stating plainly: a resale listing observed
during testing lived about **five minutes**, so a 10-minute cycle genuinely
will miss some. But a blocked watcher misses *all* of them. A watcher that
gets itself banned on day two catches nothing on day nine.

If you want to run hot during a known onsale, lower `EP_POLL_SECONDS` for that
window and put it back afterwards — and accept it may cost you the rest of the
fortnight.

If you are blocked right now, do nothing. It clears on its own, usually within
hours, and the watcher will pick back up without help.

---

## It stops itself on 28 August

Both halves retire after `2026-08-28` — the last watching day, since the
festival opens that morning and a ticket found then is still usable.

- **The Mac watcher** exits cleanly and sends one final "watcher stopped"
  email. The LaunchAgent uses `KeepAlive: {SuccessfulExit: false}`, so it
  restarts on a crash but *not* on that deliberate clean exit. With a plain
  `KeepAlive: true` it would announce its own shutdown every minute forever.
- **The GitHub workflow** skips with a message in the log.

Change `EP_STOP_AFTER` in `~/.ep2026-watcher/env` and `STOP_AFTER` in the
workflow to watch a later event. Set `EP_STOP_AFTER=` empty to disable the
stop entirely.

The goodbye email exists because unexplained silence is the one thing this
design refuses to be ambiguous about — "no more emails" should never leave
you wondering whether it finished or died.

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
  than fired once and latched. The two real listings caught on 2026-08-17
  lived 12 and 20 minutes — longer than the five-minute worst case, but not by
  enough to relax about a missed poll.
- **The overnight timeout is a hypothesis, not a measurement.** Every
  non-resolving search so far fell between 22:08 and 01:00, so the search wait
  is longer in that window. Five clustered observations say the page is slow
  then; they do not say 90 seconds is enough. If timeouts continue at that
  value the cause is something other than slowness, and a bigger number will
  not find it.
- **A blind poll is close to a missed chance, not a fraction of one.** A
  listing lives about one poll interval, so "resale readable on 76% of polls"
  is not a comfortable margin. Watch that line in `doctor`; it is the one
  health number that measures whether the watcher can do its job, as opposed
  to whether it is running.
- **The watcher is only as current as its last restart.** It runs from this
  checkout, so pulling or editing changes nothing until `./restart.sh`. The
  GitHub Actions backstop is the opposite — it checks out `origin/main`, so it
  is only as current as the last **push**.

---

## Superseded

`ticket_checker.py` and `.github/workflows/check_tickets.yml` — the old
watcher and its cron — have been deleted. They could not be fixed in place:
the approach itself (fetch the page over plain HTTP, parse `__NEXT_DATA__`,
run it in CI) fails at three independent layers, all documented above.

Nothing is lost. `git log -- ticket_checker.py` still has every line of it,
and the post-mortem lives in this README rather than in comments on a file
nobody runs.
