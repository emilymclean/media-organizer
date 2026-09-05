#!/usr/bin/env bash
set -uo pipefail

did_login=false

cleanup() {
    if [[ "$did_login" == "true" && "${MEGA_KEEP_SESSION:-false}" != "true" ]]; then
        echo "[bootstrap] Logging out of MEGA..."
        mega-logout >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

mega-cmd-server > /dev/null 2>&1 &

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

echo "[bootstrap] Running app..."
gunicorn -w 1 -b 0.0.0.0:5000 app:app "$@"
status=$?

echo "[bootstrap] app.py exited with status ${status}."
exit "$status"