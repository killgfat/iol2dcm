# -*- coding: utf-8 -*-
"""报告单渲染：A4 竖版 200dpi，2×2 公式格，与设备原厂报告单同版式。

设计原则
--------
* **缺项必须显示，不能省略**：ACD 没测出来就写「ACD: 测不出」。
  报告的完整性本身就是临床信息，行消失会被误读成「这项没做」。
* 全角括号（）在部分 PACS 渲染器里会变豆腐块；报告里的**数值行一律用
  ASCII**，中文只出现在标题/标签。
* 四公式按原表 **2×2 布局**：上左 SRK/T、上右 Haigis、下左 Hoffer Q、
  下右 Holladay 1，每眼一块，比竖排省一半高度。
* 字体自适应：不同发行版自带的中文字体不同（思源黑体 / Noto CJK），
  必须运行时探测，不能假定某一条路径存在。
"""
from __future__ import annotations

import datetime
import os
import re

from core.utils import KH, is_cjk, log

NA_TEXT = "测不出"


def _fmt_name(family, given):
    """报告单上的姓名：中文全名直接连写，拼音/英文空格分隔。

    与 dicom 的 PatientName 同一套规则，但报告单是给人看的，
    不需要考虑 DICOM PN 的保留分隔符转义。
    """
    fam = (family or "").strip()
    giv = (given or "").strip()
    if not fam and not giv:
        return ""
    if is_cjk(fam) or is_cjk(giv):
        return f"{fam}{giv}"
    return " ".join(p for p in (fam, giv) if p)



# 报告单上印的公式名
# 一律按 IOLCon 的写法：SRK/T · Haigis · Hoffer Q · Holladay 1
# 这是常数表的原生口径，报告单、XML、state 全链路统一，
# 不使用 SRK(R)/T / HofferQ / Holladay 这些别名。
FORMULA_NAMES = ["SRK/T", "Haigis", "Hoffer Q", "Holladay 1"]


def parse_const(s):
    """解析常量串。两处陷阱都必须处理：
    1) `;` 分组内部还用空格分隔多个键值对：
       `ACD:5.09 A0:1.460 A1:0.400 A2:0.100;pACD:5.68;SF:1.91;...`
       —— 只按 `;` split 会把 A0/A1/A2 整组吞掉。
    2) 键本身可能含空格：`A(SRK II):119.60`
       —— 先按空格 split 会把它撕成两半，所以只能用正则整串扫描。
    """
    s = s or ""
    out = {}
    for m in re.finditer(r'A\(([^)]+)\)\s*:\s*([-\d.]+)', s):
        out[m.group(1).strip()] = float(m.group(2))
    # 去掉已在 A(...) 里的片段，再扫普通 KEY:VALUE
    rest = re.sub(r'A\([^)]+\)\s*:\s*[-\d.]+', ' ', s)
    for m in re.finditer(r'(?<![A-Za-z0-9_])([A-Za-z][A-Za-z0-9]*)\s*:\s*([-\d.]+)', rest):
        out[m.group(1)] = float(m.group(2))
    return out


def const_lines(cst, formulas):
    """每格底部的常数行。符号按 IOLCon 写法：A / a0 a1 a2 / pACD / SF。

    ★ parse_const 把 `A(SRK/T):119.41` 的键存成 "SRK/T"（去掉 A(...) 外壳），
      所以 SRK/T 取 cst["SRK/T"]。
    """
    out = {}
    if "SRK/T" in formulas:
        a = cst.get("SRK/T")
        out["SRK/T"] = f"A: {a:.2f}" if a else ""
    if "Haigis" in formulas:
        if all(k in cst for k in ("A0", "A1", "A2")):
            out["Haigis"] = (f"a0: {cst['A0']:.3f}  a1: {cst['A1']:.3f}  "
                             f"a2: {cst['A2']:.3f}")
        else:
            out["Haigis"] = ""
    if "Hoffer Q" in formulas:
        out["Hoffer Q"] = f"pACD: {cst['pACD']:.2f}" if "pACD" in cst else ""
    if "Holladay 1" in formulas:
        out["Holladay 1"] = f"SF: {cst['SF']:.2f}" if "SF" in cst else ""
    return out


# ============================================================ 渲染
# ---- 字体探测（不同发行版字体不同，必须运行时自适应）----
_FONT_CACHE = {}


def _pick_font(cands):
    """按顺序返回第一个存在且 PIL 真能加载的字体，元素为 (path,) 或 (path, index)。

    ★ .ttc 是字体集合：Noto Sans CJK 里 index=2 才是 SC（简体），
      index=0 是 JP —— 拿错字面会把「直」「骨」这类字形写成日式。
    """
    from PIL import ImageFont
    for c in cands:
        p, idx = (c, 0) if isinstance(c, str) else c
        if not os.path.exists(p):
            continue
        try:
            ImageFont.truetype(p, 20, index=idx)
            return (p, idx)
        except Exception:
            continue
    return None


def resolve_fonts():
    """探测 CJK 正/粗 与等宽正/粗；结果进程内缓存。"""
    if _FONT_CACHE:
        return _FONT_CACHE
    han = "/usr/share/fonts/adobe-source-han-sans/"
    noto = "/usr/share/fonts/opentype/noto/"
    _FONT_CACHE["cjk"] = _pick_font([
        han + "SourceHanSansCN-Regular.otf",
        (noto + "NotoSansCJK-Regular.ttc", 2),      # index 2 = SC
        (noto + "NotoSansCJK-Regular.ttc", 0),
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    ])
    _FONT_CACHE["cjk_bold"] = _pick_font([
        han + "SourceHanSansCN-Bold.otf",
        (noto + "NotoSansCJK-Bold.ttc", 2),
        (noto + "NotoSansCJK-Bold.ttc", 0),
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    ])
    _FONT_CACHE["mono"] = _pick_font([
        "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
    ])
    _FONT_CACHE["mono_bold"] = _pick_font([
        "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    ])
    # 粗体缺失时退回常规，常规也缺就硬报错（比静默画方块强）
    for k in ("cjk", "cjk_bold", "mono", "mono_bold"):
        if _FONT_CACHE[k] is None and k.endswith("bold"):
            _FONT_CACHE[k] = _FONT_CACHE[k[:-5]]
    missing = [k for k in ("cjk", "mono") if _FONT_CACHE[k] is None]
    if missing:
        raise RuntimeError(
            f"缺少可用字体: {missing}。CJK 需 adobe-source-han-sans 或 Noto Sans CJK；"
            "等宽需 Liberation/DejaVu。安装示例: apt install fonts-noto-cjk fonts-liberation")
    return _FONT_CACHE


def render(r, out_png, dpi=200, axes=None, institution=""):
    """渲染一张 A4 报告单。

    axes         {"R": 轴位, "L": 轴位}（来自 MDB），缺省则不显示 @deg
    institution  页眉机构名。**不硬编码** —— 由 config.yaml 的 report.institution
                 注入，避免把具体医院名写进代码（开源/多院部署都不合适）。
                 为空时页眉留白，不印任何默认院名。
    """
    from PIL import Image, ImageDraw, ImageFont

    FONTS = resolve_fonts()
    W, H = int(8.27 * dpi), int(11.69 * dpi)
    M = int(0.28 * dpi)

    axes = axes or {}
    formulas = r["formulas"] if len(r["formulas"]) == 4 else FORMULA_NAMES
    cst = parse_const(r["const_str"])
    clines = const_lines(cst, formulas)
    try:
        tgt = float(r["target_ref"]) if r["target_ref"] else 0.0
    except ValueError:
        tgt = 0.0

    CJK = re.compile(r'[\u4e00-\u9fff]')
    state = {}

    def draw_all(k):
        img = Image.new("RGB", (W, H), "white")
        dr = ImageDraw.Draw(img)
        dh = dpi / 72.0

        def F(key, pt):
            p, idx = FONTS[key]
            return ImageFont.truetype(p, max(5, int(pt * dh * k)), index=idx)

        # ---- 分区字号与行距 ----
        # 不同区域用途不同：标题要醒目、参数区要宽松好读、表格区要密（9 行×4 公式×2 眼）。
        # 全部套同一个行距系数会两头受罪：参数区太挤，表格区又浪费高度。
        PT_BODY = 9.2      # 患者信息 / 参数标签
        PT_TINY = 7.6      # 常数行、表头
        PT_NUM = 9.0       # 参数数值
        PT_CELL = 8.0      # 表格数值
        PT_MID = 10.8      # OD/OS 标题
        PT_BIG = 13.5      # 医院名

        js = F("cjk", PT_BODY)
        jb = F("cjk_bold", PT_BODY)
        tiny = F("cjk", PT_TINY)
        mons = F("mono", PT_NUM)
        mb = F("mono_bold", PT_NUM)
        smon = F("mono", PT_CELL)
        smonb = F("mono_bold", PT_CELL)
        mid = F("cjk_bold", PT_MID)
        big = F("cjk_bold", PT_BIG)

        # 行距系数按区域分开：1.0 表示紧贴字号，越大越松
        LH = max(7, int(PT_BODY * dh * k * 1.62))    # 参数/信息区：宽松
        LH_CELL = max(6, int(PT_CELL * dh * k * 1.66))  # 表格数值行：仍留足字间空隙
        GAP_TITLE = int(LH * 0.30)                   # 公式标题与常数行之间

        def T(xy, s, font, anchor="la", fill="black"):
            dr.text(xy, s, font=font, fill=fill, anchor=anchor)

        def TV(x, y, s, zh_font, en_font):
            """有中文才用中文字体；纯 ASCII 走拉丁字体，规避全角符号豆腐块"""
            T((x, y), s, zh_font if CJK.search(s) else en_font)

        def hl(y, x0=None, x1=None):
            dr.line([(x0 if x0 is not None else M, y),
                     (x1 if x1 is not None else W - M, y)], fill="black", width=1)

        y = M

        # ---- 页眉 ----
        if institution:
            T((M, y), institution, big)
        by1 = y + int(0.30 * dpi)
        bx0 = W - M - int(0.90 * dpi)
        dr.rectangle([bx0, y, W - M, by1], fill="black")
        T(((bx0 + W - M) // 2, (y + by1) // 2), "ZEISS", mid, anchor="mm", fill="white")
        y = by1 + int(0.05 * dpi)
        T((M, y), "IOLMaster 人工晶状体度数计算报告单", js)
        y += LH

        # ---- 患者信息 ----
        top = y

        def _d(s):
            return (f"{s[:4]}-{s[4:6]}-{s[6:8]}"
                    if s and len(s) == 8 and s.isdigit() else (s or ""))

        rows = [("姓名", _fmt_name(r["family"], r["given"])),
                ("ID", ""),
                ("出生日期", _d(r["dob"])),
                ("检查日期", _d(r["exam_date"])),
                ("眼科手术医生", r["operator"] or ""),
                ("晶状体", r["lens"] or "")]
        lx = M + int(0.10 * dpi)
        vx = lx + int(1.05 * dpi)
        rx = M + int(3.45 * dpi)
        yy = top + int(0.04 * dpi)
        for label, val in rows:
            T((lx, yy), f"{label}:", js)
            TV(vx, yy, val, jb, mb)
            yy += LH
        T((rx, top + int(0.04 * dpi)), f"目标屈光度: {r['target_ref'] or ''} D", jb)
        T((rx, top + int(0.04 * dpi) + LH), "折射率 n: 1.3375", js)
        if r.get("mode"):
            T((rx, top + int(0.04 * dpi) + 2 * LH), f"计算模式: {r['mode']}", js)
        y = yy
        dr.rectangle([M, top, W - M, y], outline="black", width=1)

        # ---- 警示行 ----
        T((lx, y + int(0.03 * dpi)),
          "对 AL 数值要检查合理性，因为可能存在病理性变化。", tiny)
        y += LH
        dr.rectangle([M, y - LH + int(0.03 * dpi), W - M, y], outline="black", width=1)

        # ---- 双眼参数 ----
        colw = (W - 2 * M) // 2
        colx = {"OD": M, "OS": M + colw}
        y += int(0.04 * dpi)
        hrow = y
        T((colx["OD"] + int(0.10 * dpi), hrow), "OD (右眼)", mid)
        T((colx["OS"] + int(0.10 * dpi), hrow), "OS (左眼)", mid)
        y = hrow + int(0.24 * dpi)
        meas_top = y

        bottoms = []
        for eye, key in (("OD", "R"), ("OS", "L")):
            cx = colx[eye]
            e = r[key]
            ax = axes.get(key)
            k1 = KH / e["r1"] if e["r1"] else None
            k2 = KH / e["r2"] if e["r2"] else None

            # ★ 缺项也保留行，值写「测不出」，不让整行消失
            # ★ 轴位归属（用脱敏验证数据核对）：
            #   MDB 的 axis_r/axis_l 是 **K1 的轴**；K2 的轴恒为 K1 轴 +90°（主子午线互相垂直）；
            #   Cyl 的轴与 K1 同轴。原版 Zeiss 报告单 OD K1@64°/K2@154°、OS K1@116°/K2@26° 均吻合。
            def _perp(a):
                try:
                    return int((int(a) + 90) % 180)
                except (TypeError, ValueError):
                    return None

            ax2 = _perp(ax) if ax else None

            lines = [("AL", f"{e['al']:.2f} mm" if e["al"] else NA_TEXT)]
            if k1:
                s = f"{k1:.2f} D / {e['r1']:.2f} mm"
                if ax:
                    s += f" @ {ax}\u00b0"
                lines.append(("K1", s))
            else:
                lines.append(("K1", NA_TEXT))
            if k2:
                s = f"{k2:.2f} D / {e['r2']:.2f} mm"
                if ax2 is not None:
                    s += f" @ {ax2}\u00b0"
                lines.append(("K2", s))
            else:
                lines.append(("K2", NA_TEXT))
            if k1 and k2:
                s = f"{k1 - k2:+.2f} D"          # Zeiss 约定 Cyl = K1 - K2
                if ax:
                    s += f" @ {ax}\u00b0"
                lines.append(("Cyl.", s))
            else:
                lines.append(("Cyl.", NA_TEXT))
            lines.append(("ACD", f"{e['acd']:.2f} mm" if e["acd"] else NA_TEXT))
            # ★ 眼状态是**录入项**，不是测量项 —— 空就写 "None"，不能写「测不出」。
            #   「测不出」用于 AL/K/ACD 这类仪器测不出来的场景；
            #   眼状态是操作员没填，语义完全不同，混用会误导阅片者。
            lines.append(("眼状态", e.get("state") or "None"))

            yy = meas_top
            for name, val in lines:
                T((cx + int(0.12 * dpi), yy), f"{name}:", js)
                TV(cx + int(0.90 * dpi), yy, val, js, mons)
                yy += LH
            bottoms.append(yy)

        y = max(bottoms) + int(0.03 * dpi)
        hl(y)
        y += int(0.06 * dpi)

        # ---- 四公式：每眼 2×2，左右两眼**同步并排**推进 ----
        # (行, 列) -> 公式序号：上左 SRK/T、上右 Haigis、下左 Hoffer Q、下右 Holladay 1
        # ★ 必须按行同步：两眼各画自己的整块会让右眼整体下移一整块高度（实测低 417px）。
        order = [(0, 0), (0, 1), (1, 0), (1, 1)]
        subw = colw // 2
        PAD = int(0.04 * dpi)
        num_w = int(0.70 * dpi)      # 数值列间距：字号放大后要一起放宽
        LINE_PAD = int(0.045 * dpi)  # 横线与文字之间的留白（防止压字）

        def draw_cell(eye, fi, top_y):
            """在指定眼、指定公式、指定顶端 y 处画一格，返回该格底端 y。"""
            bx = colx[eye]
            idx0 = 0 if eye == "OD" else 4
            rw, cl = order[fi]
            fname = formulas[fi]
            t = r["tables"][idx0 + fi] if idx0 + fi < len(r["tables"]) else None
            cx = bx + cl * subw + PAD
            # 文字用 top 锚点，行高留白由 LH_CELL 统一控制；
            # 横线一律画在「上一行底 + LINE_PAD」，绝不贴着字底。
            cy = top_y

            T((cx, cy), fname, jb)
            cy += LH_CELL + int(0.008 * dpi)
            if clines.get(fname):
                T((cx, cy), clines[fname], tiny)
                cy += LH_CELL
            cy += int(0.02 * dpi)

            c0 = cx
            c1 = cx + num_w
            cell_r = cx + subw - 2 * PAD
            if not t:
                T((c0, cy), "该公式未计算", tiny)
                cy += LH_CELL
            else:
                T((c0, cy), "IOL(D)", tiny)
                T((c1, cy), "屈光度(D)", tiny)
                cy += LH_CELL + LINE_PAD
                hl(cy, c0, cell_r)
                cy += LINE_PAD
                best = min(range(len(t["pairs"])),
                           key=lambda i: abs(t["pairs"][i][1] - tgt))
                for i, (p, q) in enumerate(t["pairs"]):
                    fnt = smonb if i == best else smon
                    T((c0, cy), f"{p:.1f}", fnt)
                    T((c1, cy), f"{q:+.2f}", fnt)
                    cy += LH_CELL
                cy += LINE_PAD
                hl(cy, c0, cell_r)
                cy += LINE_PAD
                T((c0, cy), "正视IOL:", tiny)
                T((c0 + int(0.62 * dpi), cy), f"{t['emmetropic']:.2f}", smon)
                cy += LH_CELL
            return cy

        row_top = {0: y}
        for rw in (0, 1):
            if rw == 1:
                row_top[1] = row_bot0 + int(0.05 * dpi)
            b0 = draw_cell("OD", rw * 2, row_top[rw])
            b1 = draw_cell("OD", rw * 2 + 1, row_top[rw])
            b2 = draw_cell("OS", rw * 2, row_top[rw])
            b3 = draw_cell("OS", rw * 2 + 1, row_top[rw])
            this_bot = max(b0, b1, b2, b3)
            # 两眼上下两段之间的横向分隔线（每段结束时画）
            hl(this_bot + int(0.01 * dpi), M + int(0.02 * dpi), W - M - int(0.02 * dpi))
            if rw == 0:
                row_bot0 = this_bot
        y = this_bot + int(0.02 * dpi)

        # 眼别竖直分隔线
        dr.line([(M + colw, meas_top - int(0.07 * dpi)), (M + colw, y)],
                fill="black", width=1)

        # ---- 页脚 ----
        fy = H - M - int(0.26 * dpi)
        hl(fy)
        T((M, fy + int(0.05 * dpi)),
          "Carl Zeiss IOLMaster(R) Advanced Technology V. 5.5", tiny)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        T((W - M, fy + int(0.05 * dpi)), f"打印: {stamp}", tiny, anchor="ra")
        T((W - M, fy + int(0.05 * dpi) + int(0.13 * dpi)),
          "* = 手动更改, ! = 边界线值", tiny, anchor="ra")
        state["bottom"] = y
        state["foot"] = fy
        return img

    img = None
    used_k = 1.0
    for k in (1.00, 0.97, 0.94, 0.91, 0.88, 0.85, 0.82, 0.79, 0.76, 0.73,
              0.70, 0.67, 0.64, 0.61, 0.58, 0.55, 0.52, 0.50):
        img = draw_all(k)
        used_k = k
        if state["bottom"] < state["foot"] - int(0.08 * dpi):
            break

    # ★ out_png 为 None 时直接返回 PIL Image，不编码、不落盘。
    #   DICOM 路径（build_sc）要的是 JPEG，PNG 只是中间态 —— 编码一次
    #   再解码一次纯属浪费（实测每张 68ms 编码 + 34ms 解码）。见 render_image()。
    if out_png is None:
        return img

    img.save(out_png, "PNG", dpi=(dpi, dpi))
    return {"path": out_png, "scale": used_k,
            "content_bottom": state["bottom"], "footer_y": state["foot"],
            "page_h": H, "bytes": os.path.getsize(out_png)}


def render_image(struct, dpi=200, axes=None, institution=""):
    """渲染成 **PIL Image 对象**，供 dicom.build_sc 直接转 JPEG。

    ★ 这是 DICOM 路径的正确入口。不要用 render_png() 再解码回来 ——
      那等于 Image → PNG → 字节 → Image → JPEG，中间两次编码
      （PNG 编码 68ms + PNG 解码 34ms）完全白做。

    调用方负责在用完后释放（Image 不自动回收，每张 A4 200dpi 约 11.6MB）。
    """
    return render(struct, None, dpi=dpi, axes=axes, institution=institution)


def render_png(struct, dpi=200, axes=None, institution=""):
    """渲染成 PNG **字节**。

    ★ 只用于**要落盘/要字节**的场景：pipeline.py render 子命令、自检。
      DICOM 路径请用 render_image() —— 那里不需要 PNG。

    内部写临时文件再读回 —— PIL 的 PNG 编码器需要 seekable 流，
    而 render() 已经用 out_png 路径保存并返回统计信息，直接复用最省事。
    """
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
        out = tf.name
    try:
        render(struct, out, dpi=dpi, axes=axes, institution=institution)
        with open(out, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass
