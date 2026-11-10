#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置与常数装载：config.yaml + iolcon_constants.json

设计
----
* **只有一份常数表** `iolcon_constants.json`，由 `sources/constants.py` 从 IOLCon
  XML 生成。同一款晶体在 XML 里的 optimized / ulib / nominal 三块已按
  「逐公式取 optimized > ulib > nominal 的第一个非空值」合成完毕。
  独立 ULIB 快照已完全放弃。
* **型号清单在 config.yaml**，换型号改 yaml，不动代码。
* 缺值不兜底：某型号某公式拿不到常数 → 该格显示 —，并在装载结果里标记。
  绝不静默用别的值顶上（A 常数反推 pACD 这类换算会不可追溯地改变口径）。

YAML 解析
---------
目标机不一定装 PyYAML，所以这里带一个极简 YAML 子集解析器，
只支持本配置用到的语法：顶层 key: value、列表 `- name: x`、嵌套、
注释与空行。若系统有 PyYAML 则优先用它。
"""
from __future__ import annotations

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))   # .../core
ROOT = os.path.dirname(HERE)   # 项目根：config.yaml 与常数表在这里
DEFAULT_CONFIG = os.environ.get("IOL2DCM_CONFIG",
                               os.path.join(ROOT, "config.yaml"))

FORMULA_KEYS = ("SRK/T", "Haigis", "Hoffer Q", "Holladay 1")


class ConfigError(RuntimeError):
    pass


# ---------------------------------------------------------------- 性能参数

# 并发进程数上限。不是拍脑袋：瓶颈是 C-STORE 网络等待（见
# docs/architecture.md「并发处理」），4→8 只快 1.46×，再往上纯亏。
# 更重要的是内存 —— 每进程约 100MB，且每个进程都会开自己的 C-STORE 关联。
MAX_WORKERS = 16


def _check_performance(raw):
    """校验 performance 段。返回规整后的 dict（总是有 workers）。

    ★ 为什么不静默兜底：workers 配错（拼错成 worker、写成字符串、
      写成 999）如果在运行到一半才发现，前几例已经推给 PACS 了。
      装载阶段报错最省事。
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"config.yaml 的 performance 必须是映射，实际是 {type(raw).__name__}")

    w = raw.get("workers", 4)
    # YAML 里写 workers: "4" 也能用（字符串形式的整数），但：
    #   workers: abc  -> 报错
    #   workers: 3.7  -> 报错（int() 会静默截断成 3，等于悄悄改了用户意图）
    #   workers: 3.0  -> 允许（值是整数值的浮点，常见于模板渲染）
    if isinstance(w, bool):        # bool 是 int 的子类，True 会变成 1
        raise ConfigError(f"performance.workers 必须是整数，实际是 {w!r}")
    if isinstance(w, float):
        if not w.is_integer():
            raise ConfigError(
                f"performance.workers 必须是整数，实际是 {w!r}（不能有小数）")
        w = int(w)
    elif isinstance(w, str):
        s = w.strip()
        try:
            w = int(s)
        except ValueError:
            raise ConfigError(
                f"performance.workers 必须是整数，实际是 {raw.get('workers')!r}")
    elif not isinstance(w, int):
        raise ConfigError(
            f"performance.workers 必须是整数，实际是 {raw.get('workers')!r}")
    if w < 1:
        raise ConfigError(
            f"performance.workers 必须 >= 1（1 = 串行），实际是 {w}")
    if w > MAX_WORKERS:
        raise ConfigError(
            f"performance.workers 上限 {MAX_WORKERS}，实际是 {w}。"
            f"瓶颈是推送网络等待不是 CPU，再高只会浪费内存和 PACS 连接数")

    out = dict(raw)
    out["workers"] = w
    return out


# ---------------------------------------------------------------- YAML

def _strip_comment(raw):
    if "#" not in raw:
        return raw
    inq, q = False, ""
    for i, ch in enumerate(raw):
        if ch in "\"'":
            if inq and ch == q:
                inq = False
            elif not inq:
                inq, q = True, ch
        elif ch == "#" and not inq:
            return raw[:i]
    return raw


def _scalar(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    if s.lower() in ("true", "yes"):
        return True
    if s.lower() in ("false", "no"):
        return False
    if s.lower() in ("null", "none", "~", ""):
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _mini_yaml(text):
    """极简 YAML 子集解析，够读 config.yaml 即可。"""
    lines = []
    for raw in text.splitlines():
        raw = _strip_comment(raw)
        if raw.strip():
            lines.append(raw.rstrip())

    root = {}
    stack = [(-1, root, None, None)]   # (indent, container, list_parent, list_key)
    i = 0
    while i < len(lines):
        line = lines[i]
        indent = len(line) - len(line.lstrip())
        body = line.strip()

        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        _, cur, list_parent, list_key = stack[-1]

        if body.startswith("- "):
            if list_parent is None:
                raise ConfigError(f"列表项出现在非列表上下文: {line}")
            lst = list_parent[list_key]
            item = body[2:].strip()
            m = re.match(r"([A-Za-z_][\w-]*)\s*:\s*(.*)$", item)
            if m:
                d = {}
                lst.append(d)
                k, v = m.group(1), m.group(2).strip()
                d[k] = _scalar(v) if v else {}
                stack.append((indent, d, None, None))
            else:
                lst.append(_scalar(item))
            i += 1
            continue

        m = re.match(r"([A-Za-z_][\w-]*)\s*:\s*(.*)$", body)
        if not m:
            raise ConfigError(f"无法解析的行: {line}")
        k, v = m.group(1), m.group(2).strip()
        if v == "":
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if nxt.startswith("- "):
                cur[k] = []
                stack.append((indent, cur[k], cur, k))
            else:
                cur[k] = {}
                stack.append((indent, cur[k], None, None))
        else:
            cur[k] = _scalar(v)
        i += 1
    return root


def load_config(path=None):
    path = path or DEFAULT_CONFIG
    if not os.path.exists(path):
        raise ConfigError(f"配置文件不存在: {path}")
    text = open(path, encoding="utf-8").read()
    try:
        import yaml
        cfg = yaml.safe_load(text)
    except ImportError:
        cfg = _mini_yaml(text)
    if not isinstance(cfg, dict):
        raise ConfigError("配置文件顶层必须是字典")
    return cfg, os.path.dirname(os.path.abspath(path))


# ---------------------------------------------------------------- 常数表

def load_constants(path):
    if not os.path.exists(path):
        raise ConfigError(f"常数表不存在: {path}\n先跑 `python3 pipeline.py constants` 生成")
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    idx = {"exact": {}, "lower": {}, "model_exact": {}, "model_lower": {}}
    for r in doc.get("lenses", []):
        idx["exact"].setdefault(r["key"], r)
        idx["lower"].setdefault(r["key"].lower(), r)
        if r.get("model"):
            idx["model_exact"].setdefault(r["model"], r)
            idx["model_lower"].setdefault(r["model"].lower(), r)
    return doc, idx


def _twins(doc, key):
    """同名不同 id 的所有条目。IOLCon 上确实存在这种情况。

    Ophtec AC 205：id 653 A=115.7 / id 654 A=116.9 —— 两款不同晶体同名。
    按 key 直接取第一个会静默用错常数，所以必须先查有没有双胞胎。
    """
    return [r for r in doc.get("lenses", []) if r.get("key") == key]


def _find(idx, want, doc):
    """分级匹配：精确 > 型号精确 > 忽略大小写 > 型号忽略大小写 > 子串唯一。

    ★ 大小写敏感优先：AR40e 与 AR40E 是两个不同型号
      （Haigis a0 = +0.472 vs -2.42），只按 lower() 查会静默取错。

    ★ 同名多条目必须报错：IOLCon 有 7 组 key 相同但 id 与常数不同的条目。
      取第一个 = 悄悄用错 A 常数，而这在报告单上看不出来。
      需要指明时用 "key@id" 写法（如 "Ophtec AC 205@654"）。

    ★ 分隔符用 @ 不用 #：YAML 里 # 是注释起始符，
      "Ophtec AC 205#654" 会被截成 "Ophtec AC 205"，写进 config 就是白写。
    """
    w = str(want).strip()

    # 显式 id 写法：Ophtec AC 205@654
    if "@" in w:
        k, _, want_id = w.rpartition("@")
        hit = [r for r in doc.get("lenses", [])
               if r.get("key") == k.strip() and str(r.get("id")) == want_id.strip()]
        if len(hit) == 1:
            return hit[0]
        raise ConfigError(
            f"{w!r} 没找到（key={k.strip()!r} id={want_id.strip()!r}）。"
            f"该 key 下的 id 有：{[r.get('id') for r in _twins(doc, k.strip())]}")

    rec = (idx["exact"].get(w)
           or idx["model_exact"].get(w)
           or idx["lower"].get(w.lower())
           or idx["model_lower"].get(w.lower()))
    if rec is not None:
        twins = _twins(doc, rec["key"])
        if len(twins) > 1:
            detail = ", ".join(
                f"id={r.get('id')} A={r.get('srkt_a')} "
                f"a0={(r.get('haigis') or {}).get('a0')}" for r in twins)
            raise ConfigError(
                f"{w!r} 对应 {len(twins)} 个同名条目，常数不同，不能自动选：\n"
                f"    {detail}\n"
                f"  请在 config.yaml 里写成 {rec['key']}@<id> 指定其中一个。")
        return rec
    cand = [r for r in doc.get("lenses", []) if w.lower() in r["key"].lower()]
    if len(cand) == 1:
        return cand[0]
    if len(cand) > 1:
        raise ConfigError(
            f"{w!r} 在常数表里匹配到 {len(cand)} 款，请在 config.yaml 里写确切 key："
            f"{[c['key'] for c in cand]}")
    raise ConfigError(f"常数表里没有 {w!r}")


def build(config_path=None):
    """装载配置与常数。返回 (cfg, lenses, meta)。"""
    cfg, base = load_config(config_path)

    spec_list = cfg.get("lenses") or []
    if not spec_list:
        raise ConfigError("config.yaml 的 lenses 为空")

    cfile = cfg.get("constants_file") or "iolcon_constants.json"
    if not os.path.isabs(cfile):
        cfile = os.path.join(base, cfile)
    doc, idx = load_constants(cfile)

    calc = cfg.get("calculation") or {}
    target = float(calc.get("target_refraction", -0.5))
    step = float(calc.get("step", 0.5))

    # 并发数在装载阶段就校验 —— 配错要在启动时报，不能跑到一半
    # （int("abc") 会抛 ValueError，workers: 999 会真开 999 个进程）。
    cfg["performance"] = _check_performance(cfg.get("performance"))

    out = []
    for spec in spec_list:
        if isinstance(spec, str):
            spec = {"name": spec, "key": spec}
        name = spec.get("name") or spec.get("key")
        key = spec.get("key") or spec.get("name")
        if not name or not key:
            raise ConfigError(f"lenses 条目缺 name 或 key: {spec}")

        rec = _find(idx, key, doc)
        h = rec.get("haigis") or {}
        missing = list(rec.get("missing_formulas") or [])

        out.append({
            "name": name,
            "key": rec["key"],
            "srkt_a": rec.get("srkt_a"),
            "haigis": (h.get("a0"), h.get("a1"), h.get("a2")),
            "pacd": rec.get("hoffer_q_pacd"),
            "sf": rec.get("holladay1_sf"),
            "results": rec.get("results"),
            "sources": rec.get("sources") or {},
            "missing_formulas": missing,
            "note": (f"缺 {'/'.join(missing)} 常数" if missing else ""),
        })

    meta = {
        "constants_file": cfile,
        "constants_count": doc.get("count", 0),
        "iolcon_downloaded": doc.get("iolcon_downloaded", ""),
        "policy": doc.get("policy", ""),
        "target_refraction": target,
        "step": step,
        "cfg": cfg,
    }
    return cfg, out, meta
