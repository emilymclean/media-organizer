#!/usr/bin/env bash
#
# bootstrap.sh - container entrypoint.
#
# If MEGA_EMAIL and MEGA_PASSWORD are both set, logs into MEGAcmd before
# handing off to media_organizer.py. The first mega-* command run below
# will transparently spawn mega-cmd-server in the background if it isn't
# already running, so nothing needs to be started explicitly.
#
# Env vars:
#   MEGA_EMAIL, MEGA_PASSWORD   Optional MEGA account credentials.
#   MEGA_KEEP_SESSION           If "true", don't log out when the run
#                               finishes (useful if $HOME/.megaCmd is a
#                               mounted volume and you want the session to
#                               persist across container runs). Default: false.
#
# All other arguments are passed straight through to media_organizer.py,
# e.g.: m <mega_link> <tvdb_id> --library-root /library
#
set -uo pipefail

did_login=false

cleanup() {
    if [[ "$did_login" == "true" && "${MEGA_KEEP_SESSION:-false}" != "true" ]]; then
        echo "[bootstrap] Logging out of MEGA..."
        mega-logout >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

if [[ -n "${MEGA_EMAIL:-}" && -n "${MEGA_PASSWORD:-}" ]]; then
    echo "[bootstrap] MEGA_EMAIL set; checking MEGAcmd session..."

    current_account="$(mega-whoami 2>/dev/null || true)"
    if echo "$current_account" | grep -qi "${MEGA_EMAIL}"; then
        echo "[bootstrap] Already logged in as ${MEGA_EMAIL}; skipping login."
    else
        echo "[bootstrap] Logging into MEGA as ${MEGA_EMAIL}..."
        if mega-login "${MEGA_EMAIL}" "${MEGA_PASSWORD}"; then
            did_login=true
            echo "[bootstrap] Login successful."
        else
            echo "[bootstrap] ERROR: MEGA login failed. Check MEGA_EMAIL/MEGA_PASSWORD." >&2
            exit 1
        fi
    fi
else
    echo "[bootstrap] MEGA_EMAIL/MEGA_PASSWORD not set; MEGA downloads will run anonymously."
    echo "[bootstrap] (This is fine for public Mega links.)"
fi

echo "[bootstrap] Running media organizer..."
python3 /app/media_organizer.py "$@"
status=$?

echo "[bootstrap] media_organizer.py exited with status ${status}."
exit "$status"