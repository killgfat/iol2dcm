# -*- coding: utf-8 -*-
"""PatientID 生成：确定性、纯本地、不依赖任何 PACS 接口。

规则
----
    I + 出生日期(YYYYMMDD) + sha1(归一化姓名|生日) 前 4 位大写
    例：张伟 / 1970-01-01 → I19700101EA08

为什么不需要查 PACS
------------------
PatientID 完全由「姓名 + 生日」决定，同样的输入永远得到同样的 ID。这意味着：

* **重复推送天然幂等** —— 同一患者多次检查 PatientID 都一样，
  PACS 自动归到同一个患者档案下，不需要事先建映射。
* **不需要 REST API** —— 不查 /tools/find，不需要口令，
  不依赖任何厂商的 HTTP 接口。纯 DICOM 一条通道。
* **不需要本地映射缓存** —— 没有缓存就没有过期、失效、重建的问题。

与 HIS 登记号的关系
------------------
本 ID 是我们自己生成的，不等同于医院登记号。若日后要与 HIS 档案合流，
在 PACS 侧按「姓名 + 生日」做一次性的对照即可，不必让本管线知道登记号。

摘要里为什么带生日
    只用姓名的话，同名不同人（张伟、李娜这类）会撞成同一个档案；
    姓名+生日双键后，同名同生日才可能撞，实际概率极低。

为什么用 sha1 而非 base64
    base64 只编码不混淆，中文姓名的前几位 base64 常常相同
    （UTF-8 汉字首字节集中在 0xE4–0xE9，base64 前缀大量重复），
    4 位摘要区分度不足；sha1 是雪崩哈希，输入差一位输出完全不同。
"""
from __future__ import annotations

import hashlib
import re


def norm_name(s):
    """归一化姓名：去掉所有空白与 DICOM PN 保留分隔符，转大写。"""
    s = str(s or "")
    for ch in ("^", "=", ",", " ", "\u3000", "\t", "\r", "\n"):
        s = s.replace(ch, "")
    return s.strip().upper()


def norm_date(s):
    """归一化日期 → YYYYMMDD。接受 1970-01-01 / 19700101 / 1955/6/1。"""
    s = str(s or "").strip()
    if not s:
        return ""
    m = re.match(r"^(\d{4})[-/.]?(\d{1,2})[-/.]?(\d{1,2})$", s)
    if m:
        y, mo, d = m.groups()
        return f"{y}{int(mo):02d}{int(d):02d}"
    digits = re.sub(r"\D", "", s)
    return digits[:8] if len(digits) >= 8 else ""


def norm_sex(s):
    """归一化性别 → DICOM PatientSex (0010,0040) 的枚举值。

    DICOM 只认 M / F / O 三种非空取值（VR = CS，空串合法）。
    设备库里可能存 M/F/Male/Female/男/女/1/2，统一折算过来。

    认不出的值**返回空串**，不猜。把 "unknown" 硬映射成 "O"（Other）
    会让 PACS 端把「没录」误读成「确实是不明性别」，比留空更糟。
    """
    v = str(s or "").strip().upper()
    if not v:
        return ""
    if v in ("M", "MALE", "男", "1"):
        return "M"
    if v in ("F", "FEMALE", "女", "2"):
        return "F"
    if v in ("O", "OTHER", "其他", "未知", "UNKNOWN"):
        return "O"
    return ""


def make_pid(dob, name=""):
    """生成 PatientID：I + 生日8位 + sha1(姓名|生日) 前4位大写。

    无生日 → 返回空串（调用方应跳过，不能推一个无名档案进 PACS）。
    无姓名 → 退回 `I+生日`，避免生成空摘要的假 ID。
    """
    d = norm_date(dob)
    if not d:
        return ""
    nm = norm_name(name)
    if not nm:
        return f"I{d}"
    digest = hashlib.sha1(f"{nm}|{d}".encode("utf-8")).hexdigest()
    return f"I{d}{digest[:4].upper()}"


# 本管线生成的所有 ID 形态（含历史版本，便于识别与清理）
_RE_PATTERNS = [
    re.compile(r"^IOLM[0-9A-F]{12}$"),      # 旧版设备哈希
    re.compile(r"^IOLXM\d{8}$"),            # 旧版纯生日
    re.compile(r"^I\d{8}[0-9A-F]{4}$"),     # 现行 sha1 摘要
    re.compile(r"^I\d{8}$"),                # 无姓名时的退化形式
]


def is_generated(pid):
    """这个 PatientID 是不是本管线生成的（而非 HIS 登记号）。

    用于数据清理与核对：只删自己生成的档案，绝不误删 HIS 同步的。
    """
    s = str(pid or "").strip()
    if not s:
        return False
    return any(p.match(s) for p in _RE_PATTERNS)
