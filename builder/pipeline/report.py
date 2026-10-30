"""size_report.md / size_report.json writer."""
import json
import math
import os

CLASS_NAMES = {
    1: "major road", 2: "secondary/tertiary", 3: "minor road", 4: "track/service", 5: "path",
    6: "rail/aerialway", 7: "river", 8: "stream", 9: "contour minor", 10: "contour index",
    16: "water", 17: "wetland", 18: "glacier", 24: "peak", 25: "hut", 26: "spring", 27: "place",
    28: "saddle", 100: "DEM grid (+u16 len)", 101: "tile v2 flags byte",
}


def b64len(n):
    return 4 * math.ceil(n / 3) if n else 0


def kb(n):
    return f"{n / 1024:.1f} KB"


def write_report(out, name, origin, size, manifest_bytes, tiles, per_class, route, bench, config, encoding, dem=None):
    lod_stats = {}
    for lod in (0, 1):
        ts = [t for t in tiles if t["lod"] == lod]
        sz = sorted(t["bytes"] for t in ts)
        lod_stats[lod] = {
            "tiles": len(ts),
            "bytes": sum(sz),
            "max_tile": max(sz) if sz else 0,
            "median_tile": sz[len(sz) // 2] if sz else 0,
            "mean_tile": round(sum(sz) / len(sz)) if sz else 0,
            "base64_bytes_per_tile_sum": sum(b64len(s) for s in sz),
            "features": sum(t["features"] for t in ts),
            "points": sum(t["points"] for t in ts),
            "thinned_tiles": sum(1 for t in ts if t["thinned_step"]),
            "budget": config[lod]["budget"],
        }
    route_bytes = route["bytes"] if route else 0
    total = manifest_bytes + lod_stats[0]["bytes"] + lod_stats[1]["bytes"] + route_bytes
    total_b64 = (b64len(manifest_bytes) + lod_stats[0]["base64_bytes_per_tile_sum"]
                 + lod_stats[1]["base64_bytes_per_tile_sum"] + b64len(route_bytes))
    area_km2 = size[0] * size[1] / 1e6
    map_bytes = lod_stats[0]["bytes"] + lod_stats[1]["bytes"]
    # extrapolation: linear by area (this area is dense: town + mine + steep relief -> upper bound)
    per100 = map_bytes / area_km2 * 100
    # index for 100x100 km: 98x98 LOD0 + 25x25 LOD1 entries of 6 bytes
    idx_100 = (98 * 98 + 25 * 25) * 6
    extrap = {
        "per_10x10km_bytes": round(per100),
        "per_100x100km_bytes": round(per100 * 100 + idx_100),
        "per_100x100km_base64_bytes": round((per100 * 100 + idx_100) * 4 / 3),
        "per_100x100km_median_tile_based_bytes": round(lod_stats[0]["median_tile"] * 98 * 98
                                                       + lod_stats[1]["median_tile"] * 25 * 25 + idx_100),
        "index_100x100km_bytes": idx_100,
    }
    data = {
        "package": name, "origin": origin, "size_m": size, "encoding": encoding,
        "total_bytes": total, "total_base64_bytes": total_b64,
        "manifest_bytes": manifest_bytes, "route": route, "dem": dem,
        "lod": lod_stats,
        "per_class": {str(lod): {str(c): {"name": CLASS_NAMES.get(c, "?"), "bytes": round(v[0]), "features": v[1],
                                          "points": v[2]} for c, v in sorted(pc.items())}
                      for lod, pc in per_class.items()},
        "tiles": tiles,
        "extrapolation": extrap,
        "bench": bench,
    }
    with open(os.path.join(out, "size_report.json"), "w") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)

    L = []
    L.append(f"# Size report: `{name}`\n")
    L.append(f"Origin {origin[0]:.5f} N {origin[1]:.5f} E, {size[0]} x {size[1]} m, tile encoding `{encoding}`.\n")
    if dem:
        L.append(f"Version 2: LOD{dem['lod']} relief as DEM grid, {dem['n']} cells per tile "
                 f"({1024 // dem['n']} m), quantum {dem['quantum_m']} m, cubic x2 on the watch: {dem['cubic']}; "
                 f"contours {dem['contour_step_m']} m / index {dem['index_every_m']} m traced on the watch "
                 f"(no cls 9/10 vectors on LOD{dem['lod']}, no cls 9 on LOD1).\n")
    L.append("| item | bytes | base64 |")
    L.append("|---|---:|---:|")
    L.append(f"| manifest.bin | {manifest_bytes} | {b64len(manifest_bytes)} |")
    for lod in (0, 1):
        s = lod_stats[lod]
        L.append(f"| LOD{lod} tiles ({s['tiles']}) | {s['bytes']} ({kb(s['bytes'])}) | {s['base64_bytes_per_tile_sum']} |")
    if route:
        L.append(f"| route.bin ({route['points']} pts, {route['waypoints']} wpts) | {route_bytes} | {b64len(route_bytes)} |")
    L.append(f"| **total** | **{total} ({kb(total)})** | **{total_b64} ({kb(total_b64)})** |")
    L.append("")
    L.append("base64 column = sum over individually encoded blobs (one resource string per tile).\n")
    L.append("## Per LOD\n")
    L.append("| LOD | tiles | features | points | max tile | median | mean | budget | thinned tiles |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for lod in (0, 1):
        s = lod_stats[lod]
        L.append(f"| {lod} | {s['tiles']} | {s['features']} | {s['points']} | {s['max_tile']} | {s['median_tile']} "
                 f"| {s['mean_tile']} | {s['budget']} | {s['thinned_tiles']} |")
    L.append("")
    L.append("## Per class\n")
    L.append("| LOD | cls | name | features | points | bytes | share |")
    L.append("|---|---:|---|---:|---:|---:|---:|")
    for lod in (0, 1):
        tot = lod_stats[lod]["bytes"] or 1
        for c, v in sorted(per_class.get(lod, {}).items(), key=lambda kv: -kv[1][0]):
            L.append(f"| {lod} | {c} | {CLASS_NAMES.get(c, '?')} | {v[1]} | {v[2]} | {round(v[0])} | {100 * v[0] / tot:.1f}% |")
    L.append("")
    L.append("## Extrapolation\n")
    L.append(f"- per 10x10 km (map data, both LODs): **{kb(extrap['per_10x10km_bytes'])}**")
    L.append(f"- per 100x100 km, linear by area: **{extrap['per_100x100km_bytes'] / 1e6:.1f} MB** "
             f"(base64 {extrap['per_100x100km_base64_bytes'] / 1e6:.1f} MB), incl. "
             f"{kb(extrap['index_100x100km_bytes'])} tile index")
    L.append(f"- per 100x100 km, median-tile based: {extrap['per_100x100km_median_tile_based_bytes'] / 1e6:.1f} MB")
    L.append("- This area (town, apatite mine, 1000 m relief) is denser than typical wilderness, so "
             "the linear figure is an upper-end estimate.\n")
    if bench:
        L.append("## Encoding benchmark (same features, all tiles)\n")
        L.append("| encoding | LOD0 bytes | LOD0 max | LOD1 bytes | LOD1 max | total | B/point | py decode us/pt |")
        L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for name_, r in bench["encodings"].items():
            L.append(f"| {name_} | {r['lod0_bytes']} | {r['lod0_max_tile']} | {r['lod1_bytes']} | {r['lod1_max_tile']} "
                     f"| {r['total_bytes']} | {r['bytes_per_point']} | {r['py_decode_us_per_point']} |")
        ds = bench["delta_stats"]
        L.append(f"\nDeltas: {ds['deltas']}, fit in a nibble pair (-8..7): {ds['fit_nibble_pct']}%, "
                 f"fit 1-byte varints (-63..63): {ds['fit_1byte_varint_pct']}%.\n")
        L.append("Reference compressors on the chosen tiles.bin (no decompressor on the watch; for comparison only):\n")
        L.append("| compressor | bytes | ratio |")
        L.append("|---|---:|---:|")
        raw = bench["compressors_on_tiles"]["raw"]
        for k, v in bench["compressors_on_tiles"].items():
            L.append(f"| {k} | {v} | {v / raw:.2f} |")
        L.append("")
    L.append("## Tiles (largest 10)\n")
    L.append("| LOD | tx | ty | bytes | features | points | thinned |")
    L.append("|---|---:|---:|---:|---:|---:|---:|")
    for t in sorted(tiles, key=lambda t: -t["bytes"])[:10]:
        L.append(f"| {t['lod']} | {t['tx']} | {t['ty']} | {t['bytes']} | {t['features']} | {t['points']} | {t['thinned_step']} |")
    L.append("")
    with open(os.path.join(out, "size_report.md"), "w") as f:
        f.write("\n".join(L))
