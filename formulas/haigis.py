"""Haigis 公式的本地实现（Haigis 2000）。

三常数形式：:

    d  = a0 + a1*ACD + a2*AL
    P  = 1336/(AL-d) - 1336/(1336/K' - d)

其中 K' 为目标屈光度折算到角膜平面后的等效角膜屈光度。

A 常数只用于推导默认的 a0（0.62467*A - 72.434），这是由 A 常数换算得到的
近似起点，不是 Haigis 优化后的三常数（优化需要术前术后 IOL Master 数据
做最小二乘拟合）。若调用方提供术前实测 ACD，请一并给出优化后的 a0/a1/a2。
"""

from __future__ import annotations

from .common import (
    SPECTACLE_VERTEX_M,
    VERGENCE_SCALE,
    as_float,
    build_formula_result,
    normalize_k_to_reference,
    validate_eye,
)

LABEL = "Haigis"

CITATIONS = (
    (
        "Haigis W, Lege B, Miller N, Schneider B. Comparison of immersion "
        "ultrasound biometry and partial coherence interferometry for "
        "intraocular lens calculation according to Haigis. Graefes Arch Clin "
        "Exp Ophthalmol. 2000;238(9):765-773."
    ),
)

MODEL = "Haigis"
MODEL_NOTE = (
    "公开 Haigis 三常数公式（2000）：a1=0.40、a2=0.10 为经典取值，"
    "a0 由 A 常数换算得到，并非个体化优化结果。"
)
DEFAULT_A_CONSTANT = 118.7

A1 = 0.4
A2 = 0.1
A0_SLOPE = 0.62467
A0_INTERCEPT = -72.434
DEFAULT_ACD = 3.2

# Haigis 的角膜屈光度约定：Dc = 331.5 / r（折射率 1.3315）。
HAIGIS_KERATOMETRY_MM = 331.5
REFERENCE_KERATOMETRY_MM = 337.5

MISSING_ACD_WARNING = "缺少术前 ACD，使用默认值 3.20 mm（Haigis 建议使用实测 ACD）"


def _corneal_target(target, vertex=SPECTACLE_VERTEX_M):
    """把眼镜平面目标屈光度折算到角膜平面。"""
    target_value = as_float(target, "target_refraction")
    denominator = 1.0 - vertex * target_value
    if denominator <= 1e-6:
        raise ValueError("target_refraction 过大，无法折算到角膜平面")
    return target_value / denominator


def _invert_vergence_target(al, elp, k, iol_power, vertex=SPECTACLE_VERTEX_M):
    """由 Haigis 的聚散度式反解眼镜平面屈光度。"""
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


def _a0(a_constant):
    """由 A 常数换算的近似 a0 常数。"""
    return A0_SLOPE * a_constant + A0_INTERCEPT


def _haigis_k(k_reference):
    """把 1.3375 基准的 K 折算为 Haigis 的 1.3315 基准 Dc。"""
    return k_reference * HAIGIS_KERATOMETRY_MM / REFERENCE_KERATOMETRY_MM


def _resolve_constants(constants, a_constant):
    """返回 (a0, a1, a2, 是否为自定义常数)。"""
    if constants is None:
        return _a0(a_constant), A1, A2, False

    if isinstance(constants, dict):
        a0 = as_float(constants.get("a0"), "haigis_constants.a0")
        a1 = as_float(constants.get("a1"), "haigis_constants.a1")
        a2 = as_float(constants.get("a2"), "haigis_constants.a2")
    else:
        a0 = as_float(getattr(constants, "a0", None), "haigis_constants.a0")
        a1 = as_float(getattr(constants, "a1", None), "haigis_constants.a1")
        a2 = as_float(getattr(constants, "a2", None), "haigis_constants.a2")
    return a0, a1, a2, True


def _elp(al, acd, a0, a1=A1, a2=A2):
    """Haigis 的有效晶体位置 d（mm，自角膜顶点起算）。"""
    return a0 + a1 * acd + a2 * al


def _power(al, elp, k_target):
    """给定 ELP 与角膜平面目标 K'，求精确 IOL 度数。"""
    if al - elp <= 1e-6:
        raise ValueError("AL 与 ELP 过于接近，Haigis 公式发散")
    cornea_term = VERGENCE_SCALE / k_target - elp
    if cornea_term <= 1e-6:
        raise ValueError("角膜屈光度与 ELP 组合导致 Haigis 公式发散")
    return VERGENCE_SCALE / (al - elp) - VERGENCE_SCALE / cornea_term


def refraction_for_power(
    al,
    k,
    iol_power,
    a_constant=DEFAULT_A_CONSTANT,
    k_index=1.3375,
    acd=None,
    constants=None,
):
    """给定 IOL 度数，返回 Haigis 预测屈光度（D，眼镜平面）。

    acd 缺省时使用 3.20 mm，与 calculate 的缺省处理保持一致。
    """
    al_value, k_value = validate_eye(al, k)
    k_ref = normalize_k_to_reference(k_value, k_index)
    a_value = as_float(a_constant, "a_constant")
    acd_value = DEFAULT_ACD if acd is None else as_float(acd, "ACD")
    a0, a1, a2, _ = _resolve_constants(constants, a_value)
    elp = _elp(al_value, acd_value, a0, a1, a2)
    return _invert_vergence_target(
        al_value,
        elp,
        _haigis_k(k_ref),
        as_float(iol_power, "iol_power"),
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
    constants=None,
):
    """按统一返回结构计算 Haigis。

    lt/wtw 仅为统一各公式签名而保留：Haigis 三常数形式不使用晶体厚度和
    白到白距离。acd 缺省时按 3.20 mm 计算并标记低置信度。
    """
    al_value, k_value = validate_eye(al, k)
    k_index_value = as_float(k_index, "k_index")
    k_ref = normalize_k_to_reference(k_value, k_index_value)
    a_value = as_float(a_constant, "a_constant")
    target_value = as_float(target, "target_refraction")

    warnings = []
    if acd is None:
        acd_value = DEFAULT_ACD
        warnings.append(MISSING_ACD_WARNING)
    else:
        acd_value = as_float(acd, "ACD")

    a0, a1, a2, custom = _resolve_constants(constants, a_value)
    if not custom:
        warnings.append(
            "未提供晶体专属 a0/a1/a2，a0 由 A 常数近似；"
            "与 IOL Master 的 Haigis 结果可能相差 1–3 D"
        )
    elp = _elp(al_value, acd_value, a0, a1, a2)
    k_haigis = _haigis_k(k_ref)
    exact_power = _power(
        al_value,
        elp,
        k_haigis + _corneal_target(target_value),
    )

    def refraction_at(power):
        """返回该 IOL 度数对应的预测屈光度。"""
        return _invert_vergence_target(al_value, elp, k_haigis, power)

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
            (
                f"自定义 a0={a0:.4f}; a1={a1:.4f}; a2={a2:.4f}; "
                f"ACD={acd_value:.2f} mm; K(1.3315)={k_haigis:.2f} D"
            )
            if custom
            else (
                f"A={a_value:.2f}; a0={a0:.2f}; a1={a1:.2f}; a2={a2:.2f}; "
                f"ACD={acd_value:.2f} mm; K(1.3315)={k_haigis:.2f} D"
            )
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
]
