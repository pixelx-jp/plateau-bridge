"""World export (extension B): buildings.parquet → SDF + record sidecar.

Produces a navigable simulation world shared by sixth-sense (Nav2 costmap) and
terra-incognita (headless episodes), plus a sidecar that joins every SDF model
back to the record layer. Emitted under ``world/<city>/``:

  * ``world.sdf``            — Gazebo SDF: ground + sun + one static box model
                               per building (footprint extent × height).
  * ``record_sidecar.parquet`` — one row per SDF ``feature_id`` ↔ ``building_uid``
                               with local ENU coords and the rasterisable
                               hazard/coverage + condition columns.
  * ``index.json``           — CRS, origin, bbox, file map, coverage layers.

Honesty guarantee carried into the sim: "unsurveyed" is an **explicit layer**,
not free space. The sidecar keeps each ``{hazard}_covered`` flag and an
``unsurveyed_all`` marker, and uncovered hazards keep ``{hazard}_value`` null —
never 0/safe. A costmap builder must paint uncovered cells as a distinct
unknown layer, exactly as the 2D record layer does.
"""

from __future__ import annotations

import json
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from plateau_bridge.records import DEFAULT_CRS
from plateau_bridge.schema import DEPTH_HAZARDS, HazardKind

DEFAULT_BUILDING_HEIGHT_M = 6.0
DEFAULT_FOOTPRINT_SIZE_M = 8.0
_EARTH_LAT_M_PER_DEG = 110_540.0
_EARTH_LON_M_PER_DEG = 111_320.0


@dataclass(frozen=True)
class WorldExportResult:
    out_dir: Path
    sdf_path: Path
    sidecar_path: Path
    index_path: Path
    n_features: int
    origin_lat: float
    origin_lon: float


def _local_xy(lat: float, lon: float, olat: float, olon: float) -> tuple[float, float]:
    """Equirectangular ENU metres about (olat, olon)."""
    x = (lon - olon) * _EARTH_LON_M_PER_DEG * math.cos(math.radians(olat))
    y = (lat - olat) * _EARTH_LAT_M_PER_DEG
    return x, y


def _hazard_value_columns() -> dict[str, str]:
    """attribute-ish name -> on-disk value column, per hazard."""
    out: dict[str, str] = {}
    for kind in HazardKind:
        out[kind.value] = (
            f"{kind.value}_depth_max" if kind in DEPTH_HAZARDS else f"{kind.value}_in_zone"
        )
    return out


def _footprint_size(geom_wkb: Any) -> tuple[float, float]:
    if not isinstance(geom_wkb, (bytes, bytearray)):
        return DEFAULT_FOOTPRINT_SIZE_M, DEFAULT_FOOTPRINT_SIZE_M
    try:
        from shapely import wkb as _wkb

        geom = _wkb.loads(bytes(geom_wkb))
        minx, miny, maxx, maxy = geom.bounds
        # bounds are in degrees; convert spans to metres at this latitude
        cy = (miny + maxy) / 2.0
        sx = max(1.0, (maxx - minx) * _EARTH_LON_M_PER_DEG * math.cos(math.radians(cy)))
        sy = max(1.0, (maxy - miny) * _EARTH_LAT_M_PER_DEG)
        return sx, sy
    except Exception:  # noqa: BLE001 - geometry is best-effort for box sizing
        return DEFAULT_FOOTPRINT_SIZE_M, DEFAULT_FOOTPRINT_SIZE_M


def _read_rows(con: duckdb.DuckDBPyConnection, parquet: str, limit: int | None) -> list[dict]:
    cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM '{parquet}'").fetchall()}
    wanted = ["building_uid", "centroid_lat", "centroid_lon", "height", "geometry"]
    haz_cols = _hazard_value_columns()
    for kind in HazardKind:
        wanted.append(f"{kind.value}_covered")
        wanted.append(haz_cols[kind.value])
    for c in ("condition_covered", "condition_state"):
        if c in cols:
            wanted.append(c)
    select = ", ".join(c for c in wanted if c in cols)
    sql = f"SELECT {select} FROM '{parquet}' WHERE centroid_lat IS NOT NULL"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    cur = con.execute(sql)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, raw, strict=True)) for raw in cur.fetchall()]


def _build_sdf(world_name: str, models: list[dict]) -> bytes:
    sdf = ET.Element("sdf", version="1.9")
    world = ET.SubElement(sdf, "world", name=world_name)

    # sun
    light = ET.SubElement(world, "light", name="sun", type="directional")
    ET.SubElement(light, "direction").text = "-0.5 0.1 -0.9"

    # ground plane
    ground = ET.SubElement(world, "model", name="ground_plane")
    ET.SubElement(ground, "static").text = "true"
    glink = ET.SubElement(ground, "link", name="link")
    for tag in ("collision", "visual"):
        node = ET.SubElement(glink, tag, name=tag)
        geom = ET.SubElement(node, "geometry")
        plane = ET.SubElement(geom, "plane")
        ET.SubElement(plane, "normal").text = "0 0 1"
        ET.SubElement(plane, "size").text = "10000 10000"

    for m in models:
        model = ET.SubElement(world, "model", name=m["model_name"])
        ET.SubElement(model, "static").text = "true"
        ET.SubElement(model, "pose").text = (
            f"{m['x']:.3f} {m['y']:.3f} {m['z']:.3f} 0 0 0"
        )
        link = ET.SubElement(model, "link", name="link")
        size = f"{m['sx']:.3f} {m['sy']:.3f} {m['sz']:.3f}"
        for tag in ("collision", "visual"):
            node = ET.SubElement(link, tag, name=tag)
            geom = ET.SubElement(node, "geometry")
            box = ET.SubElement(geom, "box")
            ET.SubElement(box, "size").text = size
    return ET.tostring(sdf, encoding="utf-8", xml_declaration=True)


def export_world(
    parquet_path: str | Path,
    out_dir: str | Path,
    *,
    world_name: str = "plateau_world",
    max_buildings: int | None = None,
) -> WorldExportResult:
    """Export ``buildings.parquet`` to an SDF world + record sidecar + index."""
    parquet = str(parquet_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(":memory:")
    try:
        rows = _read_rows(con, parquet, max_buildings)
    finally:
        con.close()

    if not rows:
        raise ValueError("no buildings with a centroid to export")

    olat = min(r["centroid_lat"] for r in rows)
    olon = min(r["centroid_lon"] for r in rows)
    haz_cols = _hazard_value_columns()

    models: list[dict] = []
    sidecar_rows: list[dict] = []
    bbox = {"min_lat": olat, "min_lon": olon, "max_lat": olat, "max_lon": olon}

    for fid, r in enumerate(rows):
        lat, lon = r["centroid_lat"], r["centroid_lon"]
        bbox["max_lat"] = max(bbox["max_lat"], lat)
        bbox["max_lon"] = max(bbox["max_lon"], lon)
        x, y = _local_xy(lat, lon, olat, olon)
        sz = float(r.get("height") or DEFAULT_BUILDING_HEIGHT_M)
        sx, sy = _footprint_size(r.get("geometry"))
        model_name = f"bldg_{fid}"
        models.append({"model_name": model_name, "x": x, "y": y, "z": sz / 2.0,
                       "sx": sx, "sy": sy, "sz": sz})

        sc: dict[str, Any] = {
            "feature_id": fid,
            "building_uid": r["building_uid"],
            "model_name": model_name,
            "centroid_lat": lat,
            "centroid_lon": lon,
            "local_x": x,
            "local_y": y,
            "height": sz,
        }
        any_covered = False
        for kind in HazardKind:
            cov = bool(r.get(f"{kind.value}_covered"))
            any_covered = any_covered or cov
            sc[f"{kind.value}_covered"] = cov
            val = r.get(haz_cols[kind.value])
            # honesty: uncovered ⇒ value stays null, never 0/safe
            if not cov or val is None:
                sc[f"{kind.value}_value"] = None
            elif kind in DEPTH_HAZARDS:
                sc[f"{kind.value}_value"] = float(val)
            else:
                sc[f"{kind.value}_value"] = 1.0 if bool(val) else 0.0
        sc["condition_covered"] = bool(r.get("condition_covered"))
        sc["condition_state"] = r.get("condition_state") if sc["condition_covered"] else None
        sc["unsurveyed_all"] = not any_covered  # explicit unknown marker
        sidecar_rows.append(sc)

    # write SDF
    sdf_path = out / "world.sdf"
    sdf_path.write_bytes(_build_sdf(world_name, models))

    # write sidecar parquet (stable, named columns)
    sidecar_path = out / "record_sidecar.parquet"
    _write_sidecar(sidecar_rows, sidecar_path)

    # write index.json
    index_path = out / "index.json"
    index = {
        "crs": DEFAULT_CRS,
        "origin": {"lat": olat, "lon": olon},
        "bbox": bbox,
        "n_features": len(rows),
        "world_name": world_name,
        "local_frame": "ENU metres, equirectangular about origin",
        "files": {"sdf": sdf_path.name, "sidecar": sidecar_path.name},
        "coverage_layers": [k.value for k in HazardKind] + ["condition"],
        "honesty_note": (
            "unsurveyed is an explicit layer: join on feature_id, treat "
            "{hazard}_covered=false / unsurveyed_all=true as a distinct unknown "
            "layer — never free space, never depth 0."
        ),
    }
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    return WorldExportResult(
        out_dir=out, sdf_path=sdf_path, sidecar_path=sidecar_path, index_path=index_path,
        n_features=len(rows), origin_lat=olat, origin_lon=olon,
    )


def _write_sidecar(rows: list[dict], path: Path) -> None:
    fields: list[pa.Field] = [
        pa.field("feature_id", pa.int32(), nullable=False),
        pa.field("building_uid", pa.string(), nullable=False),
        pa.field("model_name", pa.string(), nullable=False),
        pa.field("centroid_lat", pa.float64()),
        pa.field("centroid_lon", pa.float64()),
        pa.field("local_x", pa.float64()),
        pa.field("local_y", pa.float64()),
        pa.field("height", pa.float32()),
    ]
    for kind in HazardKind:
        fields.append(pa.field(f"{kind.value}_covered", pa.bool_()))
        fields.append(pa.field(f"{kind.value}_value", pa.float64()))
    fields.append(pa.field("condition_covered", pa.bool_()))
    fields.append(pa.field("condition_state", pa.string()))
    fields.append(pa.field("unsurveyed_all", pa.bool_()))
    schema = pa.schema(fields)
    cols = {name: [r.get(name) for r in rows] for name in schema.names}
    pq.write_table(pa.table(cols, schema=schema), path)
