"""
gerber_engine.py -- 轻量级 RS-274X 几何引擎

把 Gerber 图层解析为 shapely 几何（统一毫米），供 CAM 规则模拟与渲染使用。
支持：FS/MO、标准光圈 C/R/O/P、AM 宏(图元 1/4/5/20/21/22)、
G01/G02/G03、G74/G75、G36/G37 区域、LPD/LPC。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Iterable

from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.ops import unary_union

IN_TO_MM = 25.4
ARC_SEG_MM = 0.02  # 圆弧离散精度

# ---------------------------------------------------------------- apertures


class Aperture:
    def shapes(self, x: float, y: float) -> list[Polygon]:
        raise NotImplementedError

    def sweep(self, points: list[tuple[float, float]]) -> Polygon | None:
        """线段扫掠（Minkowski 和）。默认用凸包 + 端点形状近似。"""
        shapes = [s for p in (points[0], points[-1]) for s in self.shapes(*p)]
        if not shapes:
            return None
        hull = unary_union(shapes).convex_hull
        for a, b in zip(points, points[1:]):
            if a == b:
                continue
            seg = LineString([a, b]).buffer(1e-6, resolution=1)
            hull = hull.union(seg.convex_hull)
        return hull


@dataclass
class Circle(Aperture):
    d: float
    polarity: str = "D"

    def shapes(self, x, y):
        return [Point(x, y).buffer(self.d / 2.0, resolution=16)]

    def sweep(self, points):
        if len(points) >= 2:
            return LineString(points).buffer(
                self.d / 2.0, cap_style=1, join_style=1, resolution=16
            )
        return self.shapes(*points[0])[0]


@dataclass
class Rect(Aperture):
    w: float
    h: float
    polarity: str = "D"

    def shapes(self, x, y):
        from shapely.geometry import box

        return [box(x - self.w / 2, y - self.h / 2, x + self.w / 2, y + self.h / 2)]


@dataclass
class Obround(Aperture):
    w: float
    h: float
    polarity: str = "D"

    def shapes(self, x, y):
        from shapely.geometry import box

        if self.w >= self.h:
            r = self.h / 2.0
            c1 = Point(x - self.w / 2 + r, y).buffer(r, resolution=16)
            c2 = Point(x + self.w / 2 - r, y).buffer(r, resolution=16)
            mid = box(x - self.w / 2 + r, y - r, x + self.w / 2 - r, y + r)
            return [unary_union([c1, c2, mid])]
        r = self.w / 2.0
        c1 = Point(x, y - self.h / 2 + r).buffer(r, resolution=16)
        c2 = Point(x, y + self.h / 2 - r).buffer(r, resolution=16)
        mid = box(x - r, y - self.h / 2 + r, x + r, y + self.h / 2 - r)
        return [unary_union([c1, c2, mid])]


@dataclass
class PolyAperture(Aperture):
    outer: float
    n: int
    rot: float = 0.0
    hole: float = 0.0
    polarity: str = "D"

    def shapes(self, x, y):
        pts = []
        for i in range(self.n):
            a = math.radians(self.rot + i * 360.0 / self.n)
            pts.append((x + self.outer / 2 * math.cos(a), y + self.outer / 2 * math.sin(a)))
        outer = Polygon(pts)
        if self.hole > 0:
            c = Point(x, y).buffer(self.hole / 2, resolution=16)
            return [outer.difference(c)]
        return [outer]


@dataclass
class MacroAperture(Aperture):
    """宏：按参数展开为若干带极性的形状。"""

    prims: list = field(default_factory=list)  # [(polarity, fn(x,y)->Polygon)]
    polarity: str = "D"

    def shapes(self, x, y):
        out = []
        for pol, fn in self.prims:
            try:
                out.append((pol, fn(x, y)))
            except Exception:
                pass
        return out

    def sweep(self, points):
        if len(points) < 2:
            return None
        polys = []
        for a, b in zip(points, points[1:]):
            if a == b:
                continue
            pts = self.shapes(*a)[:1] + self.shapes(*b)[:1]
            if not pts:
                continue
            shapes = [p for p in pts if isinstance(p, Polygon)]
            if shapes:
                polys.append(unary_union(shapes).convex_hull)
        if not polys:
            return None
        return unary_union(polys)


# ---------------------------------------------------------------- objects


@dataclass
class Flash:
    ap: Aperture
    x: float
    y: float
    polarity: str = "D"


@dataclass
class Stroke:
    ap: Aperture
    pts: list
    polarity: str = "D"


@dataclass
class Region:
    rings: list
    polarity: str = "D"


# ---------------------------------------------------------------- parser


class GerberParseError(Exception):
    pass


class GerberLayer:
    def __init__(self, path=None, data=None):
        self.path = path
        self.objects: list = []
        self.unit = "MM"
        self.x_int = self.x_dec = 4
        self.y_int = self.y_dec = 4
        self.attr = {}
        if data is None:
            with open(path, "r", errors="replace") as fh:
                data = fh.read()
        self._parse(data)

    # -- helpers ---------------------------------------------------------
    def _coord(self, s: str, dec: int) -> float:
        v = int(s)
        val = v / (10 ** dec)
        return val * (IN_TO_MM if self.unit == "IN" else 1.0)

    # -- main ------------------------------------------------------------
    def _parse(self, data: str):
        parts = data.split("%")
        apertures: dict[int, Aperture] = {}
        macros: dict[str, list] = {}

        cur_ap: Aperture | None = None
        polarity = "D"
        interp = 1
        multi_quadrant = True
        in_region = False
        region_rings: list = []
        cur_ring: list = []
        x = y = 0.0
        stroke_pts: list = []

        def flush_stroke():
            nonlocal stroke_pts
            if len(stroke_pts) >= 2 and cur_ap is not None:
                self.objects.append(Stroke(cur_ap, list(stroke_pts), polarity))
            stroke_pts = []

        for idx, chunk in enumerate(parts):
            if idx % 2 == 1:
                # 扩展命令 %...%
                cmd = chunk.strip()
                if not cmd:
                    continue
                if cmd.startswith("FS"):
                    m = re.match(r"FS([LT])([AI])X(\d)(\d)Y(\d)(\d)", cmd)
                    if m:
                        self.x_int, self.x_dec = int(m.group(3)), int(m.group(4))
                        self.y_int, self.y_dec = int(m.group(5)), int(m.group(6))
                elif cmd.startswith("MO"):
                    m = re.match(r"MO(IN|MM)", cmd)
                    if m:
                        self.unit = m.group(1)
                elif cmd.startswith("LP"):
                    m = re.match(r"LP([DC])", cmd)
                    if m:
                        polarity = m.group(1)
                elif cmd.startswith("AM"):
                    body = cmd[2:]
                    nl = re.search(r"[*\n]", body)
                    name = body[: nl.start()].strip() if nl else body.strip()
                    raw = body[nl.start() :].lstrip("*\n") if nl else ""
                    macros[name] = raw
                elif cmd.startswith("AD"):
                    m = re.match(r"ADD(\d+)([A-Za-z_][\w.]*)(?:,([^*]*))?", cmd)
                    if m:
                        code = int(m.group(1))
                        kind = m.group(2)
                        params = [p for p in (m.group(3) or "").split("X") if p.strip()]
                        try:
                            apertures[code] = self._make_aperture(kind, params, macros, self.unit)
                        except Exception:
                            apertures[code] = Circle(_f(params[0]) if params else 0.1)
                continue

            # 普通命令
            for stmt in chunk.split("*"):
                s = stmt.strip()
                if not s or s.startswith("G04") or s in ("M02", "M00", "M01"):
                    continue
                gs = re.findall(r"G0?(\d{1,3})", s)
                words = re.findall(r"([XYIJ])([+-]?\d+)", s)
                dm = re.search(r"D0*(\d{1,3})\s*$", s)

                for g in gs:
                    gi = int(g)
                    if gi in (1, 2, 3):
                        interp = gi
                    elif gi == 36:
                        in_region = True
                        region_rings, cur_ring = [], []
                    elif gi == 37:
                        if cur_ring:
                            region_rings.append(cur_ring)
                            cur_ring = []
                        if region_rings:
                            self.objects.append(Region(region_rings, polarity))
                        in_region = False
                        region_rings, cur_ring = [], []
                    elif gi == 74:
                        multi_quadrant = False
                    elif gi == 75:
                        multi_quadrant = True

                nx, ny, ii, jj = x, y, 0.0, 0.0
                has_xy = False
                for w, val in words:
                    if w == "X":
                        nx = self._coord(val, self.x_dec)
                        has_xy = True
                    elif w == "Y":
                        ny = self._coord(val, self.y_dec)
                        has_xy = True
                    elif w == "I":
                        ii = self._coord(val, self.x_dec)
                    elif w == "J":
                        jj = self._coord(val, self.y_dec)

                op = None
                if dm:
                    code = int(dm.group(1))
                    if code >= 10:
                        cur_ap = apertures.get(code)
                        op = None
                    elif code in (1, 2, 3):
                        op = code

                if op is None:
                    x, y = nx, ny
                    continue

                if op == 2:  # move
                    if in_region:
                        if cur_ring:
                            region_rings.append(cur_ring)
                        cur_ring = [(nx, ny)]
                    else:
                        flush_stroke()
                        stroke_pts = [(nx, ny)]
                    x, y = nx, ny
                    continue

                if op == 1:  # draw
                    if interp == 1:
                        seg = [(x, y), (nx, ny)]
                    else:
                        seg = _arc_points(
                            (x, y), (nx, ny), ii, jj, interp, multi_quadrant
                        )
                    if in_region:
                        cur_ring.extend(seg[1:])
                    else:
                        if not stroke_pts:
                            stroke_pts = [(x, y)]
                        stroke_pts.extend(seg[1:])
                    x, y = nx, ny
                    continue

                if op == 3:  # flash
                    if cur_ap is not None:
                        self.objects.append(Flash(cur_ap, nx, ny, polarity))
                    x, y = nx, ny
        flush_stroke()

    # -- aperture helpers ------------------------------------------------
    def _num(self, tok: str, params: list[float], scale: float = 1.0) -> float:
        """求值宏参数：支持 $1..$n 以及 0-$2 这类简单算式（EDA 的 RoundRect 就这么写）。"""
        s = tok.strip()
        if not s:
            return 0.0
        if s.startswith("$") and s[1:].isdigit():
            i = int(s[1:]) - 1
            return (params[i] if 0 <= i < len(params) else 0.0) * scale
        if "$" in s:
            def rep(m):
                i = int(m.group(1)) - 1
                return repr(params[i] if 0 <= i < len(params) else 0.0)

            expr = re.sub(r"\$(\d+)", rep, s)
            try:
                return float(eval(expr, {"__builtins__": {}}, {})) * scale
            except Exception:
                return 0.0
        try:
            return float(s) * scale
        except Exception:
            return 0.0

    def _parse_macro(self, body: str, params: list[float], unit: str):
        """把宏体按 '*' 拆成图元语句，逐条展开为 (exposure, fn(x,y)->Polygon)。"""
        scale = IN_TO_MM if unit == "IN" else 1.0
        prims = []
        for stmt in body.split("*"):
            stmt = stmt.strip()
            if not stmt:
                continue
            toks = [t.strip() for t in stmt.split(",")]
            if len(toks) < 2:
                continue
            code = toks[0]
            try:
                if code == "4":  # outline
                    exposure = int(round(self._num(toks[1], params)))
                    n = int(round(self._num(toks[2], params)))
                    pts = []
                    for k in range(n):
                        pts.append(
                            (
                                self._num(toks[3 + 2 * k], params, scale),
                                self._num(toks[4 + 2 * k], params, scale),
                            )
                        )
                    if len(pts) >= 3:
                        poly = Polygon(pts)
                        if poly.is_valid and poly.area > 0:
                            prims.append((exposure, _const(poly)))
                elif code == "1":  # circle
                    exposure = int(round(self._num(toks[1], params)))
                    d = self._num(toks[2], params, scale)
                    cx = self._num(toks[3], params, scale)
                    cy = self._num(toks[4], params, scale)
                    prims.append(
                        (
                            exposure,
                            lambda X, Y, d=d, cx=cx, cy=cy: Point(X + cx, Y + cy).buffer(
                                d / 2.0, resolution=16
                            ),
                        )
                    )
                elif code in ("20", "2"):  # vector line
                    exposure = int(round(self._num(toks[1], params)))
                    w = self._num(toks[2], params, scale)
                    x1 = self._num(toks[3], params, scale)
                    y1 = self._num(toks[4], params, scale)
                    x2 = self._num(toks[5], params, scale)
                    y2 = self._num(toks[6], params, scale)
                    prims.append(
                        (
                            exposure,
                            lambda X, Y, w=w, x1=x1, y1=y1, x2=x2, y2=y2: LineString(
                                [(X + x1, Y + y1), (X + x2, Y + y2)]
                            ).buffer(w / 2.0, cap_style=1, resolution=16),
                        )
                    )
                elif code == "21":  # center line
                    exposure = int(round(self._num(toks[1], params)))
                    w = self._num(toks[2], params, scale)
                    h = self._num(toks[3], params, scale)
                    cx = self._num(toks[4], params, scale)
                    cy = self._num(toks[5], params, scale)
                    prims.append((exposure, _centered_rect(w, h, cx, cy)))
                elif code == "22":  # lower-left line
                    exposure = int(round(self._num(toks[1], params)))
                    w = self._num(toks[2], params, scale)
                    h = self._num(toks[3], params, scale)
                    x = self._num(toks[4], params, scale)
                    y = self._num(toks[5], params, scale)
                    prims.append((exposure, _centered_rect(w, h, x + w / 2, y + h / 2)))
                elif code == "5":  # polygon
                    exposure = int(round(self._num(toks[1], params)))
                    n = int(round(self._num(toks[2], params)))
                    cx = self._num(toks[3], params, scale)
                    cy = self._num(toks[4], params, scale)
                    d = self._num(toks[5], params, scale)
                    rot = self._num(toks[6], params) if len(toks) > 6 else 0.0
                    prims.append((exposure, _poly_fn(cx, cy, d, n, rot)))
            except Exception:
                continue
        return prims

    def _make_aperture(self, kind: str, params, macros, unit):
        scale = IN_TO_MM if unit == "IN" else 1.0
        f = lambda i: float(params[i]) * scale
        if kind == "C" and params:
            return Circle(f(0))
        if kind == "R" and len(params) >= 2:
            return Rect(f(0), f(1))
        if kind == "O" and len(params) >= 2:
            return Obround(f(0), f(1))
        if kind == "P" and params:
            outer = f(0)
            n = int(float(params[1])) if len(params) > 1 else 3
            rot = float(params[2]) if len(params) > 2 else 0.0
            hole = f(3) if len(params) > 3 else 0.0
            return PolyAperture(outer, n, rot, hole)
        if kind in macros:
            pv = []
            for p in params:
                try:
                    pv.append(float(p))
                except Exception:
                    pv.append(0.0)
            return MacroAperture(self._parse_macro(macros[kind], pv, unit))
        if params:
            return Circle(f(0))
        return Circle(0.1)

    # -- geometry --------------------------------------------------------
    def to_shapely(self, tolerance: float = 0.0):
        """返回 (geometry) —— 已按 LPD/LPC 顺序合成。"""
        runs: list[tuple[str, list]] = []
        for ob in self.objects:
            pol = ob.polarity
            if runs and runs[-1][0] == pol:
                runs[-1][1].append(ob)
            else:
                runs.append((pol, [ob]))

        result = None
        for pol, obs in runs:
            geoms = []
            for ob in obs:
                if isinstance(ob, Flash):
                    shp = ob.ap.shapes(ob.x, ob.y)
                    if isinstance(shp, list):
                        for s in shp:
                            if isinstance(s, tuple):
                                s = s[1]
                            if isinstance(s, Polygon):
                                geoms.append(s)
                    elif isinstance(shp, Polygon):
                        geoms.append(shp)
                elif isinstance(ob, Stroke):
                    g = ob.ap.sweep(ob.pts)
                    if g is not None:
                        geoms.append(g)
                elif isinstance(ob, Region):
                    g = _region_to_poly(ob.rings)
                    if g is not None:
                        geoms.append(g)
            if not geoms:
                continue
            merged = unary_union(geoms)
            if result is None:
                result = merged if pol == "D" else None
            else:
                result = (
                    result.union(merged) if pol == "D" else result.difference(merged)
                )
        if result is None:
            from shapely.geometry import GeometryCollection

            return GeometryCollection()
        return result

    def bounds(self):
        xs, ys = [], []
        for ob in self.objects:
            if isinstance(ob, Flash):
                xs.append(ob.x)
                ys.append(ob.y)
            elif isinstance(ob, Stroke):
                xs.extend(p[0] for p in ob.pts)
                ys.extend(p[1] for p in ob.pts)
            elif isinstance(ob, Region):
                for r in ob.rings:
                    xs.extend(p[0] for p in r)
                    ys.extend(p[1] for p in r)
        if not xs:
            return None
        return (min(xs), min(ys), max(xs), max(ys))


# ---------------------------------------------------------------- utils


def _f(s):
    try:
        return abs(float(s))
    except Exception:
        return 0.1


def _const(poly):
    return lambda X, Y: Polygon(
        [(X + px, Y + py) for px, py in zip(poly.exterior.xy[0], poly.exterior.xy[1])]
    )


def _centered_rect(w, h, cx, cy):
    from shapely.geometry import box

    return lambda X, Y: box(
        X + cx - w / 2, Y + cy - h / 2, X + cx + w / 2, Y + cy + h / 2
    )


def _poly_fn(cx, cy, d, n, rot):
    def fn(X, Y):
        pts = []
        for i in range(max(3, n)):
            a = math.radians(rot + i * 360.0 / max(3, n))
            pts.append((X + cx + d / 2 * math.cos(a), Y + cy + d / 2 * math.sin(a)))
        return Polygon(pts)

    return fn


def _obround(cx, cy, w, h):
    def fn(X, Y):
        from shapely.geometry import box

        if w >= h:
            r = h / 2.0
            return unary_union(
                [
                    Point(X + cx - w / 2 + r, Y + cy).buffer(r, resolution=16),
                    Point(X + cx + w / 2 - r, Y + cy).buffer(r, resolution=16),
                    box(
                        X + cx - w / 2 + r,
                        Y + cy - r,
                        X + cx + w / 2 - r,
                        Y + cy + r,
                    ),
                ]
            )
        r = w / 2.0
        return unary_union(
            [
                Point(X + cx, Y + cy - h / 2 + r).buffer(r, resolution=16),
                Point(X + cx, Y + cy + h / 2 - r).buffer(r, resolution=16),
                box(X + cx - r, Y + cy - h / 2 + r, X + cx + r, Y + cy + h / 2 - r),
            ]
        )

    return fn


def _arc_points(p0, p1, i, j, interp, multi_quadrant):
    """把圆弧离散为折线点列（含终点）。"""
    cx, cy = p0[0] + i, p0[1] + j
    r = math.hypot(p0[0] - cx, p0[1] - cy)
    if r <= 1e-9:
        return [p0, p1]
    a0 = math.atan2(p0[1] - cy, p0[0] - cx)
    a1 = math.atan2(p1[1] - cy, p1[0] - cx)
    if interp == 2:  # CW
        while a1 >= a0:
            a1 -= 2 * math.pi
        if not multi_quadrant and abs(a0 - a1) < 1e-12:
            a1 -= 2 * math.pi
    else:  # CCW
        while a1 <= a0:
            a1 += 2 * math.pi
        if not multi_quadrant and abs(a0 - a1) < 1e-12:
            a1 += 2 * math.pi
    sweep = abs(a1 - a0)
    n = max(2, int(sweep * r / ARC_SEG_MM) + 1)
    n = min(n, 2000)
    pts = []
    for k in range(n + 1):
        a = a0 + (a1 - a0) * k / n
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    pts[-1] = p1
    return pts


def _region_to_poly(rings):
    polys = []
    for r in rings:
        if len(r) < 3:
            continue
        try:
            p = Polygon(r)
            if not p.is_valid:
                p = p.buffer(0)
            if p.is_empty:
                continue
            polys.append(p)
        except Exception:
            continue
    if not polys:
        return None
    polys.sort(key=lambda p: p.area, reverse=True)
    outer = polys[0]
    holes = [p for p in polys[1:] if outer.contains(p)]
    if holes:
        try:
            outer = Polygon(
                outer.exterior.coords,
                [h.exterior.coords for h in holes if not h.is_empty],
            )
            if not outer.is_valid:
                outer = outer.buffer(0)
        except Exception:
            pass
    rest = [p for p in polys[1:] if p not in holes]
    if rest:
        return unary_union([outer] + rest)
    return outer
