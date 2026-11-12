# -*- coding: utf-8 -*-
"""部署前只读预演：真实 MDB + state **副本**，验证「一行一案例」迁移。

不写 /opt/iol2dcm，不改生产 state，不推 PACS。

跑法（在 /tmp/verify_new 里）：

    cp /var/lib/iol2dcm/state.json ./state_copy.json
    python3 tools/verify_before_deploy.py \
        --mdb /storage/iolmaster/IOLData/IolmXP.MDB \
        --state ./state_copy.json --src /tmp/verify_new

报告三件事：
  1. 迁移账：改名多少条、拆多少组、新增多少案例、孤儿多少
  2. 零重推证明：单行组是否原样继承、多行组继承的是哪一行
  3. 迁移后 audit 结论与退出码
"""
from __future__ import annotations

import argparse
import copy
import io
import json
import os
import sys
from contextlib import redirect_stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mdb", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--src", default=".", help="代码目录（含 pipeline.py）")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    sys.path.insert(0, os.path.abspath(a.src))
    import pipeline as P
    import sources.mdb as mdb
    from core import config, patient

    cfg, lenses, meta = config.build(None)
    print(f"代码版本 {P.__version__} | 键方案 {P.KEY_SCHEME} | "
          f"cooling {P._cooling_seconds(cfg)}")
    print()

    mdb.assert_supported(a.mdb)
    groups = mdb.load_measurements(a.mdb)
    with open(a.state, encoding="utf-8") as f:
        state = json.load(f)
    before = copy.deepcopy(state)
    done_before = before.get("done") or {}

    print(f"MDB  {len(groups)} 组 / "
          f"{sum(len(r) for r in groups.values())} 行")
    print(f"state done {len(done_before)} 条 | "
          f"键方案 {state.get('key_scheme') or '(未迁移)'}")
    print()

    if state.get("key_scheme") == P.KEY_SCHEME:
        print("该 state 已是新键方案，无需迁移。")
        return 0

    # ---- 1) 迁移（只动内存副本）----
    mig = P._migrate_keys(state, groups)
    done = state.get("done") or {}
    print("=== 迁移账 ===")
    print(f"  改名（done/failed/cooling/stale 合计）: {mig.get('renamed', 0)}")
    print(f"  需拆分的多行组                      : {mig.get('split_groups', 0)}")
    print(f"  多行组将新增的案例数                : {mig.get('new_cases', 0)}")
    print(f"  孤儿（库中找不到，保留原样）        : {mig.get('orphan', 0)}")
    print(f"  撞键                                : {mig.get('conflict', 0)}")
    print(f"  迁移后 done 条数                    : {len(done)}")
    print()

    # ---- 2) 零重推证明 ----
    old_to_new = {}
    for old in done_before:
        parts = old.split("|")
        if len(parts) != 3:
            continue
        rows = groups.get(tuple(parts))
        if not rows:
            continue
        src_row, _ = mdb.pick_row(rows, already_pushed=True)
        old_to_new[old] = (f"{src_row['guid']}|{parts[2]}", len(rows), src_row)

    single = [k for k, v in old_to_new.items() if v[1] == 1]
    multi = [k for k, v in old_to_new.items() if v[1] > 1]
    ok_single = sum(1 for k in single
                    if done.get(old_to_new[k][0], {}).get("uid_seed") == ""
                    and done.get(old_to_new[k][0], {}).get("fp")
                    == done_before[k].get("fp")
                    and done.get(old_to_new[k][0], {}).get("pid")
                    == done_before[k].get("pid"))
    ok_multi = sum(1 for k in multi
                   if done.get(old_to_new[k][0], {}).get("uid_seed") == "")

    print("=== 零重推证明 ===")
    print(f"  单行组原样继承（fp/pid 未变、uid_seed=\"\"）: "
          f"{ok_single}/{len(single)}")
    print(f"  多行组：继承行 uid_seed=\"\"                 : "
          f"{ok_multi}/{len(multi)}")
    print()
    print("  ★ uid_seed=\"\" 表示用旧的 study 种子，UID 与当年推的一致")
    print("    → 指纹相同即跳过，不会重推。")
    print()

    # 新案例预估推送量
    # ★ v1.3.3：不再有跨天闸门，判据只剩「信息是否充足」。
    new_cases = []
    for old in multi:
        new_key, n, src_row = old_to_new[old]
        parts = old.split("|")
        for r in groups[tuple(parts)]:
            k = f"{r['guid']}|{parts[2]}"
            if k == new_key:
                continue
            rec = P._record(r, parts[0] + parts[1])
            if rec["eyes"] and P.calc.has_measurement(rec):
                new_cases.append((k, parts, r))
    print(f"=== 预计新增推送 ===")
    print(f"  多行组里会成为新案例并推送的: {len(new_cases)} 个")
    for k, parts, r in new_cases[:a.limit or 20]:
        print(f"    {parts[0]}{parts[1]} {parts[2]}  "
              f"采集={r.get('measure_date') or '?'} "
              f"差{P._exam_gap_days(r)}天")
    print()
    print("  ★ 这些新推送用 guid 作 study 种子 → SOP UID 全新，")
    print("    与已推内容不冲突（已核 study_uid_for 的兼容锚点）。")
    print()

    # ---- 3) 迁移后 audit（只读，内存副本）----
    saved = (mdb.assert_supported, mdb.load_measurements)
    mdb.assert_supported = lambda *x, **k: {"rows": 0}
    mdb.load_measurements = lambda *x, **k: groups
    sf = a.state + ".audit-sim"
    try:
        with open(sf, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = P.cmd_audit(type("A", (), {
                "config": None, "mdb": a.mdb, "state": sf,
                "since": "", "limit": a.limit or 0, "show_ident": False})())
        out = buf.getvalue()
    finally:
        mdb.assert_supported, mdb.load_measurements = saved
        try:
            os.remove(sf)
        except OSError:
            pass

    print("=== 迁移后 audit ===")
    for ln in out.splitlines():
        if ln.startswith(("⚠", "结论：", "核对 ")):
            print(" ", ln)
    print("  exit =", rc)
    print()
    print("说明：本脚本只在内存副本上迁移，**不会写 state**。")
    print("真正的迁移在 run_once 里做一次，并立即落盘（见 KEY_SCHEME）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
