"""DEM (Terrarium tiles) -> contour lines via our own marching squares."""
import math

import numpy as np
from PIL import Image

from .fetch import dem_tile_path, tile2deg


def load_dem(raw_dir, z, tile_range):
    """Mosaic Terrarium tiles. Returns (elev float32 [rows, cols], px0, py0) where (px0, py0)
    is the global pixel coordinate of the mosaic's top-left pixel corner at zoom z."""
    x0, y0, x1, y1 = tile_range
    rows = []
    for ty in range(y0, y1 + 1):
        row = []
        for tx in range(x0, x1 + 1):
            im = np.asarray(Image.open(dem_tile_path(raw_dir, z, tx, ty)).convert("RGB"), dtype=np.float64)
            row.append(im[:, :, 0] * 256.0 + im[:, :, 1] + im[:, :, 2] / 256.0 - 32768.0)
        rows.append(np.hstack(row))
    return np.vstack(rows).astype(np.float32), x0 * 256, y0 * 256


def gaussian_blur(a, sigma):
    if sigma <= 0:
        return a
    r = int(math.ceil(sigma * 3))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    p = np.pad(a, r, mode="edge")
    tmp = np.zeros_like(p)
    for i, w in enumerate(k):
        tmp[:, r:-r] += w * p[:, i:i + p.shape[1] - 2 * r]
    out = np.zeros_like(a)
    for i, w in enumerate(k):
        out += w * tmp[i:i + a.shape[0], r:-r]
    return out


class DemGrid:
    """DEM resampled to a regular grid in package metres (x east, y north)."""

    def __init__(self, raw_dir, z, tile_range, frame, x0, y0, x1, y1, step, sigma_m=15.0):
        elev, px0, py0 = load_dem(raw_dir, z, tile_range)
        n = 256 * 2 ** z
        self.step = step
        self.xs = np.arange(x0, x1 + step / 2, step)
        self.ys = np.arange(y0, y1 + step / 2, step)
        # package metres -> lat/lon -> global mercator pixel (bilinear sample)
        lat = frame.lat0 + self.ys / 111319.49
        lon = frame.lon0 + self.xs / frame.kx
        gx = (lon + 180.0) / 360.0 * n - px0 - 0.5
        gy = (1.0 - np.arcsinh(np.tan(np.radians(lat))) / math.pi) / 2.0 * n - py0 - 0.5
        GX, GY = np.meshgrid(gx, gy)
        ix = np.clip(np.floor(GX).astype(int), 0, elev.shape[1] - 2)
        iy = np.clip(np.floor(GY).astype(int), 0, elev.shape[0] - 2)
        fx = GX - ix
        fy = GY - iy
        e = (elev[iy, ix] * (1 - fx) * (1 - fy) + elev[iy, ix + 1] * fx * (1 - fy)
             + elev[iy + 1, ix] * (1 - fx) * fy + elev[iy + 1, ix + 1] * fx * fy)
        self.z = gaussian_blur(e.astype(np.float64), sigma_m / step)  # rows = y (north-up)


def marching_squares(z, level):
    """Contour polylines of grid z (rows=y, cols=x) at `level`, in fractional grid coords
    (col, row). Segments are stitched via shared cell-edge ids, so joins are exact."""
    h, w = z.shape
    a = np.where(z == level, level + 1e-7, z)
    above = a > level
    c = (above[:-1, :-1].astype(np.uint8) | (above[:-1, 1:] << 1) | (above[1:, 1:] << 2) | (above[1:, :-1] << 3))
    rr, cc = np.nonzero((c != 0) & (c != 15))

    # edge ids: horizontal edge between (r,c)-(r,c+1): 2*(r*w+c); vertical (r,c)-(r+1,c): 2*(r*w+c)+1
    def pos(eid):
        k, vert = divmod(eid, 2)
        r, col = divmod(k, w)
        if vert:
            z0, z1 = a[r, col], a[r + 1, col]
            t = (level - z0) / (z1 - z0)
            return (col, r + t)
        z0, z1 = a[r, col], a[r, col + 1]
        t = (level - z0) / (z1 - z0)
        return (col + t, r)

    # cell corners: 0=(r,c) 1=(r,c+1) 2=(r+1,c+1) 3=(r+1,c); edges: T=top(r), R=right(c+1), B=bottom(r+1), L=left(c)
    table = {
        1: [("L", "T")], 2: [("T", "R")], 3: [("L", "R")], 4: [("R", "B")],
        6: [("T", "B")], 7: [("L", "B")], 8: [("B", "L")], 9: [("B", "T")],
        11: [("B", "R")], 12: [("R", "L")], 13: [("R", "T")], 14: [("T", "L")],
    }
    adj = {}
    for r, col in zip(rr.tolist(), cc.tolist()):
        code = int(c[r, col])
        E = {"T": 2 * (r * w + col), "B": 2 * ((r + 1) * w + col),
             "L": 2 * (r * w + col) + 1, "R": 2 * (r * w + col + 1) + 1}
        if code in (5, 10):
            center = (a[r, col] + a[r, col + 1] + a[r + 1, col] + a[r + 1, col + 1]) / 4.0
            # center above -> the two "above" corners connect through the cell
            if code == 5:
                segs = [("T", "R"), ("B", "L")] if center > level else [("L", "T"), ("R", "B")]
            else:
                segs = [("T", "L"), ("B", "R")] if center > level else [("T", "R"), ("B", "L")]
        else:
            segs = table[code]
        for e1, e2 in segs:
            u, v = E[e1], E[e2]
            adj.setdefault(u, []).append(v)
            adj.setdefault(v, []).append(u)

    # walk chains: start at degree-1 nodes (open lines at grid border), then remaining loops
    lines = []
    visited = set()

    def walk(start):
        path = [start]
        visited.add(start)
        prev, cur = None, start
        while True:
            nxt = None
            for v in adj[cur]:
                if v != prev and v not in visited:
                    nxt = v
                    break
            if nxt is None:
                # closing a loop?
                if len(path) > 2 and start in adj[cur] and prev != start:
                    path.append(start)
                break
            visited.add(nxt)
            path.append(nxt)
            prev, cur = cur, nxt
        return path

    for node, nb in adj.items():
        if len(nb) == 1 and node not in visited:
            lines.append(walk(node))
    for node in adj:
        if node not in visited:
            lines.append(walk(node))
    return [[pos(e) for e in ln] for ln in lines if len(ln) >= 2]


def contour_lines(grid, levels):
    """Yield (ele, pts in package metres, closed) for each level."""
    out = []
    x0, y0, s = grid.xs[0], grid.ys[0], grid.step
    zmin, zmax = float(grid.z.min()), float(grid.z.max())
    for lev in levels:
        if lev <= zmin or lev >= zmax:
            continue
        for ln in marching_squares(grid.z, lev):
            pts = [(x0 + cx * s, y0 + ry * s) for cx, ry in ln]
            closed = len(pts) > 3 and pts[0] == pts[-1]
            if closed:
                pts = pts[:-1]
            out.append((lev, pts, closed))
    return out
