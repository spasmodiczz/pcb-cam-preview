"""
render.py -- 把 Gerber 几何渲染成接近实物的成品图

物理模型（以白色阻焊 + 黑色丝印 + 沉金为例）：
  板面基底               → 阻焊色（白）
  阻焊开窗 ∩ 铜          → 表面处理色（沉金）
  阻焊开窗 ∩ 无铜        → 基材色（FR-4）
  丝印（经 CAM 裁剪后）  → 丝印色（黑）
"""
from __future__ import annotations

from typing import Iterable

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon

# 工艺配色
STYLES = {
    ("white", "black", "enig"): dict(
        mask=(246, 246, 242), silk=(26, 26, 26), copper=(214, 174, 88),
        substrate=(196, 176, 128), bg=(18, 18, 18),
    ),
    ("black", "white", "enig"): dict(
        mask=(28, 28, 30), silk=(240, 240, 236), copper=(214, 174, 88),
        substrate=(196, 176, 128), bg=(18, 18, 18),
    ),
    ("green", "white", "hasl"): dict(
        mask=(22, 92, 60), silk=(242, 242, 238), copper=(202, 205, 210),
        substrate=(196, 176, 128), bg=(18, 18, 18),
    ),
}


def iter_polys(geom) -> Iterable[Polygon]:
    if geom is None or geom.is_empty:
        return
    if isinstance(geom, Polygon):
        yield geom
    elif isinstance(geom, (MultiPolygon, GeometryCollection)):
        for g in geom.geoms:
            yield from iter_polys(g)
    else:
        try:
            for g in geom.geoms:
                yield from iter_polys(g)
        except Exception:
            return


class Transform:
    """毫米 -> 像素（自动翻转 Y，使图像为俯视正像）。"""

    def __init__(self, bounds, width_px, margin_mm=0.0):
        x0, y0, x1, y1 = bounds
        self.x0, self.x1 = x0 - margin_mm, x1 + margin_mm
        self.y_top = y1 + margin_mm
        self.y_bot = y0 - margin_mm
        w_mm = self.x1 - self.x0
        h_mm = self.y_top - self.y_bot
        self.scale = width_px / w_mm
        self.W = int(round(w_mm * self.scale))
        self.H = int(round(h_mm * self.scale))

    def px(self, x, y):
        return ((x - self.x0) * self.scale, (self.y_top - y) * self.scale)

    def poly_px(self, coords):
        s, x0, yt = self.scale, self.x0, self.y_top
        return [((cx - x0) * s, (yt - cy) * s) for cx, cy in coords]


def _mask(geom, tr):
    """
    把几何栅格化为 L 通道掩膜。

    注意：CAM 输出的层里常出现「整板外框 + 巨大挖空」的容器多边形，
    而真正的图形作为独立多边形嵌在这个挖空里。因此必须按面积从大到小
    逐个绘制「外环→自身内环」，让容器先画、嵌套图形后画覆盖上去；
    不能把所有内环当成全局孔洞统一擦除（那样会抹掉嵌套图形）。
    """
    img = Image.new("L", (tr.W, tr.H), 0)
    d = ImageDraw.Draw(img)
    polys = [p for p in iter_polys(geom) if not p.is_empty]
    # 按「外环面积」而非「净面积」排序：容器多边形的净面积可能很小，
    # 但外环覆盖整板，必须先画。
    def _ext_area(p):
        xs, ys = p.exterior.xy
        return (max(xs) - min(xs)) * (max(ys) - min(ys))

    polys.sort(key=_ext_area, reverse=True)
    for p in polys:
        d.polygon(tr.poly_px(p.exterior.coords), fill=255)
        for ring in p.interiors:
            d.polygon(tr.poly_px(ring.coords), fill=0)
    return np.asarray(img)


def cutout_mask(tr, cutouts):
    """挖孔/挖槽 -> 布尔掩膜。"""
    m = np.zeros((tr.H, tr.W), dtype=bool)
    if not cutouts:
        return m
    img = Image.new("L", (tr.W, tr.H), 0)
    d = ImageDraw.Draw(img)
    for c in cutouts:
        cx, cy = tr.px(c["x"], c["y"])
        r = (c["d"] / 2.0) * tr.scale
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    return np.asarray(img) > 0


def composite(
    tr,
    mask_open=None,
    copper_top=None,
    silk=None,
    board_outline=None,
    style=("white", "black", "enig"),
    transparent_bg=False,
    cutouts=None,
):
    """
    mask_open   : 阻焊开窗（负片图形，有图形处 = 露出）
    copper_top  : 顶层铜（正面）/ 底层铜（反面）
    silk        : 丝印（已经过 CAM 裁剪）
    board_outline: 板框
    cutouts     : 挖孔列表 [{'x','y','d'}]，板面在此处被掏空
    """
    st = STYLES[style]
    H, W = tr.H, tr.W
    img = np.zeros((H, W, 4), dtype=np.uint8)

    board = _mask(board_outline, tr) > 0 if board_outline is not None else np.ones((H, W), bool)
    cut = cutout_mask(tr, cutouts)
    if cut.any():
        board = board & ~cut

    # 1. 板面基底 = 阻焊
    img[board] = (*st["mask"], 255)

    # 2. 阻焊开窗区域
    if mask_open is not None:
        op = _mask(mask_open, tr) > 0
        if copper_top is not None:
            cu = _mask(copper_top, tr) > 0
        else:
            cu = np.zeros((H, W), bool)
        region = op & board
        img[region & cu] = (*st["copper"], 255)
        img[region & ~cu] = (*st["substrate"], 255)

    # 3. 丝印（最上层）
    if silk is not None:
        sk = _mask(silk, tr) > 0
        img[sk & board] = (*st["silk"], 255)

    if transparent_bg:
        img[~board] = (0, 0, 0, 0)
    else:
        img[~board] = (*st["bg"], 255)

    return Image.fromarray(img, mode="RGBA")


_FONT_CACHE = {}


def _font(size):
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    f = None
    for p in (
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\segoeui.ttf",
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            f = ImageFont.truetype(p, size)
            break
        except Exception:
            continue
    if f is None:
        try:
            f = ImageFont.load_default(size=size)
        except Exception:
            f = ImageFont.load_default()
    _FONT_CACHE[size] = f
    return f


def annotate_cutouts(img, tr, cutouts, color=(255, 92, 92), label_prefix="挖孔"):
    """
    在成品图上标出挖孔：环形高亮 + 引线 + 标签（孔径 / 坐标）。
    返回新图，不修改入参。
    """
    if not cutouts:
        return img
    out = img.convert("RGBA").copy()
    d = ImageDraw.Draw(out)
    fs = max(15, min(34, int(tr.W / 70.0)))
    f = _font(fs)
    H, W = tr.H, tr.W

    for i, c in enumerate(cutouts):
        cx, cy = tr.px(c["x"], c["y"])
        r = (c["d"] / 2.0) * tr.scale
        rr = r + max(5, fs * 0.35)
        lw = max(2, fs // 9)

        # 虚线圈
        for a in range(0, 360, 14):
            d.arc([cx - rr, cy - rr, cx + rr, cy + rr], a, a + 7, fill=color, width=lw)

        txt = f"{label_prefix}{i+1}  ⌀{c['d']:.2f} mm"
        try:
            tw = d.textlength(txt, font=f)
            asc, desc = f.getmetrics()
            th = asc + desc
        except Exception:
            tw, th = len(txt) * fs * 0.62, fs * 1.3
        pad = fs * 0.5
        bw, bh = tw + pad * 2, th + pad * 0.6

        # 放置：默认右侧；右侧放不下改左侧；都放不下就放下方
        if cx + rr + 14 + bw < W - 2:
            lx, ly = cx + rr + 14, cy - bh / 2
        elif cx - rr - 14 - bw > 2:
            lx, ly = cx - rr - 14 - bw, cy - bh / 2
        else:
            lx, ly = cx - bw / 2, cy + rr + 14
        lx = max(2, min(W - bw - 2, lx))
        ly = max(2, min(H - bh - 2, ly))

        # 引线：从圆心连到标签最近边
        ax = min(max(cx, lx), lx + bw)
        ay = min(max(cy, ly), ly + bh)
        d.line([(cx, cy), (ax, ay)], fill=color, width=1)

        d.rectangle([lx, ly, lx + bw, ly + bh], fill=(18, 19, 21, 240),
                    outline=color, width=lw)
        d.text((lx + pad, ly + bh / 2), txt, font=f, fill=color, anchor="lm")
    return out



def diff_highlight(tr, removed, base_style=("white", "black", "enig")):
    """把「被 CAM 裁掉的丝印」高亮成红色叠图，用于问题定位。"""
    st = STYLES[base_style]
    img = np.zeros((tr.H, tr.W, 4), dtype=np.uint8)
    img[:, :] = (*st["mask"], 255)
    if removed is not None:
        rm = _mask(removed, tr) > 0
        img[rm] = (226, 75, 74, 255)
    return Image.fromarray(img, mode="RGBA")
