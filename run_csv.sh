#!/usr/bin/env bash
#
# run_csv.sh - build (if needed) and run a CSV batch through the
# media-organizer container.
#
# Usage:
#   ./run_csv.sh media.csv
#
# CSV format (with a header row):
#   mode,id,mega_link
#   m,12345,https://mega.nz/file/xxxxx#yyyyy
#   s,78910,https://mega.nz/folder/xxxxx#yyyyy
#
# Repeat mode/id rows to combine multiple Mega links into one request.
# See run.sh for environment variable configuration.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -f "$SCRIPT_DIR/.env" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$SCRIPT_DIR/.env"
    set +a
fi

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 <csv_file>" >&2
    exit 1
fi

CSV_FILE="$1"
if [[ ! -f "$CSV_FILE" ]]; then
    echo "CSV file not found: $CSV_FILE" >&2
    exit 1
fi

CSV_FILE="$(cd "$(dirname "$CSV_FILE")" && pwd)/$(basename "$CSV_FILE")"
IMAGE_NAME="${IMAGE_NAME:-media-organizer}"
LIBRARY_DIR="${LIBRARY_DIR:-$SCRIPT_DIR/library}"

mkdir -p "$LIBRARY_DIR"

if [[ -z "${TVDB_API_KEY:-}" ]]; then
    echo "Warning: TVDB_API_KEY is not set (env var or .env file)." >&2
    echo "         The TVDB lookup will fail without it." >&2
fi

echo "Building image '${IMAGE_NAME}'..."
docker build -t "$IMAGE_NAME" "$SCRIPT_DIR"

echo "Running CSV batch..."
docker run --rm -it \
    -e MEGA_EMAIL="${MEGA_EMAIL:-}" \
    -e MEGA_PASSWORD="${MEGA_PASSWORD:-}" \
    -e TVDB_API_KEY="${TVDB_API_KEY:-}" \
    -e TVDB_PIN="${TVDB_PIN:-}" \
    -v "$LIBRARY_DIR:/library" \
    -v "$CSV_FILE:/input/media.csv" \
    "$IMAGE_NAME" \
    --csv /input/media.csv --library-root /library
