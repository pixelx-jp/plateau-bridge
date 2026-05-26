"""Pre-built artifact distribution: skip nusamai, download a city in seconds.

Users with no interest in running the build pipeline themselves can fetch a
bundle that someone else already produced. The default mirror is GitHub
Releases on this repo (free, public, CDN-backed), but the cache index is
just a JSON file — any HTTPS-reachable host works.

Bundle layout (one tarball per city/year):

    plateau-13113-2023-v1.tar.zst
    ├── buildings.parquet
    ├── manifest.json
    ├── tile_index.json
    ├── style/<encoded>.arrow ×N
    ├── buildings.pmtiles
    └── buildings/<city_code>.fgb

(3D Tiles are NOT included — they're large, vary in zoom coverage, and most
downstream users only need the parquet + style tables. Heavy users still
run `plateau build` to regenerate them locally.)

Cache index (one JSON, the entry point):

    {
      "schema": 1,
      "updated": "2026-05-24T12:00:00Z",
      "cities": [
        {
          "city_code": "13113",
          "city_name": "Shibuya-ku",
          "dataset_year": 2023,
          "bundle_url": "https://github.com/.../releases/download/data-v1/plateau-13113-2023-v1.tar.zst",
          "sha256": "abc…",
          "bytes": 23456789,
          "n_buildings": 41858,
          "tool_version": "0.1.0"
        },
        …
      ]
    }
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
from tqdm import tqdm

from plateau_parquet import __version__
from plateau_parquet.config import load_settings

log = logging.getLogger(__name__)

DEFAULT_INDEX_URL = (
    "https://raw.githubusercontent.com/pixelx-jp/plateau-parquet/main/distribution/index.json"
)

# Files packaged into a bundle. 3D Tiles are deliberately omitted.
BUNDLE_FILES = (
    "buildings.parquet",
    "manifest.json",
    "tile_index.json",
    "buildings.pmtiles",
)
BUNDLE_DIRS = ("style", "buildings")  # buildings/<ward>.fgb shards


@dataclass(frozen=True)
class CityBundle:
    city_code: str
    city_name: str
    dataset_year: int
    bundle_url: str
    sha256: str
    bytes: int
    n_buildings: int
    tool_version: str


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_index(url: str = DEFAULT_INDEX_URL) -> dict:
    """Load the cache index. Accepts file:// for local testing + plain HTTPS."""
    if url.startswith("file://"):
        return json.loads(Path(url[len("file://"):]).read_text(encoding="utf-8"))
    r = httpx.get(url, follow_redirects=True, timeout=30)
    r.raise_for_status()
    return r.json()


def _open_bundle_stream(url: str):
    """Yield byte chunks from a bundle URL. Supports file:// and HTTPS."""
    if url.startswith("file://"):
        path = Path(url[len("file://"):])
        size = path.stat().st_size
        with path.open("rb") as f:
            yield size
            while True:
                chunk = f.read(1 << 16)
                if not chunk:
                    break
                yield chunk
        return
    with httpx.stream("GET", url, follow_redirects=True, timeout=300) as r:
        r.raise_for_status()
        size = int(r.headers.get("content-length", 0))
        yield size
        for chunk in r.iter_bytes(chunk_size=1 << 16):
            yield chunk


def add(
    city_code: str,
    *,
    index_url: str = DEFAULT_INDEX_URL,
    target_dir: Path | None = None,
    force: bool = False,
) -> Path:
    """Download + extract a pre-built bundle into ``target_dir`` (default
    ``out_<city>/``). Returns the extracted directory.

    Verifies sha256 against the index entry before extracting.
    """
    idx = fetch_index(index_url)
    candidates = [c for c in idx["cities"] if c["city_code"] == city_code]
    if not candidates:
        raise KeyError(f"city {city_code!r} not in {index_url}")
    # Pick most-recent year, latest tool_version.
    candidates.sort(key=lambda c: (c["dataset_year"], c["tool_version"]), reverse=True)
    entry = candidates[0]

    if target_dir is None:
        target_dir = Path.cwd() / f"out_{city_code}"
    target_dir = Path(target_dir)
    if target_dir.exists() and not force:
        raise FileExistsError(
            f"{target_dir} exists; pass force=True or delete it"
        )
    target_dir.mkdir(parents=True, exist_ok=True)

    settings = load_settings()
    cache_root = settings.cache_dir / "bundles"
    cache_root.mkdir(parents=True, exist_ok=True)
    bundle_path = cache_root / f"plateau-{city_code}-{entry['dataset_year']}-{entry['sha256'][:8]}.tar.zst"

    # GC: any older bundle for this (city_code, dataset_year) that no longer
    # matches the index sha is stale. Free disk before downloading a new one.
    stale_prefix = f"plateau-{city_code}-{entry['dataset_year']}-"
    keep_name = bundle_path.name
    for old in cache_root.glob(f"{stale_prefix}*.tar.zst"):
        if old.name != keep_name:
            log.info("removing stale bundle %s", old.name)
            old.unlink(missing_ok=True)

    if not bundle_path.exists() or _sha256_file(bundle_path) != entry["sha256"]:
        log.info("downloading %s (%d MB)", entry["bundle_url"], entry["bytes"] // (1 << 20))
        stream = _open_bundle_stream(entry["bundle_url"])
        _ = next(stream)  # discard header (size) — tqdm uses entry["bytes"]
        with bundle_path.open("wb") as f, tqdm(
            total=entry["bytes"], unit="B", unit_scale=True, desc=f"plateau-{city_code}"
        ) as bar:
            for chunk in stream:
                f.write(chunk)
                bar.update(len(chunk))
        got = _sha256_file(bundle_path)
        if got != entry["sha256"]:
            bundle_path.unlink()
            raise RuntimeError(
                f"sha256 mismatch for {bundle_path.name}: index={entry['sha256']} got={got}"
            )

    log.info("extracting → %s", target_dir)
    # tarfile doesn't support zstd natively pre-3.12; shell out to `tar` which does.
    subprocess.run(
        ["tar", "--use-compress-program=zstd -d", "-xf", str(bundle_path), "-C", str(target_dir)],
        check=True,
    )
    return target_dir


def build_bundle(out_dir: Path, dest: Path) -> tuple[Path, str, int]:
    """Pack an existing ``out_<city>/`` directory into a zstd tarball.

    Returns ``(tarball_path, sha256, byte_size)``.
    """
    out_dir = Path(out_dir)
    if not (out_dir / "buildings.parquet").exists():
        raise FileNotFoundError(f"{out_dir} has no buildings.parquet")

    dest.parent.mkdir(parents=True, exist_ok=True)
    members: list[Path] = []
    for name in BUNDLE_FILES:
        p = out_dir / name
        if p.exists():
            members.append(p)
    for name in BUNDLE_DIRS:
        p = out_dir / name
        if p.exists():
            members.append(p)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".tar") as tf:
        tar_path = Path(tf.name)
    with tarfile.open(tar_path, "w") as tar:
        for m in members:
            tar.add(m, arcname=m.relative_to(out_dir))

    # zstd-compress.
    subprocess.run(
        ["zstd", "-19", "-T0", "--force", "--rm", str(tar_path), "-o", str(dest)],
        check=True,
    )
    return dest, _sha256_file(dest), dest.stat().st_size


def push(
    out_dir: Path,
    *,
    backend: str = "github-releases",
    release_tag: str = "data-v1",
    dry_run: bool = False,
) -> CityBundle:
    """Pack ``out_dir`` and upload to the configured backend.

    Backends:
        github-releases — uses ``gh release upload`` against the current repo.
        local          — writes the bundle to ./distribution/ for inspection.

    Returns a ``CityBundle`` ready to merge into the cache index.
    """
    out_dir = Path(out_dir)
    manifest = json.loads((out_dir / "manifest.json").read_text())
    city_code = manifest["city_code"]
    year = manifest["dataset_year"]
    n = manifest["n_buildings"]

    tarball = Path(f"distribution/plateau-{city_code}-{year}-v1.tar.zst").resolve()
    log.info("packing %s → %s", out_dir, tarball)
    _, sha, nbytes = build_bundle(out_dir, tarball)

    if dry_run:
        return CityBundle(
            city_code=city_code,
            city_name=manifest.get("city_name", ""),
            dataset_year=year,
            bundle_url=f"file://{tarball}",
            sha256=sha,
            bytes=nbytes,
            n_buildings=n,
            tool_version=__version__,
        )

    if backend == "github-releases":
        log.info("uploading via `gh release upload %s`", release_tag)
        subprocess.run(
            ["gh", "release", "upload", release_tag, str(tarball), "--clobber"],
            check=True,
        )
        # gh release view to get the asset URL.
        proc = subprocess.run(
            ["gh", "release", "view", release_tag, "--json", "assets"],
            check=True, capture_output=True, text=True,
        )
        assets = json.loads(proc.stdout).get("assets", [])
        match = next((a for a in assets if a["name"] == tarball.name), None)
        if not match:
            raise RuntimeError(f"after upload, {tarball.name} not in release assets")
        bundle_url = match["url"]
    elif backend == "local":
        bundle_url = f"file://{tarball}"
    else:
        raise ValueError(f"unknown backend {backend!r}")

    return CityBundle(
        city_code=city_code,
        city_name=manifest.get("city_name", ""),
        dataset_year=year,
        bundle_url=bundle_url,
        sha256=sha,
        bytes=nbytes,
        n_buildings=n,
        tool_version=__version__,
    )


def merge_into_index(index_path: Path, bundle: CityBundle) -> dict:
    """Insert or replace ``bundle`` in the on-disk cache index JSON."""
    if index_path.exists():
        idx = json.loads(index_path.read_text())
    else:
        idx = {"schema": 1, "updated": "", "cities": []}
    idx["updated"] = datetime.now(tz=timezone.utc).isoformat()
    idx["cities"] = [
        c for c in idx["cities"]
        if not (c["city_code"] == bundle.city_code and c["dataset_year"] == bundle.dataset_year)
    ]
    idx["cities"].append({
        "city_code": bundle.city_code,
        "city_name": bundle.city_name,
        "dataset_year": bundle.dataset_year,
        "bundle_url": bundle.bundle_url,
        "sha256": bundle.sha256,
        "bytes": bundle.bytes,
        "n_buildings": bundle.n_buildings,
        "tool_version": bundle.tool_version,
    })
    idx["cities"].sort(key=lambda c: (c["city_code"], c["dataset_year"]))
    index_path.write_text(json.dumps(idx, indent=2) + "\n", encoding="utf-8")
    return idx


def _shutil_unused() -> None:  # noqa: D401
    """Quiet `shutil` import; reserved for future local-extract fallback."""
    _ = shutil
