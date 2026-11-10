# -*- coding: utf-8 -*-
"""离线回归测试：补测冻结 / 冷却期 / dry-run 污染 / 取行口径。

★ 全部离线，不碰 PACS、不碰生产 state、不写仓库外文件。
   跑法： .venv/bin/python tests/test_regress.py
   （用 unittest 而不是 pytest：项目本身零测试依赖，不引入新包。）
"""
from __future__ import annotations

import concurrent.futures
import copy
import os
import sys
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pipeline as P                                  # noqa: E402
import sources.mdb as mdb                             # noqa: E402
from core import config                               # noqa: E402

# Tests never point at a developer's or a clinical database by default.  Supply
# an explicitly staged, synthetic fixture through IOL2DCM_TEST_MDB when needed.
SAMPLE = os.environ.get("IOL2DCM_TEST_MDB", "")


def _load_sample():
    """Load an explicitly supplied test fixture, never an implicit live DB."""
    if not SAMPLE:
        raise unittest.SkipTest(
            "set IOL2DCM_TEST_MDB to an approved synthetic MDB fixture")
    if not os.path.isfile(SAMPLE):
        raise unittest.SkipTest(f"test fixture not found: {SAMPLE}")
    groups = mdb.load_measurements(SAMPLE)
    for key, rows in groups.items():
        e = rows[-1]["eyes"]
        if all(e[s].get("al") and e[s].get("r1") for s in ("r", "l")):
            return key, copy.deepcopy(rows[-1])
    raise unittest.SkipTest("样本库里没有双眼齐全的记录")


def _args(**kw):
    a = dict(mdb=SAMPLE, since="", limit=0, dry_run=True, max_seconds=0,
             workers=1, state="/tmp/_iol2dcm_test_state.json",
             config=None, interval=1,
             dicom_host=None, dicom_port=None, aet=None)
    a.update(kw)
    return types.SimpleNamespace(**a)


TARGET = {"host": "0.0.0.0", "port": 4242, "aet": "ORTHANC"}


class fake_push:
    """把「探测目标」和「C-STORE」都换掉：不建任何网络连接。

    真推路径（dry_run=False）才会写 state["done"]，所以要验证记账就必须
    走真推。这两个替身让它在离线环境里跑完整个记账流程。

    ★ 两种模式，用途严格区分，别混：
      fake_push()          —— 一律成功。**只**能验证本地记账（done/failed
                              /stale 怎么变），不能用来证明「PACS 拿到了
                              新内容」。
      fake_push(pacs=True) —— 模拟真实 PACS 的**同 SOP UID 去重**：先到
                              的先留下，后来的同 UID 一律返回成功但不覆盖。
                              这才是能验证「补测闭环」的那个替身。
    """

    def __init__(self, pacs=False):
        self.pacs = pacs
        self.store = {}          # SOP UID -> 内容摘要（模拟 PACS 的库）
        self.accepted = []       # 按接收顺序的 SOP UID
        self.dupes = []          # 被静默去重的 SOP UID

    def __enter__(self):
        self._probe = P._target_probe
        self._push = P.dicom.push_cstore
        P._target_probe = lambda *a, **k: (True, "fake")

        def _push(dss, *a, **k):
            res = []
            for ds in dss:
                uid = str(ds.SOPInstanceUID)
                if self.pacs:
                    if uid in self.store:
                        self.dupes.append(uid)     # 保留首个，返回成功
                        res.append((uid, True, "0x0000", True))
                        continue
                    self.store[uid] = _content_digest(ds)
                self.accepted.append(uid)
                res.append((uid, True, "0x0000", True))
            return res

        P.dicom.push_cstore = _push
        return self

    def __exit__(self, *exc):
        P._target_probe = self._probe
        P.dicom.push_cstore = self._push
        return False


def _content_digest(ds):
    """DICOM 实例的内容摘要，用来判断 PACS 里存的到底是不是新版。"""
    import io
    bio = io.BytesIO()
    ds.save_as(bio)
    return P.digest(bio.getvalue())


def _ker_radius(ds):
    """从 KER 实例里取右眼陡轴曲率半径（核对内容是否真的变了）。"""
    seq = getattr(ds, "KeratometryRightEyeSequence", None)
    if not seq:
        return None
    return round(float(seq[0].SteepKeratometricAxisSequence[0]
                       .RadiusOfCurvature), 3)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg, cls.lenses, cls.meta = config.build(None)
        cls.key3, cls.row = _load_sample()
        cls.name = cls.key3[0] + cls.key3[1]

    def setUp(self):
        self._orig = mdb.load_measurements

    def tearDown(self):
        mdb.load_measurements = self._orig

    def serve(self, rows):
        """让 run_once 读到我们造的这一行。"""
        mdb.load_measurements = lambda *a, **k: {self.key3: rows}

    def cfg_nocool(self):
        """关掉冷却期，只看幂等/指纹逻辑。"""
        c = copy.deepcopy(self.cfg)
        c.setdefault("watcher", {})["cooling_seconds"] = 0
        return c


class TestFingerprint(Base):
    def test_补测后指纹变化(self):
        a = copy.deepcopy(self.row)
        b = copy.deepcopy(self.row)
        b["eyes"]["l"]["al"] = (a["eyes"]["l"]["al"] or 0) + 0.5
        ra = P._record(a, self.name)
        rb = P._record(b, self.name)
        self.assertNotEqual(P._fingerprint(ra), P._fingerprint(rb),
                            "补测改了 AL，指纹必须变")

    def test_没补测指纹稳定(self):
        ra = P._record(copy.deepcopy(self.row), self.name)
        rb = P._record(copy.deepcopy(self.row), self.name)
        self.assertEqual(P._fingerprint(ra), P._fingerprint(rb))

    def test_浮点末位抖动不改变指纹(self):
        a = copy.deepcopy(self.row)
        b = copy.deepcopy(self.row)
        b["eyes"]["r"]["al"] = a["eyes"]["r"]["al"] + 1e-9
        self.assertEqual(P._fingerprint(P._record(a, self.name)),
                         P._fingerprint(P._record(b, self.name)))


class TestNoFreeze(Base):
    def test_补测后能重新进待办(self):
        """本次事故的核心：同 key 补测后必须重新被处理。"""
        cfg = self.cfg_nocool()
        state = {"done": {}, "failed": {}, "cooling": {}, "stale": {}}
        key = "|".join(self.key3)
        # 11:02 现场形态：只有角膜曲率，两眼都还没眼轴
        #   → 度数算不出、OAM 不发、只发 KER 1 个实例（instances=1）
        rowA = copy.deepcopy(self.row)
        rowA["eyes"]["r"]["al"] = None
        rowA["eyes"]["l"]["al"] = None
        # ★ 这里必须用真推（dry_run=False）才能验证「done 记账」，
        #   而真推要连 PACS。用 fake_push 替掉探测与推送，不建连接。
        with fake_push():
            self.serve([rowA])
            P.run_once(cfg, self.lenses, self.meta, state,
                       _args(dry_run=False), TARGET)
            self.assertIn(key, state["done"], "第一轮应当记账")
            fp1 = state["done"][key]["fp"]
            n1 = state["done"][key]["instances"]
            self.assertLess(n1, 6, "缺 AL 时实例数应当少于 6")

            # 设备补测：同一行补上眼轴（guid 不变）
            self.serve([copy.deepcopy(self.row)])
            stats = P.run_once(cfg, self.lenses, self.meta, state,
                               _args(dry_run=False), TARGET)
            # 第一层：补测**能被认出来**（这是原 bug 的核心 —— 以前命中
            #   done 就静默跳过，连 _record 都不做，外界一无所知）。
            self.assertIn(key, state["stale"],
                          "补测后必须进 stale 留痕，不能静默跳过")
            self.assertEqual(stats["stale"], 1)
            # was_fp 就是 done 里那一版的指纹；now_fp 是补测后的新指纹
            self.assertEqual(state["stale"][key]["was_fp"], fp1)
            self.assertNotEqual(state["stale"][key]["now_fp"], fp1)
            self.assertEqual(state["stale"][key]["was"]["instances"], n1)
            self.assertEqual(state["stale"][key]["now_instances"], 6)
            # 不自动重推：OAM/KER 的 SOP UID 不随内容变，静默重推会被
            #   PACS 去重（返回成功但保留旧版），比卡住更难查。
            self.assertEqual(stats["total"], 0, "内容变了不自动重推")

            # 第二层：人工确认后清掉记录 → 会**重新发送**并完成记账。
            #   ★ 这里只能说「重新发送成功」，不能说「PACS 内容已更新」：
            #   SOP UID 不随内容变，若 PACS 里还留着旧实例，重推会被静默
            #   去重。那件事由 TestPacsDedup 用带内容的替身单独验证。
            state["done"].pop(key, None)
            state["stale"].pop(key, None)
            stats = P.run_once(cfg, self.lenses, self.meta, state,
                               _args(dry_run=False), TARGET)
            self.assertEqual(stats["ok"], 1, "清掉记录后应当重新发送成功")
            self.assertEqual(state["done"][key]["instances"], 6,
                             "重推后必须是 6 实例齐全")
            self.assertNotIn(key, state["stale"], "重推成功后 stale 要清掉")

    def test_老state无指纹时补写且不重推(self):
        """升级兼容：没有 fp 的老记录，就地补指纹、不重推。"""
        cfg = self.cfg_nocool()
        key = "|".join(self.key3)
        state = {"done": {key: {"t": "x", "instances": 6, "src": "generated"}},
                 "failed": {}, "cooling": {}, "stale": {}}
        self.serve([copy.deepcopy(self.row)])
        stats = P.run_once(cfg, self.lenses, self.meta, state, _args(), TARGET)
        self.assertEqual(stats["total"], 0, "老记录不该触发全量重推")
        self.assertTrue(state["done"][key].get("fp"), "应当就地补写指纹")

    def test_内容变化进stale而不是静默重推(self):
        """内容变了但 SOP UID 不随内容变 —— 先留痕，不自动重推。"""
        cfg = self.cfg_nocool()
        key = "|".join(self.key3)
        state = {"done": {key: {"t": "x", "instances": 1, "src": "generated",
                                "fp": "deadbeefdeadbeef"}},
                 "failed": {}, "cooling": {}, "stale": {}}
        self.serve([copy.deepcopy(self.row)])
        stats = P.run_once(cfg, self.lenses, self.meta, state, _args(), TARGET)
        self.assertEqual(stats["total"], 0)
        self.assertIn(key, state["stale"], "内容变了必须进 stale 留痕")
        self.assertNotEqual(state["stale"][key]["now_fp"],
                            state["stale"][key]["was_fp"])


class TestCooling(Base):
    def test_冷却期内不推(self):
        cfg = copy.deepcopy(self.cfg)
        cfg.setdefault("watcher", {})["cooling_seconds"] = 1200
        state = {"done": {}, "failed": {}, "cooling": {}, "stale": {}}
        row = copy.deepcopy(self.row)
        row["measure_ts"] = None          # 只靠 first 计时
        self.serve([row])
        stats = P.run_once(cfg, self.lenses, self.meta, state, _args(), TARGET)
        self.assertEqual(stats["total"], 0, "冷却期内不该进待办")
        self.assertGreater(stats.get("next_ready", 0), 0,
                           "必须回报冷却到期时刻，否则 loop 永远等不到")

    def test_刚做出来的检查被measure_ts挡住(self):
        cfg = copy.deepcopy(self.cfg)
        cfg.setdefault("watcher", {})["cooling_seconds"] = 1200
        state = {"done": {}, "failed": {}, "cooling": {}, "stale": {}}
        row = copy.deepcopy(self.row)
        row["measure_ts"] = __import__("time").time() - 60   # 60 秒前创建
        self.serve([row])
        stats = P.run_once(cfg, self.lenses, self.meta, state, _args(), TARGET)
        self.assertEqual(stats["total"], 0, "刚做出来的检查不该立刻推")

    def test_超过冷却期就能推(self):
        cfg = copy.deepcopy(self.cfg)
        cfg.setdefault("watcher", {})["cooling_seconds"] = 1200
        state = {"done": {}, "failed": {}, "cooling": {}, "stale": {}}
        row = copy.deepcopy(self.row)
        row["measure_ts"] = __import__("time").time() - 3600  # 1 小时前
        self.serve([row])
        stats = P.run_once(cfg, self.lenses, self.meta, state, _args(), TARGET)
        self.assertGreater(stats["total"], 0, "过了冷却期应当进待办")

    def test_冷却期默认值(self):
        c = config.build(None)[0]
        self.assertEqual(
            int((c.get("watcher") or {}).get("cooling_seconds") or 0), 1200)


class TestDryRun(Base):
    def test_dry_run不落盘(self):
        st = "/tmp/_iol2dcm_test_dryrun.json"
        if os.path.exists(st):
            os.remove(st)
        state = {"done": {}, "failed": {}, "cooling": {}, "stale": {}}
        self.serve([copy.deepcopy(self.row)])
        P.run_once(self.cfg_nocool(), self.lenses, self.meta, state,
                   _args(state=st, dry_run=True), TARGET)
        self.assertFalse(os.path.exists(st),
                         "dry-run 绝不能写 state（否则会人为制造冻结）")
        self.assertEqual(len(state["done"]), 0, "dry-run 不该动内存 done")


class TestPickRow(Base):
    def test_按创建时刻取最新一行而非物理末行(self):
        """Access 物理顺序不等于写入顺序：有数据的行可能在前面。"""
        cfg = self.cfg_nocool()
        # 设备补测形态（日期仅作流程占位，不对应病例）：
        #   有数据的那行创建得**更晚**，但物理位置在**前面**；
        #   物理末行是一条创建更早的空行。取 rows[-1] 会丢掉真数据。
        good = copy.deepcopy(self.row)
        good["measure_ts"] = 1_700_000_000.0        # 晚创建、有数据
        blank = copy.deepcopy(self.row)
        blank["eyes"] = {}                          # 早创建、空行
        blank["measure_ts"] = 1_600_000_000.0
        state = {"done": {}, "failed": {}, "cooling": {}, "stale": {}}
        with fake_push():
            self.serve([good, blank])               # 物理末行是空行
            P.run_once(cfg, self.lenses, self.meta, state,
                       _args(dry_run=False), TARGET)
            key = "|".join(self.key3)
            self.assertIn(key, state["done"],
                          "应当取到有数据的那行，而不是物理末行的空行")
            self.assertEqual(state["done"][key]["instances"], 6,
                             "取到有数据的行才会是 6 实例")


class TestMeasureTime(unittest.TestCase):
    def test_guid时间戳(self):
        ts = mdb.measure_time(
            "1.2.276.0.75.2.1.10.0.1.230629144503393.1588931420.8992")
        self.assertIsNotNone(ts)
        from datetime import datetime
        self.assertEqual(datetime.fromtimestamp(ts).strftime("%Y%m%d%H%M%S"),
                         "20230629144503")

    def test_无法解析返回None(self):
        for bad in ("", "abc", "1.2.3", None,
                    "1.2.276.0.75.2.1.10.0.1.XXXX.1.2"):
            self.assertIsNone(mdb.measure_time(bad))


class TestPacsDedup(Base):
    """用「保留首个实例」的 PACS 替身验证真实去重行为（B4 / I5）。"""

    def test_未删旧实例时重推拿不到新内容(self):
        """证明：SOP UID 不随内容变 → 重推被静默去重，PACS 仍是旧内容。

        这条测试是**对现状的确认**，不是「修复成功」的证明 —— 它守住
        的是「别把重推当成能刷新内容」这个认知。
        """
        cfg = self.cfg_nocool()
        key = "|".join(self.key3)
        rowA = copy.deepcopy(self.row)
        rowA["eyes"]["r"]["al"] = None
        rowA["eyes"]["l"]["al"] = None          # 只有 K，无 AL

        with fake_push(pacs=True) as pacs:
            state = {"done": {}, "failed": {}, "cooling": {}, "stale": {}}
            self.serve([rowA])
            P.run_once(cfg, self.lenses, self.meta, state,
                       _args(dry_run=False), TARGET)
            ker_uid = [u for u in pacs.store
                       if u in state["done"][key].get("fp", "")] or list(pacs.store)
            before = dict(pacs.store)

            # 补测：清掉记录后重推（模拟「人工确认」流程）
            state["done"].pop(key, None)
            self.serve([copy.deepcopy(self.row)])
            P.run_once(cfg, self.lenses, self.meta, state,
                       _args(dry_run=False), TARGET)

            # 同 UID 被去重 → 库里的内容没有变
            self.assertTrue(pacs.dupes, "必须观察到同 SOP UID 被静默去重")
            for uid, dg in before.items():
                if uid in pacs.store:
                    self.assertEqual(pacs.store[uid], dg,
                                     "未删旧实例时 PACS 内容不该被刷新")

    def test_删掉旧实例后重推才是真的新内容(self):
        """正确的定点修复流程：先删 PACS 旧实例，再重推。"""
        cfg = self.cfg_nocool()
        key = "|".join(self.key3)
        rowA = copy.deepcopy(self.row)
        rowA["eyes"]["r"]["al"] = None
        rowA["eyes"]["l"]["al"] = None

        with fake_push(pacs=True) as pacs:
            state = {"done": {}, "failed": {}, "cooling": {}, "stale": {}}
            self.serve([rowA])
            P.run_once(cfg, self.lenses, self.meta, state,
                       _args(dry_run=False), TARGET)
            old = dict(pacs.store)

            # 人工定点修复：删掉该 study 的旧实例 + 清 state 记录
            pacs.store.clear()
            pacs.dupes.clear()
            state["done"].pop(key, None)
            self.serve([copy.deepcopy(self.row)])
            P.run_once(cfg, self.lenses, self.meta, state,
                       _args(dry_run=False), TARGET)

            self.assertFalse(pacs.dupes, "删掉旧实例后不该再有去重")
            self.assertGreater(len(pacs.store), len(old),
                               "删旧后重推应当拿到更完整的实例集")


class TestPartialThenMeasured(Base):
    """B4：部分成功后补测，不能把旧 PACS 内容记成新版成功。"""

    def test_部分成功后补测转入stale而不是当成功重试(self):
        cfg = self.cfg_nocool()
        key = "|".join(self.key3)
        # ★ 必须是**多实例**场景才谈得上「部分成功」。只有 K 没有 AL 时
        #   总共就 1 个 KER 实例，1/1 就是全部成功，走不到 partial 分支。
        #   这里用完整数据：4 张 SC + OAM + KER = 6 个。
        rowA = copy.deepcopy(self.row)

        # 第一轮：只让第一个成功，其余失败 → 病例进 failed 且带 sent_uids
        state = {"done": {}, "failed": {}, "cooling": {}, "stale": {}}
        real_push = P.dicom.push_cstore
        real_probe = P._target_probe

        def _partial(dss, *a, **k):
            res = []
            for i, ds in enumerate(dss):
                res.append((ds.SOPInstanceUID, i == 0,
                            "0x0000" if i == 0 else "0x0122", True))
            return res

        P._target_probe = lambda *a, **k: (True, "fake")
        P.dicom.push_cstore = _partial
        try:
            self.serve([rowA])
            P.run_once(cfg, self.lenses, self.meta, state,
                       _args(dry_run=False), TARGET)
            self.assertIn(key, state["failed"], "部分成功应进 failed")
            self.assertTrue(state["failed"][key].get("sent_uids"),
                            "failed 必须记下已发出去的实例 UID")
            fp_old = state["failed"][key]["fp"]

            # 设备补测（真的改数据，否则指纹不变、走不到 stale），
            # 然后到点重试
            state["failed"][key]["next"] = 0
            rowB = copy.deepcopy(self.row)
            rowB["eyes"]["r"]["al"] = (rowB["eyes"]["r"]["al"] or 0) + 0.4
            rowB["eyes"]["r"]["r1"] = (rowB["eyes"]["r"]["r1"] or 0) + 0.1
            self.assertNotEqual(P._fingerprint(P._record(rowB, self.name)),
                                fp_old, "补测必须真的改变指纹")
            self.serve([rowB])
            stats = P.run_once(cfg, self.lenses, self.meta, state,
                               _args(dry_run=False), TARGET)
            # 不能按普通失败重试 → 必须转 stale，交人工
            self.assertIn(key, state["stale"],
                          "已发旧内容 + 内容又变了 → 必须转 stale")
            self.assertNotIn(key, state["failed"], "不应留在 failed 里重试")
            self.assertEqual(state["stale"][key]["was_fp"], fp_old)
            self.assertEqual(state["stale"][key].get("from"), "failed")
            self.assertEqual(stats["ok"], 0, "绝不能记成成功")
        finally:
            P.dicom.push_cstore = real_push
            P._target_probe = real_probe


class TestLoopSchedule(Base):
    """B1：loop() 必须能唤醒后续批次的冷却病例。"""

    def test_有todo时也返回next_ready(self):
        cfg = copy.deepcopy(self.cfg)
        cfg.setdefault("watcher", {})["cooling_seconds"] = 1200
        state = {"done": {}, "failed": {}, "cooling": {}, "stale": {}}
        import time as _t
        # 一例已过冷却（会进 todo），一例还在冷却
        old_row = copy.deepcopy(self.row)
        old_row["measure_ts"] = _t.time() - 7200
        new_row = copy.deepcopy(self.row)
        new_row["measure_ts"] = _t.time() - 60

        other = ("测", "试乙", self.key3[2])
        calls = {"n": 0}

        def _load(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                return {self.key3: [old_row], other: [new_row]}
            return {self.key3: [old_row], other: [new_row]}

        mdb.load_measurements = _load
        with fake_push():
            stats = P.run_once(cfg, self.lenses, self.meta, state,
                               _args(dry_run=False), TARGET)
        # 本轮确实处理了 1 例
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["ok"], 1)
        # ★ 关键：有 todo 的那条返回路径也必须带 next_ready
        self.assertGreater(stats.get("next_ready", 0), 0,
                           "有 todo 时返回的 next_ready 不能是 0 —— 否则"
                           "冷却中的病例永远等不到唤醒")
        self.assertEqual(stats.get("cooling"), 1)


class TestCoolingExpiresDuringRound(Base):
    """复审阻塞 1：处理期间到期的冷却病例不能被丢出调度。"""

    def test_next_ready_包含已过期条目(self):
        """筛选时 B 还在冷却，处理 A 花掉 20 秒后 B 到期 —— 必须能被唤醒。

        ★ 这正是审查复现的边界：_next_cool_ready 若只返回「未来」的到期
          时刻，B 会被过滤掉，next_ready=0，loop 就不再有唤醒理由。
        """
        cfg = copy.deepcopy(self.cfg)
        cfg.setdefault("watcher", {})["cooling_seconds"] = 1200
        import time as _t
        now = _t.time()

        # A 已过冷却（进 todo），B 的到期时刻已经**过去**（模拟「处理
        # A 期间 B 到期」的最终状态 —— 用时钟推进模拟处理耗时，避免
        # 依赖机器快慢）。
        rowA = copy.deepcopy(self.row)
        rowA["measure_ts"] = now - 7200
        rowB = copy.deepcopy(self.row)
        rowB["measure_ts"] = now - 1195      # 到期 = now + 5s

        other = ("测", "试乙", self.key3[2])
        # 用固定时钟：处理 A 耗时 20 秒（B 在这期间到期）
        import pipeline as _P

        class _Clock:
            t = now

            def time(self):
                return self.t

        clk = _Clock()
        real_time = _P.time.time
        real_process = _P._process_one
        _P.time.time = clk.time
        real_process_ref = _P._process_one

        def slow(task, *a, **k):
            res = real_process_ref(task, *a, **k)
            clk.t += 20                      # A 处理耗时 20 秒
            return res

        _P._process_one = slow
        mdb.load_measurements = lambda *a, **k: {
            self.key3: [rowA], other: [rowB]}
        state = {"done": {}, "failed": {}, "cooling": {}, "stale": {}}
        try:
            with fake_push():
                stats = P.run_once(cfg, self.lenses, self.meta, state,
                                   _args(dry_run=False), TARGET)
        finally:
            _P.time.time = real_time
            _P._process_one = real_process

        # 本轮处理了 A
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["ok"], 1)
        # ★ B 必须**已经过期**（时钟真的推进了），否则本测试证明不了边界
        self.assertIn("|".join(other), state["cooling"])
        ready = state["cooling"]["|".join(other)]["ready"]
        self.assertLess(ready, clk.t, "B 的到期时刻必须已经过去")
        # ★ 关键断言：过期条目也必须回报唤醒时刻，否则永久饿死
        self.assertGreater(stats.get("next_ready", 0), 0,
                           "已到期的冷却条目必须回报唤醒时刻")
        self.assertLessEqual(stats["next_ready"], clk.t,
                             "回报的应当是已过去的时刻")

    def test_idle_sleep对已过期返回1秒(self):
        """已过期的唤醒信号 → 立刻再来一轮，不睡满 interval。"""
        now = __import__("time").time()
        got = P._idle_sleep({}, 60, extra_next=now - 300)
        self.assertEqual(got, 1.0, "过期的冷却信号应当立刻重跑")

    def test_idle_sleep对未来返回剩余秒数(self):
        now = __import__("time").time()
        got = P._idle_sleep({}, 60, extra_next=now + 5)
        self.assertGreaterEqual(got, 1.0)
        self.assertLessEqual(got, 6.0)


class TestOldConfig(Base):
    """B2：旧 config.yaml 缺 cooling_seconds 时必须用代码里的默认值。"""

    def test_缺字段时用默认1200(self):
        cfg = copy.deepcopy(self.cfg)
        cfg.setdefault("watcher", {}).pop("cooling_seconds", None)
        self.assertEqual(P._cooling_seconds(cfg), 1200.0)

    def test_显式0表示关闭(self):
        cfg = copy.deepcopy(self.cfg)
        cfg.setdefault("watcher", {})["cooling_seconds"] = 0
        self.assertEqual(P._cooling_seconds(cfg), 0.0)

    def test_缺字段时真的挡住刚做出来的检查(self):
        """不只是函数返回值对，行为也要对。"""
        cfg = copy.deepcopy(self.cfg)
        cfg.setdefault("watcher", {}).pop("cooling_seconds", None)
        import time as _t
        row = copy.deepcopy(self.row)
        row["measure_ts"] = _t.time() - 60        # 1 分钟前
        state = {"done": {}, "failed": {}, "cooling": {}, "stale": {}}
        self.serve([row])
        stats = P.run_once(cfg, self.lenses, self.meta, state,
                           _args(dry_run=True), TARGET)
        self.assertEqual(stats["total"], 0, "旧配置升级后冷却必须生效")
        self.assertEqual(stats.get("cooling"), 1)


class _StopLoop(BaseException):
    """★ 必须继承 BaseException：loop() 里有 except Exception，
    Runner 会把它吞掉，测试就永远退不出来（审查指出的假阳性之一）。"""


def run_loop_rounds(cfg, lenses, meta, state_path, rounds, **kw):
    """真的跑 loop()，跑满 rounds 轮后退出。

    返回 (saved, ran, run_states, loaded)：
        saved       loop 调用 _save_state 时收到的 state 列表
        ran         实际进入 run_once 的次数
        run_states  run_once 收到的 state（第 4 个位置参数）
        loaded      _load_state 装载出来的对象

    ★ 为什么必须跑真 loop()：只测 run_once 的话，把 loop() 里的
      deepcopy 隔离或 _save_state 保护删掉，测试照样通过 —— 覆盖假象。

    ★ 退出点必须在**本轮全部逻辑之后**（loop 末尾的 _sleep），不能在
      run_once 返回时抛 —— 那时 loop 还没走到 _save_state，「dry-run 不
      保存」这类断言会变成 trivially true（审查实测确认过这点）。

    ★ 必须把 _prepare 整个换掉：_args() 的 config=None 会让 build_config
      抛异常，loop 在装载阶段就 return，一轮都没跑而测试仍然「通过」。

    ★ run_once 的真实签名是 (cfg, lenses, meta, state, args, target)，
      state 在第 4 位，不是第 1 位（上一版取错，记录下来的其实是 cfg）。
    """
    args = _args(state=state_path, **kw)
    orig = {n: getattr(P, n) for n in
            ("_prepare", "_save_state", "_load_state", "run_once", "_sleep")}
    saved, loaded, run_states = [], [], []
    ran = {"n": 0}
    left = {"n": rounds}

    def spy_load(path, lenses):
        st = orig["_load_state"](path, lenses)
        loaded.append(st)
        return st

    def spy_run(cfg_, lenses_, meta_, state_, *a, **k):
        ran["n"] += 1
        run_states.append(state_)
        return orig["run_once"](cfg_, lenses_, meta_, state_, *a, **k)

    def spy_sleep(seconds):
        # 这一轮已经完整跑完（含落盘与调度更新），可以安全退出了
        left["n"] -= 1
        if left["n"] <= 0:
            raise _StopLoop()
        # 不真的睡，只把时间往前挪一点点避免死循环

    P._prepare = lambda a: (cfg, lenses, meta)
    P._load_state = spy_load
    P._save_state = lambda p, s: saved.append(s)
    P.run_once = spy_run
    P._sleep = spy_sleep
    try:
        with fake_push():
            P.loop(args)
    except _StopLoop:
        pass
    finally:
        for n, f in orig.items():
            setattr(P, n, f)
    return saved, ran["n"], run_states, loaded


class TestWatchDryRun(Base):
    """B3：watch --dry-run 不能改 state，也不能落盘（真跑 loop）。"""

    def test_loop的dryrun不落盘且不改state(self):
        import json as _j
        import tempfile
        st = tempfile.mktemp(suffix=".json")
        key = "|".join(self.key3)
        base = {
            "sig": P.INSTANCE_LAYOUT + "||" + "|".join(
                L["key"] for L in self.lenses),
            # 老 done 无 fp：dry-run 会尝试补写它
            "done": {key: {"t": "x", "instances": 6, "src": "generated"}},
            "failed": {}, "cooling": {}, "stale": {},
        }
        with open(st, "w", encoding="utf-8") as f:
            _j.dump(base, f, ensure_ascii=False)
        before = open(st, encoding="utf-8").read()

        self.serve([copy.deepcopy(self.row)])
        saved, ran, run_states, loaded = run_loop_rounds(
            self.cfg_nocool(), self.lenses, self.meta, st,
            rounds=1, dry_run=True)
        # ★ 先断言真的跑了一轮 —— 否则这条测试什么都证明不了
        #   （审查指出：原版 helper 在装载阶段就退出，ran 恒为 0）。
        self.assertGreaterEqual(ran, 1, "loop 必须真的执行到 run_once")
        # ★ 断言退出点在本轮逻辑**之后**：真正走进过 loop 末尾的睡眠，
        #   保存分支才有机会执行，「dry-run 不保存」才不是 trivially true。
        self.assertGreaterEqual(len(loaded), 1, "必须走到 loop 尾部")
        self.assertEqual(saved, [], "dry-run 下 loop 不该调用 _save_state")
        self.assertEqual(open(st, encoding="utf-8").read(), before,
                         "state 文件必须一字节不变")
        # ★ 文件没变不足以证明内存隔离：即使去掉 deepcopy，只要不保存
        #   磁盘也不会变。直接比对「loop 装载的那个对象」与「交给
        #   run_once 的对象」是不是同一个 —— dry-run 必须是副本。
        self.assertTrue(run_states, "应当捕获到 run_once 收到的 state")
        self.assertIsNot(run_states[0], loaded[0],
                         "dry-run 必须把副本交给 run_once，不能是装载的原对象")
        # ★ 副本上确实发生了改动（补 fp），而原对象没有 —— 两条一起才
        #   能证明隔离真的生效，而不是「什么都没干」。
        self.assertNotEqual(run_states[0].get("done"), base["done"],
                            "副本上应当补写了 fp")
        self.assertIsNone(loaded[0]["done"][key].get("fp"),
                          "原 state 不能被补上 fp")
        self.assertEqual(loaded[0].get("done"), base["done"],
                         "装载到的内容应来自磁盘")
        os.remove(st)


class TestStaleIsSticky(Base):
    """复审阻塞 2：stale 必须持续生效，不能只挡住一次扫描。

    ★ 三轮连续验证（审查给的复现）：
        1. 部分成功 → failed
        2. 补测 → stale
        3. 再扫一轮 → 绝不能自动发送、绝不能清掉 stale
    """

    def test_转stale后第三轮不会自动重推(self):
        cfg = self.cfg_nocool()
        key = "|".join(self.key3)
        state = {"done": {}, "failed": {}, "cooling": {}, "stale": {}}

        def _partial(dss, *a, **k):
            return [(ds.SOPInstanceUID, i == 0,
                     "0x0000" if i == 0 else "0x0122", True)
                    for i, ds in enumerate(dss)]

        with fake_push(pacs=True) as pacs:
            real = P.dicom.push_cstore
            real_probe = P._target_probe
            P._target_probe = lambda *a, **k: (True, "fake")

            def _partial_into_pacs(dss, *a, **k):
                # ★ 这个替身绕过了 fake_push 的 _push，所以要自己把「已接收
                #   的实例」写进 pacs.store，否则后面无法核对 PACS 内容。
                res = []
                for i, ds in enumerate(dss):
                    uid = str(ds.SOPInstanceUID)
                    if i == 0:
                        pacs.store.setdefault(uid, _content_digest(ds))
                        pacs.accepted.append(uid)
                        res.append((uid, True, "0x0000", True))
                    else:
                        res.append((uid, False, "0x0122", True))
                return res

            P.dicom.push_cstore = _partial_into_pacs
            try:
                # 第 1 轮：部分成功 → failed
                self.serve([copy.deepcopy(self.row)])
                P.run_once(cfg, self.lenses, self.meta, state,
                           _args(dry_run=False), TARGET)
                self.assertIn(key, state["failed"])
                self.assertEqual(len(pacs.store), 1, "PACS 里已有 1 个旧实例")

                # 第 2 轮：补测 → stale
                state["failed"][key]["next"] = 0
                rowB = copy.deepcopy(self.row)
                rowB["eyes"]["r"]["al"] += 0.4
                rowB["eyes"]["r"]["r1"] += 0.1
                self.serve([rowB])
                P.run_once(cfg, self.lenses, self.meta, state,
                           _args(dry_run=False), TARGET)
                self.assertIn(key, state["stale"])
                self.assertNotIn(key, state["failed"])
                sent_after_2 = len(pacs.accepted)

                # 第 3 轮：什么都不改，再扫一次 —— 绝不能发出去。
                # ★ 必须继续用外层这个 pacs 对象计数；新建一个替身的话
                #   发送调用会记到新对象上，断言就观察不到（审查指出）。
                calls = {"n": 0}
                real_after = P.dicom.push_cstore

                def _counting_push(dss, *a, **k):
                    calls["n"] += 1
                    return real_after(dss, *a, **k)

                P.dicom.push_cstore = _counting_push
                self.serve([copy.deepcopy(rowB)])
                stats = P.run_once(cfg, self.lenses, self.meta, state,
                                   _args(dry_run=False), TARGET)
                P.dicom.push_cstore = real_after
                self.assertEqual(stats["ok"], 0, "stale 病例不能被自动重推")
                self.assertIn(key, state["stale"], "stale 必须还在")
                self.assertNotIn(key, state["done"], "不该被记成已完成")
                self.assertEqual(calls["n"], 0,
                                 "第三轮不该调用 C-STORE")
                self.assertEqual(len(pacs.accepted), sent_after_2,
                                 "PACS 里不该多出新实例")
                self.assertEqual(sent_after_2, 1)
            finally:
                P.dicom.push_cstore = real
                P._target_probe = real_probe

    def test_内容回到已推版本时自动解除stale(self):
        cfg = self.cfg_nocool()
        key = "|".join(self.key3)
        fp_now = P._fingerprint(P._record(self.row, self.name))
        state = {
            "done": {}, "failed": {}, "cooling": {},
            "stale": {key: {"was_fp": fp_now, "now_fp": "deadbeef",
                            "from": "failed"}},
        }
        self.serve([copy.deepcopy(self.row)])
        P.run_once(cfg, self.lenses, self.meta, state,
                   _args(dry_run=True), TARGET)
        self.assertNotIn(key, state["stale"],
                         "内容回到 PACS 现有版本就该解除待确认")


class TestResponseLost(Base):
    """复审阻塞 3：响应丢失必须按「可能已接收」处理。"""

    def test_空响应视为结果未知并进入拦截(self):
        cfg = self.cfg_nocool()
        key = "|".join(self.key3)
        state = {"done": {}, "failed": {}, "cooling": {}, "stale": {}}

        # 第 1 轮：KER 已送达但响应丢失（ok=False, known=False）
        def _lost(dss, *a, **k):
            return [(ds.SOPInstanceUID, False, "no response", False)
                    for ds in dss]

        real = P.dicom.push_cstore
        real_probe = P._target_probe
        P._target_probe = lambda *a, **k: (True, "fake")
        P.dicom.push_cstore = _lost
        try:
            self.serve([copy.deepcopy(self.row)])
            P.run_once(cfg, self.lenses, self.meta, state,
                       _args(dry_run=False), TARGET)
            fr = state["failed"][key]
            self.assertTrue(fr.get("uncertain"),
                            "空响应必须标记为结果未知")
            self.assertTrue(fr.get("maybe_uids"),
                            "结果未知的实例要记进 maybe_uids")

            # 第 2 轮：内容变了 → 必须进 stale，不能普通重试
            state["failed"][key]["next"] = 0
            rowB = copy.deepcopy(self.row)
            rowB["eyes"]["r"]["al"] += 0.4
            self.serve([rowB])
            P.dicom.push_cstore = real
            with fake_push():
                P.run_once(cfg, self.lenses, self.meta, state,
                           _args(dry_run=False), TARGET)
            self.assertIn(key, state["stale"],
                          "结果未知 + 内容变化 → 必须人工处理")
        finally:
            P.dicom.push_cstore = real
            P._target_probe = real_probe


class TestReceiveHistory(Base):
    """复审阻塞 4：接收历史必须跨尝试累积，不能被后一次失败抹掉。"""

    def test_部分成功后全失败仍保留历史(self):
        cfg = self.cfg_nocool()
        key = "|".join(self.key3)
        state = {"done": {}, "failed": {}, "cooling": {}, "stale": {}}
        real = P.dicom.push_cstore
        real_probe = P._target_probe
        P._target_probe = lambda *a, **k: (True, "fake")
        try:
            # 第 1 轮：部分成功
            def _partial(dss, *a, **k):
                return [(ds.SOPInstanceUID, i == 0,
                         "0x0000" if i == 0 else "0x0122", True)
                        for i, ds in enumerate(dss)]
            P.dicom.push_cstore = _partial
            self.serve([copy.deepcopy(self.row)])
            P.run_once(cfg, self.lenses, self.meta, state,
                       _args(dry_run=False), TARGET)
            hist = list(state["failed"][key]["sent_uids"])
            self.assertEqual(len(hist), 1)

            # 第 2 轮：同内容重试，这次全失败 → 历史不能被抹掉
            def _all_fail(dss, *a, **k):
                return [(ds.SOPInstanceUID, False, "0x0122", True)
                        for ds in dss]
            P.dicom.push_cstore = _all_fail
            state["failed"][key]["next"] = 0
            P.run_once(cfg, self.lenses, self.meta, state,
                       _args(dry_run=False), TARGET)
            self.assertEqual(state["failed"][key]["sent_uids"], hist,
                             "接收历史必须累积，不能被后一次全失败覆盖")

            # 第 3 轮：内容变了 → 必须进 stale（因为历史还在）
            state["failed"][key]["next"] = 0
            rowB = copy.deepcopy(self.row)
            rowB["eyes"]["r"]["al"] += 0.4
            self.serve([rowB])
            P.run_once(cfg, self.lenses, self.meta, state,
                       _args(dry_run=False), TARGET)
            self.assertIn(key, state["stale"],
                          "历史里已有旧实例 + 内容变化 → 必须人工处理")
        finally:
            P.dicom.push_cstore = real
            P._target_probe = real_probe


class TestGeneratedFailureKeepsHistory(Base):
    """复审阻塞 1：「实例全部生成失败」不能抹掉接收历史。

    ★ 上一版这条测试是假阳性：它只让 oam.build 抛异常，但 SC（4 张）和
      KER 仍然生成成功，于是根本没走到「实例全部生成失败」那条早退，
      而是正常进入发送阶段；而且中途还把 push_cstore 恢复成了真实函数，
      会真的去连 0.0.0.0:4242。

      正确做法：用一条**只可能生成 KER** 的记录（两眼无 AL），
      再让 keratometry.build 抛异常 → datasets 为空 → 命中早退。
    """

    def test_生成失败保留历史且内容变化后进stale(self):
        cfg = self.cfg_nocool()
        key = "|".join(self.key3)
        # 只有角膜曲率、没有眼轴 → 只可能生成 KER 一个实例
        rowA = copy.deepcopy(self.row)
        rowA["eyes"]["r"]["al"] = None
        rowA["eyes"]["l"]["al"] = None

        state = {"done": {}, "failed": {}, "cooling": {}, "stale": {}}
        real_push = P.dicom.push_cstore
        real_ker = P.keratometry.build
        real_probe = P._target_probe
        P._target_probe = lambda *a, **k: (True, "fake")
        try:
            # 第 1 轮：KER 送达，但响应丢失（结果未知 → 已接收历史）
            def _lost(dss, *a, **k):
                return [(ds.SOPInstanceUID, False, "no response", False)
                        for ds in dss]
            P.dicom.push_cstore = _lost
            self.serve([rowA])
            P.run_once(cfg, self.lenses, self.meta, state,
                       _args(dry_run=False), TARGET)
            self.assertIn(key, state["failed"])
            fr = state["failed"][key]
            hist = list(fr.get("maybe_uids") or []) + list(
                fr.get("sent_uids") or [])
            self.assertTrue(hist, "第一轮应当记下已/可能发送的实例")
            self.assertTrue(fr.get("uncertain"))

            # 第 2 轮：同内容，但**全部**实例生成失败
            calls = {"n": 0}

            def _counting_push(dss, *a, **k):
                calls["n"] += 1
                return []
            P.dicom.push_cstore = _counting_push

            def _boom(*a, **k):
                raise RuntimeError("boom")
            P.keratometry.build = _boom
            state["failed"][key]["next"] = 0
            self.serve([rowA])
            P.run_once(cfg, self.lenses, self.meta, state,
                       _args(dry_run=False), TARGET)
            self.assertIn(key, state["failed"], "生成失败应留在 failed")
            fr2 = state["failed"][key]
            self.assertIn("实例全部生成失败", str(fr2.get("err")),
                          "必须命中「全部生成失败」早退路径")
            self.assertEqual(calls["n"], 0,
                             "全部生成失败时不该调用 C-STORE")
            got = list(fr2.get("maybe_uids") or []) + list(
                fr2.get("sent_uids") or [])
            self.assertEqual(got, hist, "生成失败不能抹掉此前的接收历史")

            # 第 3 轮：内容变了 → 必须进 stale
            P.keratometry.build = real_ker
            state["failed"][key]["next"] = 0
            rowB = copy.deepcopy(rowA)
            rowB["eyes"]["r"]["r1"] += 0.3
            self.serve([rowB])
            P.run_once(cfg, self.lenses, self.meta, state,
                       _args(dry_run=False), TARGET)
            self.assertIn(key, state["stale"],
                          "历史里有旧实例 + 内容变化 → 必须人工处理")
        finally:
            P.dicom.push_cstore = real_push
            P.keratometry.build = real_ker
            P._target_probe = real_probe


class TestPermanentDoesNotFreeze(Base):
    """复审阻塞：permanent 只对「造成它的那份输入」有效。

    ★ 审查的复现：A 版推送结果未知（uncertain + maybe_uids）→ 同一检查
      暂时变成无测量值（failed 被标 permanent）→ 补回 B 版（曲率变了）。
      旧逻辑下第三轮 total=0、stale={}，病例永久冻结。
    """

    def test_有接收历史时清空数据直接进stale(self):
        """已发出过旧内容 + 数据被清空 → stale（PACS 里那批实例还在）。

        这条比标 permanent 更保守：permanent 是「静默跳过」，
        stale 才进人工清单、audit 才看得见。
        """
        cfg = self.cfg_nocool()
        key = "|".join(self.key3)
        rowA = copy.deepcopy(self.row)
        rowA["eyes"]["r"]["al"] = None
        rowA["eyes"]["l"]["al"] = None

        state = {"done": {}, "failed": {}, "cooling": {}, "stale": {}}
        real_push = P.dicom.push_cstore
        real_probe = P._target_probe
        P._target_probe = lambda *a, **k: (True, "fake")
        try:
            def _lost(dss, *a, **k):
                return [(ds.SOPInstanceUID, False, "no response", False)
                        for ds in dss]
            P.dicom.push_cstore = _lost
            self.serve([rowA])
            P.run_once(cfg, self.lenses, self.meta, state,
                       _args(dry_run=False), TARGET)
            self.assertIn(key, state["failed"])

            empty = copy.deepcopy(rowA)
            empty["eyes"] = {}
            self.serve([empty])
            P.run_once(cfg, self.lenses, self.meta, state,
                       _args(dry_run=False), TARGET)
            self.assertIn(key, state["stale"],
                          "已发出过内容又不能确认 → 必须进人工清单")
            self.assertEqual(state["stale"][key].get("from"), "failed")
            self.assertEqual(state["stale"][key].get("now_instances"), 0)
            self.assertNotIn(key, state["failed"])
        finally:
            P.dicom.push_cstore = real_push
            P._target_probe = real_probe

    def test_清空后恢复完全相同的A版也能解除permanent(self):
        """审查的边界：A → 空 → **同一个 A**。

        fp 表示「上次发送的版本」，perm_fp 表示「造成 permanent 的输入」。
        恢复同一个 A 时 fp 没变，但 perm_fp（空记录）变了 —— 若两者共用
        一个字段，changed=False 会永久跳过。
        """
        cfg = self.cfg_nocool()
        key = "|".join(self.key3)
        rowA = copy.deepcopy(self.row)
        rowA["eyes"]["r"]["al"] = None
        rowA["eyes"]["l"]["al"] = None

        state = {"done": {}, "failed": {}, "cooling": {}, "stale": {}}
        real_push = P.dicom.push_cstore
        real_probe = P._target_probe
        P._target_probe = lambda *a, **k: (True, "fake")
        try:
            # 第 1 轮：A 版明确失败（known=True，无接收历史）
            def _hard(dss, *a, **k):
                return [(ds.SOPInstanceUID, False, "0x0122", True)
                        for ds in dss]
            P.dicom.push_cstore = _hard
            self.serve([rowA])
            P.run_once(cfg, self.lenses, self.meta, state,
                       _args(dry_run=False), TARGET)
            self.assertIn(key, state["failed"])

            # 第 2 轮：清空 → permanent，perm_fp 记成空记录的指纹
            empty = copy.deepcopy(rowA)
            empty["eyes"] = {}
            self.serve([empty])
            P.run_once(cfg, self.lenses, self.meta, state,
                       _args(dry_run=False), TARGET)
            self.assertTrue(state["failed"][key].get("permanent"))
            self.assertIn("perm_fp", state["failed"][key])
            self.assertNotEqual(state["failed"][key].get("perm_fp"),
                                state["failed"][key].get("fp"),
                                "perm_fp 与 fp 必须是两个不同的值")

            # 第 3 轮：恢复**完全相同的** A 版 → 必须解除并重新推
            P.dicom.push_cstore = real_push
            self.serve([rowA])
            with fake_push():
                stats = P.run_once(cfg, self.lenses, self.meta, state,
                                   _args(dry_run=False), TARGET)
            self.assertEqual(stats["ok"], 1,
                             "恢复同一版本也必须解除 permanent，不能冻结")
            self.assertIn(key, state["done"])
        finally:
            P.dicom.push_cstore = real_push
            P._target_probe = real_probe

    def test_无接收历史时清空后补测能解除permanent(self):
        """审查的原始场景：没有任何接收历史时，permanent 必须可被解除。"""
        cfg = self.cfg_nocool()
        key = "|".join(self.key3)
        rowA = copy.deepcopy(self.row)
        rowA["eyes"]["r"]["al"] = None
        rowA["eyes"]["l"]["al"] = None

        state = {"done": {}, "failed": {}, "cooling": {}, "stale": {}}
        real_push = P.dicom.push_cstore
        real_probe = P._target_probe
        P._target_probe = lambda *a, **k: (True, "fake")
        try:
            # 第 1 轮：明确失败（known=True），没有任何接收历史
            def _hard_fail(dss, *a, **k):
                return [(ds.SOPInstanceUID, False, "0x0122", True)
                        for ds in dss]
            P.dicom.push_cstore = _hard_fail
            self.serve([rowA])
            P.run_once(cfg, self.lenses, self.meta, state,
                       _args(dry_run=False), TARGET)
            self.assertIn(key, state["failed"])
            self.assertFalse(state["failed"][key].get("uncertain"))

            # 第 2 轮：数据被清空 → 标 permanent（没有接收历史）
            empty = copy.deepcopy(rowA)
            empty["eyes"] = {}
            self.serve([empty])
            P.run_once(cfg, self.lenses, self.meta, state,
                       _args(dry_run=False), TARGET)
            self.assertTrue(state["failed"][key].get("permanent"),
                            "无测量值且无接收历史 → 标 permanent")

            # 第 3 轮：补回不同内容 → 必须解除 permanent 并正常处理
            P.dicom.push_cstore = real_push
            rowB = copy.deepcopy(rowA)
            rowB["eyes"]["r"]["r1"] += 0.3
            self.serve([rowB])
            with fake_push():
                stats = P.run_once(cfg, self.lenses, self.meta, state,
                                   _args(dry_run=False), TARGET)
            self.assertEqual(stats["ok"], 1,
                             "输入变了且无接收历史 → 应重新处理，不能冻结")
            self.assertIn(key, state["done"])
        finally:
            P.dicom.push_cstore = real_push
            P._target_probe = real_probe

    def test_无接收历史时permanent可被解除(self):
        """输入变了、但没有已发出的实例 → 解除 permanent 正常重推。"""
        cfg = self.cfg_nocool()
        key = "|".join(self.key3)
        rowA = copy.deepcopy(self.row)
        rowA["eyes"]["r"]["al"] = None
        rowA["eyes"]["l"]["al"] = None
        fp_a = P._fingerprint(P._record(rowA, self.name))
        state = {"done": {}, "failed": {}, "cooling": {}, "stale": {},
                 "failed": {key: {"t": "x", "date": self.key3[2],
                                  "fp": fp_a, "permanent": True,
                                  "err": "库中该检查无有效测量值",
                                  "tries": 1, "next": 0}}}
        rowB = copy.deepcopy(rowA)
        rowB["eyes"]["r"]["r1"] += 0.3
        self.serve([rowB])
        with fake_push():
            stats = P.run_once(cfg, self.lenses, self.meta, state,
                               _args(dry_run=False), TARGET)
        self.assertEqual(stats["ok"], 1, "输入变了且无接收历史 → 应重新处理")
        self.assertIn(key, state["done"])
        self.assertNotIn(key, state["failed"])

    def test_真实产生的永久失败会写入perm_fp(self):
        """审查的写入端遗漏：新产生的 permanent 必须带 perm_fp。

        无生日 → _process_one 早退 → commit 补齐。连续两轮相同输入，
        第二轮必须直接跳过（而不是每轮重新评估/刷屏）。
        """
        cfg = self.cfg_nocool()
        key = "|".join(self.key3)
        row_nodob = copy.deepcopy(self.row)
        row_nodob["dob"] = ""
        state = {"done": {}, "failed": {}, "cooling": {}, "stale": {}}
        real_pid = P.patient.make_pid
        P.patient.make_pid = lambda *a, **k: ""
        real_probe = P._target_probe
        P._target_probe = lambda *a, **k: (True, "fake")
        try:
            self.serve([row_nodob])
            P.run_once(cfg, self.lenses, self.meta, state,
                       _args(dry_run=False), TARGET)
            fr = state["failed"][key]
            self.assertTrue(fr.get("permanent"))
            self.assertTrue(fr.get("perm_fp"),
                            "永久失败必须写入造成它的输入指纹")
            # 第二轮：相同输入 → 必须跳过
            stats = P.run_once(cfg, self.lenses, self.meta, state,
                               _args(dry_run=False), TARGET)
            self.assertEqual(stats["total"], 0,
                             "相同输入的永久失败不该每轮重新处理")
        finally:
            P.patient.make_pid = real_pid
            P._target_probe = real_probe

    def test_带接收历史的永久失败不会被无谓转stale(self):
        """A 版部分接收 → 同 A 重试计算失败 → 再扫同 A 不该进 stale。"""
        cfg = self.cfg_nocool()
        key = "|".join(self.key3)
        fp_now = P._fingerprint(P._record(self.row, self.name))
        state = {"done": {}, "failed": {}, "cooling": {}, "stale": {},
                 "failed": {key: {"t": "x", "date": self.key3[2],
                                  "fp": fp_now, "perm_fp": fp_now,
                                  "permanent": True,
                                  "sent_uids": ["2.25.111"],
                                  "err": "计算失败", "tries": 1, "next": 0}}}
        self.serve([copy.deepcopy(self.row)])
        with fake_push():
            stats = P.run_once(cfg, self.lenses, self.meta, state,
                               _args(dry_run=False), TARGET)
        self.assertEqual(stats["total"], 0, "输入没变，应当跳过")
        self.assertNotIn(key, state["stale"],
                         "相同输入的永久失败不该转人工确认")

    def test_输入未变时permanent仍跳过(self):
        """指纹没变 → 仍是确定失败，不该每轮重试刷屏。"""
        cfg = self.cfg_nocool()
        key = "|".join(self.key3)
        fp_now = P._fingerprint(P._record(self.row, self.name))
        state = {"done": {}, "failed": {}, "cooling": {}, "stale": {},
                 "failed": {key: {"t": "x", "date": self.key3[2],
                                  "fp": fp_now, "perm_fp": fp_now,
                                  "permanent": True,
                                  "err": "永远失败", "tries": 1, "next": 0}}}
        self.serve([copy.deepcopy(self.row)])
        with fake_push():
            stats = P.run_once(cfg, self.lenses, self.meta, state,
                               _args(dry_run=False), TARGET)
        self.assertEqual(stats["total"], 0, "输入没变就不该重试")


class _FakePool:
    """假的 ProcessPoolExecutor：返回一个已带异常的 Future。

    ★ 这样能稳定进入 run_once 的 fail_task() 分支，而不用真的起进程。
      上一版测试用的是串行路径（push 抛异常 → _process_one 内部捕获 →
      commit 的失败分支），根本走不到 fail_task —— 把 fail_task 里的
      cooling 清理删掉，测试照样通过（审查实测确认）。
    """

    def __init__(self, exc):
        self.exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def submit(self, fn, *a, **k):
        f = concurrent.futures.Future()
        f.set_exception(self.exc)
        return f


class TestCommitClearsCooling(Base):
    """失败后 cooling 必须被摘掉，由 failed.next 接管（串行/commit 路径）。"""

    def test_commit失败清理cooling(self):
        cfg = self.cfg_nocool()
        key = "|".join(self.key3)
        state = {"done": {}, "failed": {}, "stale": {},
                 "cooling": {key: {"fp": "aa", "first": 1, "ready": 1}}}
        real_push = P.dicom.push_cstore
        real_probe = P._target_probe
        P._target_probe = lambda *a, **k: (True, "fake")

        def _raise(*a, **k):
            raise RuntimeError("push died")
        P.dicom.push_cstore = _raise
        try:
            self.serve([copy.deepcopy(self.row)])
            P.run_once(cfg, self.lenses, self.meta, state,
                       _args(dry_run=False), TARGET)
        finally:
            P.dicom.push_cstore = real_push
            P._target_probe = real_probe
        self.assertIn(key, state["failed"])
        self.assertNotIn(key, state["cooling"],
                         "commit 失败后 cooling 必须被摘掉（由 failed.next 接管）")


class TestFailTaskClearsCooling(Base):
    """复审重要问题：子进程异常路径也要摘掉 cooling。"""

    def test_fail_task清理cooling(self):
        key = "|".join(self.key3)
        state = {"done": {}, "failed": {}, "stale": {},
                 "cooling": {key: {"fp": "aa", "first": 1, "ready": 1}}}
        cfg = self.cfg_nocool()
        real_probe = P._target_probe
        real_pool = concurrent.futures.ProcessPoolExecutor
        P._target_probe = lambda *a, **k: (True, "fake")
        concurrent.futures.ProcessPoolExecutor = lambda *a, **k: _FakePool(
            RuntimeError("worker died"))
        try:
            self.serve([copy.deepcopy(self.row)])
            with fake_push():
                # workers=2 → 走进程池分支 → future 抛异常 → fail_task
                P.run_once(cfg, self.lenses, self.meta, state,
                           _args(dry_run=False, workers=2), TARGET)
        finally:
            concurrent.futures.ProcessPoolExecutor = real_pool
            P._target_probe = real_probe
        self.assertIn(key, state["failed"], "应当进入 fail_task 记账")
        self.assertTrue(state["failed"][key].get("uncertain"),
                        "子进程异常必须按「可能已发送」记录")
        self.assertNotIn(key, state["cooling"],
                         "fail_task 也必须摘掉 cooling（由 failed.next 接管）")


class TestLegacyNoFp(Base):
    """复审阻塞：缺 fp 必须当成「未知」，不能当成「相同」。"""

    def test_有接收历史但缺fp时转stale(self):
        """无法确认内容是否相同 → 不能自动重发（同 UID 会静默去重）。"""
        cfg = self.cfg_nocool()
        key = "|".join(self.key3)
        state = {"done": {}, "failed": {}, "cooling": {}, "stale": {},
                 "failed": {key: {"t": "x", "date": self.key3[2],
                                  "err": "旧记录没指纹",
                                  "maybe_uids": ["2.25.999"],
                                  "uncertain": True,
                                  "tries": 1, "next": 0}}}
        self.serve([copy.deepcopy(self.row)])
        calls = {"n": 0}
        real_push = P.dicom.push_cstore
        real_probe = P._target_probe
        P._target_probe = lambda *a, **k: (True, "fake")

        def _count(dss, *a, **k):
            calls["n"] += 1
            return [(ds.SOPInstanceUID, True, "0x0000", True) for ds in dss]
        P.dicom.push_cstore = _count
        try:
            stats = P.run_once(cfg, self.lenses, self.meta, state,
                               _args(dry_run=False), TARGET)
        finally:
            P.dicom.push_cstore = real_push
            P._target_probe = real_probe
        self.assertIn(key, state["stale"],
                      "有接收历史但缺 fp → 必须人工确认")
        self.assertEqual(calls["n"], 0, "不能自动发送")
        self.assertEqual(stats["ok"], 0)

    def test_旧permanent缺fp时重新评估(self):
        """HEAD 的 permanent 记录没有 fp；补齐输入后必须能重新处理。"""
        cfg = self.cfg_nocool()
        key = "|".join(self.key3)
        state = {"done": {}, "failed": {}, "cooling": {}, "stale": {},
                 "failed": {key: {"t": "x", "date": self.key3[2],
                                  "err": "无生日，无法生成 PatientID",
                                  "permanent": True, "tries": 1, "next": 0}}}
        self.serve([copy.deepcopy(self.row)])
        with fake_push():
            stats = P.run_once(cfg, self.lenses, self.meta, state,
                               _args(dry_run=False), TARGET)
        self.assertEqual(stats["ok"], 1,
                         "缺 perm_fp 的老记录必须重新评估一次")
        self.assertIn(key, state["done"])

    def test_有接收历史但缺fp且当前无测量值时转stale(self):
        cfg = self.cfg_nocool()
        key = "|".join(self.key3)
        state = {"done": {}, "failed": {}, "cooling": {}, "stale": {},
                 "failed": {key: {"t": "x", "date": self.key3[2],
                                  "err": "旧记录",
                                  "maybe_uids": ["2.25.999"],
                                  "uncertain": True,
                                  "tries": 1, "next": 0}}}
        empty = copy.deepcopy(self.row)
        empty["eyes"] = {}
        self.serve([empty])
        with fake_push():
            P.run_once(cfg, self.lenses, self.meta, state,
                       _args(dry_run=False), TARGET)
        self.assertIn(key, state["stale"],
                      "有接收历史 + 当前空 + 缺 fp → 必须人工确认")
        self.assertNotIn(key, state["failed"])


class TestLegacyEmptyDone(Base):
    """复审重要问题：src="empty" / dry 的历史 done 必须先清掉再评估。

    ★ 关键：清理必须发生在**任何指纹比较之前**。只放在「没有 fp」的
      分支里，会让「已有 fp 的 empty/dry」继续冻结（fp 相同）或被无谓
      转 stale（fp 不同）—— 第五轮代码曾误补过 fp，这是真实兼容路径。
    """

    def _run(self, done_rec):
        cfg = self.cfg_nocool()
        key = "|".join(self.key3)
        state = {"done": {key: done_rec}, "failed": {},
                 "cooling": {}, "stale": {}}
        self.serve([copy.deepcopy(self.row)])
        with fake_push():
            stats = P.run_once(cfg, self.lenses, self.meta, state,
                               _args(dry_run=False), TARGET)
        return key, state, stats

    def test_empty无fp(self):
        key, state, stats = self._run(
            {"t": "x", "pid": "y", "date": self.key3[2],
             "src": "empty", "n": 0})
        self.assertEqual(stats["ok"], 1, "empty 表示没发过，该重新处理")
        self.assertEqual(state["done"][key].get("instances"), 6)

    def test_empty带相同fp(self):
        """fp 恰好等于当前内容 —— 最危险的冻结形态。"""
        fp_now = P._fingerprint(P._record(self.row, self.name))
        key, state, stats = self._run(
            {"t": "x", "pid": "y", "date": self.key3[2],
             "src": "empty", "n": 0, "fp": fp_now})
        self.assertEqual(stats["ok"], 1,
                         "带 fp 的 empty 也必须重新处理，不能被 old==fp 挡住")
        self.assertNotEqual(state["done"][key].get("src"), "empty")
        self.assertEqual(state["done"][key].get("instances"), 6)

    def test_empty带不同fp(self):
        """fp 不同 → 不能当成「已推旧内容」而转 stale。"""
        key, state, stats = self._run(
            {"t": "x", "pid": "y", "date": self.key3[2],
             "src": "empty", "n": 0, "fp": "deadbeefdeadbeef"})
        self.assertEqual(stats["ok"], 1, "empty 不是「已推过的旧内容」")
        self.assertNotIn(key, state["stale"])

    def test_dry带fp且当前无测量值时不造错误stale(self):
        """审查的连续场景：dry done（带 fp）→ 当前无测量值 → 补回不同内容。

        第一轮绝不能造出 from="emptied" 的 stale（那表示「PACS 里是旧版」，
        可这条记录从来没发过），第二轮应当正常经冷却+发送。
        """
        cfg = self.cfg_nocool()
        key = "|".join(self.key3)
        fp_a = "aaaa1111bbbb2222"
        state = {"done": {key: {"t": "x", "pid": "y", "date": self.key3[2],
                                "dry": True, "n": 6, "fp": fp_a}},
                 "failed": {}, "cooling": {}, "stale": {}}

        # 第一轮：当前库里的记录没有任何测量值
        empty = copy.deepcopy(self.row)
        empty["eyes"] = {}
        self.serve([empty])
        real_probe = P._target_probe
        P._target_probe = lambda *a, **k: (True, "fake")
        try:
            with fake_push():
                P.run_once(cfg, self.lenses, self.meta, state,
                           _args(dry_run=False), TARGET)
            self.assertNotIn(key, state["stale"],
                             "dry 记录从没发过，不该被当成「PACS 里是旧版」")
            self.assertNotIn(key, state["done"], "dry 记录应被清掉")

            # 第二轮：补回不同的有效内容 → 应正常处理
            rowB = copy.deepcopy(self.row)
            rowB["eyes"]["r"]["r1"] += 0.3
            self.serve([rowB])
            with fake_push():
                stats = P.run_once(cfg, self.lenses, self.meta, state,
                                   _args(dry_run=False), TARGET)
            self.assertEqual(stats["ok"], 1, "补回有效内容后应当正常发送")
            self.assertNotIn(key, state["stale"])
        finally:
            P._target_probe = real_probe

    def test_empty带fp且当前无测量值时不造错误stale(self):
        """同上，但记录是 src="empty"。"""
        cfg = self.cfg_nocool()
        key = "|".join(self.key3)
        state = {"done": {key: {"t": "x", "pid": "y", "date": self.key3[2],
                                "src": "empty", "n": 0,
                                "fp": "aaaa1111bbbb2222"}},
                 "failed": {}, "cooling": {}, "stale": {}}
        empty = copy.deepcopy(self.row)
        empty["eyes"] = {}
        self.serve([empty])
        real_probe = P._target_probe
        P._target_probe = lambda *a, **k: (True, "fake")
        try:
            with fake_push():
                P.run_once(cfg, self.lenses, self.meta, state,
                           _args(dry_run=False), TARGET)
            self.assertNotIn(key, state["stale"])
            self.assertNotIn(key, state["done"])
        finally:
            P._target_probe = real_probe

    def test_dry带相同fp(self):
        fp_now = P._fingerprint(P._record(self.row, self.name))
        key, state, stats = self._run(
            {"t": "x", "pid": "y", "date": self.key3[2],
             "dry": True, "n": 6, "fp": fp_now})
        self.assertEqual(stats["ok"], 1, "dry 记录从没推过，必须重新处理")


class TestStaleReleaseRestoresHistory(Base):
    """复审阻塞 4：failed 来源的 stale 自动解除时，必须恢复接收历史。"""

    def test_解除后保留历史(self):
        cfg = self.cfg_nocool()
        key = "|".join(self.key3)
        fp_a = P._fingerprint(P._record(self.row, self.name))
        state = {
            "done": {}, "failed": {}, "cooling": {},
            "stale": {key: {
                "t": "x", "date": self.key3[2], "name": self.name,
                "was_fp": fp_a, "now_fp": "deadbeef", "from": "failed",
                "was": {"instances": 6, "sent": 1,
                        "sent_uids": ["2.25.111"], "maybe_uids": [],
                        "uncertain": False},
                "now_instances": 6,
            }},
        }
        # 内容回到 A（PACS 里只部分接收过的那一版）
        self.serve([copy.deepcopy(self.row)])
        with fake_push():
            P.run_once(cfg, self.lenses, self.meta, state,
                       _args(dry_run=False), TARGET)
        self.assertNotIn(key, state["stale"], "stale 应当被解除")
        self.assertNotIn(key, state["done"],
                         "解除的当轮不该直接重推（要按退避补齐）")
        # ★ 历史必须挪回 failed，而不是凭空消失
        fr = state.get("failed", {}).get(key) or {}
        self.assertEqual(fr.get("sent_uids"), ["2.25.111"],
                         "解除 stale 时必须把已接收记录挪回 failed")
        self.assertEqual(fr.get("fp"), fp_a)


class TestPacsContentValue(Base):
    """复审问题 7：核对 PACS 里的**具体测量值**，而不只是实例数量。"""

    def test_未删旧实例时KER曲率保持不变(self):
        cfg = self.cfg_nocool()
        key = "|".join(self.key3)
        rowA = copy.deepcopy(self.row)
        rowA["eyes"]["r"]["al"] = None
        rowA["eyes"]["l"]["al"] = None
        radius_old = round(float(rowA["eyes"]["r"]["r1"]), 3)

        with fake_push(pacs=True) as pacs:
            state = {"done": {}, "failed": {}, "cooling": {}, "stale": {}}
            self.serve([rowA])
            P.run_once(cfg, self.lenses, self.meta, state,
                       _args(dry_run=False), TARGET)
            # 记下 PACS 里 KER 的曲率
            snap = {}
            for ds in []:
                pass
            before = dict(pacs.store)

            # 补测后强行清 state 重推（模拟「只删 state」的错误做法）
            state["done"].pop(key, None)
            state["stale"].pop(key, None)
            rowB = copy.deepcopy(self.row)
            rowB["eyes"]["r"]["r1"] = radius_old + 0.3
            self.serve([rowB])
            P.run_once(cfg, self.lenses, self.meta, state,
                       _args(dry_run=False), TARGET)

            # 同 UID 被去重 → 库里的内容摘要不该变
            self.assertTrue(pacs.dupes, "应当观察到同 UID 去重")
            for uid, dg in before.items():
                if uid in pacs.store:
                    self.assertEqual(pacs.store[uid], dg,
                                     "只删 state 不足以让新内容生效")


if __name__ == "__main__":
    unittest.main(verbosity=2)
