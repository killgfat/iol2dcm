"""公式层通用工具：输入校验、适用域评估、度数表与结果组装。

本模块只放与具体公式无关的逻辑：有限数校验、K 折射率折算、AL/K 硬边界、
舒适域评估、0.5 D 取整、候选表与统一结果容器。各公式的 ELP、目标屈光度
折算和术后屈光反解由公式模块自己实现，不放在这里。
"""

from __future__ import annotations

import math

# ---------------------------------------------------------------------------
# 通用光学/输入常数
# ---------------------------------------------------------------------------

N_AQUEOUS = 1.336
VERGENCE_SCALE = 1336.0
SPECTACLE_VERTEX_M = 0.012

K_INDEX_REF = 1.3375
K_INDEX_ALT = 1.332

# 硬边界：超出直接拒绝，避免产生无意义的医学数值。
AL_HARD_MIN = 12.0
AL_HARD_MAX = 38.0
K_HARD_MIN = 30.0
K_HARD_MAX = 60.0

# 舒适域：超出只降置信度，不拒绝计算。
AL_COMFORT_MIN = 22.0
AL_COMFORT_MAX = 26.0
K_COMFORT_MIN = 38.0
K_COMFORT_MAX = 48.0

# 其他输入的物理合理范围（用于 API 边界校验）。
ACD_MIN = 0.5
ACD_MAX = 8.0
LT_MIN = 0.0
LT_MAX = 15.0
WTW_MIN = 5.0
WTW_MAX = 25.0
TARGET_MIN = -30.0
TARGET_MAX = 30.0
A_CONSTANT_MIN = 112.0
A_CONSTANT_MAX = 125.0
STEP_MIN = 0.1
STEP_MAX = 2.0

# IOL 度数表的显示范围（覆盖常见的负度数/超高度数）。
IOL_TABLE_MIN = -10.0
IOL_TABLE_MAX = 45.0


def as_float(value, name):
    """把输入转换为有限浮点数，失败时抛出 ValueError。"""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} 必须是有效数值")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是有效数值") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} 必须是有限数值")
    return result


def astigmatism_vector(cylinder: float, axis: float) -> tuple[float, float]:
    """返回全柱镜双角矢量；C>0 时 axis 表示光学强子午线，单位 D/度。

    使用 (C*cos(2a), C*sin(2a))，不是半柱镜 J0/J45。
    负 C 表示该子午线的扁平化效应，如切口 SICA。
    """
    magnitude = as_float(cylinder, "cylinder")
    angle = math.radians(2 * (as_float(axis, "axis") % 180))
    return magnitude * math.cos(angle), magnitude * math.sin(angle)


def vector_astigmatism(x: float, y: float) -> dict:
    """把全柱镜双角矢量还原为量值、强子午线及负柱镜处方轴。

    零散光轴无定义，返回 None；axis 是强子午线，minus_cylinder_axis
    是矫正该光学散光的负柱镜轴，两者相差 90 度。
    """
    x_value = as_float(x, "vector.x")
    y_value = as_float(y, "vector.y")
    magnitude = math.hypot(x_value, y_value)
    axis = None
    if magnitude > 1e-10:
        axis = math.degrees(math.atan2(y_value, x_value)) / 2 % 180
        if math.isclose(axis, 180, abs_tol=1e-10):
            axis = 0.0
    return {
        "cylinder": magnitude if axis is not None else 0.0,
        "axis": axis,
        "minus_cylinder_axis": None if axis is None else (axis + 90) % 180,
        "vector": {"x": x_value, "y": y_value},
    }


def parse_optional_float(value, name, default=None):
    """解析可选数值：None/空字符串返回 default，其他交给 as_float。"""
    if value is None:
        return default
    if isinstance(value, str) and not value.strip():
        return default
    return as_float(value, name)


def parse_a_constant(value, default=119.30):
    """解析并校验 A 常数。"""
    parsed = parse_optional_float(value, "a_constant", default)
    if not A_CONSTANT_MIN <= parsed <= A_CONSTANT_MAX:
        raise ValueError(
            f"a_constant={parsed:.2f} 超出合理范围 "
            f"{A_CONSTANT_MIN:.0f}-{A_CONSTANT_MAX:.0f}"
        )
    return round(parsed, 2)


def parse_step(value, default=0.5):
    """解析候选表步长。"""
    parsed = parse_optional_float(value, "step", default)
    if not STEP_MIN <= parsed <= STEP_MAX:
        raise ValueError(
            f"step={parsed:.2f} 超出合理范围 {STEP_MIN:.1f}-{STEP_MAX:.1f} D"
        )
    return parsed


def normalize_k_index(value):
    """校验角膜曲率计折射率，只接受 1.3375 或 1.332。"""
    parsed = parse_optional_float(value, "k_index", K_INDEX_REF)
    if abs(parsed - K_INDEX_REF) < 1e-9:
        return K_INDEX_REF
    if abs(parsed - K_INDEX_ALT) < 1e-9:
        return K_INDEX_ALT
    raise ValueError("k_index 只支持 1.3375 或 1.332")


def normalize_k_to_reference(k, k_index=K_INDEX_REF):
    """把任意角膜曲率计折射率下的 K 折算到 1.3375 基准。"""
    k_value = as_float(k, "K")
    index = normalize_k_index(k_index)
    if abs(index - K_INDEX_REF) < 1e-9:
        return k_value
    return k_value * (K_INDEX_REF - 1.0) / (K_INDEX_ALT - 1.0)


def validate_eye(al, k):
    """校验 AL/K 的硬边界，返回 (AL, K) 浮点元组。"""
    al_value = as_float(al, "AL")
    k_value = as_float(k, "K")
    if not AL_HARD_MIN <= al_value <= AL_HARD_MAX:
        raise ValueError(
            f"AL={al_value:.2f} mm 超出可计算范围 "
            f"{AL_HARD_MIN:.0f}-{AL_HARD_MAX:.0f} mm"
        )
    if not K_HARD_MIN <= k_value <= K_HARD_MAX:
        raise ValueError(
            f"K={k_value:.2f} D 超出可计算范围 {K_HARD_MIN:.0f}-{K_HARD_MAX:.0f} D"
        )
    return al_value, k_value


def validate_optional_measurements(acd=None, lt=None, wtw=None, target=None):
    """校验可选术前测量值与目标屈光度的物理范围。

    缺省值不校验；返回归一化后的浮点值或 None。
    """
    acd_value = parse_optional_float(acd, "ACD")
    lt_value = parse_optional_float(lt, "LenThickness")
    wtw_value = parse_optional_float(wtw, "WTW")
    target_value = parse_optional_float(target, "Refraction", 0.0)

    if acd_value is not None and not ACD_MIN <= acd_value <= ACD_MAX:
        raise ValueError(
            f"ACD={acd_value:.2f} mm 超出合理范围 {ACD_MIN:.1f}-{ACD_MAX:.1f} mm"
        )
    if lt_value is not None and not LT_MIN <= lt_value <= LT_MAX:
        raise ValueError(
            f"LenThickness={lt_value:.2f} mm 超出合理范围 {LT_MIN:.1f}-{LT_MAX:.1f} mm"
        )
    if wtw_value is not None and not WTW_MIN <= wtw_value <= WTW_MAX:
        raise ValueError(
            f"WTW={wtw_value:.2f} mm 超出合理范围 {WTW_MIN:.1f}-{WTW_MAX:.1f} mm"
        )
    if not TARGET_MIN <= target_value <= TARGET_MAX:
        raise ValueError(
            f"Refraction={target_value:.2f} D 超出合理范围 "
            f"{TARGET_MIN:.0f}-{TARGET_MAX:.0f} D"
        )
    return acd_value, lt_value, wtw_value, target_value


def assess_domain(al, k):
    """评估 AL/K 是否落在舒适域内。

    返回 {"confidence", "warnings", "low_confidence"}；超出舒适域时只追加
    警告并降置信度，不拒绝计算。
    """
    al_value = as_float(al, "AL")
    k_value = as_float(k, "K")
    warnings = []
    if al_value < AL_COMFORT_MIN or al_value > AL_COMFORT_MAX:
        warnings.append(
            f"AL={al_value:.2f} mm 超出适用域 "
            f"{AL_COMFORT_MIN:.0f}-{AL_COMFORT_MAX:.0f} mm"
        )
    if k_value < K_COMFORT_MIN or k_value > K_COMFORT_MAX:
        warnings.append(
            f"K={k_value:.2f} D 超出适用域 {K_COMFORT_MIN:.0f}-{K_COMFORT_MAX:.0f} D"
        )
    low_confidence = bool(warnings)
    return {
        "confidence": "LOW_CONFIDENCE" if low_confidence else "OK",
        "warnings": warnings,
        "low_confidence": low_confidence,
    }


def round_to_step(value, step):
    """按给定步长四舍五入（step 必须为正数）。"""
    step_value = as_float(step, "step")
    if step_value <= 0:
        raise ValueError("step 必须是正数")
    value_float = as_float(value, "计数值")
    return round(math.floor(value_float / step_value + 0.5) * step_value, 4)


def format_power(value):
    """格式化为表格风格的度数文本（整数不带小数点）。"""
    power = as_float(value, "iol_power")
    if abs(power - round(power)) < 1e-9:
        return str(round(power))
    return f"{power:.1f}"


def build_table(
    exact_power,
    refraction_for_power,
    target,
    step=0.5,
    span=2.0,
    table_min=IOL_TABLE_MIN,
    table_max=IOL_TABLE_MAX,
):
    """围绕精确度数生成 IOL 候选表（默认 0.5 D 步长、span 2.0 -> 9 行）。

    每行调用 refraction_for_power(power) 得到该度数对应的预测屈光度；
    预测屈光度无法计算的档位会被跳过（极端参数下允许少于 9 行）。
    推荐行 = 预测屈光度最接近 target 的一行，且仅标记一行。
    """
    step_value = as_float(step, "step")
    if step_value <= 0:
        raise ValueError("step 必须是正数")
    span_value = as_float(span, "span")
    if span_value < 0:
        raise ValueError("span 必须是非负数")
    target_value = as_float(target, "target_refraction")

    center = round_to_step(exact_power, step_value)
    half_span = round(span_value / step_value)
    rows = []
    for offset in range(-half_span, half_span + 1):
        power = round(center + offset * step_value, 4)
        if power < table_min or power > table_max:
            continue
        try:
            refraction = float(refraction_for_power(power))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(refraction):
            continue
        rows.append(
            {
                "iol_power": format_power(power),
                # 本地公式不知道具体晶体设计；不要伪装成 Biconvex。
                "optic": None,
                "refraction": f"{refraction:.2f}",
                "recommended": False,
            }
        )

    if not rows:
        raise ValueError("无法生成 IOL 度数表：所有档位的预测屈光度都不可用")
    rows.sort(key=lambda row: float(row["iol_power"]))
    best_index = min(
        range(len(rows)),
        key=lambda index: abs(float(rows[index]["refraction"]) - target_value),
    )
    rows[best_index]["recommended"] = True
    return rows


def build_formula_result(
    *,
    al,
    k,
    target,
    step,
    exact_power,
    refraction_for_power,
    elp,
    a_constant,
    lens_factor,
    constants_text,
    model,
    model_note,
    warnings=(),
):
    """按统一返回结构组装公式结果（四个公式共用同一份字段定义）。"""
    domain = assess_domain(al, k)
    merged_warnings = list(domain["warnings"]) + [str(item) for item in warnings]
    table = build_table(
        exact_power,
        refraction_for_power,
        target,
        step=step,
    )
    recommended = next(row for row in table if row["recommended"])
    iol_power = float(recommended["iol_power"])
    predicted_refraction = float(refraction_for_power(iol_power))
    low_confidence = bool(merged_warnings) or domain["low_confidence"]
    return {
        "iol_power": iol_power,
        "iol_power_text": recommended["iol_power"],
        "iol_power_exact": float(exact_power),
        "predicted_refraction": predicted_refraction,
        "elp": float(elp),
        "lens_factor": (float(lens_factor) if lens_factor is not None else None),
        "a_constant": float(a_constant),
        "confidence": "LOW_CONFIDENCE" if low_confidence else "OK",
        "low_confidence": low_confidence,
        "warnings": merged_warnings,
        "model": model,
        "model_note": model_note,
        "constants_text": constants_text,
        "iol_options": table,
    }


__all__ = [
    "ACD_MAX",
    "ACD_MIN",
    "AL_COMFORT_MAX",
    "AL_COMFORT_MIN",
    "AL_HARD_MAX",
    "AL_HARD_MIN",
    "A_CONSTANT_MAX",
    "A_CONSTANT_MIN",
    "IOL_TABLE_MAX",
    "IOL_TABLE_MIN",
    "K_COMFORT_MAX",
    "K_COMFORT_MIN",
    "K_HARD_MAX",
    "K_HARD_MIN",
    "K_INDEX_ALT",
    "K_INDEX_REF",
    "LT_MAX",
    "LT_MIN",
    "N_AQUEOUS",
    "SPECTACLE_VERTEX_M",
    "STEP_MAX",
    "STEP_MIN",
    "TARGET_MAX",
    "TARGET_MIN",
    "VERGENCE_SCALE",
    "WTW_MAX",
    "WTW_MIN",
    "as_float",
    "assess_domain",
    "astigmatism_vector",
    "build_formula_result",
    "build_table",
    "format_power",
    "normalize_k_index",
    "normalize_k_to_reference",
    "parse_a_constant",
    "parse_optional_float",
    "parse_step",
    "round_to_step",
    "validate_eye",
    "validate_optional_measurements",
    "vector_astigmatism",
]
