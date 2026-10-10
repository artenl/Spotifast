"""Everything that touches Sunnify, which is used strictly as published.

Sunnify's licence forbids modifying it, so this module only *calls* it:
its `spotifydown_api` module for read-only metadata (as Sunnify's own web
backend does), and its `sunnify download --json` CLI for the downloads.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

# Spotify IDs are 22 base62 characters. Matching them strictly (rather than
# forwarding whatever the user pasted) means the only thing ever handed to
# the CLI is a canonical URL we rebuilt ourselves.
_WEB_URL = re.compile(
    r"^https?://open\.spotify\.com/(?:intl-[A-Za-z-]{2,12}/)?"
    r"(?:embed/)?(track|album|playlist)/([A-Za-z0-9]{22})(?:[/?#].*)?$"
)
_URI = re.compile(r"^spotify:(track|album|playlist):([A-Za-z0-9]{22})$")
MAX_URL_LEN = 512

AUDIO_FORMATS = {
    # FLAC/WAV are left out on purpose: YouTube audio is lossy at the
    # source, so they'd be 3-10x bigger for no gain in quality.
    "mp3": "MP3",
    "m4a": "M4A (AAC)",
    "opus": "Opus",
}
QUALITIES = ("128", "192", "256", "320")
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".opus", ".ogg", ".flac", ".wav"}


class InvalidSpotifyURL(ValueError):
    pass


class MetadataError(RuntimeError):
    pass


@dataclass(frozen=True)
class SpotifyRef:
    kind: str  # track | album | playlist
    id: str

    @property
    def url(self) -> str:
        return f"https://open.spotify.com/{self.kind}/{self.id}"


def parse_spotify_url(raw: object) -> SpotifyRef:
    if not isinstance(raw, str):
        raise InvalidSpotifyURL("Lien manquant.")
    text = raw.strip()
    if not text or len(text) > MAX_URL_LEN:
        raise InvalidSpotifyURL("Lien manquant ou trop long.")
    match = _WEB_URL.match(text) or _URI.match(text)
    if not match:
        raise InvalidSpotifyURL(
            "Ce n'est pas un lien Spotify de titre, d'album ou de playlist "
            "(ex. https://open.spotify.com/playlist/…)."
        )
    return SpotifyRef(kind=match.group(1), id=match.group(2))


@dataclass
class TrackPreview:
    title: str
    artists: str
    duration_ms: int
    cover: str


@dataclass
class Preview:
    ref: SpotifyRef
    name: str
    owner: str
    cover: str
    tracks: list[TrackPreview] = field(default_factory=list)
    # True when the listing was cut at the configured limit: the real
    # playlist is longer than what we're willing to download.
    truncated: bool = False
    # Size Spotify reports for the whole playlist, when known.
    total: int | None = None

    @property
    def track_count(self) -> int:
        return len(self.tracks)

    def to_json(self) -> dict:
        return {
            "kind": self.ref.kind,
            "url": self.ref.url,
            "name": self.name,
            "owner": self.owner,
            "cover": self.cover,
            "trackCount": self.track_count,
            "totalCount": max(self.total or 0, self.track_count),
            "truncated": self.truncated,
            "tracks": [
                {
                    "title": t.title,
                    "artists": t.artists,
                    "durationMs": t.duration_ms,
                    "cover": t.cover,
                }
                for t in self.tracks
            ],
        }


_SAFE_IMAGE = re.compile(r"^https://[a-z0-9.-]+\.(?:scdn\.co|spotifycdn\.com)/[^\s\"'<>]+$")


def _image(url: str | None) -> str:
    # Only Spotify's own CDNs are allowed by the page's CSP; anything else
    # is dropped rather than rendered.
    return url if url and _SAFE_IMAGE.match(url) else ""


def fetch_preview(ref: SpotifyRef, *, limit: int) -> Preview:
    """Blocking metadata fetch through Sunnify's own (unmodified) API client."""
    from spotifydown_api import PlaylistClient, SpotifyDownAPIError, SpotifyEmbedAPI

    try:
        if ref.kind == "track":
            t = SpotifyEmbedAPI().get_track(ref.id)
            cover = _image(t.cover_url)
            return Preview(
                ref=ref,
                name=t.title,
                owner=t.artists,
                cover=cover,
                tracks=[TrackPreview(t.title, t.artists, t.duration_ms or 0, cover)],
            )
        client = PlaylistClient()
        meta = client.get_playlist_metadata(ref.id, content_type=ref.kind)
        cover = _image(meta.cover_url)
        preview = Preview(ref=ref, name=meta.name, owner=meta.owner or "", cover=cover,
                          total=meta.track_count or None)
        if preview.total and preview.total > limit:
            # Known too long up front: answer now instead of walking 100+
            # tracks (past 100 Sunnify fetches them one by one, slowly).
            preview.truncated = True
            return preview
        for t in client.iter_playlist_tracks(ref.id, content_type=ref.kind):
            if len(preview.tracks) >= limit:
                preview.truncated = True
                break
            preview.tracks.append(
                TrackPreview(t.title, t.artists, t.duration_ms or 0, _image(t.cover_url) or cover)
            )
        return preview
    except (SpotifyDownAPIError, ValueError) as exc:
        raise MetadataError(str(exc)) from exc


class PreviewCache:
    """Small TTL cache: a preview is almost always followed by a download."""

    def __init__(self, ttl_s: int = 600, max_items: int = 64):
        self.ttl_s = ttl_s
        self.max_items = max_items
        self._items: dict[str, tuple[float, Preview]] = {}
        self._lock = threading.Lock()

    def get(self, ref: SpotifyRef) -> Preview | None:
        with self._lock:
            hit = self._items.get(ref.url)
            if hit and hit[0] > time.monotonic():
                return hit[1]
            self._items.pop(ref.url, None)
            return None

    def put(self, preview: Preview) -> None:
        with self._lock:
            if len(self._items) >= self.max_items:
                oldest = min(self._items, key=lambda k: self._items[k][0])
                self._items.pop(oldest, None)
            self._items[preview.ref.url] = (time.monotonic() + self.ttl_s, preview)


def download_command(
    sunnify_bin: str, ref: SpotifyRef, out_dir: Path, *, audio_format: str, quality: str
) -> list[str]:
    if audio_format not in AUDIO_FORMATS or quality not in QUALITIES:
        raise ValueError("unsupported format/quality")
    return [
        sunnify_bin,
        "download",
        ref.url,
        "--out",
        str(out_dir),
        "--format",
        audio_format,
        "--quality",
        quality,
        "--track-numbers" if ref.kind != "track" else "--no-track-numbers",
        "--json",
    ]
