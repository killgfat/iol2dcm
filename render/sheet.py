# -*- coding: utf-8 -*-
"""把一条检查记录转成报告单结构（喂给 report.render）。

为什么不直接复用厂商公式表
--------------------------
`.iol` 里那 8 张 Zeiss 公式表是设备自己算的；本项目**不再解读 .iol**，
全部数值来自 MDB，四公式由 calc.py 现算。所以这里的 tables 是我们
自己算出来的 9 档表，不是厂商的。

2×2 布局顺序（与设备原厂报告单一致）
----------------------------------
    OD: SRK/T    Haigis
        Hoffer Q Holladay 1
    OS: 同上
    → tables 是长度 8 的列表，前 4 个 OD、后 4 个 OS
"""
from __future__ import annotations

from formulas.calc import constants_text
from core.utils import FORMULA_ORDER, KH


def _eye_report(e):
    """报告单表头那几行：AL / r1 / r2 / ACD / 眼状态。

    ★ 注意报告单印的是**曲率半径 r (mm)**，不是 K (D)。
      设备原厂报告单就是这样，换算成 K 只用于计算。
    """
    if not e:
        return {"al": None, "r1": None, "r2": None, "acd": None}
    return {
        "al": e.get("al"),
        "r1": e.get("r1"),
        "r2": e.get("r2"),
        "acd": e.get("acd"),
        # 眼状态：MDB 的中文原文（有晶状体 / 硅油充填眼 / 无晶状体 …）。
        # 以前这里硬编码 "Phakic"，硅油眼和无晶状体眼全被标错。
        "state": e.get("state") or "",
    }


def build(rec, lens, target=-0.5, step=0.5, mode="多重公式"):
    """组装 report.render() 需要的 dict。

    参数
    ----
    rec    pipeline 组装好的记录：name/dob/date/eyes{OD,OS:{al,r1,r2,acd,calc}}
    lens   单款晶体（config 装载的一个 dict）

    返回
    ----
    dict，字段与 report.render 的期望一致。
    """
    name = lens["name"]

    tables = []
    for side in ("OD", "OS"):
        e = rec["eyes"].get(side)
        for fname in FORMULA_ORDER:
            v = ((e or {}).get("calc") or {}).get(fname, {}).get(name) if e else None
            if v is None:
                tables.append(None)
            else:
                tables.append({
                    "emmetropic": v["exact"],
                    # 报告单只放得下 9 行
                    "pairs": v["options"][:9],
                })

    return {
        "file": f"{rec['date']},{rec['name']},{name}",
        # 中文姓名直接放 given，family 留空 —— _fmt_name 会把它们连写
        "family": "",
        "given": rec["name"],
        "dob": rec.get("dob") or "",
        "exam_date": rec["date"],
        "operator": rec.get("operator") or "",
        "lens": name,
        "target_ref": f"{target:.1f}",
        "mode": mode,
        "formulas": list(FORMULA_ORDER),
        # ★ 常数串必须带 A(SRK/T): 外壳，否则 report.parse_const 解析不出 SRK/T 的 A
        "const_str": constants_text(lens),
        "tables": tables,
        "R": _eye_report(rec["eyes"].get("OD")),
        "L": _eye_report(rec["eyes"].get("OS")),
        "summary": {},
    }
