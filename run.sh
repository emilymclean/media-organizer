#!/usr/bin/env bash
#
# run.sh - build (if needed) and run the media-organizer container.
#
# Usage:
#   ./run.sh <m|s> <tvdb_id> <mega_link> [<mega_link> ...]
#
# Provide more than one mega_link when a show's episodes are split across
# multiple Mega links/folders -- they're all downloaded and pooled together
# before organising.
#
# Examples:
#   ./run.sh m 12345 'https://mega.nz/file/xxxxx#yyyyy'
#   ./run.sh s 78910 'https://mega.nz/folder/xxxxx#yyyyy'
#   ./run.sh s 78910 'https://mega.nz/folder/aaa#bbb' 'https://mega.nz/folder/ccc#ddd'
#
# Configuration (env vars, or put them in a .env file next to this script):
#   TVDB_API_KEY      Required. Your TVDB v4 API key.
#   TVDB_PIN          Optional. TVDB subscriber PIN.
#   MEGA_EMAIL        Optional. MEGA account email (else anonymous downloads).
#   MEGA_PASSWORD     Optional. MEGA account password.
#   MEGA_KEEP_SESSION Optional. "true" to keep the MEGAcmd session logged in
#                     across runs (requires MEGA_CONFIG_DIR to persist).
#   LIBRARY_DIR       Optional. Host directory organised media is written to.
#                     Default: ./library
#   MEGA_CONFIG_DIR   Optional. Host directory for MEGAcmd session/config
#                     persistence. Default: ./mega-config
#   IMAGE_NAME        Optional. Docker image tag. Default: media-organizer
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load a .env file if present (simple KEY=VALUE lines, no quoting needed).
if [[ -f "$SCRIPT_DIR/.env" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$SCRIPT_DIR/.env"
    set +a
fi

if [[ $# -lt 3 ]]; then
    echo "Usage: $0 <m|s> <tvdb_id> <mega_link> [<mega_link> ...]" >&2
    exit 1
fi

IMAGE_NAME="${IMAGE_NAME:-media-organizer}"
LIBRARY_DIR="${LIBRARY_DIR:-$SCRIPT_DIR/library}"
MEGA_CONFIG_DIR="${MEGA_CONFIG_DIR:-$SCRIPT_DIR/mega-config}"

mkdir -p "$LIBRARY_DIR" "$MEGA_CONFIG_DIR"

if [[ -z "${TVDB_API_KEY:-}" ]]; then
    echo "Warning: TVDB_API_KEY is not set (env var or .env file)." >&2
    echo "         The TVDB lookup will fail without it." >&2
fi

echo "Building image '${IMAGE_NAME}'..."
docker build -t "$IMAGE_NAME" "$SCRIPT_DIR"

echo "Running container..."
docker run --rm -it \
    -e MEGA_EMAIL="${MEGA_EMAIL:-}" \
    -e MEGA_PASSWORD="${MEGA_PASSWORD:-}" \
    -e MEGA_KEEP_SESSION="${MEGA_KEEP_SESSION:-false}" \
    -e TVDB_API_KEY="${TVDB_API_KEY:-}" \
    -e TVDB_PIN="${TVDB_PIN:-}" \
    -v "$LIBRARY_DIR:/library" \
    -v "$MEGA_CONFIG_DIR:/config" \
    "$IMAGE_NAME" \
    "$@" --library-root /library