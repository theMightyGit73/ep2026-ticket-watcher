# EP2026 Ticket Watcher

Watches Electric Picnic 2026 and emails `davidcoyne73@gmail.com` when a ticket
is actually buyable. Three pages, not one:

- [Weekend Camping](https://www.ticketmaster.ie/electric-picnic-2026-weekend-camping-co-laois-28-08-2026/event/18006314BD813D3E)
  — the standard, pay-in-full listing
- [Weekend Camping Instalment Plan](https://www.ticketmaster.ie/electric-picnic-2026-weekend-camping-instalment-co-laois-28-08-2026/event/18006314CFB4A99E)
  — the same weekend, paid in stages
- [Early Entry Pass](https://www.ticketmaster.ie/electric-picnic-2026-early-entry-pass-co-laois-27-08-2026/event/18006314E36BAC7B)
  — campsite access from 2pm Thursday. An **add-on**, not a ticket: only
  valid alongside a Weekend Ticket. Watched and alerted on, but **not secured**
  — it appears several times a day and each attempt spends the buying browser

They are separate products with separate inventory and separate resale panels.
A ticket can appear on one and not the other, so both are watched and every
alert says **which**. See [Two pages, watched separately](#two-pages-watched-separately).

It works by doing what you do by hand: setting the quantity, pressing
**Find Tickets**, and reading the answer — including the **Verified Resale**
panel that the search reveals.

---

## What is in here

Long, because everything in it was measured rather than assumed. If you
are here for one thing, it is most likely
[Keeping it running](#keeping-it-running) or
[When something looks wrong](#when-something-looks-wrong).

- [Read this first if you are back for 2027](#read-this-first-if-you-are-back-for-2027)
- [Why the old one never worked](#why-the-old-one-never-worked)
- [What actually works, and how it was established](#what-actually-works-and-how-it-was-established)
- [What it has actually caught](#what-it-has-actually-caught)
- [Two pages, watched separately](#two-pages-watched-separately)
- [Securing a ticket automatically (opt-in, off by default)](#securing-a-ticket-automatically-opt-in-off-by-default)
  - [The test ran, and `qty=1` is refused too](#the-test-ran-and-qty1-is-refused-too)
  - [Ten of fourteen retries never left the machine](#ten-of-fourteen-retries-never-left-the-machine)
  - [The chase is two minutes now, not twelve](#the-chase-is-two-minutes-now-not-twelve)
  - [A run of refusals now raises its own alarm](#a-run-of-refusals-now-raises-its-own-alarm)
  - [The alerts stopped shouting about tickets he has already seen](#the-alerts-stopped-shouting-about-tickets-he-has-already-seen)
  - [The URL works. We reached a checkout and did not notice](#the-url-works-we-reached-a-checkout-and-did-not-notice)
  - [It is the instalment page that works](#it-is-the-instalment-page-that-works)
  - [Pressing the one button that reserves](#pressing-the-one-button-that-reserves)
  - [Reading Fastly's edge is reading the past](#reading-fastlys-edge-is-reading-the-past)
  - [The final-48-hours cadence](#the-final-48-hours-cadence)
  - [What the 45s cadence actually cost](#what-the-45s-cadence-actually-cost)
  - [How it runs](#how-it-runs)
  - [What happens when a listing appears](#what-happens-when-a-listing-appears)
  - [The weekend ticket always wins the browser](#the-weekend-ticket-always-wins-the-browser)
  - [Nothing may restart the watcher while a ticket is held](#nothing-may-restart-the-watcher-while-a-ticket-is-held)
  - [The hold probably cannot travel — but the email offers the link anyway](#the-hold-probably-cannot-travel--but-the-email-offers-the-link-anyway)
  - [What is proven and what is not](#what-is-proven-and-what-is-not)
- [Setup](#setup)
  - [Run it continuously](#run-it-continuously)
- [Running is not the same as seeing](#running-is-not-the-same-as-seeing)
  - [When the page will not ask, ask the endpoint yourself](#when-the-page-will-not-ask-ask-the-endpoint-yourself)
  - [Watching the response is not watching the panel](#watching-the-response-is-not-watching-the-panel)
- [Keeping it running](#keeping-it-running)
  - [The logs rotate themselves, by copying rather than renaming](#the-logs-rotate-themselves-by-copying-rather-than-renaming)
  - [What is backed up, and what a backup cannot do](#what-is-backed-up-and-what-a-backup-cannot-do)
  - [Three states that look identical from outside](#three-states-that-look-identical-from-outside)
  - [When something looks wrong](#when-something-looks-wrong)
- [Commands](#commands)
- [Configuration](#configuration)
  - [When the alert itself cannot get out](#when-the-alert-itself-cannot-get-out)
  - [Any number of connections, not two](#any-number-of-connections-not-two)
  - [Two faults that get named in their own words](#two-faults-that-get-named-in-their-own-words)
  - [The browser identity is rebuilt before it is refused](#the-browser-identity-is-rebuilt-before-it-is-refused)
  - [Why quantity matters](#why-quantity-matters)
- [The emails](#the-emails)
  - [The availability alert leads with a link, not a recipe](#the-availability-alert-leads-with-a-link-not-a-recipe)
  - [Session summaries, at each change of settings](#session-summaries-at-each-change-of-settings)
  - [Alternating home Wi-Fi and the phone hotspot](#alternating-home-wi-fi-and-the-phone-hotspot)
  - [Ask only for the IPv4 address](#ask-only-for-the-ipv4-address)
  - [Testing the emails](#testing-the-emails)
- [The Inventory Status API (worth doing, not yet available)](#the-inventory-status-api-worth-doing-not-yet-available)
- [Running it somewhere other than the MacBook](#running-it-somewhere-other-than-the-macbook)
  - [Cloud providers: two hard requirements](#cloud-providers-two-hard-requirements)
  - [Setting one up](#setting-one-up)
  - [Keeping the MacBook going instead](#keeping-the-macbook-going-instead)
  - [The free GitHub Actions fallback](#the-free-github-actions-fallback)
  - [Run both at once — they fail differently](#run-both-at-once--they-fail-differently)
- [Rate limiting — read this before speeding it up](#rate-limiting--read-this-before-speeding-it-up)
- [It stops itself on 28 August](#it-stops-itself-on-28-august)
- [Honest limits](#honest-limits)
- [Superseded](#superseded)

---
## Read this first if you are back for 2027

The 2026 run is over. **119 distinct resale listings found and alerted on,
128 attempts to buy one, none secured, no ticket.** What follows is what that
bought in knowledge, written for whoever picks this up next August — most
likely David, a year older and in a hurry.

### The timing model, which is the most valuable thing here

| | Measured, 2026 |
| --- | --- |
| First listing seen | 20 Aug, 13:36 |
| **Last listing ever seen** | **26 Aug, 10:37** |
| Busiest day | 25 Aug — **44 listings** |
| Event date | 28 Aug |
| Event pages returned HTTP 410 Gone | 27 Aug, 23:23 |

Two things follow, and both contradict what this project assumed at the time.

**Resale closes about two days before the gates, not on the day.** The last
listing appeared 46 hours before the event. Everything after that was an empty
feed, and then the pages were removed outright. Any plan that plays for "the
last-minute panic sell" is playing for a window that does not exist.

**Supply peaks three days out, not in the final 48 hours.** The 25th produced
44 listings — more than double any other day — and the 26th produced 12, all
before 10:37. On the 24th this README confidently predicted the heaviest
supply would come on the 26th and 27th. It was wrong, and the cost of being
wrong was tuning for speed during the days when the market was already
closing.

So: **be running and fully working by 20 August at the latest**, treat 23–25
August as the real window, and expect nothing after the 26th.

### The bug that actually cost the ticket

At 00:53 on 26 August the watcher reached a live checkout page — HTTP 200,
ticket type, section, Total to Pay €366.39, David's name fields, one button
left to press. It did not recognise it, logged "no basket appeared", emailed
to say it could not hold the ticket, and moved on.

`BASKET_MARKERS` was looking for `proceed to checkout` and `your tickets are
reserved`. The page said **SECURE CHECKOUT**, **Proceed to payment** and
**Total to Pay**. Every string in that list had been *imagined*, never
measured, because no checkout page had ever been seen to measure.

**The lesson generalises past this one list.** This project spent a fortnight
reverse-engineering a purchase flow entirely from failures, and produced four
confident diagnoses in a row — a lost race, somebody else's basket, a quantity
of zero, a dead URL — each written into the code as settled fact and each
overturned by the next day's logs. There was never a positive control.

If you do one thing differently next year: **get a successful checkout
captured early**, by hand if necessary, on any event you are willing to
actually buy a ticket for. Read the real vocabulary off the real page before
writing a single matcher against it.

### What worked, and what to keep

- **The resale sweep is the detector.** Every weekend listing found in 2026
  came from the direct `/api/quickpicks/{event}/resale` call, not from the
  rendered panel. The panel is unreliable by design — 78 of the first 80
  resale-blind polls never saw it render at all.
- **Watch the instalment plan at least as hard as the standard page.** It is
  the same festival, the same weekend, the same money — and it is where both
  checkouts came from. Standard page: **0 reached out of 149 requests**.
  Instalment: **2 out of 81**. The likeliest reason is simply that fewer
  people race for a pay-in-stages listing.
- **Alert on new listings loudly and on repeats quietly.** Undifferentiated
  alerting sent 69 pushes for 16 listings in one day, which teaches the reader
  to ignore the one that matters.

### What to stop doing

- **Do not chase speed past the cache.** The endpoint sits behind Fastly with
  `max-age=15, stale-while-revalidate=30`, so an answer can be 45 seconds old
  before it reaches you. Query parameters do not bust it — the cache key
  ignores the query string entirely, which was measured, and even changing
  `limit` returns the same cached copy. That floor is unavoidable, and it
  applies to everyone racing for the same listing.
- **Do not run the sweep hot.** 45s drew a block roughly every hour and cost
  ten in a day. 90s was survivable but still drew 403s. 300s was quiet. The
  connection being blocked costs every listing, not just the fast ones.
- **Do not trust `check-buy` alone.** It proves cookies are on disk, not that
  Ticketmaster still honours them. A sign-out-everywhere leaves the jar
  looking perfect. Ask `identity.ticketmaster.ie/json/signed-in` from a copy
  of the profile if it matters.

### The suite had a time bomb in it, now defused

Worth knowing before you trust a green run next year, because it was found by
accident on 30 August 2026 — two days after the event — while updating this
file.

Six test files went red the moment the festival passed, and none of them
because anything broke:

- The Early Entry Pass carries a hard-coded `stop_after` of `2026-08-27`, so
  `expired()` began returning True for real. Three files that count "every
  watched page" quietly became two-page counts.
- `watchdog.sh` correctly refuses to restart the watcher once past
  `EP_STOP_AFTER`, so every check asserting that a stalled watcher *should* be
  restarted started failing.

Both behaviours are right in production. The tests were simply reading the
wall clock and calling it a defect. A suite that fails on a calendar cannot
tell a real regression from a turned page, which is exactly the state you do
not want to inherit in a hurry next August.

Fixed by pinning the dates inside the tests that depend on them — 
`EP_EARLY_ENTRY_STOP_AFTER` and `EP_STOP_AFTER` are set to `2099-12-31` in
`tests/_sandbox.py`, `test_page_budget.py`, `test_event_identity.py`,
`test_hold_not_restarted.py` and `test_liveness.py`, each with a note saying
why. **All 73 files pass again**, and they will still pass in 2027.

### Turning it back on

The code is intact and the switches are all documented below. To point it at
2027: set the new event URLs in `config.EVENTS`, set `EP_STOP_AFTER` to the
new date, re-run `login-buy` for the buying profile, and start it. The
LaunchAgents were unloaded on 30 Aug 2026 and `restart.sh` reinstalls them.

Everything else in this file is the working history of the 2026 attempt,
including the parts that were wrong, which are marked rather than deleted.

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

| Page | Peak (10:00–20:00) | Off-peak | Secured? |
| --- | --- | --- | --- |
| Weekend Camping | 3–6 min | 8–14 min | yes |
| Weekend Camping Instalment Plan | 30–60 min | 60–90 min | yes |
| Early Entry Pass | **not searched** | — | no |

Peak load is 14.7 searches/hour and a day costs about 211, against a ceiling of
~20/hour. Run `python -m ep_watcher budget` for what is actually in force —
the numbers here are prose and the command computes.

### The Early Entry Pass is switched off

Off since 2026-08-20, on David's instruction, and off means genuinely
untouched: not searched, not swept, not alerted on, not held. The reasoning is
worth stating because it also says when to reverse it — **the weekend ticket
is the critical thing and he does not have one yet**, so every request the
watcher can spend should go to finding one. A pass is worth nothing on its
own; Ticketmaster's own note reads "Early Entry passes are only valid with a
Weekend Ticket".

**To turn it back on**, on the day there is a real ticket for it to sit beside:

```bash
echo 'export EP_EARLY_ENTRY=1' >> ~/.ep2026-watcher/env
./restart.sh
```

That is the whole procedure, and it restores **both** halves — the page is
searched again *and* a pass found on it is held, not just emailed about. The
two used to be separate settings and are deliberately tied to this one flag,
because a search that only ever sends an email is a switch that looks like it
worked and does half the job. Nothing about the pass has been deleted: its
cadence, its priority, its stop date and its history are all still in
`config.py` waiting. `tests/test_early_entry_switch.py` exercises the ON path
on every test run, so it stays known-good while it is unused.

Turning it on costs 2.7 searches/hour and takes peak load to 17.3 — still
under the line, and the test suite asserts that it stays there. The pass comes
back on a **15–30 minute** clock rather than the ticket's, because on the day
that switch is thrown he already has the important half. It was pinned to the
standard page's range while he considered the two equally important; leaving it
pinned would now add 13.3 searches/hour and take peak load to 28, so the one
switch he has been promised he can throw in a hurry would be the one that gets
him blocked.

#### What that parity cost while it was on

Between 2026-08-19 and 2026-08-20 the pass was searched exactly as hard as the
weekend ticket, and the bill was paid by the standard page. Three pages cannot
all be searched every three minutes:

| Both fast pages at | Peak searches/hour | |
| --- | --- | --- |
| 3 min mean | 42 | over the line |
| 4 min mean | 32 | over the line |
| 6 min mean | 22 | over the line |
| **7 min mean** | **18.5** | fits |

The ~20/hour ceiling is not arbitrary — it is what got the home connection
flagged during development. So the standard page slowed from a 4-minute mean
to 7, which on a ~4.6 minute listing lifetime takes the chance of catching one
from roughly 56% to about 45%.

**That slowdown was reversed on 2026-08-20**, when the pass was switched off
and gave the requests back. The standard page is on 3–6 minutes again.
It deliberately did not go to the ceiling: the watcher ran at 18.5/hour on the
19th and 20th and drew six blocks in two days, so the high teens are already
too warm whatever the documented line says. Every one of those blocks cleared
on the first fresh profile — the identity ages out, not the address — which is
why a block costs a poll cycle rather than the connection, and why spending
the headroom is worth doing at all. `EP_STANDARD_PEAK_MIN=300` and
`EP_STANDARD_PEAK_MAX=540` put it back to 5–9 if that proves too warm.

#### How this setting has moved

Four positions in three days, kept because the reasoning changed every time
and the next change deserves to know what the previous ones were for:

| | Searched | Held | Why |
| --- | --- | --- | --- |
| Added 2026-08-19 | yes | no | Holding an add-on pulls David to a checkout for something useless on its own |
| 2026-08-19 | yes | yes | "Treat it as importantly as the ticket" — priority, not exclusion, keeps a weekend listing safe |
| 2026-08-20 am | yes | no | Passes at €46.50 appear several times a day — five of the first eight finds — and each attempt spends a buying-browser cold start |
| **2026-08-20 pm** | **no** | **no** | The weekend ticket is critical and is not yet in hand; the pass should not be spending searches the weekend pages could use |

When it is on, the alert says plainly what the thing is:

```text
This is the EARLY ENTRY PASS — an ADD-ON for campsite access from 2pm
on the Thursday. It is NOT a festival ticket and is only valid alongside
a Weekend Ticket. It is worth nothing on its own.
```

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

It answers from the profile's cookie database and never opens a browser — so
it needs no network, costs nothing, and cannot get the buying profile
challenged for asking. `doctor` runs the same check on every run and warns
when the session has three days or less left.

**Why cookies rather than reading the page.** The obvious check is "does the
page say Sign Out". On 2026-08-19 that was tested against every page capture
the watcher has ever taken — nine of them — and **not one contains "sign
out", "my account", or even "sign in"**. Ticketmaster puts the account control
somewhere Playwright's flattened `inner_text` cannot reach. The check would
have answered "not signed in" for a perfectly good session, and the buyer
would have refused to act on the first real listing after you had signed in
correctly.

Cookie *presence* is not the answer either: the signed-out watcher profile
already carries 33 ticketmaster.ie cookies, every one analytics or consent.
What distinguishes a signed-in profile is **which** names are there — and the
only moment anyone can know that for certain is the moment a human says "I
have just signed in". So `login-buy` records the names it finds at that
moment, and every later check compares against that recording:

```text
  Signed in. Buying session saved to ~/.ep2026-watcher/chrome-profile-buy
  Recorded 2 account cookie(s) so the session can be
  checked later without opening a browser:
    · SESSION
    · identity.session
```

A guess made once, by a human looking at the real thing, beats a guess
hard-coded by someone who has never seen the page.

**And securing no longer refuses on it.** Since the sign-in state cannot
always be known, `secure()` notes what it thinks and tries anyway. A
signed-out attempt bounces off a login wall, holds nothing, and reports
honestly — the same outcome as refusing, without the chance of being wrong
about it. The availability alert has already gone out either way.

### What happens when a listing appears

1. The availability alert goes out **first**, always, before any securing is
   attempted. If everything below fails you are no worse off than before.
2. The buying browser opens, sets the quantity to 1, searches, and clicks the
   listing.
3. It presses only buttons on an allowlist — Continue, Next, Get tickets,
   Select — and refuses anything matching pay, buy, purchase, checkout,
   confirm order, or place order. The refusal checks **every** place a label
   can live, not just the visible text: see below.
4. It gives up after 45 seconds.
5. **If a basket appears**: a second, louder alert, and the window is left
   open and frontmost on the checkout page. You have roughly four minutes.
6. **If not**: an email that says plainly there is *no* hold, and names the
   step that failed.

#### A label is not always the text you can see

The denylist in front of the allowlist exists for one specific scenario: a
page labelling its payment control "Continue to payment", which the allowlist
would otherwise wave through on the strength of "continue".

Until 2026-08-20 that guard had a hole big enough to drive the scenario
through. Playwright matches `get_by_role(name=...)` against the **accessible
name**, and the check ran against `inner_text()` — which are different
strings. A button labelled only by `aria-label`, or `title`, or an
`<input type="submit">`'s `value`, has an accessible name and renders no text
at all. It matched the allowlist on its accessible name, arrived at the
forbidden check as the empty string, was forbidden by nothing, and got
clicked. The one guard between this code and a completed purchase could be
walked past by a button with no text in it.

It now collects every label a control carries — inner text, `aria-label`,
`title`, `value` — and refuses if **any** of them is a payment word. A button
whose label cannot be read from any source is refused too, on the grounds that
the allowlist matched it on *something*, and if we cannot see what, that is a
reason to stop rather than to proceed. Skipping a real "Continue" costs a hold
you could still make by hand; pressing an unread control is how software
spends your money.

There is one choke point for this in the whole module — `secure()` makes
exactly three clicks: the search button, the listing row, and this guard —
which is what makes the property checkable at all.

### The weekend ticket always wins the browser

There is one buying browser and one account, so two listings cannot be held at
once — and on 2026-08-19 the Early Entry Pass appeared four times against four
weekend sightings, so the collision is an ordinary afternoon rather than a
corner case.

David's rule: **the weekend ticket is always priority, but try to get the
early one as well.** Both halves are implemented.

This describes the arrangement **when the pass is switched on**. It is off
today (see above), so nothing below is currently in play — it is kept because
the switch is meant to be flipped back, and this is what happens when it is.

- The pass is watched, alerted on, and secured whenever the buying browser is
  free. It is worth having.
- A weekend ticket outranks it, and outranking is real: a weekend find will
  **close the browser on a held pass** and go for the ticket instead.
- The pass may never do the reverse, and neither weekend page may evict the
  other — they rank equally, because either is the real ticket.
- The most important page is also polled first, so a cycle cut short by a 403
  never skips the weekend ticket.

The reason precedence is worth the complexity: Ticketmaster only honours an
Early Entry pass alongside a Weekend Ticket. Holding one while a weekend
ticket goes past spends the single browser on the single product that is
useless on its own — the worst outcome available.

The cost is real and is stated in the alerts rather than hidden: preempting
drops a hold that was certain for one that may already be gone. Both the
"held" email and the "could not hold it" email say plainly that the pass was
let go, and why, so finding out never means walking to a laptop that is
showing something different. If the swap fails, the record stops claiming
anything is held.

Set `EP_PRIORITY_WEEKEND` and `EP_PRIORITY_ADDON` to change the ranking.

### Nothing may restart the watcher while a ticket is held

A basket lives in the browser the watcher launched, so anything that kills the
watcher process throws the ticket away. That is not a hypothetical: two
separate paths did exactly that, and both were found by reading the code
rather than by losing a ticket.

- **The watchdog.** It restarts a watcher whose poll clock has stopped
  advancing, which is right in every case but one — a watcher paused for a
  checkout looks identical to a wedged one. The watcher used to print
  "Reserve accepted — pausing the loop so you can check out" and then sleep
  without writing anything down, so fifteen minutes later `launchctl
  kickstart -k` would have killed the checkout it was protecting.
- **`restart.sh`.** It cleared stale Chromes with
  `pkill -f "ep2026-watcher/chrome-profile"`, and `pkill -f` matches
  substrings — so it also matched `chrome-profile-buy`. The repair for a
  wedged watcher was also the way to destroy a live hold, and `doctor` prints
  `restart.sh` as the fix for half its failure lines.

Now a live hold is written into `state.json` as `hold_until`; the watchdog
reads it and stands down, `doctor` and `status` report it, and `restart.sh`
leaves the buying browser alone and says that it has. The protection is
**bounded** — the hold plus `EP_HOLD_PAUSE_EXTRA` minutes — because a hold
nobody completes must not silence the watch for the rest of the fortnight.
Ambiguous silence is the one thing this project refuses.

A second find while the first is still held is refused rather than collided
with: Chrome locks the profile directory, so the attempt would otherwise fail
with a message about singleton locks that says nothing about a ticket. The
alert says the buying browser is already open and what to do about it.

### The hold probably cannot travel — but the email offers the link anyway

A Ticketmaster basket lives in the session cookies of the browser that created
it, so opening a link on your phone may get you a different session and an
empty basket while the hold expires on the Mac. That is why the buyer runs on
the machine you will finish payment on.

This section used to end "and the 'held' email contains **no link** — only an
instruction to go to that Mac." That was reversed on 2026-08-19 and the email
now carries the checkout URL, framed as worth trying rather than as the
answer. The reasoning is certainly right for a signed-OUT session and may be
wrong for a signed-in one, where the cart could be bound to the account
server-side and follow you to any device you are signed in on. Nobody has
tested which applies here, and the error is asymmetric: offering a link that
does not work costs a glance at an empty basket, while withholding one that
would have worked costs the ticket every time you are out of the house.

### What is proven and what is not

Tested offline, no network: it will not press a payment button; it will not
claim a hold it cannot see on the page; the availability alert fires
regardless; the failure email always sends; it stays off until enabled; it
refuses to act on an event it was not given.

**Partly proven since:** on 2026-08-19 a real listing page and a real
mid-hold checkout page were captured on another event using the same
interface. Those settled three things that had been guesses — the listing
detail screen ("Ticket type" / "Section"), the dead end you reach when a
listing has gone ("these tickets are unavailable"), and what a live basket
actually says, which is "Place Order" and "Cancel Order" and **none** of the
three phrases previously guessed at. That last one mattered most: a
successful hold would have been reported as a failure.

**Now proven, and it changes the answer:** the click-through works. The
attempt of 2026-08-24 at 10:07 reached the listing's own page and clicked
into it **5.3 seconds** after the sweep saw it — 0.08s to set the quantity,
0.73s to search, 3.48s for the panel, 0.02s to find the row, 0.96s to click.
It was refused anyway.

---

### The reason nothing was ever held: we were asking for zero tickets

Settled on 2026-08-24 by recording what the page actually does. Across four
listings and **eighteen requests without a single exception**, clicking a
resale row made the browser ask:

```http
GET https://secure.ticketmaster.ie/{eventId}/{listingId}?qty=0
    -> 302 -> /error/q404
```

`qty=0`. Ticketmaster redirects a zero-quantity offer to the same "sold or
removed" screen that a genuinely gone listing produces — so **every securing
attempt this project has ever made asked for no tickets, was refused for it,
and recorded the refusal as somebody else having taken the listing.**

That one field explains everything and retires both standing theories:

- Ten distinct listings refused, every one `"active": true` — of course they
  were. Nothing was ever wrong with the tickets.
- One clicked **5.3 seconds** after the sweep saw it, refused identically —
  of course it was. Speed cannot rescue a malformed request.
- It was never a lost race, and never somebody else's basket.

The page builds that link from its own quantity state, and the stepper has to
be driven with arrow keys because an overlay eats real clicks on it. So the
resale *search* goes out as `qty=1` while the *offer* link is built from state
that never left zero.

The fix is not to fight that state but to stop depending on it. The attempt
now goes straight to the offer URL, built from two things the resale feed
already hands us:

- the **event id**, already parsed out of the event URL, and
- the **listing id** — which is also what the feed's `offerIds` contain. They
  are not opaque: base32-decoded, every one observed is `9|{resaleListingId}`
  (`HF6GYMRXOQ2GQMTE` → `9|l27t4h2d`). In all eighteen traced requests the
  listing segment of the URL equalled exactly that.

Skipping the quantity, the search and the panel wait saves about twenty of an
attempt's twenty-two seconds — which now looks like the *smaller* half of the
benefit. `EP_DIRECT_OFFER=0` reverts to clicking the row.

The old search path stays underneath as the fallback, and the attempt only
carries on down the direct one with **positive** evidence of where it landed —
a basket, or the listing's own page. A refusal screen or a page it cannot
identify falls back, because "the URL loaded" is not "the URL worked".

**Still unproven:** that `qty=1` completes. No listing has been live since the
change. The evidence that it is the right request is strong — it is the page's
own URL with the one field corrected — but treat the next find as the test.

### The test ran, and `qty=1` is refused too

Answered the same evening, 2026-08-24, by thirty-one requests across nine
listings. **Every one was refused identically.**

```
?qty=0   18 requests   0 succeeded   302 -> /error/q404
?qty=1   31 requests   0 succeeded   302 -> /error/q404
```

Taken together with everything before it, the checkout URL has now been asked
**49 times and has never once returned an HTTP 200**, on any listing, on any
day, at either quantity.

> **Superseded on 2026-08-26.** It does return 200 — twice, both on the
> instalment page, at 15:27 and 23:53 on the 25th. The sentence above was true
> when written and stopped being true the next day, which is exactly the
> pattern the rest of this section is about. See
> [The URL works](#the-url-works-we-reached-a-checkout-and-did-not-notice).

The quantity was a real bug and fixing it was right. It was not *the* bug, and
the section above is left standing rather than edited because the way it was
wrong matters more than that it was wrong. It is the third confident diagnosis
in a row — "we lost the race", then "it sat in somebody's basket", then "we
asked for zero" — each written into the code as settled fact, each overturned
by the following day's logs.

The reason that keeps happening is structural: **this project has never
observed a single successful checkout.** Every belief it holds about how
buying works is reverse-engineered from failures, and there is no positive
control anywhere in the data to check any of it against.

Four causes are ruled out by evidence. It is not a lost race — the refusal
arrives in a fifth of a second while the listing stays in the feed for another
ten minutes. It is not a signed-out session — `check-buy` reports the cookies
intact, and in one capture `identity.ticketmaster.ie` bounced straight back
with a 302, which only happens when already authenticated. It is not a
malformed URL — the offer id decodes and re-encodes exactly. And it is not a
sold ticket — the error payload says `"active": true` at the moment of
refusal.

What remains is one of: the buying browser is flagged and `q404` is a polite
refusal; the URL is no longer the live entry point and the real flow goes
through `checkout.ticketmaster.ie/graphql`, which the traces show the page
talking to; or a listing-level rule this cannot see. **`python -m ep_watcher
probe-offer` separates all three** — see [Commands](#commands).

> **All three were wrong.** `probe-offer` showed a clean signed-out browser
> refused identically, which retired the flagging theory; driving the page's
> full checkout session first changed nothing, which retired the session
> theory; and then the URL simply worked. The answer was none of the above —
> see below.

### Ten of fourteen retries never left the machine

Found in the same review, and it makes most of what the chase recorded
fictional.

Ticketmaster's 302 to `/error/q404` is cacheable, so Chrome caches it. On
listing `l0vmtvwkd2`, fourteen visits produced **four** distinct Ticketmaster
error ids; the other ten were replays from disk, returning in **1–2
milliseconds** — not a possible round trip to Dublin, where the fastest real
answer measured 120ms.

That is worse than wasted time. A retry that cannot observe a change makes the
chase logically incapable of succeeding, and every identical "still refused"
it logged was then read as evidence the listing was still held — evidence the
browser manufactured by not asking.

Fixed by putting a nonce on RETRY navigations only, and by recording any
sub-50ms reply as a replay rather than counting it as a refusal.

**The first fix for this was worse than the bug, and is worth recording.** It
set `Cache-Control: no-cache` with `page.set_extra_http_headers`, and its
docstring said it "affects nothing else". That is not what the call does: it
is sticky for the life of the page, so it put no-cache on *every* subsequent
request the buying browser made — the parked event page, reloaded uncached
each time, and every poll of `/api/quickpicks/…/resale`, which is rate-limited
and answers 403 when pushed. On the morning of 2026-08-25 the second attempt
of the 10:10 chase never returned at all: the worker sat inside it for 390
seconds, past the ceiling meant to bound it, and the next listing was refused
with "the browser was busy".

So the scope has to be the request, not the page. A nonce is the only thing
that is genuinely per-navigation — it changes the cache key for one URL,
touches nothing else, cannot be left switched on, and has no browser round
trip to hang in. Attempt one still goes out as the exact URL Ticketmaster's
own page builds, with no parameter of ours in it; the unknown-parameter risk
is only ever taken on a request that would otherwise be a cache replay, and
therefore guaranteed to tell us nothing. `EP_OFFER_NO_CACHE=0` reverts.

### The chase is two minutes now, not twelve

`EP_SECURE_ACTIVE_TIMEOUT` went from 720 to 120, and `EP_SECURE_ACTIVE_RETRIES`
from 10 to 3, on 2026-08-24.

The twelve-minute window existed to outlast a basket hold. The evidence says
there is no basket to outlast: a hold would show the listing dropping out of
the feed and returning, and across fourteen visits `l0vmtvwkd2` never left.
Ten chases ran the full window that day and none converted.

What it cost was **122 minutes of buying-browser time in one day** — a fifth
of it — plus six deferred watchdog restarts, because the watchdog rightly will
not kill a chase it cannot distinguish from a hang. Three real, cache-busted
attempts answer the only question this loop can currently answer: whether a
refusal is transient or systematic.

### A run of refusals now raises its own alarm

Five live listings refused in a row sends one loud alert saying the buying
path is broken, separately from the per-attempt failure emails.

This is a reporting fix, not a buying one, and it is the one that would have
saved the most time. The per-attempt emails arrived faithfully and truthfully
for five days while 65 attempts produced 65 refusals, and nothing in the
system ever said the sentence that mattered — that all of them were the same
failure. A pattern is a different fact from any of its instances, and no
per-listing message can state it however well written it is.

Distinct from the block alert on purpose: a block is Ticketmaster asking for
less traffic, and resting is the cooperative answer. This is something wrong
at our end, which does not clear on its own, and the useful response is to
tell David to buy by hand.

### The alerts stopped shouting about tickets he has already seen

On 2026-08-24 the watcher sent **69 pushes for 16 listings** — roughly four
apiece, every one urgent, identically titled, and ringing the phone.

An alert that arrives four times is not four times as loud; it teaches its
reader to ignore it. That matters more here than it would anywhere else,
because with the buyer converting nothing the plan now rests on David seeing
an alert and buying by hand. The notification *is* the product.

So a listing he has not been told about keeps everything: urgent priority, the
ring, `NEW` first in the title. A repeat says `still there` first, drops to
default priority, and does not ring — while still carrying the link, because a
quiet reminder he happens to see is still a ticket he can buy.

### The URL works. We reached a checkout and did not notice

At **00:53 on 2026-08-26**, listing `ljw94z59` on the instalment page answered
`secure.ticketmaster.ie/{event}/{listing}?qty=1` with **HTTP 200**. No 302, no
`q404`. The page title was *Checkout | Electric Picnic 2026 - Weekend Camping
Instalment Plan | Ticketmaster*, and it carried the ticket type, the section,
a cost breakdown of €310.50 plus €55.89 service charge, a **Total to Pay of
€366.39**, name/email/phone fields, and this warning:

> Proceed to payment to reserve these tickets

The watcher was standing on a live checkout, signed in as David. It recorded
*"no basket appeared"*, emailed him to say it could not hold the ticket, and
moved on.

**The cause was four words.** `BASKET_MARKERS` was looking for `proceed to
checkout` and `your tickets are reserved`. The real page says **SECURE
CHECKOUT**, **Proceed to payment** and **Total to Pay**. Every marker in that
list had been *inferred*, never measured, because until 00:53 there had never
been a checkout page to measure. The observed vocabulary is now in the list
and tagged as observed rather than guessed.

This is the most expensive near-miss in the project, and it retired three
theories at once — see the corrections above.

### It is the instalment page that works

The reach rate is not uniform, and the split is the most useful number here:

| Page | Offer requests | Reached checkout |
| --- | --- | --- |
| Weekend Camping (standard) | 149 | **0** — 0.0% |
| Weekend Camping **Instalment** | 81 | **2** — 2.5% |

149 consecutive refusals on one page and two successes on the other is not a
small sample on the losing side. The likeliest reading is competition: fewer
people chase a pay-in-stages listing, so we are not always beaten to it. Both
checkouts were on the instalment page, and one was at **15:27 in the
afternoon** — so it is not a quiet-hours effect, it is a which-page effect.

Worth stating plainly because the instalment plan is the same festival, the
same weekend and the same camping ticket. The captured checkout showed a
Total to Pay of €366.39, which is the same money as the standard listing.

### Pressing the one button that reserves

The checkout page has exactly one forward control, labelled **Continue To
Payment**, and the stepper above it reads *1 Your Order → 2 Payment → 3
Confirmation*. There is no hold, no basket and no reserve control. Beside that
button Ticketmaster writes "Proceed to payment to reserve these tickets", so
**that button is the reservation step**.

Until 2026-08-26 the watcher would not touch it, because `FORBIDDEN_BUTTONS`
blocks anything containing "pay". That rule was right while nobody knew what
the control did, and wrong once the captured page said so in its own words: it
left a ticket the watcher had already reached sitting there, takeable by
anyone, until David got to a laptop.

David authorised it having been told what it costs — it agrees to the Ticket
Exchange Policy on his behalf and moves the browser onto the payment screen,
one step closer to a purchase than this project had ever gone. It is the same
scope he set on 2026-08-19: secure it, then hand off for payment.

**Three independent things keep it inside the line.** It presses one control,
matched on its **exact** accessible name — never a prefix or substring. After
pressing it returns, so there is no loop and nothing else is ever pressed. And
`NEVER_PRESS` (`pay now`, `place order`, `confirm`, `complete purchase`) is
checked against the button's own labels first, so a page that relabels its
final purchase control "Continue to payment" cannot get through the exact
match either. **Card details are never entered.**

`EP_RESERVE_AT_CHECKOUT=0` returns to stopping at the order page.

The alert distinguishes the two states, because they demand different speed:
**RESERVED — JUST PAY** when the step was taken, and **CHECKOUT OPEN — PAY NOW
TO GET IT** when it was not and the ticket is still winnable by anyone.

### Reading Fastly's edge is reading the past

From David's own network capture on 2026-08-25. His signed-in browser called
`/api/quickpicks/{event}/resale` and got back:

```http
x-cache: HIT, MISS, MISS, MISS
age: 13
cache-control: max-age=15, stale-if-error=3600, stale-while-revalidate=30
x-served-by: cache-dub4373-DUB
```

**`age: 13`.** The answer had been sitting at the Dublin edge for thirteen
seconds. With `stale-while-revalidate=30` on top of `max-age=15`, a listing can
exist for **up to forty-five seconds** before any edge copy mentions it.

No cadence closes that gap — asking the same URL ten times in ten seconds
returns the same stale object ten times. It is also the best explanation for
listings that arrive already half-dead, and for 70 of the first 75 being seen
exactly once: part of that window was spent before we could possibly have seen
them.

So the sweep put a nonce on the URL, meaning to miss the edge and force
origin to answer.

> **It does not work, measured 2026-08-26.** Asked from a real browser, back
> to back:
>
> ```
> plain        200  age 18  x-cache HIT
> &_=<ms>      200  age 18  x-cache HIT    <- same cached object
> &epcb=<ms>   200  age 18  x-cache HIT
> limit=21     200  age 18  x-cache HIT    <- a different REQUEST
> ```
>
> Fastly keys this route on the **path** and ignores the query string, so a
> novel URL is not novel to it — even asking for a different `limit` is
> answered from the same copy. There is no parameter that reaches origin.
>
> The diagnosis was right and the remedy was wrong. The edge answer really is
> up to 45 seconds old and nothing sendable from a browser makes it fresher.
> That floor applies to everyone reading this endpoint, including whoever else
> is racing for the listing, which is the only consolation on offer.
> `EP_EDGE_BYPASS` now defaults to **0** — switched off rather than deleted,
> because an unknown parameter on a rate-limited endpoint is a cost with
> nothing bought.

### The final-48-hours cadence

Set on 2026-08-26 at David's request, with two days to the gates.

| Setting | Value | Why |
| --- | --- | --- |
| `EP_RESALE_SWEEP_SECONDS` | `45` | Chosen against the **backoff**, not the budget: three 403s double the interval, so a 45s base degrades to 90s — exactly the old base. Worst case is no worse than before. 25s was tried and drew three refusals in two minutes plus a block, so this is half the rate known to break. |
| `EP_NIGHT_POLL_SECONDS` | `0` | Removes the 30-minute overnight floor between 00:00 and 06:00 UTC. Fourteen listings appeared between 19:00 and midnight on the 25th, and one of the only two checkouts ever reached was at **00:53** — inside the window that brake was slowing. |
| `EP_*_OFFPEAK_MIN/MAX` | `300`/`560` | Offpeak set equal to peak, so the day rate is sustained around the clock. This lifts the trough without raising the **peak** hourly rate, which is the number that has drawn blocks. |

Searches themselves were **not** sped up: they sit at 16.7/hour against a
20/hour ceiling, and the watcher was blocked once while running below that
rate. The sweep is the cheap lever and that is where the speed went.

### What the 45s cadence actually cost

Measured on 2026-08-26, because the argument for 45s was a prediction and
predictions in this project have not aged well.

The first backoff came **22 minutes** after the change, and the sweep degraded
to 90s — exactly the floor the choice was made against. It recovered to 45s
after 20 clean answers, 52 minutes later.

| Base | Observed pattern | Effective interval |
| --- | --- | --- |
| 90s | 4 backoffs in 12h — ~10h at 90s, ~2h at 180s | **~105s** |
| 45s | ~22 min clean, then ~30 min at 90s | **~71s** |

So the sweep looks about a third more often than it did, while never being
slower than the previous base. That is the whole reason 45s was picked against
the **backoff multiplier** rather than against the request budget: three 403s
double the interval, so the downside case is bounded at the old rate.

The monitoring changed with it. Firing an alarm on the first backoff was the
wrong signal — at 45s a backoff is expected and self-healing. What matters is
the rate, so the watch now fires only if four or more land inside an hour,
which would mean the sweep is degraded more than it is fast and 45s is above
the ceiling after all.

### It was never a race, and the refusal page says so

The single most useful thing this project has found, and it was sitting
unread in a field the code already captured.

A refused click lands on `secure.ticketmaster.ie/error/q404?cid=…&ctx=…`.
That `ctx` is a URL-encoded, gzipped JSON document, and it is Ticketmaster's
own record of the listing it has just refused:

```json
{"listing": {"urlId": "lw09yvzt", "active": true, "sellPrice": 310.5,
             "offerType": "Three+ Presale Ticket", "section": "STNDNG",
             "row": "GA6", "buyerFeeValue": 55.89}}
```

`active: true`. **Every one of the fifteen refusals captured between
2026-08-21 and 2026-08-24 carries it**, including the 5.3-second attempt
above. Those tickets had not sold. They existed, they were live, and
Ticketmaster refused to sell them to us anyway — which means somebody else
was holding them in a basket.

Three conclusions follow, and the first two are corrections:

- **"The race being lost at the last step" was the wrong verdict** on most of
  the tickets it was written for. It is the single most common reason in the
  log, and it pointed a fortnight of work at shaving seconds off a click that
  was already fast enough. Five seconds was not enough because speed was not
  what refused us.
- **A listing vanishing from the resale feed is not proof that it sold.** The
  feed answers "is this offerable to me right now", and a ticket in somebody
  else's basket is not — so it drops out while remaining perfectly for sale.
  The old give-up rule read that as "it sold" and abandoned the chase after
  two or three goes, on tickets Ticketmaster's own payload called active.
- **Waiting is the winning move, not hurrying.** Baskets lapse, usually
  inside ten minutes. The buyer now chases a listing the error page calls
  active for up to `EP_SECURE_ACTIVE_TIMEOUT` (12 min) across
  `EP_SECURE_ACTIVE_RETRIES` (10) goes, instead of the 5 minutes and 6 goes
  it gets for a listing it merely still sees in the feed.

Reading it costs nothing: no request, no race, and — critically — no origin
problem. The dead end is served from `secure.ticketmaster.ie` while the resale
endpoint is same-origin to `www.ticketmaster.ie`, so at the exact moment the
question matters most, the feed **cannot be asked at all**. The answer was in
the URL bar the whole time.

### The chase watches the feed, it does not hammer the search

Ten extra goes at a full attempt would be about 55 searches an hour against a
budget of 16.7 that is already deliberately under the 20 that first drew a
block — and the block screen causes half of all refusals. Chasing a live
ticket that way would manufacture the very thing that loses them.

So the pause between goes watches the resale endpoint instead: one
same-origin XHR every `EP_SECURE_RELIST_POLL` (10s), the identical call the
sweep already makes every ninety seconds, from a page already open. A whole
chase costs one page load and a handful of XHRs per pause, and the expensive
attempt is spent only when the feed says there is something to spend it on.
It is also faster at the thing that matters: a basket that lapses one second
into a flat forty-second sleep used to go unnoticed for thirty-nine of them.

### The offer types are worth watching

Recorded on every `hold` event now, because the refusals are not evenly
spread across them:

| `offerType` | Refusals |
| --- | --- |
| `Three+ Presale Ticket` | 6 |
| `General Admission Tier 2 - 3rd and Final Payment .BO` | 6 |
| `General Admission Tier 2 Ticket` | 3 |

Two of those three are not ordinary single tickets — one is a group presale,
the other an instalment plan mid-payment. It is worth knowing whether a type
is *never* honoured at quantity 1, because that would be a listing the feed
advertises and the offer flow will not sell, and no amount of chasing wins
it. There is not enough data to say yet; the field is now in the event log so
that the next few refusals can settle it.

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

### The logs rotate themselves, by copying rather than renaming

Nothing was ever going to stop `watcher.log` growing. launchd appends to
`StandardOutPath` for the whole life of the job, macOS's `newsyslog` does not
manage files under `~/`, and the watcher only prints. It reached 2 MB in the
first eight days. That log is the first thing you open when something has gone
quiet, so an unbounded one is a diagnostic problem well before it is a disk
problem.

The watchdog now rotates anything in the log directory over 5 MB
(`EP_LOG_MAX_BYTES`), keeping one previous generation as `.1`.

It **copies and truncates**; it never renames. This matters more than it
sounds. launchd holds an open descriptor on the log's inode, so `mv
watcher.log watcher.log.1` takes the live log away with it: every line the
watcher prints afterwards lands in a file nobody is tailing, while
`watcher.log` sits at zero bytes looking exactly like a watcher that has died.
Truncating in place keeps the inode, so the open descriptor carries on writing
and only the history moves. [tests/test_log_rotation.py](tests/test_log_rotation.py)
holds a descriptor open across a rotation and checks the next line lands in
the file you are reading.

### What is backed up, and what a backup cannot do

Four things the watcher needs are deliberately outside git, and were therefore
in exactly one place each:

| | Losing it costs |
| --- | --- |
| `~/.ep2026-watcher/env` | The Gmail app password. The watcher refuses to start without it |
| `chrome-profile-buy/` | The signed-in session — a human at a keyboard doing `login-buy` |
| `state.json` | What has been alerted on, block history per connection, any live hold |
| `buy-session.json` | The sign-in fingerprint that makes "still signed in?" exact rather than a guess |

`python -m ep_watcher backup` copies all four into a timestamped, `chmod 700`
snapshot under `~/.ep2026-watcher-backups/` — deliberately *outside* the
directory it protects, because the likeliest way to lose the originals is a
command aimed at that directory. The watch loop takes one a day by itself, in
the sleep window between polls, so it happens whether or not anyone remembers.
Seven snapshots are kept (`EP_BACKUP_KEEP`).

Only the session-carrying parts of the Chrome profile are copied — `Cookies`,
`Local State`, `Local Storage`, `Preferences`. The profile on disk is 161 MB
and essentially all of that is `Cache` and `Code Cache`, which Chrome rebuilds
on demand; the snapshot is about 130 KB.

**The limitation, stated plainly, because a backup you trust wrongly is worse
than none:** on macOS Chrome keeps the key that decrypts its cookies in the
login Keychain, not in the profile. These snapshots restore on *this* Mac,
under *this* user, and nowhere else. That covers a bad profile reset, a
mistaken `rm`, or a corrupted state file — which is what it is for. It is not
a way to move the buying session to another machine.

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
every night between midnight and six.

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
| `probe-offer` | Ask one live checkout URL from **two** browsers — a copy of the signed-in buying profile, and a clean signed-out one — and compare. Two page loads; never sets a quantity, clicks a listing, baskets or pays. Needs a live listing, and finds one itself |
| `calibrate` | Dump screenshot + text + HTML after a search |
| `networks` | List every connection the watcher has seen, with blocks against each |
| `status` | Print config and health, including the peak request rate |
| `budget` | What this cadence actually spends, hour by hour, against the rate that drew a block. Non-zero exit if over |
| `backup` | Copy the four things that live outside git and cannot be recreated |
| `resolve-id` | Look up the Discovery event id for the API source |

---

## Configuration

> **Nothing is assumed delivered.** Every alert reports whether it actually
> reached you, and the clocks that stop it repeating are only started when it
> did. If the email and the push both fail — which is exactly what happens when
> the fault is your network — the watcher keeps trying on the next poll instead
> of going quiet for six hours. See "When the alert itself cannot get out".

> **Armed but signed out is reported hourly, not just at startup.** The
> startup banner says whether the buying profile is signed in, which is no
> help on day nine of a fortnight — cookies lapse, and the banner that would
> have mentioned it scrolled off the log a week earlier. The hourly report now
> asks the same question of the same evidence, and takes over the subject line
> when the answer is no, because a securing feature that cannot work otherwise
> announces itself at the single worst moment: with a real listing on screen
> and ninety seconds to act. It speaks only on a **definite** signed-out
> reading; "cannot tell" stays quiet, because a warning in every email is one
> that stops being read.

Environment variables, all optional:

| Variable | Default | Notes |
| --- | --- | --- |
| `WANTED_QUANTITIES` | `1` | Quantities to search per poll |
| `EP_PEAK_START_HOUR` | `10` | Start of the window listings actually appear in, local time |
| `EP_PEAK_END_HOUR` | `20` | End of it. Outside this the same budget is spent more slowly |
| `EP_STANDARD_PEAK_MIN` | `180` | Shortest gap between peak searches of the standard page. The gap is drawn fresh from the range after each search, so the traffic is not a metronome |
| `EP_STANDARD_PEAK_MAX` | `360` | Longest |
| `EP_STANDARD_OFFPEAK_MIN` | `480` | The same, outside the peak window |
| `EP_STANDARD_OFFPEAK_MAX` | `840` | |
| `EP_EARLY_ENTRY` | `0` | **The Early Entry Pass switch.** `1` searches the page again *and* holds a pass found on it. Off since 2026-08-20 so the whole budget goes to the weekend ticket — turn it on once there is a real ticket for a pass to sit beside |
| `EP_EARLY_PEAK_MIN` / `_MAX` | `900` / `1800` | The Early Entry Pass's cadence when `EP_EARLY_ENTRY=1`. Slower than the ticket's on purpose — see above |
| `EP_EARLY_OFFPEAK_MIN` / `_MAX` | `1800` / `3600` | |
| `EP_INSTALMENT_PEAK_MIN` / `_MAX` | `1800` / `3600` | The instalment plan. One of nine sightings was here, so it keeps a small share |
| `EP_INSTALMENT_OFFPEAK_MIN` / `_MAX` | `3600` / `5400` | |
| `EP_BLOCK_RATE_PER_HOUR` | `20` | The rate that drew a 403 in development. `budget` and the test suite both check against it |
| `EP_LOOP_TICK_SECONDS` | `45` | Ceiling on how often the loop wakes to ask if a page is due. Costs no requests. Must stay well under the shortest gap any page can draw, or it quantises the cadence upward |
| `EP_RESALE_SWEEP` | `1` | The cheap resale check between searches — one same-origin XHR from the page already open. Every weekend listing found so far was found by this rather than by a search |
| `EP_RESALE_SWEEP_SECONDS` | `45` | How often it asks, per swept page. The endpoint's own `cache-control` says `max-age=15`, so a 45s sweep is close to the freshest answer available. Chosen against the backoff: three 403s double the interval, so 45s degrades to 90s — the previous base |
| `EP_EDGE_BYPASS` | `1` | Put a nonce on the resale call so Fastly cannot answer it and origin must. Removes up to 45s of edge staleness; costs an origin hit per call. `0` reads the edge |
| `EP_RESERVE_AT_CHECKOUT` | `1` | Press **Continue To Payment**, the step that reserves a resale ticket, and stop dead on the card screen. Never enters card details, never confirms an order. `0` stops at the order page |
| `EP_RESALE_SWEEP_MAX` | `240` | The slowest it may become after refusals. Was `600`, which was slower than the 180–360s peak search it exists to beat — a detector that had quietly become the slowest thing in the system |
| `EP_RESALE_SWEEP_RECOVER_AFTER` | `20` | Clean answers that win the speed back, halving the interval. Without this the ladder only went down: three refusal bursts overnight on 2026-08-20 left the sweep at ten-minute intervals for the morning, and only a restart undid it |
| `EP_SWEEP_INSTALMENT` | `0` | Include the instalment page in the sweep. Off since 2026-08-21: refusals scale with how many pages are swept, and halving that buys latency on the page that matters. The instalment page is still searched, alerted on and secured as normal |
| `EP_SECURE_ON_FIND` | `0` | Set `1` to let the buying browser hold a resale listing. Needs `login-buy` first |
| `EP_SECURE_TIMEOUT_SECONDS` | `300` | Seconds to spend trying to secure before giving up |
| `EP_SECURE_ACTIVE_TIMEOUT` | `720` | The longer window used when Ticketmaster's own refusal page says the listing is still `active` — i.e. it did not sell, somebody is holding it, and a basket lapse is a real thing to wait for. Twelve minutes because a Ticketmaster basket holds for about ten, so anything shorter cannot see the event it is waiting for |
| `EP_SECURE_ACTIVE_RETRIES` | `10` | Goes at a listing the refusal page calls active, against `EP_SECURE_RETRIES` for one the feed merely still shows. Not unlimited: if a basket has not lapsed in eight minutes, the buyer behind it is paying rather than dithering |
| `EP_DIRECT_OFFER` | `1` | Go straight to `secure.ticketmaster.ie/{eventId}/{listingId}?qty=1` instead of clicking the resale row. On by default because clicking the row is what lost every ticket: the page built that same link with `qty=0` on all eighteen requests ever traced, and Ticketmaster 302s a zero-quantity offer to the "sold or removed" screen. Set `0` to go back to clicking |
| `EP_SECURE_RELIST_POLL` | `10` | Seconds between resale-feed checks while waiting out a basket. The pause watches the endpoint rather than the clock — one XHR a look instead of a whole search a retry, which is what keeps a ten-go chase from becoming the ~55 searches/hour that draws a block |
| `EP_SECURE_MIN_INTERVAL` | `60` | Shortest gap between two securing attempts on one page. Separate from the alerting re-nag on purpose: a repeat email is noise, a repeat attempt is the job. Before this they shared a clock, and on 2026-08-20 stock visible at 20:04 and 20:06 drew no attempt because David had been emailed at 20:02 |
| `EP_HOLD_PAUSE_EXTRA` | `10` | Minutes added to the hold window during which nothing will restart the watcher |
| `EP_PRIORITY_WEEKEND` | `100` | Securing precedence of the two weekend pages. Higher wins the buying browser |
| `EP_PRIORITY_ADDON` | `10` | Securing precedence of the Early Entry Pass. Lower, so a weekend ticket preempts a held pass |
| `EP_MAC_SILENT_RENAG_HOURS` | `6` | How often the GitHub backstop may repeat "your Mac has gone quiet" about the same silence |
| `EP_LIVENESS_COOLDOWN` | `30` | Minutes to stop publishing the heartbeat after ntfy answers 429 |
| `EP_NTFY_DAILY_LIMIT` | `250` | ntfy.sh's anonymous daily message allowance, per IP. `doctor` reports the day's usage against it |
| `EP_NTFY_ALERT_RESERVE` | `80` | Messages held back for real alerts. The heartbeat stops publishing once fewer than this remain |
| `EP_LIVENESS_MINUTES` | `10` | How often the Mac publishes its "still alive" beacon. Throttled because it shares an ntfy quota with the alert that matters |
| `EP_BUY_PROFILE_DIR` | `~/.ep2026-watcher/chrome-profile-buy` | Where the signed-in buying profile lives |
| `EP_POLL_SECONDS` | `300` | Fallback per-page gap for a page configured without a range |
| `EP_WATCH_LABEL` | `Electric Picnic 2026` | What to call the watch in emails covering every page |
| `EP_HEARTBEAT_HOURS` | `1` | How often to send the "still nothing" report |
| `EP_NIGHT_POLL_SECONDS` | `0` | Overnight cycle floor. Set to `0` for the final 48 hours so the night runs at the day rate |
| `EP_NIGHT_START_HOUR` | `0` | When the overnight slowdown begins, local time |
| `EP_NIGHT_END_HOUR` | `6` | When it ends. Was `7` until 2026-08-21, when a real weekend listing appeared at 06:57 local — inside the window, with the searches at half-hourly and only the sweep looking |
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
  Next change               : 06:00 local (or the first poll after), back to daytime

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

There is also a plain runner, which is what CI uses:

```bash
./run_tests.sh              # every test file, one PASS/FAIL line each
./run_tests.sh resale       # only the ones whose name matches
```

It sandboxes `EP_STATE_FILE`, `EP_DIAG_DIR` and `EP_LOG_DIR` into a temporary
directory, so running the tests can never disturb a watcher that is working.
`.github/workflows/tests.yml` runs it on every push — until 2026-08-19 nothing
ran these at all unless somebody remembered to.

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
- **The cadence is per page, randomised, and split into three windows.** Do
  not read the number off this page — it has been wrong twice. Run:

  ```bash
  ./run_watcher.sh budget
  ```

  which prints the real hourly rate for whatever the settings currently are,
  hour by hour, and exits non-zero if the busiest hour is over the line. As of
  2026-08-19 that is **18.5 searches an hour at peak, 293 a day**, against the
  20/hour that drew the block. The three earlier attempts to state this figure
  in prose — 12/hour here, "~15.3" and "~17" in `config.py` — were each
  accurate when written and none survived the next cadence change, which is
  why the number now comes from a command and is asserted by
  `tests/test_request_budget.py`.

  The shape is deliberate. Each page draws its next gap fresh from a range
  rather than ticking on a fixed interval, because a metronome is itself a bot
  signature. The peak window (10:00–20:00 local, where all eight recorded
  sightings fell) is faster and the rest of the day is correspondingly slower,
  so the day's total is redistributed rather than increased.
- **Overnight it used to drop to 30 minutes** (`EP_NIGHT_POLL_SECONDS`,
  midnight to 07:00 local), on the reasoning that a headstart is worth nothing
  while you are asleep and those hours accumulate volume unattended.

  **Set to `0` on 2026-08-26**, for the final 48 hours. The reasoning held
  while the watcher could only notify; it stopped holding once the watcher
  could reserve a ticket by itself, because then it does not need David awake
  to be useful. The evidence is direct: fourteen listings appeared between
  19:00 and midnight on the 25th, and one of the only two checkouts this
  project has ever reached was at **00:53**, inside the window that brake was
  slowing. The block risk it was managing is real and unchanged — this is a
  deliberate trade for the last two days, not a repeal.

The tension is real and worth stating plainly. A resale listing observed
during testing lived about five minutes; the fuller measurement is worse than
that, and is the reason the cadence moved at all — of 75 distinct listings,
**70 were seen exactly once**, gone by the next look. But a blocked watcher misses *all* of them. A watcher that
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

- **It will not pay for the ticket.** With securing off — the default — it
  finds and alerts and nothing else. With `EP_SECURE_ON_FIND=1` it will click
  into a resale listing and hold it in a basket, and then stop dead: there is
  no code path that enters payment details or confirms an order, and the
  buttons it may press are an allowlist with a payment denylist in front of
  it. The last step is always yours. This bullet used to read "it will not buy
  the ticket", which stopped being the whole truth on 2026-08-19 — see
  [Securing a ticket automatically](#securing-a-ticket-automatically-opt-in-off-by-default)
  for what the account is now exposed to and why that trade was made
  deliberately.
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
