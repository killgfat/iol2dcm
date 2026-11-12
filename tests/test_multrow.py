# -*- coding: utf-8 -*-
"""同组多行的选行口径、补指纹闸门与「陈旧数据」拦截 —— 离线回归测试。

★ 不依赖 IOL2DCM_TEST_MDB：用合成行 + monkeypatch 取数层，任何机器上都能
   真跑。tests/test_regress.py 里依赖样本库的用例在没配 fixture 时会 skip，
   等于没跑；这一片是护栏。

跑法： .venv/bin/python tests/test_multrow.py
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import shutil
import types
import unittest
from contextlib import redirect_stdout

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pipeline as P                                  # noqa: E402
import sources.mdb as mdb                             # noqa: E402
from core import config, patient                      # noqa: E402


GOOD_DOB = "20120924"          # 有数据那行的生日（= PACS 里已建的档案）
PLACEHOLDER_DOB = "19000924"   # 设备占位生日（1.3.1 之前会算出另一个 PID）
EXAM = "20251101"
TARGET = {"host": "0.0.0.0", "port": 4242, "aet": "ORTHANC"}


_SEQ = [0]


def make_row(dob, ts, measured=True, al=24.14, r1=7.91, r2=7.69,
             state="", exam=EXAM, guid_date=None, guid=None):
    """合成一行 MDB 记录（字段与 load_measurements 的输出同构）。

    guid_date: guid 里的 8 位日期，默认取 exam（= 当天采集）。
               传别的日期表示「这批数据是那天采的，挂在 exam 这天的就诊下」。
    """
    if measured:
        e = {"al": al, "r1": r1, "r2": r2, "acd": 3.10, "axis": 10.0,
             "state": state}
    else:
        e = {"al": None, "r1": None, "r2": None, "acd": None, "axis": None,
             "state": state}
    gd = (guid_date or exam)[2:]                  # YYMMDD...
    # ★ 每行必须有唯一 guid —— 幂等键是 guid|exam_date，同 guid 同日期会被
    #   当成同一案例。以前这里 guid 只由日期派生，多行场景会全部撞成一个键。
    _SEQ[0] += 1
    g = guid or f"1.2.276.0.75.2.1.10.0.1.{gd}090000{_SEQ[0]:03d}.{_SEQ[0]}.2"
    return {
        "guid": g,
        "dob": dob, "exam_date": exam, "sex": "",
        "measure_ts": ts,
        "measure_date": guid_date or exam,
        "eyes": {"r": dict(e), "l": dict(e)},
    }


class Ctx:
    """把取数与推送全部换掉的最小运行环境。"""

    def __init__(self, rows, named=None):
        self.key3 = named or ("张", "某", EXAM)
        self.rows = rows
        # ★ v1.3.3 起幂等键是 guid|exam_date（一行一案例），不再是 姓|名|日期。
        #   测试里按 Ctx 的第一行算键；多行场景用 Ctx.keys() 拿全部。
        self.key = self.keys()[0]
        self.pushed = []
        self._tmp = tempfile.NamedTemporaryFile(suffix=".mdb", delete=False)
        self._tmp.close()
        self.state_path = self._tmp.name + ".json"
        self._saved = {}

    def keys(self):
        """本 Ctx 里每行对应的幂等键（guid|exam_date）。"""
        return [f"{r.get('guid')}|{r.get('exam_date')}" for r in self.rows]

    def __enter__(self):
        self._saved = {
            "assert_supported": mdb.assert_supported,
            "load_measurements": mdb.load_measurements,
            "probe": P._target_probe,
            "push": P.dicom.push_cstore,
        }
        mdb.assert_supported = lambda *a, **k: {"rows": len(self.rows)}
        mdb.load_measurements = lambda *a, **k: {self.key3: self.rows}
        P._target_probe = lambda *a, **k: (True, "fake")

        def _push(dss, *a, **k):
            self.pushed.append(len(dss))
            return [(str(ds.SOPInstanceUID), True, "0x0000", True)
                    for ds in dss]
        P.dicom.push_cstore = _push
        return self

    def __exit__(self, *exc):
        mdb.assert_supported = self._saved["assert_supported"]
        mdb.load_measurements = self._saved["load_measurements"]
        P._target_probe = self._saved["probe"]
        P.dicom.push_cstore = self._saved["push"]
        shutil.rmtree(self.state_path + ".spool", ignore_errors=True)
        for p in (self._tmp.name, self.state_path):
            try:
                os.remove(p)
            except OSError:
                pass
        return False

    def args(self, **kw):
        a = dict(mdb=self._tmp.name, since="", limit=0, dry_run=False,
                 max_seconds=0, workers=1, state=self.state_path,
                 config=None, interval=1,
                 dicom_host=None, dicom_port=None, aet=None)
        a.update(kw)
        return types.SimpleNamespace(**a)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg, cls.lenses, cls.meta = config.build(None)

    def cfg_nocool(self, **watcher):
        c = copy.deepcopy(self.cfg)
        c.setdefault("watcher", {})["cooling_seconds"] = 0
        c["watcher"].update(watcher)
        return c

    def run_once(self, ctx, state, cfg=None, **kw):
        with ctx:
            return P.run_once(cfg or self.cfg_nocool(), self.lenses,
                              self.meta, state, ctx.args(**kw), TARGET)

    def empty_state(self):
        return {"done": {}, "failed": {}, "cooling": {}, "stale": {}}


# ---------------------------------------------------------------- 迁移

class TestKeyMigration(Base):
    """旧键（姓|名|日期）→ 新键（guid|exam_date）。零重推是硬约束。"""

    def _old_key(self, fam, giv, date):
        return f"{fam}|{giv}|{date}"

    def test_单行组原样继承不重推(self):
        r = make_row(GOOD_DOB, 1_600_000_000.0)
        k3 = ("张", "蓝", EXAM)
        groups = {k3: [r]}
        state = self.empty_state()
        state["done"][self._old_key(*k3)] = {
            "t": "x", "pid": patient.make_pid(GOOD_DOB, "张蓝"),
            "date": EXAM, "instances": 6, "sc": 4,
            "oam_eyes": 2, "ker_eyes": 2, "fp": "deadbeefdeadbeef"}

        mig = P._migrate_keys(state, groups)

        nk = f"{r['guid']}|{EXAM}"
        self.assertEqual(mig["renamed"], 1)
        self.assertEqual(mig["split_groups"], 0)
        self.assertEqual(mig["new_cases"], 0)
        self.assertIn(nk, state["done"])
        self.assertNotIn(self._old_key(*k3), state["done"])
        d = state["done"][nk]
        self.assertEqual(d["uid_seed"], "",
                         "uid_seed 必须为空 —— 这样 study UID 与当年一致")
        self.assertEqual(d["fp"], "deadbeefdeadbeef", "指纹原样继承")
        self.assertEqual(d["instances"], 6)

    def test_多行组只继承有数据那行(self):
        blank = make_row(PLACEHOLDER_DOB, 1_700_000_000.0, measured=False)
        good = make_row(GOOD_DOB, 1_600_000_000.0, guid_date="20250301")
        k3 = ("张", "逸", EXAM)
        groups = {k3: [blank, good]}
        state = self.empty_state()
        state["done"][self._old_key(*k3)] = {
            "t": "x", "pid": patient.make_pid(GOOD_DOB, "张逸"),
            "date": EXAM, "instances": 6}

        mig = P._migrate_keys(state, groups)

        nk_good = f"{good['guid']}|{EXAM}"
        nk_blank = f"{blank['guid']}|{EXAM}"
        self.assertEqual(mig["split_groups"], 1)
        self.assertEqual(mig["new_cases"], 1)
        self.assertIn(nk_good, state["done"], "有数据那行继承 done")
        self.assertNotIn(nk_blank, state["done"], "空行是全新案例，不建记录")
        self.assertEqual(state["done"][nk_good]["uid_seed"], "")

    def test_迁移幂等(self):
        r = make_row(GOOD_DOB, 1_600_000_000.0)
        k3 = ("张", "蓝", EXAM)
        groups = {k3: [r]}
        state = self.empty_state()
        state["done"][self._old_key(*k3)] = {"t": "x", "instances": 6}

        P._migrate_keys(state, groups)
        snapshot = copy.deepcopy(state)
        mig2 = P._migrate_keys(state, groups)

        self.assertFalse(mig2["migrated"], "第二次不该再迁移")
        self.assertEqual(state, snapshot, "幂等：state 一个字节都不变")

    def test_孤儿保留不删(self):
        k3 = ("张", "蓝", EXAM)
        state = self.empty_state()
        state["done"][self._old_key(*k3)] = {"t": "x", "instances": 6}

        mig = P._migrate_keys(state, {})       # MDB 里找不到

        self.assertEqual(mig["orphan"], 1)
        self.assertIn(self._old_key(*k3), state["done"], "孤儿绝不删除")
        self.assertEqual(state["done"][self._old_key(*k3)]["uid_seed"], "",
                         "孤儿也要补 uid_seed，否则以后重推会换 UID")

    def test_两轮都有数据_第二轮是新案例(self):
        first = make_row(GOOD_DOB, 1_600_000_000.0, al=23.0)
        second = make_row(GOOD_DOB, 1_700_000_000.0, al=24.5)
        k3 = ("周", "三", EXAM)
        groups = {k3: [first, second]}
        state = self.empty_state()
        state["done"][self._old_key(*k3)] = {
            "t": "x", "pid": patient.make_pid(GOOD_DOB, "周三"),
            "date": EXAM, "instances": 6}

        P._migrate_keys(state, groups)

        nk1 = f"{first['guid']}|{EXAM}"
        nk2 = f"{second['guid']}|{EXAM}"
        inherited = [k for k in (nk1, nk2) if k in state["done"]]
        self.assertEqual(len(inherited), 1, "只继承一行，另一行是新案例")
        self.assertEqual(state["done"][inherited[0]]["uid_seed"], "")
        # 两轮的 study UID 必须不同，否则 PACS 静默去重
        pid = state["done"][inherited[0]]["pid"]
        u_old = P.dicom.study_uid_for(pid, EXAM, "")
        u_new = P.dicom.study_uid_for(pid, EXAM, second["guid"])
        self.assertNotEqual(u_old, u_new)


class TestStudyUidCompat(unittest.TestCase):
    """study_uid_for 的兼容锚点：默认参数必须与旧公式完全一致。"""

    def test_空case等于旧公式(self):
        from dicomlib import dicom
        self.assertEqual(dicom.study_uid_for("I123", "20251101"),
                         dicom.study_uid_for("I123", "20251101", ""))

    def test_带case换新study(self):
        from dicomlib import dicom
        a = dicom.study_uid_for("I123", "20251101", "")
        b = dicom.study_uid_for("I123", "20251101", "1.2.3")
        self.assertNotEqual(a, b)
        self.assertTrue(b.startswith("2.25."))


# ---------------------------------------------------------------- 选行口径

class TestPickRow(unittest.TestCase):
    """验证旧选行接口的来源判定。"""

    def test_当天有值优先于更早有值(self):
        """多次采集：取当天那次，不能拿几个月前的。"""
        old = make_row(GOOD_DOB, 1_600_000_000.0, al=23.5,
                       guid_date="20250201")
        today = make_row(GOOD_DOB, 1_700_000_000.0, al=24.1)
        row, pick = mdb.pick_row([old, today])
        self.assertIs(row, today)
        self.assertEqual(pick, mdb.PICK_OWN)

    def test_当天空行不能赢过更早有值行_但结论是other(self):
        """8 组里的 6 组形态：当天建了空记录，有数据的是几十天前那行。"""
        blank = make_row(PLACEHOLDER_DOB, 1_700_000_000.0, measured=False)
        old = make_row(GOOD_DOB, 1_600_000_000.0, guid_date="20250301")
        row, pick = mdb.pick_row([blank, old])
        self.assertIs(row, blank, "出单口径：先看是不是当天的行")
        self.assertEqual(pick, mdb.PICK_OTHER,
                         "当天只有空记录 → 本次就诊还没有数据，不能拿旧数据顶上")

    def test_已推比对口径取有数据那行(self):
        """PACS 里那份内容只能是「有数据那行」推出去的（空行产不出实例）。"""
        blank = make_row(PLACEHOLDER_DOB, 1_700_000_000.0, measured=False)
        old = make_row(GOOD_DOB, 1_600_000_000.0, guid_date="20250301")
        row, pick = mdb.pick_row([blank, old], already_pushed=True)
        self.assertIs(row, old)
        self.assertEqual(pick, mdb.PICK_PUSHED,
                         "已推路径的回答是「PACS 里那份来自这行」，"
                         "不是「这行能不能出单」")

    def test_已推路径永不返回OWN或STALE(self):
        """返回值语义必须自洽：已推路径不回答「能不能出单」。

        否则调用方就得靠额外的 isinstance 条件兜着，改一处就垮。
        """
        cases = [
            [make_row(GOOD_DOB, 1_600_000_000.0, guid_date="20240325")],
            [make_row(GOOD_DOB, 1_700_000_000.0, measured=False)],
            [make_row(GOOD_DOB, 1_600_000_000.0),
             make_row(GOOD_DOB, 1_700_000_000.0, measured=False)],
        ]
        for rows in cases:
            _, pick = mdb.pick_row(rows, already_pushed=True)
            self.assertIn(pick, (mdb.PICK_PUSHED, mdb.PICK_EMPTY),
                          f"已推路径返回了 {pick}")

    def test_已推路径整组全空返回empty(self):
        rows = [make_row(GOOD_DOB, 1_600_000_000.0, measured=False)]
        self.assertEqual(mdb.pick_row(rows, already_pushed=True)[1],
                         mdb.PICK_EMPTY)

    def test_当天有值_已推口径也取当天(self):
        old = make_row(GOOD_DOB, 1_600_000_000.0, al=23.5,
                       guid_date="20250201")
        today = make_row(GOOD_DOB, 1_700_000_000.0, al=24.1)
        row, pick = mdb.pick_row([old, today], already_pushed=True)
        self.assertIs(row, today)
        self.assertEqual(pick, mdb.PICK_PUSHED)

    def test_只有旧日期的数据_结论是stale(self):
        r = make_row(GOOD_DOB, 1_600_000_000.0, guid_date="20240325")
        row, pick = mdb.pick_row([r])
        self.assertIs(row, r)
        self.assertEqual(pick, mdb.PICK_STALE,
                         "单行但 guid 日期 != exam_date：不能直接出单")

    def test_当天空行_结论是other而不是empty(self):
        """当天建了记录但没测出值 → 「本次就诊尚无数据」，不是「整组没数据」。

        两者对下游是一样的（都不出单），但含义不同：other 说明当天确实
        有这个患者的检查（记录在），empty 说明连记录都没有。
        """
        r = make_row(GOOD_DOB, 1_600_000_000.0, measured=False)
        self.assertEqual(mdb.pick_row([r])[1], mdb.PICK_OTHER)

    def test_整组空且无当天行_结论是empty(self):
        r = make_row(GOOD_DOB, 1_600_000_000.0, measured=False,
                     guid_date="20240325")
        self.assertEqual(mdb.pick_row([r])[1], mdb.PICK_EMPTY)

    def test_当天多次采集取最新(self):
        a = make_row(GOOD_DOB, 1_600_000_000.0, al=23.0)
        b = make_row(GOOD_DOB, 1_700_000_000.0, al=24.0)
        row, pick = mdb.pick_row([a, b])
        self.assertIs(row, b)
        self.assertEqual(pick, mdb.PICK_OWN)

    def test_只有眼状态不算测量值(self):
        st = make_row(GOOD_DOB, 1_600_000_000.0, measured=False,
                      state="硅油充填眼")
        self.assertFalse(mdb.row_has_measurement(st),
                         "眼状态是录入项，单独有它不构成可录入的测量值")

    def test_guid解析不出时按当天处理(self):
        """无法判定必须放行 —— 否则 guid 格式一变，全库都不出单。"""
        r = make_row(GOOD_DOB, 1_600_000_000.0)
        r["guid"] = "not-a-guid"
        r["measure_date"] = ""
        self.assertTrue(mdb.row_is_on_exam_date(r))
        self.assertEqual(mdb.pick_row([r])[1], mdb.PICK_OWN)

    def test_pick_latest_行为不变(self):
        """兼容契约：外部脚本调用时选行行为保持不变。"""
        good = make_row(GOOD_DOB, 1_600_000_000.0)
        blank = make_row(PLACEHOLDER_DOB, 1_700_000_000.0, measured=False)
        self.assertIs(mdb.pick_latest([good, blank]), blank,
                      "pick_latest 必须保持「取 guid 时刻最晚」的原语义")

    def test_measure_date与measure_time同源(self):
        r = make_row(GOOD_DOB, 1_700_000_000.0, guid_date="20240325")
        self.assertEqual(mdb.measure_date(r["guid"]), "20240325")
        self.assertIsNotNone(mdb.measure_time(r["guid"]))


# ---------------------------------------------------------------- 自愈路径

class TestBackfillFingerprint(Base):
    """老记录 + 选到有数据行 → 补指纹，不推。"""

    def test_已推老记录选到空行也能补上指纹(self):
        ctx = Ctx([make_row(GOOD_DOB, 1_600_000_000.0),
                   make_row(PLACEHOLDER_DOB, 1_700_000_000.0,
                            measured=False)])
        state = self.empty_state()
        state["done"][ctx.key] = {
            "t": "x", "pid": patient.make_pid(GOOD_DOB, "张某"),
            "date": EXAM, "instances": 6, "sc": 4,
            "oam_eyes": 2, "ker_eyes": 2}

        stats = self.run_once(ctx, state)

        self.assertIn("fp", state["done"][ctx.key],
                      "选到有数据行之后必须补上指纹")
        self.assertNotIn(ctx.key, state["stale"])
        self.assertEqual(ctx.pushed, [], "补指纹不算重推，不能碰 PACS")
        self.assertEqual(stats["ok"] + stats["fail"], 0)

    def test_生日不同只留痕不拦(self):
        """使用者口径：「数据对不上就不要硬核对」「就当是两个人也行」。

        所以 pid 对不上时不进 stale、不排队，照常按当前内容补指纹。
        代价（已告知使用者）：若真是另一个人，PACS 里不会有他的档案 ——
        这是业务上接受的「两条独立记录」。
        """
        ctx = Ctx([make_row(GOOD_DOB, 1_600_000_000.0),
                   make_row(PLACEHOLDER_DOB, 1_700_000_000.0,
                            measured=False)])
        state = self.empty_state()
        state["done"][ctx.key] = {
            "t": "x", "pid": patient.make_pid(PLACEHOLDER_DOB, "张某"),
            "date": EXAM, "instances": 6}

        self.run_once(ctx, state)

        self.assertIn("fp", state["done"][ctx.key],
                      "生日对不上也要登记基线，否则这例会永远停在待处理")
        self.assertNotIn(ctx.key, state["stale"],
                         "不能变成需要人工长期跟进的队列")
        self.assertEqual(ctx.pushed, [], "补指纹不推 PACS")

    def test_内容真的变了自动新增Rec2(self):
        """补测使用新 SOP UID，原 study 内自动形成第二轮。"""
        ctx = Ctx([make_row(GOOD_DOB, 1_600_000_000.0, r1=8.30)])
        state = self.empty_state()
        state["done"][ctx.key] = {
            "t": "x", "pid": patient.make_pid(GOOD_DOB, "张某"),
            "date": EXAM, "instances": 6,
            "fp": P._fingerprint(P._record(
                make_row(GOOD_DOB, 1_600_000_000.0), "张某"))}

        self.run_once(ctx, state)

        self.assertNotIn(ctx.key, state["stale"])
        self.assertEqual(ctx.pushed, [6])
        self.assertEqual(state["done"][ctx.key]["revision"], 2)

    def test_同天两轮各自成案例(self):
        """一行 = 一个案例：同一天测两轮，两个案例各自出单、互不覆盖。"""
        first = make_row(PLACEHOLDER_DOB, 1_600_000_000.0, al=23.00)
        second = make_row(GOOD_DOB, 1_700_000_000.0, al=24.14)
        ctx = Ctx([first, second])
        state = self.empty_state()

        stats = self.run_once(ctx, state)

        self.assertEqual(stats["ok"], 2, "两行 = 两个案例，都要出单")
        k1, k2 = ctx.keys()
        self.assertIn(k1, state["done"])
        self.assertIn(k2, state["done"])
        # 各自用自己的生日 → 各自正确的 PID，不串
        self.assertEqual(state["done"][k1]["pid"],
                         patient.make_pid(PLACEHOLDER_DOB, "张某"))
        self.assertEqual(state["done"][k2]["pid"],
                         patient.make_pid(GOOD_DOB, "张某"))
        # 两轮的 study 种子不同 → UID 不同 → 不会互相去重
        self.assertNotEqual(state["done"][k1]["uid_seed"],
                            state["done"][k2]["uid_seed"])
        self.assertNotEqual(
            P.dicom.study_uid_for(state["done"][k1]["pid"], EXAM,
                                  state["done"][k1]["uid_seed"]),
            P.dicom.study_uid_for(state["done"][k2]["pid"], EXAM,
                                  state["done"][k2]["uid_seed"]),
            "两轮必须落在不同 study，否则 PACS 按同 SOP UID 静默去重")


# ---------------------------------------------------------------- 陈旧数据

class TestOffdateNotGated(Base):
    """v1.3.3：采集日与 exam_date 不同**不再拦单**。

    使用者口径：「跨天就是测了两次」「一行算一次有效结果」。
    一行就是一个案例，采集日不同是设备的业务行为，按「信息是否充足」处理。
    旧 max_stale_days 规则可能误拦独立案例。
    """

    def test_跨335天的数据照常出单(self):
        ctx = Ctx([make_row(GOOD_DOB, 1_600_000_000.0, guid_date="20241115")])
        state = self.empty_state()

        stats = self.run_once(ctx, state)

        self.assertEqual(stats["ok"], 1, "信息充足就该出单，与采集日无关")
        self.assertNotIn(ctx.key, state["stale"], "不该产生人工待办")
        self.assertGreater(len(ctx.pushed), 0)

    def test_先测后诊2天照常出单(self):
        ctx = Ctx([make_row(GOOD_DOB, 1_600_000_000.0, guid_date="20251030")])
        state = self.empty_state()

        stats = self.run_once(ctx, state)

        self.assertEqual(stats["ok"], 1)
        self.assertNotIn(ctx.key, state["stale"])

    def test_信息不足仍按原规则跳过(self):
        """删掉闸门不等于放水：没 AL 也没 r1+r2 的行照样不出单。"""
        ctx = Ctx([make_row(GOOD_DOB, 1_600_000_000.0, measured=False,
                            guid_date="20241115")])
        state = self.empty_state()

        stats = self.run_once(ctx, state)

        self.assertEqual(stats["ok"] + stats["fail"], 0)
        self.assertEqual(ctx.pushed, [])

    def test_max_stale_days已从代码里移除(self):
        self.assertFalse(hasattr(P, "DEFAULT_MAX_STALE_DAYS"),
                         "v1.3.3 起不再有 max_stale_days")
        self.assertFalse(hasattr(P, "_max_stale_days"))
        self.assertNotIn("max_stale_days",
                         (config.build(None)[0].get("watcher") or {}))

    def test_已推记录不受影响(self):
        ctx = Ctx([make_row(GOOD_DOB, 1_600_000_000.0, guid_date="20241115")])
        state = self.empty_state()
        state["done"][ctx.key] = {
            "t": "x", "pid": patient.make_pid(GOOD_DOB, "张某"),
            "date": EXAM, "instances": 6, "uid_seed": "",
            "fp": P._fingerprint(P._record(
                make_row(GOOD_DOB, 1_600_000_000.0), "张某"))}

        self.run_once(ctx, state)

        self.assertNotIn(ctx.key, state["stale"])
        self.assertEqual(ctx.pushed, [])


# ---------------------------------------------------------------- 无测量值分支

class TestEmptiedLegacy(Base):
    """无测量值 + 老记录缺 fp：与 failed 分支同一原则，必须留痕。"""

    def test_缺指纹时转stale而不是静默跳过(self):
        ctx = Ctx([make_row(PLACEHOLDER_DOB, 1_700_000_000.0,
                            measured=False)])
        state = self.empty_state()
        state["done"][ctx.key] = {
            "t": "x", "pid": patient.make_pid(GOOD_DOB, "张某"),
            "date": EXAM, "instances": 6}

        self.run_once(ctx, state)

        self.assertIn(ctx.key, state["stale"],
                      "done 存在 = 发过实例，版本未知就必须人看")
        self.assertEqual(state["stale"][ctx.key]["from"], "emptied")
        self.assertIsNone(state["stale"][ctx.key]["was_fp"])
        self.assertEqual(ctx.pushed, [])

    def test_有指纹时就报内容已变(self):
        ctx = Ctx([make_row(PLACEHOLDER_DOB, 1_700_000_000.0,
                            measured=False)])
        state = self.empty_state()
        state["done"][ctx.key] = {
            "t": "x", "pid": patient.make_pid(GOOD_DOB, "张某"),
            "date": EXAM, "instances": 6, "fp": "0123456789abcdef"}

        self.run_once(ctx, state)

        self.assertIn(ctx.key, state["stale"])
        self.assertEqual(state["stale"][ctx.key]["was_fp"], "0123456789abcdef")


# ---------------------------------------------------------------- audit

class TestAuditClassification(Base):
    """audit 必须区分旧记录指纹缺失与普通不一致。"""

    def _audit(self, groups, state):
        fd, p = tempfile.mkstemp(suffix=".mdb")
        os.close(fd)
        sf = tempfile.mktemp(suffix=".json")
        with open(sf, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        saved = (mdb.assert_supported, mdb.load_measurements)
        mdb.assert_supported = lambda *a, **k: {"rows": 1}
        mdb.load_measurements = lambda *a, **k: groups
        try:
            import io
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = P.cmd_audit(types.SimpleNamespace(
                    config=None, mdb=p, state=sf, since="", limit=0))
            return rc, buf.getvalue()
        finally:
            mdb.assert_supported, mdb.load_measurements = saved
            for x in (p, sf):
                try:
                    os.remove(x)
                except OSError:
                    pass

    def test_多行组逐行核对(self):
        """一行 = 一个案例：audit 也逐行核对，不再选一行代表整组。"""
        key3 = ("张", "某", EXAM)
        blank = make_row(PLACEHOLDER_DOB, 1_700_000_000.0, measured=False)
        good = make_row(GOOD_DOB, 1_600_000_000.0)
        groups = {key3: [blank, good]}
        # 只给有数据那行记 done（当年推的就是它），空行是新案例
        key_good = f"{good['guid']}|{EXAM}"
        state = {"done": {key_good: {"t": "x", "date": EXAM, "instances": 6,
                                     "pid": patient.make_pid(GOOD_DOB, "张某")}},
                 "failed": {}, "cooling": {}, "stale": {}}

        rc, out = self._audit(groups, state)

        self.assertIn("核对 1 条已推记录", out, "只核已推的那一行")
        self.assertIn("已推内容现在没有有效测量值（PACS 里仍是旧版）  0 条", out,
                      "空行没有 done，不该被报成 emptied")
        self.assertIn("无指纹（老记录，无法判断是否陈旧）  1 条", out)
        self.assertIn("同日同名多行", out, "多行明细仍要列出两行")
        self.assertEqual(rc, 0, "无指纹不计入结论，不应让 cron 报警")

    def test_PatientID漂移只是提示不计入结论(self):
        key3 = ("朱", "熹", "20261013")
        groups = {key3: [make_row(PLACEHOLDER_DOB, 1_700_000_000.0, exam="20261013"),
                         make_row(GOOD_DOB, 1_600_000_000.0, exam="20261013")]}
        key = f"{groups[key3][0]['guid']}|{key3[2]}"
        state = {"done": {key: {"t": "x", "date": "20261013", "instances": 6,
                                "pid": patient.make_pid(GOOD_DOB, "朱熹")}},
                 "failed": {}, "cooling": {}, "stale": {}}

        rc, out = self._audit(groups, state)

        self.assertNotIn("PatientID 与当前选中的行不一致", out,
                         "已按使用者口径从结论项移除")
        self.assertIn("同日同名多行", out, "但要在多行明细里能看见")
        self.assertEqual(rc, 0, "不是需要人处理的不一致")

    def test_采集日不同只是只读提示(self):
        key3 = ("梅", "诗", "20261020")
        groups = {key3: [make_row(GOOD_DOB, 1_600_000_000.0,
                                  guid_date="20250623", exam="20261020")]}
        state = {"done": {}, "failed": {}, "cooling": {}, "stale": {}}

        rc, out = self._audit(groups, state)

        self.assertIn("采集日期与检查日期不同", out, "要让人看见")
        self.assertIn("相差 484 天", out)
        self.assertNotIn("已暂停出单", out, "不再拦单")
        self.assertEqual(rc, 0, "只读提示不计入结论")


if __name__ == "__main__":
    unittest.main(verbosity=2)
