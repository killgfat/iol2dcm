#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IOLCon 匿名 XML → 单一常数表 JSON（四公式口径）

数据来源只有一处：https://iolcon.org/downloadLenses.php?action=download
但同一款晶体在 XML 里可能有三种 <Constants> 块，本脚本按优先级合成一条记录：

    optimized  >  ulib  >  nominal

     optimized  IOLCon 用临床数据回归的优化值（带 results 样本数）—— 最优先
     ulib       ULIB 平台优化值（92 款，IOLCon 转存）
     nominal    厂商声明值（无优化数据时的兜底）

★ 只保留四个公式需要的字段：
      SRK/T      → srkt_a
      Haigis     → haigis {a0, a1, a2}
      Hoffer Q   → hoffer_q_pacd
      Holladay 1 → holladay1_sf

  其他字段（SRK II、Barrett LF/DF、Olsen、Castrop、型号规格参数）一律不写，
  让 JSON 只服务这一条管线，避免下游误用。

缺值处理
--------
某个公式在三个来源里都拿不到 → 该字段写 null，并在 missing_formulas 列出。
不做任何换算兜底（如用 A 常数反推 pACD）——那是隐式的、不可追溯的口径改变。

用法
----
    python3 pipeline.py constants                       # 联网抓取并重建
    python3 pipeline.py constants --from-xml dl.xml     # 离线解析本地 XML
    python3 pipeline.py constants --show "Nidek SZ-1"   # 只看某款，不写文件

★ 合规：IOLCon 条款 8.1 只允许为自身计算目的使用数据、禁止再分发。
  生成的 JSON 是本机自用缓存，不进公开仓库、不转交第三方。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime

IOLCON_URL = "https://iolcon.org/downloadLenses.php?action=download"
SCHEMA_VERSION = 3
HERE = os.path.dirname(os.path.abspath(__file__))   # .../sources
ROOT = os.path.dirname(HERE)   # 项目根：常数表输出到这里
DATA_DIR = os.environ.get("IOL2DCM_DATA", ROOT)
DEFAULT_OUT = os.path.join(DATA_DIR, "iolcon_constants.json")

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# 四公式字段：JSON 里只保留这些
FORMULAS = ("SRK/T", "Haigis", "Hoffer Q", "Holladay 1")


def fetch_xml(timeout=180):
    """下载 IOLCon XML。

    ★ 必须发 Accept-Encoding: gzip（2026-11-06 实测）：
      服务器支持 gzip，1.1MB 的 XML 压到 34KB。
      不发这个头时是裸传，iolcon.org 出数据只有 ~12KB/s，要 93 秒；
      带上之后 3~10 秒，快 10~30 倍。

    ★ 手工解压而不是让 urllib 代劳：urllib 不会自动解压，
      必须自己看 Content-Encoding。gzip/deflate 都兜一下。
    """
    import gzip
    import urllib.request
    import zlib

    req = urllib.request.Request(IOLCON_URL, headers={
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        enc = (r.headers.get("Content-Encoding") or "").lower().strip()

    if enc == "gzip" or raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    elif enc == "deflate":
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw.decode("utf-8", "replace")


def _f(text):
    if text is None:
        return None
    t = str(text).strip()
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _read_block(node):
    """读一个 <Constants> 节点，只取四公式相关字段。"""
    results = None
    r = node.get("results")
    if r is not None:
        try:
            results = int(r)
        except ValueError:
            results = None

    h = node.find("Haigis")
    return {
        "srkt_a": _f(node.findtext("SRKt")),
        "haigis": {
            "a0": _f(h.findtext("a0")) if h is not None else None,
            "a1": _f(h.findtext("a1")) if h is not None else None,
            "a2": _f(h.findtext("a2")) if h is not None else None,
        },
        "hoffer_q_pacd": _f(node.findtext("HofferQ")),
        "holladay1_sf": _f(node.findtext("Holladay1")),
        "results": results,
    }


def _block_score(blk):
    """该块覆盖了几个公式（0–4）。"""
    if not blk:
        return 0
    n = 0
    if blk.get("srkt_a") is not None:
        n += 1
    h = blk.get("haigis") or {}
    if all(h.get(k) is not None for k in ("a0", "a1", "a2")):
        n += 1
    if blk.get("hoffer_q_pacd") is not None:
        n += 1
    if blk.get("holladay1_sf") is not None:
        n += 1
    return n


def parse(xml_text):
    """XML → 记录列表，每条已按 optimized > ulib > nominal 合成。"""
    root = ET.fromstring(xml_text)
    downloaded = root.get("downloaded", "")
    recs = []

    for lens in root.findall("Lens"):
        brand = (lens.findtext("Manufacturer") or "").strip()
        model = (lens.findtext("Name") or "").strip()
        if not brand and not model:
            continue

        blocks = {"optimized": None, "ulib": None, "nominal": None}
        for c in lens.findall("Constants"):
            t = (c.get("type") or "").strip().lower()
            if t in blocks:
                blk = _read_block(c)
                # optimized 块 results=0 且字段全空时视为无效
                if t == "optimized" and _block_score(blk) == 0:
                    continue
                blocks[t] = blk

        # ★ 优先级：optimized > ulib > nominal，逐公式取第一个非空值。
        #   不做整块二选一——某款可能 optimized 只给了 SRK/T 和 Hoffer Q，
        #   而 ulib 给了完整的四个，逐字段合并才能拿到最多信息。
        merged = {"srkt_a": None, "haigis": {"a0": None, "a1": None, "a2": None},
                  "hoffer_q_pacd": None, "holladay1_sf": None}
        sources = {}
        for name in ("optimized", "ulib", "nominal"):
            blk = blocks.get(name)
            if not blk:
                continue
            if merged["srkt_a"] is None and blk.get("srkt_a") is not None:
                merged["srkt_a"] = blk["srkt_a"]
                sources["SRK/T"] = name
            bh = blk.get("haigis") or {}
            if merged["haigis"]["a0"] is None and bh.get("a0") is not None:
                merged["haigis"] = {"a0": bh.get("a0"), "a1": bh.get("a1"),
                                    "a2": bh.get("a2")}
                sources["Haigis"] = name
            if merged["hoffer_q_pacd"] is None and blk.get("hoffer_q_pacd") is not None:
                merged["hoffer_q_pacd"] = blk["hoffer_q_pacd"]
                sources["Hoffer Q"] = name
            if merged["holladay1_sf"] is None and blk.get("holladay1_sf") is not None:
                merged["holladay1_sf"] = blk["holladay1_sf"]
                sources["Holladay 1"] = name

        missing = [f for f in FORMULAS if f not in sources]
        # 样本数：取 optimized 的 results（若有）
        results = (blocks.get("optimized") or {}).get("results")

        recs.append({
            "key": f"{brand} {model}".strip(),
            "brand": brand,
            "model": model,
            "id": lens.get("id"),
            "srkt_a": merged["srkt_a"],
            "haigis": merged["haigis"],
            "hoffer_q_pacd": merged["hoffer_q_pacd"],
            "holladay1_sf": merged["holladay1_sf"],
            "results": results,
            "sources": sources,
            "missing_formulas": missing,
            "complete": not missing,
        })
    return downloaded, recs


# ---------------------------------------------------------------- 指纹与差异

# 参与指纹的字段：只有这四个公式常数会进入计算。
# 材质、直径、Toric 这些规格字段不参与 —— 它们变了不影响出单结果。
FINGERPRINT_FIELDS = ("srkt_a", "haigis", "hoffer_q_pacd", "holladay1_sf")


def fingerprint(rec):
    """一款晶体的常数指纹。四公式数值一致 = 同一个指纹。

    用途：IOLCon 修正某款常数时，本地能发现「这款变了」，
    而不是只有 count 相同就以为一切照旧。
    """
    payload = json.dumps(
        [rec.get(f) for f in FINGERPRINT_FIELDS],
        sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _ident(rec):
    """唯一标识一条记录。

    ★ 不能用 key：IOLCon 上有 7 组同名不同 id 的条目
      （Ophtec AC 205 的 id 653 A=115.7 / id 654 A=116.9 是两款晶体）。
      按 key 归并会把 646 款压成 639，差异报告的数字就永远对不上。
    """
    return f"{rec['key']}#{rec.get('id') or ''}"


def diff(old_doc, new_recs):
    """对比旧 JSON 与新解析结果，返回差异明细。

    返回 dict：
      added    [标识, ...]     新表有、旧表没有
      removed  [标识, ...]     旧表有、新表没有
      changed  [(标识, 旧, 新), ...]  两边都有但常数变了
      same     int             完全没变的数量
    """
    old = {_ident(r): r.get("fp") or fingerprint(r)
           for r in (old_doc or {}).get("lenses", [])}
    new = {_ident(r): fingerprint(r) for r in new_recs}

    added = sorted(k for k in new if k not in old)
    removed = sorted(k for k in old if k not in new)
    changed = sorted(
        (k, old[k], new[k]) for k in new if k in old and old[k] != new[k])
    same = sum(1 for k in new if k in old and old[k] == new[k])
    return {"added": added, "removed": removed,
            "changed": changed, "same": same}


def report_diff(d, stream=None):
    """把 diff 结果打成几行，给终端看。"""
    stream = stream or sys.stderr
    n_a, n_r, n_c = len(d["added"]), len(d["removed"]), len(d["changed"])
    if not (n_a or n_r or n_c):
        print(f"  与旧表一致：{d['same']} 款常数无变化", file=stream)
        return
    print(f"  与旧表相比：新增 {n_a} / 变更 {n_c} / 移除 {n_r} / 不变 {d['same']}",
          file=stream)
    for k in d["added"]:
        print(f"    + {k}", file=stream)
    for k, _o, _n in d["changed"]:
        print(f"    ~ {k}", file=stream)
    for k in d["removed"]:
        print(f"    - {k}", file=stream)


def atomic_write(path, doc):
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".const_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成四公式晶体常数表")
    ap.add_argument("-o", "--output", default=DEFAULT_OUT)
    ap.add_argument("--from-xml", help="解析本地 XML，不联网")
    ap.add_argument("--show", help="打印匹配型号的解析结果，不写文件")
    ap.add_argument("--local", action="store_true",
                    help="配合 --show：查本地已有 JSON，不联网（毫秒级）")
    ap.add_argument("--save-xml", nargs="?", const="",
                    help="把原始 XML 另存一份（便于离线重解析）。"
                         "不给路径则存到 JSON 同目录的 iolcon_lenses.xml")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--min-ratio", type=float, default=0.5,
                    help="新表款数低于旧表的这个比例时拒绝替换（防页面改版），默认 0.5")
    args = ap.parse_args(argv)

    # --show --local：只查本地表，一个字节都不下。编辑 config.yaml 时用这个。
    if args.show and args.local:
        if not os.path.exists(args.output):
            print(f"✗ 本地没有常数表: {args.output}", file=sys.stderr)
            return 2
        with open(args.output, encoding="utf-8") as f:
            doc = json.load(f)
        pat = args.show.lower()
        hits = [r for r in doc.get("lenses", [])
                if pat in r["key"].lower() or pat in (r.get("model") or "").lower()]
        if not hits:
            print(f"本地表里没有匹配 {args.show!r} 的型号", file=sys.stderr)
            return 1
        print(json.dumps(hits, ensure_ascii=False, indent=1))
        return 0

    if args.from_xml:
        xml_text = open(args.from_xml, encoding="utf-8", errors="replace").read()
    else:
        print(f"下载 {IOLCON_URL} ...", file=sys.stderr)
        xml_text = fetch_xml(args.timeout)

    # 顺手留一份原始 XML：以后想取 Specifications 里那 21 个字段
    # （Toric / 材质 / 直径…）不用再等一次下载。
    if args.save_xml is not None and not args.from_xml:
        xp = args.save_xml or os.path.join(
            os.path.dirname(os.path.abspath(args.output)), "iolcon_lenses.xml")
        os.makedirs(os.path.dirname(os.path.abspath(xp)) or ".", exist_ok=True)
        with open(xp, "w", encoding="utf-8") as f:
            f.write(xml_text)
        print(f"✓ 原始 XML → {xp}", file=sys.stderr)

    try:
        downloaded, recs = parse(xml_text)
    except ET.ParseError as e:
        print(f"✗ XML 解析失败（页面改版或下载不完整）: {e}", file=sys.stderr)
        return 3
    if not recs:
        print("✗ 解析出 0 款晶体", file=sys.stderr)
        return 3

    if args.show:
        pat = args.show.lower()
        hits = [r for r in recs if pat in r["key"].lower()]
        if not hits:
            print(f"未匹配到 {args.show!r}", file=sys.stderr)
            return 1
        print(json.dumps(hits, ensure_ascii=False, indent=1))
        return 0

    recs.sort(key=lambda x: x["key"].lower())
    # 指纹写进记录：下次更新时靠它判断「这款常数变了没有」
    for r in recs:
        r["fp"] = fingerprint(r)
    src_stat = {s: 0 for s in ("optimized", "ulib", "nominal")}
    for r in recs:
        for s in set(r["sources"].values()):
            src_stat[s] += 1

    doc = {
        "schema_version": SCHEMA_VERSION,
        "source": IOLCON_URL,
        "iolcon_downloaded": downloaded,
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "count": len(recs),
        "policy": "逐公式取 optimized > ulib > nominal 的第一个非空值",
        "formulas": list(FORMULAS),
        "stats": {
            "complete": sum(1 for r in recs if r["complete"]),
            "incomplete": sum(1 for r in recs if not r["complete"]),
            "with_results": sum(1 for r in recs if r["results"]),
            "fields_from_optimized": src_stat["optimized"],
            "fields_from_ulib": src_stat["ulib"],
            "fields_from_nominal": src_stat["nominal"],
        },
        "lenses": recs,
    }
    # 读旧表：一次读，两个用途 —— 款数保护 + 差异报告。
    old_doc = None
    if os.path.exists(args.output):
        try:
            with open(args.output, encoding="utf-8") as f:
                old_doc = json.load(f)
        except Exception:
            old_doc = None
    old_count = int((old_doc or {}).get("count") or 0)

    # 防页面改版/截断下载：新表款数骤减说明解析出了问题，宁可留着旧表不换。
    # 一旦把好表冲掉，整条管线就会因为「型号在常数表里找不到」而全线停摆。
    if old_count and len(recs) < old_count * args.min_ratio:
        print(f"✗ 新表只有 {len(recs)} 款，旧表 {old_count} 款 —— "
              f"降幅超过 {1 - args.min_ratio:.0%}，拒绝替换。\n"
              f"  IOLCon 页面可能改版了，先人工核对：\n"
              f"    {sys.argv[0]} --show <型号名> --from-xml <手工下载的 xml>",
              file=sys.stderr)
        return 4

    atomic_write(args.output, doc)

    # 差异报告：告诉现场「这次更新到底动了什么」。
    # 以前只有 count 一样就以为没变 —— 实际上某款常数可能被悄悄修正了。
    if old_doc is not None:
        report_diff(diff(old_doc, recs))

    s = doc["stats"]
    print(f"✓ {args.output}", file=sys.stderr)
    print(f"  {doc['count']} 款 | 四公式齐全 {s['complete']} / 缺值 {s['incomplete']}",
          file=sys.stderr)
    print(f"  字段来源：optimized {s['fields_from_optimized']} 款 / "
          f"ulib {s['fields_from_ulib']} 款 / nominal {s['fields_from_nominal']} 款",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
