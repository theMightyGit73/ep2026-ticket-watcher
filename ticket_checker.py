import os, sys, smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import cloudscraper

EVENT_URL  = "https://www.ticketmaster.ie/electric-picnic-2026-weekend-camping-co-laois-28-08-2026/event/18006314BD813D3E"
ALERT_TO   = "davidcoyne73@gmail.com"
GMAIL_FROM = os.environ.get("GMAIL_ADDRESS")
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD")

SOLD_OUT_MARKERS = [
    "there aren't enough tickets to complete your request",
    "no tickets are currently available",
    "this event is sold out",
]

def utc_now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

def fetch_page():
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    try:
        resp = scraper.get(EVENT_URL, timeout=30)
        resp.raise_for_status()
        return resp.text
    except Exception as exc:
        print(f"[{utc_now()}] Could not fetch page: {exc}")
        return None

def is_sold_out(html):
    lower = html.lower()
    return any(marker in lower for marker in SOLD_OUT_MARKERS)

def send_alert(is_test=False):
    msg = MIMEMultipart()
    msg["From"]    = GMAIL_FROM
    msg["To"]      = ALERT_TO
    
    if is_test:
        msg["Subject"] = "TEST: Electric Picnic 2026 Ticket Watcher"
        body = (
            "Hi David,\n\n"
            "This is a TEST email from your ticket checking script. "
            "Your email credentials and script are working perfectly!\n\n"
            f"Checked at: {utc_now()}\n"
        )
    else:
        msg["Subject"] = "Electric Picnic 2026 - Tickets may be available NOW!"
        body = (
            "Hi David,\n\n"
            "The sold-out message has DISAPPEARED from the Electric Picnic 2026 "
            "Weekend Camping page - tickets may be on sale right now!\n\n"
            f"Book here: {EVENT_URL}\n\n"
            "Move fast - these go in minutes.\n\n"
            f"Checked at: {utc_now()}\n\nGood luck!\n"
        )

    msg.attach(MIMEText(body, "plain"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
        srv.login(GMAIL_FROM, GMAIL_PASS)
        srv.send_message(msg)
    print(f"[{utc_now()}] Alert sent to {ALERT_TO}")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        print(f"[{utc_now()}] Running in TEST mode. Attempting to send test email...")
        if not GMAIL_FROM or not GMAIL_PASS:
            print("ERROR: Missing GMAIL_ADDRESS or GMAIL_APP_PASSWORD environment variables.")
            sys.exit(1)
        try:
            send_alert(is_test=True)
            print("Success! Check your inbox.")
        except Exception as exc:
            print(f"ERROR sending test email: {exc}")
            sys.exit(1)
        sys.exit(0)

    print(f"[{utc_now()}] Checking for tickets...")
    html = fetch_page()
    if html is None:
        print("Could not reach Ticketmaster - will retry next run.")
        sys.exit(0)
    if is_sold_out(html):
        print(f"[{utc_now()}] Still sold out.")
    else:
        print(f"[{utc_now()}] Tickets may be available! Sending alert...")
        try:
            send_alert()
        except Exception as exc:
            print(f"ERROR sending email: {exc}")
            sys.exit(1)
