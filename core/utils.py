#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""共享工具：UID 派生、中文判定、日志、数值清洗。"""
from __future__ import annotations

import hashlib
import sys
from datetime import datetime

# ---------------------------------------------------------------- 常量

# 公式名一律按 IOLCon 写法。
# 常数表、报告单、XML、state、CLI 输出全链路统一这一套，不再有别名。
# IOLCon 原文：SRK/T · Haigis · Hoffer Q · Holladay 1
FORMULA_ORDER = ["SRK/T", "Haigis", "Hoffer Q", "Holladay 1"]

# 常数表 JSON 里的键名（与 IOLCon XML 标签对应：SRKt / Haigis / HofferQ / Holladay1）
FORMULA_KEYS = ("SRK/T", "Haigis", "Hoffer Q", "Holladay 1")

# K(D) = KH / r(mm)。IOLMaster 打印机自己的换算基准。
KH = 337.5


def uid(*parts) -> str:
    """确定性 UID：同样的输入永远得到同样的 2.25.x，重复推送幂等。"""
    h = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return "2.25." + str(int(h[:32], 16))


def digest(*parts) -> str:
    """内容指纹：同样的输入永远得到同样的 16 位十六进制短串。

    与 uid() 的区别：uid 必须是 DICOM 的 2.25.<整数> 形态且要能当 UID
    用；指纹只用来比对「这份内容我推过没有」，取 hex 更好读，也方便
    直接写进日志和 state。

    用途见 pipeline._fingerprint：设备对同一次检查补测后会改写同一行
    （guid 不变），只有比对内容才知道数据变了。
    """
    h = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return h[:16]


def log_label(value: str) -> str:
    """Return a stable non-identifying label for operational log messages."""
    if not value:
        return "<unknown>"
    return "patient-" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:10]


def is_cjk(s: str) -> bool:
    if not s:
        return False
    return any(
        "\u4e00" <= ch <= "\u9fff"      # CJK 统一表意文字
        or "\u3400" <= ch <= "\u4dbf"   # 扩展 A
        or "\uf900" <= ch <= "\ufaff"   # 兼容表意文字
        for ch in s
    )


def log(msg: str, stream=None) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts}   {msg}", file=stream or sys.stdout, flush=True)


def num(v):
    """转 float；空 / 0 / 'None' 一律返回 None。

    MDB 里未测出的字段存的是 0，不是 NULL —— 直接当有效值会让
    公式算出荒唐的度数（AL=0 时 SRK/T 会吐出几百 D）。
    """
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "0", "0.0", "0.00", "None", "NULL", "-"):
        return None
    try:
        f = float(s)
    except (TypeError, ValueError):
        return None
    return None if f == 0.0 else f


def fnum(v):
    """转 float，允许 0（用于看轴位这类合法零值）。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fmt(v, nd=4) -> str:
    """数值格式化；None → 空串（不是 'None' 也不是 0）。"""
    return "" if v is None else f"{v:.{nd}f}"


def dicom_patient_name(family, given) -> str:
    """拼 DICOM PatientName (PN)，**不用 ^ 分隔符**。

    不使用 PACS 传统的「姓^名」写法。
      · 中文名 → 直接全名拼接：张伟
      · 拼音/英文 → 空格分隔：Zhang San

    ⚠️ PN 的 VR 里 `^` 是组件分隔符，`,` 是多重人格分隔符，`=` 是组件组
    分隔符。中文全名不含这些字符，安全。但若操作员手输时用逗号分隔
    （如 "Smith, John"），**必须去掉逗号**，否则 Orthanc 会把它拆成两个
    人格，患者列表里出现莫名其妙的重影条目。
    """
    fam = (family or "").strip()
    giv = (given or "").strip()
    if not fam and not giv:
        return "UNKNOWN"
    if is_cjk(fam) or is_cjk(giv):
        return f"{fam}{giv}"
    full = " ".join(p for p in (fam, giv) if p)
    for ch in ("^", "=", ","):
        full = full.replace(ch, " ")
    return " ".join(full.split()) or "UNKNOWN"
