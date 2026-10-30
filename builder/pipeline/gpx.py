"""GPX in/out, course loading (GPX or FIT, see fit.py), route blob (FORMAT.md section 6) and a demo-route generator.

Demo route: Dijkstra over the real OSM way graph (roads/tracks/paths), preferring trails,
through a list of via points. Used to produce pipeline/samples/demo_route.gpx.

    python3 -m pipeline.gpx demo --osm data/raw/osm_X.json --out pipeline/samples/demo_route.gpx
"""
import argparse
import heapq
import json
import math
import os
import xml.etree.ElementTree as ET

from .encode import encode_route
from .geo import ROUTE_Q, Frame
from .geom import simplify
from .label import ascii_label

NS = "{http://www.topografix.com/GPX/1/1}"


def _strip(tag):
    return tag.split("}", 1)[-1]


def parse_gpx(path):
    """Returns (name, [(lat, lon)], [(lat, lon, name)]). Uses trk, else rte points."""
    root = ET.parse(path).getroot()
    name = None
    trk, rte, wpts = [], [], []
    for el in root.iter():
        t = _strip(el.tag)
        if t in ("trkpt", "rtept", "wpt"):
            pt = (float(el.get("lat")), float(el.get("lon")))
            if t == "trkpt":
                trk.append(pt)
            elif t == "rtept":
                rte.append(pt)
            else:
                nm = None
                for c in el:
                    if _strip(c.tag) == "name":
                        nm = (c.text or "").strip()
                wpts.append((pt[0], pt[1], nm))
        elif t in ("trk", "rte", "metadata") and name is None:
            for c in el:
                if _strip(c.tag) == "name" and c.text:
                    name = c.text.strip()
    return name, (trk or rte), wpts


def course_format(path):
    """'fit' or 'gpx': by content (FIT header signature), else by extension."""
    from .fit import is_fit
    with open(path, "rb") as f:
        head = f.read(14)
    if is_fit(head) or os.path.splitext(path)[1].lower() == ".fit":
        return "fit"  # a .fit without a valid header fails in parse_fit with a clear message
    return "gpx"


def parse_course(path):
    """GPX or FIT course (auto-detected) -> (format, name, [(lat, lon)], [(lat, lon, name)])."""
    fmt = course_format(path)
    if fmt == "fit":
        from .fit import parse_fit
        return (fmt,) + parse_fit(path)
    return (fmt,) + parse_gpx(path)


def route_blob(frame, name, latlons, wpts, eps_m=2.0):
    """Project, DP-simplify (eps_m), quantize to 4 m units -> route.bin bytes.
    Course and waypoint names come from the user's file, so they get the same English-only
    treatment as map labels."""
    xy = [frame.to_xy(la, lo) for la, lo in latlons]
    xy = simplify(xy, eps_m)
    pts = []
    for x, y in xy:
        p = (int(math.floor(x / ROUTE_Q + 0.5)), int(math.floor(y / ROUTE_Q + 0.5)))
        if not pts or pts[-1] != p:
            pts.append(p)
    wq = []
    for la, lo, nm in wpts:
        x, y = frame.to_xy(la, lo)
        wq.append((int(math.floor(x / ROUTE_Q + 0.5)), int(math.floor(y / ROUTE_Q + 0.5)), ascii_label(nm) or None))
    return encode_route(ascii_label(name), pts, wq), pts, wq


def write_gpx(path, name, latlons, wpts):
    root = ET.Element("gpx", {"version": "1.1", "creator": "microMAPS pipeline",
                              "xmlns": "http://www.topografix.com/GPX/1/1"})
    md = ET.SubElement(root, "metadata")
    ET.SubElement(md, "name").text = name
    for la, lo, nm in wpts:
        w = ET.SubElement(root, "wpt", {"lat": "%.7f" % la, "lon": "%.7f" % lo})
        ET.SubElement(w, "name").text = nm
    trk = ET.SubElement(root, "trk")
    ET.SubElement(trk, "name").text = name
    seg = ET.SubElement(trk, "trkseg")
    for la, lo in latlons:
        ET.SubElement(seg, "trkpt", {"lat": "%.7f" % la, "lon": "%.7f" % lo})
    ET.indent(root)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


# cost multipliers per highway type (lower = preferred for a hike)
HIKE_COST = {"path": 1.0, "footway": 1.1, "bridleway": 1.0, "track": 1.2, "steps": 1.5,
             "unclassified": 1.6, "residential": 1.6, "living_street": 1.5, "pedestrian": 1.2,
             "service": 2.0, "tertiary": 2.5, "secondary": 3.0, "cycleway": 1.3}


def build_graph(osm_path, frame):
    with open(osm_path, encoding="utf-8") as f:
        data = json.load(f)
    pos = {}
    adj = {}
    for el in data["elements"]:
        if el["type"] != "way":
            continue
        hw = el.get("tags", {}).get("highway")
        if hw not in HIKE_COST or "geometry" not in el:
            continue
        ids = el["nodes"]
        geo = el["geometry"]
        k = HIKE_COST[hw]
        for i, nid in enumerate(ids):
            if geo[i] is None:
                continue
            pos[nid] = (geo[i]["lat"], geo[i]["lon"])
        for i in range(len(ids) - 1):
            a, b = ids[i], ids[i + 1]
            if a not in pos or b not in pos:
                continue
            ax, ay = frame.to_xy(*pos[a])
            bx, by = frame.to_xy(*pos[b])
            c = math.hypot(bx - ax, by - ay) * k
            adj.setdefault(a, []).append((b, c))
            adj.setdefault(b, []).append((a, c))
    return pos, adj


def nearest(pos, adj, frame, lat, lon):
    tx, ty = frame.to_xy(lat, lon)
    best, bd = None, 1e18
    for nid in adj:
        x, y = frame.to_xy(*pos[nid])
        d = (x - tx) ** 2 + (y - ty) ** 2
        if d < bd:
            best, bd = nid, d
    return best, math.sqrt(bd)


def dijkstra(adj, s, t):
    dist = {s: 0.0}
    prev = {}
    pq = [(0.0, s)]
    while pq:
        d, u = heapq.heappop(pq)
        if u == t:
            break
        if d > dist.get(u, 1e18):
            continue
        for v, c in adj.get(u, ()):
            nd = d + c
            if nd < dist.get(v, 1e18):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, v))
    if t not in dist:
        return None
    path = [t]
    while path[-1] != s:
        path.append(prev[path[-1]])
    return path[::-1]


def demo_route(osm_path, frame, vias):
    """vias: [(lat, lon, name)]. Returns (latlons, waypoints)."""
    pos, adj = build_graph(osm_path, frame)
    nodes = []
    for la, lo, nm in vias:
        nid, d = nearest(pos, adj, frame, la, lo)
        nodes.append((nid, nm, d))
    out = []
    for i in range(len(nodes) - 1):
        p = dijkstra(adj, nodes[i][0], nodes[i + 1][0])
        if p is None:
            raise RuntimeError(f"no path between via {i} and {i + 1}")
        out.extend(p if not out else p[1:])
    latlons = [pos[n] for n in out]
    wpts = [(pos[n][0], pos[n][1], nm) for n, nm, _ in nodes]
    return latlons, wpts


# Kirovsk centre -> trail west of Lake Bolshoy Vudyavr -> Geographers' pass (saddle, 880 m)
DEMO_VIAS = [
    (67.6151, 33.6640, "Kirovsk"),
    (67.6349, 33.6154, "Trail"),
    (67.6499579, 33.5863063, "Pass Geografov"),
]


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo")
    d.add_argument("--osm", required=True)
    d.add_argument("--out", required=True)
    d.add_argument("--origin", default="67.59,33.58")
    a = ap.parse_args(argv)
    lat0, lon0 = (float(v) for v in a.origin.split(","))
    frame = Frame(lat0, lon0)
    latlons, wpts = demo_route(a.osm, frame, DEMO_VIAS)
    xy = [frame.to_xy(*p) for p in latlons]
    length = sum(math.hypot(xy[i + 1][0] - xy[i][0], xy[i + 1][1] - xy[i][1]) for i in range(len(xy) - 1))
    write_gpx(a.out, "Kirovsk - Geografov pass", latlons, wpts)
    print(f"{a.out}: {len(latlons)} pts, {length / 1000:.1f} km")


if __name__ == "__main__":
    main()
