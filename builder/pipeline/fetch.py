"""Download OSM (Overpass JSON) and DEM (AWS Terrain Tiles, Terrarium PNG) for a bbox.

Everything is cached under data/raw/; re-running is free. urllib honours the usual
http_proxy / https_proxy environment variables.

    python3 -m pipeline.fetch --bbox S,W,N,E [--raw data/raw] [--dem-zoom 12] [--postpass]

OSM comes from Overpass, or with postpass=True from Postpass first (Geofabrik's public PostGIS
of OSM): the same objects in a second or two where the Overpass mirrors take minutes. Its
answer is rewritten into the Overpass `out geom` shape, so the cache and pipeline/osm.py see
one format.
"""
import argparse
import concurrent.futures
import gzip
import hashlib
import json
import math
import os
import sys
import time
import urllib.error
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
            if i + 1 < tries:
                time.sleep(5 * (i + 1))
    raise last


# ---- Postpass, https://postpass.geofabrik.de. A free service with terms of use on that page:
# identify yourself, one request at a time, use the indexes, keep what you got, and no mass use
# (a project that grows is expected to run its own). Three indexed bbox queries per area.

POSTPASS = "https://postpass.geofabrik.de/api/interpreter"
POSTPASS_TIMEOUT_S = 60
# geometry is cut this far outside the bbox: a lake or a river of a thousand kilometres comes
# as the part that matters, and the cut edges stay outside the package
CLIP_MARGIN_M = 1000
# the tags pipeline/osm.py and label.py read: the rest of an object's tags stays on the server
_WAY_KEYS = ("highway", "railway", "aerialway", "waterway", "natural", "landuse", "area", "service", "footway")
_NODE_KEYS = ("natural", "mountain_pass", "tourism", "amenity", "place", "ele", "name", "name:en", "int_name")
_LIGHT_HIGHWAYS = ("service", "footway", "steps", "cycleway", "pedestrian", "corridor", "platform")


def _sql_list(values):
    return "(" + ",".join("'%s'" % v for v in values) + ")"


def _sql_tags(keys):
    return "jsonb_strip_nulls(jsonb_build_object(%s)) AS tags" % ",".join("'%s',tags->'%s'" % (k, k) for k in keys)


def postpass_queries(s, w, n, e, light=False):
    """The SQL for one bbox, the same objects as overpass_query(): {"point"|"line"|"polygon": sql}.
    Only numbers go into the text."""
    env = "ST_MakeEnvelope(%.6f,%.6f,%.6f,%.6f,4326)" % (w, s, e, n)
    dlat = CLIP_MARGIN_M / 111319.49
    dlon = dlat / max(0.05, math.cos(math.radians(max(abs(s), abs(n)))))
    cut = "ST_ClipByBox2D(geom, ST_MakeEnvelope(%.6f,%.6f,%.6f,%.6f,4326)) AS geom" % (
        w - dlon, s - dlat, e + dlon, n + dlat)
    highway = "tags ? 'highway'"
    if light:
        highway = "(tags ? 'highway' AND tags->>'highway' NOT IN %s)" % _sql_list(_LIGHT_HIGHWAYS)
    lines = ("(%s OR tags->>'railway' IN ('rail','narrow_gauge') OR tags ? 'aerialway' OR tags->>'waterway' IN "
             "('river','canal','stream','ditch','drain','riverbank'))" % highway)
    areas = ("(tags->>'natural' IN ('water','wetland','glacier') OR tags->>'landuse'='reservoir' "
             "OR tags->>'waterway'='riverbank')")
    points = ("(tags->>'natural' IN ('peak','volcano','saddle','spring') OR tags->>'mountain_pass'='yes' "
              "OR tags->>'tourism' IN ('alpine_hut','wilderness_hut') OR tags->>'amenity' IN "
              "('shelter','drinking_water') OR tags->>'place' IN ('city','town','village','hamlet'))")
    return {
        "point": "SELECT osm_type, osm_id, %s, geom FROM postpass_point WHERE geom && %s AND %s" % (
            _sql_tags(_NODE_KEYS), env, points),
        # a closed way with a tag that may mean an area sits in the polygon table, the rest here;
        # relations in the line table are routes and boundaries
        "line": "SELECT osm_type, osm_id, %s, %s FROM postpass_line WHERE osm_type='W' AND geom && %s AND %s" % (
            _sql_tags(_WAY_KEYS), cut, env, lines),
        "polygon": "SELECT osm_type, osm_id, %s, %s FROM postpass_polygon WHERE geom && %s AND "
                   "(%s OR (osm_type='W' AND %s))" % (_sql_tags(_WAY_KEYS), cut, env, areas, lines),
    }


def _latlon(ring):
    return [{"lat": p[1], "lon": p[0]} for p in ring]


def _end_key(p):
    # stands in for the node id: ways that share a node share its coordinates to the last digit
    return "%.7f,%.7f" % (p[0], p[1])


def _parts(geom, kind):
    """Coordinate lists of every LineString (kind "line") or Polygon (kind "polygon") in a GeoJSON
    geometry. A clipped geometry may come back as a collection, or as nothing."""
    if not isinstance(geom, dict):
        return []
    t = geom.get("type")
    c = geom.get("coordinates") or []
    if t == "GeometryCollection":
        return [p for g in geom.get("geometries") or [] for p in _parts(g, kind)]
    if kind == "line":
        return [c] if t == "LineString" else list(c) if t == "MultiLineString" else []
    return [c] if t == "Polygon" else list(c) if t == "MultiPolygon" else []


def postpass_doc(answers):
    """Postpass answers {"point"|"line"|"polygon": GeoJSON FeatureCollection} as one document in the
    Overpass `out geom` shape that pipeline/osm.py reads. The snapshot is the oldest of the three:
    the service answers from more than one replica."""
    out = []
    stamps = []
    for kind in ("point", "line", "polygon"):
        fc = answers[kind]
        stamps.append(fc["postpass_properties"]["timestamp"])
        for f in fc["features"]:
            props = f.get("properties") or {}
            tags = props.get("tags") or {}
            oid = props.get("osm_id")
            geom = f.get("geometry")
            if kind == "point":
                if isinstance(geom, dict) and geom.get("type") == "Point":
                    lon, lat = geom["coordinates"][:2]
                    out.append({"type": "node", "id": oid, "lat": lat, "lon": lon, "tags": tags})
            elif kind == "line":
                for line in _parts(geom, "line"):
                    if len(line) >= 2:
                        out.append({"type": "way", "id": oid, "tags": tags, "geometry": _latlon(line),
                                    "nodes": [_end_key(line[0]), _end_key(line[-1])]})
            elif props.get("osm_type") == "R":
                # osm2pgsql took the `type` tag away and joined the member ways into rings
                rings = [poly[0] for poly in _parts(geom, "polygon") if poly and len(poly[0]) >= 4]
                if rings:
                    out.append({"type": "relation", "id": oid, "tags": dict(tags, type="multipolygon"),
                                "members": [{"type": "way", "role": "outer", "geometry": _latlon(r)}
                                            for r in rings]})
            else:
                for poly in _parts(geom, "polygon"):
                    if poly and len(poly[0]) >= 4:
                        ring = poly[0]
                        out.append({"type": "way", "id": oid, "tags": tags, "geometry": _latlon(ring),
                                    "nodes": [_end_key(ring[0]), _end_key(ring[-1])]})
    return {"version": 0.6, "generator": "Postpass", "osm3s": {"timestamp_osm_base": min(stamps)}, "elements": out}


def _postpass_ask(sql, url, timeout, tries=2):
    body = urllib.parse.urlencode({"data": sql}).encode()
    req = urllib.request.Request(url, data=body, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
            break
        except urllib.error.HTTPError as ex:
            # the reason is in the body ("pq: canceling statement due to statement timeout"), and
            # the same question gets the same answer: Overpass is next
            raise RuntimeError("HTTP %d %s" % (ex.code, ex.read(200).decode("utf-8", "replace").strip())) from None
        except OSError:
            # a dropped connection: once more, not a third time on a server that is busy
            if i + 1 == tries:
                raise
            time.sleep(3)
    fc = json.loads(data)
    if not isinstance(fc.get("features"), list) or "timestamp" not in fc.get("postpass_properties", {}):
        raise RuntimeError("not a Postpass answer")
    return fc


def fetch_postpass(bbox, light=False, url=POSTPASS, timeout=POSTPASS_TIMEOUT_S):
    """The OSM objects of a bbox from Postpass, as an Overpass-shaped document. Three requests,
    one after another: the terms of the service ask for that."""
    answers = {}
    for kind, sql in postpass_queries(*bbox, light=light).items():
        answers[kind] = _postpass_ask(sql, url, timeout)
    return postpass_doc(answers)


def _write(path, data):
    """All or nothing: several builds may share one cache directory."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def osm_cache_path(bbox, raw_dir, light=False):
    key = hashlib.sha1(overpass_query(*bbox, light).encode()).hexdigest()[:12]
    return os.path.join(raw_dir, f"osm_{key}.json")


def fetch_osm(bbox, raw_dir, offline=False, endpoints=OVERPASS, tries=2, timeout=240, light=False,
              postpass=False):
    """Path of the cached OSM document of a bbox, downloading it first if needed. postpass: ask
    Postpass before the Overpass endpoints."""
    path = osm_cache_path(bbox, raw_dir, light)
    if os.path.exists(path):
        return path
    if offline:
        raise FileNotFoundError(f"{path} not cached (run without --offline)")
    last = None
    if postpass:
        try:
            print(f"postpass: {POSTPASS}", file=sys.stderr)
            doc = fetch_postpass(bbox, light)
            _write(path, json.dumps(doc, separators=(",", ":")).encode())
            return path
        except Exception as ex:  # noqa: BLE001 - whatever it is, Overpass is the way round it
            print(f"  failed: {ex}", file=sys.stderr)
            last = ex
    body = urllib.parse.urlencode({"data": overpass_query(*bbox, light)}).encode()
    for url in endpoints:
        try:
            print(f"overpass: {url}", file=sys.stderr)
            data = http_get(url, body, tries=tries, timeout=timeout)
            doc = json.loads(data)  # validate
            # a query that timed out answers 200 with what it had so far and says so here
            if doc.get("remark"):
                raise RuntimeError(str(doc["remark"])[:200])
            _write(path, data)
            return path
        except Exception as ex:  # noqa: BLE001
            print(f"  failed: {ex}", file=sys.stderr)
            last = ex
    raise RuntimeError(f"all OSM sources failed: {last}")


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


DEM_WORKERS = 4


def _fetch_dem_tile(raw_dir, z, tx, ty):
    """One tile and, from the same answer, the providers behind it (dem_sources())."""
    url = TERRARIUM.format(z=z, x=tx, y=ty)
    print(f"dem: {url}", file=sys.stderr)
    last = None
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
                sources = r.headers.get("x-amz-meta-x-imagery-sources")
            break
        except Exception as ex:  # noqa: BLE001 - network errors of all kinds
            last = ex
            time.sleep(2 * (i + 1))
    else:
        raise last
    path = dem_tile_path(raw_dir, z, tx, ty)
    if sources is not None:
        _write(path + ".sources", sources.encode("ascii", "replace"))
    _write(path, data)


def fetch_dem(bbox, raw_dir, z, offline=False):
    x0, y0, x1, y1 = dem_tile_range(bbox, z)
    missing = [(tx, ty) for ty in range(y0, y1 + 1) for tx in range(x0, x1 + 1)
               if not os.path.exists(dem_tile_path(raw_dir, z, tx, ty))]
    if missing and offline:
        raise FileNotFoundError(f"{dem_tile_path(raw_dir, z, *missing[0])} not cached (run without --offline)")
    if missing:
        # static files of a public S3 bucket: a few at a time is what a map viewer does
        with concurrent.futures.ThreadPoolExecutor(DEM_WORKERS) as pool:
            for _ in pool.map(lambda t: _fetch_dem_tile(raw_dir, z, *t), missing):
                pass
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
                _write(path, v.encode("ascii", "replace"))
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
    ap.add_argument("--postpass", action="store_true", help="ask Postpass for the OSM data before Overpass")
    a = ap.parse_args(argv)
    if a.sources:
        print(", ".join(dem_sources(a.bbox, a.raw, a.dem_zoom)))
        return
    print(fetch_osm(a.bbox, a.raw, postpass=a.postpass))
    print(fetch_dem(a.bbox, a.raw, a.dem_zoom))


if __name__ == "__main__":
    main()
