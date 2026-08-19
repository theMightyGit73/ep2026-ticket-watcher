"""Automated sign-in: the password must not leak, and a challenge must stop it.

Opt-in, on the `automated-login` branch only. `login-buy` — a human signing in
once, cookies persisting — remains the supported path and needs no password to
exist anywhere.

David asked for this on 2026-08-19 for a secondary Yahoo account he described
as disposable, having been told that scripted sign-ins are what Ticketmaster's
account security is built to catch. That trade is his to make. What is not
negotiable is the handling: the password comes from the environment, is never
written to a log, a note, an exception or a commit, and a challenge is never
retried — a script hammering a human check is how a locked account becomes a
banned one.

Every selector in autologin.py is a guess. Nobody has driven this flow, exactly
as nobody had driven the resale purchase flow — where every guess turned out
wrong the moment a real page arrived. These checks pin the behaviour that must
hold whether or not the selectors are right.

Run with:  .venv/bin/python tests/test_autologin.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ep_watcher import autologin, config  # noqa: E402

failures = []

SECRET = "a-very-distinctive-not-real-password-9137"
EMAIL = "someone@example.com"


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


class FakeElement:
    def __init__(self, page, kind, visible=True):
        self.page, self.kind, self.visible = page, kind, visible
        self.filled = None

    def is_visible(self, timeout=None):
        return self.visible

    def fill(self, value, timeout=None):
        self.filled = value
        self.page.filled[self.kind] = value


class FakeButton:
    def __init__(self, page, label):
        self.page, self.label = page, label

    def is_visible(self, timeout=None):
        return bool(self.label)

    def inner_text(self, timeout=None):
        return self.label

    def click(self, timeout=None):
        self.page.clicks.append(self.label)
        self.page.advance()


class _First:
    def __init__(self, element):
        self.first = element


class FakePage:
    """An identity page that can be one-step, two-step, or a challenge.

    `button` is the label of the form's submit control, and an empty string
    means the page has no usable one. That distinction matters since the
    submit is found by `button[type=submit]` rather than by label: any
    non-empty label is now a working button, so "no form" has to be modelled
    by the absence of one rather than by an unrecognised name.
    """

    def __init__(self, body="", has_password=True, button="sign in"):
        self.body, self.has_password, self.button = body, has_password, button
        self.filled, self.clicks, self.visited = {}, [], []
        self.keys = []

    def advance(self):
        self.has_password = True      # a two-step form reveals it after submit

    def goto(self, url, wait_until=None):
        self.visited.append(url)

    def inner_text(self, _sel):
        return self.body

    def locator(self, selector):
        lowered = selector.lower()
        # The real page's submit control is a button[type=submit] labelled
        # "Continue", sitting beside a "Sign In With A Passkey" button that is
        # NOT type=submit. Modelled here because targeting the submit
        # attribute rather than the label is what stopped the passkey button
        # being clicked — see NEVER_CLICK.
        if "button" in lowered and "submit" in lowered:
            return _First(FakeButton(self, self.button))
        if "password" in lowered:
            return _First(FakeElement(self, "password", visible=self.has_password))
        return _First(FakeElement(self, "email"))

    def get_by_role(self, role, name=None, exact=False):
        # Returns the button's REAL label, not the query that found it. That
        # distinction is the whole point: Playwright's substring match finds
        # "Sign In With A Passkey" when asked for "sign in", and the guard can
        # only refuse it if it sees what the button actually says. A fake that
        # echoed the query back made the trap untestable.
        if name and name.lower() in self.button.lower():
            return _First(FakeButton(self, self.button))
        return _First(FakeElement(self, "none", visible=False))

    @property
    def keyboard(self):
        page = self

        class _KB:
            @staticmethod
            def press(key):
                page.keys.append(key)

        return _KB()


class FakeSession:
    def __init__(self, page):
        self._page = page

    @property
    def page(self):
        return self._page

    def _dismiss_consent(self):
        return True


def with_credentials(email=EMAIL, password=SECRET):
    config.TM_EMAIL, config.TM_PASSWORD = email, password


def without_credentials():
    config.TM_EMAIL, config.TM_PASSWORD = "", ""


def leaked(result) -> bool:
    """Did the password reach anything a human or a log file will ever see?"""
    haystack = " ".join(result.notes) + " " + (result.reason or "") + " " + result.outcome
    return SECRET in haystack


print("\nWithout credentials it refuses, and says where they belong")
without_credentials()
r = autologin.sign_in(FakeSession(FakePage()))
check("outcome", r.outcome, "error")
check("not signed in", r.signed_in, False)
check_true("names the env file", "ep2026-watcher/env" in r.reason)
check_true("warns against the command line", "command line" in r.reason)

print("\nA one-step form is filled and submitted")
with_credentials()
page = FakePage(has_password=True)
r = autologin.sign_in(FakeSession(page))
check("the email is entered", page.filled.get("email"), EMAIL)
check("the password is entered", page.filled.get("password"), SECRET)
check_true("something was submitted", page.clicks or page.keys)
check("outcome", r.outcome, "ok")

print("\nA two-step form submits the email first, then the password")
page = FakePage(has_password=False)
r = autologin.sign_in(FakeSession(page))
check("the email still lands", page.filled.get("email"), EMAIL)
check("and so does the password once revealed", page.filled.get("password"), SECRET)
check_true("the form was submitted more than once", len(page.clicks) >= 2)
check("outcome", r.outcome, "ok")

print("\nThe password must never reach a note, a reason, or an exception")
# The whole reason a password may be handled here at all is that it stays
# handled. A log line is forever and gets pasted into chats.
for scenario, page in (
    ("one-step", FakePage(has_password=True)),
    ("two-step", FakePage(has_password=False)),
    ("challenge", FakePage(body="Please complete the CAPTCHA to continue")),
    ("rejected", FakePage(body="That password is incorrect")),
    ("no form", FakePage(has_password=False, button="")),
):
    r = autologin.sign_in(FakeSession(page))
    check(f"[{scenario}] the password does not appear anywhere", leaked(r), False)

# And no source file may carry a credential as a literal, however convenient.
#
# Checked by shape rather than by value. Asserting `"<the real password>" not
# in source` would put the real password — or a recognisable piece of it — in
# this file, which is the exact thing being prevented, and this file is
# committed. So the rule is that the only place these values are ever read is
# os.environ, and nothing assigns them anything else.
import re  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
source = (ROOT / "ep_watcher" / "autologin.py").read_text()
config_source = (ROOT / "ep_watcher" / "config.py").read_text()

check_true("the password is read from config, not passed around",
           "config.TM_PASSWORD" in source)
check_true("credentials come from the environment",
           'os.environ.get("TM_PASSWORD"' in config_source)
for name, text in (("autologin.py", source), ("config.py", config_source),
                   ("__main__.py", (ROOT / "ep_watcher" / "__main__.py").read_text())):
    # TM_EMAIL / TM_PASSWORD may only ever be assigned from os.environ.
    assignments = re.findall(r"^\s*TM_(?:EMAIL|PASSWORD)\s*=\s*(.+)$", text, re.M)
    check(f"[{name}] every credential assignment reads the environment",
          [a for a in assignments if "os.environ" not in a], [])
    # No hardcoded mailbox that could be a Ticketmaster login identity.
    #
    # The alert DESTINATION is deliberately in config.py and has been since
    # the project started — that is where the watcher sends mail, not an
    # account it signs into. Excluded by name so this check stays about the
    # thing it is for: the sign-in identity must come from the environment.
    allowed = {config.ALERT_TO, config.GMAIL_ADDRESS, "davidcoyne73@gmail.com"}
    mailboxes = [m for m in re.findall(r"['\"]([^'\"]+@[^'\"]+\.[a-z]{2,})['\"]", text)
                 if "example" not in m and m not in allowed]
    check(f"[{name}] no login identity is hardcoded", mailboxes, [])

print("\nThe passkey button beside Continue must never be clicked")
# The real page, dumped on 2026-08-19, carries two buttons: "Continue"
# (type=submit) and "Sign In With A Passkey" (type=button). Trying "sign in"
# first matched the passkey one on a substring and clicked it, and the run
# died reporting "no password field appeared" while three steps into a
# passkey flow nobody wanted.
check_true("a passkey button is refused", autologin._never_click("Sign In With A Passkey"))
check_true("and so is 'Sign in with Google'", autologin._never_click("Sign in with Google"))
check_true("create-account is refused", autologin._never_click("Create Account"))
check_true("forgot-password is refused", autologin._never_click("Forgot password?"))
check("but plain Continue is fine", autologin._never_click("Continue"), False)
check("and plain Sign In is fine", autologin._never_click("Sign In"), False)
# Order matters: continue before sign in, so the real submit wins a tie.
check_true("continue is tried before sign in",
           autologin.SUBMIT_LABELS.index("continue") < autologin.SUBMIT_LABELS.index("sign in"))

with_credentials()
trap = FakePage(has_password=False, button="Sign In With A Passkey")
r = autologin.sign_in(FakeSession(trap))
check("the passkey button is not pressed", trap.clicks, [])
check("and the attempt reports honestly", r.outcome, "no-form")


print("\nA human check stops it dead — it is never retried")
# Repeating a challenge is how a locked account becomes a banned one.
for wording in ("Please complete the CAPTCHA",
                "We sent a code to your phone",
                "Unusual activity detected on this account",
                "Enter your verification code",
                "Two-factor authentication required"):
    page = FakePage(body=wording)
    r = autologin.sign_in(FakeSession(page))
    check(f"[{wording[:28]}...] recognised as a challenge", r.outcome, "challenged")
    check(f"[{wording[:28]}...] not claimed as signed in", r.signed_in, False)
check_true("and it says to finish by hand", "login-buy" in r.reason)

print("\nBad credentials are reported as bad credentials, not as a challenge")
page = FakePage(body="Sorry, that email or password is incorrect")
r = autologin.sign_in(FakeSession(page))
check("outcome", r.outcome, "rejected")
check_true("and points at the env file", "ep2026-watcher/env" in r.reason)

print("\nA page it cannot read is reported honestly, not guessed at")
page = FakePage(has_password=False, button="")
r = autologin.sign_in(FakeSession(page))
check("outcome", r.outcome, "no-form")
check("not claimed as signed in", r.signed_in, False)


class ExplodingPage(FakePage):
    def locator(self, selector):
        raise RuntimeError("the page went away")


r = autologin.sign_in(FakeSession(ExplodingPage()))
check("a thrown page is an error, not a crash", r.outcome, "no-form")
check("still not signed in", r.signed_in, False)

print("\nSuccess is never claimed from the page's own say-so")
# Ticketmaster renders no account text Playwright can read — checked against
# nine real captures on 2026-08-19. Only cookies decide, which is why
# sign_in() reports "ok" for "the form went through" and the CALLER checks
# the profile before telling David anything.
page = FakePage(has_password=True)
r = autologin.sign_in(FakeSession(page))
check("sign_in never sets signed_in itself", r.signed_in, False)
main_source = (Path(__file__).resolve().parent.parent
               / "ep_watcher" / "__main__.py").read_text()
check_true("the command verifies via the cookie fingerprint",
           "record_signed_in_fingerprint" in main_source)

without_credentials()
print("\nTelling 'your password is wrong' apart from 'something went wrong'")
# The cost of confusing these is specific: a false "rejected" tells David to
# go and change a password that was never the problem, and the honest thing
# to do with that advice is to rotate a working credential by hand.
#
# "try again" used to be on REJECTED_MARKERS and is the tail of half the
# transient errors a website can produce.
REJECTIONS = (
    "The email or password you entered is incorrect.",
    "Invalid email or password.",
    "That password does not match our records.",
    "We cannot find an account with that email.",
)
NOT_REJECTIONS = (
    "Something went wrong. Please try again.",
    "We are having trouble right now — try again in a few minutes.",
    "Sorry, this page is temporarily unavailable.",
)
for text in REJECTIONS:
    check_true(f"rejected: {text[:38]!r}",
               any(m in text.lower() for m in autologin.REJECTED_MARKERS))
for text in NOT_REJECTIONS:
    check(f"not a rejection: {text[:38]!r}",
          any(m in text.lower() for m in autologin.REJECTED_MARKERS), False)

print("\nA challenge is never reported as a bad password")
# They call for opposite actions: one needs a human at the keyboard, the
# other needs a new credential. sign_in() checks challenge markers first, so
# the two sets must not overlap on the phrases a challenge page carries.
CHALLENGES = (
    "Please verify you are human to continue.",
    "We sent a code to your email. Enter the verification code below.",
    "Unusual activity detected on this account.",
)
for text in CHALLENGES:
    check_true(f"challenge: {text[:38]!r}",
               any(m in text.lower() for m in autologin.CHALLENGE_MARKERS))
    check(f"and not also a rejection: {text[:30]!r}",
          any(m in text.lower() for m in autologin.REJECTED_MARKERS), False)


print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
