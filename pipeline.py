# -*- coding: utf-8 -*-
"""iol2dcm 唯一入口：MDB → 四公式计算 → 晶体单 + OAM → DICOM → PACS。

装完即跑，不需要单独的入口脚本。systemd 直接执行：
    python3 /opt/iol2dcm/pipeline.py

子命令（排障与运维用，日常不需要）：
    pipeline.py watch        常驻监视 MDB（systemd 就用这个，也是无参默认）
    pipeline.py run          跑一轮后退出（cron / 补数）
    pipeline.py constants    抓取 IOLCon 常数 → JSON
    pipeline.py lenses       列出上表型号与常数
    pipeline.py check        自检各环节
    pipeline.py render       只渲染到本地 PNG（不推 PACS）

触发方式
--------
**只监视 MDB 的 mtime**。
不碰 `.iol` 文件、不做目录轮询。

为什么不监视 .iol
    设备每次检查写一个 .iol，但 .iol 里**没有散光轴位**，且公式表是厂商算的。
    MDB 是设备活库，一次检查一行、含轴位，而且同一个患者可能有多次检查
    需要各自成单。以 MDB 为唯一数据源更干净。

轮询节奏
--------
    每 interval 秒 stat 一次 MDB（成本极低）
    mtime 变了 → 跑一轮
    failed 有到期的重试 → 也跑一轮（MDB 没变也跑）
    每轮有 max_seconds 预算，避免积压几千例时新检查被饿死

PACS 未就绪
-----------
    开机顺序不受控：DICOM 服务端常常比本管线晚起来。目标不可达时**原地
    守候**（每 watchdog.probe_seconds 探测 TCP + C-ECHO），一通就补推，
    不退出、不跳过、不消费签名。等多久都不丢 —— 每轮都按全库重新筛选待办。

断点续跑
--------
状态文件记录已成功推送的 (姓名|生日|检查日期)。失败的进 failed 队列按
指数退避重试（60s → 15min 封顶）；确定失败（如库里没生日）标 permanent 不再
重试。DICOM 目标整个不可达时本轮什么都不消费，签名不推进，目标一通就补推。
状态每 25 例原子写一次，中途断电最多丢 25 例的进度。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import json
import os
import sys
import threading
import time
from datetime import datetime

# pipeline.py 位于项目根（不在任何包内），把自身目录加进搜索路径
# 才能 import 同级的 core / sources / formulas / render / dicomlib
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import dicomlib.dicom as dicom
import dicomlib.keratometry as keratometry
import dicomlib.oam as oam
import formulas.calc as calc
import render.report as report
import render.sheet as sheet
import sources.constants as constants
import sources.mdb as mdb
from core import config, patient
from core.config import build as build_config
from core.utils import FORMULA_ORDER, digest, log, log_label

__version__ = "1.3.1"

# 一次检查生成哪些实例。改动实例结构时必须提升这个串 ——
# state 的签名含它，否则老病例不会被重推，新加的那路 series 永远补不上。
#   1.0.0  SC×N + OAM
#   1.1.0  SC×N + OAM（含 ACD 段测量）+ KER
#   1.2.0  推送目标不可达时原地守候，单例失败退避
#   1.3.0  缺 AL 或缺 K 时不出晶体单，只发 OAM/KER；不再因缺一项丢整眼
INSTANCE_LAYOUT = "sc,oam+acd,ker;partial-eyes"
# ★ 1.3.1 没有改实例结构（仍是 SC×N + OAM + KER），所以这个串**不动**：
#   动它会让 state 整体作废、全量重推。本版新增的是「按内容
#   指纹判重」和「冷却期」，属于调度策略，不影响已推实例的内容。

# ⚠️ 适用范围：仅对 Carl Zeiss IOLMaster 500 测试过。详见 DISCLAIMER.md
SUPPORTED_DEVICE = "Carl Zeiss IOLMaster 500"
SCOPE_WARNING = (
    f"适用范围：本项目仅对 {SUPPORTED_DEVICE} 测试过。"
    "IOLMaster 700 及其他型号/厂商设备未经测试，不做任何可用性保证。"
)


def _workers(cfg, args):
    """本轮并发进程数。

    优先级：命令行 --workers > config.yaml performance.workers > 默认 4。

    ★ 为什么默认 4 而不是 CPU 数：瓶颈是 C-STORE 网络等待，不是 CPU
      （实测 8 进程只比 4 进程快 1.27×，因为推送成了瓶颈）。4 进程已经
      把单例从 406ms 压到 ~275ms，且对 PACS 的并发连接压力可控。

    ★ 为什么用进程而不是线程：优化掉 PNG 中转后，剩下的全是 FreeType
      文本绘制 + JPEG 编码 + pydicom 封装，**一点 GIL 都不放**。实测
      线程池 0.83×（比串行还慢），进程池 4.13×。

    返回 1 表示串行（排障、dry-run、单核机器）。
    """
    w = getattr(args, "workers", 0) or 0
    if not w:
        w = int((cfg.get("performance") or {}).get("workers") or 0)
    if not w:
        w = 4
    w = max(1, int(w))
    if args.dry_run:
        return 1          # dry-run 不推送，进程池开销反而亏
    return w


def _save_state(path, state):
    """原子写：先写 .tmp 再 rename，防中途断电写出半个 JSON。"""
    try:
        d = os.path.dirname(path) or "."
        os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        # ★ state.json 里有姓名与生日，按 0600 建。先 open 再 chmod 会有
        #   一个窗口期是 0644，所以用 os.open 一步到位。
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as e:
        log(f"WARN 状态落盘失败: {e}")


def _record(row, name):
    """MDB 一行 → 管线内部记录。

    K 值在这里算好（calc 里也用，但报告单表头要显示 r 而不是 K，
    两处都留着免得来回换算）。
    """
    rec = {
        "name": name,
        "dob": row.get("dob") or "",
        "date": row.get("exam_date") or "",
        "sex": patient.norm_sex(row.get("sex")),
        "operator": "",
        "eyes": {},
    }
    for side, s in (("OD", "r"), ("OS", "l")):
        e = row["eyes"].get(s) or {}
        al, r1, r2 = e.get("al"), e.get("r1"), e.get("r2")
        acd, axis = e.get("acd"), e.get("axis")
        state = e.get("state") or ""

        # ★ 缺一项**不再丢整眼**。
        #   以前 al/r1/r2 任一为 None 就 continue，于是「只测了眼轴、没测
        #   角膜曲率」的检查连 AL 这个确定的实测值都一起丢了 —— 晶体度数
        #   确实算不出来（calc 自己会判），但结构化数据（OAM 的眼轴、
        #   眼状态）本来录得进去。缺不缺、缺哪一项，交给下游各自判断：
        #     · calc  → 缺 al/r1/r2 就给 None，格子显示「该公式未计算」
        #     · oam   → 有 al 才写该眼
        #     · ker   → 有 r1+r2 才写该眼
        #     · SC    → 整例一个度数都算不出就不出单（见 _process_one）
        k1 = calc.k_from_radius(r1)
        k2 = calc.k_from_radius(r2)

        # 一眼什么实测值都没有时不留空壳（有些行全是 0/空，是操作员开着
        # 界面没测就保存的）。眼状态是录入项，单独有它也不算「测量」，
        # 但 OAM 的 Lens Status 要用它，所以只有状态时仍保留该眼。
        if al is None and k1 is None and k2 is None \
                and acd is None and not state:
            continue

        rec["eyes"][side] = {
            "al": al,
            "r1": r1,
            "r2": r2,
            "k1": k1,
            "k2": k2,
            "acd": acd,
            "axis": axis,
            "state": state,
        }
    # 晶状体/玻璃体状态：OAM 的 Lens Status / Vitreous Status 要用。
    # 单独放顶层，不进 eyes —— 它是整眼状态而非测量值。
    rec["state_r"] = (row["eyes"].get("r") or {}).get("state") or ""
    rec["state_l"] = (row["eyes"].get("l") or {}).get("state") or ""
    # 本行在设备上的创建时刻（epoch 秒），冷却期要用。None = 时间未知。
    rec["measure_ts"] = row.get("measure_ts")
    return rec


# 冷却期默认值（秒）。只在这里定义一处，config 缺字段时用它。
DEFAULT_COOLING_SECONDS = 1200

# 注：这里曾经有个 MAX_COOLING_SPIN「防空转」开关，已删除。
# 它数的是**正常等待**轮数（没有新数据/没有到期重试/冷却未到期），
# 于是默认 20 分钟冷却下，第 5 个 interval（约 300s）就会把 cool_until
# 清掉 —— 等到第 1200s 真正到期时，唯一的唤醒理由已经没了，病例饿死。
# 真正的空转源头（冷却到期但案件进不了待办）改用下面的两处结构性处理：
#   1. commit 失败时把该 key 从 cooling 移除（退避调度接管）；
#   2. loop 里「冷却触发了但本轮毫无进展」时按 interval 退避，而不是删除信号。


def _cooling_seconds(cfg):
    """生效的冷却秒数。0 = 关闭。

    ★ 显式配 0 要能被区分出来：`or 0` 会把 0 和"缺字段"混为一谈，
      导致想关的人关不掉、没配的人也不生效。这里用 key 是否存在判断。
    """
    w = cfg.get("watcher") or {}
    if "cooling_seconds" not in w:
        return float(DEFAULT_COOLING_SECONDS)
    try:
        v = float(w.get("cooling_seconds"))
    except (TypeError, ValueError):
        log(f"⚠ watcher.cooling_seconds 不是数字（{w.get('cooling_seconds')!r}），"
            f"按默认 {DEFAULT_COOLING_SECONDS}s 处理")
        return float(DEFAULT_COOLING_SECONDS)
    return max(0.0, v)


def _fingerprint(rec):
    """一条检查「原始测量值」的内容指纹（16 位十六进制）。

    ★ 为什么需要它
      设备对同一次检查补测时**改写同一行、guid 不变**，MDB 签名变了、
      也扫到了这行，但 state 里的幂等键是「姓|名|检查日期」，不含内容
      —— 命中 done 就再也不会被处理，病例永久停在半成品状态。

      指纹只由**实测值**构成，所以：
        · 没补测 → 指纹不变 → 不重推（幂等照旧）
        · 补测过 → 指纹变了 → 能立刻认出来

    ★ 为什么不含计算结果/型号/常数
      那些变了由 state 的 sig 负责（_load_state 整体重置）。指纹要回答
      的是「设备那边的实测数据变了没有」，混进派生量会让「换型号」和
      「补测」混为一谈。

    ★ 浮点按 4 位小数
      MDB 同一份字节每次读出来的 float 是一致的；4 位（0.0001 mm）
      远细于任何有临床意义的差异，又能吸收设备重算带来的末位抖动。
    """
    def f(v):
        # 4 位小数（0.0001 mm）远细于任何有临床意义的差异，又能吸收
        # 设备重算/浮点表示带来的末位抖动。注意它不是严格的误差容忍
        # 区间：值恰好落在舍入边界两侧时，极小扰动仍会改变指纹 ——
        # 那是可接受的（宁可多重推一次，也不要漏掉真实补测）。
        return "" if v is None else f"{float(v):.4f}"

    # ★ sex 必须纳入：PatientSex 会写进 SC/OAM/KER 三个实例
    #   （dicomlib 里 norm_sex(rec["sex"])），性别被修正后内容确实变了。
    #   以前只比测量值，性别改动会被 old == fp 分支静默跳过，audit 也
    #   查不出来。operator 目前由 _record() 固定为 ""、不从 MDB 读，
    #   等以后真的读了这个字段再一起纳入。
    parts = [rec.get("name") or "", rec.get("dob") or "",
             rec.get("date") or "", str(rec.get("sex") or "")]
    for side in ("OD", "OS"):
        e = rec["eyes"].get(side) or {}
        parts.append(",".join(f(e.get(k)) for k in
                              ("al", "r1", "r2", "acd", "axis")))
        parts.append(str(e.get("state") or ""))
    return digest(*parts)


def _expected_instances(rec, lenses):
    """按这份数据**本来该发**几个实例。

    ★ 与 _process_one 里的 expected 同口径，抽出来是为了两处不跑偏：
      _process_one 用它记账，stale 检测用它告诉人「本该 6 个，实际只推了 1 个」。
    调用前 rec 必须已经过 calc.calc_record（calcable 依赖计算结果）。
    """
    n = len(lenses) if calc.calcable(rec) else 0
    if any(e.get("al") for e in rec["eyes"].values()):
        n += 1                                   # OAM
    if any(e.get("r1") and e.get("r2") for e in rec["eyes"].values()):
        n += 1                                   # KER
    return n


def _process_one(task, cfg, lenses, meta, push_target, dry_run):
    """单例全流程。**模块级函数**（不是闭包）—— 进程池要 pickle 它。

    与旧版 run_once 内联循环的唯一区别：不碰 state、不写日志之外的全局，
    返回 (key, ok, info, errs, log_lines) 由调用方统一记账。
    日志先在子进程里攒成行，回主进程按顺序打 —— 避免多进程交错。

    返回 (key, ok, info, errs, lines)：
        ok=True  -> info 是要写进 state["done"] 的 dict
        ok=False -> info 是要写进 state["failed"] 的 dict
    """
    date, key, rec, fp = task
    target, step = meta["target_refraction"], meta["step"]
    name = rec["name"]
    lines = []

    # ★ PatientID 纯本地确定性生成：同样的姓名+生日永远得到同样的 ID，
    #   重复推送幂等，不需要查任何 PACS 接口。
    pid = patient.make_pid(rec["dob"], name)
    if not pid:
        lines.append(f"SKIP(无生日，无法生成 PatientID) {name} {date}")
        # permanent：库里就没生日，重试一万次也不会有
        # ★ 所有失败返回都要带 fp：commit() 靠它判断「这次失败是不是同一版
        #   内容」，同内容才合并接收历史。少了 fp，一次生成失败就会把
        #   之前已发出的 sent_uids/maybe_uids 冲掉。
        # ★ perm_fp = 「造成这份永久失败的输入」，与 fp（上次发送的版本）
        #   是两个不同的东西。只写 fp 的话，这条记录下一轮会被当成
        #   「输入未知」而反复重新评估，永久失败形同虚设；而带接收历史的
        #   情况还会被无谓转成人工待确认。
        return key, False, {"t": _now(), "date": date,
                            "fp": fp, "perm_fp": fp,
                            "err": "无生日，无法生成 PatientID",
                            "permanent": True}, [], lines

    # 计算
    try:
        calc.calc_record(rec, lenses, target=target, step=step)
    except Exception as e:
        lines.append(f"✗ 计算失败 {name} {date}: {e}")
        # permanent：计算失败是确定性的（常数表/型号/配置问题），
        # 重试只会每轮刷同一条日志。改配置或型号后 state 签名变化会重置。
        return key, False, {"t": _now(), "date": date,
                            "fp": fp, "perm_fp": fp,
                            "err": f"计算失败: {e}"[:200],
                            "permanent": True}, [], lines

    # ---------------- 渲染 + 封装 ----------------
    # ★ 晶体单与结构化实例的取舍：
    #     度数算得出  → 晶体单 SC × N + OAM + KER（照旧）
    #     度数算不出  → 只发 OAM / KER，**不出晶体单**
    #   算不出的原因是缺 AL 或缺 K。这时计算层返回全空（每格 None），
    #   出单只会得到一张满屏「该公式未计算」的空表 —— 而实测值本身
    #   （眼轴、角膜曲率、眼状态）是有意义的，必须照录。
    datasets, errs = [], []
    can_calc = calc.calcable(rec)

    # ★ expected 是「按这份数据本来该发几个」，sent 是「实际凑出来的」。
    #   两者必须分开：只拿动态计数当 expect 的话，某个实例生成失败（比如
    #   OAM 抛异常）会让 expect 自动变小，于是「全部生成成功」照样成立，
    #   病例被标 done —— 那个缺失的 series 永远补不上。
    # ★ 与 audit 共用同一个判据函数，避免两处口径跑偏（audit 说「本该
    #   6 个」而这里算出 5 个，会把人带进沟里）。
    expected = _expected_instances(rec, lenses)
    has_al_eye = any((e.get("al") for e in rec["eyes"].values()))
    has_k_eye = any((e.get("r1") and e.get("r2"))
                    for e in rec["eyes"].values())

    n_oam = n_ker = 0            # 实例数（0/1）
    oam_eyes = ker_eyes = 0      # 实例里实际写了几只眼
    if not can_calc:
        lines.append(f"  · {name} {date} 度数不可算（缺 AL 或缺 K），"
                     f"跳过晶体单，只发结构化实例")

    n_sc = 0
    for si, L in enumerate(lenses, start=1):
        if not can_calc:
            break
        try:
            struct = sheet.build(rec, L, target=target, step=step)
            # ★ 直接拿 Image 交给 build_sc，不走 PNG 中转
            #   （省 68ms PNG 编码 + 34ms 解码 + 一次磁盘往返，每张）
            img = report.render_image(struct, axes=_axes(rec),
                                      institution=_institution(cfg))
            datasets.append(dicom.build_sc(rec, img, cfg, pid, L,
                                           series_num=si, instance_num=1))
            n_sc += 1
        except Exception as e:
            errs.append(f"{L['name']}: {e}")
            lines.append(f"  ✗ {name} {date} {L['name']} 生成失败: {e}")

    # ★ series 号从「晶体单实际生成了几张」往下排，不能写死 len(lenses)+1：
    #   不出晶体单时那几号是空的，PACS 里会看到跳号的 series。
    next_series = n_sc + 1
    expect = n_sc          # 已凑出来的实例数，逐项累加

    # Ophthalmic Axial Measurements Storage：承载眼轴 + 前房深度 + 眼状态。
    # 按眼判断：缺 AL 的眼写不出合法的 per-eye item（Measurements Sequence
    # 是 Type 1），该眼整体不写；两眼都没有 AL 则整实例不发。
    if has_al_eye:
        try:
            study_uid = dicom.study_uid_for(pid, rec.get("date"))
            ds_oam = oam.build(
                rec, cfg, pid, study_uid,
                oam.series_uid_for(study_uid),
                oam.sop_uid_for(study_uid),
                series_num=next_series)
            if ds_oam is not None:
                datasets.append(ds_oam)
                n_oam = 1
                oam_eyes = sum(1 for s in ("OD", "OS")
                               if (rec["eyes"].get(s) or {}).get("al"))
                expect += 1
                next_series += 1
        except Exception as e:
            errs.append(f"OAM: {e}")
            lines.append(f"  ✗ {name} {date} OAM 生成失败: {e}")

    # Keratometry Measurements Storage：K1/K2、曲率半径 r1/r2、散光轴位。
    # 这三项在 OAM IOD 里没有位置（K 有独立的 78.3 SOP Class），
    # 所以单独一个 series。缺 r 的眼不进序列，两眼都缺则不生成。
    if has_k_eye:
        try:
            study_uid = dicom.study_uid_for(pid, rec.get("date"))
            ker = keratometry.build(
                rec, cfg, pid, study_uid,
                keratometry.series_uid_for(study_uid),
                keratometry.sop_uid_for(study_uid),
                series_num=next_series)
            if ker is not None:
                datasets.append(ker)
                n_ker = 1
                ker_eyes = sum(1 for s in ("OD", "OS")
                               if (rec["eyes"].get(s) or {}).get("r1")
                               and (rec["eyes"].get(s) or {}).get("r2"))
                expect += 1
                next_series += 1
        except Exception as e:
            errs.append(f"KER: {e}")
            lines.append(f"  ✗ {name} {date} 角膜曲率生成失败: {e}")

    if dry_run:
        lines.append(f"DRY {name} {date} pid={pid} "
                     f"{len(datasets)}/{expected} 实例就绪")
        return key, True, {"dry": True, "pid": pid, "n": len(datasets)}, errs, lines

    if not datasets:
        # ★ 区分两件事，别把「生成失败」当成「本来没数据」记进 done：
        #   · 本该有实例却一个都没生成（渲染/封装全炸）→ 失败，按退避重试
        #   · 确实没有任何可录入的测量值（两眼既无 AL 也无 r）→ 跳过
        if can_calc or calc.has_measurement(rec):
            lines.append(f"✗ {name} {date} 实例全部生成失败")
            # ★ 必须带 fp：commit() 靠它判断「这次失败是不是同一版内容」，
            #   同内容才把之前的 sent_uids/maybe_uids 合并过来。少了 fp，
            #   一次生成失败就会把「旧内容已在 PACS 里」这段历史冲掉，
            #   之后内容一变就按普通失败重试 → 同 UID 被静默去重。
            return (key, False,
                    {"t": _now(), "date": date, "fp": fp,
                     "err": ("实例全部生成失败: "
                             + "; ".join(errs))[:200]}, errs, lines)
        lines.append(f"SKIP(无可录入的测量值) {name} {date}")
        return key, True, {"t": _now(), "pid": pid, "date": date,
                           "src": "empty", "n": 0}, errs, lines

    # 一次 C-STORE 推完整 study（同关联，省握手）
    sent = 0
    sent_uids = []           # 已被 PACS **确认接收**的 SOP Instance UID
    maybe_uids = []          # 可能已接收（响应丢失，无法确定）
    uncertain = False        # 有任何实例的结果不确定
    try:
        res = dicom.push_cstore(datasets, push_target["host"],
                                push_target["port"], push_target["aet"],
                                timeout=120, prefer_jpeg=True)
        for sop, good, msg, known in res:
            uid = str(sop)
            if good:
                sent += 1
                sent_uids.append(uid)
            elif not known:
                # ★ 空响应/无应答：PACS 很可能已经存下了这个实例。
                #   绝不能当成「明确没收到」—— 否则内容变更后会普通重试，
                #   同 UID 被静默去重，账目记成新版成功、PACS 还是旧内容。
                uncertain = True
                maybe_uids.append(uid)
                errs.append(f"{uid[-12:]} {msg}(结果未知)")
                lines.append(f"  ? {name} {date} 实例结果未知 {msg}")
            else:
                errs.append(f"{uid[-12:]} {msg}")
                lines.append(f"  ✗ {name} {date} 实例推送失败 {msg}")
    except Exception as e:
        # 异常时整批都可能已经到达对方，无法区分
        uncertain = True
        maybe_uids.extend(str(ds.SOPInstanceUID) for ds in datasets)
        errs.append(f"推送异常: {e}")
        lines.append(f"  ✗ {name} {date} 推送异常: {e}")

    # ★ 部分成功 / 异常：把「这次发的是哪一版内容」记进 failed。
    #   否则设备在退避期间补测后，下次重试会带着**新内容**、用**同一批
    #   SOP UID** 再推一遍 —— PACS 保留首个（旧内容）并返回成功，于是
    #   state 记「6 实例齐全 + 新指纹」，PACS 里还是旧数据，而且 done.fp
    #   已经是新的，连 audit 都失去发现依据。这与本次事故是同一个坑，
    #   只是入口从 done 换成了 failed。
    # ★ sent_uids + maybe_uids 一起表示「这一版内容可能已经有一部分在
    #   PACS 里了」。只要非空，内容一变就不能普通重试（同 SOP UID 会被
    #   静默去重，导致账目与 PACS 不一致）。
    partial = {"t": _now(), "date": date, "fp": fp,
               "sent_uids": sent_uids, "maybe_uids": maybe_uids,
               "sent": sent, "expect": expect,
               "uncertain": uncertain}

    if sent == expected and expect == expected:
        # state 里记**实际**发了什么：缺 AL 或缺 K 的病例没出晶体单，
        # 只写 lenses 会让人以为出过单。
        # ★ fp 记的是**这一版内容**的指纹，与 done 一起落盘。
        #   下一轮扫到同一 key 时比对：一样就跳过，不一样说明设备补测过。
        info = {"t": _now(), "pid": pid, "src": "generated", "date": date,
                "sc": n_sc,
                "lenses": [L["name"] for L in lenses][:n_sc],
                "oam_eyes": oam_eyes, "ker_eyes": ker_eyes,
                "instances": expected, "fp": fp}
        detail = [f"SC×{n_sc}"] if n_sc else []
        if n_oam:
            detail.append(f"OAM({oam_eyes}眼)")
        if n_ker:
            detail.append(f"KER({ker_eyes}眼)")
        lines.append(f"OK {name} {date} pid={pid} {expected} 实例齐全"
                     f"（{' + '.join(detail)}）")
        return key, True, info, errs, lines

    if expect < expected:
        # ★ 生成阶段就少了实例（渲染/封装抛异常）。这些**不能**当成
        #   「数据本来就没有」—— 否则病例被标 done，缺的 series 永远补不上。
        #   按退避重试，PACS 恢复/字体修好后能补齐。
        lines.append(f"✗ {name} {date} 实例不齐 {expect}/{expected}"
                     f"（生成阶段失败）")
        info = dict(partial)
        info["err"] = (f"实例不齐 {expect}/{expected}: "
                       + "; ".join(errs)[:160])
        return key, False, info, errs, lines
    if sent > 0:
        lines.append(f"⚠ {name} {date} 部分成功 {sent}/{expect}")
        info = dict(partial)
        info["err"] = f"部分成功 {sent}/{expect}: " + "; ".join(errs)[:160]
        return key, False, info, errs, lines
    lines.append(f"✗ {name} {date} 全部失败")
    info = dict(partial)
    info["err"] = "; ".join(errs)[:200]
    return key, False, info, errs, lines


def run_once(cfg, lenses, meta, state, args, push_target):
    """跑一轮。返回统计 dict。"""
    mdb_path = args.mdb or cfg.get("mdb_path")
    if not mdb_path or not os.path.exists(mdb_path):
        log(f"✗ MDB 不存在: {mdb_path}")
        return {"ok": 0, "fail": 0, "skip": 0, "total": 0, "blocked": "mdb"}

    # ★ 结构校验：确认这是本项目验证过的 IOLMaster 500 库结构。
    #   结构不符就停，不能拿错误的结构往 PACS 推度数不对的单子。
    try:
        mdb.assert_supported(mdb_path)
    except Exception as e:
        log(f"✗ {e}")
        return {"ok": 0, "fail": 0, "skip": 0, "total": 0, "blocked": "schema"}

    # ★ 推送目标先做可达性预检：连不上就别白算一遍再逐例失败，
    #   15000 例的活库里跑一轮要好几分钟，白算太浪费。
    #
    # ★ 但「不可达」不等于「这批数据从此不要了」：返回 blocked 让调用方
    #   知道本轮没消费 MDB 签名，下次（PACS 起来后）还会针对同一批数据
    #   重来。以前这里返回普通空统计，loop() 已经把签名记成「已处理」，
    #   于是这批检查再也不会被扫到 —— 开机时 PACS 慢一步就永久丢单。
    if not args.dry_run:
        ok, detail = _target_probe(push_target, _watchdog(cfg))
        if not ok:
            log(f"✗ DICOM 目标不可达 {push_target['host']}:"
                f"{push_target['port']}（{detail}）"
                f"（确认 config.yaml 的 push.dicom_host 或 --dicom-host）")
            return {"ok": 0, "fail": 0, "skip": 0, "total": 0, "blocked": "pacs"}

    target = meta["target_refraction"]
    step = meta["step"]

    # ★ 读库失败不能冒泡到 loop() 的 except —— 那只会打一行 traceback 然后
    #   整个循环继续，看不出「这一轮白跑了」。这里显式返回空统计，让调用方
    #   知道本轮什么都没做（state 不变，下轮重试）。
    try:
        groups = mdb.load_measurements(mdb_path)
    except Exception as e:
        log(f"✗ 读取 MDB 失败（本轮跳过，下轮重试）: {e}")
        return {"ok": 0, "fail": 0, "skip": 0, "total": 0, "blocked": "mdb"}

    # ---------------- 待办筛选 ----------------
    now = time.time()
    pol = _retry_policy(cfg)

    def _wait_left(fr):
        """还差多少秒到重试时间（<=0 表示现在就能试）。"""
        try:
            return float(fr.get("next") or 0) - now
        except (TypeError, ValueError):
            return 0.0

    # ★ 冷却期：一条检查从「首次测量」到「允许推送」要等多久。
    #   起点 = row["measure_ts"]（设备上这一行被创建的时刻，guid 内嵌）。
    #   注意它**不是**「距离最后修改」：设备补测是改写同一行、guid 不变，
    #   这个时刻也不会变。所以冷却保证的是「距首测至少 cooling_s」，
    #   不保证「每一版内容都稳定这么久」—— 后者要靠内容指纹 + audit。
    #   measure_ts 解析不出来（guid 格式变了）时，退回用
    #   state.cooling[key].first（这版内容首次被扫描看到的时刻）兜底。
    # ★ 默认值必须写在这里，不能只写在 config.yaml 模板里。
    #   已装的现场机器用的是**旧 config.yaml**（install.sh 默认保留现场
    #   配置，config.build 也不补这个字段），模板里的 1200 根本到不了
    #   运行时 —— 于是「升级后默认开冷却」变成「默认关」，这次修复对
    #   生产等于没生效。显式配 0 仍然表示关闭。
    cooling_s = _cooling_seconds(cfg)
    cooling = state.setdefault("cooling", {})
    stale = state.setdefault("stale", {})

    todo, retry_n, wait_n = [], 0, 0
    live = set()
    cooling_n = stale_n = backfill_n = 0
    for (fam, giv, date), rows in groups.items():
        key = f"{fam}|{giv}|{date}"
        live.add(key)                        # ★ 任何日期都算「库里还在」
        if args.since and date < args.since:
            continue
        # ★ 同一 (姓,名,日期) 可能有多行。取**最新的一行**，不能取表里的
        #   物理末行 —— Access 的物理顺序不等于写入顺序（改动过的行会被
        #   挪到别处）。实测样本库里有 2 例因此取到了空行、丢掉有数据的行。
        #   判据用 guid 内嵌的创建时刻；它缺失时退化为原来的「末行」。
        row = mdb.pick_latest(rows)
        name = f"{fam}{giv}" if _has_cjk(fam + giv) else f"{fam} {giv}".strip()
        rec = _record(row, name)
        log_name = log_label(name)
        fr = state["failed"].get(key)

        # ★★ 统一在这里清掉「从没真正推过实例」的历史 done，**必须早于
        #    本 key 的一切指纹比较**（包括下面「无测量值」那条路径）。
        #    这两个状态不是「PACS 里已有这个版本」：
        #      · dry-run 记录（旧 bug 写的 {"dry": true, ...}）
        #      · src="empty"（当时没有任何可录入的测量值，一个实例都没发）
        #    拿它们跟当前指纹比会出两种错：
        #      fp 相同 → 当成已完成，病例永久冻结；
        #      fp 不同 → 当成「已推旧内容」而无谓转 stale 等人处理，
        #                可它其实一次都没发过。
        #    以前清理只放在「有测量值」那条路径里，于是「dry done + 当前
        #    无测量值」会先在上面的 emptied 分支造出错误 stale 并 continue，
        #    永远走不到清理。所以这里提前，且 d 置 None —— 只从字典 pop
        #    而留着 d，后面的分支仍会把它当旧版本。
        d = state["done"].get(key)
        if isinstance(d, dict) and (d.get("dry") or d.get("src") == "empty"):
            log(f"  ↻ {log_name} {date} 清理历史 "
                f"{'dry-run' if d.get('dry') else 'empty'} 记录，重新处理")
            state["done"].pop(key, None)
            d = None

        # ★ 判据从「有没有眼」改成「有没有可录入的测量值」。
        #   以前 _record 缺一项就丢整眼，于是「有 AL 无 K」的检查在这里
        #   被判成没数据、整条跳过 —— 连眼轴都录不进 OAM。
        #   现在只要有一眼有 AL（→OAM）或 r1+r2（→KER）就照常处理，
        #   度数算不出来只是不出晶体单。
        if not rec["eyes"] or not calc.has_measurement(rec):
            # ★ 已推过的病例变成「无有效测量值」（测量值被清空、或同日
            #   新建了一条更空的记录被选中）：不能静默跳过。以前这里
            #   直接 continue，既不进 stale、audit 也不算 checked，看起来
            #   一切正常，而 PACS 里仍留着旧内容。
            # 到这里 d 一定不是 dry/empty（上面已清理并置 None）
            if isinstance(d, dict):
                fp0 = _fingerprint(rec)
                if d.get("fp") and d.get("fp") != fp0:
                    prev = stale.get(key) or {}
                    if prev.get("now_fp") != fp0:
                        stale_n += 1
                        log(f"  ⚠ {log_name} {date} 已推内容现在没有有效测量值"
                            f"（数据被清空？），PACS 里仍是旧版")
                    stale[key] = {
                        "t": _now(), "date": date, "name": name,
                        "was_fp": d.get("fp"), "now_fp": fp0,
                        "was": {k: d.get(k) for k in
                                ("instances", "sc", "oam_eyes", "ker_eyes")},
                        "now_instances": 0, "from": "emptied",
                    }
            # 库中这条检查确实没有任何可录入的测量值（只有眼状态/ACD，
            # 或全是 0）。重试也没用，但要留痕 —— 否则它会一直挂在
            # failed 里反复触发到期重试，每 interval 白读一次全库。
            fp_empty = _fingerprint(rec)
            if isinstance(fr, dict):
                # ★ 之前发出去过旧内容、现在数据被清空 → 必须人工处理。
                #   不能只标 permanent 了事：PACS 里那批实例还在。
                landed = bool(fr.get("sent_uids") or fr.get("maybe_uids")
                              or fr.get("uncertain"))
                # ★ 有接收历史时：fp 缺失 = 无法确认版本 → 也算「变了」，
                #   必须转人工。缺 fp 绝不能当成「和当前空记录相同」。
                fp_changed_or_unknown = (
                    landed and (fr.get("fp") is None
                                or fr.get("fp") != fp_empty))
                if fp_changed_or_unknown:
                    prev = stale.get(key) or {}
                    if prev.get("now_fp") != fp_empty:
                        stale_n += 1
                        log(f"  ⚠ {log_name} {date} 已推送过实例但数据被清空，"
                            f"PACS 里仍是旧版")
                    stale[key] = {
                        "t": _now(), "date": date, "name": name,
                        "was_fp": fr.get("fp"), "now_fp": fp_empty,
                        "was": {"instances": fr.get("expect"),
                                "sent": fr.get("sent"),
                                "sent_uids": fr.get("sent_uids"),
                                "maybe_uids": fr.get("maybe_uids"),
                                "uncertain": fr.get("uncertain")},
                        "now_instances": 0, "from": "failed",
                    }
                    state["failed"].pop(key, None)
                else:
                    # ★ fp 与 perm_fp 是**两个不同的东西**，不能共用一个字段：
                    #     fp      = 上次尝试发送的那一版内容（接收历史的基线）
                    #     perm_fp = 造成「永久失败」的那份输入
                    #   以前只有 fp，于是「A 版失败 → 清空（标 permanent）
                    #   → 恢复完全相同的 A 版」时 fp 没变，changed=False，
                    #   仍然 continue —— 而让 permanent 成立的原因（没有
                    #   测量值）其实已经消失了，病例又被冻结。
                    fr["perm_fp"] = fp_empty
                    fr["permanent"] = True
                    fr["err"] = fr.get("err") or "库中该检查无有效测量值"
                    fr["next"] = 0
            cooling.pop(key, None)
            continue

        # ---------------- 幂等键：内容指纹 ----------------
        # ★ 设备的幂等键是「姓|名|检查日期」，不含数据内容。设备对同一次
        #   检查补测时**改写同一行、guid 不变**，于是 MDB 签名变了、这行
        #   也扫到了，但 key 命中 done 就再也不会被处理 —— 病例永久停在
        #   半成品状态（生产 2026-11-10 已实际发生一次）。
        #   现在 done 里存 fp，比对的是「内容」而不是「这例处理过没有」。
        fp = _fingerprint(rec)
        if isinstance(d, dict):
            old = d.get("fp")
            if old == fp:
                stale.pop(key, None)
                cooling.pop(key, None)
                continue                     # 内容没变，真·已推过
            if not old:
                # ★ 老 state（本改动之前写的 done）没有 fp。这里把
                #   **当前**内容认作「已推过」，就地补指纹，**不重推**
                #   —— 否则升级即全量重推，而 PACS 会按同
                #   SOP UID 静默去重，白打一遍网络还给不出任何新内容。
                #   代价：升级前已经发生的补测查不出来（个别已
                #   人工修复）。要核对历史用 audit。
                d["fp"] = fp
                backfill_n += 1
                continue

        # ---------------- 人工处理状态（stale）优先于一切 ----------------
        # ★ stale 一旦写入，就是**持续生效**的人工闸门：不管它来自 done
        #   变更、来自 failed 的部分接收、还是来自「已推内容被清空」，
        #   只要内容还没变回 PACS 里已有的那一版，就绝不能再自动发送。
        #
        #   这是上一轮的漏洞：从 failed 转 stale 时删掉了 failed，下一轮
        #   该 key 既无 done 也无 failed → 直接进 todo → 发送成功并清掉
        #   stale，等于「只挡住一次扫描」。
        s_prev = stale.get(key)
        if isinstance(s_prev, dict):
            if s_prev.get("was_fp") == fp:
                # 内容又变回「之前那一版」→ 解除人工拦截。
                #
                # ⚠️ 但这**不等于**「PACS 里已有完整的那一版」：
                #    · 从 done 来的 stale：done.fp 曾记录过它，说明确实推成过
                #      完整版本（前面 done 判定已经处理了这条路径）。
                #    · 从 failed 来的 stale：was_fp 那一版**只部分接收**，
                #      甚至只是"可能接收"。此时必须把当时的接收历史
                #      挪回 failed，否则下一轮同内容重试若明确全失败，
                #      历史就丢了，之后内容一变又会普通重试、同 UID 去重。
                was = s_prev.get("was") or {}
                if s_prev.get("from") == "failed":
                    landed = bool(was.get("sent_uids") or was.get("maybe_uids")
                                  or was.get("uncertain"))
                    if landed:
                        state["failed"][key] = {
                            "t": _now(), "date": date, "fp": s_prev["was_fp"],
                            "err": "内容回到曾部分接收的版本，按退避重试补齐",
                            "tries": 0, "next": 0,
                            "sent_uids": list(was.get("sent_uids") or []),
                            "maybe_uids": list(was.get("maybe_uids") or []),
                            "sent": int(was.get("sent") or 0),
                            "uncertain": bool(was.get("uncertain")),
                            "expect": was.get("instances"),
                        }
                        log(f"  ✓ {log_name} {date} 数据已回到 PACS 现有版本，"
                            f"解除待确认（保留已接收记录，按退避补齐）")
                        stale.pop(key, None)
                        cooling.pop(key, None)
                        # ★ 必须留到下一轮：本轮刚恢复的 failed 记录还没被
                        #   读取（fr 是更早取的），直接放行会绕过退避，
                        #   而且失败时也拿不到刚恢复的历史。
                        continue
                    else:
                        log(f"  ✓ {log_name} {date} 数据已回到 PACS 现有版本，"
                            f"解除待确认")
                else:
                    log(f"  ✓ {log_name} {date} 数据已回到 PACS 现有版本，解除待确认")
                stale.pop(key, None)
            else:
                if s_prev.get("now_fp") != fp:
                    s_prev["now_fp"] = fp
                    stale_n += 1
                    log(f"  ⚠ {log_name} {date} 数据仍在更新，PACS 里是旧版，"
                        f"需人工确认后重推（已跳过自动发送）")
                cooling.pop(key, None)
                continue

        # ---------------- 失败退避 ----------------
        if isinstance(fr, dict):
            # ★ 上一次的尝试可能已经把**旧内容**的一部分送进了 PACS：
            #   确认接收（sent_uids）或结果未知（maybe_uids / uncertain）。
            #   内容一旦变化就绝不能按普通失败重试 —— 重试会用同一批 SOP
            #   UID 推新内容，PACS 保留首个（旧内容）却返回成功，账目记成
            #   新内容齐全、PACS 还是旧数据，连 audit 都查不出来。
            landed = bool(fr.get("sent_uids") or fr.get("maybe_uids")
                          or fr.get("uncertain"))
            fp_prev = fr.get("fp")           # 上次**尝试发送**的那一版
            perm_fp = fr.get("perm_fp")      # 造成「永久失败」的那份输入

            def _to_stale(why, was_fp):
                """把这一例转成人工处理，保留接收历史。"""
                nonlocal stale_n
                stale_n += 1
                log(f"  ⚠ {log_name} {date} {why}，需人工确认后重推")
                stale[key] = {
                    "t": _now(), "date": date, "name": name,
                    "was_fp": was_fp, "now_fp": fp,
                    "was": {"instances": fr.get("expect"),
                            "sent": fr.get("sent"),
                            "sent_uids": fr.get("sent_uids"),
                            "maybe_uids": fr.get("maybe_uids"),
                            "uncertain": fr.get("uncertain")},
                    "now_instances": None,
                    "from": "failed",
                }
                cooling.pop(key, None)
                state["failed"].pop(key, None)

            # ★ permanent 只对「造成它的那份输入」有效，判据是 perm_fp
            #   而不是 fp：两者含义不同（见无测量值分支的说明）。
            #   老记录没有 perm_fp（HEAD 的失败返回根本不带指纹），此时
            #   「输入有没有变」是**未知**，不是「没变」—— 必须重新评估
            #   一次，否则补齐生日/补测之后仍然永久跳过。
            if fr.get("permanent"):
                unchanged = perm_fp is not None and perm_fp == fp
                if unchanged:
                    continue                 # 输入没变，确定失败，重试无意义
                if landed:
                    # 旧内容可能已经在 PACS 里 → 人工处理（不自动重发）
                    _to_stale("曾经的失败已推送过实例（旧内容），"
                              "输入已变化但无法确认", fp_prev)
                    continue
                # 无接收历史 → 解除 permanent，重新走完整流程（含冷却）
                log(f"  ↻ {log_name} {date} 输入已变化或未知，"
                    f"解除永久失败，重新处理")
                state["failed"].pop(key, None)
                fr = None
                retry_n += 1

            if isinstance(fr, dict):
                # ★ 有接收历史时，只要**不能证明**「现在的内容和当初发出去
                #   的一样」就必须转人工：
                #     · fp 缺失（HEAD 的失败返回不带指纹）= 无法确认 → 未知；
                #     · fp 不同 = 确定变了。
                #   None != fp 恒为真，所以缺 fp 自然落进来，无需单独分支。
                #   若把「缺 fp」当成「相同」去重推，PACS 会按同 UID 静默
                #   去重并返回成功，账目记成新版齐全而 PACS 仍是旧内容 ——
                #   正是本次事故的形态。
                if landed and fp_prev != fp:
                    _to_stale(
                        "上次已推送实例但无法确认内容版本" if fp_prev is None
                        else "上次已推送部分实例（旧内容），数据又更新了",
                        fp_prev)
                    continue
                if _wait_left(fr) > 0:
                    wait_n += 1
                    continue                 # 退避中，还没到点
                retry_n += 1

        # ---------------- 冷却期 ----------------
        # ★ 数据源没有「这次检查已结束」这个事件：MDB 是可变快照，设备
        #   随时会补测改写。所以「推之前先等一会儿」是唯一能降低押错概率
        #   的手段。起点见上方说明：优先 measure_ts，缺失时退回 first。
        if cooling_s > 0:
            c = cooling.get(key)
            if not isinstance(c, dict) or c.get("fp") != fp:
                c = {"fp": fp, "first": now}
                cooling[key] = c
            # ★ 起点：有创建时刻就用它，**不要**再跟 first 取 max。
            #   历史积压（首次启动时库里已有的旧检查）的 measure_ts
            #   可能是几个月前，跟 first=now 取 max 会让它们全部无谓地
            #   再等一个冷却期，白白推迟整批补推。
            #   measure_ts 缺失（guid 格式变了）时才退回用 first 兜底。
            ts = row.get("measure_ts")
            start = float(ts) if ts else float(c.get("first") or now)
            ready = start + cooling_s
            # 记下到期时刻，_next_cool_ready() 直接取用（不在返回值里现算
            # measure_ts，避免同一条被反复解析）。
            c["ready"] = ready
            if ready > now:
                cooling_n += 1
                continue

        # ---------------- 内容变了，但先不自动重推 ----------------
        # ⚠️ OAM/KER 的 SOP Instance UID 只由 study + 布局版本派生，不含
        #    内容。同 UID 不同内容推上去，PACS 默认保留首个并**返回成功**
        #    —— 结果会是「推送成功、state 记 6 实例齐全、PACS 里还是旧的
        #    单眼版本」，比现在卡住更难查。先留痕，由 audit/repair 显式处理。
        if isinstance(d, dict) and d.get("fp"):
            prev = stale.get(key) or {}
            if prev.get("now_fp") != fp:
                stale_n += 1
                log(f"  ⚠ {log_name} {date} 数据已更新（补测），"
                    f"PACS 内容仍是旧版，需人工确认后重推")
            try:
                calc.calc_record(rec, lenses, target=target, step=step)
                now_n = _expected_instances(rec, lenses)
            except Exception:
                now_n = None
            stale[key] = {
                "t": _now(), "date": date, "name": name,
                "was_fp": d.get("fp"), "now_fp": fp,
                "was": {k: d.get(k) for k in
                        ("instances", "sc", "oam_eyes", "ker_eyes")},
                "now_instances": now_n,
            }
            cooling.pop(key, None)
            continue

        todo.append((date, key, rec, fp))

    # ★ failed 里指向已不在库中的检查（设备归档/清库）：重试无从下手。
    #   不清理的话「有到期待重试」会永远为真 —— 每轮白读一次全库、白起
    #   一次进程池，而那条记录永远不会成功。dry-run 不动 state。
    if not args.dry_run:
        orphan = [k for k in state["failed"] if k not in live]
        if orphan:
            for k in orphan:
                state["failed"].pop(k, None)
            log(f"failed 队列清理 {len(orphan)} 条（库中已无对应检查，"
                f"无从重试）")
        # cooling / stale 同样要清：设备归档或清库后这些 key 不会再出现，
        # 留着只会让 state 无限膨胀，也让「还有多少例在等」的数字失真。
        for book, label in ((cooling, "cooling"), (stale, "stale")):
            gone = [k for k in book if k not in live]
            if gone:
                for k in gone:
                    book.pop(k, None)
                log(f"{label} 队列清理 {len(gone)} 条（库中已无对应检查）")

    # 新检查优先：今天拍的片子医生在 PACS 里等着看
    todo.sort(key=lambda x: x[0], reverse=True)
    if args.limit:
        todo = todo[:args.limit]
    _extra = (f"，其中到期重试 {retry_n} 例" if retry_n else "") + \
             (f"，{wait_n} 例退避中" if wait_n else "") + \
             (f"，{cooling_n} 例冷却中" if cooling_n else "") + \
             (f"，{stale_n} 例数据已更新待确认" if stale_n else "") + \
             (f"，{backfill_n} 例补写指纹" if backfill_n else "")
    log(f"待处理 {len(todo)} 例（累计已完成 {len(state['done'])}{_extra}）")

    # ★ 调度信息统一由 _finish() 附加到**每一个**返回路径。
    #   以前只在「todo 为空」那条路径上带 cooling/next_ready，本轮一旦
    #   有待办，末尾那个手写的 return 就把它们丢了 → loop() 拿到的
    #   next_ready=0 → 冷却中的病例再也等不到唤醒。多例分批到期时，
    #   处理第一批就会饿死后面几批。
    def _next_cool_ready():
        """调度唤醒时刻。**调用时现算**，不用筛选阶段的快照。

        本轮成功的病例已从 cooling 删掉、失败的还留着，本轮也可能新增
        条目；拿筛选时的值会让 loop() 睡过头或空转。

        ★ 关键：**已经到期但本轮没来得及处理**的条目也必须返回。
          反例：筛选时 A 已过冷却、B 还差 10 秒；本轮处理 A 花了 20 秒，
          结束时 B 已到期。若这里只返回「未来」的到期时刻，B 被过滤掉，
          next_ready=0 → loop() 消费掉 MDB 签名后没有任何唤醒理由 →
          B 永久卡在 cooling 里。

          因此返回「最早的有效 ready」，无论它在过去还是未来；过去的时间
          由调用方（_idle_sleep）按「至少 1 秒」处理，等价于「立刻再来一轮」。
        """
        ts = []
        for v in cooling.values():
            if not isinstance(v, dict):
                continue
            try:
                t = float(v.get("ready") or 0)
            except (TypeError, ValueError):
                continue
            if t > 0:
                ts.append(t)
        return min(ts, default=0.0)

    def _finish(st):
        out = {"cooling": len(cooling), "stale": len(stale),
               "next_ready": _next_cool_ready()}
        out.update(st)
        return out

    if not todo:
        return _finish({"ok": 0, "fail": 0, "skip": 0, "total": 0})

    workers = _workers(cfg, args)
    stats = {"ok": 0, "fail": 0, "skip": 0, "total": len(todo), "done": 0}
    t_begin = time.time()

    # ---------------- 单例提交与记账 ----------------
    # ★ state 的读-改-写不是原子的：多个 worker 同时更新 done/failed 会丢更新，
    #   且 (ok+fail) % 25 可能永远不命中。所有 state 变更都在这个锁内做。
    # ★ _save_state 的 .tmp + fsync + os.replace 本身是原子的（并发写不会
    #   写出半个 JSON），所以锁只需要保护「读改写 + 触发落盘」这一段。
    lock = threading.Lock()
    total_ok = 0

    def commit(res):
        nonlocal total_ok
        key, good, info, errs, lines = res
        for ln in lines:
            log(ln)
        note = None                       # 退避相关的日志留到锁外打
        with lock:
            if good:
                if not args.dry_run:
                    state["done"][key] = info
                    state["failed"].pop(key, None)
                    # 推成功了：冷却与 stale 都不该再留着这一条
                    state.setdefault("cooling", {}).pop(key, None)
                    state.setdefault("stale", {}).pop(key, None)
                stats["ok"] += 1
            else:
                # ★ 单例失败要退避：PACS 半通（端口开、关联被拒 / 部分成功）
                #   时，光靠「下轮再来」会让每一轮把几百例重渲一遍，白烧 CPU
                #   还把 PACS 打得更惨。tries 递增、next 记绝对时间戳。
                prev = state["failed"].get(key)
                tries = int((prev or {}).get("tries") or 0) + 1 \
                    if isinstance(prev, dict) else 1
                info = dict(info or {})
                info["tries"] = tries
                # ★ 接收历史必须**跨尝试累积**，不能只留最近一次。
                #   反例：A 版部分成功（sent_uids 有值）→ 同内容重试时
                #   全失败（sent_uids=[]）→ 若直接覆盖，历史被抹掉 →
                #   设备补测成 B 版后普通重试 → 同 UID 去重，PACS 里留着
                #   A 版，账目却记成 B 版成功。
                #   PACS 先前收到的 A 版不会因为第二次推送失败而消失。
                if isinstance(prev, dict) and prev.get("fp") == info.get("fp"):
                    for f in ("sent_uids", "maybe_uids"):
                        merged = list(prev.get(f) or [])
                        for u in (info.get(f) or []):
                            if u not in merged:
                                merged.append(u)
                        if merged:
                            info[f] = merged
                    if prev.get("uncertain"):
                        info["uncertain"] = True
                    if prev.get("sent"):
                        info["sent"] = max(int(info.get("sent") or 0),
                                           int(prev.get("sent") or 0))
                if info.get("permanent"):
                    # ★ 兜底：永久失败必须记住「造成它的那份输入」。
                    #   漏了 perm_fp 会让这条记录每轮都被当成「输入未知」
                    #   重新评估（永久失败形同虚设），带接收历史时还会被
                    #   无谓转成人工待确认。
                    if not info.get("perm_fp") and info.get("fp"):
                        info["perm_fp"] = info["fp"]
                    info["next"] = 0
                    note = (f"  ⊘ {key} 确定失败"
                            f"（{str(info.get('err', ''))[:80]}），不再重试"
                            f"（改配置/型号后 state 重置会重来一次）")
                else:
                    delay = _backoff(pol, tries)
                    info["next"] = time.time() + delay
                    note = (f"  ↻ {key} 第 {tries} 次失败，{delay:.0f}s 后重试"
                            f"（上限 {pol['cap']:.0f}s）")
                state["failed"][key] = info
                # ★ 失败时把 cooling 条目摘掉：退避调度（failed.next）
                #   接管这一例。留着的话 cooling 里的 ready 会一直落在
                #   过去，让 loop() 每轮都以为「有冷却到期要处理」而
                #   空转读全库（但也绝不能删 cool_until 本身 —— 那是
                #   其它仍在冷却中的病例的唯一唤醒理由）。
                if not args.dry_run:
                    state.setdefault("cooling", {}).pop(key, None)
                stats["fail"] += 1
            total_ok += 1
            # ★ dry-run 一律不落盘。以前这里没有 dry-run 保护，跑
            #   `run --dry-run --limit 25` 会在第 25 例把 {"dry": true}
            #   写进 done —— 生产上拿 dry-run 排障，事后再跑真推时这些
            #   病例已被标成「已完成」，**人为制造**出和补测一样的冻结。
            if total_ok % 25 == 0 and not args.dry_run:
                _save_state(args.state, state)
        if note:
            log(note)

    def budget_left():
        return not args.max_seconds or (time.time() - t_begin) < args.max_seconds

    if workers <= 1:
        # 串行：排障、dry-run、单核机器。行为与旧版完全一致。
        for idx, task in enumerate(todo, 1):
            if not budget_left():
                log(f"本轮预算 {args.max_seconds}s 用尽，已处理 {idx - 1}/{len(todo)}，"
                    f"剩余 {len(todo) - idx + 1} 例下轮继续")
                break
            log(f"[{idx}/{len(todo)}] {task[2]['name']} {task[0]}")
            commit(_process_one(task, cfg, lenses, meta, push_target, args.dry_run))
    else:
        # 并发：有界窗口。★ 预算检查放在**提交点**，所以新检查（todo 已按
        #   日期倒序）不会被老病例堵住 —— 队列里最多 workers 个在飞。
        log(f"并发 {workers} 进程处理 {len(todo)} 例")
        with concurrent.futures.ProcessPoolExecutor(
                max_workers=workers) as ex:
            pending = {}          # future -> task
            cursor = 0            # todo 的下一个待提交下标
            stop_submit = False   # 预算用尽或池失效后不再提交

            def fail_task(task, err):
                log(f"✗ {task[2]['name']} {task[0]} 子进程异常: {err}")
                with lock:
                    # 子进程异常（OOM 被杀、段错误）也是可重试失败，同样进退避：
                    # 大量任务同时炸时不该让下一轮立刻又把它们全渲一遍。
                    prev = state["failed"].get(task[1])
                    tries = int((prev or {}).get("tries") or 0) + 1 \
                        if isinstance(prev, dict) else 1
                    delay = _backoff(pol, tries)
                    # ★ 子进程异常 = 我们不知道它推了多少。可能已经发出
                    #   一部分实例，然后进程被杀。必须按「可能已接收」
                    #   记录（uncertain），并把该内容版本的接收历史带过来
                    #   —— 否则下一轮内容一变就会普通重试，同 UID 去重。
                    keep = {}
                    if isinstance(prev, dict) and prev.get("fp") == task[3]:
                        for f in ("sent_uids", "maybe_uids"):
                            if prev.get(f):
                                keep[f] = list(prev[f])
                        if prev.get("sent"):
                            keep["sent"] = prev["sent"]
                    state["failed"][task[1]] = {
                        "t": _now(), "date": task[0],
                        "fp": task[3],
                        "err": f"子进程异常: {err}"[:200],
                        "tries": tries, "next": time.time() + delay,
                        "uncertain": True,
                        **keep}
                    # ★ 与 commit() 的失败分支保持一致：把 cooling 摘掉，
                    #   由 failed.next 接管。漏了这里的话，cooling 里会
                    #   残留一条 ready 已过期的记录，让 loop() 每轮都以为
                    #   「有冷却到期要处理」，在长退避期间反复白读全库。
                    if not args.dry_run:
                        state.setdefault("cooling", {}).pop(task[1], None)
                    stats["fail"] += 1

            def fill():
                """把窗口填满。返回 False 表示进程池已失效。"""
                nonlocal cursor, stop_submit
                while len(pending) < workers and cursor < len(todo):
                    if not budget_left():
                        stop_submit = True
                        return True
                    task = todo[cursor]
                    try:
                        fut = ex.submit(_process_one, task, cfg, lenses, meta,
                                        push_target, args.dry_run)
                    except concurrent.futures.process.BrokenProcessPool:
                        # ★ 子进程被 OOM killer 杀掉或段错误时，
                        #   ProcessPoolExecutor 进入 BrokenProcessPool 且
                        #   **永久不可用** —— 之后任何 submit 都会抛。
                        #   这条任务没提交成功，cursor 不动，交给串行兜底。
                        return False
                    pending[fut] = task
                    cursor += 1
                return True

            alive = fill()
            while pending:
                done, _ = concurrent.futures.wait(
                    pending, return_when=concurrent.futures.FIRST_COMPLETED)
                for fut in done:
                    task = pending.pop(fut)
                    try:
                        commit(fut.result())
                    except Exception as e:
                        fail_task(task, e)
                if not alive:
                    continue
                alive = fill()
                if not alive:
                    log("⚠ 进程池已失效（子进程被杀），剩余任务改为串行处理")
                    # 在飞的 future 已废，它们的结果拿不到了 -> 记失败待重试
                    for fut, task in list(pending.items()):
                        fail_task(task, "进程池失效，未执行（下轮重试）")
                    pending.clear()
                    break
                if stop_submit and not pending:
                    break

            # 兜底：把没做完的补上。
            #   · 池失效（alive=False）-> **必须**串行跑完，否则这些记录既不
            #     处理也不记账，state 里凭空少一批（实测漏过 22 例）。
            #   · 预算用尽（stop_submit=True）-> 不跑，留给下轮。
            if cursor < len(todo):
                rest = len(todo) - cursor
                if not alive:
                    log(f"⚠ 进程池失效，串行兜底处理剩余 {rest} 例")
                    while cursor < len(todo):
                        commit(_process_one(todo[cursor], cfg, lenses, meta,
                                            push_target, args.dry_run))
                        cursor += 1
                elif stop_submit:
                    log(f"本轮预算 {args.max_seconds}s 用尽，"
                        f"剩余 {rest} 例下轮继续")

    if not args.dry_run:
        _save_state(args.state, state)  # 收尾兜底，确保最后一次进度落盘

    # ★ 还有到期待重试的病例没轮到（预算用尽 / 进程池失效）时，loop()
    #   不能就此睡到 MDB 下次变化 —— 它得按退避的节奏再来一轮。
    #   这里把「下一次最早什么时候该再跑」一并返回，避免调用方自己算。
    nxt = [float((v or {}).get("next") or 0)
           for v in state["failed"].values() if isinstance(v, dict)
           and not v.get("permanent")]
    # ★ cooling 里可能还有本轮没轮到、或本轮新产生的条目，next_ready
    #   必须是「现在这一刻」重新算的，不能用筛选阶段那个值 —— 本轮处理
    #   成功的病例会把对应条目删掉，处理失败的不会。
    return _finish({"ok": stats["ok"], "fail": stats["fail"], "skip": 0,
                    "total": len(todo), "blocked": None,
                    "next_retry": min([t for t in nxt if t > 0],
                                      default=0.0)})


def _institution(cfg):
    """页眉机构名。来自 config.yaml，不在代码里硬编码。"""
    return ((cfg.get("report") or {}).get("institution") or "").strip()


def _axes(rec):
    """报告单表头的轴位（K1/K2 后面的 @度数）。"""
    out = {}
    for side, k in (("OD", "R"), ("OS", "L")):
        ax = (rec["eyes"].get(side) or {}).get("axis")
        if ax is not None:
            out[k] = int(round(ax))
    return out


def _has_cjk(s):
    from core.utils import is_cjk
    return is_cjk(s)


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _watchdog(cfg):
    """守候参数。全部来自 config.yaml 的 watchdog 段。"""
    d = (cfg.get("watchdog") or {})

    def _num(key, default, cast=float):
        try:
            v = d.get(key)
            return default if v is None else cast(v)
        except (TypeError, ValueError) as e:
            log(f"WARN watchdog.{key} 取值非法（{d.get(key)!r}: {e}），"
                f"用默认 {default}")
            return default

    return {
        "probe_seconds": max(1, _num("probe_seconds", 15, int)),
        "connect_timeout": max(1, _num("connect_timeout", 3, int)),
        "cecho_timeout": max(1, _num("cecho_timeout", 5, int)),
        "require_cecho": bool(d.get("require_cecho", False)),
        "log_every": max(1, _num("log_every", 20, int)),
    }


def _retry_policy(cfg):
    """单例失败的退避参数。"""
    d = (cfg.get("retry") or {})

    def _num(key, default, cast=float):
        try:
            v = d.get(key)
            return default if v is None else cast(v)
        except (TypeError, ValueError):
            return default

    return {
        "initial": max(1.0, _num("initial_seconds", 60)),
        "factor": max(1.0, _num("factor", 2.0)),
        "cap": max(1.0, _num("max_seconds", 900)),
    }


def _backoff(pol, tries):
    """第 tries 次失败后等多少秒再试。tries 从 1 开始。"""
    delay = pol["initial"] * (pol["factor"] ** max(0, tries - 1))
    return min(delay, pol["cap"])


def _target_probe(target, wd):
    """探推送目标。返回 (可用?, 描述)。

    ★ partial（TCP 通但 C-ECHO 没应答）默认算**可用** —— 有些 SCP 不实现
      Verification SOP Class，照样收 C-STORE，判死会永远推不上去。
      只有配了 watchdog.require_cecho: true 才要求 C-ECHO 必须成功。
    """
    if not target.get("host"):
        return False, "未配置 push.dicom_host"
    state, detail = dicom.probe(
        target["host"], target["port"], target.get("aet", ""),
        timeout=wd["connect_timeout"],
        cecho_timeout=wd["cecho_timeout"])
    if state == "up":
        return True, detail
    if state == "partial":
        if wd["require_cecho"]:
            return False, detail + "（watchdog.require_cecho=true，要求 C-ECHO 成功）"
        return True, detail + "（端口可达，按可推送处理）"
    return False, detail


def _wait_push_target(target, wd, dry_run=False, quiet=False):
    """阻塞守候，直到推送目标可用。

    ★ 这就是「开机时 PACS 比管线上线慢」的兜底：设备/服务器开机顺序不可控，
      管线的 systemd 单元不会因为对端慢就罢工 —— 它在这里等着，每
      probe_seconds 探一次，一通就返回，接着把积压一次补推。

    ★ 为什么阻塞而不退出重来：退出要靠 systemd Restart=always 重启，
      RestartSec=30 意味着每 30s 冷启动一次（重载常数表、读全库），
      且日志里只看到「服务在重启」，看不出真正原因。守候是原地等。

    ★ 待办不会因为在等而丢失：每轮都是**基于全库重新筛选** todo（state
      done 去重），不做增量游标。等多久都不影响最终补推的完整性。
    """
    if dry_run:
        return True
    probe_sec = int(wd["probe_seconds"])
    every = int(wd["log_every"])
    t0 = None
    n = 0
    while True:
        ok, detail = _target_probe(target, wd)
        if ok:
            if t0 is None:
                # quiet：上一轮刚确认过在线，别每轮都刷一行「在线」
                if not quiet:
                    log(f"✓ DICOM 目标在线 {target['host']}:{target['port']} "
                        f"AE={target['aet']} —— {detail}")
            else:
                log(f"✓ DICOM 目标恢复 {target['host']}:{target['port']} "
                    f"AE={target['aet']} —— 等待 {time.time() - t0:.0f}s 后"
                    f"第 {n + 1} 次探测成功，继续补推积压")
            return True
        n += 1
        if t0 is None:
            t0 = time.time()
            log(f"⚠ DICOM 目标不可达 {target['host']}:{target['port']} "
                f"AE={target['aet']} —— {detail}")
            log(f"  原地守候：每 {probe_sec}s 探测一次，通了立刻补推"
                f"（不等也不丢，待办按全库重新筛选）")
        elif n % every == 0:
            log(f"  … 仍在守候 {target['host']}:{target['port']}，"
                f"已 {time.time() - t0:.0f}s／第 {n} 次探测 —— {detail}")
        time.sleep(probe_sec)


def _retries_due(state, now=None):
    """failed 队列里已到重试时间的 key 列表。

    ★ 有到期记录时，即使 MDB 没变化也要跑一轮 —— 否则 PACS 恢复后那批
      推送失败的病例永远不会再试（用户实测踩到的正是这一类：MDB 没动，
      轮询认为「无事可做」）。
    """
    now = time.time() if now is None else now
    out = []
    for k, v in (state.get("failed") or {}).items():
        if not isinstance(v, dict):
            continue
        if v.get("permanent"):
            continue                       # 确定失败，重试无意义
        try:
            nxt = float(v.get("next") or 0)
        except (TypeError, ValueError):
            nxt = 0.0
        if nxt <= now:
            out.append(k)
    return out


# ---------------------------------------------------------------- 入口

def run_once_cli(args):
    """一次性运行（cron / 手动补数）。

    退出码
    ------
    0  本轮没有失败
    1  有单例失败（已进 failed 队列，下次运行会按退避重试）
    2  配置/装载层错误
    3  推送目标不可达，本轮**什么都没做**（state 不动，下次运行会补）
    """
    cfg, lenses, meta = _prepare(args)
    if cfg is None:
        return 2

    target = _push_target(cfg, args)
    if not args.dry_run and not target.get("host"):
        log("✗ 未配置推送目标（config.yaml 的 push.dicom_host）")
        return 2

    state = _load_state(args.state, lenses)
    stats = run_once(cfg, lenses, meta, state, args, target)
    if not args.dry_run:
        _save_state(args.state, state)

    if stats.get("blocked"):
        # cron 场景不该在这里无限等（会把定时任务堵住），直接退出，
        # 靠下一次运行补推；常驻的 watch 才会原地守候。
        why = {"pacs": "DICOM 目标不可达",
               "mdb": "MDB 不存在或读不出来",
               "schema": "MDB 结构不符"}.get(stats["blocked"],
                                              stats["blocked"])
        log(f"✗ {why}，本轮未处理（state 未改动，下次运行补推）")
        return 3
    log(f"本轮结束 ok={stats['ok']} fail={stats['fail']} "
        f"累计={len(state['done'])}")
    return 0 if stats["fail"] == 0 else 1


def _sleep(seconds):
    """睡眠的唯一入口。

    ★ 单独包一层只为可测：测试要能在「本轮处理 + 落盘都做完之后」精确
      退出，而不是撞进真实的 time.sleep（实测会让测试挂满 20 分钟）。
      直接在测试里 patch time.sleep 会波及 mdb.wait_settled 里的轮询。
    """
    time.sleep(seconds)


def _idle_sleep(state, interval, extra_next=0.0):
    """空闲时睡多久。

    平时 interval；failed 里有退避中的记录、或有冷却到期要处理的检查时，
    睡到最早到期那一刻（但不长于 interval，也不短于 1s）—— 到期的事
    不能被轮询间隔拖太久。

    extra_next 是 run_once 回报的「冷却中最早到期时刻」：冷却期默认
    20 分钟，比 interval（60s）长得多，光靠 interval 轮询会在冷却到期
    后最多再等一个 interval 才推，而且每轮都要白读一次全库。
    """
    now = time.time()
    nxt = []
    for v in (state.get("failed") or {}).values():
        if not isinstance(v, dict) or v.get("permanent"):
            continue
        try:
            t = float(v.get("next") or 0)
        except (TypeError, ValueError):
            continue
        if t > now:
            nxt.append(t)
    try:
        ex = float(extra_next or 0)
    except (TypeError, ValueError):
        ex = 0.0
    # ★ 已到期的冷却（ex <= now）也要算进来：它表示「有病例现在就该处理，
    #   只是上一轮处理别的病例花掉了时间」。返回 1s 让 loop() 立刻再来
    #   一轮，而不是睡满 interval。
    if ex > 0:
        nxt.append(max(ex, now + 1.0))
    if not nxt:
        return interval
    return max(1.0, min(float(interval), min(nxt) - now))


def loop(args):
    """常驻：监视 MDB mtime，变了就跑一轮；另外按退避节奏重试失败病例。

    三个「本轮要不要跑」的触发条件（任一成立就跑一轮）：
      1. MDB 变了（有新检查/新数据）
      2. failed 队列里有到期的重试记录（PACS 恢复后靠这条补推）
      3. 首次启动（last_sig 为 None —— 处理历史积压，state 保证幂等）

    ★ 签名（last_sig）只在**一轮真的消费过数据**之后才推进。推送目标
      不可达、MDB 读失败、库结构不符时都保持不动，下次针对同一批数据
      重来。这条是「开机时 PACS 比管线上线慢」不再丢单的根本原因。
    """
    cfg, lenses, meta = _prepare(args)
    if cfg is None:
        return 2

    target = _push_target(cfg, args)
    if not target.get("host"):
        log("✗ 未配置推送目标（config.yaml 的 push.dicom_host）")
        return 2

    w = cfg.get("watcher") or {}
    interval = int(args.interval or w.get("interval_seconds", 60))
    mdb_path = args.mdb or cfg.get("mdb_path")
    wd = _watchdog(cfg)

    state = _load_state(args.state, lenses)
    log(f"常驻启动 | MDB={mdb_path} | 间隔={interval}s | "
        f"型号={'/'.join(L['name'] for L in lenses)}")
    log(f"推送守候 | {target['host']}:{target['port']} AE={target['aet']} | "
        f"探测间隔 {wd['probe_seconds']}s"
        f"{'（要求 C-ECHO 成功）' if wd['require_cecho'] else '（端口通即视为在线）'}")

    settle = float(w.get("settle_seconds", 5))
    settle_timeout = float(w.get("settle_timeout_seconds", 120))

    last_sig = None          # 已成功消费的 MDB 签名（消费失败时保持不动）
    idle_rounds = 0
    target_ok = None         # 上一轮的探测结论：None=还没探过
    cool_until = 0.0         # 冷却中最早的到期时刻（上一轮 run_once 回报）
    while True:
        try:
            sig = mdb.mdb_signature(mdb_path)
            if sig is None:
                log(f"⚠ MDB 不可达: {mdb_path}")
                _sleep(interval)
                continue

            new_data = last_sig is None or sig > last_sig
            due = _retries_due(state)
            # ★ 冷却到期也是一轮要跑的理由：数据齐全/补测补齐之后，MDB
            #   不会再有变化，光靠 new_data 永远等不到 —— 这条漏了就是
            #   「设了冷却期之后所有检查都不推了」。
            cooling_due = bool(cool_until) and time.time() >= cool_until
            if not new_data and not due and not cooling_due:
                idle_rounds += 1
                if idle_rounds == 1:
                    log("MDB 无变化，进入等待")
                _sleep(_idle_sleep(state, interval, cool_until))
                continue

            idle_rounds = 0
            if new_data:
                if last_sig is not None:
                    log(f"MDB 有更新（{datetime.fromtimestamp(last_sig[0] / 1e9):%H:%M:%S}"
                        f" → {datetime.fromtimestamp(sig[0] / 1e9):%H:%M:%S}）")
                # ★ 先等落盘稳定再读。mtime 变了不等于写完了 —— 见
                #   sources/mdb.wait_settled 的说明。
                ok, settled, waited = mdb.wait_settled(
                    mdb_path, settle=settle, timeout=settle_timeout)
                if not ok:
                    log(f"⚠ MDB 在 {waited:.0f}s 内仍在变化，本轮跳过，"
                        f"下轮再看（避免读到写入中间态）")
                    _sleep(interval)
                    continue
                if waited >= 1:
                    log(f"  已稳定（等待 {waited:.1f}s），开始处理")
                run_sig = settled
            else:
                run_sig = last_sig
                if due:
                    log(f"到期重试 {len(due)} 例（MDB 无变化）")
                else:
                    log("冷却到期，开始推送")

            # ★ 守候：目标不在线就在原地等，通了立刻往下走。不能「探测一次
            #   失败就退出/跳过」—— 开机时 PACS 常常比本管线晚几十秒到几分钟。
            _wait_push_target(target, wd, dry_run=args.dry_run,
                              quiet=bool(target_ok))

            # ★ dry-run 必须完全隔离：run_once 里会给老 done 补 fp、
            #   增删 cooling/stale、更新 failed —— 这些都在**内存**里改。
            #   只给 _save_state 加条件是不够的：外层这里仍然会无条件
            #   落盘，而且把「排障时看到的当前 MDB 内容」固化成了未来的
            #   比对基线（等于把真补测永久洗白）。
            #   做法：dry-run 用深拷贝跑，跑完丢弃，state 一个字节都不变。
            run_state = copy.deepcopy(state) if args.dry_run else state
            stats = run_once(cfg, lenses, meta, run_state, args, target)
            if not args.dry_run:
                _save_state(args.state, state)
            target_ok = stats.get("blocked") != "pacs"

            if stats.get("blocked"):
                # 本轮一个都没处理成（MDB/结构/PACS 又掉了）：签名不推进，
                # 下轮针对同一批数据重来。
                log(f"  本轮未消费（{stats['blocked']}），"
                    f"MDB 签名保持不动，下轮重试")
                _sleep(interval)
                continue

            last_sig = run_sig
            # dry-run 下 cooling 是拷副本里算的，仍可用它决定睡多久
            # （只影响轮询节奏，不影响任何持久化内容）。
            #
            # ★ 防「无进展空转」：如果本轮是被冷却唤醒的、却一个都没处理成
            #   （ok+fail==0），说明那些条目暂时进不了待办（比如被 --limit
            #   挡住）。此时不能立刻再跑 —— 但**也不能删掉 cool_until**，
            #   否则冷却中的病例永远等不到唤醒。正确做法是把这次唤醒
            #   推迟一个 interval。
            cool_until = float(stats.get("next_ready") or 0)
            no_progress = (stats["ok"] + stats["fail"]) == 0
            if cooling_due and no_progress and cool_until:
                cool_until = max(cool_until, time.time() + interval)
            if stats["ok"] or stats["fail"]:
                log(f"本轮 ok={stats['ok']} fail={stats['fail']} "
                    f"累计={len(state['done'])}")
            if stats.get("stale"):
                log(f"⚠ {stats['stale']} 例数据已更新但未重推"
                    f"（state.json 的 stale 段，见 audit）")
        except Exception:
            import traceback
            log("循环异常:\n" + traceback.format_exc())
            # 出过异常就别假定对端还正常，下一轮重新打一遍探测结论
            target_ok = None
        # ★ 必须带上 cool_until：不带的话，冷却到期后最多还要再等一个
        #   interval 才会被唤醒（默认 60s），而且这期间每轮白读一次全库。
        _sleep(_idle_sleep(state, interval, cool_until))


def _prepare(args):
    """装载配置、常数、型号。失败返回 (None, None, None)。"""
    try:
        cfg, lenses, meta = build_config(args.config)
    except Exception as e:
        log(f"✗ 配置/常数装载失败: {e}")
        return None, None, None

    log(f"常数表 {meta['constants_count']} 款"
        f"（IOLCon {meta['iolcon_downloaded']}）")
    log(f"口径：{meta['policy']}")
    log(f"目标屈光 {meta['target_refraction']} D，步进 {meta['step']} D")
    log("上表型号：" + " | ".join(
        f"{L['name']}" + (f"(n={L['results']})" if L["results"] else "")
        for L in lenses))
    for L in lenses:
        if L["note"]:
            log(f"  ⚠ {L['name']}：{L['note']}")
    return cfg, lenses, meta


def _push_target(cfg, args):
    """推送目标。命令行 > config.yaml。

    ★ 只有三个字段：host / port / aet。没有口令 ——
      DICOM C-STORE 协议本身无认证，也不假设目标一定是 Orthanc。

    ★ 没有 env 文件这一层：两处配同一个东西只会让「改了不生效」这种坑
      变得难查。要改 PACS 就改 config.yaml 的 push 段。
    """
    pu = cfg.get("push") or {}
    host = args.dicom_host or pu.get("dicom_host") or ""
    port = int(args.dicom_port or pu.get("dicom_port") or 4242)
    aet = args.aet or pu.get("ae_title") or "ORTHANC"
    return {"host": host, "port": port, "aet": aet}


def _load_state(path, lenses):
    """装载状态。型号集变化时整体失效重推。"""
    state = {"done": {}, "failed": {}}
    if os.path.exists(path):
        try:
            state = json.load(open(path, encoding="utf-8"))
        except Exception:
            pass
    state.setdefault("done", {})
    state.setdefault("failed", {})
    # cooling: key -> {fp, first}  「这版内容」首次被扫描看到的时刻。
    #   只放冷却中的条目（推成功后删），稳态规模 = 冷却窗口内的新检查数。
    # stale:   key -> {...}         数据已更新但 PACS 里还是旧版，待人工确认。
    state.setdefault("cooling", {})
    state.setdefault("stale", {})

    # ★ 签名里含型号集与常数 key：换型号必须重推，
    #   否则旧记录会被当成「已完成」永远不再生成新单。
    #
    # ★ 还要含 INSTANCE_LAYOUT：新增/删除 series 时同样得重推。
    #   只按型号做签名的话，加上 Keratometry series 之后，已经推过的
    #   老病例永远不会补出这一路 —— 那正是本次改动要解决的问题。
    sig = INSTANCE_LAYOUT + "||" + "|".join(L["key"] for L in lenses)
    if state.get("sig") != sig:
        log(f"型号/口径变更，state 重置（旧 {len(state['done'])} 条作废）")
        state = {"done": {}, "failed": {}, "sig": sig}
    state["sig"] = sig
    return state


# ---------------------------------------------------------------- 子命令

def cmd_watch(a):
    """常驻监视 MDB。"""
    log(SCOPE_WARNING)
    return loop(a)


def cmd_run(a):
    """跑一轮。"""
    log(SCOPE_WARNING)
    return run_once_cli(a)


def cmd_probe(a):
    """探测推送目标是否在线。

    退出码：0=在线（TCP 通），1=不可达。给运维和脚本用 ——
    想知道「现在是 PACS 的问题还是我的问题」时先跑这个。
    """
    try:
        cfg, _lenses, _meta = build_config(a.config)
    except Exception as e:
        log(f"✗ 配置装载失败: {e}")
        return 2

    target = _push_target(cfg, a)
    if not target.get("host"):
        log("✗ 未配置推送目标（config.yaml 的 push.dicom_host）")
        return 2

    wd = _watchdog(cfg)
    state, detail = dicom.probe(
        target["host"], target["port"], target["aet"],
        timeout=wd["connect_timeout"], cecho_timeout=wd["cecho_timeout"])
    ok, verdict = _target_probe(target, wd)

    if a.json:
        print(json.dumps({
            "host": target["host"], "port": target["port"],
            "aet": target["aet"], "state": state, "detail": detail,
            "pushable": ok, "verdict": verdict,
            "require_cecho": wd["require_cecho"],
        }, ensure_ascii=False, indent=1))
    else:
        mark = {"up": "✓", "partial": "⚠", "down": "✗"}[state]
        print(f"{mark} {target['host']}:{target['port']} AE={target['aet']}"
              f" → {state}")
        print(f"  {detail}")
        print(f"  结论：{'可推送' if ok else '不可推送'} —— {verdict}")
    return 0 if ok else 1


def cmd_constants(a):
    """抓取 / 生成常数表。"""
    argv = []
    if a.output:
        argv += ["-o", a.output]
    if a.from_xml:
        argv += ["--from-xml", a.from_xml]
    if a.show:
        argv += ["--show", a.show]
    if a.local:
        argv += ["--local"]
    if a.save_xml is not None:
        argv += ["--save-xml", a.save_xml]
    if a.timeout:
        argv += ["--timeout", str(a.timeout)]
    return constants.main(argv)


def cmd_lenses(a):
    """列出上表型号与常数。"""
    try:
        cfg, lenses, meta = config.build(a.config)
    except Exception as e:
        log(f"✗ {e}")
        return 2

    print(f"常数表：{meta['constants_count']} 款"
          f"（IOLCon {meta['iolcon_downloaded']}）")
    print(f"合并口径：{meta['policy']}")
    print(f"目标屈光：{meta['target_refraction']} D   档位步进：{meta['step']} D")
    print(f"\n上表 {len(lenses)} 款：\n")

    hdr = f"{'名称':<12}{'常数表 key':<38}{'A':>9}{'pACD':>8}{'SF':>8}  来源"
    print(hdr)
    print("-" * len(hdr.encode("gbk", "ignore")) if sys.stdout.encoding else "-" * 90)
    for L in lenses:
        a0, a1, a2 = L["haigis"]
        src = ",".join(sorted(set((L.get("sources") or {}).values()))) or "-"
        print(f"{L['name']:<12}{L['key']:<38}"
              f"{L['srkt_a'] if L['srkt_a'] is not None else '-':>9}"
              f"{L['pacd'] if L['pacd'] is not None else '-':>8}"
              f"{L['sf'] if L['sf'] is not None else '-':>8}  {src}")
        print(f"{'':<12}Haigis a0={a0} a1={a1} a2={a2}"
              + (f"   n={L['results']}" if L["results"] else ""))
        if L["note"]:
            print(f"{'':<12}⚠ {L['note']}")
    return 0


def cmd_check(a):
    """自检：把每个环节都跑一遍，报告哪一步不通。"""
    ok_all = True

    def item(name, fn, critical=True):
        """跑一项检查。

        critical=False 的项失败只打 ⚠ 警告，不改变退出码 ——
        用于「现在不通但管线自己能重试」的环节（DICOM 目标端口）。
        install.sh 用 --soft-push 走这条路：PACS 晚起来不该拦住安装。
        """
        nonlocal ok_all
        try:
            msg = fn()
            print(f"  ✓ {name}: {msg}")
        except Exception as e:
            if critical:
                ok_all = False
                print(f"  ✗ {name}: {e}")
            else:
                print(f"  ⚠ {name}: {e}"
                      f"（不阻止启动，watch 会原地守候并重试）")

    print("配置与常数")
    holder = {}

    def _cfg():
        cfg, lenses, meta = config.build(a.config)
        holder.update(cfg=cfg, lenses=lenses, meta=meta)
        return (f"{meta['constants_count']} 款常数 / 上表 {len(lenses)} 款 / "
                f"目标屈光 {meta['target_refraction']} D")

    item("装载", _cfg)
    if not holder:
        return 1

    cfg, lenses, meta = holder["cfg"], holder["lenses"], holder["meta"]

    def _formulas():
        bad = []
        for L in lenses:
            for f in FORMULA_ORDER:
                if f == "SRK/T" and L["srkt_a"] is None:
                    bad.append(f"{L['name']}:SRK/T")
                if f == "Haigis" and None in L["haigis"]:
                    bad.append(f"{L['name']}:Haigis")
                if f == "Hoffer Q" and L["pacd"] is None:
                    bad.append(f"{L['name']}:HofferQ")
                if f == "Holladay 1" and L["sf"] is None:
                    bad.append(f"{L['name']}:Holladay1")
        if bad:
            raise RuntimeError("常数不完整 → " + ", ".join(bad))
        return f"四公式 × {len(lenses)} 款常数齐全"

    item("常数完整性", _formulas)

    print("\n报告单渲染")
    mdb_path = a.mdb or cfg.get("mdb_path")

    def _fonts():
        return str(report.resolve_fonts())

    item("中文字体", _fonts)

    print("\n数据源")
    subject = {}

    def _schema():
        if not mdb_path or not os.path.exists(mdb_path):
            raise FileNotFoundError(f"MDB 不存在: {mdb_path}")
        ok, info = mdb.check_schema(mdb_path)
        if not ok:
            raise RuntimeError(
                f"库结构不是 IOLMaster 500 的 IolmMeasurements"
                f"（缺 {', '.join(info['missing'])}）—— 其他机型未适配")
        subject["schema"] = info
        return (f"{info['table']} {info['rows']} 行，"
                f"必需字段齐全，可选字段 {len(info['present_optional'])}/"
                f"{len(mdb.OPTIONAL_COLUMNS)}")

    item("库结构", _schema)

    def _mdb():
        groups = mdb.load_measurements(mdb_path)
        subject["groups"] = groups
        return f"{len(groups)} 组检查记录"

    item("MDB 解码", _mdb)

    print("\n计算与封装")
    if subject.get("groups"):
        # ★ 挑一条**有测量值**的记录做试跑样本，不能用「第一条」：
        #   库里存在只有眼状态、既没 AL 也没 r 的行（操作员开着界面没测就
        #   保存），那种记录本来就不产生任何实例，拿它试跑会把「这条没数据」
        #   误报成「整条链路不通」。
        def _pick():
            for (f_, g_, d_), rws in subject["groups"].items():
                r_ = _record(mdb.pick_latest(rws), f"{f_}{g_}")
                if calc.has_measurement(r_):
                    return f_, g_, d_, r_
            return None

        picked = _pick()

        def _e2e():
            if picked is None:
                raise RuntimeError(
                    "库中没有任何一条记录带可用测量值"
                    "（既无 AL 也无 r1+r2）—— 请确认 MDB 路径指向的是活库")
            fam, giv, date, rec = picked
            name = rec["name"]
            calc.calc_record(rec, lenses,
                             target=meta["target_refraction"], step=meta["step"])

            # ★ 组装走生产的 _record()，不再另写一套判据 ——
            #   两处判据不一致时，自检会通过而管线不出单（或反过来）。
            pid = patient.make_pid(rec.get("dob"), name)
            if not pid:
                raise RuntimeError(f"{name} 无生日，无法生成 PatientID")

            study_uid = dicom.study_uid_for(pid, rec.get("date"))
            made = []

            # 晶体单：只在度数算得出时生成（与 _process_one 一致）
            sc_txt = "跳过（度数不可算）"
            if calc.calcable(rec):
                L = lenses[0]
                struct = sheet.build(rec, L,
                                     target=meta["target_refraction"],
                                     step=meta["step"])
                img = report.render_image(struct, axes=_axes(rec),
                                          institution=_institution(cfg))
                sc = dicom.build_sc(rec, img, cfg, pid, L)
                made.append("SC")
                # 报的是**实际要推的** JPEG 字节数，不是中间 PNG
                sc_txt = (f"SC {sc.Rows}×{sc.Columns} "
                          f"{len(sc.PixelData) // 1024}KB")

            # OAM：两眼都没 AL 时 build 返回 None（与 _process_one 一致）
            ed = oam.build(rec, cfg, pid, study_uid,
                           oam.series_uid_for(study_uid),
                           oam.sop_uid_for(study_uid))
            if ed is not None:
                made.append("OAM")

            ker = keratometry.build(
                rec, cfg, pid, study_uid,
                keratometry.series_uid_for(study_uid),
                keratometry.sop_uid_for(study_uid))
            if ker is not None:
                made.append("KER")

            if not made:
                raise RuntimeError(f"{name} {date} 一个实例都凑不出来")

            # 抽检 KER 的陡/平是否按半径判对，以及 ACD 段有没有进 OAM
            ker_txt = "—"
            if ker is not None:
                seq = getattr(ker, "KeratometryRightEyeSequence", None) \
                    or getattr(ker, "KeratometryLeftEyeSequence", None)
                if seq:
                    st = seq[0].SteepKeratometricAxisSequence[0]
                    ker_txt = (f"r={st.RadiusOfCurvature:.2f} "
                               f"K={st.KeratometricPower:.2f} "
                               f"@{st.KeratometricAxis:.0f}°")
            acd_txt = "—"
            if ed is not None:
                reye = getattr(ed,
                               "OphthalmicAxialMeasurementsRightEyeSequence",
                               None)
                if reye:
                    for m in reye[0].OphthalmicAxialLengthMeasurementsSequence:
                        if (m.OphthalmicAxialLengthMeasurementsType
                                != "SEGMENTAL LENGTH"):
                            continue
                        seg = getattr(
                            m,
                            "OphthalmicAxialLengthMeasurementsSegmentalLengthSequence",
                            None) or []
                        for sg in seg:
                            acd_txt = f"{sg.OphthalmicAxialLength:.2f}mm"

            oam_txt = (f"OAM Modality={ed.Modality} ACD={acd_txt}"
                       if ed is not None else "OAM 跳过（无眼轴）")
            return (f"{name} {date} → {sc_txt} | {oam_txt} | "
                    f"KER {ker_txt} | {'+'.join(made)} | PID {pid}")

        item("端到端试跑", _e2e)
    else:
        # ★ 空库（0 行）不拦安装 —— 新装的机器、设备还没做过检查都可能这样。
        #   但要**显式**说出来：静默跳过会让人以为「试跑通过了」。
        print("  ⚠ 端到端试跑: 跳过 —— 库里没有任何检查记录"
              "（新装/空库/路径指向空表）")

    print("\n推送目标")
    target = _push_target(cfg, a)

    def _push():
        if not target["host"]:
            raise RuntimeError("未配置 push.dicom_host")
        wd = _watchdog(cfg)
        state, detail = dicom.probe(
            target["host"], target["port"], target["aet"],
            timeout=wd["connect_timeout"], cecho_timeout=wd["cecho_timeout"])
        where = f"{target['host']}:{target['port']} AE={target['aet']}"
        if state == "up":
            return f"{where} {detail}"
        if state == "partial":
            # 端口通但 C-ECHO 没应答：不算致命（老 SCP 可能不实现
            # Verification SOP Class），只是提示「DICOM 层未确认」。
            # 真正的判据是 C-STORE，常驻管线会自己退避重试。
            return (f"{where} 端口可达，C-ECHO 未确认（{detail}）"
                    f"—— 老 SCP 可能不支持 Verification，按可达处理")
        raise RuntimeError(f"{where} {detail}")

    item("DICOM 目标（TCP + C-ECHO）", _push,
         critical=not getattr(a, "soft_push", False))

    print()
    print("全部通过 ✓" if ok_all else "存在问题 ✗")
    return 0 if ok_all else 1


def cmd_render(a):
    """只渲染一张单到本地，不推 PACS。"""
    try:
        cfg, lenses, meta = config.build(a.config)
    except Exception as e:
        log(f"✗ {e}")
        return 2

    mdb_path = a.mdb or cfg.get("mdb_path")
    if not mdb_path or not os.path.exists(mdb_path):
        log(f"✗ MDB 不存在: {mdb_path}")
        return 2

    groups = mdb.load_measurements(mdb_path)
    want_name, want_date = a.patient, a.date
    hit = None
    for (fam, giv, date), rows in groups.items():
        if want_name and want_name not in f"{fam}{giv}":
            continue
        if want_date and date != want_date:
            continue
        hit = (fam, giv, date, mdb.pick_latest(rows))
        break
    if not hit:
        log(f"✗ MDB 里找不到 {want_name or '*'} / {want_date or '*'}")
        return 1

    fam, giv, date, row = hit
    name = f"{fam}{giv}"
    log_name = log_label(name)
    rec = _record(row, name)
    if not rec["eyes"] or not calc.has_measurement(rec):
        log(f"✗ {log_name} {date} 没有任何可录入的测量值"
            f"（两眼既无 AL 也无 r1+r2）")
        return 1

    calc.calc_record(rec, lenses, target=meta["target_refraction"], step=meta["step"])
    if not calc.calcable(rec):
        # 度数算不出就不出晶体单 —— 与管线行为保持一致，免得排障时以为
        # 「render 能出图、管线却不出」是 bug。
        log(f"✗ {log_name} {date} 度数不可算（缺 AL 或缺 K），本命令只出晶体单，"
            f"故无图。这类检查在管线里只发 OAM/KER 结构化实例。")
        return 1

    os.makedirs(a.outdir, exist_ok=True)
    written = []
    for L in lenses:
        if a.lens and a.lens.lower() != L["name"].lower():
            continue
        struct = sheet.build(rec, L, target=meta["target_refraction"],
                             step=meta["step"])
        png = report.render_png(struct, axes=_axes(rec),
                                institution=_institution(cfg))
        path = os.path.join(a.outdir, f"{name}_{date}_{L['name']}.png")
        open(path, "wb").write(png)
        written.append(path)
        log(f"  {L['name']:<12} → {path}  ({len(png) // 1024}KB)")

    # OAM 的关键字段也打出来（本地排障时确认状态映射对不对）
    pid = patient.make_pid(rec["dob"], name)
    study_uid = dicom.study_uid_for(pid, rec.get("date"))
    ed = oam.build(rec, cfg, pid, study_uid,
                   oam.series_uid_for(study_uid),
                   oam.sop_uid_for(study_uid))
    lens_me = ed.OphthalmicAxialMeasurementsRightEyeSequence[0] \
        .LensStatusCodeSequence[0].CodeMeaning \
        if "OphthalmicAxialMeasurementsRightEyeSequence" in ed else "—"
    vit_me = ed.OphthalmicAxialMeasurementsRightEyeSequence[0] \
        .VitreousStatusCodeSequence[0].CodeMeaning \
        if "OphthalmicAxialMeasurementsRightEyeSequence" in ed else "—"
    log(f"  {'OAM':<12} → Modality={ed.Modality}  Lens={lens_me}  "
        f"Vitreous={vit_me}")

    # KER 的陡/平是按半径判的，排障时要能一眼看出判对没有
    ker = keratometry.build(rec, cfg, pid, study_uid,
                            keratometry.series_uid_for(study_uid),
                            keratometry.sop_uid_for(study_uid))
    if ker is not None:
        for side, attr in (("OD", "KeratometryRightEyeSequence"),
                           ("OS", "KeratometryLeftEyeSequence")):
            seq = getattr(ker, attr, None)
            if not seq:
                log(f"  {'KER ' + side:<12} → 未测量")
                continue
            st = seq[0].SteepKeratometricAxisSequence[0]
            fl = seq[0].FlatKeratometricAxisSequence[0]
            log(f"  {'KER ' + side:<12} → Steep r={st.RadiusOfCurvature:.2f} "
                f"K={st.KeratometricPower:.2f} @{st.KeratometricAxis:.0f}°  "
                f"Flat r={fl.RadiusOfCurvature:.2f} "
                f"K={fl.KeratometricPower:.2f} @{fl.KeratometricAxis:.0f}°")
    else:
        log(f"  {'KER':<12} → 双眼都缺 K/r，不生成")

    print()
    print(f"{name}  {date}  dob={rec['dob']}  "
          f"sex={rec.get('sex') or '—'}  PID={pid}")
    for side in ("OD", "OS"):
        e = rec["eyes"].get(side)
        if not e:
            continue
        print(f"  {side}: AL={e['al']:.2f}mm  K1={e['k1']:.2f}D  "
              f"K2={e['k2']:.2f}D  ACD={e['acd'] or '—'}  axis={e.get('axis') or '—'}")
    return 0


def cmd_audit(a):
    """只读审计：按当前 MDB 重算，找出 state 与真实数据不一致的病例。

    ★ 全程只读：不推 PACS、不改 state、不写任何文件。可以在生产上随便跑。

    检查三件事
    ----------
    1. 指纹不符  —— done 里记的 fp 与当前 MDB 的内容指纹不同
                    = 设备补测过，PACS 里是旧版（本次事故的形态）
    2. 实例数不符 —— 按当前数据本该发 N 个，done 里记的是 M 个
    3. 缺指纹    —— 本改动之前写的老记录，无法判断是否陈旧

    为什么不能只看第 2 条
    --------------------
    实例数相同不代表内容相同：补测后仍是 6 个实例，但晶体单上的度数
    已经变了。只有指纹能发现这种。
    """
    try:
        cfg, lenses, meta = config.build(a.config)
    except Exception as e:
        log(f"✗ 配置/常数装载失败: {e}")
        return 2

    mdb_path = a.mdb or cfg.get("mdb_path")
    if not mdb_path or not os.path.exists(mdb_path):
        log(f"✗ MDB 不存在: {mdb_path}")
        return 2
    try:
        mdb.assert_supported(mdb_path)
        groups = mdb.load_measurements(mdb_path)
    except Exception as e:
        log(f"✗ 读取 MDB 失败: {e}")
        return 2

    try:
        state = json.load(open(a.state, encoding="utf-8"))
    except Exception as e:
        log(f"✗ 读 state 失败（{a.state}）: {e}")
        return 2
    if a.limit < 0:
        log(f"✗ --limit 不能为负（{a.limit}）")
        return 2
    done = state.get("done") or {}
    stale = state.get("stale") or {}

    print(f"MDB      {mdb_path}")
    print(f"state    {a.state}")
    print(f"done     {len(done)} 条   stale {len(stale)} 条"
          f"   cooling {len(state.get('cooling') or {})} 条")
    print()

    changed, mismatch, nofp, missing, emptied = [], [], [], [], []
    # dry/empty 的历史污染记录：从没真正发过实例。单列、不计入 bad，
    # 因为下次 run_once 扫描会自动清掉重扫（见 run_once 里的清理逻辑）。
    polluted = []
    checked = 0
    for (fam, giv, date), rows in groups.items():
        key = f"{fam}|{giv}|{date}"
        if a.since and date < a.since:
            continue
        row = mdb.pick_latest(rows)
        name = f"{fam}{giv}" if _has_cjk(fam + giv) else f"{fam} {giv}".strip()
        rec = _record(row, name)
        d = done.get(key)
        # ★ 先判「这条推过没有」，再判「现在还有没有测量值」。
        #   反过来会把「已推内容被清空」静默排除掉 —— 那种情况下 PACS
        #   里仍留着旧内容，恰恰是最需要人看的一种。
        #
        # ★ dry / empty 都不是「已推过」：
        #     dry   = 旧 bug 写的干跑记录，一个实例都没发出去；
        #     empty = 当时没有任何可录入的测量值，同样一个实例都没发。
        #   它们必须与「已推记录」分开统计，否则 audit 的「未发现不一致」
        #   会被误读成「PACS 里已经有这条检查」。
        #   这类记录下一次 run_once 扫描会自动清掉重扫，不需要人工介入，
        #   所以单列一类、不计入 bad。
        if not isinstance(d, dict):
            continue
        if d.get("dry") or d.get("src") == "empty":
            polluted.append((key, name, date,
                             "dry" if d.get("dry") else "empty"))
            continue
        checked += 1
        if not rec["eyes"] or not calc.has_measurement(rec):
            emptied.append((key, name, date, d.get("instances")))
            continue
        try:
            calc.calc_record(rec, lenses, target=meta["target_refraction"],
                             step=meta["step"])
            now_n = _expected_instances(rec, lenses)
        except Exception:
            now_n = None
        fp = _fingerprint(rec)
        old_fp = d.get("fp")
        was_n = d.get("instances")

        if not old_fp:
            nofp.append((key, name, date, was_n, now_n))
            # 老记录没指纹，退化为只比实例数
            if now_n is not None and was_n is not None and now_n != was_n:
                mismatch.append((key, name, date, was_n, now_n))
            continue
        if old_fp != fp:
            changed.append((key, name, date, was_n, now_n, old_fp, fp))
        elif now_n is not None and was_n is not None and now_n != was_n:
            mismatch.append((key, name, date, was_n, now_n))

    # done 里指向已不在库中的检查（设备归档/清库）
    # ★ --since 也要作用于 missing，否则按日期筛完再看到一堆历史缺失项，
    #   会让人误以为「最近丢了这么多」。
    live = {f"{f}|{g}|{d}" for (f, g, d) in groups}
    missing = []
    for k in done:
        if k in live:
            continue
        try:
            kdate = k.rsplit("|", 1)[1]
        except IndexError:
            kdate = ""
        if a.since and kdate and kdate < a.since:
            continue
        missing.append(k)

    def section(title, items, fmt):
        print(f"{title}  {len(items)} 条")
        for it in items[:a.limit or 30]:
            print("   " + fmt(it))
        if a.limit == 0 and len(items) > 30:
            print(f"   … 其余 {len(items) - 30} 条（--limit 0 只看前 30）")
        print()

    print(f"核对 {checked} 条已推记录\n")
    section("⚠ 内容已变（设备补测过，PACS 里是旧版）", changed,
            lambda x: f"{x[1]} {x[2]}  实例 {x[3]}→{x[4]}  "
                      f"fp {x[5]}→{x[6]}")
    section("⚠ 实例数不符（按当前数据重算）", mismatch,
            lambda x: f"{x[1]} {x[2]}  记录 {x[3]} 个，重算 {x[4]} 个")
    section("⚠ 已推内容现在没有有效测量值（PACS 里仍是旧版）", emptied,
            lambda x: f"{x[1]} {x[2]}  记录 {x[3]} 个，当前无可录入值")
    section("· 从未真正发送过的历史记录（dry/empty，下次扫描会自动重扫）",
            polluted,
            lambda x: f"{x[1]} {x[2]}  {x[3]}")
    section("· 无指纹（老记录，无法判断是否陈旧）", nofp,
            lambda x: f"{x[1]} {x[2]}  记录 {x[3]} 个，重算 {x[4]} 个")
    section("· 库中已无对应检查", missing, lambda k: k)

    # ★ 清单必须用和结论**同一份**过滤结果，否则会出现
    #   「清单里有一堆历史 stale，结论却说 0 条」的误导。
    stale_shown = [kv for kv in stale.items()
                   if not (a.since and str((kv[1] or {}).get("date") or "")
                           and str((kv[1] or {}).get("date") or "") < a.since)]
    if stale_shown:
        print(f"state.stale 里待确认 {len(stale_shown)} 条（已计入结论）：")
        for k, v in stale_shown[:a.limit or 10]:
            print(f"   {v.get('name')} {v.get('date')}  "
                  f"记录 {v.get('was', {}).get('instances')} 个 → "
                  f"重算 {v.get('now_instances')} 个  （{v.get('t')}）")
        print()

    # ★ 按 key 去重后计数：四类之间**有意重叠**（无指纹且实例数不符
    #   会同时出现在两处），直接相加会重复。
    # ★ stale 也必须计入：从 failed 转来的 stale 没有 done，主循环根本
    #   看不到它。只打印不计数会让 cron 的退出码监控漏报「PACS 里还是
    #   旧内容」这种最需要人看的情况。
    needs = set()
    for x in changed:
        needs.add(x[0])
    for x in mismatch:
        needs.add(x[0])
    for x in emptied:
        needs.add(x[0])
    for k, v in stale.items():
        if not isinstance(v, dict):
            continue
        if a.since:
            kdate = str(v.get("date") or "")
            if kdate and kdate < a.since:
                continue
        needs.add(k)
    bad = len(needs)
    print("结论：" + (f"发现 {bad} 条需要处理" if bad else "未发现不一致 ✓"))
    if changed or emptied:
        print("  这些病例需人工确认后重推。注意 OAM/KER 的 SOP UID")
        print("  不随内容变化，直接重推会被 PACS 静默去重（返回成功但保留旧版）")
        print("  —— 必须先删 PACS 旧实例再重推，见 docs/operations.md。")
    return 0 if not bad else 1


# ---------------------------------------------------------------- 参数

def build_parser():
    ap = argparse.ArgumentParser(
        prog="pipeline.py",
        description="IOLMaster MDB → 四公式晶体单 → DICOM → PACS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("公式名按 IOLCon 写法：SRK/T · Haigis · Hoffer Q · Holladay 1\n\n"
                f"⚠️  {SCOPE_WARNING}\n"
                "    完整声明见 DISCLAIMER.md"))
    ap.add_argument("-V", "--version", action="version",
                    version=f"iol2dcm {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=False)

    def common(p, push=True):
        p.add_argument("--config", help="config.yaml 路径")
        p.add_argument("--mdb", help="IolmXP.MDB 路径")
        if push:
            p.add_argument("--dicom-host", help="DICOM 目标主机")
            p.add_argument("--dicom-port", type=int, help="DICOM 端口（默认 4242）")
            p.add_argument("--aet", help="Called AE Title（默认 ORTHANC）")

    # watch
    p = sub.add_parser("watch", help="常驻：监视 MDB mtime，有新检查就出单")
    common(p)
    p.add_argument("--interval", type=int, help="轮询间隔秒（默认取 config）")
    p.add_argument("--state", default="/var/lib/iol2dcm/state.json")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--max-seconds", type=int, default=300)
    p.add_argument("--since", default="")
    p.add_argument("--workers", type=int, default=0,
                   help="并发进程数（0=用 config.yaml 的 performance.workers，"
                        "1=串行）")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_watch)

    # run
    p = sub.add_parser("run", help="跑一轮后退出（cron / 补数）")
    common(p)
    p.add_argument("--state", default="/var/lib/iol2dcm/state.json")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--max-seconds", type=int, default=0)
    p.add_argument("--since", default="", help="只处理 >= 该日期的检查（YYYYMMDD）")
    p.add_argument("--workers", type=int, default=0,
                   help="并发进程数（0=用 config.yaml 的 performance.workers，"
                        "1=串行）")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_run)

    # audit
    p = sub.add_parser(
        "audit",
        help="只读审计：按当前 MDB 重算，找出 state 与数据不一致的病例")
    p.add_argument("--config", help="config.yaml 路径")
    p.add_argument("--mdb", help="IolmXP.MDB 路径")
    p.add_argument("--state", default="/var/lib/iol2dcm/state.json")
    p.add_argument("--since", default="",
                   help="只核对 >= 该日期的检查（YYYYMMDD）")
    p.add_argument("--limit", type=int, default=0,
                   help="每类最多列出几条（0=前 30 条）")
    p.set_defaults(func=cmd_audit)

    # constants
    p = sub.add_parser("constants", help="抓取 IOLCon 常数 → JSON")
    p.add_argument("-o", "--output", help="输出路径")
    p.add_argument("--from-xml", help="解析本地 XML，不联网")
    p.add_argument("--show", help="只打印匹配型号，不写文件")
    p.add_argument("--local", action="store_true",
                   help="配合 --show：查本地 JSON，不联网（毫秒级）")
    p.add_argument("--save-xml", nargs="?", const="",
                   help="另存原始 XML（不给路径则存到 JSON 同目录）")
    p.add_argument("--timeout", type=int, default=180)
    p.set_defaults(func=cmd_constants)

    # lenses
    p = sub.add_parser("lenses", help="列出上表型号与常数")
    p.add_argument("--config")
    p.set_defaults(func=cmd_lenses)

    # check
    p = sub.add_parser("check", help="自检各环节")
    common(p)
    p.add_argument("--soft-push", action="store_true",
                   help="推送目标不可达只警告、不影响退出码（install.sh 用）")
    p.set_defaults(func=cmd_check)

    # probe
    p = sub.add_parser("probe", help="探测 DICOM 目标是否在线（TCP + C-ECHO）")
    common(p)
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.set_defaults(func=cmd_probe)

    # render
    p = sub.add_parser("render", help="只渲染到本地 PNG（不推 PACS）")
    common(p, push=False)
    p.add_argument("--patient", help="姓名子串")
    p.add_argument("--date", help="检查日期 YYYYMMDD")
    p.add_argument("--lens", help="只渲染某型号")
    p.add_argument("--outdir", default="./out")
    p.set_defaults(func=cmd_render)

    return ap


# ★ 新增子命令时必须同步加进这个集合：main() 靠它判断「argv 里有没有
#   出现子命令词」，漏加的话新命令会被当成 watch 的参数，报
#   "unrecognized arguments: audit" —— 而 --help 里却明明列着它。
SUBCOMMANDS = {"watch", "run", "audit", "constants", "lenses", "check",
               "render", "probe"}

# 只在顶层 parser 上成立的选项：单独出现时不该被当成 watch 的参数
TOP_ONLY = {"-h", "--help", "-V", "--version"}


def main(argv=None):
    """入口。

    ★ 无子命令 = watch（装完 systemd 直接跑，不用加参数）：
        pipeline.py                              → watch
        pipeline.py --mdb /path --interval 60    → watch
        pipeline.py run --limit 5                → run
        pipeline.py check                        → check

    ★ 判据是「argv 里有没有出现子命令词」，不是「第一个参数是不是选项」——
      因为 watch 自己也有 --mdb / --interval 这些选项。
    """
    raw = list(argv if argv is not None else sys.argv[1:])

    if not any(tok in SUBCOMMANDS for tok in raw):
        # ★ 例外：只给 -h/--help/-V/--version 时不要前缀 watch ——
        #   否则 'pipeline.py --help' 打出的是 watch 的帮助，看不到子命令
        #   列表，'pipeline.py -V' 更是直接报 unrecognized arguments。
        #   带了其他参数（如 --mdb x -h）才按 watch 解释，那里用户要的是
        #   watch 自己的选项说明。
        if not (raw and all(tok in TOP_ONLY for tok in raw)):
            raw = ["watch"] + raw

    ap = build_parser()
    a = ap.parse_args(raw)
    return a.func(a) or 0


if __name__ == "__main__":
    sys.exit(main())
