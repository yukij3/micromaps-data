"""Download OSM (Overpass JSON) and DEM (AWS Terrain Tiles, Terrarium PNG) for a bbox.

Everything is cached under data/raw/; re-running is free. urllib honours the usual
http_proxy / https_proxy environment variables.

    python3 -m pipeline.fetch --bbox S,W,N,E [--raw data/raw] [--dem-zoom 12]
"""
import argparse
import hashlib
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request

UA = "microMAPS-pipeline/0.1 (offline maps for Garmin watches; contact trdimads@gmail.com)"
OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
TERRARIUM = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"


# a city answers 502 to the full query; these ways are the first a dense block loses anyway
LIGHT_WAYS = 'way[highway][highway!~"^(service|footway|steps|cycleway|pedestrian|corridor|platform)$"]'


def overpass_query(s, w, n, e, light=False):
    bb = "%.6f,%.6f,%.6f,%.6f" % (s, w, n, e)
    ways = LIGHT_WAYS if light else "way[highway]"
    return f"""[out:json][timeout:180][bbox:{bb}];
(
  {ways};
  way[railway~"^(rail|narrow_gauge)$"];
  way[aerialway];
  way[waterway~"^(river|canal|stream|ditch|drain|riverbank)$"];
  way[natural~"^(water|wetland|glacier)$"];
  way[landuse=reservoir];
  relation[type=multipolygon][natural~"^(water|wetland|glacier)$"];
  relation[type=multipolygon][landuse=reservoir];
  relation[type=multipolygon][waterway=riverbank];
  node[natural~"^(peak|volcano|saddle|spring)$"];
  node[mountain_pass=yes];
  node[tourism~"^(alpine_hut|wilderness_hut)$"];
  node[amenity~"^(shelter|drinking_water)$"];
  node[place~"^(city|town|village|hamlet)$"];
);
out geom qt;
"""


def http_get(url, data=None, tries=3, timeout=240):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as ex:  # noqa: BLE001 - network errors of all kinds
            last = ex
            time.sleep(5 * (i + 1))
    raise last


def osm_cache_path(bbox, raw_dir, light=False):
    key = hashlib.sha1(overpass_query(*bbox, light).encode()).hexdigest()[:12]
    return os.path.join(raw_dir, f"osm_{key}.json")


def fetch_osm(bbox, raw_dir, offline=False, endpoints=OVERPASS, tries=2, timeout=240, light=False):
    path = osm_cache_path(bbox, raw_dir, light)
    if os.path.exists(path):
        return path
    if offline:
        raise FileNotFoundError(f"{path} not cached (run without --offline)")
    body = urllib.parse.urlencode({"data": overpass_query(*bbox, light)}).encode()
    last = None
    for url in endpoints:
        try:
            print(f"overpass: {url}", file=sys.stderr)
            data = http_get(url, body, tries=tries, timeout=timeout)
            doc = json.loads(data)  # validate
            # a query that timed out answers 200 with what it had so far and says so here
            if doc.get("remark"):
                raise RuntimeError(str(doc["remark"])[:200])
            os.makedirs(raw_dir, exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
            return path
        except Exception as ex:  # noqa: BLE001
            print(f"  failed: {ex}", file=sys.stderr)
            last = ex
    raise RuntimeError(f"all Overpass endpoints failed: {last}")


def deg2tile(lat, lon, z):
    """Fractional web-mercator tile coordinates."""
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def tile2deg(x, y, z):
    n = 2 ** z
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lat, lon


def dem_tile_range(bbox, z, margin_m=600):
    s, w, n, e = bbox
    dlat = margin_m / 111319.49
    dlon = dlat / math.cos(math.radians(s))
    x0, y0 = deg2tile(n + dlat, w - dlon, z)
    x1, y1 = deg2tile(s - dlat, e + dlon, z)
    # the margin of a block at the 180 meridian: there is no tile past the last column
    return max(0, int(x0)), int(y0), min(2 ** z - 1, int(x1)), int(y1)


def dem_tile_path(raw_dir, z, x, y):
    return os.path.join(raw_dir, "terrarium", str(z), str(x), f"{y}.png")


def fetch_dem(bbox, raw_dir, z, offline=False):
    x0, y0, x1, y1 = dem_tile_range(bbox, z)
    for ty in range(y0, y1 + 1):
        for tx in range(x0, x1 + 1):
            path = dem_tile_path(raw_dir, z, tx, ty)
            if os.path.exists(path):
                continue
            if offline:
                raise FileNotFoundError(f"{path} not cached (run without --offline)")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            url = TERRARIUM.format(z=z, x=tx, y=ty)
            print(f"dem: {url}", file=sys.stderr)
            data = http_get(url)
            with open(path, "wb") as f:
                f.write(data)
            time.sleep(0.2)
    return (x0, y0, x1, y1)


SOURCE_NAMES = {"pgdc": "ArcticDEM", "gmted": "GMTED2010", "srtm": "SRTM", "ned": "NED", "ned13": "NED",
                "ned19": "NED", "etopo": "ETOPO1", "gimp": "GIMP", "austria": "Austria DGM", "kaga": "KAGA",
                "linz": "LINZ", "nrcan": "NRCan CDEM", "eudem": "EU-DEM"}


def dem_sources(bbox, raw_dir, z, offline=False):
    """Providers behind the DEM tiles of a bbox, from the S3 header of every tile: the
    attribution must name them (blockers: SRTM is not used in Khibiny). Cached per tile."""
    x0, y0, x1, y1 = dem_tile_range(bbox, z)
    names = set()
    for ty in range(y0, y1 + 1):
        for tx in range(x0, x1 + 1):
            path = dem_tile_path(raw_dir, z, tx, ty) + ".sources"
            if not os.path.exists(path):
                if offline:
                    raise FileNotFoundError(f"{path} not cached (run without --offline)")
                req = urllib.request.Request(TERRARIUM.format(z=z, x=tx, y=ty), method="HEAD",
                                             headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=60) as r:
                    v = r.headers.get("x-amz-meta-x-imagery-sources") or ""
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="ascii") as f:
                    f.write(v)
            with open(path, encoding="ascii") as f:
                for item in f.read().split(","):
                    key = item.strip().split("/")[0].split("_")[0].lower()
                    if key:
                        names.add(SOURCE_NAMES.get(key, key))
    return sorted(names)


def parse_bbox(s):
    v = [float(t) for t in s.split(",")]
    if len(v) != 4:
        raise argparse.ArgumentTypeError("bbox must be S,W,N,E")
    return tuple(v)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bbox", type=parse_bbox, required=True, help="S,W,N,E in degrees")
    ap.add_argument("--raw", default="data/raw")
    ap.add_argument("--dem-zoom", type=int, default=12)
    ap.add_argument("--sources", action="store_true", help="print the DEM providers and stop")
    a = ap.parse_args(argv)
    if a.sources:
        print(", ".join(dem_sources(a.bbox, a.raw, a.dem_zoom)))
        return
    print(fetch_osm(a.bbox, a.raw))
    print(fetch_dem(a.bbox, a.raw, a.dem_zoom))


if __name__ == "__main__":
    main()
