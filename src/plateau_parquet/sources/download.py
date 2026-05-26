"""Minimal cached HTTP downloader for catalog assets.

We deliberately avoid heavy frameworks. PLATEAU assets are zips of CityGML;
we stream to disk and unzip once. Caching is content-addressed by URL.
"""

from __future__ import annotations

import hashlib
import logging
import zipfile
from pathlib import Path

import httpx
from tqdm import tqdm

log = logging.getLogger(__name__)


def _url_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def cache_path_for_url(url: str, cache_dir: Path) -> Path:
    """Return the on-disk directory where ``url`` would be extracted.

    Useful for pruning per-URL cache after a build completes without
    having to walk the directory.
    """
    return cache_dir / _url_key(url)


def fetch_and_unzip(url: str, cache_dir: Path, *, timeout: float = 60.0) -> Path:
    """Download ``url`` into ``cache_dir`` and unzip. Returns the extracted dir.

    Idempotent: a marker file (`.done`) inside the extracted dir means cached.
    """
    key = _url_key(url)
    target = cache_dir / key
    marker = target / ".done"
    if marker.exists():
        return target

    target.mkdir(parents=True, exist_ok=True)
    zip_path = target / "src.zip"
    log.info("downloading %s", url)
    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        with open(zip_path, "wb") as f, tqdm(
            total=total or None, unit="B", unit_scale=True, desc=url.rsplit("/", 1)[-1]
        ) as bar:
            for chunk in r.iter_bytes(chunk_size=1 << 16):
                f.write(chunk)
                bar.update(len(chunk))

    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(target)
    zip_path.unlink(missing_ok=True)
    marker.touch()
    return target
