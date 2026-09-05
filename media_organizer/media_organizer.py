#!/usr/bin/env python3
"""
media_organizer.py

Download a movie or TV show from a Mega.nz link, then rename and organise
the resulting file(s) into a standard media-server-friendly folder layout:

    Movies:
        <Title> (<Year>) [tvdbid-<id>]/
            <Title> (<Year>) [tvdbid-<id>].<ext>

    Shows:
        <Title> (<Year>) [tvdbid-<id>]/
            Season <N>/
                <Title> - S<NN>E<NN> - <Episode Title>.<ext>

Metadata is fetched from a pluggable "MetadataProvider". A TVDB (v4 API)
provider is included; add more providers by subclassing MetadataProvider
and registering them in PROVIDERS.

Usage:
    python media_organizer.py m <tvdb_id> <mega_link> [<mega_link> ...]
    python media_organizer.py s <tvdb_id> <mega_link> [<mega_link> ...]

    Provide more than one <mega_link> when a show's episodes are split
    across multiple Mega links/folders -- they're all downloaded into the
    same source pool before organising, so episodes from any of them get
    matched up correctly.

    Each <mega_link> does not need to be a raw URL. If it isn't already a
    valid http(s) URL, the script will try base64-decoding it (up to 5
    times, since it's sometimes wrapped in base64 more than once) until
    it finds one, or give up and error out.

Environment:
    TVDB_API_KEY   Required for the TVDB provider.
    TVDB_PIN       Optional, only needed for TVDB "subscriber" API keys.

Dependencies:
    pip install tvdb_v4_official

    MEGAcmd must also be installed (provides the `mega-get`, `mega-login`,
    `mega-whoami`, `mega-logout` binaries and the `mega-cmd-server`
    background process they talk to over a local socket):
        https://github.com/meganz/MEGAcmd

Note on Mega downloading:
    This script shells out to the MEGAcmd client binaries (mega-get, etc.)
    rather than using a Python Mega library. The first call to any mega-*
    command will automatically spawn mega-cmd-server if it isn't already
    running, so nothing needs to be started manually. If --mega-email /
    --mega-password are supplied, the script logs into that account for
    the duration of the download (useful for large/rate-limited
    downloads) and logs back out afterwards; otherwise it downloads
    anonymously, which works fine for public links.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List
from urllib.parse import urlparse

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".flv", ".webm", ".ts",
}
SUBTITLE_EXTENSIONS = {".srt", ".sub", ".ass", ".ssa", ".vtt"}

INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*]')


def sanitize(name: str) -> str:
    """Strip characters that are illegal in file/folder names on common
    filesystems, and trim trailing whitespace/dots."""
    name = INVALID_FILENAME_CHARS.sub("", name)
    name = name.strip().rstrip(".")
    return name


# --------------------------------------------------------------------------
# Metadata layer
# --------------------------------------------------------------------------

@dataclass
class MediaRequest:
    type: str
    provider_id: str
    mega_links: List[str]


@dataclass
class MovieMetadata:
    title: str
    year: Optional[int]
    provider_id: str


@dataclass
class ShowMetadata:
    title: str
    year: Optional[int]
    provider_id: str
    # (season, episode) -> episode title
    episodes: dict = field(default_factory=dict)

    def episode_title(self, season: int, episode: int) -> Optional[str]:
        return self.episodes.get((season, episode))


class MetadataProvider(ABC):
    """Base class for metadata providers. Implement both methods to add
    a new source (e.g. TMDB, AniDB, etc.)."""

    @abstractmethod
    def get_movie(self, provider_id: str) -> MovieMetadata:
        ...

    @abstractmethod
    def get_show(self, provider_id: str) -> ShowMetadata:
        """Should return a ShowMetadata with its `episodes` dict fully
        populated so episode titles can be looked up offline afterwards."""
        ...


class TVDBProvider(MetadataProvider):
    """Metadata provider backed by the official `tvdb_v4_official` package
    (https://github.com/thetvdb/tvdb-v4-python)."""

    def __init__(self, api_key: Optional[str] = None, pin: Optional[str] = None):
        try:
            import tvdb_v4_official
        except ImportError as exc:
            raise RuntimeError(
                "The 'tvdb_v4_official' package is required. Install it with:\n"
                "    pip install tvdb_v4_official"
            ) from exc

        api_key = api_key or os.environ.get("TVDB_API_KEY")
        pin = pin or os.environ.get("TVDB_PIN")
        if not api_key:
            raise RuntimeError(
                "TVDB API key not set. Pass --tvdb-api-key or set TVDB_API_KEY."
            )

        # The client authenticates immediately on construction.
        self._client = tvdb_v4_official.TVDB(api_key, pin=pin or "")

    # -- public API -----------------------------------------------------

    def get_movie(self, provider_id: str) -> MovieMetadata:
        data = self._client.get_movie_extended(int(provider_id))
        title = (
                next((item.get("name") for item in data.get("aliases", []) if item.get("language") == "eng"), None)
                or data.get("name")
                or _first_translated_name(data)
        )
        year = _extract_year(
            data.get("year")
            or (data.get("first_release") or {}).get("date")
        )
        return MovieMetadata(title=title, year=year, provider_id=str(provider_id))

    def get_show(self, provider_id: str) -> ShowMetadata:
        series = self._client.get_series_extended(int(provider_id))
        title = (
                next((item.get("name") for item in series.get("aliases", []) if item.get("language") == "eng"), None)
                or series.get("name")
                or _first_translated_name(series)
        )
        year = _extract_year(series.get("firstAired"))

        show = ShowMetadata(title=title, year=year, provider_id=str(provider_id))

        page = 0
        while True:
            info = self._client.get_series_episodes(int(provider_id), page=page, lang='eng')
            episodes = info.get("episodes") or []
            if not episodes:
                break

            for ep in episodes:
                season_num = ep.get("seasonNumber")
                ep_num = ep.get("number")
                ep_title = ep.get("name") or f"Episode {ep_num}"
                if season_num is not None and ep_num is not None:
                    show.episodes[(int(season_num), int(ep_num))] = ep_title

            page += 1
            if page > 200:  # safety net against an unexpected infinite loop
                break

        return show


def _extract_year(date_like) -> Optional[int]:
    if not date_like:
        return None
    match = re.search(r"(\d{4})", str(date_like))
    return int(match.group(1)) if match else None


def _first_translated_name(data: dict) -> str:
    translations = data.get("aliases") or []
    return translations[0]["name"] if translations else data.get("slug", "Unknown Title")


# Registry so new providers can be added and selected by name.
PROVIDERS = {
    "tvdb": TVDBProvider,
}


# --------------------------------------------------------------------------
# Link resolution (handles links that arrive base64-encoded)
# --------------------------------------------------------------------------

MAX_BASE64_DECODE_ATTEMPTS = 5


def is_valid_url(candidate: str) -> bool:
    parsed = urlparse(candidate)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _try_base64_decode(candidate: str) -> Optional[str]:
    """Attempt to base64-decode candidate (standard or URL-safe alphabet,
    tolerant of missing padding). Returns the decoded string, or None if
    it isn't valid base64 / doesn't decode to text."""
    stripped = candidate.strip()
    # Re-pad if needed; base64 length must be a multiple of 4.
    padded = stripped + ("=" * (-len(stripped) % 4))

    decoders = (
        lambda s: base64.b64decode(s, validate=True),
        lambda s: base64.urlsafe_b64decode(s),
    )
    for decoder in decoders:
        try:
            decoded_bytes = decoder(padded)
        except (binascii.Error, ValueError):
            continue
        try:
            return decoded_bytes.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return None


def resolve_link(raw_link: str, max_attempts: int = MAX_BASE64_DECODE_ATTEMPTS) -> str:
    """Return raw_link as-is if it's already a valid URL. Otherwise, try
    base64-decoding it repeatedly (up to max_attempts times, since links
    are sometimes double-encoded) until a valid URL appears.

    Raises ValueError if no valid URL is found within the attempt budget.
    """
    candidate = raw_link.strip()

    if is_valid_url(candidate):
        return candidate

    for attempt in range(1, max_attempts + 1):
        decoded = _try_base64_decode(candidate)
        if decoded is None:
            break

        decoded = decoded.strip()
        print(f"  Decoded base64 layer {attempt}: {decoded[:80]}"
              f"{'...' if len(decoded) > 80 else ''}")

        if is_valid_url(decoded):
            return decoded

        candidate = decoded

    raise ValueError(
        f"'{raw_link}' is not a valid URL and could not be resolved to one "
        f"after up to {max_attempts} base64 decode attempt(s)."
    )


# --------------------------------------------------------------------------
# Candidate Provider
# --------------------------------------------------------------------------
def media_request_from_dataframe(row: dict) -> MediaRequest:
    return MediaRequest(
        type=row["mode"],
        provider_id=row["id"],
        mega_links=list(map(lambda x: resolve_link(x.strip()), row["mega_links"].split(";")))
    )


class CandidateProvider(ABC):

    def looping(self) -> bool:
        pass

    def list_candidates(self) -> List[MediaRequest]:
        pass

    def remove_candidate(self, candidate: MediaRequest):
        pass

    def refresh_candidates(self):
        pass


class ArgsCandidateProvider(CandidateProvider):

    def __init__(self, args: argparse.Namespace):
        self.args = args

    def looping(self) -> bool:
        return False

    def list_candidates(self) -> List[MediaRequest]:
        return [MediaRequest(
            type=self.args.mode,
            provider_id=self.args.id,
            mega_links=self.args.mega_links
        )]

    def remove_candidate(self, candidate: MediaRequest):
        pass

    def refresh_candidates(self):
        pass


# --------------------------------------------------------------------------
# Mega download layer (via MEGAcmd / mega-cmd-server)
# --------------------------------------------------------------------------

class MegaCmdError(RuntimeError):
    pass


def _run_mega_cmd(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a MEGAcmd client binary (e.g. mega-get, mega-login).

    The first invocation of any mega-* binary automatically starts
    mega-cmd-server in the background if it isn't already running, and
    talks to it over a local socket, so there's nothing to start
    explicitly here.
    """
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:
        raise MegaCmdError(
            f"'{args[0]}' was not found. Install MEGAcmd: "
            "https://github.com/meganz/MEGAcmd"
        ) from exc

    if check and result.returncode != 0:
        raise MegaCmdError(
            f"Command failed: {' '.join(args)}\n"
            f"stdout: {result.stdout.strip()}\n"
            f"stderr: {result.stderr.strip()}"
        )
    return result


def mega_login(email: str, password: str) -> None:
    print(f"Logging into Mega as {email}...")
    _run_mega_cmd(["mega-login", email, password])


def mega_logout() -> None:
    _run_mega_cmd(["mega-logout"], check=False)


def download_from_mega(links: list[str], dest_dir: Path) -> Path:
    """Download one or more Mega.nz links (file or folder) into dest_dir
    using the MEGAcmd client (mega-get), which talks to the
    mega-cmd-server background process. All links land in the same
    dest_dir, so this is how a show split across multiple Mega links can
    be stacked into a single set of source files before organising.
    Returns dest_dir."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    for index, link in enumerate(links, start=1):
        print(f"Downloading from Mega ({index}/{len(links)}): {link}")
        # mega-get handles both single-file and folder links, downloading
        # recursively into dest_dir.
        _run_mega_cmd(["mega-get", link, str(dest_dir)])

    return dest_dir


def find_video_files(root: Path) -> list[Path]:
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    )


def find_companion_subtitle(video_path: Path) -> Optional[Path]:
    """Find a subtitle file with the same stem as the video, if any."""
    for ext in SUBTITLE_EXTENSIONS:
        candidate = video_path.with_suffix(ext)
        if candidate.exists():
            return candidate
    return None


# --------------------------------------------------------------------------
# Episode number parsing (for shows)
# --------------------------------------------------------------------------

EPISODE_PATTERNS = [
    re.compile(r"[Ss](?P<season>\d{1,2})[Ee](?P<episode>\d{1,3})"),
    re.compile(r"(?P<season>\d{1,2})[xX](?P<episode>\d{1,3})"),
    re.compile(
        r"[Ss]eason[\s._-]*(?P<season>\d{1,2})[\s._-]*"
        r"[Ee]pisode[\s._-]*(?P<episode>\d{1,3})"
    ),
]


def parse_season_episode(filename: str) -> Optional[tuple[int, int]]:
    for pattern in EPISODE_PATTERNS:
        match = pattern.search(filename)
        if match:
            return int(match.group("season")), int(match.group("episode"))
    return None


# --------------------------------------------------------------------------
# Organising
# --------------------------------------------------------------------------

def build_show_root_name(show: ShowMetadata) -> str:
    year_part = f" ({show.year})" if show.year else ""
    return sanitize(f"{show.title}{year_part} [tvdbid-{show.provider_id}]")


def build_movie_root_name(movie: MovieMetadata) -> str:
    year_part = f" ({movie.year})" if movie.year else ""
    return sanitize(f"{movie.title}{year_part} [tvdbid-{movie.provider_id}]")


def organise_movie(video_files: list[Path], movie: MovieMetadata, library_root: Path) -> Path:
    if not video_files:
        raise RuntimeError("No video files found in the downloaded content.")
    # Assume the largest video file is the feature (extras/samples are usually smaller).
    main_video = max(video_files, key=lambda p: p.stat().st_size)
    featurette_videos = video_files.copy()
    featurette_videos.remove(main_video)

    root_name = build_movie_root_name(movie)
    movie_dir = library_root / root_name
    movie_dir.mkdir(parents=True, exist_ok=True)

    dest_video = movie_dir / f"{root_name}{main_video.suffix.lower()}"
    shutil.copyfile(str(main_video), str(dest_video))
    os.remove(str(main_video))
    print(f"  Movie -> {dest_video}")

    features_dir = library_root / root_name / "Featurettes"
    features_dir.mkdir(parents=True, exist_ok=True)
    for video in featurette_videos:
        dest_feature = features_dir / video.name
        shutil.copyfile(str(video), str(dest_feature))
        os.remove(str(video))
        print(f"  Feature {video.name} -> {dest_feature}")

    sub = find_companion_subtitle(main_video)
    if sub and sub.exists():
        dest_sub = dest_video.with_suffix(sub.suffix.lower())
        shutil.copyfile(str(sub), str(dest_sub))
        os.remove(str(sub))
        print(f"  Subtitle -> {dest_sub}")

    return movie_dir


def organise_show(video_files: list[Path], show: ShowMetadata, library_root: Path) -> Path:
    if not video_files:
        raise RuntimeError("No video files found in the downloaded content.")

    root_name = build_show_root_name(show)
    show_dir = library_root / root_name
    show_dir.mkdir(parents=True, exist_ok=True)

    unmatched = []
    for video in video_files:
        parsed = parse_season_episode(video.name)
        if not parsed:
            unmatched.append(video)
            continue

        season, episode = parsed
        ep_title = show.episode_title(season, episode) or f"Episode {episode}"

        season_dir = show_dir / f"Season {season}"
        season_dir.mkdir(parents=True, exist_ok=True)

        filename = sanitize(
            f"{show.title} - S{season:02d}E{episode:02d} - {ep_title}"
        )
        dest_video = season_dir / f"{filename}{video.suffix.lower()}"
        shutil.copyfile(str(video), str(dest_video))
        os.remove(str(video))
        print(f"  S{season:02d}E{episode:02d} -> {dest_video}")

        sub = find_companion_subtitle(video)
        if sub and sub.exists():
            dest_sub = dest_video.with_suffix(sub.suffix.lower())
            shutil.copyfile(str(sub), str(dest_sub))
            os.remove(str(sub))
            print(f"    Subtitle -> {dest_sub}")

    if unmatched:
        print("\nWarning: could not detect season/episode for the following files;"
              " they were left in place for manual sorting:")
        for f in unmatched:
            print(f"  - {f}")

    return show_dir


# --------------------------------------------------------------------------
# Main / CLI
# --------------------------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download a movie/show from Mega and organise it into a "
                    "standard media library structure."
    )
    parser.add_argument(
        "mode", choices=["m", "s"], nargs="?", default=None,
        help="'m' for movie, 's' for show"
    )
    parser.add_argument(
        "id", nargs="?", default=None,
        help="Metadata ID to look up (e.g. TVDB id)"
    )
    parser.add_argument(
        "mega_links", nargs="*",
        help="One or more Mega.nz file/folder links (space-separated). "
             "Provide more than one when a show's episodes are split "
             "across multiple Mega links -- all of them are downloaded "
             "into the same source pool before organising. Each link may "
             "also be base64-encoded (see module docstring)."
    )

    parser.add_argument(
        "--provider", default="tvdb", choices=list(PROVIDERS.keys()),
        help="Metadata provider to use (default: tvdb)"
    )
    parser.add_argument(
        "--library-root", default=".",
        help="Directory under which the organised <Title> (<Year>) folder is created"
    )
    parser.add_argument(
        "--movie-library-root", default=None,
        help="Directory under which the organised <Title> (<Year>) folder is created"
    )
    parser.add_argument(
        "--show-library-root", default=None,
        help="Directory under which the organised <Title> (<Year>) folder is created"
    )
    parser.add_argument(
        "--tvdb-api-key", default=None,
        help="TVDB API key (overrides TVDB_API_KEY env var)"
    )
    parser.add_argument(
        "--tvdb-pin", default=None,
        help="TVDB subscriber PIN, if required (overrides TVDB_PIN env var)"
    )
    parser.add_argument(
        "--mega-email", default=None,
        help="Mega account email (optional, enables logged-in downloads)"
    )
    parser.add_argument(
        "--mega-password", default=None,
        help="Mega account password (optional, used with --mega-email)"
    )
    parser.add_argument(
        "--keep-download", action="store_true",
        help="Keep the raw downloaded files/temp dir after organising (for debugging)"
    )

    args = parser.parse_args(argv)

    if not args.mode or not args.id or not args.mega_links:
        parser.error("provide mode, metadata ID, and Mega links")
    return args


def build_provider(args: argparse.Namespace) -> MetadataProvider:
    provider_cls = PROVIDERS[args.provider]
    if args.provider == "tvdb":
        return provider_cls(api_key=args.tvdb_api_key, pin=args.tvdb_pin)
    return provider_cls()


def fetch(
        request: MediaRequest,
        provider: MetadataProvider,
        library_root: Path,
        keep_download: bool = False,
):
    with tempfile.TemporaryDirectory(prefix="media_dl_") as tmp:
        tmp_path = Path(tmp)
        download_from_mega(request.mega_links, tmp_path)

        video_files = find_video_files(tmp_path)
        if not video_files:
            print("No video files were found in the downloaded content.", file=sys.stderr)
            return

        print(f"\nFound {len(video_files)} video file(s). Fetching metadata...")

        if request.type == "m":
            movie = provider.get_movie(request.provider_id)
            print(f"Movie: {movie.title} ({movie.year}) [tvdbid-{movie.provider_id}]")
            dest = organise_movie(video_files, movie, library_root)
        else:
            show = provider.get_show(request.provider_id)
            print(f"Show: {show.title} ({show.year}) [tvdbid-{show.provider_id}] "
                  f"- {len(show.episodes)} episode(s) in metadata")
            dest = organise_show(video_files, show, library_root)

        if keep_download:
            keep_path = library_root / f"_raw_download_{Path(tmp).name}"
            shutil.copytree(tmp_path, keep_path)
            print(f"\nRaw download copy kept at: {keep_path}")

    print(f"\nDone. Organised into: {dest}")


def main(argv=None) -> int:
    args = parse_args(argv)
    library_root = Path(args.library_root).expanduser().resolve()
    library_root.mkdir(parents=True, exist_ok=True)

    movie_library_root = None
    if args.movie_library_root:
        movie_library_root = Path(args.movie_library_root).expanduser().resolve()
        movie_library_root.mkdir(parents=True, exist_ok=True)

    show_library_root = None
    if args.show_library_root:
        show_library_root = Path(args.show_library_root).expanduser().resolve()
        show_library_root.mkdir(parents=True, exist_ok=True)

    metadata_provider = build_provider(args)
    candidate_provider = ArgsCandidateProvider(args)

    loggedIn = False
    if args.mega_email and args.mega_password:
        mega_login(args.mega_email, args.mega_password)

    try:
        while True:
            candidates = candidate_provider.list_candidates()

            for candidate in candidates:
                destination = library_root
                if candidate.type == 'm' and movie_library_root:
                    destination = movie_library_root
                elif candidate.type == 's' and show_library_root:
                    destination = show_library_root

                fetch(candidate, metadata_provider, destination, keep_download=args.keep_download)
                candidate_provider.remove_candidate(candidate)

            if candidate_provider.looping():
                print("No more candidates, sleeping for 60 seconds...")
                time.sleep(60)
                continue
            break
    finally:
        if loggedIn:
            mega_logout()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
