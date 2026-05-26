"""`plateau` command-line interface.

Subcommands:
- ``plateau info`` — list cities in the bundled catalog.
- ``plateau build <city_code>`` — run Gate A → B → C end-to-end.
- ``plateau cache ls|clear`` — inspect / clean the on-disk cache (platformdirs).
"""

from __future__ import annotations

import contextlib
import logging
import shutil
from functools import lru_cache
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from plateau_bridge import __version__
from plateau_bridge.catalog import load_registry, resolve_city
from plateau_bridge.config import load_settings
from plateau_bridge.pipeline import run_gate_a, run_gate_b, run_gate_c
from plateau_bridge.verify import verify as verify_bundle


@lru_cache(maxsize=1)
def _completion_table() -> tuple[tuple[str, str], ...]:
    """Build the (code → "city_name year") table once per process.

    Shell completion forks a fresh Python for every tab press; lru_cache
    inside that subprocess still saves us the JSON re-parse on the
    multiple internal `load_registry()` calls Typer makes during a single
    completion. For the same-process case (programmatic use) the cache is
    a clear win.
    """
    try:
        reg = load_registry()
    except Exception:  # noqa: BLE001
        return tuple()
    from plateau_bridge.catalog import _SLUG_TO_CODE
    code_to_slug = {code: slug for slug, code in _SLUG_TO_CODE.items()}
    seen: dict[str, tuple[int, str]] = {}
    for (code, year), cat in reg.items():
        prev = seen.get(code)
        if prev is None or year > prev[0]:
            seen[code] = (year, f"{cat.city_name} {year}")
    rows: list[tuple[str, str]] = []
    for code, (_year, label) in seen.items():
        rows.append((code, label))
        slug = code_to_slug.get(code)
        if slug:
            rows.append((slug, label))
    return tuple(sorted(rows))


def _complete_city(incomplete: str) -> list[tuple[str, str]]:
    """Dynamic shell-completion for the city positional arg.

    Accepts both JIS codes (``13113``) and slugs (``shibuya``). Reads the
    bundled catalog so tab-completion stays in sync with whatever cities
    ship in the installed wheel — no hard-coded list to drift. Yields
    ``(value, "city_name year")`` tuples; Typer renders the second
    element as a hint next to each candidate.
    """
    inc = incomplete.lower()
    return [(c, h) for c, h in _completion_table() if c.lower().startswith(inc)]


def _complete_gates(incomplete: str) -> list[str]:
    return [g for g in ("A", "B", "C", "AB", "BC", "ABC") if g.startswith(incomplete)]

app = typer.Typer(help="plateau-bridge: a trustworthy PLATEAU pipeline")
cache_app = typer.Typer(help="Manage the on-disk cache")
app.add_typer(cache_app, name="cache")
console = Console()


@app.callback()
def main(verbose: bool = typer.Option(False, "-v", "--verbose")) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@app.command()
def version() -> None:
    """Print version."""
    console.print(f"plateau-bridge {__version__}")


@app.command()
def info() -> None:
    """List cities + datasets in the bundled registry."""
    registry = load_registry()
    if not registry:
        console.print("[yellow]No catalog entries bundled. See README to add cities.[/yellow]")
        return
    table = Table(title="Bundled cities")
    table.add_column("city_code")
    table.add_column("name")
    table.add_column("year")
    table.add_column("themes")
    for cat in sorted(registry.values(), key=lambda c: (c.city_code, c.dataset_year)):
        themes = ", ".join(sorted({e.theme + ("/" + e.hazard_kind if e.hazard_kind else "") for e in cat.entries}))
        table.add_row(cat.city_code, cat.city_name, str(cat.dataset_year), themes)
    console.print(table)


@app.command()
def build(
    city: str = typer.Argument(
        ..., help="City slug (`shibuya`) or 5-digit JIS code (`13113`)",
        autocompletion=_complete_city,
    ),
    year: int = typer.Option(None, "--year", help="Dataset year (default: latest)"),
    out: Path = typer.Option(Path("out"), "--out", "-o", help="Output directory"),
    gates: str = typer.Option(
        "ABC", "--gates", help="Which gates to run, in order",
        autocompletion=_complete_gates,
    ),
    skip_3dtiles: bool = typer.Option(False, "--skip-3dtiles", help="Skip 3D Tiles emission (faster Gate A)"),
    admin: Path = typer.Option(
        None, "--admin",
        help="Path to admin boundary polygon (GeoJSON/Shapefile/FGB). "
             "Defaults to the bundled Tokyo polygons; pass --no-admin to disable.",
    ),
    no_admin: bool = typer.Option(False, "--no-admin", help="Skip admin boundary lookup entirely"),
    no_hazards: bool = typer.Option(
        False, "--no-hazards",
        help="Skip hazard intersection (much faster for huge cities; hazard fields stay NULL/unknown)",
    ),
    prune_cache: bool = typer.Option(
        False, "--prune-cache",
        help="After a successful build, delete this city's unzipped dataset "
             "cache (~3–18 GB each). Use when running many cities sequentially "
             "to keep the on-disk footprint bounded. Re-runs will re-download.",
    ),
) -> None:
    """Build the full artifact bundle for one city."""
    try:
        city = resolve_city(city)
    except KeyError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    registry = load_registry()
    key_year = year
    if key_year is None:
        years = [k[1] for k in registry if k[0] == city]
        if not years:
            console.print(f"[red]No catalog entry for city {city!r}[/red]")
            raise typer.Exit(1)
        key_year = max(years)
    catalog = registry[(city, key_year)]
    out.mkdir(parents=True, exist_ok=True)

    a_result = None
    if "A" in gates:
        console.print(f"[bold cyan]Gate A[/bold cyan] — buildings.parquet for {city} {key_year}")
        admin_arg = None
        if no_admin:
            pass  # explicit opt-out
        elif admin is not None:
            from plateau_bridge.admin import load_admin_from_path
            admin_arg = load_admin_from_path(admin)
        a_result = run_gate_a(
            catalog, out, emit_3dtiles=not skip_3dtiles, admin_boundary=admin_arg,
            skip_hazards=no_hazards,
        )
        console.print(f"  → {a_result.buildings_parquet}")

    # Helper: load existing Gate-A artifacts from disk when Gate B/C is
    # invoked without Gate A in the same call. This lets `--gates B` or
    # `--gates BC` resume from an existing build (e.g. after `plateau hazard`
    # added hazard columns to the parquet).
    def _load_existing_a():
        import geopandas as gpd
        parquet = out / "buildings.parquet"
        if not parquet.exists():
            console.print(f"[red]no {parquet}; run `--gates A` first[/red]")
            raise typer.Exit(1)
        existing_gdf = gpd.read_parquet(parquet)
        # Try cached 3D Tiles location (gate_a writes here).
        tiles_dir = out / "_work" / "bldg" / "3dtiles"
        if not (tiles_dir / "tileset.json").exists():
            # Re-emit 3D Tiles via nusamai; Gate A's converter is idempotent.
            console.print("[yellow]3D Tiles not cached; running nusamai 3dtiles sink (~5–30 min)[/yellow]")
            from plateau_bridge.config import load_settings
            from plateau_bridge.sources.citygml import convert_buildings
            from plateau_bridge.sources.download import fetch_and_unzip
            settings = load_settings()
            bldg_entry = catalog.building()
            bldg_root = fetch_and_unzip(bldg_entry.url, settings.cache_dir / "datasets")
            bldg_src = bldg_root / bldg_entry.udx_subdir if bldg_entry.udx_subdir else bldg_root
            convert_buildings(
                bldg_src, out / "_work" / "bldg",
                converter_bin=settings.converter_bin, emit_3dtiles=True,
            )
        return existing_gdf, tiles_dir if (tiles_dir / "tileset.json").exists() else None

    b_result = None
    if "B" in gates:
        if a_result is not None and a_result.tiles3d_dir is not None:
            gdf_b, tiles_b = a_result.gdf, a_result.tiles3d_dir
        else:
            gdf_b, tiles_b = _load_existing_a()
        if tiles_b is None:
            console.print("[yellow]Gate B skipped — no 3D Tiles available[/yellow]")
        else:
            console.print("[bold cyan]Gate B[/bold cyan] — 3D Tiles style tables")
            b_result = run_gate_b(gdf_b, out, tiles_b)
            console.print(f"  → {b_result.style_dir} (verified={b_result.verified})")

    if "C" in gates:
        if b_result is not None:
            gdf_c = b_result.gdf
        elif a_result is not None:
            gdf_c = a_result.gdf
        else:
            gdf_c, _ = _load_existing_a()
        console.print("[bold cyan]Gate C[/bold cyan] — PMTiles + FGB + zoning backfill")
        c_result = run_gate_c(gdf_c, catalog, out)
        console.print(f"  → {c_result.pmtiles_path}, {len(c_result.fgb_paths)} FGB shard(s)")

    if prune_cache:
        from plateau_bridge.config import load_settings
        from plateau_bridge.sources.download import cache_path_for_url
        settings = load_settings()
        datasets_root = settings.cache_dir / "datasets"
        freed_bytes = 0
        removed = 0
        for entry in catalog.entries:
            d = cache_path_for_url(entry.url, datasets_root)
            if d.exists():
                # Walk to compute size before removal (best-effort; ignore errors).
                for p in d.rglob("*"):
                    with contextlib.suppress(OSError):
                        freed_bytes += p.stat().st_size
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        if removed:
            console.print(
                f"[dim]pruned {removed} cached dataset dir(s), "
                f"freed ~{freed_bytes / 1024 / 1024 / 1024:.1f} GB[/dim]"
            )


@app.command()
def poster(
    parquet: Path = typer.Argument(Path("out/buildings.parquet"), help="buildings.parquet"),
    out: Path = typer.Option(Path("out/age_rainbow.png"), "--out", "-o"),
    title: str = typer.Option(None, "--title"),
    subtitle: str = typer.Option(None, "--subtitle"),
    bbox: str = typer.Option(None, "--bbox", help="min_lon,min_lat,max_lon,max_lat"),
    dpi: int = typer.Option(220, "--dpi"),
    svg: bool = typer.Option(False, "--svg", help="Also emit SVG"),
    color_by: str = typer.Option(
        "year_built", "--color-by",
        help="Column to colour by. Use 'height' or 'usage' for cities where yearOfConstruction is unpopulated.",
    ),
) -> None:
    """Render the Building Age Rainbow poster (requires the `poster` extra)."""
    try:
        from plateau_bridge.poster import render_age_rainbow
    except ImportError as e:
        console.print(f"[red]matplotlib not installed:[/red] {e}\nTry `pip install 'plateau-bridge[poster]'`.")
        raise typer.Exit(1) from e
    bbox_tup = None
    if bbox:
        parts = [float(x) for x in bbox.split(",")]
        if len(parts) != 4:
            console.print("[red]--bbox must be min_lon,min_lat,max_lon,max_lat[/red]")
            raise typer.Exit(2)
        bbox_tup = (parts[0], parts[1], parts[2], parts[3])
    res = render_age_rainbow(
        parquet, out, title=title, subtitle=subtitle, bbox=bbox_tup, dpi=dpi,
        also_svg=svg, color_by=color_by,
    )
    console.print(f"  → {res.png_path}  ({res.n_buildings_rendered:,} buildings rendered)")
    if res.svg_path:
        console.print(f"  → {res.svg_path}")


@app.command()
def hazard(
    city: str = typer.Argument(
        ..., help="City code (must be in catalog)",
        autocompletion=_complete_city,
    ),
    out: Path = typer.Option(Path("out"), "--out", "-o", help="Output directory containing buildings.parquet"),
    year: int = typer.Option(None, "--year"),
) -> None:
    """Add (or re-run) hazard intersection on an existing buildings.parquet.

    Pair this with ``plateau build CITY --no-hazards`` for huge cities where
    the hazard sjoin dominates wall-clock time. Re-uses cached hazard GeoJSON
    when present (the ``_work/hzd_*`` dirs the original build wrote).
    """
    from plateau_bridge.pipeline.hazard_only import run_hazard_only
    registry = load_registry()
    key_year = year if year is not None else max(k[1] for k in registry if k[0] == city)
    catalog = registry[(city, key_year)]
    console.print(f"[bold cyan]hazard[/bold cyan] — re-running intersection for {city} {key_year}")
    res = run_hazard_only(catalog, out)
    console.print(f"  → {res.buildings_parquet}  ({res.n_buildings:,} buildings)")


@app.command()
def diff(
    a: Path = typer.Argument(..., help="buildings.parquet — older / reference dataset"),
    b: Path = typer.Argument(..., help="buildings.parquet — newer / comparison dataset"),
) -> None:
    """Compare two `buildings.parquet` across PLATEAU dataset_years.

    Reports: matched count, only-in-A (gone), only-in-B (new), and per-hazard
    deltas (newly_covered / newly_hit / no_longer_hit / depth_grew/shrank).
    """
    from plateau_bridge.diff import diff as compute_diff
    rep = compute_diff(a, b)
    console.print(
        f"[bold]plateau diff[/bold]  match_key=[cyan]{rep.match_key}[/cyan]\n"
        f"  {a}: {rep.n_a:,} buildings\n"
        f"  {b}: {rep.n_b:,} buildings"
    )
    console.print(f"  matched: {rep.n_matched:,}   only-in-A (gone): {rep.n_only_in_a:,}   only-in-B (new): {rep.n_only_in_b:,}")
    if rep.hazard_deltas:
        table = Table(title="Hazard deltas (B relative to A)")
        table.add_column("kind")
        table.add_column("newly_covered", justify="right")
        table.add_column("newly_hit", justify="right")
        table.add_column("no_longer_hit", justify="right")
        table.add_column("depth_grew", justify="right")
        table.add_column("depth_shrank", justify="right")
        for kind, d in rep.hazard_deltas.items():
            table.add_row(
                kind,
                f"{d.get('newly_covered',0):,}",
                f"{d.get('newly_hit',0):,}",
                f"{d.get('no_longer_hit',0):,}",
                f"{d.get('depth_grew',0):,}",
                f"{d.get('depth_shrank',0):,}",
            )
        console.print(table)


@app.command()
def bench(
    parquet: Path = typer.Argument(Path("out/buildings.parquet"), help="buildings.parquet"),
    iterations: int = typer.Option(10, "--iterations", "-n", help="Timed runs per query"),
    warmup: int = typer.Option(1, "--warmup", help="Untimed warmup runs per query"),
) -> None:
    """Benchmark the canonical DuckDB query suite against a buildings.parquet."""
    from plateau_bridge.bench import run_suite
    results = run_suite(parquet, iterations=iterations, warmup=warmup)
    table = Table(title=f"Bench · {parquet} · n={iterations}")
    table.add_column("query")
    table.add_column("median_ms", justify="right")
    table.add_column("p99_ms", justify="right")
    table.add_column("rows", justify="right")
    for r in results:
        table.add_row(r.name, f"{r.median_ms:.2f}", f"{r.p99_ms:.2f}", f"{r.rows_returned:,}")
    console.print(table)


@app.command()
def verify(
    out_dir: Path = typer.Argument(Path("out"), help="Output directory from `plateau build`"),
    strict: bool = typer.Option(False, "--strict", help="Exit non-zero on any warning"),
) -> None:
    """Run health checks on a built artifact bundle.

    Exits 0 if there are no errors (or no warnings under --strict).
    Errors include the honesty-invariant violation (covered=false with depth>0)
    and duplicate building_uid; warnings include orphan source ids and
    manifest count mismatches.
    """
    report = verify_bundle(out_dir)
    icon = {"error": "[red]✗[/red]", "warn": "[yellow]![/yellow]", "info": "[cyan]i[/cyan]"}
    console.print(f"[bold]plateau verify[/bold] {out_dir}  · {report.n_buildings} buildings")
    if not report.findings:
        console.print("[green]✓ no findings.[/green]")
    else:
        for f in report.findings:
            console.print(f"  {icon.get(f.severity, '?')} [{f.code}] {f.message}")
    if report.errors or (strict and report.warnings):
        raise typer.Exit(1)


@cache_app.command("ls")
def cache_ls() -> None:
    """Print the on-disk cache layout."""
    s = load_settings()
    console.print(f"cache_dir = {s.cache_dir}")
    if not s.cache_dir.exists():
        return
    for p in sorted(s.cache_dir.rglob("*"))[:200]:
        rel = p.relative_to(s.cache_dir)
        kind = "DIR " if p.is_dir() else "FILE"
        console.print(f"  {kind} {rel}")


@cache_app.command("add")
def cache_add(
    city: str = typer.Argument(
        ..., autocompletion=_complete_city,
        help="City slug (`shibuya`) or 5-digit JIS code (`13113`)",
    ),
    out_dir: Path = typer.Option(None, "--out", "-o", help="Destination (default: ./out_<city>)"),
    index_url: str = typer.Option(None, "--index", help="Custom cache-index JSON URL"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing out dir"),
) -> None:
    """Download a pre-built city bundle (skip the build pipeline).

    The default index points at the plateau-bridge GitHub Releases mirror.
    See docs/DATA.md for distribution strategy and docs/DISTRIBUTION.md
    for maintainer-side push instructions.
    """
    from plateau_bridge.distribution import DEFAULT_INDEX_URL, add
    try:
        city = resolve_city(city)
    except KeyError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    url = index_url or DEFAULT_INDEX_URL
    try:
        target = add(city, index_url=url, target_dir=out_dir, force=force)
    except KeyError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    console.print(f"  → {target}")


@cache_app.command("push")
def cache_push(
    out_dir: Path = typer.Argument(..., help="Output directory of a previous `plateau build`"),
    backend: str = typer.Option("github-releases", "--backend", help="github-releases / local"),
    release_tag: str = typer.Option("data-v1", "--tag", help="GitHub release tag to upload into"),
    index_path: Path = typer.Option(Path("distribution/index.json"), "--index", help="Local cache index to update"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Pack but don't upload"),
) -> None:
    """Pack an `out_<city>/` directory and upload it as a cache bundle.

    Maintainers only — produces a tarball and inserts it into the cache
    index JSON. End users use `cache add` instead.
    """
    from plateau_bridge.distribution import merge_into_index, push
    bundle = push(out_dir, backend=backend, release_tag=release_tag, dry_run=dry_run)
    merge_into_index(index_path, bundle)
    console.print(f"  → {bundle.bundle_url}")
    console.print(f"  → updated {index_path}")


@cache_app.command("clear")
def cache_clear(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Delete the entire on-disk cache."""
    s = load_settings()
    if not yes:
        typer.confirm(f"Delete {s.cache_dir} and all contents?", abort=True)
    shutil.rmtree(s.cache_dir, ignore_errors=True)
    console.print(f"removed {s.cache_dir}")


if __name__ == "__main__":
    app()
