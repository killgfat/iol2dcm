"""Holladay 1 公式的本地实现（Holladay 1988）。

术后有效晶体位置由角膜厚度、Fyodorov 角膜穹隆高度与 Surgeon Factor 相加：

    r    = 337.5 / K
    rag  = max(r, 7.0)
    AG   = min(12.5*AL/23.45, 13.5)
    H    = rag - sqrt(rag^2 - AG^2/4)
    aACD = 0.56 + H
    ELP  = aACD + SF
    ALm  = AL + 0.2                    （视网膜厚度修正）
    P    = 1336/(ALm-ELP) - 1336/(1336/K' - ELP)

K' 为目标屈光度折算到角膜平面后的等效角膜屈光度（顶点距离 12 mm）。
rag/AG 的双重截断来自 Holladay 原论文，目的是避免根号内出现负数：
角膜半径不小于 7 mm、角膜直径不大于 13.5 mm。ELP 在 AL≈25.32 mm 附近取极大值。

SF（Surgeon Factor）是 Holladay 1 自己的晶体常数，与 SRK/T 的 A 常数、
Hoffer Q 的 pACD 互不通用。缺省时按厂商标准关系换算，这只是兜底起点：

    SF = 0.5663*A - 65.60

需要晶体专属 SF 时请从 ULIB / IOLCon 取值后显式传入（sf 参数）。
"""

from __future__ import annotations

import math

from .common import (
    SPECTACLE_VERTEX_M,
    VERGENCE_SCALE,
    as_float,
    build_formula_result,
    normalize_k_to_reference,
    validate_eye,
)

LABEL = "Holladay 1"

CITATIONS = (
    (
        "Holladay JT, Prager TC, Chandler TY, Musgrove KH, Lewis JW, "
        "Ruiz RS. A three-part system for refining intraocular lens power "
        "calculations. J Cataract Refract Surg. 1988;14(1):17-24."
    ),
)

MODEL = "Holladay 1"
MODEL_NOTE = (
    "公开 Holladay 1 公式（Holladay 1988）：角膜厚度 + Fyodorov 角膜高度 "
    "+ Surgeon Factor 推导 ELP，含视网膜厚度修正。"
)
DEFAULT_A_CONSTANT = 118.7

# 角膜屈光度与半径的换算（1.3375 折射率）。
KERATOMETRY_MM = 337.5
# 角膜厚度常数（mm），即 Holladay 论文中角膜顶点到内皮的距离。
CORNEA_THICKNESS = 0.56
# 视网膜厚度修正：光学眼轴 ALm = AL + 0.2 mm。
RETINA_THICKNESS = 0.2
# 角膜穹隆高度的两个截断值。
MIN_CORNEAL_RADIUS = 7.0
MAX_CORNEAL_DIAMETER = 13.5
CORNEAL_DIAMETER_SLOPE = 12.5 / 23.45

# SF 与 A 常数的厂商标准换算关系（不是 ULIB 优化关系）。
SF_SLOPE = 0.5663
SF_INTERCEPT = -65.60

# SF 输入边界。ULIB 304 款实测范围是 -7.08（Alcon MA60MA -diopters）
# 到 10.29（Alcon MA60MA +diopters），因此不能按 pACD 的 0~8 收窄，
# 否则型号查表拿到的 SF 会被自家校验拒掉。
SF_MIN = -10.0
SF_MAX = 12.0


def _corneal_target(target, vertex=SPECTACLE_VERTEX_M):
    """把眼镜平面目标屈光度折算到角膜平面。"""
    target_value = as_float(target, "target_refraction")
    denominator = 1.0 - vertex * target_value
    if denominator <= 1e-6:
        raise ValueError("target_refraction 过大，无法折算到角膜平面")
    return target_value / denominator


def _invert_vergence_target(al, elp, k, iol_power, vertex=SPECTACLE_VERTEX_M):
    """由 Holladay 1 的聚散度式反解眼镜平面屈光度。"""
    al_value = as_float(al, "AL") + RETINA_THICKNESS
    elp_value = as_float(elp, "ELP")
    k_value = as_float(k, "K")
    power_value = as_float(iol_power, "iol_power")
    if al_value - elp_value <= 1e-6:
        return float("nan")
    vergence_after_iol = VERGENCE_SCALE / (al_value - elp_value) - power_value
    if vergence_after_iol <= 0:
        return float("nan")
    k_target = VERGENCE_SCALE / (VERGENCE_SCALE / vergence_after_iol + elp_value)
    corneal_refraction = k_target - k_value
    denominator = 1.0 + vertex * corneal_refraction
    if denominator <= 1e-6:
        return float("nan")
    return corneal_refraction / denominator


def _sf(a_constant):
    """由 A 常数换算的近似 Surgeon Factor（mm）。"""
    return SF_SLOPE * a_constant + SF_INTERCEPT


def _anterior_acd(al, k):
    """角膜厚度 + Fyodorov 角膜穹隆高度（mm，自角膜顶点起算）。"""
    radius = max(KERATOMETRY_MM / k, MIN_CORNEAL_RADIUS)
    diameter = min(CORNEAL_DIAMETER_SLOPE * al, MAX_CORNEAL_DIAMETER)
    discriminant = radius * radius - diameter * diameter / 4.0
    height = radius - math.sqrt(discriminant) if discriminant > 0 else 0.0
    return CORNEA_THICKNESS + height


def _elp(al, k, sf):
    """Holladay 1 的有效晶体位置（mm，自角膜顶点起算）。"""
    return _anterior_acd(al, k) + sf


def _power(al, elp, k_target):
    """给定 ELP 与角膜平面目标 K'，求精确 IOL 度数。"""
    al_optical = al + RETINA_THICKNESS
    if al_optical - elp <= 1e-6:
        raise ValueError("AL 与 ELP 过于接近，Holladay 1 公式发散")
    cornea_term = VERGENCE_SCALE / k_target - elp
    if cornea_term <= 1e-6:
        raise ValueError("角膜屈光度与 ELP 组合导致 Holladay 1 公式发散")
    return VERGENCE_SCALE / (al_optical - elp) - VERGENCE_SCALE / cornea_term


def resolve_sf(sf, a_constant=DEFAULT_A_CONSTANT):
    """返回实际使用的 SF；None 表示由 A 常数换算。"""
    if sf is None:
        return _sf(as_float(a_constant, "a_constant"))
    return as_float(sf, "sf")


def refraction_for_power(
    al,
    k,
    iol_power,
    a_constant=DEFAULT_A_CONSTANT,
    k_index=1.3375,
    sf=None,
):
    """给定 IOL 度数，返回 Holladay 1 预测屈光度（D，眼镜平面）。"""
    al_value, k_value = validate_eye(al, k)
    k_ref = normalize_k_to_reference(k_value, k_index)
    elp = _elp(al_value, k_ref, resolve_sf(sf, a_constant))
    return _invert_vergence_target(
        al_value, elp, k_ref, as_float(iol_power, "iol_power")
    )


def calculate(
    al,
    k,
    target=0.0,
    a_constant=DEFAULT_A_CONSTANT,
    k_index=1.3375,
    acd=None,
    lt=None,
    wtw=None,
    step=0.5,
    sf=None,
):
    """按统一返回结构计算 Holladay 1。

    acd/lt/wtw 仅为统一各公式签名而保留：Holladay 1 自行推导前房深度，
    不使用术前实测的 ACD、晶体厚度和白到白距离。
    """
    al_value, k_value = validate_eye(al, k)
    k_index_value = as_float(k_index, "k_index")
    k_ref = normalize_k_to_reference(k_value, k_index_value)
    a_value = as_float(a_constant, "a_constant")
    target_value = as_float(target, "target_refraction")

    warnings = []
    custom_sf = sf is not None
    sf_value = resolve_sf(sf, a_value)
    if not custom_sf:
        warnings.append(
            "未提供晶体专属 SF，按厂商标准关系 SF=0.5663*A-65.60 换算；"
            "与 IOL Master 的 Holladay 1 结果可能有偏差"
        )

    elp = _elp(al_value, k_ref, sf_value)
    exact_power = _power(
        al_value,
        elp,
        k_ref + _corneal_target(target_value),
    )

    def refraction_at(power):
        """返回该 IOL 度数对应的预测屈光度。"""
        return _invert_vergence_target(al_value, elp, k_ref, power)

    return build_formula_result(
        al=al_value,
        k=k_value,
        target=target_value,
        step=step,
        exact_power=exact_power,
        refraction_for_power=refraction_at,
        elp=elp,
        a_constant=a_value,
        lens_factor=None,
        constants_text=(
            f"SF={sf_value:.2f} mm; aACD={_anterior_acd(al_value, k_ref):.2f} mm; "
            f"ELP={elp:.2f} mm; K index={k_index_value}"
        ),
        model=MODEL,
        model_note=MODEL_NOTE,
        warnings=warnings,
    )


__all__ = [
    "CITATIONS",
    "LABEL",
    "calculate",
    "refraction_for_power",
    "resolve_sf",
]
