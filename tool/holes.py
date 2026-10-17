"""
holes.py -- 钻孔/挖孔解析

两套文件的钻孔格式并不统一，这里都吃：
  * Excellon（EDA 的 Drill_*.DRL，以 M48 开头）
  * Gerber 闪光（立创生产稿的 drl，本质是带 D03 的 Gerber）

输出统一的圆孔列表 [(x, y, d)]，单位毫米。
"""
from __future__ import annotations

import os
import re

from gerber_engine import GerberLayer, IN_TO_MM

DRILL_HINTS = (".drl", "drl", "drill", "nc", "ncd")


def is_drill_file(name: str) -> bool:
    base = os.path.basename(name).lower()
    if base.endswith(".drl"):
        return True
    return base in ("drl", "drill", "nc", "ncd")


def is_nonplated(name: str, text: str) -> bool:
    low = (os.path.basename(name) + "\n" + text[:400]).lower()
    return ("npth" in low) or ("non_plated" in low) or ("non-plated" in low)


def parse_excellon(text: str) -> list[tuple[float, float, float]]:
    lines = [ln.strip() for ln in text.splitlines()]
    unit = "MM"
    xi = xd = 4
    tools: dict[str, float] = {}
    cur_tool = None
    holes = []
    in_header = True

    for ln in lines:
        if not ln or ln.startswith(";"):
            continue
        up = ln.upper()
        if up.startswith("M48"):
            in_header = True
            continue
        if ln == "%" or up.startswith("M95"):
            in_header = False
            continue
        if up.startswith("METRIC"):
            unit = "MM"
            m = re.search(r"(\d+)\.(\d+)", ln)
            if m:
                xi, xd = int(m.group(1)), int(m.group(2))
            continue
        if up.startswith("INCH"):
            unit = "IN"
            m = re.search(r"(\d+)\.(\d+)", ln)
            if m:
                xi, xd = int(m.group(1)), int(m.group(2))
            continue
        m = re.match(r"^T0*(\d+)C([\d.]+)", up)
        if m:
            tools[m.group(1)] = float(m.group(2))
            continue
        m = re.match(r"^T0*(\d+)\s*$", up)
        if m:
            cur_tool = m.group(1)
            continue
        m = re.match(r"^X([+-]?[\d.]+)?Y([+-]?[\d.]+)?", up)
        if m and not up.startswith(("G0", "M")):
            def val(s):
                if s is None:
                    return None
                return float(s) if "." in s else int(s) / (10 ** xd)

            x = val(m.group(1))
            y = val(m.group(2))
            d = tools.get(cur_tool, 0.0) if cur_tool else 0.0
            if x is not None and y is not None:
                k = IN_TO_MM if unit == "IN" else 1.0
                holes.append((x * k, y * k, d * k))
    return holes


def parse_gerber_drill(text: str) -> list[tuple[float, float, float]]:
    g = GerberLayer(data=text)
    out = []
    for ob in g.objects:
        if type(ob).__name__ == "Flash":
            ap = ob.ap
            d = getattr(ap, "d", None)
            if d is None:
                shp = ap.shapes(ob.x, ob.y)
                if shp:
                    s = shp[0]
                    if isinstance(s, tuple):
                        s = s[1]
                    try:
                        d = 2 * (s.area / 3.141592653589793) ** 0.5
                    except Exception:
                        d = 0.0
                else:
                    d = 0.0
            out.append((ob.x, ob.y, d))
    return out


def load_holes(files: dict[str, bytes]) -> dict:
    """返回 {'cutouts': [...], 'all': [...], 'files': [...]}"""
    all_holes = []
    npth = []
    used = []
    for name, data in files.items():
        if not is_drill_file(name):
            continue
        text = data.decode("utf-8", "replace")
        # Excellon 的 M48 可能不在首行（EDA 前面还有 ;TYPE / ;Layer 注释）
        head = text[:500]
        try:
            holes = parse_excellon(text) if "M48" in head else parse_gerber_drill(text)
        except Exception:
            continue
        if not holes:
            continue
        used.append(os.path.basename(name))
        all_holes.extend(holes)
        if is_nonplated(name, text):
            npth.extend(holes)
    return dict(all=all_holes, npth=npth, files=used)


def pick_cutouts(holes: dict, min_d: float = 1.0) -> list[dict]:
    """
    选出「挖孔」。
    优先用 NPTH 标记；没有标记（立创生产稿）时按孔径阈值判定：
    0.3mm 级别是过孔，≥1mm 的才是机械挖孔。
    """
    src = holes.get("npth") or holes.get("all") or []
    out = []
    seen = set()
    for x, y, d in src:
        if d < min_d:
            continue
        key = (round(x, 2), round(y, 2), round(d, 2))
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(x=x, y=y, d=d, area=3.141592653589793 * (d / 2) ** 2))
    out.sort(key=lambda c: -c["d"])
    return out
