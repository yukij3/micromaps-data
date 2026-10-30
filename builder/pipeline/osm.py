"""OSM (Overpass `out geom` JSON) -> classified features in package metres (FORMAT.md section 4)."""
import json

from .label import pick_name

LINE, AREA, POINT = "LINE", "AREA", "POINT"


def kind_of(cls):
    """Kind is implied by the class number (section 4 / section 5)."""
    if cls < 16:
        return LINE
    if cls < 24:
        return AREA
    return POINT


PLACE_RANK = {"city": 3, "town": 2, "village": 1}


class Feat:
    """A feature in package metres (float), before tiling."""

    __slots__ = ("cls", "pts", "ele", "name", "ends", "minor", "rank")

    def __init__(self, cls, pts, ele=None, name=None, ends=None, minor=False, rank=0):
        self.cls = cls
        self.pts = pts
        self.ele = ele
        self.name = name
        self.ends = ends  # (first node id, last node id) for lines, used for chaining
        self.minor = minor  # dropped at LOD1 (parking aisles, driveways, sidings, sidewalks)
        self.rank = rank  # places: the bigger the better, see build.cap_points()

    @property
    def kind(self):
        return kind_of(self.cls)

    def __repr__(self):
        return f"Feat({self.cls}, n={len(self.pts)}, ele={self.ele}, name={self.name!r})"


HIGHWAY = {
    "motorway": 1, "motorway_link": 1, "trunk": 1, "trunk_link": 1, "primary": 1, "primary_link": 1,
    "secondary": 2, "secondary_link": 2, "tertiary": 2, "tertiary_link": 2,
    "unclassified": 3, "residential": 3, "living_street": 3, "road": 3,
    "track": 4, "service": 4,
    "path": 5, "footway": 5, "bridleway": 5, "steps": 5, "cycleway": 5, "pedestrian": 5,
}
AERIALWAYS = {"cable_car", "gondola", "mixed_lift", "chair_lift", "drag_lift", "t-bar", "j-bar",
              "platter", "rope_tow", "magic_carpet", "zip_line"}


def classify_way(t):
    """Return (cls, is_area) or None."""
    if t.get("area") == "yes" and "highway" in t:
        return None
    nat = t.get("natural")
    if nat == "water" or t.get("landuse") == "reservoir" or t.get("waterway") == "riverbank":
        return 16, True
    if nat == "wetland":
        return 17, True
    if nat == "glacier":
        return 18, True
    hw = t.get("highway")
    if hw in HIGHWAY:
        return HIGHWAY[hw], False
    if t.get("railway") in ("rail", "narrow_gauge"):
        return 6, False
    if t.get("aerialway") in AERIALWAYS:
        return 6, False
    ww = t.get("waterway")
    if ww in ("river", "canal"):
        return 7, False
    if ww in ("stream", "ditch", "drain"):
        return 8, False
    return None


MINOR_SERVICE = {"parking_aisle", "driveway", "drive-through", "alley", "siding", "yard", "spur",
                 "crossover", "sidewalk", "crossing"}


def is_minor(t):
    return (t.get("service") in MINOR_SERVICE or t.get("footway") in MINOR_SERVICE
            or "aerialway" in t and t.get("aerialway") in ("magic_carpet", "rope_tow"))


def classify_node(t):
    nat = t.get("natural")
    if nat in ("peak", "volcano"):
        return 24
    if nat == "saddle" or t.get("mountain_pass") == "yes":
        return 28
    if t.get("tourism") in ("alpine_hut", "wilderness_hut") or t.get("amenity") == "shelter":
        return 25
    if nat == "spring" or t.get("amenity") == "drinking_water":
        return 26
    if t.get("place") in ("city", "town", "village", "hamlet"):
        return 27
    return None


def parse_ele(s):
    if s is None:
        return None
    s = s.strip().replace(",", ".").split(" ")[0].rstrip("m")
    try:
        return int(round(float(s)))
    except ValueError:
        return None


def _join_rings(ways):
    """Join open way geometries (lists of (lat,lon)) into closed rings by shared endpoints."""
    rings = []
    pending = [list(w) for w in ways if len(w) >= 2]
    while pending:
        cur = pending.pop()
        changed = True
        while cur[0] != cur[-1] and changed:
            changed = False
            for i, w in enumerate(pending):
                if w[0] == cur[-1]:
                    cur = cur + w[1:]
                elif w[-1] == cur[-1]:
                    cur = cur + w[::-1][1:]
                elif w[-1] == cur[0]:
                    cur = w + cur[1:]
                elif w[0] == cur[0]:
                    cur = w[::-1] + cur[1:]
                else:
                    continue
                pending.pop(i)
                changed = True
                break
        if cur[0] == cur[-1] and len(cur) >= 4:
            rings.append(cur[:-1])
    return rings


def dropped(t, drop):
    """drop: {tag: values} of ways to leave out (blocks.THIN), "minor" for the is_minor() ways."""
    return (drop.get("minor") and is_minor(t)) or any(t.get(k) in v for k, v in drop.items() if k != "minor")


def load_osm(path, frame, drop=None):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    feats = []
    for el in data["elements"]:
        t = el.get("tags", {})
        typ = el["type"]
        if typ == "node":
            cls = classify_node(t)
            if cls is None:
                continue
            ele = parse_ele(t.get("ele")) if cls in (24, 28) else None
            name = pick_name(t) if cls in (24, 25, 27, 28) else None
            if cls == 27 and not name:
                continue
            feats.append(Feat(cls, [frame.to_xy(el["lat"], el["lon"])], ele, name, rank=PLACE_RANK.get(t.get("place"), 0)))
        elif typ == "way":
            c = classify_way(t)
            if c is None or "geometry" not in el or (drop and dropped(t, drop)):
                continue
            cls, is_area = c
            geom = [(g["lat"], g["lon"]) for g in el["geometry"] if g]
            if is_area:
                nodes = el.get("nodes", [])
                if len(geom) < 4 or geom[0] != geom[-1]:
                    continue
                pts = [frame.to_xy(la, lo) for la, lo in geom[:-1]]
                feats.append(Feat(cls, pts))
            else:
                if len(geom) < 2:
                    continue
                nodes = el.get("nodes") or [None, None]
                pts = [frame.to_xy(la, lo) for la, lo in geom]
                feats.append(Feat(cls, pts, ends=(nodes[0], nodes[-1]), minor=is_minor(t)))
        elif typ == "relation":
            c = classify_way(t)
            if c is None or not c[1]:
                continue
            outers = []
            for m in el.get("members", []):
                if m.get("type") == "way" and m.get("role", "outer") in ("outer", "") and m.get("geometry"):
                    outers.append([(g["lat"], g["lon"]) for g in m["geometry"] if g])
            for ring in _join_rings(outers):
                feats.append(Feat(c[0], [frame.to_xy(la, lo) for la, lo in ring]))
    return feats


def chain_osm_lines(feats):
    """Merge LINE features of the same class that share an end node (degree-2 joins first)."""
    lines = [f for f in feats if f.kind == LINE and f.ends and f.ends[0] is not None]
    others = [f for f in feats if not (f.kind == LINE and f.ends and f.ends[0] is not None)]
    out = []
    for key in sorted({(f.cls, f.minor) for f in lines}):
        group = [f for f in lines if (f.cls, f.minor) == key]
        out.extend(_chain_by_key(group, lambda f: f.ends[0], lambda f: f.ends[1]))
    return others + out


def _chain_by_key(group, head, tail):
    """Greedy end-to-end merge. Each item: Feat with .pts; keys from head()/tail()."""
    items = [[list(f.pts), head(f), tail(f), f] for f in group]
    idx = {}
    for i, it in enumerate(items):
        idx.setdefault(it[1], []).append(i)
        idx.setdefault(it[2], []).append(i)
    used = [False] * len(items)

    def take(key, exclude):
        for j in idx.get(key, ()):
            if not used[j] and j != exclude and items[j][1] != items[j][2]:
                return j
        return None

    out = []
    for i, it in enumerate(items):
        if used[i]:
            continue
        used[i] = True
        pts, h, t, f = it[0], it[1], it[2], it[3]
        if h == t:  # closed loop
            out.append(Feat(f.cls, pts, f.ele, f.name, (h, t), f.minor))
            continue
        while True:
            j = take(t, i)
            if j is None:
                break
            used[j] = True
            p, jh, jt = items[j][0], items[j][1], items[j][2]
            if jh == t:
                pts = pts + p[1:]
                t = jt
            else:
                pts = pts + p[::-1][1:]
                t = jh
            if t == h:
                break
        while h != t:
            j = take(h, i)
            if j is None:
                break
            used[j] = True
            p, jh, jt = items[j][0], items[j][1], items[j][2]
            if jt == h:
                pts = p + pts[1:]
                h = jh
            else:
                pts = p[::-1] + pts[1:]
                h = jt
        out.append(Feat(f.cls, pts, f.ele, f.name, (h, t), f.minor))
    return out
