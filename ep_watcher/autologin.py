"""Sign the buying profile in without a human at the keyboard.

Opt-in, on its own branch, and not the default. `login-buy` — where David
signs in by hand once and the cookies persist — remains the supported path and
needs no password to exist anywhere.

Why this exists anyway
----------------------
He asked for it on 2026-08-19, for a secondary Yahoo account he described as
disposable: "if I lose this account it means nothing to me anyway". That
answers the objection that actually mattered. Scripted sign-ins are what
Ticketmaster's account security is built to catch, and the realistic cost of
being caught is a locked account — which is a serious cost for the account you
buy with, and a trivial one for an account you would shrug at losing.

What has NOT changed is where the password lives. It comes from the
environment, which run_watcher.sh sources from ~/.ep2026-watcher/env (chmod
600). Never a literal in this file, never a commit, never a command-line
argument — arguments are visible in `ps` to every process on the machine.

What this can and cannot do
---------------------------
It fills the email and password fields Ticketmaster's identity page presents
and submits them. It cannot answer a captcha, a two-factor prompt, or an
"unusual activity" challenge, and it does not try: each of those is reported
by name and left for a human, because a script hammering a challenge is how a
locked account becomes a banned one.

Every selector here is a guess. Nobody has driven this flow, the same way
nobody had driven the resale purchase flow — and every single guess in that
module turned out wrong when a real page finally arrived. Expect the same.
The flow is written so a wrong guess reports which field it could not find
rather than failing silently.
"""

import time
from dataclasses import dataclass, field
from typing import List, Optional

from . import config
from .state import stamp

#: How the email field might be labelled. Tried in order.
EMAIL_SELECTORS = (
    "input[type='email']",
    "input[name='email']",
    "input[id*='email' i]",
    "input[autocomplete='username']",
    "input[name='username']",
)

#: And the password field.
PASSWORD_SELECTORS = (
    "input[type='password']",
    "input[name='password']",
    "input[id*='password' i]",
    "input[autocomplete='current-password']",
)

#: Buttons that move the form forward.
#:
#: Order matters, and the first version had it backwards. The real page —
#: dumped on 2026-08-19 — is a two-step form whose submit control is labelled
#: "Continue", sitting beside a second button labelled "Sign In With A
#: Passkey". Trying "sign in" first matched the passkey button on a substring,
#: clicked it, and the run died with "no password field appeared" while
#: actually being three steps into a passkey flow nobody wanted. Exactly the
#: same class of mistake as "Find More Tickets" on the resale dead end.
#:
#: `button[type=submit]` is tried before any of these anyway, because the real
#: Continue button carries it and the passkey button does not. Labels are the
#: fallback for a page that does not mark its submit properly.
SUBMIT_LABELS = ("continue", "next", "log in", "sign in", "submit")

#: Never clicked, whatever else matches. A passkey flow cannot be completed
#: with a stored password and only wastes the attempt.
NEVER_CLICK = ("passkey", "sign in with", "create account", "forgot")

#: Text that means a human is needed. Checked before anything is called a
#: failure, because "wrong password" and "prove you are human" are different
#: problems and only one of them is worth retrying.
CHALLENGE_MARKERS = (
    "captcha",
    "verify you are human",
    "unusual activity",
    "verification code",
    "two-factor",
    "authentication code",
    "we sent a code",
    "security check",
)

#: Text that means the credentials were rejected.
REJECTED_MARKERS = (
    "incorrect",
    "does not match",
    "invalid email or password",
    "we cannot find an account",
    "try again",
)


@dataclass
class LoginResult:
    """What came of one automated sign-in attempt."""

    signed_in: bool = False
    #: "ok" | "challenged" | "rejected" | "no-form" | "error"
    outcome: str = "error"
    reason: str = ""
    notes: List[str] = field(default_factory=list)

    def note(self, text: str) -> None:
        self.notes.append(text)
        print(f"    [autologin] {text}")


def _first_visible(page, selectors, timeout_ms: int = 4000):
    """The first of these selectors that is actually on the page."""
    for selector in selectors:
        try:
            element = page.locator(selector).first
            if element.is_visible(timeout=timeout_ms):
                return element, selector
        except Exception:
            continue
    return None, ""


def _never_click(label: str) -> bool:
    lowered = (label or "").strip().lower()
    return any(bad in lowered for bad in NEVER_CLICK)


def _press_submit(page, result: LoginResult) -> bool:
    # The form's own submit control first. On the real page this is the
    # "Continue" button and it carries type=submit, while the passkey button
    # beside it does not — so this one selector avoids the whole class of
    # label-matching mistakes.
    try:
        submit = page.locator("button[type='submit']").first
        if submit.is_visible(timeout=2000):
            label = (submit.inner_text(timeout=1500) or "").strip()
            if not _never_click(label):
                submit.click(timeout=5000)
                result.note(f"pressed the form's submit button ({label!r})")
                return True
            result.note(f"refusing to press {label!r}")
    except Exception:
        pass

    for label in SUBMIT_LABELS:
        try:
            button = page.get_by_role("button", name=label, exact=False).first
            if not button.is_visible(timeout=1500):
                continue
            actual = (button.inner_text(timeout=1500) or "").strip()
            if _never_click(actual):
                result.note(f"refusing to press {actual!r}")
                continue
            button.click(timeout=5000)
            result.note(f"pressed {actual!r}")
            return True
        except Exception:
            continue
    # Some identity forms submit on Enter and have no visible button.
    try:
        page.keyboard.press("Enter")
        result.note("no submit button found — pressed Enter instead")
        return True
    except Exception:
        return False


def _page_text(page) -> str:
    try:
        return (page.inner_text("body") or "").lower()
    except Exception:
        return ""


def sign_in(session, result: LoginResult = None) -> LoginResult:
    """Fill in the sign-in form on an already-open session. Never raises.

    `session` must be started and sitting on, or able to reach, the identity
    page. Returns rather than throws so the caller can report what happened;
    a login failure is information, not a crash.
    """
    result = result or LoginResult()

    if not config.have_login_credentials():
        result.outcome = "error"
        result.reason = (
            "TM_EMAIL and TM_PASSWORD are not set. Put them in "
            "~/.ep2026-watcher/env (chmod 600) — never on the command line."
        )
        result.note(result.reason)
        return result

    try:
        page = session.page

        for candidate in config.SIGNIN_URLS:
            try:
                page.goto(candidate, wait_until="domcontentloaded")
                result.note(f"opened {candidate}")
                break
            except Exception:
                continue

        # The cookie wall blocks the form underneath it, exactly as it blocks
        # the bot check on the event page.
        try:
            session._dismiss_consent()
        except Exception:
            pass

        email_field, used = _first_visible(page, EMAIL_SELECTORS)
        if email_field is None:
            result.outcome = "no-form"
            result.reason = (
                "no email field found on the sign-in page — the selectors here "
                "are guesses and this is the likeliest one to be wrong"
            )
            result.note(result.reason)
            return result
        result.note(f"email field found via {used}")
        email_field.fill(config.TM_EMAIL, timeout=5000)

        # The password field may already be present, or may only appear after
        # the email is submitted. Look first, submit only if it is absent.
        password_field, used = _first_visible(page, PASSWORD_SELECTORS, timeout_ms=1500)
        if password_field is None:
            result.note("password field not shown yet — submitting the email first")
            _press_submit(page, result)
            time.sleep(2.5)
            password_field, used = _first_visible(page, PASSWORD_SELECTORS)

        if password_field is None:
            text = _page_text(page)
            if any(marker in text for marker in CHALLENGE_MARKERS):
                result.outcome = "challenged"
                result.reason = (
                    "Ticketmaster asked for a human check before the password "
                    "step — finish this one by hand with `login-buy`"
                )
            else:
                result.outcome = "no-form"
                result.reason = "no password field appeared after the email"
            result.note(result.reason)
            return result

        result.note(f"password field found via {used}")
        # Never logged, never printed, never stored.
        password_field.fill(config.TM_PASSWORD, timeout=5000)
        _press_submit(page, result)
        time.sleep(4)

        text = _page_text(page)
        if any(marker in text for marker in CHALLENGE_MARKERS):
            result.outcome = "challenged"
            result.reason = (
                "Ticketmaster wants a code or a human check. NOT retrying — "
                "repeating a challenge is how a locked account becomes a "
                "banned one. Finish this sign-in by hand with `login-buy`."
            )
            result.note(result.reason)
            return result

        if any(marker in text for marker in REJECTED_MARKERS):
            result.outcome = "rejected"
            result.reason = (
                "the credentials were refused — check TM_EMAIL and TM_PASSWORD "
                "in ~/.ep2026-watcher/env"
            )
            result.note(result.reason)
            return result

        # The only answer worth trusting: did cookies actually appear? The
        # page cannot be read for this — Ticketmaster renders no account text
        # Playwright can see, which is why session_evidence() exists.
        result.outcome = "ok"
        result.note("form submitted with no challenge and no rejection")
        return result

    except Exception as exc:
        result.outcome = "error"
        result.reason = f"{type(exc).__name__}: {exc}"
        result.note(f"sign-in attempt failed — {result.reason}")
        return result
