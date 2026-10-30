"""Coordinate frame (docs/FORMAT.md section 1) and LOD constants (section 2)."""
import math

M_PER_DEG = 111319.49

# lod -> (tile size in metres, quantum in metres)
# LOD0 is quantized to 8 m (= 1 px at 8 m/px, 2 px at the default 4 m/px; build.py --q0 4
# for the v1 layout), so its in-tile range is 0..128; LOD1 0..256.
LODS = {0: (1024, 8), 1: (4096, 16)}
TILE_UNITS = 256  # v1 in-tile range; in general tile_m / quantum_m
ROUTE_Q = 4  # route units: 4 m (section 6)


class Frame:
    """Local equirectangular frame around the package SW corner."""

    def __init__(self, lat0, lon0):
        self.lat0 = lat0
        self.lon0 = lon0
        self.kx = math.cos(math.radians(lat0)) * M_PER_DEG

    def to_xy(self, lat, lon):
        return ((lon - self.lon0) * self.kx, (lat - self.lat0) * M_PER_DEG)

    def to_latlon(self, x, y):
        return (self.lat0 + y / M_PER_DEG, self.lon0 + x / self.kx)


def bbox_from_origin(lat0, lon0, width_m, height_m):
    """(south, west, north, east) for a package of the given size."""
    n, e = Frame(lat0, lon0).to_latlon(width_m, height_m)
    return (lat0, lon0, n, e)


def size_from_bbox(s, w, n, e):
    x, y = Frame(s, w).to_xy(n, e)
    return (int(round(x)), int(round(y)))
