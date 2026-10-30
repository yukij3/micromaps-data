"""The grid of map blocks (docs/BLOCKS.md). web/blocks.js holds the same numbers, the tests
compare both with pipeline/tests/data/block_grid.json.

Ids are read from a public queue, so parse() takes hostile text: only the one spelling of a
block on the grid passes, nothing else reaches a path, a query or a commit message.
"""
import math
import re

from .geo import M_PER_DEG

PITCH_M = 5000
SIZES = (10, 20)
MAX_LAT = 80
DLAT = PITCH_M / M_PER_DEG
# not \d or $: those also take other scripts' digits and a trailing newline
ID_RE = re.compile(r"(10|20)-[0-9]{1,5}-[0-9]{1,5}")
MAX_BYTES = 120 * 1024

# (places, other points) kept per tile of a LOD. MapRenderer.drawPoints() walks every point in view
# in one step: 73 places and 170 points in view tripped the watchdog of v2.0.0 at 32 m/px over
# Chamonix, 29 points are known to pass. A wide view labels 3 or 4 of them anyway.
POINT_CAP = ((2, 6), (1, 2))

# The rungs a block is tried on until it fits, build.py --thin. Rung 0 is the block profile for
# a normal area; existing packs do not use any of this. budget: vector bytes per tile at LOD0 and
# LOD1 of a 10 km block, what is left of the 4 KB read target after the DEM grid of an LOD0 tile;
# past rung 0 a 20 km block has a quarter of it per tile, the 120 KB are shared by four times the
# area. eps: factor on build.CONFIG eps_m. drop: ways left out before chaining, what a city has
# plenty of goes first and trails last. A tile still over budget after build.thin() loses
# TILE_DROP one class per step.
TILE_DROP = (5, 8, 4, 17, 3)  # paths, streams, tracks and service, wetlands, minor roads
_URBAN = {"service", "footway", "steps", "cycleway", "pedestrian"}
_TRAILS = _URBAN | {"path", "track", "bridleway"}
_STREETS = _TRAILS | {"residential", "unclassified", "living_street", "road"}
THIN = (
    dict(budget=(3584, 4096), eps=1.0),
    dict(budget=(2048, 3072), eps=1.5, drop={"minor": True}),
    dict(budget=(1536, 2048), eps=2.0, drop={"minor": True, "highway": _URBAN}),
    # Chamonix at 20 km ends here: 126 KB one rung up, and without trails it is no hiking map
    dict(budget=(1280, 2048), eps=2.5, drop={"minor": True, "highway": _URBAN, "waterway": {"ditch", "drain"}}),
    dict(budget=(1024, 2048), eps=2.5, drop={"minor": True, "highway": _TRAILS,
                                             "waterway": {"ditch", "drain", "stream"}}),
    dict(budget=(768, 1536), eps=3.0, drop={"minor": True, "highway": _STREETS,
                                            "waterway": {"ditch", "drain", "stream"}}),
)


def lat0(r):
    return round(r * DLAT - 90, 6)


def dlon(r):
    return PITCH_M / (M_PER_DEG * math.cos(math.radians(lat0(r))))


def lon0(r, c):
    return round(c * dlon(r) - 180, 6)


def block_id(size, r, c):
    return "%d-%d-%d" % (size, r, c)


def snap(lat, lon, size):
    """Id of the block with this point in its south-west pitch cell."""
    r = math.floor((lat + 90) / DLAT)
    return block_id(size, r, math.floor((lon + 180) / dlon(r)))


def centred(lat, lon, size):
    """Id of the block closest to having this point in its centre."""
    half = size * 500.0
    r = math.floor((lat - half / M_PER_DEG + 90) / DLAT)
    return snap(lat - half / M_PER_DEG, lon - half / (M_PER_DEG * math.cos(math.radians(lat0(r)))), size)


def fits(size, r, c):
    """Grid limits: |lat0| at most MAX_LAT, the block starts east of -180 and ends west of 180."""
    if size not in SIZES or r < 0 or c < 0:
        return False
    if abs(lat0(r)) > MAX_LAT:
        return False
    return lon0(r, c) + dlon(r) * size * 1000 / PITCH_M <= 180


def parse(text):
    """(size, r, c) of a block id, ValueError for anything else."""
    if not isinstance(text, str) or not ID_RE.fullmatch(text):
        raise ValueError("not a block id")
    size, r, c = (int(v) for v in text.split("-"))
    if block_id(size, r, c) != text:  # leading zeros: a second name for the same block
        raise ValueError("not a block id")
    if not fits(size, r, c):
        raise ValueError("block is off the grid")
    return size, r, c


def origin(text):
    """(lat0, lon0, size in metres) of a block id."""
    size, r, c = parse(text)
    return lat0(r), lon0(r, c), size * 1000
