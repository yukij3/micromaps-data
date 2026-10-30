"""Build an MMAP package (docs/FORMAT.md) from cached/fetched OSM + DEM.

    python3 -m pipeline.build --bbox S,W,N,E --out out/khibiny [--course course.gpx|course.fit]
    python3 -m pipeline.build --origin 67.59,33.58 --size 10000 --out out/khibiny

--contours dem (default): version 2, LOD0 relief as a DEM grid per tile (FORMAT.md 8),
contours are traced on the watch; LOD1 keeps vector index contours only.
--contours vector: version 1, contours as cls 9/10 lines on both LODs.

Outputs in --out: manifest.bin, tiles.bin, tiles/<lod>/<tx>_<ty>.bin (slices of tiles.bin),
route.bin (if --course; GPX or FIT, detected by content, --gpx is an alias), size_report.md, size_report.json.
"""
import argparse
import json
import math
import os
import shutil
import sys
import time

from . import bench, dem, fetch
from .blocks import POINT_CAP, THIN, TILE_DROP
from .contours import DemGrid, contour_lines
from .encode import ENC_BITS, ENC_DEFAULT, ENC_NAMES, ENC_VEC2, encode_manifest, encode_tile, encode_tile_v2
from .geo import LODS, TILE_UNITS, Frame, bbox_from_origin, size_from_bbox
from .geom import (bbox, chain_exact, clip_line, clip_ring, dedupe_ring, line_length, quantize, ring_area,
                   simplify, simplify_ring)
from .label import ascii_label
from .osm import AREA, LINE, POINT, Feat, chain_osm_lines, kind_of, load_osm

ALL = set(range(1, 32))
CONFIG = {
    0: dict(
        eps_m=8.0,              # DP epsilon, metres (= 1 quantum, --q0; v1: 4)
        contour_step=20, index_every=100, dem_sigma_m=15.0,
        classes=ALL,
        min_closed_contour_m=60.0,   # drop closed contour loops with bbox diagonal below this
        min_area_m2={16: 300.0, 17: 2000.0, 18: 2000.0},
        min_line_m={},
        budget=6 * 1024,
    ),
    1: dict(
        eps_m=24.0,             # 1.5 quanta
        contour_step=50, index_every=250, dem_sigma_m=30.0,
        classes={1, 2, 3, 4, 6, 7, 9, 10, 16, 18, 24, 27},
        # minor contours are never drawn at LOD1 (style.json: zi 0-1 only, LOD1 = zi >= 3).
        # Dropped after order_features so the rest keeps the v1 order (identical rendering).
        drop_after_order={9},
        min_closed_contour_m=250.0,
        min_area_m2={16: 40000.0, 18: 100000.0},
        min_line_m={4: 300.0, 3: 150.0, 6: 300.0},
        budget=12 * 1024,
    ),
}
# LOD1 keeps railways but not aerialways/minor sidings: handled by Feat.minor + rail check below.

# --contours dem: DEM grid parameters (docs/FORMAT.md 8; choice measured in scripts/dem_compare.py)
DEM = dict(n=16, quantum_m=2, cubic=True, lod=0, contour_step_m=20, index_every_m=100)
# classes dropped from the vector layer when contours come from the DEM. LOD1 minor contours
# (cls 9) are never drawn either; they go via CONFIG[1]["drop_after_order"] (keeps v1 order).
DEM_DROP = {0: {9, 10}}


class TFeat:
    """Tile feature in integer tile units (what gets encoded)."""

    __slots__ = ("cls", "pts", "ele", "name")

    def __init__(self, cls, pts, ele=None, name=None):
        self.cls = cls
        self.pts = pts
        self.ele = ele
        self.name = name

    @property
    def kind(self):
        return kind_of(self.cls)


def prepare(osm_feats, contours, lod):
    """Select + simplify features for one LOD, in package metres."""
    cfg = CONFIG[lod]
    out = []
    for f in osm_feats:
        if f.cls not in cfg["classes"]:
            continue
        if lod >= 1 and f.minor:
            continue
        if f.kind == LINE:
            if line_length(f.pts) < cfg["min_line_m"].get(f.cls, 0.0):
                continue
            out.append(Feat(f.cls, simplify(f.pts, cfg["eps_m"])))
        elif f.kind == AREA:
            if abs(ring_area(f.pts)) < cfg["min_area_m2"].get(f.cls, 0.0):
                continue
            r = simplify_ring(f.pts, cfg["eps_m"])
            if len(r) >= 3:
                out.append(Feat(f.cls, r))
        else:
            out.append(f)
    for ele, pts, closed in contours:
        cls = 10 if ele % cfg["index_every"] == 0 else 9
        if cls not in cfg["classes"]:
            continue
        if closed:
            x0, y0, x1, y1 = bbox(pts)
            if math.hypot(x1 - x0, y1 - y0) < cfg["min_closed_contour_m"]:
                continue
            r = simplify_ring(pts, cfg["eps_m"])
            if len(r) < 3:
                continue
            pts = r + [r[0]]
        else:
            if line_length(pts) < cfg["min_closed_contour_m"]:
                continue
            pts = simplify(pts, cfg["eps_m"])
        out.append(Feat(cls, pts, ele if cls == 10 else None))
    return out


def cap_points(feats, lod):
    """Blocks only: at most POINT_CAP points per tile, the biggest places and highest peaks first."""
    S = LODS[lod][0]
    points = [f for f in feats if f.kind == POINT]
    points.sort(key=lambda f: (-f.rank, -(f.ele or 0)))
    used = {}
    keep = set()
    for f in points:
        key = (f.pts[0][0] // S, f.pts[0][1] // S, f.cls == 27)
        if used.get(key, 0) < POINT_CAP[lod][0 if f.cls == 27 else 1]:
            used[key] = used.get(key, 0) + 1
            keep.add(id(f))
    return [f for f in feats if f.kind != POINT or id(f) in keep]


def tile_features(feats, lod, width, height):
    """Clip + quantize into {(tx, ty): [TFeat]}."""
    S, q = LODS[lod]
    nx, ny = math.ceil(width / S), math.ceil(height / S)
    tiles = {}
    for f in feats:
        if f.kind == POINT:
            x, y = f.pts[0]
            if not (0 <= x < width and 0 <= y < height):
                continue
            tx, ty = int(x // S), int(y // S)
            p = quantize([(x, y)], tx * S, ty * S, q)
            tiles.setdefault((tx, ty), []).append(TFeat(f.cls, p, f.ele, f.name))
            continue
        x0, y0, x1, y1 = bbox(f.pts)
        if x1 < 0 or y1 < 0 or x0 > width or y0 > height:
            continue
        for ty in range(max(0, int(y0 // S)), min(ny - 1, int(y1 // S)) + 1):
            for tx in range(max(0, int(x0 // S)), min(nx - 1, int(x1 // S)) + 1):
                ox, oy = tx * S, ty * S
                rect = (ox, oy, min(ox + S, width), min(oy + S, height))
                if f.kind == LINE:
                    for piece in clip_line(f.pts, *rect):
                        qp = quantize(piece, ox, oy, q)
                        if len(qp) >= 2 and line_length(qp) >= 1.5:
                            tiles.setdefault((tx, ty), []).append(TFeat(f.cls, qp, f.ele, f.name))
                else:
                    r = clip_ring(f.pts, *rect)
                    if not r:
                        continue
                    qr = dedupe_ring(quantize(r, ox, oy, q))
                    if len(qr) >= 3 and abs(ring_area(qr)) >= 1.0:
                        tiles.setdefault((tx, ty), []).append(TFeat(f.cls, qr, f.ele, f.name))
    return tiles, nx, ny


def order_features(feats):
    """Chain touching lines, sort by class, then greedy nearest-neighbour within a class
    (lines may be reversed) to keep the first-point jump small."""
    lines = [f for f in feats if f.kind == LINE]
    rest = [f for f in feats if f.kind != LINE]
    chained = chain_exact([((f.cls, f.ele), f.pts) for f in lines])
    feats = rest + [TFeat(k[0], p, k[1]) for k, p in chained]
    out = []
    cx, cy = 0, 0
    for cls, has_ele in sorted({(f.cls, f.ele is not None) for f in feats}):
        group = [f for f in feats if f.cls == cls and (f.ele is not None) == has_ele]
        while group:
            best, bi, rev = None, -1, False
            for i, f in enumerate(group):
                d = abs(f.pts[0][0] - cx) + abs(f.pts[0][1] - cy)
                if best is None or d < best:
                    best, bi, rev = d, i, False
                if f.kind == LINE:
                    d = abs(f.pts[-1][0] - cx) + abs(f.pts[-1][1] - cy)
                    if d < best:
                        best, bi, rev = d, i, True
            f = group.pop(bi)
            if rev:
                f = TFeat(f.cls, f.pts[::-1], f.ele, f.name)
            out.append(f)
            cx, cy = f.pts[-1]
    return out


def thin(feats, step, eps_units):
    """Progressive thinning for over-budget tiles. step 1,2: re-simplify lines/areas with
    larger epsilon; step 3: also drop minor contours (cls 9); past that (blocks only) one more
    class of blocks.TILE_DROP per step."""
    factor = {1: 2.0, 2: 3.0}.get(step, 3.0)
    gone = TILE_DROP[:max(0, step - 3)]
    out = []
    for f in feats:
        if (step >= 3 and f.cls == 9) or f.cls in gone:
            continue
        if f.kind == LINE and f.cls in (4, 5, 8, 9, 10):
            closed = f.pts[0] == f.pts[-1]
            p = simplify(f.pts, eps_units * factor)
            if closed and len(p) < 4:
                continue
            out.append(TFeat(f.cls, p, f.ele, f.name))
        elif f.kind == AREA:
            p = simplify_ring(f.pts, eps_units * factor)
            if len(p) >= 3:
                out.append(TFeat(f.cls, p, f.ele, f.name))
        else:
            out.append(f)
    return out


def build_lod(osm_feats, grid, lod, width, height, enc, log):
    cfg = CONFIG[lod]
    t = time.time()
    levels = range(0, 9000, cfg["contour_step"])
    cl = contour_lines(grid, levels)
    feats = prepare(osm_feats, cl, lod)
    if cfg.get("cap_points"):
        feats = cap_points(feats, lod)
    tiles, nx, ny = tile_features(feats, lod, width, height)
    S, q = LODS[lod]
    tu = S // q
    eps_units = cfg["eps_m"] / q
    out = {}
    thinned = {}
    drop = cfg.get("drop_after_order", set())

    def ordered(fs):
        return [f for f in order_features(fs) if f.cls not in drop]

    for key in sorted(tiles):
        fs = ordered(tiles[key])
        blob = encode_tile(fs, enc, tu=tu)
        step = 0
        while len(blob) > cfg["budget"] and step < cfg.get("steps", 3):
            step += 1
            fs = ordered(thin(tiles[key], step, eps_units))
            blob = encode_tile(fs, enc, tu=tu)
        if step:
            thinned[key] = (step, len(blob))
        if fs:
            out[key] = fs
    log(f"lod{lod}: {len(cl)} contour lines, {len(feats)} feats, {len(out)} tiles, "
        f"{time.time() - t:.1f}s, thinned={thinned}")
    return out, nx, ny, thinned


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bbox", type=fetch.parse_bbox, help="S,W,N,E (origin = SW corner)")
    ap.add_argument("--origin", help="LAT,LON of the SW corner (alternative to --bbox)")
    ap.add_argument("--size", type=int, default=10000, help="metres, with --origin (square)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--name", help="package name (default: basename of --out)")
    ap.add_argument("--course", "--gpx", dest="course", metavar="FILE",
                    help="course to embed as route.bin: GPX or FIT (detected by content); --gpx is an alias")
    ap.add_argument("--raw", default="data/raw", help="download cache")
    ap.add_argument("--offline", action="store_true", help="fail instead of downloading")
    ap.add_argument("--light", action="store_true", help="the OSM query without the small ways of a city")
    ap.add_argument("--dem-zoom", type=int, default=12)
    ap.add_argument("--no-bench", action="store_true")
    ap.add_argument("--contours", choices=("dem", "vector"), default="dem",
                    help="dem: version-2 package with a DEM layer (default); vector: version 1")
    ap.add_argument("--dem-n", type=int, default=DEM["n"], help="DEM cells per 1024 m tile")
    ap.add_argument("--dem-q", type=int, default=DEM["quantum_m"], help="DEM quantum, metres")
    ap.add_argument("--dem-linear", action="store_true", help="no cubic x2 upsampling on the watch")
    ap.add_argument("--enc", choices=("vec2", "bits"), default=ENC_NAMES[ENC_DEFAULT],
                    help="vector tile encoding: vec2 (default, FORMAT.md 5.7) or bits (v1)")
    ap.add_argument("--q0", type=int, choices=(4, 8), default=LODS[0][1],
                    help="LOD0 quantum and DP epsilon, metres (8: pixel generalization, default; 4: v1)")
    ap.add_argument("--lod1-minor", action="store_true",
                    help="keep minor contours in LOD1 (v1 behaviour; they are never drawn)")
    ap.add_argument("--thin", type=int, choices=range(len(THIN)),
                    help="block profile, a rung of blocks.THIN (default: the normal build)")
    a = ap.parse_args(argv)
    LODS[0] = (LODS[0][0], a.q0)
    CONFIG[0]["eps_m"] = float(a.q0)
    rung = None if a.thin is None else THIN[a.thin]
    if rung:
        share = 4 if a.thin and a.size > 10000 else 1
        for lod in (0, 1):
            CONFIG[lod].update(budget=rung["budget"][lod] // share, cap_points=True, steps=3 + len(TILE_DROP),
                               eps_m=CONFIG[lod]["eps_m"] * rung["eps"],
                               dem_sigma_m=CONFIG[lod]["dem_sigma_m"] * rung.get("sigma", 1.0),
                               min_line_m=dict(CONFIG[lod]["min_line_m"], **rung.get("min_line", {})),
                               min_area_m2=dict(CONFIG[lod]["min_area_m2"], **rung.get("min_area", {})))
    use_dem = a.contours == "dem"
    dem_cfg = dict(DEM, n=a.dem_n, quantum_m=a.dem_q, cubic=not a.dem_linear) if use_dem else None
    if use_dem:
        for lod, drop in DEM_DROP.items():
            CONFIG[lod]["classes"] = set(CONFIG[lod]["classes"]) - drop

    def log(msg):
        print(msg, file=sys.stderr)

    if a.origin:
        lat0, lon0 = (float(v) for v in a.origin.split(","))
        width = height = a.size
        bb = bbox_from_origin(lat0, lon0, width, height)
    elif a.bbox:
        bb = a.bbox
        lat0, lon0 = bb[0], bb[1]
        width, height = size_from_bbox(*bb)
    else:
        ap.error("--bbox or --origin required")
    name = ascii_label(a.name or os.path.basename(os.path.normpath(a.out)), 255)
    frame = Frame(lat0, lon0)
    course = None
    if a.course:  # parse up front so a broken course fails before the slow map build
        from xml.etree.ElementTree import ParseError
        from .fit import FitError
        from .gpx import parse_course
        try:
            course = parse_course(a.course)
        except (FitError, OSError, ParseError) as e:
            ap.error(f"--course {a.course}: {e}")
    enc = ENC_VEC2 if a.enc == "vec2" else ENC_BITS
    if a.lod1_minor:
        CONFIG[1]["drop_after_order"] = set()

    osm_path = fetch.fetch_osm(bb, a.raw, a.offline, light=a.light)
    trange = fetch.fetch_dem(bb, a.raw, a.dem_zoom, a.offline)
    osm_feats = chain_osm_lines(load_osm(osm_path, frame, rung and rung.get("drop")))
    log(f"osm: {len(osm_feats)} features after chaining")

    margin = 300.0
    lod_tiles = {}
    meta = {}
    dem_blobs = {}
    for lod in (0, 1):
        grid = DemGrid(a.raw, a.dem_zoom, trange, frame, -margin, -margin, width + margin, height + margin,
                       10.0, CONFIG[lod]["dem_sigma_m"])
        tiles, nx, ny, thinned = build_lod(osm_feats, grid, lod, width, height, enc, log)
        lod_tiles[lod] = tiles
        meta[lod] = (nx, ny, thinned)
        if use_dem and lod == dem_cfg["lod"]:
            t = time.time()
            for ty in range(ny):
                for tx in range(nx):
                    K = dem.sample_tile(grid, tx, ty, dem_cfg["n"], dem_cfg["quantum_m"])
                    dem_blobs[(tx, ty)] = dem.encode(K, dem_cfg["n"])
            log(f"dem: {len(dem_blobs)} tiles, n {dem_cfg['n']}, q {dem_cfg['quantum_m']} m, "
                f"{sum(len(b) for b in dem_blobs.values())} B, {time.time() - t:.1f}s")

    # ---- write package
    if os.path.isdir(os.path.join(a.out, "tiles")):
        shutil.rmtree(os.path.join(a.out, "tiles"))
    os.makedirs(a.out, exist_ok=True)
    blob = bytearray()
    lods = []
    report_tiles = []
    per_class = {}
    for lod in (0, 1):
        nx, ny, thinned = meta[lod]
        S, q = LODS[lod]
        entries = []
        os.makedirs(os.path.join(a.out, "tiles", str(lod)), exist_ok=True)
        for ty in range(ny):
            for tx in range(nx):
                fs = lod_tiles[lod].get((tx, ty))
                db = dem_blobs.get((tx, ty)) if use_dem and lod == dem_cfg["lod"] else None
                if not fs and not db:
                    entries.append((len(blob), 0))
                    continue
                sizes = []
                tb = encode_tile(fs, enc, sizes, S // q) if fs else b""
                if use_dem:
                    tb = encode_tile_v2(db, tb)
                    if db:
                        sizes.append((100, len(db) + 2))
                    sizes.append((101, 1))
                fs = fs or []
                entries.append((len(blob), len(tb)))
                blob += tb
                with open(os.path.join(a.out, "tiles", str(lod), f"{tx}_{ty}.bin"), "wb") as f:
                    f.write(tb)
                for cls, n in sizes:
                    pc = per_class.setdefault(lod, {}).setdefault(cls, [0, 0, 0])
                    pc[0] += n
                    pc[1] += 1
                npts = sum(len(f.pts) for f in fs)
                for f in fs:
                    per_class[lod][f.cls][2] += len(f.pts)
                if db:
                    per_class[lod][100][2] += (dem_cfg["n"] + 1) ** 2
                report_tiles.append({"lod": lod, "tx": tx, "ty": ty, "bytes": len(tb), "features": len(fs),
                                     "points": npts, "thinned_step": thinned.get((tx, ty), (0,))[0]})
        lods.append({"lod": lod, "tile_m": S, "quantum_m": q, "nx": nx, "ny": ny, "entries": entries})
    manifest = encode_manifest(name, lat0, lon0, width, height, lods, enc, dem_cfg)
    with open(os.path.join(a.out, "manifest.bin"), "wb") as f:
        f.write(manifest)
    with open(os.path.join(a.out, "tiles.bin"), "wb") as f:
        f.write(blob)

    route_info = None
    rp = os.path.join(a.out, "route.bin")
    if course:
        from .gpx import route_blob
        _fmt, rname, latlons, wpts = course
        rb, rpts, rw = route_blob(frame, rname, latlons, wpts)
        with open(rp, "wb") as f:
            f.write(rb)
        route_info = {"source": os.path.basename(a.course), "name": rname, "input_points": len(latlons),
                      "points": len(rpts), "waypoints": len(rw), "bytes": len(rb)}
    elif os.path.exists(rp):
        os.remove(rp)

    bench_res = None
    if not a.no_bench:
        bench_res = bench.run(lod_tiles, bytes(blob), log)
    from .report import write_report
    write_report(a.out, name=name, origin=(lat0, lon0), size=(width, height), manifest_bytes=len(manifest),
                 tiles=report_tiles, per_class=per_class, route=route_info, bench=bench_res,
                 config=CONFIG, encoding=ENC_NAMES[enc], dem=dem_cfg)
    log(f"wrote {a.out}: manifest {len(manifest)} B, tiles {len(blob)} B"
        + (f", route {route_info['bytes']} B" if route_info else ""))


if __name__ == "__main__":
    main()
