#!/usr/bin/env python3
"""Stand-in for the `sunnify` CLI: same NDJSON contract, no network.

Behaviour is steered by FAKE_SUNNIFY_MODE (read from a file next to the
work dir, since the job runner passes a scrubbed environment):
  ok      every track lands
  partial the last track fails
  block   YouTube bot-check on every track
  slow    sleeps between tracks (for cancellation tests)
"""

import json
import os
import signal
import sys
import time
from pathlib import Path


def emit(**event):
    print(json.dumps(event), flush=True)


def main() -> int:
    args = sys.argv[1:]
    assert args[0] == "download", args
    url = args[1]
    out = Path(args[args.index("--out") + 1])
    fmt = args[args.index("--format") + 1]
    mode_file = out.parent.parent / "fake-mode"
    mode = mode_file.read_text().strip() if mode_file.exists() else "ok"
    tracks = int(os.environ.get("FAKE_TRACKS", "3"))
    if (out.parent.parent / "fake-tracks").exists():
        tracks = int((out.parent.parent / "fake-tracks").read_text())
    if "/track/" in url:
        tracks = 1

    stopped = False

    def on_int(*_):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, on_int)
    folder = out / "Ma Playlist" if "/track/" not in url else out
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".sunnify-manifest.jsonl").write_text("{}\n")
    emit(event="run_started", url=url, type="playlist", folder=str(out), format=fmt)
    landed, failed = 0, []
    for i in range(1, tracks + 1):
        if stopped:
            break
        title = f"Morceau {i}"
        if mode == "block":
            emit(event="warning", message=f"YouTube blocked this network - '{title}' skipped")
            failed.append(title)
            continue
        if mode == "partial" and i == tracks:
            emit(event="warning", message=f"'{title}' not found on YouTube")
            failed.append(title)
            continue
        if mode == "slow":
            time.sleep(0.5)
        path = folder / f"{i:02d}. {title} - Artiste.{fmt}"
        path.write_bytes(os.urandom(2048) + title.encode())
        landed += 1
        emit(event="track_done", title=title, artists="Artiste", file=str(path),
             bytes=path.stat().st_size)
    code = 1 if failed or stopped else 0
    emit(event="run_summary", landed=landed, skipped=0, failed=len(failed),
         failed_titles=failed, stopped=stopped, elapsed_s=0.1, folder=str(out), exit_code=code)
    return code


if __name__ == "__main__":
    sys.exit(main())
