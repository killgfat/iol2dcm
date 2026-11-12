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


def _guid_dt(guid):
    """从 guid 第 11 段解析出的 datetime。取不到返回 None。

    格式：``YYMMDDHHMMSSmmm``（15 位数字，毫秒级本地时间）。

    ★ 实测这是**原始采集**时刻，不随 exam_date 被改写而变：
      同一个 guid 在表里可以对应多个 exam_date，
      说明设备会为了「把这次测量挂到另一个检查日期下」而新增一行，
      数据也重新分析过（AL 每次微调）。所以「guid 日期」回答的是
      「这条数据是哪次采集的」，`exam_date` 回答的是「挂在哪次就诊下」。
    """
    try:
        p = str(guid).split(".")
        if len(p) != 13:
            return None
        s = p[10]
        if len(s) != 15 or not s.isdigit():
            return None
        return datetime.strptime(s, "%y%m%d%H%M%S%f")
    except (ValueError, OSError, OverflowError):
        return None


def measure_time(guid):
    """从 guid 里取这行记录的**原始采集**时刻（epoch 秒）。取不到返回 None。

    guid 形态（应以脱敏 fixture 验证）：

        1.2.276.0.75.2.1.10.0.1.{YYMMDDHHMMSSmmm}.{patient_key}.{seq}
                                   └─ 第 11 段（下标 10），毫秒级本地时间

    ★ 这是「这次采集是什么时候做的」，不是「这行记录最后一次被改」，
      也不是「这次就诊的日期」。它能回答「这批数据有多旧」，不能回答
      「最后一次动过是什么时候」—— 后者只能靠内容指纹（见
      pipeline._fingerprint）每轮观测出来。

    ★ 时区：设备写的是机器本地时间，这里按本地时区解释，
      只用于算「距今多久」，不参与任何跨机器比对。

    解析失败（guid 格式变了 / 不是这台机器的库）返回 None，
    调用方必须把 None 当成「时间未知」而不是「很久以前」。
    """
    dt = _guid_dt(guid)
    return dt.timestamp() if dt is not None else None


def measure_date(guid):
    """guid 里那 8 位日期（YYYYMMDD）。取不到返回空串。

    用来判断该行采集日期是否与 exam_date 相同；改挂业务日期时两者可能不同。
    """
    dt = _guid_dt(guid)
    return dt.strftime("%Y%m%d") if dt is not None else ""


def row_has_measurement(row):
    """这一行里有没有任何可录入的测量值（AL，或成对的 r1+r2）。

    ★ 判据必须与 formulas.calc.has_measurement() 完全一致。
      取数层先判一次，是因为**选行要在 _record() 之前做**：_record()
      会把「既无实测值也无眼状态」的眼丢掉，之后再看 rec["eyes"] 就
      分不清「这行本来空」和「被组装丢成空」。

      MDB 里未测出存的是 0，num() 已经把 0 折成 None（见 core.utils.num），
      所以这里的 or 判空是可靠的，不会把 0.0 当成"有值"。
    """
    for e in (row.get("eyes") or {}).values():
        if e.get("al") or (e.get("r1") and e.get("r2")):
            return True
    return False


def row_is_on_exam_date(row):
    """这一行的 guid 日期是否等于它的 exam_date（真实本地检查日）。

    ★ 取不到 guid 日期时返回 True —— 「无法判定」必须按**合格**处理。
      若返回 False，那么 guid 格式一旦变化，所有行都会被判成「不是当天
      建的」，于是永远选不到 be_own 档，一路掉进「整组无可用行」而
      把每一例都拦成人工待办。方向必须偏向「放行」。
    """
    d = measure_date(row.get("guid"))
    if not d:
        return True
    return d == str(row.get("exam_date") or "").strip()[:8]


def pick_latest(rows):
    """同一天同一患者的多行里，取 guid 时刻**最新的一行**。

    ★ 不能用 rows[-1]。Access 表的物理顺序不等于写入顺序：被改过的行
      会被挪到别处，删过的行留下空洞，不能据此判断创建先后。

    判据：guid 内嵌时刻。全部取不到时退回物理末行。

    ⚠️ **这个口径会选到「当天新建但没测出值」的空行。**
       当天空行比历史有值行更晚创建时，仅按时刻选择会忽略已推内容的来源。

       **管线内部一律用 pick_row()**。本函数保留原样，是给外部脚本
       的兼容契约：它的行为不能变，
       否则那些脚本会在升级后跟着改变选行结果。
    """
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]
    return max(enumerate(rows),
               key=lambda ir: (ir[1].get("measure_ts") or 0, ir[0]))[1]


# 选行结论。**只回答「能不能出单」**，唯一的例外是 PICK_PUSHED。
#   PICK_OWN    当天采集、有测量值         → 可以出单
#   PICK_OTHER  当天有这条记录但没测出值   → 本次就诊尚无数据
#   PICK_STALE  组内没有当天的行，只有别日期的数据 → **不许自动出单**
#   PICK_EMPTY  整组没有任何可录入的测量值
#   PICK_PUSHED 已推记录的**来源行**（already_pushed=True 专用）
#               → 「能不能出单」这个问题不适用，调用方也不该判断
#
# ★ 为什么 PICK_PUSHED 必须单独一个值，不能复用 PICK_OWN / PICK_STALE：
#   已推的组里，「PACS 里那份是哪一行推出去的」和「这次就诊该用哪一行出单」
#   是两个问题。来源行可以早于 exam_date，不能据此否定它是已推内容的来源。
#   若复用 PICK_STALE，安全性就取决于调用方有没有多写一句
#   `and not isinstance(done_record, dict)` —— 那是跨文件的隐式耦合，
#   谁改一处就会把已推病例错误地拖进人工待办。用独立值让代码自己说清楚。
PICK_OWN, PICK_OTHER, PICK_STALE, PICK_EMPTY, PICK_PUSHED = (
    "own", "other", "stale", "empty", "pushed")


def pick_row(rows, already_pushed=False):
    """组内取「这一例该用哪一行」，并给出这行能不能直接出单的结论。

    返回 (row, pick)，pick 是 PICK_* 之一。

    ★ 两个调用者问的是**两个不同问题**，所以有一个开关：

      already_pushed=False  「这次就诊该用哪一行出单？」
          必须先满足「这条测量是这次就诊当天采的」（guid 日期 == exam_date）。
          组里没有当天的行 → 返回 PICK_STALE，**不许推**：那意味着手上只有
          几个月前的数据，按今天的日期出单会把旧度数当成新检查。
          返回值 ∈ {OWN, OTHER, STALE, EMPTY}。

      already_pushed=True   「PACS 里那份内容是哪一行推出去的？」
          这条只用于比对指纹、补写指纹或判陈旧，**永远不会触发推送**。
          空行产不出实例，所以当年推的必然是有测量值的那行 —— 优先取它。
          返回值 ∈ {PUSHED, EMPTY}，**永远不返回 OWN / OTHER / STALE**：
          那几个值回答的是「能不能出单」，这条路径不回答这个问题。
          若整组都空，说明内容确实被清空了（PICK_EMPTY）。

    为什么不能一律「有测量值优先」
    ----------------------------
    当天行为空、历史行有值时，不能将历史眼轴/曲率按今天的日期出单。
    当天行有值时应走 PICK_OWN；当天行为空则明确返回 PICK_OTHER。
    已推内容的来源判断独立于上述出单规则。

    组内多行到底是什么
    ------------------
    同一个 guid 可以对应多个 exam_date：设备可将同次采集改挂到另一个
    业务日期，并重新分析数据。因此跨日期记录的数据不一定完全相同。

    排序键
    ------
    already_pushed=True  ：有测量值 → 当天 → guid 时刻最新 → 下标靠后
                           （结论固定是 PUSHED 或 EMPTY，见上）
    already_pushed=False ：先在「当天的行」里按 有值 → 最新 → 下标 选；
                           当天一行都没有时，退回全组同样排序（结论是
                           PICK_STALE，除非整组无值）。
    """
    if not rows:
        return None, PICK_EMPTY

    def rank(i, r):
        return (row_has_measurement(r),
                row_is_on_exam_date(r),
                r.get("measure_ts") or 0,
                i)

    if already_pushed:
        _, row = max(enumerate(rows), key=lambda ir: rank(*ir))
        if not row_has_measurement(row):
            return row, PICK_EMPTY
        # ★ 不判 row_is_on_exam_date：这条路径问的是「PACS 里那份来自哪行」，
        #   不是「这行是不是当天采的」。来源行可能早于 exam_date，
        #   若在这里返回 STALE，调用方就得靠额外条件才不会被误判成陈旧数据。
        return row, PICK_PUSHED

    same = [(i, r) for i, r in enumerate(rows) if row_is_on_exam_date(r)]
    pool = same or list(enumerate(rows))
    i, row = max(pool, key=lambda ir: rank(*ir))
    if not row_has_measurement(row):
        return row, (PICK_OTHER if same else PICK_EMPTY)
    return row, (PICK_OWN if same else PICK_STALE)


def pick_best(rows):
    """兼容壳：只要行、不要结论。等价于 pick_row(rows)[0]。

    ★ 保留这个名字是因为现场的 gapfill 脚本可能引用过；管线的真实口径
      一律用 pick_row()，因为**选行结论决定能不能推**，不能丢。
      不要用它做「该推哪一行」的判断 —— 它不知道这一例是否已推过。
    """
    return pick_row(rows)[0]


def load_measurements(mdb_path, table=MDB_TABLE):
    """读 IolmXP.MDB 的 IolmMeasurements 表，按 (姓, 名, 检查日期) 分组。

    返回 {(fam, giv, date): [row, ...]}，同一天多次检查按出现顺序。

    每行：
        guid / dob / exam_date / sex / measure_ts / measure_date
        eyes: {r: {al, r1, r2, acd, axis, state}, l: {...}}

    sex 原样保留 MDB 的 patient_sex 值，不做映射 —— 归一化在
    core.patient.norm_sex() 里做，取数层不猜语义。

    测量字段可部分缺失。AL、角膜半径与 ACD 分别判断可用性；
    lens_thickness_*、used_r / used_l 不参与本项目计算。
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
    # patient_sex 可能为空，但字段确实存在。
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
            # 这一行的**原始采集**时刻（epoch 秒），取不到为 None。
            # 见 measure_time()：它不随 exam_date 改写而变。
            "measure_ts": measure_time(c_guid[i]),
            # guid 里的 8 位日期，用来判「是不是当天建的」。取不到为空串。
            "measure_date": measure_date(c_guid[i]),
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
    # ★ 与管线同口径：轴位必须从「实际出单用的那一行」取，
    #   否则会出现「报告单的轴位来自空行、度数来自有数据行」。
    row = pick_row(rows)[0]
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
