"""
updater.py
----------
Little toolbox for the GUI:

* works out which version is newer
* grabs the latest release JSON from GitHub
* pulls every asset down in parallel (zip-free, ~1 MiB chunks)

Only three deps: requests, packaging, Py>=3.9
"""

from __future__ import annotations
import re, pathlib, threading, requests
from pathlib import Path
from typing import Callable, Dict, Any, List
from concurrent.futures import ThreadPoolExecutor, as_completed
from packaging import version      # PEP-440 parser

# -------------------------------------------------
# version helpers
# -------------------------------------------------
_semver = re.compile(r"(\d+\.\d+\.\d+)")   # first thing that looks like 1.2.3

def _extract(v: str) -> str | None:
    m = _semver.search(v or "")
    return m.group(1) if m else None

def best_version_string(tag: str, title: str | None = None) -> str:
    """Prefer a real x.y.z, fall back to whatever the tag is."""
    return _extract(tag) or _extract(title) or tag

def compare_versions(remote_tag: str, local: str, title: str | None = None) -> bool:
    """
    True if the remote thing beats what we’ve got on disk.

    Logic:
    • if both sides have a semver → normal compare
    • if remote has a semver but local doesn’t → update
    • if local has semver but remote doesn’t → skip
    • if neither has numbers → assume remote is newer
    """
    remote = _extract(remote_tag) or _extract(title)
    local  = _extract(local)

    if remote and local:
        return version.parse(remote) > version.parse(local)
    if remote and not local:
        return True
    if not remote and local:
        return False
    return True

# -------------------------------------------------
# GitHub helper – hits one endpoint, returns JSON
# -------------------------------------------------
def fetch_latest_release(repo: str, token: str | None = None) -> Dict[str, Any]:
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    hdr = {}
    if token:
        hdr["Authorization"] = token if token.startswith(("token ", "Bearer ")) else f"token {token}"
    r = requests.get(url, timeout=15, headers=hdr)
    r.raise_for_status()
    return r.json()

# -------------------------------------------------
# quick’n’dirty download manager (parallel)
# -------------------------------------------------
CHUNK        = 1 << 20   # 1 MiB blocks feel like a good trade-off
MAX_WORKERS  = 4         # bump if you ship a bunch of tiny files

def _grab(session, asset, dest: Path, tick, offset):
    downloaded = 0
    with session.get(asset["browser_download_url"], stream=True, timeout=60) as r, open(dest, "wb") as f:
        r.raise_for_status()
        for chunk in r.iter_content(chunk_size=CHUNK):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                tick(offset + downloaded)

def download_all_assets(
        assets: List[Dict[str, Any]],
        target_dir: pathlib.Path,
        on_progress: Callable[[int], None] | None = None
) -> str | None:
    """
    Pulls every non-“Source code” asset into *target_dir* (overwrites by name).

    Progress callback gets 0-100 ints.
    Returns local path to changelog.json if we downloaded one, else None.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    todo   = [a for a in assets if not a["name"].startswith("Source code")]
    total  = sum(a["size"] for a in todo)
    cl_path: Path | None = None

    def tick(done_bytes):
        if on_progress and total:
            on_progress(int(done_bytes / total * 100))

    with requests.Session() as sess, ThreadPoolExecutor(MAX_WORKERS) as pool:
        futures = []
        offset  = 0
        for a in todo:
            out = target_dir / a["name"]
            if a["name"].lower() == "changelog.json":
                cl_path = out
            futures.append(pool.submit(_grab, sess, a, out, tick, offset))
            offset += a["size"]

        for f in as_completed(futures):   # bubble up exceptions
            f.result()

    tick(total)   # make sure we end on 100 %
    return str(cl_path) if cl_path else None
