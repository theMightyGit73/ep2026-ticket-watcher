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
#
# ARCHITECTURE MATTERS HERE. Google ships Chrome for Linux on amd64 only —
# there is no arm64 build. So an ARM host (Oracle's Ampere free tier, AWS
# Graviton, an Apple-silicon `docker build`) can only run Chromium, which is
# a different fingerprint from the browser this was verified against, and
# Chromium-flavoured browsers are exactly what this site rejects. The build
# still works on ARM so you can experiment, but it warns, and you should
# expect it to be blocked. Use an x86-64 host.
# xauth is not optional despite not looking like a dependency: xvfb-run shells
# out to it to create the X authority file, and without it fails with
# "xauth command not found" before Chrome is ever launched. It is not pulled
# in by the xvfb package itself.
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget gnupg ca-certificates xvfb xauth fonts-liberation \
    && ARCH="$(dpkg --print-architecture)" \
    && if [ "$ARCH" = "amd64" ]; then \
           wget -q -O /tmp/chrome.deb \
               https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
        && apt-get install -y --no-install-recommends /tmp/chrome.deb \
        && rm -f /tmp/chrome.deb \
        && echo "chrome" > /etc/ep-browser-channel; \
       else \
           echo "=======================================================" >&2; \
           echo " WARNING: $ARCH is not amd64. Google Chrome is amd64-" >&2; \
           echo " only, so this image falls back to Chromium, which"    >&2; \
           echo " ticketmaster.ie is expected to block. Use x86-64."     >&2; \
           echo "=======================================================" >&2; \
           apt-get install -y --no-install-recommends chromium \
        && echo "chromium" > /etc/ep-browser-channel; \
       fi \
    && rm -rf /var/lib/apt/lists/*

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

COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["watch"]
