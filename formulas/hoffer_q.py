"""Hoffer Q 公式的本地实现（Hoffer 1993 及 1994/2007 errata）。

术后前房深度由 A 常数与眼轴/角膜屈光度共同修正：:

    pACD = 0.58357*A - 63.896
    m, g = (1, 28)    (AL <= 23.0)
           (-1, 23.5) (AL > 23.0)
    ALc  = clamp(AL, 18.5, 31.0)
    ACD  = pACD + 0.3*(ALc-23.5) + tan(K)^2
           + 0.1*m*(23.5-ALc)^2 * tan(0.1*(g-ALc)^2) - 0.99166
    P    = 1336/(AL-ACD-0.05) - 1336/(1336/K' - (ACD+0.05))

其中 K' 为目标屈光度折算到角膜平面后的等效角膜屈光度。
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

LABEL = "Hoffer Q"

CITATIONS = (
    (
        "Hoffer KJ. The Hoffer Q formula: a comparison of theoretic and "
        "regression formulas. J Cataract Refract Surg. 1993;19(6):700-712 "
        "(errata: 1994;20(6):677, 2007;33(2):193)."
    ),
)

MODEL = "Hoffer Q"
MODEL_NOTE = (
    "公开 Hoffer Q 公式（Hoffer 1993）：A 常数推导 pACD，"
    "并按眼轴与角膜屈光度做前房深度修正。"
)
DEFAULT_A_CONSTANT = 118.7

PACD_SLOPE = 0.58357
PACD_INTERCEPT = -63.896
ELP_OFFSET = 0.05  # Hoffer Q 在 ACD 之上追加的晶体位置偏移（mm）
AL_CLAMP_MIN = 18.5
AL_CLAMP_MAX = 31.0


def _corneal_target(target, vertex=SPECTACLE_VERTEX_M):
    """把眼镜平面目标屈光度折算到角膜平面。"""
    target_value = as_float(target, "target_refraction")
    denominator = 1.0 - vertex * target_value
    if denominator <= 1e-6:
        raise ValueError("target_refraction 过大，无法折算到角膜平面")
    return target_value / denominator


def _invert_vergence_target(al, elp, k, iol_power, vertex=SPECTACLE_VERTEX_M):
    """由 Hoffer Q 的聚散度式反解眼镜平面屈光度。"""
    al_value = as_float(al, "AL")
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


def _pacd(a_constant):
    """由 A 常数推导的基准前房深度 pACD（mm）。"""
    return PACD_SLOPE * a_constant + PACD_INTERCEPT


def _acd(al, k, a_constant, pacd=None):
    """Hoffer Q 修正后的前房深度 ACD（mm，不含 0.05 偏移）。"""
    al_clamped = min(max(al, AL_CLAMP_MIN), AL_CLAMP_MAX)
    if al <= 23.0:
        m, g = 1.0, 28.0
    else:
        m, g = -1.0, 23.5
    tan_k = math.tan(math.radians(k))
    return (
        (_pacd(a_constant) if pacd is None else pacd)
        + 0.3 * (al_clamped - 23.5)
        + tan_k * tan_k
        + 0.1
        * m
        * (23.5 - al_clamped) ** 2
        * math.tan(math.radians(0.1 * (g - al_clamped) ** 2))
        - 0.99166
    )


def _power(al, elp, k_target):
    """给定 ELP 与角膜平面目标 K'，求精确 IOL 度数。"""
    if al - elp <= 1e-6:
        raise ValueError("AL 与 ELP 过于接近，Hoffer Q 公式发散")
    cornea_term = VERGENCE_SCALE / k_target - elp
    if cornea_term <= 1e-6:
        raise ValueError("角膜屈光度与 ELP 组合导致 Hoffer Q 公式发散")
    return VERGENCE_SCALE / (al - elp) - VERGENCE_SCALE / cornea_term


def refraction_for_power(
    al,
    k,
    iol_power,
    a_constant=DEFAULT_A_CONSTANT,
    k_index=1.3375,
):
    """给定 IOL 度数，返回 Hoffer Q 预测屈光度（D，眼镜平面）。"""
    al_value, k_value = validate_eye(al, k)
    k_ref = normalize_k_to_reference(k_value, k_index)
    a_value = as_float(a_constant, "a_constant")
    elp = _acd(al_value, k_ref, a_value) + ELP_OFFSET
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
    pacd=None,
):
    """按统一返回结构计算 Hoffer Q。

    acd/lt/wtw 仅为统一各公式签名而保留：Hoffer Q 自行推导前房深度，
    不使用术前实测的 ACD、晶体厚度和白到白距离。
    """
    al_value, k_value = validate_eye(al, k)
    k_index_value = as_float(k_index, "k_index")
    k_ref = normalize_k_to_reference(k_value, k_index_value)
    a_value = as_float(a_constant, "a_constant")
    target_value = as_float(target, "target_refraction")

    pacd_value = _pacd(a_value) if pacd is None else as_float(pacd, "pACD")
    elp = _acd(al_value, k_ref, a_value, pacd_value) + ELP_OFFSET
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
            f"A={a_value:.2f}; pACD={pacd_value:.2f} mm; ACD={elp:.2f} mm; "
            f"K index={k_index_value}"
        ),
        model=MODEL,
        model_note=MODEL_NOTE,
    )


__all__ = [
    "CITATIONS",
    "LABEL",
    "calculate",
    "refraction_for_power",
]
