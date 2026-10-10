from __future__ import annotations

import re
import stat
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spotifast import main as main_module
from spotifast.config import Settings
from spotifast.security import hash_password
from spotifast.sunnify import Preview, TrackPreview

ORIGIN = "https://spotifast.test"
FAKE_CLI = Path(__file__).with_name("fake_sunnify.py")
PASSWORD = "correct horse battery"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    FAKE_CLI.chmod(FAKE_CLI.stat().st_mode | stat.S_IXUSR)
    return Settings(
        public_origin=ORIGIN,
        data_dir=tmp_path / "data",
        work_dir=tmp_path / "work",
        sunnify_bin=str(FAKE_CLI),
        cookie_secure=True,
        max_tracks_per_job=10,
        daily_track_quota=25,
        min_free_disk_mb=0,
        result_ttl_s=600,
        after_download_ttl_s=1,
    )


@pytest.fixture
def fake_preview(monkeypatch):
    """Replace the Spotify metadata call; track count steered per test."""
    state = {"tracks": 3}

    def fetch(ref, *, limit):
        n = state["tracks"]
        tracks = [TrackPreview(f"Morceau {i}", "Artiste", 180000, "") for i in range(1, n + 1)]
        name = "Ma Playlist" if ref.kind != "track" else "Morceau 1"
        truncated = len(tracks) > limit
        return Preview(ref=ref, name=name, owner="moi", cover="", tracks=tracks[:limit],
                       truncated=truncated)

    monkeypatch.setattr(main_module, "fetch_preview", fetch)
    return state


@pytest.fixture
def app(settings, fake_preview):
    return main_module.create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app, base_url="https://spotifast.test", headers={"Origin": ORIGIN},
                    follow_redirects=False) as c:
        yield c


def make_user(app, username: str, *, admin: bool = False, must_change: bool = False):
    db = app.state.db
    user = db.create_user(username, hash_password(PASSWORD), is_admin=admin)
    db.set_password(user.id, user.password_hash, must_change=must_change)
    return db.get_user(user.id)


def login(client: TestClient, username: str, password: str = PASSWORD):
    return client.post("/login", data={"username": username, "password": password, "next": "/"})


def csrf_of(client: TestClient) -> str:
    page = client.get("/account")
    match = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
    assert match, page.text[:500]
    return match.group(1)


def api(client: TestClient, method: str, path: str, **kwargs):
    headers = kwargs.pop("headers", {})
    headers.setdefault("X-CSRF-Token", csrf_of(client))
    return client.request(method, path, headers=headers, **kwargs)


def set_fake_mode(settings: Settings, mode: str) -> None:
    (settings.work_dir / "fake-mode").write_text(mode)


def wait_job(client: TestClient, job_id: str, timeout: float = 15) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()["job"]
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} still {job['status']}")


@pytest.fixture
def friend(app, client):
    make_user(app, "camille")
    assert login(client, "camille").status_code == 303
    return client


PLAYLIST = "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc"
TRACK = "https://open.spotify.com/intl-fr/track/4uLU6hMCjMI75M1A2tKUQC"

