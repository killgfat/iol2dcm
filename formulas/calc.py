# -*- coding: utf-8 -*-
"""四公式计算：MDB 测量值 × 各款晶体常数 → 度数档位表。

数据流
------
    MDB IolmMeasurements
      └ al_mm / r1_mm / r2_mm / acd_mm      ← 原始测量值
          └ K = 337.5 / r                   ← 换算成角膜屈光力
              └ 四公式 × 各型号常数
                  └ [{iol_power, refraction}, ...]   ← 档位表

公式名一律按 IOLCon 写法：SRK/T · Haigis · Hoffer Q · Holladay 1

缺值处理
--------
* AL / r1 / r2 缺任一 → **该公式整块不出**（这三项是所有公式的必要输入）
* ACD 缺 → Haigis 不出（它必须有实测前房深度）；Hoffer Q 与 Holladay 1
  仍可算（它们用 pACD / SF 推 ELP），SRK/T 也可算
* 某型号某公式常数缺 → 该组合留空，**绝不拿别的值顶上**
"""
from __future__ import annotations

from . import haigis
from . import hoffer_q
from . import holladay1
from . import srkt
from core.utils import FORMULA_ORDER, KH, log


def k_from_radius(r_mm):
    """曲率半径 mm → 角膜屈光力 D。

    K = 337.5 / r。这个 337.5 是 IOLMaster 打印机的换算基准
    （应使用脱敏 fixture 或现场验证数据核对，不在源码中固化病例数值）。

    ⚠️ 历史坑：曾写成 0.3375/r，差 1000 倍，度数全错。
    """
    if not r_mm:
        return None
    try:
        r = float(r_mm)
    except (TypeError, ValueError):
        return None
    if r <= 0:
        return None
    return KH / r


def calc_eye(al, r1, r2, acd, lenses, target=-0.5, step=0.5):
    """单眼四公式 × 各型号。

    参数
    ----
    al     眼轴 mm
    r1/r2  角膜曲率半径 mm（不是 K）
    acd    前房深度 mm，可为 None
    lenses config.yaml 装载的型号列表
    target 目标屈光 D
    step   档位步进 D

    返回
    ----
    {公式: {型号名: {power, pred, options, exact, constants_text} 或 None}}
    """
    res = {f: {} for f in FORMULA_ORDER}
    if al is None or r1 is None or r2 is None:
        return res

    k1, k2 = k_from_radius(r1), k_from_radius(r2)
    if k1 is None or k2 is None:
        return res
    k_mean = (k1 + k2) / 2.0

    def pack(r_):
        # ★ 档位表按晶体度数**从大到小**排。
        #   公式模块返回升序（低度数→高度数），这里翻转。
        #   屈光度随之变成升序（高度数对应更负的屈光），这是物理正确的。
        opts = [(float(o["iol_power"]), float(o["refraction"]))
                for o in r_["iol_options"]]
        opts.sort(key=lambda x: x[0], reverse=True)
        return {
            "power": float(r_["iol_power"]),
            "pred": float(r_["predicted_refraction"]),
            "options": opts,
            "exact": float(r_["iol_power_exact"]),
            "constants_text": r_.get("constants_text", ""),
            "note": r_.get("model_note", ""),
        }

    for L in lenses:
        name = L["name"]
        a0, a1, a2 = L["haigis"]

        # ---- SRK/T：需要 A 常数 ----
        if L.get("srkt_a") is None:
            res["SRK/T"][name] = None
        else:
            try:
                r = srkt.calculate(al, k_mean, target=target,
                                   a_constant=L["srkt_a"], acd=acd, step=step)
                res["SRK/T"][name] = pack(r)
            except Exception as e:
                log(f"    SRK/T {name}: {e}")
                res["SRK/T"][name] = None

        # ---- Haigis：必须有实测 ACD + 完整 a0/a1/a2 ----
        if acd is None or None in (a0, a1, a2):
            res["Haigis"][name] = None
        else:
            try:
                r = haigis.calculate(al, k_mean, target=target,
                                     a_constant=L.get("srkt_a"), acd=acd, step=step,
                                     constants={"a0": a0, "a1": a1, "a2": a2})
                res["Haigis"][name] = pack(r)
            except Exception as e:
                log(f"    Haigis {name}: {e}")
                res["Haigis"][name] = None

        # ---- Hoffer Q：需要 pACD ----
        if L.get("pacd") is None:
            res["Hoffer Q"][name] = None
        else:
            try:
                r = hoffer_q.calculate(al, k_mean, target=target,
                                       a_constant=L.get("srkt_a"), acd=acd,
                                       step=step, pacd=L["pacd"])
                res["Hoffer Q"][name] = pack(r)
            except Exception as e:
                log(f"    Hoffer Q {name}: {e}")
                res["Hoffer Q"][name] = None

        # ---- Holladay 1：需要 SF ----
        if L.get("sf") is None:
            res["Holladay 1"][name] = None
        else:
            try:
                r = holladay1.calculate(al, k_mean, target=target,
                                        a_constant=L.get("srkt_a"), acd=acd,
                                        step=step, sf=L["sf"])
                res["Holladay 1"][name] = pack(r)
            except Exception as e:
                log(f"    Holladay 1 {name}: {e}")
                res["Holladay 1"][name] = None
    return res


def calc_record(rec, lenses, target=-0.5, step=0.5):
    """给整条检查记录（双眼）算四公式。原地写入 eye["calc"]。

    rec 结构（由 pipeline 组装）：
        name / dob / date
        eyes: {OD: {al, r1, r2, k1, k2, acd, axis}, OS: {...}}
    """
    for side in ("OD", "OS"):
        e = rec["eyes"].get(side)
        if not e:
            continue
        e["calc"] = calc_eye(e.get("al"), e.get("r1"), e.get("r2"),
                             e.get("acd"), lenses, target=target, step=step)
    return rec


def calcable(rec):
    """本记录是否算得出**至少一个**度数。

    ★ 判据是「有没有结果」，不是「数据完不完整」。缺 AL 或缺 K 时
      calc_eye 返回全空结构（每格都是 None），此时不该出一张满屏
      「该公式未计算」的晶体单 —— 但 OAM/KER 里的实测值照录。
    """
    for e in (rec.get("eyes") or {}).values():
        for per_lens in (e.get("calc") or {}).values():
            if any(v is not None for v in per_lens.values()):
                return True
    return False


def has_measurement(rec):
    """本记录是否有任何可录入的结构化测量值。

    OAM 需要 AL，KER 需要 r1+r2。两者都没有时这条检查没有任何 DICOM
    内容可发（只有眼状态是录不进 OAM 的 —— 它的 Lens Status 挂在
    per-eye item 上，而那个 item 的 Measurements Sequence 是 Type 1）。
    """
    for e in (rec.get("eyes") or {}).values():
        if e.get("al") or (e.get("r1") and e.get("r2")):
            return True
    return False


def constants_text(lens):
    """型号常数串，写进报告单每格底部。

    ★ 键名必须是 `A(SRK/T):` 这种带括号的形式 ——
      report.parse_const 用正则 `A\\(([^)]+)\\)\\s*:` 抓它，
      并把括号内作为键（得到 "SRK/T"）。写成裸 `A:119.30` 会被
      解析成键 "A"，const_lines 里取 cst["SRK/T"] 就是 None，
      SRK/T 格的常数行整行消失。
    """
    a0, a1, a2 = lens["haigis"]
    parts = []
    if lens.get("srkt_a") is not None:
        parts.append(f"A(SRK/T):{lens['srkt_a']:.2f}")
    if None not in (a0, a1, a2):
        parts.append(f"A0:{a0:.3f} A1:{a1:.3f} A2:{a2:.3f}")
    if lens.get("pacd") is not None:
        parts.append(f"pACD:{lens['pacd']:.2f}")
    if lens.get("sf") is not None:
        parts.append(f"SF:{lens['sf']:.2f}")
    return ";".join(parts)
