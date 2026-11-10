"""SRK/T 公式的本地实现（Retzlaff 1990）。

公式使用平均角膜屈光度与 A 常数推导术后前房深度，并对角膜穹隆高度、
视网膜厚度做经验修正：:

    R     = 337.5 / K'
    Lcor  = AL                          (AL <= 24.2)
            其余 -3.446 + 1.716*AL - 0.0237*AL^2
    CW    = -5.41 + 0.58412*Lcor + 0.098*K'
    H     = R - sqrt(R^2 - CW^2/4)
    ACD   = 0.62467*A - 68.747 + H - 3.336
    ALopt = AL + 0.65696 - 0.02029*AL
    P     = 1000*n_a*(n_a*R - (n_c-1)*ALopt)
            / ((ALopt - ACD)*(n_a*R - (n_c-1)*ACD))

n_a = 1.336，n_c = 1.333，V = 12 mm。目标屈光度按 Retzlaff 1990 附录公式 (10)
进入分子/分母（不是把 Rx 直接加到 K 上）::

    T = n_a*R - (n_c-1)*ALopt
    U = n_a*R - (n_c-1)*ACD
    P = 1000*n_a*(T - 0.001*Rx*(V*T + ALopt*R))
        / ((ALopt-ACD)*(U - 0.001*Rx*(V*U + ACD*R)))

预测屈光度用同一附录的公式 (11) 解析反解。ELP 只由术前 AL/K/A 常数决定，
不随目标屈光度改变。
"""

from __future__ import annotations

import math

from .common import (
    N_AQUEOUS,
    as_float,
    build_formula_result,
    normalize_k_to_reference,
    validate_eye,
)

LABEL = "SRK/T"

CITATIONS = (
    (
        "Retzlaff JA, Sanders DR, Kraff MC. Development of the SRK/T "
        "intraocular lens implant power calculation formula. J Cataract "
        "Refract Surg. 1990;16(3):333-340."
    ),
)

MODEL = "SRK/T"
MODEL_NOTE = (
    "公开 SRK/T 公式（Retzlaff 1990）：平均 K + A 常数推导 ACD，"
    "含角膜穹隆高度与视网膜厚度修正。"
)
DEFAULT_A_CONSTANT = 118.7

N_CORNEA = 1.333
KERATOMETRY_MM = 337.5  # 1.3375 折射率下 K = 337.5 / R
VERTEX_MM = 12.0


def _acd(al, k_ref, a_constant):
    """SRK/T 推导出的有效晶体位置（即 ACD，mm）。"""
    _, _, elp = _geometry(al, k_ref, a_constant)
    return elp


def _geometry(al, k_ref, a_constant):
    """返回 (角膜半径, 光学眼轴, 推导 ACD)。"""
    radius = KERATOMETRY_MM / k_ref
    if al <= 24.2:
        lcor = al
    else:
        lcor = -3.446 + 1.716 * al - 0.0237 * al * al
    corneal_width = -5.41 + 0.58412 * lcor + 0.098 * k_ref
    discriminant = radius * radius - corneal_width * corneal_width / 4.0
    height = radius - math.sqrt(discriminant) if discriminant > 0 else radius
    elp = 0.62467 * a_constant - 68.747 + height - 3.336
    al_optical = al + 0.65696 - 0.02029 * al
    return radius, al_optical, elp


def _power(al, k_ref, a_constant, target):
    """给定眼镜平面目标屈光度，按 Retzlaff 1990 公式 (10) 求度数。"""
    radius, al_optical, elp = _geometry(al, k_ref, a_constant)
    target_value = as_float(target, "target_refraction")
    term_l = N_AQUEOUS * radius - (N_CORNEA - 1.0) * al_optical
    term_a = N_AQUEOUS * radius - (N_CORNEA - 1.0) * elp
    numerator = (
        1000.0
        * N_AQUEOUS
        * (term_l - 0.001 * target_value * (VERTEX_MM * term_l + al_optical * radius))
    )
    denominator = (al_optical - elp) * (
        term_a - 0.001 * target_value * (VERTEX_MM * term_a + elp * radius)
    )
    if abs(denominator) <= 1e-12:
        raise ValueError("SRK/T 公式发散：参数组合无法求出度数")
    return numerator / denominator


def _refraction(al, k_ref, a_constant, iol_power):
    """按 Retzlaff 1990 公式 (11) 解析反解预测屈光度。"""
    radius, al_optical, elp = _geometry(al, k_ref, a_constant)
    power_value = as_float(iol_power, "iol_power")
    term_l = N_AQUEOUS * radius - (N_CORNEA - 1.0) * al_optical
    term_a = N_AQUEOUS * radius - (N_CORNEA - 1.0) * elp
    numerator = 1000.0 * N_AQUEOUS * term_l - power_value * (al_optical - elp) * term_a
    denominator = N_AQUEOUS * (
        VERTEX_MM * term_l + al_optical * radius
    ) - 0.001 * power_value * (al_optical - elp) * (VERTEX_MM * term_a + elp * radius)
    if abs(denominator) <= 1e-12:
        return float("nan")
    return numerator / denominator


def refraction_for_power(
    al,
    k,
    iol_power,
    a_constant=DEFAULT_A_CONSTANT,
    k_index=1.3375,
):
    """给定 IOL 度数，返回 SRK/T 预测屈光度（D，眼镜平面）。"""
    al_value, k_value = validate_eye(al, k)
    k_ref = normalize_k_to_reference(k_value, k_index)
    return _refraction(
        al_value,
        k_ref,
        as_float(a_constant, "a_constant"),
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
):
    """按统一返回结构计算 SRK/T。

    acd/lt/wtw 仅为统一各公式签名而保留：SRK/T 自行推导前房深度，不使用
    术前实测的 ACD、晶体厚度和白到白距离。
    """
    al_value, k_value = validate_eye(al, k)
    k_index_value = as_float(k_index, "k_index")
    k_ref = normalize_k_to_reference(k_value, k_index_value)
    a_value = as_float(a_constant, "a_constant")
    target_value = as_float(target, "target_refraction")

    # ELP 用术前 K 推导；目标屈光度按论文公式 (10) 进入光学部分。
    elp = _acd(al_value, k_ref, a_value)
    exact_power = _power(al_value, k_ref, a_value, target_value)

    def refraction_at(power):
        """返回该 IOL 度数对应的预测屈光度。"""
        return _refraction(al_value, k_ref, a_value, power)

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
        constants_text=(f"A={a_value:.2f}; ACD={elp:.2f} mm; K index={k_index_value}"),
        model=MODEL,
        model_note=MODEL_NOTE,
    )


__all__ = [
    "CITATIONS",
    "LABEL",
    "calculate",
    "refraction_for_power",
]
