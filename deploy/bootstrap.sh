#!/bin/bash
# Bare Ubuntu/Debian server -> running watcher, in one command.
#
#   curl -fsSL https://raw.githubusercontent.com/theMightyGit73/ep2026-ticket-watcher/main/deploy/bootstrap.sh | bash
#
# or, having cloned the repo:  bash deploy/bootstrap.sh
#
# It stops before starting the service and runs a single `check` first,
# because that check answers the one question that decides whether this host
# is usable at all: does ticketmaster.ie serve a datacentre IP, or block it?
# Starting a watcher that can never read anything just burns a machine and
# fills your inbox with failure reports.
set -euo pipefail

REPO_URL="https://github.com/theMightyGit73/ep2026-ticket-watcher.git"
APP_DIR="${APP_DIR:-$HOME/ep2026-ticket-watcher}"
ENV_FILE="$HOME/.ep2026-watcher/env"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

# ── Architecture check, before anything expensive ────────────────────────────
ARCH="$(uname -m)"
if [ "$ARCH" != "x86_64" ]; then
    say "WARNING: this host is $ARCH, not x86_64"
    cat <<'EOF'
  Google Chrome for Linux is amd64-only. On ARM this falls back to Chromium,
  which ticketmaster.ie is expected to block — the whole approach depends on
  being a real Chrome. This is why Oracle's free Ampere tier does not suit.

  Continue anyway? It will build and run, and probably get blocked.
EOF
    # Read from the terminal, not stdin: this script is usually run as
    # `curl ... | bash`, where stdin is the pipe carrying the script itself.
    # A plain `read` there consumes the script's own remaining lines.
    if [ -r /dev/tty ]; then
        read -rp "  Type 'yes' to continue: " reply < /dev/tty
    else
        reply="no"
    fi
    [ "$reply" = "yes" ] || { echo "  Stopping. Use an x86-64 host."; exit 1; }
fi

# ── Base packages ────────────────────────────────────────────────────────────
# Minimal cloud images routinely ship without git.
if ! command -v git >/dev/null 2>&1; then
    say "Installing git"
    sudo apt-get update -qq
    sudo apt-get install -y -qq git ca-certificates curl
fi

# ── Docker ───────────────────────────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
    say "Installing Docker"
    curl -fsSL https://get.docker.com | sudo sh
    sudo usermod -aG docker "$USER" || true
    echo "  (You may need to log out and back in for group membership.)"
fi

DOCKER="docker"
docker info >/dev/null 2>&1 || DOCKER="sudo docker"

# ── Code ─────────────────────────────────────────────────────────────────────
if [ -d "$APP_DIR/.git" ]; then
    say "Updating $APP_DIR"
    git -C "$APP_DIR" pull --ff-only
else
    say "Cloning into $APP_DIR"
    git clone --depth 20 "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"

# ── Secrets ──────────────────────────────────────────────────────────────────
mkdir -p "$(dirname "$ENV_FILE")"
if [ ! -f "$ENV_FILE" ]; then
    say "Creating $ENV_FILE"
    cat > "$ENV_FILE" <<'EOF'
GMAIL_ADDRESS=davidcoyne73@gmail.com
GMAIL_APP_PASSWORD=
NTFY_TOPIC=
TM_DISCOVERY_KEY=
EOF
    chmod 600 "$ENV_FILE"
    echo "  Put your Gmail app password in $ENV_FILE, then re-run this script."
    exit 1
fi

# shellcheck disable=SC1090
if ! grep -q '^GMAIL_APP_PASSWORD=.\+' "$ENV_FILE"; then
    say "GMAIL_APP_PASSWORD is empty in $ENV_FILE"
    echo "  Without it nothing can reach you. Fill it in and re-run."
    exit 1
fi

# ── Build ────────────────────────────────────────────────────────────────────
say "Building the image (a few minutes: it installs Chrome)"
$DOCKER build -t ep-watcher -f deploy/Dockerfile .

# ── The decisive test ────────────────────────────────────────────────────────
say "Testing whether this host can read the page at all"
cat <<'EOF'
  Watch the result below.

    "nothing available" / a real reading  -> this host works. Start it.
    "HTTP 403 ... rate-limited"           -> this IP is blocked. Try another
                                             provider, or a residential proxy.
EOF

set +e
$DOCKER run --rm --shm-size=1g --env-file "$ENV_FILE" \
    -v ep-watcher-data:/data ep-watcher check
RESULT=$?
set -e

echo
if [ $RESULT -eq 0 ]; then
    say "This host works. Start the watcher with:"
    echo "    cd $APP_DIR && $DOCKER compose -f deploy/docker-compose.yml up -d"
    echo "    $DOCKER compose -f deploy/docker-compose.yml logs -f"
else
    say "This host could not read the page"
    echo "  Do NOT start the watcher here — it would fail every poll and email"
    echo "  you about it. Try a different provider before committing to one."
fi
