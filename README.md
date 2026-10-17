# PCB 成品预演（嘉立创 CAM 规则）

下单前用**板厂视角**看成品：哪些丝印会被阻焊开窗裁掉、挖孔在哪、开窗露出什么颜色。

解决的问题很具体：嘉立创 EDA 的 2D/3D 预览会让丝印盖在焊盘开窗上照常显示，
但实际生产时 CAM 会把落在露铜/露基材区域的丝印删掉。这个差异只有拿到生产稿
才会暴露，而此时板子已经投产。

> 本工具不替代嘉立创官方的「确认生产稿」流程，而是把它的结果**提前**摆到你面前。

## 功能

- **成品外观预测**：按阻焊色 / 丝印色 / 表面处理合成接近实物的效果图
- **丝印损失定位**：列出会被裁掉的区域（面积、中心坐标、外接尺寸），可直接回 EDA 修改
- **挖孔识别与标注**：从钻孔文件读 NPTH，渲染成真实孔洞，图上画圈标孔径
- **自检**：上传立创生产稿，自动给出预测与真实生产稿的吻合度
- **网页版**：拖 zip 即用，拖动分割线对比「设计原稿 ↔ 实际成品」

## 快速开始

### 方式一：直接用 exe（Windows）

下载 `PCBCamPreview.exe`，双击运行，浏览器会自动打开页面；关闭命令行窗口即退出。

### 方式二：从源码运行

```bash
pip install -r requirements.txt

python tool/webapp.py            # 网页版 → http://127.0.0.1:8777
python tool/cam_preview.py --eda 下单Gerber.zip --out out/   # 命令行版
```

网页版：拖入 EDA 导出的 zip（必填）、立创生产稿 zip（可选，用于自检），点「开始预演」。

### 命令行参数

| 参数 | 说明 | 默认 |
|---|---|---|
| `--eda` | 嘉立创EDA「文件 → 导出 → PCB制板文件(Gerber)」生成的 zip | 必填 |
| `--jlc` | 立创生产稿 zip（含 `ok/`），用于自检评分 | 可选 |
| `--out` | 输出目录 | `out` |
| `--mask` | 阻焊色 `white` / `black` / `green` | `white` |
| `--silk` | 丝印色 `black` / `white` | `black` |
| `--finish` | 表面处理 `enig`（沉金）/ `hasl`（喷锡） | `enig` |
| `--cutout-min` | 孔径 ≥ 该值才算挖孔（mm） | `1.0` |
| `--no-annotate` | 不输出带标注的图 | 关闭 |
| `--silk-clearance` | 丝印距开窗的安全间距（mm） | `0.0` |
| `--mask-expand` | 阻焊开窗单边补偿（mm） | `0.0` |
| `--width` | 渲染宽度（px） | `2000` |

### 输出

```
out/
  report.html            可视化报告（自包含，可直接分享）
  asdesigned_top.png     设计原稿（EDA 看到的）
  predicted_top.png      实际成品（干净版）
  marked_top.png         实际成品 + 挖孔标注
  silkloss_top.png       会被裁掉的丝印（红色）
  actual_top.png         立创生产稿渲染（提供 --jlc 时）
  stats.json             机读数据
```

## 挖孔 / 挖槽

从**钻孔文件**读，不靠猜：

- EDA 导出：`Drill_NPTH_Through.DRL`（Excellon，带 `;TYPE=NON_PLATED` 标记）
- 立创生产稿：`ok/drl`（本质是带 `D03` 闪光点的 Gerber）

判定：NPTH 优先；无 NPTH 标记时按孔径阈值（默认 ≥1mm 是机械挖孔，0.3mm 级是过孔）。
渲染时板面在孔位掏空，标注图上画虚线圈 + 标签（孔径 / 编号）。

> 真实案例：某艺术板上一个看似「金色圆盘」的图形，实际是 **⌀6mm 非金属化孔**
> 外套一圈 8mm 阻焊开窗，成品是**金环 + 中心黑洞**。EDA 预览把它画成实心圆，
> 直接导致误判。

## 规则怎么来的

规则不是照抄文档，而是用一对真实的「原稿 Gerber ↔ 立创生产稿」反推标定的。

1. **丝印裁剪**：丝印落在阻焊开窗范围内即被删除，裁剪边界 ≈ 开窗边界。
   实测 `--silk-clearance 0` 拟合最好（顶面丝印 IoU 99.4%）。
   > 嘉立创官方口径是「字符距焊盘 <0.15mm 删除」，但实测**图像类丝印**走的是
   > 「按开窗边界裁掉」，没有 0.15mm 缓冲。两者不要混用。
2. **阻焊补偿**：实测未做全局外扩，默认 `--mask-expand 0`。
3. **板边开窗**：立创会额外在板边加一圈约 0.15mm 阻焊开窗环，并把板边丝印刮掉。

来源：嘉立创《技术指导：字符设计规范》《阻焊基本设计》，观察时间 2026-10。

## 精度自检

用一块 85 × 53.8 mm、白阻焊 + 黑丝印 + 沉金、含 1 个 ⌀6mm 挖孔的 2 层板验证
（原稿 → 生产稿为同一订单的真实一对）：

| 指标 | 结果 |
|---|---|
| 顶面丝印几何 IoU | **99.4%** |
| 成品图逐像素一致（±1px 配准） | **99.0%** |
| 顶面丝印损失预测 | 81.93 mm²（2.5%，47 处） |
| 实际损失 | 88.67 mm² |

残差主要来自：立创会把丝印多边形整体重绘（节点不同）、笔画宽度有微小差异。

> 踩过的坑：板框层是**描边**而非填充，bbox 会比真实板边大半个线宽。早期没修正
> 时整体偏移 0.13mm，IoU 只有 93.3%。工具用 `area/length` 反推线宽并内缩，
> 挖孔坐标与 EDA 的误差 <0.01mm。

## 已知限制

- 只做**外观预测**，不替代嘉立创「确认生产稿」的正式工程审核。
- 规则参数按单次订单反推，**彩色丝印 / 高清字符 / 高精字符**走不同产线，未覆盖。
- 未模拟：阻焊桥、塞孔、沉金厚度色差、字符线宽不足导致的模糊、外形铣切公差。
- 大板 + 高精度渲染较慢（85×54mm 约 15s 纯预测 / 80s 含自检）。

## 目录结构

```
├─ tool/
│  ├─ gerber_engine.py   RS-274X 解析 → shapely 几何（纯自研，无第三方 Gerber 库）
│  ├─ holes.py           Excellon / Gerber 双格式钻孔解析，挖孔判定
│  ├─ render.py          栅格化 + 按工艺分层合成 + 挖孔标注
│  ├─ cam_preview.py     规则引擎 + CLI + HTML 报告
│  ├─ webapp.py          本地网页服务（标准库 http.server，无 Flask）
│  ├─ launcher.py        打包入口（自动选端口 + 开浏览器）
│  ├─ build_exe.py       一键打包 exe
│  └─ web/index.html     前端页面
├─ requirements.txt
├─ requirements-dev.txt
└─ .gitignore
```

运行时产物写在 `out/`（打包后写到 `%LOCALAPPDATA%\PCBCamPreview`），几何缓存写在
`tool/.cache/`，都会被 git 忽略。

## 打包成 exe

```bash
pip install -r requirements-dev.txt
python tool/build_exe.py
# 产物: dist/PCBCamPreview.exe
```

原理：PyInstaller `--onefile` 打包 `launcher.py`，用 `--add-data` 把
`tool/web` 塞进临时解包目录（运行时由 `sys._MEIPASS` 定位）。打包后没有独立
Python 解释器，`webapp` 会自动把子进程调用切换为进程内调用。

exe 体积较大（含 shapely/numpy/Pillow），已在 `.gitignore` 中排除 `dist/`，
建议作为 GitHub Release 附件分发而不是提交进仓库。

## 免责

输出为外观预测，不能替代厂家的工程审核。工艺参数换产线/换工艺后需重新标定。
