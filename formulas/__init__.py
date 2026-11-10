"""四公式计算：SRK/T · Haigis · Hoffer Q · Holladay 1。

各公式模块统一接口 calculate(al, k, target, a_constant, acd, step, **常数)。
calc.py 负责按型号编排（把常数分配下去、收集档位表）。
"""
from . import calc, common, haigis, hoffer_q, holladay1, srkt

__all__ = ["calc", "common", "srkt", "haigis", "hoffer_q", "holladay1"]
