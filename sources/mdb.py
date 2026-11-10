# -*- coding: utf-8 -*-
"""IOLMaster 数据库解码：`IolmXP.MDB` 活库。

数据源
------
`IolmXP.MDB`（Jet 4 活库，IOLMaster 自己维护）：表 `IolmMeasurements`
每次测量一行，含 axis_r / axis_l。用 access_parser 纯 Python 读，
不依赖 JRE / ODBC。

本模块只做**取数**，不做任何计算。

⚠️ 适用范围
-----------
**仅对 Carl Zeiss IOLMaster 500（软件 V. 5.5）实测验证过。**

表名 `IolmMeasurements` 与下列字段名全部来自对该机型的实测：
    family_name / given_name / birth_date / exam_date / guid
    al_r al_l / r1_r r1_l / r2_r r2_l / acd_r acd_l / axis_r axis_l / state_r state_l

IOLMaster 700 使用**完全不同的库结构**（`CZM_*.MDB` 系列，不是 `IolmXP.MDB`），
字段名、单位、编码方式都可能不同。本项目**未对其做任何适配**，
不做任何可用性保证。详见 DISCLAIMER.md。

若要在别的机型上使用，先自行核对表名与字段是否存在、语义是否一致、
单位是否为 mm 与 D，再与设备原厂报告单逐例比对。
"""
from __future__ import annotations

import os
import time
from datetime import datetime

from core.utils import log, num

# ============================================================ MDB

MDB_TABLE = "IolmMeasurements"


def measure_time(guid):
    """从 guid 里取这行记录的**创建**时刻（epoch 秒）。取不到返回 None。

    guid 形态（应以脱敏 fixture 验证）：

        1.2.276.0.75.2.1.10.0.1.{YYMMDDHHMMSSmmm}.{patient_key}.{seq}
                                   └─ 第 11 段（下标 10），毫秒级本地时间

    ★ 这是「首次创建」，不是「最后修改」。
      设备对同一次检查补测时**改写同一行、guid 不变**，所以这个时间戳
      也不会变。它能回答「这条检查有多新」，不能回答「最后一次动过是
      什么时候」—— 后者只能靠内容指纹（见 pipeline._fingerprint）
      每轮观测出来。

    ★ 时区：设备写的是机器本地时间，这里按本地时区解释，
      只用于算「距今多久」，不参与任何跨机器比对。

    解析失败（guid 格式变了 / 不是这台机器的库）返回 None，
    调用方必须把 None 当成「时间未知」而不是「很久以前」。
    """
    try:
        p = str(guid).split(".")
        if len(p) != 13:
            return None
        s = p[10]
        if len(s) != 15 or not s.isdigit():
            return None
        return datetime.strptime(s, "%y%m%d%H%M%S%f").timestamp()
    except (ValueError, OSError, OverflowError):
        return None


def pick_latest(rows):
    """同一天同一患者的多行里，取**最新的一行**。

    ★ 不能用 rows[-1]。Access 表的物理顺序不等于写入顺序：被改过的行
      会被挪到别处，删过的行留下空洞。实测样本库里有 2 例因此取到了
      一条创建更早的**空行**，丢掉真正有数据的那行。

    判据：guid 内嵌创建时刻。**全部**时间戳都取不到时，才退回物理末行
    （保持旧行为）。混有「有/无时间戳」时，优先选有时间戳的 —— 那是
    可判定更新的行；无时间戳的只能当排序兜底。

    所有需要「这条检查的最新状态」的入口都必须走这里（run_once、audit、
    check、render、load_axes），否则排障时看到的数据会和管线实际用的
    不一致。
    """
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]
    return max(enumerate(rows),
               key=lambda ir: (ir[1].get("measure_ts") or 0, ir[0]))[1]


def load_measurements(mdb_path, table=MDB_TABLE):
    """读 IolmXP.MDB 的 IolmMeasurements 表，按 (姓, 名, 检查日期) 分组。

    返回 {(fam, giv, date): [row, ...]}，同一天多次检查按出现顺序。

    每行：
        guid / dob / exam_date / sex
        eyes: {r: {al, r1, r2, acd, axis, state}, l: {...}}

    sex 原样保留 MDB 的 patient_sex 值，不做映射 —— 归一化在
    core.patient.norm_sex() 里做，取数层不猜语义。

    ⚠️ 实测该表里：
       - al / r1 / r2 约 91% 有值
       - acd 仅 33% 有值（设备只在这部分检查里测 ACD）
       - lens_thickness_* 全为 0，used_r / used_l 全空
       这不是读取错误，是设备本来就只存这些。
    """
    from access_parser import AccessParser

    t0 = time.time()
    t = AccessParser(mdb_path).parse_table(table)
    if "family_name" not in t:
        raise RuntimeError(f"{mdb_path} 的 {table} 表里没有 family_name 字段")
    n = len(t["family_name"])

    def col(c):
        return t.get(c) or [None] * n

    c_fam, c_giv = col("family_name"), col("given_name")
    c_dob, c_date, c_guid = col("birth_date"), col("exam_date"), col("guid")
    # patient_sex 在实测库里全为 None，但字段确实存在。
    # 照样读、照样往下传 —— 设备哪天开始写了，不用改代码就能落库。
    c_sex = col("patient_sex")
    eyes = {s: {k: col(f"{k}_{s}")
                for k in ("al", "r1", "r2", "acd", "axis", "state")}
            for s in ("r", "l")}

    out = {}
    for i in range(n):
        fam = str(c_fam[i] or "").strip()
        giv = str(c_giv[i] or "").strip()
        # 自检行：姓名以 ! 开头或含 CHECK
        if fam.startswith("!") or giv.startswith("!") or "CHECK" in fam.upper():
            continue
        date = str(c_date[i] or "")[:8]
        if len(date) != 8:
            continue
        row = {
            "guid": str(c_guid[i] or ""),
            "dob": str(c_dob[i] or "")[:10],
            "exam_date": date,
            "sex": str(c_sex[i] or "").strip(),
            # 本行在设备上的创建时刻（epoch 秒），取不到为 None。
            # 见 measure_time()：补测不改 guid，所以这不是「最后修改时间」。
            "measure_ts": measure_time(c_guid[i]),
            "eyes": {},
        }
        for s in ("r", "l"):
            # ★ num() 把 0 当无效：MDB 未测出存的是 0，不是 NULL。
            #   直接采信会让公式算出荒唐度数（AL=0 时 SRK/T 吐出几百 D）。
            row["eyes"][s] = {k: num(eyes[s][k][i])
                              for k in ("al", "r1", "r2", "acd", "axis")}
            row["eyes"][s]["state"] = str(eyes[s]["state"][i] or "")
        out.setdefault((fam, giv, date), []).append(row)

    log(f"MDB {n} 行 → {len(out)} 组（{time.time() - t0:.1f}s）")
    return out


def load_axes(mdb_path, family, given, exam_date, table=MDB_TABLE):
    """从 MDB 取某次检查的角膜散光轴位。返回 {"R": 度数, "L": 度数}。

    取不到返回空 dict，由调用方决定是否告警 —— 不能静默降级。
    """
    try:
        groups = load_measurements(mdb_path, table)
    except Exception as e:
        log(f"WARN 读 MDB 失败（本次无轴位）: {e}")
        return {}

    key = (str(family or "").strip(), str(given or "").strip(),
           str(exam_date or "").strip()[:8])
    rows = groups.get(key)
    if not rows:
        return {}
    row = pick_latest(rows)
    out = {}
    for side, s in (("R", "r"), ("L", "l")):
        ax = row["eyes"][s].get("axis")
        if ax is not None:
            out[side] = int(round(ax))
    return out


def mdb_mtime(path):
    """MDB 最后修改时间。None 表示不可达。"""
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def mdb_signature(path):
    """文件的「身份指纹」：(mtime_ns, size)。不可达返回 None。

    ★ 用 st_mtime_ns 而不是 st_mtime：秒级精度下，同一秒内的两次写入
      指纹相同，会被误判成「已稳定」。
    ★ size 一起比：Access 引擎原地改写页时 mtime 可能不变（某些 SMB/NFS
      挂载的 mtime 精度粗），但页数变化会体现在 size 上。
    """
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def wait_settled(path, settle=5.0, timeout=120.0, poll=1.0):
    """等 MDB 写完落盘，而不是读到写入的中间态。

    判定：连续 poll 秒内 mtime_ns 与 size 都没变，且已经稳定了 settle 秒。

    为什么需要这个
    --------------
    「mtime 变了」只说明有人碰过文件，不说明写完了。设备写 Access 库时
    mtime 先变、页还在刷；此时去 access_parser 解析，最坏情况会读出
    半写入的浮点字节，然后按错误度数出单推给 PACS。

    为什么不用 .ldb 锁文件
    ----------------------
    设备机器开着 .ldb 就不消失，它标记的是「库被 Access 引擎打开」，
    不是「正在写」。所以锁文件无法区分空闲与写入。

    返回
    ----
    (ok, sig, waited_seconds)
        ok    True  = 已稳定，可以读
              False = 超时仍未稳定（调用方决定是跳过还是硬读）
        sig   稳定时的 (mtime_ns, size)；超时返回最后一次观测值
    """
    t0 = time.time()
    prev = None
    prev_change = t0          # 最后一次观察到变化的时间
    sig = None

    while True:
        now = time.time()
        sig = mdb_signature(path)
        if sig is None:
            return False, None, now - t0

        if sig != prev:
            prev = sig
            prev_change = now
        elif (now - prev_change) >= settle:
            # 连续 settle 秒没变
            return True, sig, now - t0

        if (now - t0) >= timeout:
            return False, sig, now - t0

        time.sleep(poll)


# ---------------------------------------------------------------- 结构校验

# IOLMaster 500 的 IolmMeasurements 必需字段。缺任何一个都说明这不是
# 我们验证过的那套库结构，必须停下来而不是猜。
REQUIRED_COLUMNS = (
    "family_name", "given_name", "birth_date", "exam_date",
    "al_r", "al_l", "r1_r", "r1_l", "r2_r", "r2_l",
)

# 有这个字段才能出完整报告单（轴位、前房深度）
OPTIONAL_COLUMNS = ("acd_r", "acd_l", "axis_r", "axis_l", "guid",
                    "patient_sex")


def check_schema(mdb_path, table=MDB_TABLE):
    """校验库结构是否为本项目验证过的 IOLMaster 500 结构。

    返回 (ok, info)：
        ok    True 表示必需字段齐全
        info  {"table", "rows", "missing", "present_optional", "tables"}

    ⚠️ 这个检查存在的理由：
       IOLMaster 700 用的不是 IolmXP.MDB，而是 CZM_*.MDB 系列，表名列名
       完全不同。若把 700 的库指过来，access_parser 要么直接抛错、要么
       读出全 None，然后公式算出荒唐的度数 —— 而且不会报任何错。
       与其静默出错，不如一开始就说清楚「这不是我认识的库」。
    """
    from access_parser import AccessParser

    info = {"table": table, "rows": 0, "missing": [],
            "present_optional": [], "tables": []}
    ap = AccessParser(mdb_path)
    try:
        info["tables"] = sorted(ap.catalog.keys()) if hasattr(ap, "catalog") else []
    except Exception:
        pass

    # access_parser 在表不存在时会抛 AttributeError: 'NoneType' object has no
    # attribute 'parse' —— 那个报错完全看不出「表不存在」，所以这里自己接住。
    try:
        t = ap.parse_table(table)
    except AttributeError:
        t = None
    except Exception as e:
        info["error"] = str(e)
        t = None

    if not t:
        info["missing"] = list(REQUIRED_COLUMNS)
        info["error"] = info.get("error") or f"表 {table} 不存在"
        return False, info

    cols = set(t.keys())
    info["missing"] = [c for c in REQUIRED_COLUMNS if c not in cols]
    info["present_optional"] = [c for c in OPTIONAL_COLUMNS if c in cols]
    if "family_name" in t:
        info["rows"] = len(t["family_name"])
    return (not info["missing"]), info


def assert_supported(mdb_path, table=MDB_TABLE):
    """结构不符就抛错，附上排查指引。

    在 pipeline 开跑前调用 —— 宁可一条都不生成，也不能拿错误的结构
    往 PACS 里推一堆度数不对的晶体单。
    """
    ok, info = check_schema(mdb_path, table)
    if ok:
        return info

    lines = [
        "库结构与本项目验证过的 IOLMaster 500 结构不符，已停止。",
        f"  库文件   : {mdb_path}",
        f"  表       : {table}",
    ]
    if info.get("error"):
        lines.append(f"  原因     : {info['error']}")
    if info["tables"]:
        lines.append(f"  存在的表 : {', '.join(info['tables'][:12])}"
                     + (" …" if len(info["tables"]) > 12 else ""))
    if info["missing"]:
        lines.append(f"  缺少字段 : {', '.join(info['missing'])}")
    lines += [
        "",
        "本项目仅对 Carl Zeiss IOLMaster 500（软件 V. 5.5）测试过。",
        "IOLMaster 700 使用 CZM_*.MDB 系列，结构完全不同，本项目未做适配。",
        "若确认要在其他机型上使用，请先完成 DISCLAIMER.md 列出的四项核对，",
        "并把表名/字段映射写进 config.yaml 后再运行。",
    ]
    raise RuntimeError("\n".join(lines))
