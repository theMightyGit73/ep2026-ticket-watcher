# Full-capability watcher (real Chrome) on a Linux host.
#
# The key detail: this runs Chrome *headed*, under Xvfb — a virtual display —
# rather than in Chrome's headless mode. That distinction is the whole point.
# Measured against ticketmaster.ie: headless Chrome gets HTTP 403 every time,
# headed Chrome gets 200. Under Xvfb, Chrome is a completely ordinary headed
# browser that happens to be drawing to a display nobody is looking at.
#
# The remaining unknown is the IP. The headed-Chrome result was measured from
# a residential connection; this container will run from a datacentre range,
# which is exactly what bot detection weights most heavily. It may work, it
# may not. Run `check` once after deploying and you will know in 60 seconds.
#
#   docker build -t ep-watcher -f deploy/Dockerfile .
#   docker run --rm --env-file ~/.ep2026-watcher/env ep-watcher check

FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# Google Chrome proper, not Chromium: Playwright's channel="chrome" uses the
# same build a human would, which is one less thing that looks unusual.
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget gnupg ca-certificates xvfb fonts-liberation \
    && wget -q -O /tmp/chrome.deb \
        https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y --no-install-recommends /tmp/chrome.deb \
    && rm -rf /tmp/chrome.deb /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ep_watcher/ ./ep_watcher/

# Profile and state live on a volume so the cookies that got through the bot
# check — and the alert history — survive a container restart.
ENV EP_PROFILE_DIR=/data/chrome-profile \
    EP_STATE_FILE=/data/state.json \
    EP_DIAG_DIR=/data/diagnostics \
    EP_HEADLESS=0 \
    EP_OFFSCREEN=0
VOLUME /data

# xvfb-run gives Chrome a display to draw into. Without it a headed Chrome
# cannot start at all on a machine with no window server.
ENTRYPOINT ["xvfb-run", "-a", "--server-args=-screen 0 1440x900x24", \
            "python", "-m", "ep_watcher"]
CMD ["watch"]
