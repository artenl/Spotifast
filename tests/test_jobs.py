import io
import time
import zipfile

import pytest
from conftest import PLAYLIST, TRACK, api, login, make_user, set_fake_mode, wait_job

from spotifast.sunnify import InvalidSpotifyURL, parse_spotify_url


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "https://evil.example/playlist/37i9dQZF1DXcBWIGoYBM5M",
        "https://open.spotify.com.evil.example/playlist/37i9dQZF1DXcBWIGoYBM5M",
        "https://open.spotify.com/user/someone",
        "https://open.spotify.com/playlist/short",
        "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M --exec rm",
        "--out=/etc https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
        "file:///etc/passwd",
        None,
        ["https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"],
    ],
)
def test_bad_urls_are_rejected(raw):
    with pytest.raises(InvalidSpotifyURL):
        parse_spotify_url(raw)


def test_urls_are_rebuilt_canonically():
    ref = parse_spotify_url(" https://open.spotify.com/intl-fr/album/4aawyAB9vmqN3uQ7FjRGTy?si=x#y ")
    assert ref.url == "https://open.spotify.com/album/4aawyAB9vmqN3uQ7FjRGTy"
    ref = parse_spotify_url("spotify:track:4uLU6hMCjMI75M1A2tKUQC")
    assert ref.url == "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"


def start_job(client, url=PLAYLIST, **extra):
    return api(client, "POST", "/api/jobs", json={"url": url, "format": "mp3",
                                                  "quality": "320", **extra})


def test_preview(friend):
    r = api(friend, "POST", "/api/preview", json={"url": PLAYLIST})
    assert r.status_code == 200
    preview = r.json()["preview"]
    assert preview["name"] == "Ma Playlist" and preview["trackCount"] == 3
    assert preview["url"] == "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"


def test_playlist_job_streams_a_zip_and_cleans_up(app, friend, settings):
    r = start_job(friend)
    assert r.status_code == 201, r.text
    job = wait_job(friend, r.json()["job"]["id"])
    assert job["status"] == "done" and job["landed"] == 3 and job["downloadable"]
    job_dir = settings.work_dir / job["id"]
    assert job_dir.is_dir()

    dl = friend.get(f"/api/jobs/{job['id']}/download")
    assert dl.status_code == 200
    assert dl.headers["content-type"] == "application/zip"
    assert "Ma Playlist.zip" in dl.headers["content-disposition"]
    archive = zipfile.ZipFile(io.BytesIO(dl.content))
    assert archive.testzip() is None
    names = archive.namelist()
    assert names == [f"Ma Playlist/{i:02d}. Morceau {i} - Artiste.mp3" for i in (1, 2, 3)]
    assert not any(".sunnify" in n for n in names)

    # once downloaded the files only linger for after_download_ttl_s (1 s)
    after = friend.get(f"/api/jobs/{job['id']}").json()["job"]
    assert after["expiresAt"] <= time.time() + 2
    time.sleep(1.1)
    app.state.manager.sweep()
    assert not job_dir.exists()
    assert friend.get(f"/api/jobs/{job['id']}").json()["job"]["status"] == "expired"
    assert friend.get(f"/api/jobs/{job['id']}/download").status_code == 410


def test_single_track_is_served_as_a_file(friend):
    job = wait_job(friend, start_job(friend, TRACK).json()["job"]["id"])
    dl = friend.get(f"/api/jobs/{job['id']}/download")
    assert dl.status_code == 200
    assert dl.headers["content-type"] == "audio/mpeg"
    assert int(dl.headers["content-length"]) == len(dl.content)
    assert "filename*=UTF-8''" in dl.headers["content-disposition"]


def test_sse_reports_progress_until_done(friend):
    job_id = start_job(friend).json()["job"]["id"]
    statuses = []
    with friend.stream("GET", f"/api/jobs/{job_id}/events") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if line.startswith("data: "):
                import json
                statuses.append(json.loads(line[6:])["status"])
    assert statuses[-1] == "done"


def test_jobs_are_private_to_their_owner(app, friend, client):
    job_id = start_job(friend).json()["job"]["id"]
    wait_job(friend, job_id)
    client.cookies.clear()
    make_user(app, "intrus")
    login(client, "intrus")
    for path in ("", "/download", "/events"):
        assert client.get(f"/api/jobs/{job_id}{path}").status_code == 404
    assert api(client, "POST", f"/api/jobs/{job_id}/cancel").status_code == 404
    assert client.get("/api/jobs").json()["jobs"] == []
    assert client.get("/api/jobs/..%2f..%2fetc/download").status_code == 404


def test_partial_failure_is_reported(friend, settings):
    set_fake_mode(settings, "partial")
    job = wait_job(friend, start_job(friend).json()["job"]["id"])
    assert job["status"] == "partial"
    assert job["landed"] == 2 and job["failed"] == 1
    assert job["failedTitles"] == ["Morceau 3"]


def test_youtube_block_is_explained(friend, settings):
    set_fake_mode(settings, "block")
    job = wait_job(friend, start_job(friend).json()["job"]["id"])
    assert job["status"] == "failed" and not job["downloadable"]
    assert "robot" in job["message"]
    assert not (settings.work_dir / job["id"]).exists()


def test_cancel_stops_the_run(friend, settings):
    set_fake_mode(settings, "slow")
    (settings.work_dir / "fake-tracks").write_text("40")
    job_id = start_job(friend).json()["job"]["id"]
    time.sleep(0.8)
    assert api(friend, "POST", f"/api/jobs/{job_id}/cancel").status_code == 200
    job = wait_job(friend, job_id)
    assert job["status"] == "cancelled"
    assert 0 < job["landed"] < 40 and job["downloadable"]


def test_limits(friend, fake_preview, settings):
    fake_preview["tracks"] = 11  # > max_tracks_per_job (10)
    r = start_job(friend)
    assert r.status_code == 400 and "limite" in r.json()["error"]

    fake_preview["tracks"] = 3
    set_fake_mode(settings, "slow")
    (settings.work_dir / "fake-tracks").write_text("6")
    first = start_job(friend, "https://open.spotify.com/album/4aawyAB9vmqN3uQ7FjRGTy")
    assert first.status_code == 201
    second = start_job(friend, "https://open.spotify.com/album/1aawyAB9vmqN3uQ7FjRGTy")
    assert second.status_code == 429 and "déjà" in second.json()["error"]
    wait_job(friend, first.json()["job"]["id"])


def test_daily_quota(friend, fake_preview, settings):
    fake_preview["tracks"] = 10
    (settings.work_dir / "fake-tracks").write_text("10")
    for i in range(2):
        job_id = start_job(friend, f"https://open.spotify.com/album/{i}aawyAB9vmqN3uQ7FjRGTy")
        wait_job(friend, job_id.json()["job"]["id"])
    r = start_job(friend, "https://open.spotify.com/album/9aawyAB9vmqN3uQ7FjRGTy")
    assert r.status_code == 429 and "Quota" in r.json()["error"]


def test_bad_format_is_rejected(friend):
    assert start_job(friend, format="exe").status_code == 400
    assert start_job(friend, quality="9999").status_code == 400
    assert api(friend, "POST", "/api/jobs", content=b"not json",
               headers={"Content-Type": "application/json"}).status_code == 400
