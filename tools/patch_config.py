#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按 key 就地改写 config.yaml，保留注释、缩进与空行。

为什么不用 PyYAML
-----------------
``yaml.safe_dump()`` 会把整份文件的注释、空行、键序全部重排。而 config.yaml
的注释本身就是给现场医生看的说明书（型号名差一个字母就是另一个晶体的警告、
mdb_path 要按现场改的提示……），重排一遍就废了。

所以这里做行级替换：只动被点名的键的值，其余一个字节都不碰。

用法
----
    patch_config.py config.yaml mdb_path=/storage/x.MDB push.dicom_port=4242
    patch_config.py config.yaml --print          # 只列出当前值，不写盘

键的写法
--------
    顶层键      mdb_path
    段内键      push.dicom_host      report.institution

值
--
    空字符串 = 写成 ``""``（例如 report.institution 留空即不印院名）
    含 ``:`` ``#`` ``"`` 或首尾空格的值自动加双引号

退出码
------
    0  成功
    1  用法错误 / 文件读不了
    2  键在文件里找不到（模板结构变了，绝不静默跳过）
"""
from __future__ import annotations

import os
import re
import sys
import tempfile

TOP = re.compile(r'^([A-Za-z_][\w-]*)\s*:(.*)$')
SUB = re.compile(r'^(\s+)([A-Za-z_][\w-]*)\s*:(.*)$')

USAGE = """\
用法: patch_config.py <config.yaml> KEY=VALUE [KEY=VALUE ...]
      patch_config.py <config.yaml> --print

  KEY        顶层键        mdb_path
             段内键        push.dicom_port
  --print    打印当前可读的键值，不修改文件
"""


def split_comment(s):
    """拆出行内注释，只在引号外认 #。返回 (值部分, '#...' 或 '')。"""
    inq, q = False, ""
    for i, ch in enumerate(s):
        if ch in "\"'":
            if inq and ch == q:
                inq = False
            elif not inq:
                inq, q = True, ch
        elif ch == "#" and not inq:
            return s[:i].rstrip(), s[i:]
    return s.rstrip(), ""


def unquote(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def fmt(v):
    """Python 值 → 能直接写进 YAML 的字面量。

    ★ 优先用单引号：`core/config.py` 内置的 mini_yaml 解析器只做「首尾去引号」，
      **不还原反斜杠转义**。所以 `"a\\"b"` 在那里会读成 `a\\"b`。
      含双引号的值改单引号包裹，两边解析器都能原样读出。
    """
    s = "" if v is None else str(v)
    if s == "":
        return '""'
    # 含特殊字符或首尾空白就必须加引号
    if not (re.search(r'[:#\'"\n]', s) or s != s.strip()):
        return s
    if "'" not in s:
        return "'" + s + "'"      # 含双引号（或什么引号都没有）→ 单引号包裹
    if '"' not in s:
        return '"' + s + '"'      # 含单引号 → 双引号包裹
    # 两种引号都有：只能双引号 + 转义。PyYAML 下正确，
    # mini_yaml 下会残留反斜杠 —— 机构名不会走到这里，可以接受。
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def scan(lines, want):
    """逐行扫描。返回 (新行列表, 命中的键集合, 当前值字典)。

    只跟踪一层的段（config.yaml 最深也就两层），够用且不会误伤。
    """
    out, found, current = [], set(), {}
    section = None       # 当前所在的顶层段名；None = 顶层

    for line in lines:
        m = TOP.match(line)
        if m:
            key, rest = m.group(1), m.group(2)
            val, comment = split_comment(rest)
            if key in want:
                found.add(key)
                current[key] = unquote(val)
                line = f"{key}: {fmt(want[key])}" + (f"    {comment}" if comment else "")
            else:
                current[key] = unquote(val)
                # 值非空 = 叶子键，不改变当前段；值为空 = 段的开始
                section = key if not val.strip() else None
            out.append(line)
            continue

        m = SUB.match(line)
        if m:
            indent, key, rest = m.group(1), m.group(2), m.group(3)
            val, comment = split_comment(rest)
            full = f"{section}.{key}" if section else key
            current[full] = unquote(val)
            if full in want:
                found.add(full)
                line = (f"{indent}{key}: {fmt(want[full])}"
                        + (f"    {comment}" if comment else ""))
            out.append(line)
            continue

        out.append(line)

    return out, found, current


def read_lines(path):
    with open(path, encoding="utf-8") as f:
        return f.read().split("\n")


def write_lines(path, lines):
    """原子写：先落 .tmp 再 rename。写坏 config.yaml 等于把现场搞停摆。"""
    text = "\n".join(lines)
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        sys.stdout.write(USAGE)
        return 0

    path = argv[0]
    rest = argv[1:]
    if not os.path.exists(path):
        sys.stderr.write(f"✗ 配置文件不存在: {path}\n")
        return 1

    if not rest or rest[0] in ("-p", "--print"):
        lines = read_lines(path)
        _, _, current = scan(lines, {})
        for k, v in current.items():
            if v or "." not in k:
                print(f"{k}={v}")
        return 0

    want = {}
    for a in rest:
        if "=" not in a:
            sys.stderr.write(f"✗ 参数不是 KEY=VALUE: {a}\n{USAGE}")
            return 1
        k, v = a.split("=", 1)
        want[k.strip()] = v

    lines = read_lines(path)
    out, found, _ = scan(lines, want)

    missing = [k for k in want if k not in found]
    if missing:
        sys.stderr.write(
            "✗ config.yaml 里找不到这些键，未做任何修改：\n"
            + "".join(f"    {k}\n" for k in missing)
            + "  模板结构可能变了，请核对 config.yaml 与 install.sh 是否配套。\n")
        return 2

    if out != lines:
        write_lines(path, out)
        print("  -> 已更新 config.yaml: " + ", ".join(sorted(found)))
    else:
        print("  -> config.yaml 无需改动")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
