"""Geometry helpers: Douglas-Peucker, rectangle clipping, chaining, quantization."""
import math


def _seg_dist2(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 == 0:
        ex, ey = px - ax, py - ay
        return ex * ex + ey * ey
    t = ((px - ax) * dx + (py - ay) * dy) / l2
    if t < 0:
        t = 0.0
    elif t > 1:
        t = 1.0
    ex, ey = px - (ax + t * dx), py - (ay + t * dy)
    return ex * ex + ey * ey


def simplify(pts, eps):
    """Douglas-Peucker (iterative). Keeps first and last point."""
    n = len(pts)
    if n <= 2 or eps <= 0:
        return list(pts)
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    e2 = eps * eps
    while stack:
        i, j = stack.pop()
        ax, ay = pts[i]
        bx, by = pts[j]
        best, bi = -1.0, -1
        for k in range(i + 1, j):
            d = _seg_dist2(pts[k][0], pts[k][1], ax, ay, bx, by)
            if d > best:
                best, bi = d, k
        if best > e2:
            keep[bi] = True
            stack.append((i, bi))
            stack.append((bi, j))
    return [p for p, k in zip(pts, keep) if k]


def simplify_ring(ring, eps):
    """DP for a closed ring (closing edge implied). Splits at the point farthest from ring[0]."""
    n = len(ring)
    if n <= 3:
        return list(ring)
    x0, y0 = ring[0]
    far = max(range(n), key=lambda k: (ring[k][0] - x0) ** 2 + (ring[k][1] - y0) ** 2)
    a = simplify(ring[: far + 1], eps)
    b = simplify(ring[far:] + [ring[0]], eps)
    return a[:-1] + b[:-1]


def ring_area(ring):
    s = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def line_length(pts):
    return sum(math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]) for i in range(len(pts) - 1))


def bbox(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def clip_line(pts, xmin, ymin, xmax, ymax):
    """Clip a polyline to a closed rectangle. Returns a list of polylines (Liang-Barsky)."""
    out = []
    cur = []
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        dx, dy = x1 - x0, y1 - y0
        t0, t1 = 0.0, 1.0
        ok = True
        for p, q in ((-dx, x0 - xmin), (dx, xmax - x0), (-dy, y0 - ymin), (dy, ymax - y0)):
            if p == 0:
                if q < 0:
                    ok = False
                    break
            else:
                r = q / p
                if p < 0:
                    if r > t1:
                        ok = False
                        break
                    if r > t0:
                        t0 = r
                else:
                    if r < t0:
                        ok = False
                        break
                    if r < t1:
                        t1 = r
        if not ok:
            if cur:
                out.append(cur)
                cur = []
            continue
        a = (x0 + t0 * dx, y0 + t0 * dy) if t0 > 0 else (x0, y0)
        b = (x0 + t1 * dx, y0 + t1 * dy) if t1 < 1 else (x1, y1)
        if not cur:
            cur = [a, b]
        elif t0 > 0:  # re-entered: start a new piece
            out.append(cur)
            cur = [a, b]
        else:
            cur.append(b)
        if t1 < 1:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return [c for c in out if len(c) >= 2]


def clip_ring(ring, xmin, ymin, xmax, ymax):
    """Sutherland-Hodgman clip of a closed ring against a rectangle. Returns ring or []."""
    def clip_edge(poly, inside, inter):
        res = []
        n = len(poly)
        for i in range(n):
            cur = poly[i]
            prev = poly[i - 1]
            ci, pi_ = inside(cur), inside(prev)
            if ci:
                if not pi_:
                    res.append(inter(prev, cur))
                res.append(cur)
            elif pi_:
                res.append(inter(prev, cur))
        return res

    def ix(c):
        def f(a, b):
            t = (c - a[0]) / (b[0] - a[0])
            return (c, a[1] + t * (b[1] - a[1]))
        return f

    def iy(c):
        def f(a, b):
            t = (c - a[1]) / (b[1] - a[1])
            return (a[0] + t * (b[0] - a[0]), c)
        return f

    poly = list(ring)
    for inside, inter in (
        (lambda p: p[0] >= xmin, ix(xmin)),
        (lambda p: p[0] <= xmax, ix(xmax)),
        (lambda p: p[1] >= ymin, iy(ymin)),
        (lambda p: p[1] <= ymax, iy(ymax)),
    ):
        if not poly:
            return []
        poly = clip_edge(poly, inside, inter)
    return poly if len(poly) >= 3 else []


def quantize(pts, ox, oy, q, lim=256):
    """Metres -> integer tile units, clamped to [0, lim], consecutive duplicates removed."""
    out = []
    for x, y in pts:
        ix = int(math.floor((x - ox) / q + 0.5))
        iy = int(math.floor((y - oy) / q + 0.5))
        ix = 0 if ix < 0 else (lim if ix > lim else ix)
        iy = 0 if iy < 0 else (lim if iy > lim else iy)
        if not out or out[-1] != (ix, iy):
            out.append((ix, iy))
    return out


def dedupe_ring(pts):
    """Cyclic dedupe + removal of zero-width spikes (A,B,A) in a quantized ring."""
    out = list(pts)
    changed = True
    while changed and len(out) >= 3:
        changed = False
        n = len(out)
        for i in range(n):
            if out[i] == out[(i + 1) % n]:
                del out[i]
                changed = True
                break
            if out[i - 1] == out[(i + 1) % n]:
                del out[i]  # leaves A,A which the next pass collapses
                changed = True
                break
    return out if len(out) >= 3 else []


def chain_exact(lines):
    """Merge polylines [(key, pts)] of the same key whose ends coincide exactly.

    Greedy: at junctions any two ends may be joined (strokes render identically).
    Closed lines are left alone. Returns list of (key, pts); deterministic."""
    from collections import defaultdict
    out = []
    groups = defaultdict(list)
    order = []
    for key, pts in lines:
        if key not in groups:
            order.append(key)
        groups[key].append(list(pts))
    for key in order:
        group = groups[key]
        ends = defaultdict(list)
        for i, p in enumerate(group):
            if p[0] != p[-1]:
                ends[p[0]].append(i)
                ends[p[-1]].append(i)
        alive = [True] * len(group)

        def unlink(j):
            q = group[j]
            ends[q[0]].remove(j)
            ends[q[-1]].remove(j)

        for i in range(len(group)):
            p = group[i]
            if not alive[i] or p[0] == p[-1]:
                continue
            unlink(i)
            while p[0] != p[-1] and ends[p[-1]]:
                j = ends[p[-1]][0]
                unlink(j)
                alive[j] = False
                q = group[j]
                p = p + (q[1:] if q[0] == p[-1] else q[::-1][1:])
            while p[0] != p[-1] and ends[p[0]]:
                j = ends[p[0]][0]
                unlink(j)
                alive[j] = False
                q = group[j]
                p = (q if q[-1] == p[0] else q[::-1])[:-1] + p
            group[i] = p
        out.extend((key, p) for i, p in enumerate(group) if alive[i])
    return out
