"""Email + push delivery.

Lifted from the original watcher, which got this part right: every send is
wrapped so a bad credential or an ntfy hiccup can never take down the run or
lose the state write.
"""

import os
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Callable, List

import requests

from . import config
from .model import GOOD_STATUSES, UNKNOWN, Listing, Reading
from .state import stamp


#: Set while sending sample alerts, so every one of them is unmistakably a
#: drill. Without this a sample reads exactly like the real thing — a test
#: "watcher stopped" email once landed saying "this is the last email you'll
#: get from it" while the watcher was running perfectly well. An alert you
#: cannot tell from a rehearsal is worse than no rehearsal.
TEST_MODE = False

#: Seconds any single send may take. Without a ceiling, smtplib will sit on a
#: dead network for minutes and stall the poll that is trying to find a ticket.
SEND_TIMEOUT_SECONDS = float(os.environ.get("EP_SEND_TIMEOUT", "20"))


def _mark(text: str) -> str:
    return f"[TEST — not real] {text}" if TEST_MODE else text


def _from_watcher() -> str:
    """"(via the VPS watcher)" and the like, when more than one is running.

    Empty on a single-machine setup. With two, David has to be able to tell
    which one spoke — if only to know which one to go and look at when they
    disagree.
    """
    return f" [{config.WATCHER_LABEL}]" if config.WATCHER_LABEL else ""


def _send_email(subject: str, body: str) -> None:
    if not (config.GMAIL_ADDRESS and config.GMAIL_APP_PASSWORD):
        raise RuntimeError("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set")
    subject = _mark(subject)

    msg = MIMEMultipart()
    msg["From"] = config.GMAIL_ADDRESS
    msg["To"] = config.ALERT_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=SEND_TIMEOUT_SECONDS) as srv:
        srv.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
        srv.send_message(msg)
    print(f"[{stamp()}] Email sent to {config.ALERT_TO}")


def _header_safe(text: str) -> str:
    """Make a string safe to put in an HTTP header, keeping every character.

    ntfy carries the title as a header, and HTTP headers are latin-1. Any
    character outside it raises UnicodeEncodeError inside requests before a
    single byte leaves the machine — so the push does not fail late or arrive
    mangled, it never goes at all.

    This is not an edge case in this project, it is the normal case:

      * Prices are in euro. On 2026-08-20 at 09:44 a real Early Entry listing
        was found at €46.50, the email went out, and the push died with
        "'latin-1' codec can't encode character '\u20ac'". The push is the
        channel that reaches David away from the desk, on listings that live
        twelve to twenty minutes.
      * The em-dash is worse, because it is in fixed titles rather than in
        data. "EP2026: TICKET HELD — check out NOW" and "HELD ... — TAP TO
        PAY" both contain one, which means the single most urgent push this
        system can send was guaranteed to fail every time.
      * _mark() prepends "[TEST — not real]" in test mode, so every push
        failed there too, which is where it should have been noticed.

    RFC 2047 encoded-words are the fix ntfy documents, and the round trip was
    verified against a live topic: a title goes out base64-wrapped and comes
    back with its em-dash and euro sign intact. Pure-ASCII titles are left
    alone so the common case stays readable in logs and in any client that
    does not decode.
    """
    if not text:
        return ""
    try:
        text.encode("latin-1")
        return text
    except UnicodeEncodeError:
        import base64

        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        return f"=?utf-8?B?{encoded}?="


def _send_ntfy(title: str, message: str, priority: str = "default", tags=None,
               click: str = None) -> None:
    """`click` is where tapping the notification takes you.

    Defaulting it to the first event was fine with one page; with two it
    would open the wrong one, at speed, while the real listing sold.
    """
    if not config.NTFY_TOPIC:
        return
    resp = requests.post(
        f"https://ntfy.sh/{config.NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={
            # Header values, and therefore latin-1 only. See _header_safe.
            "Title": _header_safe(_mark(title)),
            "Priority": priority,
            "Tags": ",".join(tags or []),
            "Click": click or config.EVENT_URL,
        },
        timeout=SEND_TIMEOUT_SECONDS,
    )
    # A 200 is the only thing that means the message is on its way. Silently
    # accepting a 4xx/5xx would report a delivery that never happened, which
    # is the whole failure this module is being hardened against.
    if resp.status_code == 429:
        # The server knows its own limit better than our tally does.
        from . import pushquota

        pushquota.note_exhausted()
    resp.raise_for_status()
    # Counted only when ntfy accepted it. A refusal spends no quota, and
    # counting one would make the watcher throttle itself against a limit it
    # had not actually reached.
    from . import pushquota

    pushquota.note_sent()
    print(f"[{stamp()}] Push sent")


def _safe(label: str, fn: Callable, *args, **kwargs) -> bool:
    """Run a send, swallow its failure, and report whether it worked.

    The return value is the point, and it was missing. A send that failed was
    indistinguishable from one that succeeded, so callers stamped their
    "already told him" clocks either way. Demonstrated on 2026-08-18: a power
    cut took the network down, the watchdog fired at 09:39, both the email and
    the push died on DNS resolution, and the six-hour re-nag clock started
    anyway. Had the outage lasted, the watcher would have stayed silent for
    six hours believing it had raised the alarm — the exact ambiguity this
    project exists to abolish, hiding one layer below the alerting logic.
    """
    try:
        fn(*args, **kwargs)
        return True
    except Exception as exc:
        print(f"[{stamp()}] WARNING: {label} notification failed: {exc}")
        return False


def _push(label: str, **kwargs) -> bool:
    """Send a push, reporting whether it actually went.

    Returns False when no topic is configured, rather than raising: an
    email-only setup should not log a warning on every alert. It is still not
    counted as a delivery, because nothing was delivered.
    """
    if not config.NTFY_TOPIC:
        return False
    return _safe(label, _send_ntfy, **kwargs)


# ── The alerts ───────────────────────────────────────────────────────────────

def _event_of(reading: Reading) -> tuple:
    """(name, url) for the page a reading is about. Never guessed from config.

    Two pages are watched — an ordinary Weekend Camping ticket and the
    Weekend Camping Instalment Plan — and they are separate products with
    separate inventory. An alert that takes its name and link from
    config.EVENT_* always describes the first one, so a find on the instalment
    page would send David to a page with nothing on it while the real listing
    sold. The config values remain only as the fallback for a reading that
    genuinely carries no event, which is the single-event and test case.
    """
    name = getattr(reading, "event_name", "") or config.EVENT_NAME
    url = getattr(reading, "event_url", "") or config.EVENT_URL
    return name, url


def _which_ticket(name: str) -> str:
    """The one-line distinction between the two pages, spelled out.

    Both are "Weekend Camping" and the names differ only by a trailing
    "Instalment Plan", which is easy to skim past on a phone at speed. Since
    the two cost differently and are bought differently, the email says which
    kind it is in its own words rather than relying on the reader spotting a
    suffix.
    """
    lowered = (name or "").lower()
    if "early entry" in lowered:
        # The distinction that matters most of the three, because acting on
        # this one in a hurry could mean buying something unusable. It is an
        # add-on: Ticketmaster's own note reads "Early Entry passes are only
        # valid with a Weekend Ticket".
        return ("This is the EARLY ENTRY PASS — an ADD-ON for campsite access "
                "from 2pm\non the Thursday. It is NOT a festival ticket and is "
                "only valid alongside\na Weekend Ticket. It is worth nothing on "
                "its own.")
    if "instalment" in lowered:
        return ("This is the INSTALMENT PLAN page — the pay-in-stages listing, "
                "not the standard one.")
    return ("This is the STANDARD Weekend Camping page — the pay-in-full "
            "listing, not the instalment plan.")


def _listing_block(listings: List[Listing]) -> str:
    if not listings:
        return "  (the source reported availability but named no specific tier)"
    return "\n".join(f"  • {l.describe()}" for l in listings)


def buy_url(event_url: str, quantity: int = None, listing: Listing = None) -> str:
    """The shortest link that lands on the listing rather than on the event.

    The plain event URL costs David the whole search by hand — pick the
    quantity, press Find Tickets, scroll to Other Options — and on 2026-08-19
    he reported that this is where the ticket is usually lost. Every listing
    found so far has been visible for a single poll, so the seconds are the
    product.

    `quantity` is the load-bearing part and is the one parameter we are sure
    of: the page defaults to 2, resale results are filtered by quantity, and a
    single ticket is invisible to a search for two. Carrying it in the URL
    removes the step most likely to be fumbled at speed.

    `listing_id` is appended as a fragment when known. That is a guess about
    Ticketmaster's routing rather than something observed — an unknown
    fragment is ignored by any page that does not use it, so it costs nothing
    if wrong, and the find recorder writes the constructed URL alongside the
    listing so the next live sighting settles whether it works.
    """
    if not event_url:
        return event_url
    quantity = config.WANTED_QUANTITY if quantity is None else quantity
    joiner = "&" if "?" in event_url else "?"
    url = f"{event_url}{joiner}quantity={quantity}"
    if listing is not None and getattr(listing, "listing_id", None):
        url = f"{url}#resale-{listing.listing_id}"
    return url


def _best_listing(listings: List[Listing]) -> Listing:
    """The listing an alert should point at when several are live at once.

    Resale before primary, because resale is what actually appears here, and
    a known id before an unknown one, because only the former can be linked
    to. Beyond that the first, which is the order Ticketmaster returned them.
    """
    if not listings:
        return None
    ranked = sorted(
        listings,
        key=lambda l: (l.kind != "resale", not getattr(l, "listing_id", None)),
    )
    return ranked[0]


def _headline(listing: Listing) -> str:
    """Section and price in one line, for a phone's lock screen.

    The push used to say only "a ticket is on the resale" and put the detail
    in the body, so deciding whether it was worth opening meant opening it.
    Section and price are what that decision actually needs.
    """
    if listing is None:
        return "a ticket is live"
    bits = []
    if getattr(listing, "section", None):
        bits.append(f"Section {listing.section}")
    if listing.price:
        bits.append(listing.price)
    return " · ".join(bits) or listing.name


def available(reading: Reading, reason: str, new_listings: List[str]) -> None:
    # Is this a ticket he has not been told about, or the same one again?
    #
    # The alerts did not distinguish these, and on 2026-08-24 that sent 69
    # pushes for 16 listings — roughly four apiece, every one titled and
    # toned exactly like the first. An alert that arrives four times is not
    # four times as loud; it is the alert being taught to be ignored, which
    # matters more here than anywhere else because the plan now depends on
    # David buying by hand from these.
    #
    # So a new ticket keeps everything: urgent priority, the ring, the works.
    # A repeat says plainly that it is a repeat and arrives quietly. Both
    # still carry the link, because a quiet reminder he happens to see is
    # still a ticket he can buy.
    is_repeat = not new_listings
    which = []
    if reading.primary in GOOD_STATUSES:
        which.append("box office")
    if reading.resale in GOOD_STATUSES:
        which.append("verified resale")
    where = " and ".join(which) or "Ticketmaster"

    new_block = ""
    if new_listings:
        new_block = "\nNew since the last check:\n" + "\n".join(f"  • {n}" for n in new_listings) + "\n"

    # Name the event from the reading, never from config: with more than one
    # page being watched, an alert that says the wrong one sends you to a page
    # with nothing on it while the real listing sells.
    name, url = _event_of(reading)

    pick = _best_listing(reading.listings)
    link = buy_url(url, config.WANTED_QUANTITY, pick)

    # The link goes first, and alone on its line. Everything below it is
    # context for afterwards; a listing that lives four minutes is not read
    # top to bottom.
    subject = (
        f"still there ({where}): {_headline(pick)} — {name}{_from_watcher()}"
        if is_repeat else
        f"TICKETS AVAILABLE ({where}): {_headline(pick)} — {name}{_from_watcher()}"
    )
    body = (
        f"Hi David,\n\n"
        f"GO — {_headline(pick)} on the {where}, {name}.\n\n"
        f"{link}\n\n"
        f"That link asks for the page with the quantity already set to "
        f"{config.WANTED_QUANTITY}.\n"
        f"Open it in a BROWSER, not the app — the resale panel only exists on\n"
        f"the website.\n\n"
        f"IF THE LISTING IS NOT THERE, the link did not do its job — do this:\n\n"
        f"  1. Set the quantity to {config.WANTED_QUANTITY}. The page defaults to 2, and\n"
        f"     resale results are filtered by quantity — a single ticket does\n"
        f"     not appear at all when you ask for two.\n"
        f"  2. Press 'Find Tickets' (or 'Search Again').\n"
        f"  3. Scroll to 'Other Options' > 'Verified Resale Tickets'.\n\n"
        f"{_which_ticket(name)}\n\n"
        f"What the watcher saw:\n{_listing_block(reading.listings)}\n{new_block}\n"
        f"Trigger : {reason}\n"
        f"Source  : {reading.source}\n"
        f"Wanted  : {config.WANTED_QUANTITY} ticket(s)\n\n"
        f"GO NOW. Most listings here are gone by the watcher's next look —\n"
        f"70 of the 75 seen so far were visible exactly once, which at a 90\n"
        f"second sweep means well under two minutes on sale. A few do come\n"
        f"back later, so it is still worth a second try, but do not plan on\n"
        f"it.\n\n"
        f"NOTE: the watcher itself is currently being refused at the checkout\n"
        f"on every listing, while Ticketmaster's own error page confirms the\n"
        f"ticket is still live. That is being investigated. It does not appear\n"
        f"to affect buying by hand — so please do not wait for the watcher to\n"
        f"secure this one.\n\n"
        f"Plain event page, if the link above misbehaves:\n{url}\n\n"
        f"Checked at: {stamp()}\n"
    )
    _safe("available-email", _send_email, subject, body)
    _push(
        "available-push",
        # Section and price in the title, so the lock screen alone is enough
        # to decide whether to move. A repeat says so in the first word, for
        # the same reason — the title is all that is read on a lock screen,
        # and "still there" and "NEW" call for different reactions.
        title=(f"still there — EP2026 {_headline(pick)}" if is_repeat
               else f"NEW — EP2026 {_headline(pick)}"),
        message=f"{name}\n\n" + _listing_block(reading.listings)
                + f"\n\nTap to open at quantity {config.WANTED_QUANTITY}.",
        # Urgent bypasses do-not-disturb on the phone. That is exactly right
        # for a ticket he has not seen and exactly wrong for the fourth
        # reminder about one he has.
        priority="default" if is_repeat else "urgent",
        tags=["tickets"] if is_repeat else ["tickets", "rotating_light"],
        click=link,
    )
    # Last, and only for the real thing. A ringing phone is the one channel
    # that works when the others do not — asleep, in a pocket, in a cinema —
    # and it is the only one that costs money and wakes people, so it is fired
    # for a ticket and for nothing else. Never for a heartbeat or a watchdog.
    #
    # And never for `python -m ep_watcher test`, which sends five sample
    # alerts including this one. Being rung by a test is how somebody learns
    # that a call from this number can be ignored — the precise habit that
    # would cost the ticket. `python -m ep_watcher ring` is the command for
    # proving the phone works, and it rings for real.
    # And never for a reminder. The ring is reserved for news; a phone that
    # rings about a ticket he has already been told about twice is the fastest
    # way to make the ring itself ignorable.
    if not TEST_MODE and not is_repeat:
        _safe("call", ring_phone, f"{_headline(pick)} on {name}")


#: Whether the phone rang for this find already, keyed by nothing — a single
#: flag, because the re-nag alert fires every few minutes while a listing
#: stays up and David does not need to be rung every time.
_last_call_at = 0.0


def ring_phone(what: str) -> bool:
    """Actually telephone David. False when not configured, which is the default.

    Everything else this module sends is a notification: it arrives, and it
    waits to be noticed. A resale ticket on this event has never survived long
    enough to wait — the two on 2026-08-20 were gone inside a minute — so the
    difference between a push seen now and a push seen in ten minutes is the
    whole product. A phone call is the only channel that interrupts rather
    than queues.

    Twilio because it is the one that needs no app installed and rings a
    normal phone through the normal network, so it works with the handset
    face-down, on silent, or with the ntfy app killed by iOS overnight.

    Deliberately inert unless three variables are set, and silent about it —
    this is an optional extra on the hottest path in the system, and a missing
    account must never be able to delay or break the alert that does work.
    Set these in ~/.ep2026-watcher/env to switch it on:

        TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM, ALERT_PHONE

    Rate limited to one call per RING_COOLDOWN_MINUTES. The availability alert
    re-fires every few minutes while a listing stays up, and being rung on
    each of those would train him to decline the call — which would cost
    exactly the ticket this exists to catch.
    """
    global _last_call_at

    if not config.can_ring_phone():
        return False
    now = time.time()
    if now - _last_call_at < config.RING_COOLDOWN_MINUTES * 60:
        print(f"[{stamp()}] not ringing again — called "
              f"{(now - _last_call_at) / 60:.1f} min ago")
        return False

    # Spoken twice: a phone answered from a pocket loses the first sentence.
    speech = (f"Electric Picnic ticket alert. {what}. "
              f"Check your email now. "
              f"Repeating. Electric Picnic ticket alert. {what}.")
    try:
        response = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{config.TWILIO_SID}"
            f"/Calls.json",
            auth=(config.TWILIO_SID, config.TWILIO_TOKEN),
            data={
                "To": config.ALERT_PHONE,
                "From": config.TWILIO_FROM,
                "Twiml": f"<Response><Say voice='alice'>{_xml_safe(speech)}"
                         f"</Say></Response>",
            },
            timeout=config.RING_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        print(f"[{stamp()}] could not place the call: {type(exc).__name__}: {exc}")
        return False

    if response.status_code >= 300:
        print(f"[{stamp()}] Twilio refused the call: HTTP "
              f"{response.status_code} {response.text[:200]}")
        return False
    _last_call_at = now
    print(f"[{stamp()}] PHONE RINGING — {config.ALERT_PHONE}")
    return True


def _xml_safe(text: str) -> str:
    """Escape for TwiML. The section and price come from Ticketmaster's feed.

    A stray ampersand in a listing description would otherwise produce
    malformed XML, which Twilio rejects — turning a ticket alert into a silent
    phone on the one occasion it was most needed.
    """
    return (text.replace("&", "and").replace("<", "").replace(">", "")
                .replace('"', "").replace("'", ""))


def _preempt_line(hold) -> str:
    """One line saying an earlier hold was dropped to attempt this one.

    Never omitted, and never buried. A weekend ticket is allowed to close the
    browser on a held Early Entry pass — that is David's rule — but he has to
    be told the pass is gone, whether the swap paid off or not. Finding out by
    walking to a laptop that is showing something different is not acceptable.
    """
    if not getattr(hold, "preempted", False):
        return ""
    return (
        "NOTE: an Early Entry pass was being held and has been LET GO to try\n"
        "for this. That is your rule — the weekend ticket comes first, and the\n"
        "pass is only valid alongside one anyway.\n\n"
    )


def _where_to_finish(hold) -> str:
    """Where to go to pay, phone first when there is a link to try.

    This alert used to say flatly "do NOT try to pick this up on your phone",
    on the reasoning that a Ticketmaster basket lives in the session that
    created it. That is certainly true of a signed-OUT session. It may well be
    wrong for a signed-in one, where the cart can be bound to the account
    server-side and follow him to any device he is signed in on. Nobody has
    tested which applies here.

    Being wrong in that direction is expensive and asymmetric. If the cart
    does travel and the email told him not to try, the ticket is lost every
    time he is away from the laptop. If it does not travel and he tries, he
    sees an empty basket and walks to the laptop — which is what he would have
    done anyway, minus a few seconds.

    So the link goes first when there is one, described honestly as worth
    trying rather than as the answer, with the laptop named as the certainty.
    """
    laptop = (
        "THE LAPTOP DEFINITELY HAS IT. The Chrome window holding the ticket has\n"
        "been brought to the front, signed in and sitting on the checkout page.\n"
        "That one is not a maybe.\n\n"
    )
    url = getattr(hold, "checkout_url", "") or ""
    if not url:
        return (
            "GO TO THE MACHINE RUNNING THE WATCHER.\n\n" + laptop +
            "No checkout link could be captured this time, so the laptop is the\n"
            "only way in.\n\n"
        )
    return (
        f"TRY THIS ON YOUR PHONE FIRST — it may just work:\n\n"
        f"{url}\n\n"
        f"You must already be signed in to Ticketmaster on the phone, as the\n"
        f"same account. If the basket is there, pay and you are done.\n\n"
        f"IF IT SHOWS AN EMPTY BASKET, stop and go to the laptop. That means\n"
        f"the hold is tied to the browser that made it, and every second spent\n"
        f"reloading on the phone is a second off the clock.\n\n"
        f"{laptop}"
    )


def _clock_line(hold, minutes: int) -> str:
    """How long he has, and how much to trust the number.

    A measured countdown and an estimate deserve different wording. The
    estimate comes from a single observation of an entirely different event —
    a boxing match at Croke Park — and David said plainly he would not trust
    another event's timer, which is right.
    """
    if getattr(hold, "minutes_measured", False):
        return (
            f"THE PAGE ITSELF SAYS {minutes} MINUTES — that is its own countdown,\n"
            f"read at the moment the ticket was held, not an estimate.\n\n"
        )
    # No countdown is not the same as an unknown countdown.
    #
    # The checkout reached on 2026-08-26 had no clock on it, and that was not
    # a reading failure — there was nothing to count down, because nothing was
    # reserved. Quoting "around 10 minutes" there would invent a safety margin
    # that does not exist, from a boxing match at Croke Park, on a page whose
    # own text says the tickets are not yours until you pay.
    return (
        f"NO COUNTDOWN ON THE PAGE. Do not read that as time in hand — on the\n"
        f"one checkout this has ever reached there was no clock because\n"
        f"nothing was reserved. Someone else buying it first is what ends\n"
        f"this, and nothing on screen will warn you. Go now.\n\n"
    )


def secured_hold(reading: Reading, hold) -> None:
    """A resale listing is sitting in a basket, signed in as David, right now.

    The loudest thing this project sends, and the only one with a countdown
    on it. Everything about the wording assumes he is not at the machine: it
    says which machine, it says the hold dies without him, and it does not
    bury either under context.

    Deliberately does NOT offer a link as the primary action. A Ticketmaster
    basket belongs to the session that created it, so a link opened on his
    phone is a different session with an empty checkout — sending him there
    while a real hold ticks away on the laptop would be the worst possible
    outcome of this whole feature.
    """
    name, _url = _event_of(reading)
    pick = _best_listing(reading.listings)
    minutes = getattr(hold, "minutes_hint", 0) or config.HOLD_MINUTES_HINT

    # "On the checkout page", not "held".
    #
    # The only checkout this project has ever reached — 00:53 on 2026-08-26 —
    # said in its own words: "Proceed to payment to reserve these tickets".
    # The tickets were NOT reserved. Nothing was held. Telling David a ticket
    # is his when it is merely reachable would be the cruellest thing this
    # system could say, and it would also slow him down: a held ticket can be
    # strolled to, an unheld one cannot.
    subject = (f"CHECKOUT OPEN — PAY NOW TO GET IT: {_headline(pick)} — "
               f"{name}{_from_watcher()}")
    body = (
        f"Hi David,\n\n"
        f"The watcher has a CHECKOUT PAGE open for this ticket, signed in as\n"
        f"you, right now.\n\n"
        f"  {_headline(pick)}\n"
        f"  {name}\n\n"
        f"THE TICKET IS NOT RESERVED YET. Ticketmaster's own words on that\n"
        f"page are 'Proceed to payment to reserve these tickets'. It becomes\n"
        f"yours when you pay and not before, so this is a race, not a hold.\n\n"
        f"{_preempt_line(hold)}"
        f"{_where_to_finish(hold)}"
        f"{_clock_line(hold, minutes)}"
        f"The watcher will not pay for it: it stops at the basket, every\n"
        f"time, by design.\n\n"
        f"WHAT THE CHECKOUT PAGE WILL ASK YOU — from a real one captured on\n"
        f"2026-08-19, so you are not reading it for the first time in a hurry.\n"
        f"Three of these are compulsory and the page will refuse to submit\n"
        f"until each has an answer:\n\n"
        f"  1. Payment — choose a method (the page offers PayPal).\n"
        f"  2. 'Protect Your Ticket Purchase' — insurance, about €9. Either\n"
        f"     answer is accepted; 'No, I do not want to protect my tickets'\n"
        f"     is the quick one.\n"
        f"  3. 'Event Partners' — marketing opt-in. 'No, I don't want to hear\n"
        f"     from them' is the quick one.\n"
        f"  4. 'Event Extras' (a souvenir ticket, ~€6) defaults to 0 — leave it.\n\n"
        f"Then 'Place Order'. Do NOT press 'Cancel Order', which sits beside it\n"
        f"and throws the hold away.\n\n"
        f"{_which_ticket(name)}\n\n"
        f"What was held:\n{_listing_block(reading.listings)}\n\n"
        f"Held at: {stamp()}\n"
    )
    _safe("secured-email", _send_email, subject, body)
    _push(
        "secured-push",
        # The push is what reaches him when he is away from the desk, which is
        # precisely the case the checkout link exists for. Tapping it is the
        # fastest possible route to paying, and costs a glance at an empty
        # basket when the cart turns out not to travel.
        title=f"HELD {_headline(pick)} — TAP TO PAY"
              if getattr(hold, "checkout_url", "") else
              f"HELD {_headline(pick)} — GO TO THE LAPTOP",
        message=f"{name}\n\nIn a basket under your account. Roughly {minutes} "
                f"minutes to pay."
                + ("\n\nTap to try it here. Empty basket = go to the laptop."
                   if getattr(hold, "checkout_url", "") else
                   "\n\nOn the watcher's machine only."),
        priority="urgent",
        tags=["rotating_light", "shopping_cart"],
        click=getattr(hold, "checkout_url", "") or None,
    )


def early_entry_worth_it(reading: Reading) -> None:
    """A weekend ticket is held, so the Early Entry Pass is worth having again.

    The one standing instruction in this project with a trigger rather than a
    date on it. David switched the pass off on 2026-08-20 — not watched, not
    swept, not secured — on the reasoning that every request the watcher can
    spend should go to finding a weekend ticket, and asked to be able to turn
    it back on easily once he had one. Ticketmaster's own note is why:
    "Early Entry passes are only valid with a Weekend Ticket".

    That moment has just arrived, and it is the worst imaginable moment to
    rely on anyone remembering a configuration flag — there is a live basket
    with a countdown on it and he is being told to run to the laptop. So this
    is a separate, quiet message that will still be in his inbox afterwards.

    Deliberately does NOT flip the switch. It restores searching and securing
    together, and turning either on is his call — the ticket in the basket is
    not paid for yet, and a pass secured against a purchase that fell through
    would be exactly the outcome the priority rules exist to prevent.
    """
    name, _url = _event_of(reading)
    subject = f"Now worth turning the Early Entry Pass back on{_from_watcher()}"
    body = (
        f"Hi David,\n\n"
        f"A weekend ticket is held ({name}), so the Early Entry Pass has\n"
        f"stopped being a distraction and become the next thing worth having.\n"
        f"Ticketmaster only honour a pass alongside a Weekend Ticket, which is\n"
        f"why it was switched off while you had neither.\n\n"
        f"Pay for the ticket first. Then, if you want the pass:\n\n"
        f"    echo 'export EP_EARLY_ENTRY=1' >> ~/.ep2026-watcher/env\n"
        f"    ./restart.sh\n\n"
        f"That is the whole procedure. It restores searching AND holding for\n"
        f"the pass together — deliberately, because a search that only emails\n"
        f"you on the day you want one held would be a switch that looks like\n"
        f"it worked and does half the job.\n\n"
        f"Nothing about the pass was deleted when it was switched off: its\n"
        f"page, cadence, priority and history are all still in place.\n\n"
        f"Sent at: {stamp()}\n"
    )
    _safe("early-entry-email", _send_email, subject, body)


def buyer_blocked(reading: Reading, minutes: float) -> None:
    """Ticketmaster is refusing the buying browser. Only David can buy now.

    Sent once when the block has survived enough finds to be called a block
    rather than a challenge, and deliberately separate from the per-attempt
    failure email — that one arrives beside every find and is easy to stop
    reading. This says the thing that changes what he does: for the next while,
    the watcher can tell him a ticket exists and nothing more.
    """
    name, _url = _event_of(reading)
    _safe(
        "buyer-blocked-email", _send_email,
        f"the watcher cannot buy — only you can, for now{_from_watcher()}",
        f"Hi David,\n\n"
        f"Ticketmaster has started showing the watcher's buying browser a\n"
        f"block screen instead of the ticket page, on {name}.\n\n"
        f"WHAT THIS MEANS. Finding and alerting are completely unaffected —\n"
        f"you will still get the loud email the moment a ticket appears, with\n"
        f"the link. What has stopped is the watcher putting it in a basket for\n"
        f"you. Until this clears, YOU are the only one who can buy.\n\n"
        f"WHAT IT IS NOT. Nothing is wrong with your account, and nothing is\n"
        f"wrong with the sign-in. This is bot detection objecting to the\n"
        f"browser's traffic, not to you.\n\n"
        f"The buying browser will stand down for {minutes:.0f} minutes rather\n"
        f"than keep knocking — every blocked attempt costs the watcher several\n"
        f"minutes of not looking, and gives whatever is unhappy another reason\n"
        f"to stay unhappy. It will try again quietly after that.\n\n"
        f"Sent at: {stamp()}\n"
    )
    _push(
        "buyer-blocked-push",
        title="Watcher can't buy — you must",
        message=f"Ticketmaster is blocking the buying browser. Alerts still "
                f"work; tap the ticket email and buy it yourself.",
        priority="high",
        tags=["no_entry"],
    )


def buying_broken(reading: Reading, streak: int) -> None:
    """A run of live listings refused at checkout. The buying path is broken.

    Deliberately distinct from buyer_blocked() above, because the two states
    look identical from the outside and call for opposite responses. A block
    is Ticketmaster asking for less traffic; it clears, and resting is the
    right answer. This is a checkout that accepts the request and hands back a
    "sold out" page for a ticket that is still on sale — it does not clear on
    its own, and waiting is the wrong answer.

    Sent once per run, not once per refusal. The whole point is that the
    per-attempt failure emails were arriving faithfully and truthfully for
    five days while nothing said the sentence that mattered, which is that all
    of them were the same failure.
    """
    name, url = _event_of(reading)
    _safe(
        "buying-broken-email", _send_email,
        f"the watcher cannot buy at all — buy by hand{_from_watcher()}",
        f"Hi David,\n\n"
        f"{streak} tickets in a row have now been refused at the checkout on\n"
        f"{name} — and in each case Ticketmaster's own error page confirmed\n"
        f"the listing was still ACTIVE at the moment it refused us.\n\n"
        f"WHAT THIS MEANS. The tickets are real and they are on sale. Finding\n"
        f"them is working perfectly. What is not working is the last step,\n"
        f"where the watcher clicks through to buy — that step is being\n"
        f"refused every single time, on every listing.\n\n"
        f"WHAT TO DO. Buy by hand from the link in the ticket alerts. Do not\n"
        f"wait for the watcher to secure one; on current evidence it will not.\n"
        f"Move immediately. 70 of the 75 listings seen so far were gone by\n"
        f"the next look — there is less time than it feels like.\n\n"
        f"WHAT IT IS NOT. Not your account, and not the sign-in — both check\n"
        f"out fine. Not a race being lost either; the refusal arrives in a\n"
        f"fifth of a second, long before any race could be run.\n\n"
        f"{url}\n\n"
        f"Sent at: {stamp()}\n"
    )
    _push(
        "buying-broken-push",
        title="Watcher can't buy — buy by hand",
        message=f"{streak} live tickets refused at checkout in a row. Finding "
                f"works, buying does not. Use the link in the ticket alert.",
        priority="high",
        tags=["rotating_light"],
    )


def _timing_block(hold) -> str:
    """Where the seconds went, for an email about a race that was lost.

    These listings are consumed in well under a minute, so "it was gone when
    we got there" is only half an answer — the other half is how long getting
    there took, and which step ate it. Without this the only available reply
    to "why did we lose" is an estimate read off minute-resolution log lines,
    which is what was being done until 2026-08-20.

    Silent when nothing was measured, so an attempt that failed before it
    started does not print a heading over an empty list.
    """
    line = getattr(hold, "timing_line", lambda: "")()
    if not line:
        return ""
    return f"Where the time went:\n  {line}\n\n"


def secure_failed(reading: Reading, hold) -> None:
    """We tried to hold it and could not. Say so plainly, and why.

    Sent alongside the ordinary availability alert rather than instead of it.
    Its job is to stop him trusting a hold that does not exist — silence here
    would be read as "it's in the basket" the moment the feature is switched
    on and he stops reading the other email closely.
    """
    name, _url = _event_of(reading)
    reason = getattr(hold, "reason", "") or "no reason recorded"
    # A block is a different message from a lost race, and the subject line is
    # the part he reads on a phone. "Could not hold it" invites a shrug; being
    # told the watcher is shut out entirely is what makes him open the other
    # email and click the link himself, which is the only thing that can work
    # while this lasts.
    blocked = bool(getattr(hold, "challenged", False))
    subject = (f"BLOCKED — buy it yourself NOW: {name}{_from_watcher()}"
               if blocked else
               f"could not hold it — buy it yourself: {name}{_from_watcher()}")
    _safe(
        "secure-failed-email", _send_email,
        subject,
        f"Hi David,\n\n"
        f"A listing appeared and the watcher tried to put it in a basket for\n"
        f"you. It did not manage to.\n\n"
        f"Why: {reason}\n\n"
        + ("Ticketmaster is refusing the watcher's buying browser, so it\n"
           "cannot reach ANY listing until this clears. Nothing is wrong with\n"
           "your account and nothing is wrong with the code — you are simply\n"
           "the only one who can buy right now. Use the link in the\n"
           "'TICKETS AVAILABLE' email, in your own browser.\n\n"
           if blocked else "")
        + f"{_preempt_line(hold)}"
        f"There is NO hold. The separate 'TICKETS AVAILABLE' email has the\n"
        f"link — if the listing is still there it is still yours to take.\n\n"
        f"What it saw:\n{_listing_block(reading.listings)}\n\n"
        f"{_verdict_block(hold)}"
        f"{_timing_block(hold)}"
        f"Tried at: {stamp()}\n",
    )


def _verdict_block(hold) -> str:
    """Did it sell, or was it never takeable? The distinction, in the email.

    "Could not hold it" has meant the same sentence all week whether the
    ticket was bought by somebody else a second earlier or was never available
    to anybody — and those call for opposite responses from David. If it sold,
    refreshing is pointless and the next listing is the only hope. If it is
    still in the feed, it is in a basket that will lapse, and trying again in
    a few minutes is the single most useful thing he can do.

    Empty when the question could not be asked, rather than guessing. A
    confident wrong answer here sends him either to a dead page or away from a
    live one.
    """
    # Ticketmaster's own error page, before either of the feed answers below.
    #
    # It outranks them because it is a statement by the party that refused us,
    # made at the instant of refusal, about the thing that matters: whether
    # the listing still exists. The feed can only say whether the ticket is
    # offerable right now, and one in somebody's basket is not — which is why
    # "the feed agrees it is gone" has told David a ticket sold on occasions
    # when Ticketmaster's own payload said it was live.
    active = getattr(hold, "listing_active", None)
    if active is None:
        active = getattr(hold, "ever_active", False) or None
    if active:
        offer = getattr(hold, "offer_summary", "") or ""
        # No basket theory, and no promise to keep trying.
        #
        # This block used to say the ticket was "sitting in somebody else's
        # basket" and that those lapse within about ten minutes. Both halves
        # are now contradicted by the watcher's own measurements, and the
        # email of 2026-08-25 10:53 carried this paragraph directly beneath a
        # reason that said the cause was NOT established — two sentences in
        # one message, disagreeing.
        #
        # The chase of 10:51 settles it as far as this project can: three
        # genuine, uncached requests over two minutes, three distinct
        # Ticketmaster error ids, the listing reported ACTIVE at every one,
        # and refused every time. A basket that was about to lapse would have
        # lapsed. Telling David to sit tight for ten minutes on that basis is
        # advice to wait out something nobody has ever observed happening.
        return (
            "IT DID NOT SELL. TICKETMASTER SAYS SO ITSELF.\n"
            "The refusal page carries Ticketmaster's own record of the\n"
            "listing, and it says the listing is still ACTIVE. So the ticket\n"
            "exists and this was not a race lost by a second.\n"
            + (f"The listing: {offer}\n" if offer else "")
            + "WHY WE WERE REFUSED IS NOT KNOWN. The watcher has now been\n"
            "refused on every listing it has ever tried, always with the\n"
            "listing still live. Do not wait for it to get this one.\n"
            "GO AND BUY IT YOURSELF from the link in the other email —\n"
            "buying by hand does not appear to be affected.\n\n"
        )
    still = getattr(hold, "still_listed_after", None)
    if still is None:
        return ""
    if still:
        ids = ", ".join(getattr(hold, "ids_after", []) or []) or "unknown"
        return (
            "IT MAY NOT ACTUALLY BE GONE.\n"
            "Ticketmaster showed the 'sold or removed' page, but its own\n"
            f"resale feed still listed a ticket a second later (id: {ids}).\n"
            "So it had not sold when we were turned away. What is holding\n"
            "it is not established — the basket theory this used to state\n"
            "as fact has not survived the watcher's own measurements.\n"
            "TRY THE LINK YOURSELF, NOW AND AGAIN IN A FEW MINUTES.\n"
            "Most listings are gone within a couple of minutes, but a few\n"
            "do reappear later.\n\n"
        )
    return (
        "It really did go: the resale feed agreed it was no longer there\n"
        "when asked immediately afterwards. Refreshing will not bring it\n"
        "back — the next listing is the one to wait for.\n\n"
    )


def reserved_in_browser(reading: Reading) -> None:
    """The strongest possible signal: we pressed the button and it worked.

    A successful reserve holds the ticket for a few minutes only, so this
    alert is worded to get David to the already-open browser window fast.

    Every identifying detail comes from the reading. This alert used to read
    them from config, which meant the loudest, most time-critical message the
    watcher can send — the one with a countdown on it — always named the
    standard Weekend Camping page and linked to it, whichever page had
    actually reserved. available() was fixed for that; this was not, and the
    only real find so far was on the instalment page.
    """
    name, url = _event_of(reading)

    subject = f"IN THE BASKET — finish checkout now: {name}{_from_watcher()}"
    body = (
        f"Hi David,\n\n"
        f"The watcher pressed 'Find Tickets' and Ticketmaster ACCEPTED it —\n"
        f"{config.WANTED_QUANTITY} ticket(s) are held in a basket right now.\n\n"
        f"Event: {name}\n"
        f"{_which_ticket(name)}\n\n"
        f"{_listing_block(reading.listings)}\n\n"
        f"This hold expires in a couple of minutes. The Chrome window the\n"
        f"watcher used has been left open on the checkout page — go finish it\n"
        f"there, or open the link below and the basket should still be yours.\n\n"
        f"{url}\n\n"
        f"Reserved at: {stamp()}\n"
    )
    _safe("reserved-email", _send_email, subject, body)
    _push(
        "reserved-push",
        title="EP2026: TICKET HELD — check out NOW",
        message=f"{name}\n\nA reserve succeeded. The hold expires in minutes.",
        priority="urgent",
        tags=["rotating_light", "shopping_cart"],
        click=url,
    )


def _status_word(status: str) -> str:
    """Spell out UNKNOWN, which is the one status that reads as reassuring.

    "UNAVAILABLE" is a real answer and looks like one. "UNKNOWN" sitting in
    the same column looks like a third flavour of no, when it actually means
    nothing could read that market at all — which is the state most likely to
    cost the ticket, and so the one that must not read as calm.
    """
    if status == UNKNOWN:
        return "UNKNOWN — nothing could read this market"
    return status


def _health_section(health) -> str:
    """Render the connection-health block that goes into status emails.

    Deliberately always present, including when everything is fine. The point
    is that David can tell at a glance whether his connection is in trouble
    without having to infer it from a run of quiet failures — which is exactly
    what he had to do the day his home IP got flagged.
    """
    severity, headline, action = health
    marker = {"ok": "OK", "watch": "WATCH", "blocked": "BLOCKED"}[severity]
    return (
        f"Connection health [{marker}]\n"
        f"  {headline}\n\n"
        f"  {action}"
    )


def _network_section(net) -> str:
    """Render the "which connection, and should you switch" block."""
    should_switch, headline, instruction = net
    marker = "SWITCH NOW" if should_switch else "OK"
    body = f"Network [{marker}]\n  {headline}"
    if instruction:
        body += f"\n\n{instruction if should_switch else '  ' + instruction}"
    return body


def _reading_block(events, reading: Reading) -> str:
    """What every watched page last said.

    The hourly report used to print whichever single reading happened to
    trigger it, beneath a link hardcoded to the first event — so a report
    could show the instalment plan's statuses under the standard page's URL,
    and there was no way to tell from the email which one it meant. It now
    reports both pages, from each page's own history, every hour.

    `events` is a list of (name, url, primary, resale, age_minutes) from
    state.event_summaries(). Without it this falls back to the single reading,
    which is the API-only and single-event case.

    The age is printed because the pages are no longer read at the same rate:
    the standard page every 6 minutes, the instalment plan every 30. Two
    statuses side by side can therefore be minutes and half an hour old, and
    without saying so the older one reads as being exactly as fresh.
    """
    if not events:
        name, url = _event_of(reading)
        return (
            f"Last reading — {name}\n"
            f"  Box office     : {_status_word(reading.primary)}\n"
            f"  Verified resale: {_status_word(reading.resale)}\n"
            f"  {url}"
        )

    lines = ["Last reading, page by page:"]
    for row in events:
        name, url, primary, resale = row[:4]
        age = row[4] if len(row) > 4 else None
        when = f"  (read {age:.0f} min ago)" if isinstance(age, (int, float)) else ""
        lines.append(
            f"\n  {name}{when}\n"
            f"    Box office     : {_status_word(primary)}\n"
            f"    Verified resale: {_status_word(resale)}\n"
            f"    {url}"
        )
    return "\n".join(lines)


def heartbeat(checks: int, failures: int, hours: float, reading: Reading,
              health=None, net=None, coverage=None, events=None,
              securing=None) -> bool:
    """The hourly "still nothing, still trying" report.

    Deliberately carries the numbers rather than just the sentiment. "No
    success in the last hour" is compatible with both a healthy watcher and
    one that has been failing every attempt — which is exactly the ambiguity
    the previous watcher died in — so the counts are the point of the email.

    `coverage` is (degraded, resale_blind) and answers the harder question:
    not "did the watcher run" but "could it see". A poll that ran fine and
    learned nothing about resale is the one that quietly costs the ticket.

    `securing` is a sentence about the buying profile having lost its sign-in,
    or "" when there is nothing wrong. It is checked hourly rather than only
    at startup because the account cookies can lapse at any point in a run
    that lasts a fortnight — and a securing feature that is armed but cannot
    work announces itself, otherwise, at the single worst moment: when a real
    listing is on screen and there are ninety seconds to act.
    """
    healthy = failures < checks or checks == 0

    def row(label, value):
        return f"{label:<30}: {value}"

    # Say what is being counted. Every watched page is polled on every cycle
    # and counted separately, so with two pages this number is twice the
    # number of cycles — which reads as a suspiciously busy watcher unless
    # the email says so.
    pages = len(events) if events else len(config.EVENTS)
    across = f" (across {pages} pages)" if pages > 1 else ""
    health_line = "\n".join([
        row(f"Page checks in the last {hours:.1f}h", f"{checks}{across}"),
        row("Of those, unhealthy", failures),
    ])
    if coverage:
        degraded, resale_blind = coverage
        health_line += "\n" + "\n".join([
            row("  · partial (a source failed)", degraded),
            row("  · resale could not be read", resale_blind),
        ])
        if checks and resale_blind == checks:
            health_line += (
                "\n\nRESALE WAS UNREADABLE ON EVERY CHECK THIS HOUR. Primary stock\n"
                "is still being read, so the watcher looks fine — but resale is\n"
                "the market a ticket has actually turned up on, and right now it\n"
                "is dark. Worth running doctor."
            )
        elif checks and resale_blind > checks / 2:
            health_line += (
                f"\n\nResale was unreadable on {resale_blind} of {checks} checks — over half.\n"
                "The searches are resolving before the resale panel renders."
            )
    if checks and failures == checks:
        health_line += (
            "\n\nEVERY check was unhealthy this hour. That is a broken watcher,\n"
            "not a quiet Ticketmaster — the numbers above are the difference."
        )
    if securing:
        health_line += f"\n\n{securing}"

    # What is switched off, said every hour. A page nobody is searching is not
    # a fault and gets no alarm, but it must not become invisible either: the
    # Early Entry Pass was switched off on an afternoon when the weekend
    # ticket was the only thing that mattered, and the day that stops being
    # true is a day David has to remember unprompted. This is the prompt.
    paused = config.paused_note()
    if paused:
        health_line += f"\n\n{paused}"

    health_block = f"\n{_health_section(health)}\n" if health else ""
    net_block = f"\n{_network_section(net)}\n" if net else ""
    if net_block:
        health_block += net_block

    # Put the ask in the subject line: a "switch networks" instruction buried
    # three paragraphs into an hourly "no luck yet" email is one nobody reads.
    # Priority order, and it is deliberate. A securing feature that is armed
    # but signed out is worse news than a network nudge: the nudge asks for a
    # small chore, this one says a ticket found in the next hour will not be
    # held. Whichever is worst gets the subject line, because only the subject
    # survives being read on a phone.
    switch_now = bool(net and net[0])
    if securing:
        subject = "Securing is armed but SIGNED OUT — EP2026 watcher"
    elif switch_now:
        subject = "Switch the MacBook to your other network — EP2026 watcher"
    else:
        subject = f"No luck yet — still watching {config.WATCH_LABEL}"
    body = (
        f"Hi David,\n\n"
        f"No ticket has appeared in the last hour. Still trying.\n\n"
        f"{health_line}\n"
        f"{health_block}\n"
        f"{_reading_block(events, reading)}\n\n"
        f"Searching for {config.WANTED_QUANTITY} ticket(s) on each page.\n\n"
        f"You'll get a separate, much louder email the moment anything shows up,\n"
        f"naming which of the pages above it is. This one just proves the\n"
        f"watcher is still alive.\n\n"
        f"Checked at: {stamp()}\n"
    )
    delivered = _safe("heartbeat-email", _send_email, subject, body)
    if not healthy:
        _push(
            "heartbeat-push",
            title="EP2026 watcher: every check failing",
            message=f"{failures}/{checks} checks failed this hour.",
            priority="high",
            tags=["warning"],
        )
    # Email only. This report's whole job is to prove the watcher is alive, and
    # a push saying so is not a substitute — the hour is only "reported" once
    # the mail lands, so on False the caller keeps the clock running and
    # retries rather than silently losing the hour.
    return delivered


def session_summary(session: dict, to_mode: str, hours: float, settings,
                    next_change: str, health=None, events=None) -> None:
    """Sent when the watcher crosses between daytime and overnight settings.

    Two jobs, and the second is the reason it exists. It reports what the
    finished session actually achieved — which the hourly heartbeat can only
    ever show an hour of — and it says plainly that the watcher's settings
    have just changed underneath him. A watcher that silently starts polling
    three times more slowly is one whose behaviour you cannot reason about
    from the inbox, and every ambiguity of that kind in this project has
    eventually cost something.

    `settings` is [(label, before, after)]; `session` is the counters from
    state.session().
    """
    from_mode = "night" if to_mode == "day" else "day"
    ended = "Overnight" if from_mode == "night" else "Daytime"

    checks = session.get("checks", 0)
    unhealthy = session.get("unhealthy", 0)
    blind = session.get("resale_blind", 0)
    finds = session.get("finds", 0)
    readable = checks - blind

    def row(label, value):
        return f"  {label:<26}: {value}"

    stats = "\n".join([
        row("Ran for", f"{hours:.1f} hours"),
        row("Page checks", checks),
        row("Of those, unhealthy", unhealthy),
        row("  · partial (a source failed)", session.get("degraded", 0)),
        row("Resale readable",
            f"{readable}/{checks}"
            + (f" ({100 * readable / checks:.0f}%)" if checks else "")),
        row("Rate-limit blocks", session.get("blocks", 0)),
        row("Tickets found", finds),
    ])

    # The listings themselves, because a count is not an answer. "1 ticket
    # found" tells you nothing about what it was or what it cost, and by the
    # time you read this it has almost certainly sold.
    seen = session.get("listings") or []
    if seen:
        stats += "\n\n  What turned up:\n" + "\n".join(f"    • {s}" for s in seen)
    elif checks:
        stats += "\n\n  Nothing appeared on either page during this session."

    change_block = "\n".join(
        f"  {label:<26}: {before}  →  {after}" for label, before, after in settings
    ) or "  (no settings differ between the two modes)"

    why = (
        "Overnight the watcher polls far less often. A headstart is worth "
        "nothing while you are asleep — a resale listing lives minutes — so "
        "those hours would otherwise accumulate request volume on your "
        "connection unattended, with nobody awake to notice a block. It also "
        "waits longer for each search, because that is when the page is slow."
        if to_mode == "night" else
        "Back to the faster daytime cadence, which is when you can actually "
        "act on a listing. This is the part of the day the watcher exists for."
    )

    health_block = f"\n{_health_section(health)}\n" if health else ""
    reading_block = f"\n{_reading_block(events, Reading(source='session'))}\n" if events else ""

    subject = (
        f"Switching to {'overnight' if to_mode == 'night' else 'daytime'} watching "
        f"— {ended.lower()} session summary"
    )
    body = (
        f"Hi David,\n\n"
        f"The watcher has just switched to {'overnight' if to_mode == 'night' else 'daytime'} "
        f"settings. Nothing for you to do — this is on a timer.\n\n"
        f"SETTINGS CHANGED\n{change_block}\n"
        f"  {'Next change':<26}: {next_change}\n\n"
        f"  {why}\n\n"
        f"{ended.upper()} SESSION JUST ENDED\n{stats}\n"
        f"{health_block}{reading_block}\n"
        f"You will still get the hourly 'no luck yet' report throughout, and a\n"
        f"much louder email the moment anything shows up.\n\n"
        f"Switched at: {stamp()}\n"
    )
    _safe("session-email", _send_email, subject, body)
    # No push. This is a scheduled, expected change and a phone buzz for it
    # would train him to swipe away the notification channel that carries the
    # ticket alert.


def network_switched(now_label: str, now_ip: str, was_label: str, was_ip: str,
                     health=None, was_blocks: int = 0, switch_after: str = "",
                     readdressed: bool = False, now_detail: str = "",
                     known=(), naming_key: str = "", named: bool = True) -> None:
    """Confirm, in writing, that the watcher moved to a different connection.

    The switch was already detected and logged, but only ever appeared in the
    hourly report — up to an hour later, in a section that also says "no luck
    yet". Since which connection is in use decides where blocks land, and the
    burnt one is the one he must not try to buy on, the change deserves saying
    at the moment it happens.

    `readdressed` distinguishes the two cases that look identical in the
    state file. Moving between home Wi-Fi and the hotspot is something David
    did; a hotspot being issued a new address is something the carrier did,
    and telling him he "switched networks" for that would be wrong.
    """
    if readdressed:
        headline = (
            f"Your {now_label} has been given a new address by the network.\n"
            f"Nothing was done at your end, and nothing needs doing."
        )
        subject = f"New address on your {now_label} — EP2026 watcher"
    else:
        headline = (
            f"The watcher noticed the MacBook is on a different connection and\n"
            f"has moved to it by itself. Nothing to confirm."
        )
        subject = f"Now watching over your {now_label} — EP2026 watcher"

    # A switch onto an already-flagged connection is the one case here that
    # needs acting on, so it goes in the subject rather than three paragraphs
    # down. Switching is supposed to buy a clean connection; landing on a
    # burnt one silently would defeat the whole scheme.
    severity = health[0] if health else "ok"
    if severity == "blocked":
        subject = f"CAUTION: your {now_label} is already rate-limited — EP2026 watcher"

    left_block = ""
    if was_blocks:
        left_block = (
            f"\nThe connection you just left, {was_label}, took {was_blocks} block(s)\n"
            f"in the last 24 hours. Leave it to recover before browsing or buying\n"
            f"on it — these decay on their own within a few hours.\n"
        )

    health_block = f"\n{_health_section(health)}\n" if health else ""

    # Every connection the watcher has ever seen, and how each has fared. When
    # one of them is flagged, the question is not "is this one bad" but "which
    # one should I go and buy on" — and that needs the whole list.
    seen_block = ""
    if known:
        rows = []
        for label, _key, searches, blocks, is_current in known:
            mark = "→" if is_current else " "
            trouble = f", {blocks} block(s)" if blocks else ", no blocks"
            rows.append(f"  {mark} {label} — {searches} searches{trouble}")
        seen_block = "\nConnections this watcher knows:\n" + "\n".join(rows) + "\n"

    # An unnamed connection still works perfectly; it is just described rather
    # than named. Offer the exact line to name it, at the moment he knows
    # which network it is — asking him to find a router MAC later would mean
    # it never gets done.
    naming_block = ""
    if not named and naming_key:
        naming_block = (
            f"\nThe name above is the watcher's own guess. To set it yourself, add\n"
            f"this to ~/.ep2026-watcher/env:\n\n"
            f'  EP_NETWORK_NAMES="{naming_key}=whatever you call it"\n\n'
            f"Separate several with commas. Nothing breaks if you never do it —\n"
            f"this connection is already tracked and blamed on its own.\n"
        )

    body = (
        f"Hi David,\n\n"
        f"{headline}\n\n"
        f"  Was : {was_label} ({was_ip or 'unknown'})\n"
        f"  Now : {now_detail or now_label} ({now_ip})\n"
        f"{health_block}{left_block}{seen_block}{naming_block}\n"
        f"What happens from here:\n"
        f"  · Request counters for this connection start from zero.\n"
        f"  · Every block from now on is recorded against this connection,\n"
        f"    so the health line above stays about the one you are actually on.\n"
        f"  · {switch_after or 'You will be told when it is time to switch again.'}\n\n"
        f"Noticed at: {stamp()}\n"
    )
    _safe("network-email", _send_email, subject, body)
    # No push. Switching is something he just did, so a buzz confirming it is
    # noise — and the push channel has to stay worth looking at.


def mac_watcher_silent(hours: float, repeat: bool = False) -> None:
    """Sent from GitHub when the Mac has stopped checking in.

    This is the alert nothing on the Mac could ever send, because by
    definition the Mac is the thing that stopped. It goes out from GitHub's
    infrastructure, which is why it survives a shut lid, a flat battery or a
    dropped Wi-Fi connection.

    Which is also why it must not print config.REPO_DIR. Every other message
    in this module is written on the Mac, where REPO_DIR is the right answer;
    this one is written on a GitHub runner, where it is the runner's checkout.
    David received exactly that on 2026-08-19 — an alert telling him to `cd`
    into /home/runner/work/... on his laptop — and this is the one alert that
    reaches him when he is away from the machine and can only act on what the
    email says. See config.MAC_REPO_DIR.

    A caveat worth carrying in the wording: "has not checked in" means no
    heartbeat arrived, which is not the same as the Mac being off. The beacon
    travels over ntfy, and on 2026-08-19 ntfy rate-limited this client for 2.8
    hours and produced this very email about a watcher that was running
    perfectly and had just completed its 800th check. So the message now says
    what it actually knows and gives him a way to tell the two apart from his
    phone.
    """
    subject = ("EP2026: your Mac watcher is STILL quiet"
               if repeat else "EP2026: your Mac watcher has gone quiet")
    body = (
        f"Hi David,\n\n"
        f"The watcher on your MacBook has not checked in for {hours:.1f} hours.\n"
        f"This message comes from GitHub, not from the Mac — which is the point:\n"
        f"if the laptop is shut, flat, or off the network, nothing on it could\n"
        f"tell you.\n\n"
        f"The GitHub backstop is still running, but it can only see a coarse\n"
        f"re-release. It cannot see a Verified Resale listing, which is how a\n"
        f"ticket has actually appeared so far. So right now you have much less\n"
        f"cover than you think.\n\n"
        f"What this actually means: no heartbeat has arrived. That is usually\n"
        f"a stopped Mac, but it is not the same thing — the heartbeat travels\n"
        f"over ntfy, and if ntfy is rate-limiting or down, a perfectly healthy\n"
        f"watcher goes silent from here. That happened on 2026-08-19.\n\n"
        f"To tell the two apart without getting up: if the hourly \"still\n"
        f"nothing\" emails are still arriving, the Mac is alive and it is the\n"
        f"heartbeat channel that is broken. If they have stopped too, the Mac\n"
        f"really is down.\n\n"
        f"To fix, on the MacBook:\n\n"
        f"  1. Wake it, and make sure it is on Wi-Fi or the hotspot.\n"
        f"  2. cd {config.MAC_REPO_DIR} && ./run_watcher.sh doctor\n"
        f"  3. If anything is wrong:  ./restart.sh\n\n"
        f"You should see this stop within about 15 minutes of the watcher\n"
        f"running again.\n\n"
        f"Noticed at: {stamp()}\n"
    )
    _safe("mac-silent-email", _send_email, subject, body)
    _push(
        "mac-silent-push",
        title="EP2026: Mac watcher is down",
        message=f"No check-in for {hours:.1f}h. The sharp watcher is not running.",
        priority="high",
        tags=["warning"],
    )


def watchdog(reason: str, failures: int, health=None) -> bool:
    """Tell David the watcher is broken. True if the news actually reached him.

    The caller must not start its re-nag clock on a False. When the fault is
    the network itself, this is precisely the alert that cannot get out, and
    treating the attempt as the telling is how a real outage goes quiet.
    """
    health_block = f"\n{_health_section(health)}\n" if health else ""

    subject = f"EP2026 watcher is not working{_from_watcher()}"
    body = (
        f"Hi David,\n\n"
        f"The ticket watcher has failed {failures} checks in a row.\n\n"
        f"Reason: {reason}\n"
        f"{health_block}\n"
        f"It will keep retrying, and it will keep nagging you every "
        f"{config.WATCHDOG_RENAG_HOURS}h until it recovers, so you can trust "
        f"silence to mean 'working'.\n\n"
        f"To find out what is actually wrong, and get the exact fix:\n\n"
        f"    {config.REPO_DIR}/run_watcher.sh doctor\n\n"
        f"To just put everything back the way it should be:\n\n"
        f"    {config.REPO_DIR}/restart.sh\n\n"
        f"Both are safe to run as often as you like.\n\n"
        f"Checked at: {stamp()}\n"
    )
    delivered = _safe("watchdog-email", _send_email, subject, body)
    pushed = _push(
        "watchdog-push",
        title="EP2026 watcher is broken",
        message=reason,
        priority="high",
        tags=["warning"],
    )
    return delivered or pushed


def stopped(checks_total: int) -> None:
    """Final email: the watcher has reached its stop date and shut down.

    Sent once, on the way out. Without it the watcher simply goes silent, and
    silence is the one thing this whole design refuses to be ambiguous about —
    "no more emails" should never leave you wondering whether it died or
    finished.
    """
    subject = f"Watcher stopped — {config.WATCH_LABEL}"
    pages = "\n".join(f"  · {e.name}" for e in config.EVENTS)
    body = (
        f"Hi David,\n\n"
        f"The watcher has reached its stop date ({config.STOP_AFTER_DATE}) and shut\n"
        f"itself down. This is the last email you'll get from it.\n\n"
        f"It ran {checks_total} checks in total, across:\n{pages}\n\n"
        f"Nothing is left running on a schedule. If you want to tidy up:\n\n"
        f"  launchctl unload ~/Library/LaunchAgents/com.davidcoyne.ep2026watcher.plist\n"
        f"  sudo pmset -a disablesleep 0\n\n"
        f"To watch a later event, set EP_STOP_AFTER to a new date in\n"
        f"~/.ep2026-watcher/env and start it again.\n\n"
        f"Stopped at: {stamp()}\n"
    )
    _safe("stopped-email", _send_email, subject, body)
    _push(
        "stopped-push",
        title="EP2026 watcher stopped",
        message="Reached its stop date and shut down. No further alerts.",
        priority="low",
        tags=["checkered_flag"],
    )


def recovered(after: int, dark_minutes: float = 0.0, reason: str = "") -> bool:
    """The watcher is reading Ticketmaster again after a run of failures.

    Carries how long the blackout lasted, because that is the part David
    cannot reconstruct afterwards. While the watcher was down it could not see
    a resale listing, and a listing lives ten to twenty minutes — so "we were
    dark for 69 minutes" is the honest statement of what the outage might have
    cost, and it is the only place that number ever appears.
    """
    gap = ""
    if dark_minutes >= 1:
        missed = ""
        if dark_minutes >= config.POLL_INTERVAL_SECONDS / 60:
            missed = (
                f" — roughly {dark_minutes / (config.POLL_INTERVAL_SECONDS / 60):.0f} "
                f"missed check(s)"
            )
        gap = (
            f"It was unable to read Ticketmaster for {dark_minutes:.0f} minutes"
            f"{missed}.\nA resale listing lives ten to twenty minutes, so if one "
            f"appeared in that\nwindow it was missed. Nothing can recover it now; "
            f"this is just so you know\nthe gap was there.\n\n"
        )
    why = f"What went wrong: {reason}\n\n" if reason else ""

    pushed = _push(
        "recovered-push",
        title="EP2026 watcher recovered",
        message=f"Back to normal after {after} failed checks.",
        priority="low",
        tags=["white_check_mark"],
    )
    delivered = _safe(
        "recovered-email", _send_email,
        "EP2026 watcher is working again",
        f"Hi David,\n\nThe watcher recovered after {after} failed checks and is\n"
        f"reading Ticketmaster normally again.\n\n"
        f"{gap}{why}"
        f"At: {stamp()}\n",
    )
    return delivered or pushed


def verify_email() -> tuple:
    """Log in to Gmail without sending anything. Returns (ok, detail).

    Checking that GMAIL_ADDRESS and GMAIL_APP_PASSWORD are *set* proves
    nothing — an app password revoked in six months' time looks identical
    from here, and the first thing to discover it would be a ticket alert
    that never arrived. This actually opens the connection and authenticates,
    which is every step of a real send except the message itself, so it can
    run on every doctor without filling the inbox.
    """
    if not (config.GMAIL_ADDRESS and config.GMAIL_APP_PASSWORD):
        return False, "no Gmail address or app password set — no email can be sent"
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=SEND_TIMEOUT_SECONDS) as srv:
            srv.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
        return True, f"signed in to Gmail as {config.GMAIL_ADDRESS}"
    except smtplib.SMTPAuthenticationError:
        return False, (
            "Gmail rejected the app password — generate a new one at "
            "https://myaccount.google.com/apppasswords"
        )
    except Exception as exc:
        return False, f"could not reach Gmail: {exc}"


def verify_push() -> tuple:
    """Publish a silent message and read it back. Returns (ok, detail).

    Proves the ntfy topic is live and reachable end to end, rather than
    assuming a POST that returned 200 arrived anywhere. It cannot prove the
    phone is subscribed — nothing server-side can — so the detail line says
    what was and was not established, instead of implying more than it knows.

    Priority 1 and no tags, so this never buzzes a phone that IS subscribed.
    """
    if not config.NTFY_TOPIC:
        return False, "NTFY_TOPIC not set — no push notifications at all"

    marker = f"selfcheck-{int(time.time())}"
    try:
        resp = requests.post(
            f"https://ntfy.sh/{config.NTFY_TOPIC}",
            data=marker.encode("utf-8"),
            headers={"Title": "EP2026 self-check", "Priority": "1", "Tags": "gear"},
            timeout=15,
        )
        if resp.status_code == 429:
            from . import pushquota

            pushquota.note_exhausted()
            # Named specifically, because the fix is the opposite of the one
            # printed for every other failure. A 429 means the topic is
            # correct and the quota is spent; telling David to go and check
            # NTFY_TOPIC sends him to edit a setting that is not wrong.
            return False, (
                "ntfy is rate-limiting this client (HTTP 429) — the topic is "
                "fine and the quota is spent. Push will recover on its own; "
                "email is unaffected"
            )
        if resp.status_code != 200:
            return False, f"ntfy rejected the publish (HTTP {resp.status_code})"
    except requests.RequestException as exc:
        return False, f"could not reach ntfy.sh: {exc}"

    # Read it back from the topic's cache. Not instantly: ntfy accepts the
    # publish before the message is queryable, so reading with no pause
    # reports a perfectly working topic as broken — which it did, the first
    # time this ran. Retry briefly rather than trusting one attempt.
    last = "no response"
    for delay in (1.5, 2.5, 4.0):
        time.sleep(delay)
        try:
            got = requests.get(
                f"https://ntfy.sh/{config.NTFY_TOPIC}/json",
                params={"poll": "1", "since": "120s"},
                timeout=15,
            )
            if marker in got.text:
                return True, f"published and read back from topic {config.NTFY_TOPIC}"
            last = "published, but the message did not appear in the topic"
        except requests.RequestException as exc:
            last = f"published, but read-back failed: {exc}"
    return False, last


def test() -> None:
    """Send one real example of every email the watcher can produce.

    Not just a "credentials work" ping. The alert that matters will arrive
    exactly once, under time pressure, and there is no second chance to
    discover that it went to spam or that the link in it was wrong. So this
    puts all four in the inbox now, while it costs nothing to check them.
    """
    global TEST_MODE
    TEST_MODE = True

    pages = "\n".join(f"  · {e.name}\n    {e.url}" for e in config.EVENTS)
    print(f"[{stamp()}] 1/5 connectivity")
    _send_email(
        f"{config.WATCH_LABEL} watcher is wired up",
        f"Hi David,\n\nIf you can read this, Gmail credentials work and mail is\n"
        f"reaching you. The next four are samples of the real alerts.\n\n"
        f"Watching {len(config.EVENTS)} page(s):\n{pages}\n\n"
        f"At: {stamp()}\n",
    )

    # Deliberately NOT the first configured page. Every alert used to describe
    # config.EVENT_* whatever had actually been found, so a drill built on the
    # first event proved nothing; this one is wrong the moment that regresses.
    #
    # Chosen by position until 2026-08-19, as "the last page, the instalment
    # plan". Adding the Early Entry Pass silently made EVENTS[-1] a different
    # page — the same positional assumption that had quietly broken three test
    # files the same day. Named explicitly now, with a positional fallback so
    # this still works if the pages are ever renamed.
    found_on = next(
        (e for e in config.EVENTS if e.slug == "weekend-camping-instalment"),
        config.EVENTS[-1] if len(config.EVENTS) > 1 else config.EVENTS[0],
    )

    print(f"[{stamp()}] 2/5 availability alert")
    sample = Reading(
        source="test",
        event_slug=found_on.slug,
        event_name=found_on.name,
        event_url=found_on.url,
        primary="UNAVAILABLE",
        resale="AVAILABLE",
        listings=[Listing(name="Verified Resale — Section STNDN1 (WEEKEND CAMPING)",
                          price="€366.39", kind="resale")],
    )
    available(sample, "TEST — this is what a real find looks like", [])

    print(f"[{stamp()}] 3/5 basket alert")
    # The loudest one the watcher can send, and the one that arrives with a
    # checkout timer already running. Worth seeing once in advance.
    reserved_in_browser(
        Reading(
            source="test",
            event_slug=found_on.slug,
            event_name=found_on.name,
            event_url=found_on.url,
            primary="AVAILABLE",
            listings=[Listing(name="General Admission (in basket)",
                              price="€310.50", kind="primary")],
        )
    )

    print(f"[{stamp()}] 4/5 hourly report")
    heartbeat(
        checks=12, failures=0, hours=1.0,
        reading=Reading(source="test", primary="UNAVAILABLE", resale="UNAVAILABLE"),
        # Shown exactly as the real hourly report renders it: every watched
        # page, with its own statuses and its own link.
        events=[
            (e.name, e.url, "UNAVAILABLE", "UNAVAILABLE" if i == 0 else "UNKNOWN")
            for i, e in enumerate(config.EVENTS)
        ],
        health=("ok", "No blocks in the last 24 hours — this connection looks healthy.",
                "Nothing to do."),
        net=(
            True,
            "On home Wi-Fi (86.44.208.194) — 61 searches over 6.2h.",
            "TIME TO SWITCH NETWORKS — 6.2h on this connection and 61 searches from it.\n\n"
            "  Move the MacBook from home Wi-Fi to your phone hotspot.\n\n"
            "  On the MacBook: click the Wi-Fi icon in the menu bar and pick your\n"
            "  iPhone's Personal Hotspot. (On the phone: Settings > Personal Hotspot.)\n\n"
            "  Nothing else to do. The watcher notices the new connection by itself,\n"
            "  resets its counters, and will tell you when to switch back.",
        ),
    )

    print(f"[{stamp()}] 5/5 watchdog")
    # Sent with the worst-case health block on purpose: this is the email that
    # has to be useful on the day the connection is actually in trouble, so
    # the sample should show the full set of instructions, not the calm case.
    watchdog(
        "TEST — this is what a broken watcher looks like.", failures=4,
        health=(
            "blocked",
            "8 blocks in the last hour (23 in 24h) — this connection is blocked.",
            "Act on this one:\n"
            "  1. Stop the watcher. Repeated attempts extend the block.\n"
            "  2. To browse or buy right now, switch to mobile data.\n"
            "  3. Sign in to your Ticketmaster account.\n"
            "  4. Leave it a few hours — these blocks decay on their own.\n"
            "  5. Raise EP_POLL_SECONDS before restarting.",
        ),
    )

    _send_ntfy(
        title="TEST: EP2026 watcher",
        message="Test push — ntfy is wired up.",
        tags=["test_tube"],
    )
    TEST_MODE = False
    print(f"\n  Five emails sent to {config.ALERT_TO}, each subject prefixed")
    print("  '[TEST — not real]' so you can tell them from the genuine article.")
    print("  Check they arrived AND that none landed in spam — mark them")
    print("  'not spam' now if they did, not on the day it matters.")
    print()
    print(f"  The find and basket samples are about: {found_on.name}")
    print(f"  Check they name and link THAT page — the {len(config.EVENTS)} pages are")
    print("  easily confused, and an alert pointing at the wrong one costs the")
    print("  ticket. One of them is an add-on that is worthless on its own.")
