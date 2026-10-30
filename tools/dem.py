"""DEM layer of MMAP v2 (docs/FORMAT.md section 8): relief as a small elevation grid per
LOD0 tile, contours are traced on the watch with integer marching squares.

Everything below the encoder is integer-only and written to port line by line to Monkey C
(app/source/MmapDecode.mc DemGrid, app/source/MapRenderer.mc demLayer):

  decode()      Rice stream -> K (node values, quanta)
  node_values() K -> elevation in 1/256 m, optionally cubic-upsampled x2 (`upsample2`)
  segments()    marching squares of one level over a value grid, segments in 1/16 m

Grid: n cells per 1024 m tile, (n+1)^2 nodes, node (i, j) at (tx*1024 + i*1024/n,
ty*1024 + j*1024/n) package metres, row 0 = south edge. Neighbouring tiles share their edge
nodes, so contours join exactly across tile borders.
"""
import math

import numpy as np

QMAX = 20      # unary length that triggers the escape (QMAX zeros + 16-bit raw value)
RESET = 32     # halve the Rice statistics when N reaches this
TILE_M = 1024


# ---------------------------------------------------------------------------
# bit I/O (MSB first, same convention as the vector deltas, FORMAT.md 5.4)

class BitWriter:
    def __init__(self):
        self.buf = bytearray()
        self.acc = 0
        self.nb = 0

    def put(self, v, w):
        for i in range(w - 1, -1, -1):
            self.acc = (self.acc << 1) | ((v >> i) & 1)
            self.nb += 1
            if self.nb == 8:
                self.buf.append(self.acc)
                self.acc = 0
                self.nb = 0

    def flush(self):
        if self.nb:
            self.buf.append(self.acc << (8 - self.nb))
            self.acc = 0
            self.nb = 0
        return bytes(self.buf)


class BitReader:
    def __init__(self, d, p=0):
        self.d = d
        self.p = p
        self.acc = 0
        self.nb = 0

    def bit(self):
        if self.nb == 0:
            self.acc = self.d[self.p]
            self.p += 1
            self.nb = 8
        self.nb -= 1
        return (self.acc >> self.nb) & 1

    def bits(self, w):
        v = 0
        for _ in range(w):
            v = (v << 1) | self.bit()
        return v


# ---------------------------------------------------------------------------
# codec

def predict(K, i, j, W):
    """Planar predictor; 2-point linear extrapolation on the first row / column."""
    if j == 0:
        a = K[i - 1]
        return a + (a - K[i - 2] if i >= 2 else 0)
    if i == 0:
        b = K[(j - 1) * W]
        return b + (b - K[(j - 2) * W] if j >= 2 else 0)
    return K[j * W + i - 1] + K[(j - 1) * W + i] - K[(j - 1) * W + i - 1]


def encode(K, n):
    """K: (n+1)^2 ints (row-major, row 0 south), each in i16 range. Returns the DEM blob."""
    W = n + 1
    if len(K) != W * W:
        raise ValueError("expected %d nodes" % (W * W))
    if not all(-32768 <= k < 32768 for k in K):
        raise ValueError("node value out of i16 range")
    out = bytearray((K[0] & 0xFFFF).to_bytes(2, "little"))
    bw = BitWriter()
    A, N = 4, 1
    for idx in range(1, W * W):
        j, i = divmod(idx, W)
        e = K[idx] - predict(K, i, j, W)
        m = 2 * e if e >= 0 else -2 * e - 1
        k = 0
        while (N << k) < A:
            k += 1
        q = m >> k
        if q < QMAX:
            bw.put(0, q)
            bw.put(1, 1)
            bw.put(m & ((1 << k) - 1), k)
        else:
            if m >= 1 << 16:
                raise ValueError("residual too large for the escape")
            bw.put(0, QMAX)
            bw.put(m, 16)
        A += abs(e)
        N += 1
        if N == RESET:
            A >>= 1
            N >>= 1
    return bytes(out) + bw.flush()


def decode(d, n, p=0):
    """DEM blob (at offset p) -> K list. Mirrors MmapDecode.mc DemGrid.initialize."""
    W = n + 1
    K = [0] * (W * W)
    k0 = d[p] | (d[p + 1] << 8)
    K[0] = k0 - 0x10000 if k0 >= 0x8000 else k0
    br = BitReader(d, p + 2)
    A, N = 4, 1
    for idx in range(1, W * W):
        j, i = divmod(idx, W)
        k = 0
        while (N << k) < A:
            k += 1
        q = 0
        while q < QMAX and br.bit() == 0:
            q += 1
        m = br.bits(16) if q == QMAX else (q << k) | br.bits(k)
        e = (m >> 1) ^ -(m & 1)
        K[idx] = predict(K, i, j, W) + e
        A += e if e >= 0 else -e
        N += 1
        if N == RESET:
            A >>= 1
            N >>= 1
    return K


# ---------------------------------------------------------------------------
# sampling (PC side)

def node_coords(n):
    """Node offsets inside a tile, metres (float for n not dividing 1024)."""
    return np.arange(n + 1) * (TILE_M / n)


def bilinear(grid, xm, ym):
    """Sample a pipeline.contours.DemGrid at package metres (arrays)."""
    c = (xm - grid.xs[0]) / grid.step
    r = (ym - grid.ys[0]) / grid.step
    c0 = np.clip(np.floor(c).astype(int), 0, grid.z.shape[1] - 2)
    r0 = np.clip(np.floor(r).astype(int), 0, grid.z.shape[0] - 2)
    fc = c - c0
    fr = r - r0
    z = grid.z
    return (z[r0, c0] * (1 - fc) * (1 - fr) + z[r0, c0 + 1] * fc * (1 - fr)
            + z[r0 + 1, c0] * (1 - fc) * fr + z[r0 + 1, c0 + 1] * fc * fr)


def sample_tile(grid, tx, ty, n, q):
    """K = floor(z / q) at the (n+1)^2 nodes of LOD0 tile (tx, ty)."""
    g = node_coords(n)
    X, Y = np.meshgrid(tx * TILE_M + g, ty * TILE_M + g)
    return [int(v) for v in np.floor(bilinear(grid, X, Y) / q).ravel()]


# ---------------------------------------------------------------------------
# watch path (integer): node values and marching squares

def raw_values(K, q):
    """Bin centre in 1/256 m: (K*q + q/2) * 256."""
    return [(k * q * 2 + q) * 128 for k in K]


def upsample2(K, n, q):
    """(n+1)^2 K -> (2n+1)^2 values in 1/256 m. 4-tap cubic (-1, 9, 9, -1)/16 per axis,
    linear in the first/last interval of a row/column, so edge values depend only on edge
    nodes (tiles stay seamless). Horizontal pass first (x16), then vertical (>>4)."""
    W = n + 1
    M = 2 * n + 1
    V = [(k * q * 2 + q) * 8 for k in K]           # metres * 16
    rows = []
    for j in range(W):
        r = V[j * W:(j + 1) * W]
        out = [0] * M
        for i in range(W):
            out[2 * i] = r[i] * 16
        for i in range(n):
            if 1 <= i <= n - 2:
                out[2 * i + 1] = -r[i - 1] + 9 * r[i] + 9 * r[i + 1] - r[i + 2]
            else:
                out[2 * i + 1] = 8 * (r[i] + r[i + 1])
        rows.append(out)                           # 1/256 m
    U = [None] * M
    for j in range(W):
        U[2 * j] = rows[j]
    for j in range(n):
        if 1 <= j <= n - 2:
            a, b, c, d = rows[j - 1], rows[j], rows[j + 1], rows[j + 2]
            U[2 * j + 1] = [(-a[i] + 9 * b[i] + 9 * c[i] - d[i]) >> 4 for i in range(M)]
        else:
            b, c = rows[j], rows[j + 1]
            U[2 * j + 1] = [(b[i] + c[i]) >> 1 for i in range(M)]
    return [v for row in U for v in row]


def node_values(K, n, q, cubic):
    """Flat value grid (1/256 m) and its width M."""
    if cubic:
        return upsample2(K, n, q), 2 * n + 1
    return raw_values(K, q), n + 1


def cell_segments(a, b, c, d, L):
    """Edges crossed by level L in a cell with corners a=SW, b=SE, c=NE, d=NW.
    Returns a list of edge pairs; edges: 0 = S (a->b), 1 = E (b->c), 2 = N (d->c), 3 = W (a->d).
    Rule: a node is 'above' when v >= L. Saddles (5, 10) are resolved by the centre mean."""
    code = (1 if a >= L else 0) | (2 if b >= L else 0) | (4 if c >= L else 0) | (8 if d >= L else 0)
    if code == 0 or code == 15:
        return ()
    if code in (1, 14):
        return ((0, 3),)
    if code in (2, 13):
        return ((0, 1),)
    if code in (3, 12):
        return ((3, 1),)
    if code in (4, 11):
        return ((1, 2),)
    if code in (6, 9):
        return ((0, 2),)
    if code in (7, 8):
        return ((3, 2),)
    ctr = (a + b + c + d) >> 2
    if (code == 5) != (ctr >= L):
        return ((0, 3), (1, 2))
    return ((0, 1), (2, 3))


def edge_frac(v0, v1, L):
    """Crossing position along an edge from v0 to v1, Q8 (0..256)."""
    return ((L - v0) << 8) // (v1 - v0)


def tile_segments(U, M, cs16, step_m, only_level=None):
    """Contour segments over a flat value grid U (M x M) for every level that is a multiple
    of step_m (or just `only_level`): list of (level_m, (x0, y0), (x1, y1)) in 1/16 m from
    the tile origin; cs16 = cell size in 1/16 m. One pass over the cells, levels per cell
    (what the watch does)."""
    S = step_m * 256
    out = []
    for j in range(M - 1):
        for i in range(M - 1):
            a = U[j * M + i]
            b = U[j * M + i + 1]
            c = U[(j + 1) * M + i + 1]
            d = U[(j + 1) * M + i]
            lo = min(a, b, c, d)
            hi = max(a, b, c, d)
            k1 = hi // S
            k = lo // S + 1          # levels L with lo < L <= hi
            if k > k1:
                continue
            x0 = i * cs16
            y0 = j * cs16
            while k <= k1:
                L = k * S
                k += 1
                if only_level is not None and L != only_level * 256:
                    continue

                def pt(e):
                    if e == 0:
                        return (x0 + (edge_frac(a, b, L) * cs16 >> 8), y0)
                    if e == 1:
                        return (x0 + cs16, y0 + (edge_frac(b, c, L) * cs16 >> 8))
                    if e == 2:
                        return (x0 + (edge_frac(d, c, L) * cs16 >> 8), y0 + cs16)
                    return (x0, y0 + (edge_frac(a, d, L) * cs16 >> 8))
                for e0, e1 in cell_segments(a, b, c, d, L):
                    out.append((L // 256, pt(e0), pt(e1)))
    return out


def segments(U, M, cs16, level_m):
    """Segments of one level: [((x0, y0), (x1, y1))] in 1/16 m."""
    return [(p, q) for _l, p, q in tile_segments(U, M, cs16, level_m, level_m)]


def value_at(U, M, cs, fx, fy):
    """Bilinear elevation (metres, float) at tile-local metres - PC helper (altitude stand-in)."""
    i = min(M - 2, max(0, int(fx // cs)))
    j = min(M - 2, max(0, int(fy // cs)))
    u = fx / cs - i
    w = fy / cs - j
    a, b = U[j * M + i], U[j * M + i + 1]
    c, d = U[(j + 1) * M + i + 1], U[(j + 1) * M + i]
    return ((a * (1 - u) + b * u) * (1 - w) + (d * (1 - u) + c * u) * w) / 256.0


def tile_levels(U, step_m):
    """Levels (metres) that cross a value grid."""
    lo = min(U) // 256
    hi = max(U) // 256 + 1
    return list(range((lo // step_m) * step_m, hi + step_m, step_m))


def dem_grid_extent(width, height):
    return math.ceil(width / TILE_M), math.ceil(height / TILE_M)
