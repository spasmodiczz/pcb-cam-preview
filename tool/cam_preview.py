"""
cam_preview.py -- 嘉立创 CAM 规则预演工具

在下单前，用「板厂视角」把你导出的 Gerber 渲染成实际成品外观，
并列出会被 CAM 删掉/改动的丝印区域。

用法:
  python cam_preview.py --eda <EDA导出zip> [--jlc <立创生产稿zip>] --out out/

  --eda   嘉立创EDA「导出 - PCB制板文件(Gerber)」生成的 zip
  --jlc   可选。立创生产稿 zip（含 ok/ 文件夹），用于自检与精度评分
  --mask  阻焊颜色: white / black / green      (默认 white)
  --silk  丝印颜色: black / white              (默认 black)
  --finish 表面处理: enig / hasl               (默认 enig)
  --silk-clearance  丝印距开窗的安全间距 mm    (默认 0.0，实测最贴合)
  --mask-expand     阻焊开窗单边补偿 mm        (默认 0.0，实测最贴合)
  --width           渲染宽度 px                (默认 2000)
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import time
import zipfile
from dataclasses import dataclass, field

from shapely.geometry import Polygon, box
from shapely.ops import unary_union

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gerber_engine import GerberLayer  # noqa: E402
from holes import load_holes, pick_cutouts  # noqa: E402
from render import (  # noqa: E402
    STYLES, Transform, annotate_cutouts, composite, _mask,
)

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

# ---------------------------------------------------------------- 图层识别

# 立创生产稿（ok/ 目录）与 EDA 导出（Gerber_*.Gxx）的层名映射
ALIASES = {
    "tsilk": ["gerber_topsilkscreenlayer.gto", "gto", "to", "f.silks"],
    "bsilk": ["gerber_bottomsilkscreenlayer.gbo", "gbo", "bo", "b.silks"],
    "tsmask": ["gerber_topsoldermasklayer.gts", "gts", "ts", "f.mask"],
    "bsmask": ["gerber_bottomsoldermasklayer.gbs", "gbs", "bs", "b.mask"],
    "tcu": ["gerber_toplayer.gtl", "gtl", "tl", "f.cu"],
    "bcu": ["gerber_bottomlayer.gbl", "gbl", "bl", "b.cu"],
    "outline": ["gerber_boardoutlinelayer.gko", "gko", "ko", "gm1"],
}


def discover(root_files: dict[str, bytes]) -> dict[str, bytes]:
    """按文件名把各层挑出来。"""
    found: dict[str, bytes] = {}
    for path, data in root_files.items():
        base = os.path.basename(path).lower()
        for key, names in ALIASES.items():
            if key in found:
                continue
            if base in names:
                found[key] = data
                break
    return found


def read_zip(path: str) -> dict[str, bytes]:
    """读 zip；若含 ok/ 子目录（立创生产稿），优先取 ok/ 下的文件。"""
    z = zipfile.ZipFile(path)
    names = z.namelist()
    ok = [n for n in names if n.lower().startswith("ok/") and not n.endswith("/")]
    pick = ok if ok else [n for n in names if not n.endswith("/")]
    return {n: z.read(n) for n in pick}


def load_layers(files: dict[str, bytes], label: str, cache_key: str = "") -> dict:
    import hashlib
    import pickle

    cache_dir = os.environ.get("PCBCAM_CACHE") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".cache"
    )
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except Exception:
        import tempfile

        cache_dir = tempfile.mkdtemp(prefix="pcbcam-cache-")
    cpath = ""
    if cache_key:
        h = hashlib.md5(
            (cache_key + str(len(files)) + str(sum(len(v) for v in files.values()))).encode()
        ).hexdigest()[:12]
        cpath = os.path.join(cache_dir, f"{label}_{h}.pkl")
        if os.path.exists(cpath):
            try:
                out = pickle.load(open(cpath, "rb"))
                print(f"  [{label}] 命中缓存 {os.path.basename(cpath)}")
                return out
            except Exception:
                pass

    out = {}
    for key, data in discover(files).items():
        t = time.time()
        layer = GerberLayer(data=data.decode("utf-8", "replace"))
        out[key] = layer.to_shapely()
        print(f"  [{label}] {key:8s} {time.time()-t:5.1f}s  area={out[key].area:9.1f} mm2")
    if cpath:
        try:
            pickle.dump(out, open(cpath, "wb"))
        except Exception:
            pass
    return out



# ---------------------------------------------------------------- 归一化


def canonicalize(layers: dict) -> tuple[dict, tuple, Polygon, tuple]:
    """
    把坐标统一到「板框左下角为原点、Y 向上」的坐标系。
    EDA 导出的 Y 向下（板框在 y 负半轴），立创生产稿 Y 向上，
    这里统一按板框 bbox 平移即可对齐（已实测两套文件重合度 >97%）。
    返回 (平移后的图层, 画布范围, 板面多边形, 平移量)。
    """
    from shapely import affinity

    ref = layers.get("outline")
    if ref is None:
        allg = unary_union([g for g in layers.values() if not g.is_empty])
        bounds = allg.bounds
    else:
        bounds = ref.bounds
    x0, y0, x1, y1 = bounds

    # 板框层是「描边」而不是填充，bbox 会比实际板边大半个线宽。
    # 用 area/length 反推线宽再内缩，坐标才能和 EDA 里的板边对齐。
    if ref is not None and ref.length > 0:
        hw = ref.area / ref.length
        if 0 < hw < 0.6:
            x0, y0, x1, y1 = x0 + hw, y0 + hw, x1 - hw, y1 - hw

    moved = {}
    for k, v in layers.items():
        g = affinity.translate(v, -x0, -y0)
        # 只简化参与布尔运算的丝印/阻焊层；铜层与板框保持原样
        if k.endswith("silk") or k.endswith("smask"):
            g = g.simplify(0.008, preserve_topology=True)
        moved[k] = g

    board = box(0, 0, x1 - x0, y1 - y0)
    if ref is not None:
        hull = affinity.translate(ref.convex_hull, -x0, -y0)
        hw2 = ref.area / ref.length if ref.length > 0 else 0.0
        if 0 < hw2 < 0.6:
            shrunk = hull.buffer(-hw2)
            if not shrunk.is_empty:
                hull = shrunk
        if hull.area > 0.4 * board.area:
            board = hull
    return moved, (0.0, 0.0, x1 - x0, y1 - y0), board, (x0, y0)


# ---------------------------------------------------------------- CAM 规则


@dataclass
class CamConfig:
    silk_clearance: float = 0.0   # 丝印与开窗的安全间距（实测 0 最贴合）
    mask_expand: float = 0.0      # 阻焊开窗单边补偿
    min_silk_width: float = 0.15  # 字符最小线宽（仅告警，不改几何）
    min_silk_height: float = 1.0

    def mask_final(self, mask):
        if self.mask_expand <= 0:
            return mask
        return mask.buffer(self.mask_expand, resolution=6, join_style=1)

    def silk_final(self, silk, mask):
        """嘉立创规则：丝印落在阻焊开窗（露铜/露基材）范围内会被删除。"""
        m = self.mask_final(mask)
        if self.silk_clearance > 0:
            m = m.buffer(self.silk_clearance, resolution=6, join_style=1)
        return silk.difference(m)


def analyze_silk_loss(silk, silk_final, min_area=0.05, top_n=60):
    """列出被裁掉的丝印区块，按面积排序，给出坐标便于回 EDA 修正。"""
    lost = silk.difference(silk_final)
    if lost.is_empty:
        return lost, []
    geoms = list(lost.geoms) if hasattr(lost, "geoms") else [lost]
    rows = []
    for g in geoms:
        try:
            if g.area < min_area:
                continue
            c = g.centroid
            bx0, by0, bx1, by1 = g.bounds
            rows.append(
                dict(
                    area=round(g.area, 3),
                    cx=round(c.x, 2),
                    cy=round(c.y, 2),
                    w=round(bx1 - bx0, 2),
                    h=round(by1 - by0, 2),
                )
            )
        except Exception:
            continue
    rows.sort(key=lambda r: -r["area"])
    return lost, rows[:top_n]


# ---------------------------------------------------------------- 渲染


def render_views(layers, board, bounds, cfg, style, width, cutouts=None,
                 annotate=True, silk_f_map=None):
    tr = Transform(bounds, width, margin_mm=1.0)
    cut = cutouts or []
    out = {}
    for side, name in (("t", "top"), ("b", "bottom")):
        silk = layers[f"{side}silk"]
        mask = layers[f"{side}smask"]
        cu = layers[f"{side}cu"]
        silk_f = (silk_f_map or {}).get(side) or cfg.silk_final(silk, mask)

        asd = composite(tr, mask, cu, silk, board, style, cutouts=cut)
        prd = composite(tr, cfg.mask_final(mask), cu, silk_f, board, style, cutouts=cut)
        out[f"asdesigned_{name}"] = asd
        out[f"predicted_{name}"] = prd            # 干净版：用于对比
        out[f"silkloss_{name}"] = _loss_image(tr, silk.difference(silk_f), style)
        if annotate and cut:
            out[f"marked_{name}"] = annotate_cutouts(prd, tr, cut)
    return tr, out


def _loss_image(tr, lost, style):
    st = STYLES[style]
    img = np.zeros((tr.H, tr.W, 4), np.uint8)
    img[:, :] = (*st["mask"], 255)
    m = _mask(lost, tr) > 0
    img[m] = (226, 75, 74, 255)
    return Image.fromarray(img, mode="RGBA")


# ---------------------------------------------------------------- 报告


def b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def build_report(path, args, stats, views, tr, rows_t, rows_b, validation, notes, cutouts=None):
    def tile(key, cap):
        return (
            f'<figure><img src="{b64(views[key])}" alt="{cap}">'
            f"<figcaption>{cap}</figcaption></figure>"
        )

    def table(rows, side):
        if not rows:
            return "<p class='ok'>此面没有可检出的丝印损失。</p>"
        body = "".join(
            f"<tr><td>{i+1}</td><td>{r['area']:.2f}</td><td>({r['cx']:.1f}, {r['cy']:.1f})</td>"
            f"<td>{r['w']:.1f} × {r['h']:.1f}</td></tr>"
            for i, r in enumerate(rows)
        )
        return (
            "<table><thead><tr><th>#</th><th>被裁面积 mm²</th><th>中心坐标 mm</th>"
            f"<th>外接尺寸 mm</th></tr></thead><tbody>{body}</tbody></table>"
        )

    cutouts = cutouts or []
    if cutouts:
        body = "".join(
            f"<tr><td>{i+1}</td><td>⌀{c['d']:.2f}</td>"
            f"<td>({c['x']:.2f}, {c['y']:.2f})</td><td>{c['area']:.2f}</td></tr>"
            for i, c in enumerate(cutouts)
        )
        cutout_html = (
            f"<p class='sub'>共 {len(cutouts)} 处，已在预测图上用红色虚线圈标注。</p>"
            "<table><thead><tr><th>#</th><th>孔径</th><th>中心坐标 mm</th>"
            f"<th>面积 mm²</th></tr></thead><tbody>{body}</tbody></table>"
        )
    else:
        cutout_html = "<p class='ok'>未检出挖孔 / 挖槽。</p>"

    mark_html = ""
    if cutouts and "marked_top" in views:
        mark_html = (
            '<div class="grid2" style="margin-top:14px">'
            + tile("marked_top", "顶面 · 挖孔标注")
            + tile("marked_bottom", "底面 · 挖孔标注")
            + "</div>"
        )

    val_html = ""
    if validation:
        val_html = f"""
    <section>
      <h2>自检：与立创实际生产稿的吻合度</h2>
      <div class="kpis">
        <div><b>{validation['pixel_agree']:.2f}%</b><span>成品渲染逐像素一致</span></div>
        <div><b>{validation['iou_top']*100:.1f}%</b><span>顶面丝印几何 IoU</span></div>
        <div><b>{validation['iou_bot']*100:.1f}%</b><span>底面丝印几何 IoU</span></div>
      </div>
      <div class="grid2">
        {tile('predicted_top', '工具预测 · 顶面')}
        {tile('actual_top', '立创生产稿 · 顶面') if 'actual_top' in views else ''}
      </div>
    </section>"""

    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>PCB 成品预演报告</title>
<style>
 body{{margin:0;background:#111214;color:#e8e8e6;font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}
 .wrap{{max-width:1180px;margin:0 auto;padding:40px 28px 80px}}
 h1{{font-size:22px;font-weight:500;margin:0 0 6px}}
 h2{{font-size:16px;font-weight:500;margin:36px 0 14px;padding-bottom:8px;border-bottom:1px solid #2a2c30}}
 .sub{{color:#9a9c9f;font-size:13px;margin-bottom:28px}}
 .kpis{{display:flex;gap:14px;flex-wrap:wrap;margin:16px 0 22px}}
 .kpis>div{{flex:1;min-width:150px;background:#1a1c1f;border:1px solid #2a2c30;border-radius:10px;padding:14px 16px}}
 .kpis b{{display:block;font-size:22px;font-weight:500;color:#7fd1c0}}
 .kpis span{{font-size:12px;color:#9a9c9f}}
 .grid2{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
 figure{{margin:0;background:#1a1c1f;border:1px solid #2a2c30;border-radius:10px;overflow:hidden}}
 figure img{{width:100%;display:block}}
 figcaption{{padding:9px 12px;font-size:12px;color:#9a9c9f;border-top:1px solid #2a2c30}}
 table{{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:10px}}
 th,td{{text-align:left;padding:7px 10px;border-bottom:1px solid #24262a}}
 th{{color:#9a9c9f;font-weight:400}}
 td{{font-variant-numeric:tabular-nums}}
 .ok{{color:#7fd1c0}}
 .warn{{background:#1e1a12;border-left:2px solid #d8a13a;padding:12px 16px;border-radius:6px;color:#e2c894;font-size:13px}}
 .legend{{display:flex;gap:18px;font-size:12px;color:#9a9c9f;margin:10px 0 0}}
 .legend i{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}}
</style></head><body><div class="wrap">
<h1>PCB 成品预演报告</h1>
<div class="sub">{os.path.basename(args.eda)} · 阻焊 {args.mask} / 丝印 {args.silk} / {args.finish} ·
板框 {bounds_w(tr):.1f} × {bounds_h(tr):.1f} mm</div>

<section>
<h2>一句话结论</h2>
<div class="warn">{notes}</div>
<div class="kpis">
  <div><b>{stats['lost_top']:.2f} mm²</b><span>顶面丝印将被裁掉</span></div>
  <div><b>{stats['lost_bot']:.2f} mm²</b><span>底面丝印将被裁掉</span></div>
  <div><b>{stats['lost_top_pct']:.1f}%</b><span>顶面丝印损失比例</span></div>
  <div><b>{len(rows_t)} / {len(rows_b)}</b><span>顶/底明显损失区块</span></div>
</div>
</section>

<section>
<h2>顶面：设计图 vs 实际成品</h2>
<div class="grid2">
  {tile('asdesigned_top', '设计原稿（EDA 看到的）')}
  {tile('predicted_top', '实际成品（板厂处理后）')}
</div>
</section>

<section>
<h2>底面：设计图 vs 实际成品</h2>
<div class="grid2">
  {tile('asdesigned_bottom', '设计原稿（EDA 看到的）')}
  {tile('predicted_bottom', '实际成品（板厂处理后）')}
</div>
</section>

<section>
<h2>会被裁掉的丝印（红色区域）</h2>
<div class="grid2">
  {tile('silkloss_top', '顶面丝印损失')}
  {tile('silkloss_bottom', '底面丝印损失')}
</div>
<div class="legend"><span><i style="background:#e24b4a"></i>将被 CAM 删除</span>
<span><i style="background:{'#f6f6f2' if args.mask=='white' else '#1c1c1e'}"></i>阻焊面</span></div>
</section>

<section>
<h2>顶面损失明细（按面积排序）</h2>
{table(rows_t, 'top')}
</section>

<section>
<h2>底面损失明细</h2>
{table(rows_b, 'bot')}
</section>

<section>
<h2>识别到的挖孔 / 挖槽</h2>
{cutout_html}
{mark_html}
</section>
{val_html}

<section>
<h2>本工具采用的规则</h2>
<table><tbody>
<tr><th>丝印裁剪</th><td>丝印落在阻焊开窗（露铜/露基材）范围内即被删除，裁剪边界 = 开窗边界
（间距 {args.silk_clearance:.2f} mm）</td></tr>
<tr><th>阻焊补偿</th><td>开窗单边外扩 {args.mask_expand:.2f} mm</td></tr>
<tr><th>依据</th><td>嘉立创《字符设计规范》：裸露铜面/喷锡面/镀金面上的字符直接删除；
间距 &lt;0.15mm 的字符删除。参数由本次「原稿 ↔ 生产稿」实测标定。</td></tr>
</tbody></table>
<p class="sub" style="margin-top:14px">规则参数按单次订单反推，不同工艺（彩色丝印、高清字符）、
不同板厂可能不同，可调整参数后重跑。本工具输出为外观预测，不能替代嘉立创
「确认生产稿」环节的正式工程审核。</p>
</section>
</div></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def bounds_w(tr):
    return tr.W / tr.scale


def bounds_h(tr):
    return tr.H / tr.scale


# ---------------------------------------------------------------- main


def main(argv=None):
    ap = argparse.ArgumentParser(description="嘉立创 CAM 规则预演")
    ap.add_argument("--eda", required=True, help="EDA 导出的 Gerber zip")
    ap.add_argument("--jlc", help="立创生产稿 zip（可选，用于自检）")
    ap.add_argument("--out", default="out", help="输出目录")
    ap.add_argument("--mask", default="white", choices=["white", "black", "green"])
    ap.add_argument("--silk", default="black", choices=["black", "white"])
    ap.add_argument("--finish", default="enig", choices=["enig", "hasl"])
    ap.add_argument("--silk-clearance", type=float, default=0.0)
    ap.add_argument("--mask-expand", type=float, default=0.0)
    ap.add_argument("--width", type=int, default=2000)
    ap.add_argument("--cutout-min", type=float, default=1.0,
                    help="孔径 >= 该值( mm )才判定为挖孔，默认 1.0")
    ap.add_argument("--no-annotate", action="store_true", help="不在图上标注挖孔")
    args = ap.parse_args(argv)

    style = (args.mask, args.silk, args.finish)
    if style not in STYLES:
        style = ("white", "black", "enig")

    os.makedirs(args.out, exist_ok=True)
    t0 = time.time()

    print("读取 EDA 导出…")
    files_eda = read_zip(args.eda)
    eda = load_layers(files_eda, "EDA", cache_key=os.path.abspath(args.eda))
    eda, bounds, board, off = canonicalize(eda)

    # 挖孔 / 挖槽：来自钻孔文件（EDA 的 NPTH，或立创 drl 里的大孔）
    holes = load_holes(files_eda)
    cutouts = pick_cutouts(holes, args.cutout_min)
    for c in cutouts:
        c["x"] -= off[0]
        c["y"] -= off[1]
    print(f"  钻孔文件: {holes['files'] or '未找到'}  判定挖孔 {len(cutouts)} 个")

    cfg = CamConfig(silk_clearance=args.silk_clearance, mask_expand=args.mask_expand)

    # 丝印裁剪只算一次，渲染与统计共用
    t0 = time.time()
    silk_f_map, lost_all, rows_all, stats = {}, {}, {}, {}
    names = {"t": "top", "b": "bot"}
    for side in ("t", "b"):
        silk = eda[f"{side}silk"]
        sf = cfg.silk_final(silk, eda[f"{side}smask"])
        silk_f_map[side] = sf
        lost, rows = analyze_silk_loss(silk, sf)
        lost_all[side] = lost
        rows_all[side] = rows
        stats[f"lost_{names[side]}"] = lost.area
        stats[f"lost_{names[side]}_pct"] = (
            lost.area / silk.area * 100 if silk.area > 0 else 0.0
        )
    print(f"  丝印裁剪计算 {time.time()-t0:.1f}s")

    tr, views = render_views(
        eda, board, bounds, cfg, style, args.width,
        cutouts=cutouts, annotate=not args.no_annotate,
        silk_f_map=silk_f_map,
    )

    validation = None
    if args.jlc:
        print("读取立创生产稿…")
        files_jlc = read_zip(args.jlc)
        jlc = load_layers(files_jlc, "JLC", cache_key=os.path.abspath(args.jlc))
        jlc, _, _, off_j = canonicalize(jlc)
        holes_j = load_holes(files_jlc)
        cut_j = pick_cutouts(holes_j, args.cutout_min)
        for c in cut_j:
            c["x"] -= off_j[0]
            c["y"] -= off_j[1]

        def iou(a, b):
            u = a.union(b).area
            return a.intersection(b).area / u if u > 0 else 0.0

        iou_t = iou(cfg.silk_final(eda["tsilk"], eda["tsmask"]), jlc["tsilk"])
        iou_b = iou(cfg.silk_final(eda["bsilk"], eda["bsmask"]), jlc["bsilk"])

        for side, name in (("t", "top"), ("b", "bottom")):
            views[f"actual_{name}"] = composite(
                tr,
                jlc[f"{side}smask"],
                jlc[f"{side}cu"],
                jlc[f"{side}silk"],
                board,
                style,
                cutouts=cut_j,
            )
        a = np.asarray(views["predicted_top"].convert("RGB")).astype(int)
        b = np.asarray(views["actual_top"].convert("RGB")).astype(int)
        # 允许 ±4px 的整像素配准误差（两份 Gerber 的原点约定不同）
        best = (0.0, 0, 0)
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                aa = a[max(0, dy): a.shape[0] + min(0, dy), max(0, dx): a.shape[1] + min(0, dx)]
                bb = b[max(0, -dy): b.shape[0] - max(0, dy), max(0, -dx): b.shape[1] - max(0, dx)]
                r = (np.abs(aa - bb).sum(2) <= 30).mean() * 100
                if r > best[0]:
                    best = (r, dx, dy)
        agree = best[0]
        validation = dict(
            pixel_agree=agree, iou_top=iou_t, iou_bot=iou_b,
            shift=(best[1], best[2]),
        )

    stats["cutouts"] = cutouts
    notes = (
        f"顶面有 {stats['lost_top']:.2f} mm²（{stats['lost_top_pct']:.1f}%）的丝印会被板厂删除，"
        f"集中为 {len(rows_all['t'])} 处；底面 {stats['lost_bot']:.2f} mm²（{len(rows_all['b'])} 处）。"
        " 这些位置在 EDA 的 2D/3D 预览里都正常，只有成品才会暴露。"
    )
    if len(rows_all["t"]) == 0 and len(rows_all["b"]) == 0:
        notes = "未检出会被裁剪的丝印：所有丝印都落在阻焊油墨上，可以放心下单。"

    build_report(
        os.path.join(args.out, "report.html"),
        args, stats, views, tr, rows_all["t"], rows_all["b"], validation, notes,
        cutouts=cutouts,
    )
    for k in ("asdesigned_top", "predicted_top", "asdesigned_bottom",
              "predicted_bottom", "silkloss_top", "silkloss_bottom", "actual_top",
              "actual_bottom", "marked_top", "marked_bottom"):
        if k in views:
            views[k].save(os.path.join(args.out, f"{k}.png"))

    json.dump(
        dict(
            stats=stats,
            loss_top=rows_all["t"],
            loss_bottom=rows_all["b"],
            validation=validation,
            cfg=vars(cfg),
        ),
        open(os.path.join(args.out, "stats.json"), "w", encoding="utf-8"),
        ensure_ascii=False, indent=2,
    )
    print(f"\n完成，用时 {time.time()-t0:.1f}s -> {args.out}/report.html")
    print(notes)
    if validation:
        print(f"自检：顶面丝印 IoU {validation['iou_top']*100:.1f}%  "
              f"成品像素一致 {validation['pixel_agree']:.2f}%")


if __name__ == "__main__":
    main()
