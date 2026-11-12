"""Synthetic rows + real DICOM builders + first-wins PACS double. No MDB/REST."""
from __future__ import annotations

import copy
import concurrent.futures
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pipeline as P
from core import patient, revisions
from PIL import Image

REAL_CSTORE = P.dicom.push_cstore

def row():
    eye = {"al": 24.0, "r1": 7.8, "r2": 7.6, "acd": 3.1,
           "axis": 10.0, "state": ""}
    return {"guid": "synthetic-guid-1", "dob": "19700101", "exam_date": "20000102",
            "sex": "M", "measure_ts": 1.0, "eyes": {"r": dict(eye), "l": dict(eye)}}


def lens(name):
    # Fictional test-only constants; never used for clinical output.
    return {"name": name, "key": "synthetic:" + name, "srkt_a": 118.0,
            "haigis": (1.0, 0.4, 0.1), "pacd": 5.0, "sf": 1.0}


class Rounds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = self.tmp.name + "/state.json"
        Path(self.tmp.name, "source.mdb").touch()
        self.args = types.SimpleNamespace(mdb=self.tmp.name + "/source.mdb", since="",
                    limit=0, dry_run=False, max_seconds=0, workers=1, state=self.path)
        self.cfg = {"watcher": {"cooling_seconds": 0}}
        self.lenses = [lens(n) for n in ("DCB00", "Test B", "Test C", "Test D")]
        self.meta = {"target_refraction": -0.5, "step": 0.5}
        self.target = {"host": "fake", "port": 4242, "aet": "ORTHANC"}
        self.rows = [row()]
        self.key = "synthetic-guid-1|20000102"
        self.state = {"done": {}, "failed": {}, "cooling": {}, "stale": {},
                      "key_scheme": P.KEY_SCHEME,
                      "sig": P.INSTANCE_LAYOUT + "||" + "|".join(l['key'] for l in self.lenses)}
        self.store = {}
        self.calls = []
        self.fail_mode = None
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(P.mdb, "assert_supported", return_value={}))
        self.stack.enter_context(patch.object(P.mdb, "load_measurements",
            side_effect=lambda *a: getattr(self, 'groups',
                {("合", "成", "20000102"): self.rows} if self.rows else {})))
        self.stack.enter_context(patch.object(P, "_target_probe", return_value=(True, "fake")))
        self.stack.enter_context(patch.object(P.report, "render_image",
            side_effect=lambda *a, **k: Image.new("RGB", (64, 64), "white")))
        self.stack.enter_context(patch.object(P.dicom, "push_cstore", side_effect=self.push))
        self.output = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.output))

    def push(self, datasets, *args, **kwargs):
        journal = json.loads(Path(self.path).read_text())
        matches = [p for p in journal['pending'].values()
                   if P.dicom.study_uid_for(patient.make_pid(p['rec']['dob'], p['rec']['name']),
                       p['date'], p['uid_seed']) == str(datasets[0].StudyInstanceUID)]
        self.assertEqual(len(matches), 1)
        pending = matches[0]
        self.assertTrue(pending['ready'], "journal must be durable before any C-STORE")
        self.assertTrue(Path(pending['rec']['_spool'], 'manifest.json').is_file())
        self.calls.append(copy.deepcopy(datasets))
        result = []
        for i, ds in enumerate(datasets):
            sop = str(ds.SOPInstanceUID)
            if self.fail_mode == 'reject' or (self.fail_mode == 'partial' and i > 0):
                result.append((sop, False, '0xA700', True))
                continue
            self.store.setdefault(sop, copy.deepcopy(ds))
            if self.fail_mode == 'lost':
                result.append((sop, False, 'no response', False))
            elif self.fail_mode == 'warning':
                result.append((sop, True, '0xB000', True))
            else:
                result.append((sop, True, '0x0000', True))
        return result

    def run_round(self, reload=False):
        if reload:
            self.state = P._load_state(self.path, self.lenses)
        return P.run_once(self.cfg, self.lenses, self.meta, self.state, self.args, self.target)

    def due(self):
        self.state['failed'][self.key]['next'] = 0
        P._save_state(self.path, self.state)

    def change(self, radius):
        self.rows[0]['eyes']['r']['r1'] = radius

    def ker(self, datasets):
        return next(ds for ds in datasets if hasattr(ds, 'KeratometryRightEyeSequence'))

    def radius(self, datasets):
        return float(self.ker(datasets).KeratometryRightEyeSequence[0]
                     .FlatKeratometricAxisSequence[0].RadiusOfCurvature)

    def test_same_count_rec2_rec3_same_study_no_duplicate(self):
        self.assertEqual(self.run_round()['ok'], 1)
        first = self.calls[-1]
        self.change(8.0)
        self.assertEqual(self.run_round()['ok'], 1)
        second = self.calls[-1]
        self.change(8.2)
        self.assertEqual(self.run_round()['ok'], 1)
        third = self.calls[-1]
        self.assertEqual([len(x) for x in (first, second, third)], [6, 6, 6])
        self.assertEqual(len({str(ds.StudyInstanceUID) for ds in self.store.values()}), 1)
        self.assertEqual(len(self.store), 18)
        self.assertEqual(len({str(ds.SeriesInstanceUID) for ds in self.store.values()}), 18)
        self.assertEqual([self.radius(x) for x in (first, second, third)], [7.8, 8.0, 8.2])
        self.assertTrue(all(str(ds.SeriesDescription).endswith(' Rec 2') for ds in second))
        self.assertIn('IOL Repo DCB00 Rec 2', [str(ds.SeriesDescription) for ds in second])
        self.assertTrue(all(str(ds.SeriesDescription).endswith(' Rec 3') for ds in third))
        self.assertTrue(all(ds.SOPInstanceUID == ds.file_meta.MediaStorageSOPInstanceUID
                            for ds in self.store.values()))
        self.assertEqual(self.state['done'][self.key]['revision'], 3)
        self.assertEqual(len(self.state['done'][self.key]['history']), 2)
        self.assertEqual(self.run_round(reload=True)['total'], 0)
        self.assertEqual(len(self.calls), 3)

    def test_supplemental_second_eye_same_six_instances_updates_oam_and_ker(self):
        self.rows[0]['eyes'].pop('l')
        self.assertEqual(self.run_round()['ok'], 1)
        first = self.calls[-1]
        self.rows[0]['eyes']['l'] = copy.deepcopy(row()['eyes']['l'])
        self.rows[0]['eyes']['l']['al'] = 25.2
        self.rows[0]['eyes']['l']['r1'] = 8.3
        self.assertEqual(self.run_round()['ok'], 1)
        latest = self.calls[-1]
        self.assertEqual([len(first), len(latest)], [6, 6])
        old_oam = next(ds for ds in first if str(ds.SOPClassUID) == P.oam.SOP_OAM)
        new_oam = next(ds for ds in latest if str(ds.SOPClassUID) == P.oam.SOP_OAM)
        self.assertFalse(hasattr(old_oam, 'OphthalmicAxialMeasurementsLeftEyeSequence'))
        self.assertFalse(hasattr(self.ker(first), 'KeratometryLeftEyeSequence'))
        self.assertAlmostEqual(float(new_oam.OphthalmicAxialMeasurementsLeftEyeSequence[0]
            .OpticalSelectedOphthalmicAxialLengthSequence[0]
            .SelectedTotalOphthalmicAxialLengthSequence[0].OphthalmicAxialLength), 25.2, places=5)
        self.assertAlmostEqual(float(self.ker(latest).KeratometryLeftEyeSequence[0]
            .FlatKeratometricAxisSequence[0].RadiusOfCurvature), 8.3, places=5)
        self.assertEqual(len(self.store), 12)
        self.assertEqual(new_oam.StudyInstanceUID, old_oam.StudyInstanceUID)
        self.assertNotEqual(new_oam.SOPInstanceUID, old_oam.SOPInstanceUID)

    def test_transport_retains_warning_when_later_send_throws(self):
        self.run_round()
        datasets = self.calls[-1]
        class FakeAssociation:
            is_established = True
            def __init__(self, status):
                self.status = status
            def send_c_store(self, ds):
                if self.status is None:
                    raise OSError('synthetic disconnect')
                return types.SimpleNamespace(Status=self.status)
            def release(self):
                raise OSError('synthetic release failure')
        class FakeAE:
            def __init__(self, **kw):
                pass
            def add_requested_context(self, *a):
                pass
            def associate(self, *a, **kw):
                return FakeAssociation(next(statuses))
        # Both objects are structured, so no JPEG fallback is needed.
        structured = [ds for ds in datasets if str(ds.SOPClassUID) != P.dicom.SOP_SC]
        statuses = iter([0xB000, None])
        with patch('pynetdicom.AE', FakeAE):
            results = REAL_CSTORE(structured, 'fake', 4242, 'ORTHANC')
        self.assertEqual(results[0][2], '0xB000')
        self.assertTrue(results[0][1])
        self.assertFalse(results[1][3])
        self.change(8.0)
        # The parent must preserve the first Warning as a permanent pause.
        def warning_then_disconnect(current, *a, **kw):
            return [(str(ds.SOPInstanceUID), i == 0,
                     '0xB000' if i == 0 else 'no response', i == 0)
                    for i, ds in enumerate(current)]
        with patch.object(P.dicom, 'push_cstore', side_effect=warning_then_disconnect):
            self.assertEqual(self.run_round()['fail'], 1)
        self.assertTrue(self.state['failed'][self.key]['permanent'])
        self.assertEqual(self.state['done'][self.key]['revision'], 1)

    def test_large_legacy_baseline_only_changed_rows_get_rec2(self):
        original = row()
        baseline = P._fingerprint(P._record(original, '合成'))
        pid = patient.make_pid(original['dob'], '合成')
        self.rows = []
        for i in range(15000):
            r = copy.deepcopy(original)
            r['guid'] = f'synthetic-{i}'
            self.rows.append(r)
            key = P._row_key(r)
            self.state['done'][key] = {"fp": baseline, "pid": pid, "instances": 6,
                                      "date": '20000102', "uid_seed": r['guid']}
        for i in range(3):
            self.rows[i]['eyes']['r']['r1'] = 8.1
            key = P._row_key(self.rows[i])
            self.state['stale'][key] = {'was_fp': baseline, 'now_fp': 'old',
                                       'was': {'instances': 6}, 'now_instances': 6}
        unaffected = {k: copy.deepcopy(v) for k, v in self.state['done'].items()
                      if k not in self.state['stale']}
        # Instrumented transport uses self.key to inspect the journal.
        def multi_push(datasets, *a, **k):
            self.assertTrue(any(p.get('ready') for p in
                                json.loads(Path(self.path).read_text())['pending'].values()))
            self.calls.append(copy.deepcopy(datasets))
            for ds in datasets:
                self.store.setdefault(str(ds.SOPInstanceUID), copy.deepcopy(ds))
            return [(str(ds.SOPInstanceUID), True, '0x0000', True) for ds in datasets]
        with patch.object(P.dicom, 'push_cstore', side_effect=multi_push):
            P._save_state(self.path, self.state)
            self.assertEqual(self.run_round(reload=True)['ok'], 3)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len(self.store), 18)
        self.assertEqual(len(self.state['done']), 15000)
        self.assertFalse(self.state['stale'])
        self.assertTrue(all(self.state['done'][P._row_key(self.rows[i])]['revision'] == 2
                            for i in range(3)))
        self.assertNotIn('revision', self.state['done'][P._row_key(self.rows[3])])
        self.assertEqual({k: self.state['done'][k] for k in unaffected}, unaffected)

    def test_partial_round_finishes_before_new_content_even_after_restart(self):
        self.run_round()
        self.change(8.0)
        self.fail_mode = 'partial'
        self.assertEqual(self.run_round()['fail'], 1)
        attempted = self.calls[-1]
        self.assertEqual(self.state['done'][self.key]['revision'], 1)
        self.change(8.3)
        self.due()
        self.fail_mode = None
        self.assertEqual(self.run_round(reload=True)['ok'], 1)
        self.assertEqual([str(ds.SOPInstanceUID) for ds in attempted],
                         [str(ds.SOPInstanceUID) for ds in self.calls[-1]])
        self.assertEqual(self.radius(self.calls[-1]), 8.0)
        self.assertEqual(len(self.store), 12)
        self.assertGreater(self.run_round()['next_ready'], 0)
        self.assertEqual(self.state['done'][self.key]['revision'], 3)
        self.assertEqual(self.radius(self.calls[-1]), 8.3)
        self.assertEqual(len(self.store), 18)

    def legacy_three(self):
        """Synthetic legacy journal: three distinct synthetic patients,
        missing revision/from and missing-or-empty historical study seed.
        """
        self.groups = {}
        self.legacy_keys = []
        for i in range(3):
            r = row()
            r['guid'] = f'legacy-synthetic-{i}'
            r['dob'] = f'1970010{i + 1}'
            name = f'测试{i}'
            rec = P._record(r, name)
            key, fp = P._row_key(r), P._fingerprint(rec)
            rec['_revision'] = 1
            rec['_spool'] = str(Path(self.tmp.name, f'old-{i}'))
            _, good, info, _, _ = P._process_one(
                ('20000102', key, rec, fp, ''), self.cfg, self.lenses,
                self.meta, self.target, False)
            self.assertTrue(good)
            datasets, info = revisions.read_payload(rec['_spool'])
            for ds in datasets:
                self.store[str(ds.SOPInstanceUID)] = copy.deepcopy(ds)
            info = {k: v for k, v in info.items()
                    if k not in ('revision', 'study_uid', 'prepared', 'guid')}
            if i % 2 == 0:
                info.pop('uid_seed')
            self.state['done'][key] = info
            r['eyes']['r']['r1'] = 8.0 + i / 10
            self.groups[('测', f'试{i}', '20000102')] = [r]
            self.state['stale'][key] = {
                't': '2000-01-02 00:00:00', 'date': '20000102',
                'was_fp': fp, 'now_fp': P._fingerprint(P._record(r, name)),
                'was': {'instances': 6, 'sc': 4, 'oam_eyes': 2, 'ker_eyes': 2},
                'now_instances': 6}
            self.legacy_keys.append(key)
        self.old_objects = copy.deepcopy(self.store)
        self.old_done = copy.deepcopy(self.state['done'])
        P._save_state(self.path, self.state)

    def assert_three_converged(self):
        disk = json.loads(Path(self.path).read_text())
        self.assertFalse(disk['stale'])
        self.assertFalse(disk['pending'])
        self.assertEqual(len(self.store), 36)
        self.assertEqual(len({str(ds.StudyInstanceUID) for ds in self.store.values()}), 3)
        for sop, old in self.old_objects.items():
            self.assertEqual(self.store[sop], old)
        for i, key in enumerate(self.legacy_keys):
            done = disk['done'][key]
            self.assertEqual(done['revision'], 2)
            self.assertEqual(done['instances'], 6)
            self.assertEqual(len(done['sop_uids']), 6)
            self.assertEqual(set(done['payload_sha256']), set(done['sop_uids']))
            self.assertEqual(done['fp'], P._fingerprint(
                P._record(self.groups[('测', f'试{i}', '20000102')][0], f'测试{i}')))
            current = [ds for sop, ds in self.store.items()
                       if sop not in self.old_objects and str(ds.StudyInstanceUID) == done['study_uid']]
            self.assertEqual(len(current), 6)
            self.assertTrue(all(str(ds.SeriesDescription).endswith(' Rec 2') for ds in current))
            self.assertEqual(self.radius(current), 8.0 + i / 10)
        calls = len(self.calls)
        self.assertEqual(self.run_round(reload=True)['total'], 0)
        self.assertEqual(self.run_round(reload=True)['total'], 0)
        self.assertEqual(len(self.calls), calls)

    def test_three_legacy_six_to_six_missing_seed_auto_rec2(self):
        self.legacy_three()
        self.assertEqual(self.run_round(reload=True)['ok'], 3)
        self.assertEqual(len(self.calls), 3)
        self.assert_three_converged()

    def test_three_legacy_partial_or_lost_response_restart_keeps_rec2(self):
        self.legacy_three()
        self.fail_mode = 'partial'
        self.assertEqual(self.run_round(reload=True)['fail'], 3)
        disk = json.loads(Path(self.path).read_text())
        self.assertEqual(len(disk['stale']), 3)
        self.assertEqual(disk['done'], self.old_done)
        self.assertIn('ERROR', self.output.getvalue())
        self.assertIn('Rec 2 未完成 1/6', self.output.getvalue())
        self.assertNotIn('Rec 2 6 实例齐全，done 已持久化', self.output.getvalue())
        attempted = copy.deepcopy(self.calls)
        self.fail_mode = 'lost'
        for key in self.legacy_keys:
            self.state['failed'][key]['next'] = 0
        P._save_state(self.path, self.state)
        self.assertEqual(self.run_round(reload=True)['fail'], 3)
        self.assertEqual(len(self.store), 36)
        self.assertEqual(len(self.state['stale']), 3)
        self.fail_mode = None
        for key in self.legacy_keys:
            self.state['failed'][key]['next'] = 0
        P._save_state(self.path, self.state)
        self.assertEqual(self.run_round(reload=True)['ok'], 3)
        for before, after in zip(attempted, self.calls[-3:]):
            self.assertEqual(before, after)
        self.assert_three_converged()

    def test_three_legacy_done_write_failure_preserves_stale_until_restart(self):
        self.legacy_three()
        save = P._save_state
        def fail_commit(path, state):
            if any(d.get('revision') == 2 for d in state['done'].values()):
                raise P.StatePersistenceError('synthetic disk full at migration commit')
            return save(path, state)
        with patch.object(P, '_save_state', side_effect=fail_commit):
            with self.assertRaises(P.StatePersistenceError):
                self.run_round(reload=True)
        disk = json.loads(Path(self.path).read_text())
        self.assertEqual(len(disk['stale']), 3)
        self.assertEqual(disk['done'], self.old_done)
        self.assertEqual(len(self.store), 24)
        self.assertNotIn('Rec 2 6 实例齐全，done 已持久化', self.output.getvalue())
        self.assertEqual(self.run_round(reload=True)['ok'], 3)
        self.assert_three_converged()

    def test_shared_historical_study_is_blocked(self):
        self.legacy_three()
        key = self.legacy_keys[0]
        self.state['done']['unrelated-owner|20000102'] = copy.deepcopy(self.state['done'][key])
        P._save_state(self.path, self.state)
        stats = self.run_round(reload=True)
        self.assertEqual(stats['blocked_cases'], 1)
        self.assertIn(key, self.state['stale'])
        self.assertNotIn('revision', self.state['done'][key])
        self.assertEqual(len(self.calls), 2)

    def test_archived_legacy_stale_is_not_cleared_as_if_converged(self):
        self.legacy_three()
        self.groups = {}
        stats = self.run_round(reload=True)
        self.assertEqual(stats['blocked_cases'], 3)
        self.assertFalse(self.calls)
        self.assertEqual(len(self.state['stale']), 3)
        self.assertEqual(self.state['done'], self.old_done)

    def test_payload_metadata_cannot_be_reassigned_to_another_study(self):
        self.fail_mode = 'partial'
        self.run_round()
        spool = Path(self.state['pending'][self.key]['rec']['_spool'])
        manifest = json.loads((spool / 'manifest.json').read_text())
        manifest['info']['study_uid'] = '2.25.999'
        (spool / 'manifest.json').write_text(json.dumps(manifest))
        self.due()
        calls = len(self.calls)
        self.run_round(reload=True)
        self.assertEqual(len(self.calls), calls)
        self.assertTrue(self.state['failed'][self.key]['permanent'])
        self.assertFalse(self.state['done'])

    def test_lost_response_retries_same_bytes_and_same_number(self):
        self.fail_mode = 'lost'
        self.assertEqual(self.run_round()['fail'], 1)
        self.assertFalse(self.state['done'])
        self.assertEqual(len(self.store), 6)
        before = copy.deepcopy(self.store)
        self.due()
        self.fail_mode = None
        self.assertEqual(self.run_round(reload=True)['ok'], 1)
        self.assertEqual(len(self.store), 6)
        self.assertEqual(self.state['done'][self.key]['revision'], 1)
        for ds in self.calls[-1]:
            self.assertEqual(ds, before[str(ds.SOPInstanceUID)])

    def test_generation_failure_sends_nothing(self):
        with patch.object(P.oam, 'build', side_effect=ValueError('synthetic build error')):
            self.assertEqual(self.run_round()['fail'], 1)
        self.assertFalse(self.calls)
        self.assertFalse(self.state['done'])
        self.assertFalse(self.state['pending'][self.key].get('ready'))
        self.due()
        self.assertEqual(self.run_round()['ok'], 1)
        self.assertEqual(self.state['done'][self.key]['revision'], 1)

    def test_corrected_unsent_permanent_input_can_recover_automatically(self):
        self.rows[0]['dob'] = ''
        self.assertEqual(self.run_round()['fail'], 1)
        self.assertTrue(self.state['failed'][self.key]['permanent'])
        self.assertFalse(self.calls)
        self.rows[0]['dob'] = row()['dob']
        self.assertEqual(self.run_round(reload=True)['ok'], 1)
        self.assertEqual(self.state['done'][self.key]['revision'], 1)
        self.assertFalse(self.state['pending'])

    def test_missing_or_corrupt_published_payload_stops(self):
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt):
                self.fail_mode = 'reject'
                self.run_round()
                spool = self.state['pending'][self.key]['rec']['_spool']
                if corrupt:
                    Path(spool, '0.dcm').write_bytes(b'corrupted')
                else:
                    shutil.rmtree(spool)
                count = len(self.calls)
                self.due()
                self.run_round(reload=True)
                self.assertEqual(len(self.calls), count)
                self.assertTrue(self.state['failed'][self.key]['permanent'])
                self.assertIn(self.key, self.state['pending'])
                self.assertFalse(self.state['done'])
                if not corrupt:
                    # Reset only this synthetic subcase; real deployment must restore backup.
                    self.state['pending'].clear()
                    self.state['failed'].clear()

    def test_journal_write_failure_prevents_send(self):
        with patch.object(P, '_save_state', side_effect=P.StatePersistenceError('disk full')):
            with self.assertRaises(P.StatePersistenceError):
                self.run_round()
        self.assertFalse(self.calls)
        self.assertFalse(self.state['done'])

    def test_ready_write_failure_prevents_send_and_reuses_spool(self):
        original = P._save_state
        def fail_ready(path, state):
            if state.get('pending', {}).get(self.key, {}).get('ready'):
                raise P.StatePersistenceError('disk full at ready')
            return original(path, state)
        with patch.object(P, '_save_state', side_effect=fail_ready):
            with self.assertRaises(P.StatePersistenceError):
                self.run_round()
        self.assertFalse(self.calls)
        self.assertEqual(self.run_round(reload=True)['ok'], 1)

    def test_success_before_done_write_failure_replays_without_duplicate(self):
        original = P._save_state
        def fail_done(path, state):
            if state['done']:
                raise P.StatePersistenceError('disk full at done')
            return original(path, state)
        with patch.object(P, '_save_state', side_effect=fail_done):
            with self.assertRaises(P.StatePersistenceError):
                self.run_round()
        self.assertEqual(len(self.store), 6)
        disk = json.loads(Path(self.path).read_text())
        self.assertFalse(disk['done'])
        self.assertTrue(disk['pending'][self.key]['ready'])
        self.assertEqual(self.run_round(reload=True)['ok'], 1)
        self.assertEqual(len(self.store), 6)

    def test_warning_does_not_commit(self):
        self.fail_mode = 'warning'
        self.assertEqual(self.run_round()['fail'], 1)
        self.assertFalse(self.state['done'])
        self.assertTrue(self.state['failed'][self.key]['uncertain'])
        self.assertTrue(self.state['failed'][self.key]['permanent'])
        count = len(self.calls)
        self.run_round(reload=True)
        self.assertEqual(len(self.calls), count)

    def test_pending_target_change_is_blocked(self):
        self.fail_mode = 'reject'
        self.run_round()
        count = len(self.calls)
        self.target['host'] = 'another-pacs'
        self.due()
        with self.assertRaisesRegex(ValueError, 'target changed'):
            self.run_round()
        self.assertEqual(len(self.calls), count)

    def test_source_archival_still_completes_pending_round(self):
        self.fail_mode = 'partial'
        self.run_round()
        self.rows.clear()
        self.due()
        self.fail_mode = None
        self.assertEqual(self.run_round(reload=True)['ok'], 1)
        self.assertEqual(len(self.store), 6)

    def test_dry_run_does_not_reserve_or_write(self):
        self.args.dry_run = True
        self.run_round()
        self.assertFalse(self.calls)
        self.assertNotIn('pending', self.state)
        self.assertFalse(Path(self.path).exists())
        self.assertFalse(Path(self.path + '.spool').exists())

    def test_corrupt_state_and_changed_signature_do_not_reset(self):
        Path(self.path).write_text('{broken')
        with self.assertRaisesRegex(ValueError, '状态损坏'):
            P._load_state(self.path, self.lenses)
        self.run_round()
        with self.assertRaisesRegex(ValueError, '签名不符'):
            P._load_state(self.path, [lens('different')])

    def test_round1_uid_compat_and_suffix_truncation(self):
        self.run_round()
        first = self.calls[-1]
        study = first[0].StudyInstanceUID
        self.assertEqual(self.ker(first).SOPInstanceUID, P.keratometry.sop_uid_for(study))
        copied = copy.deepcopy(first)
        copied[0].SeriesDescription = 'x' * 64
        revisions.apply_round(copied, 2)
        self.assertEqual(len(copied[0].SeriesDescription), 64)
        self.assertTrue(copied[0].SeriesDescription.endswith(' Rec 2'))
        self.assertEqual(copied[0].StudyInstanceUID, study)
        self.assertNotEqual(copied[0].SOPInstanceUID, first[0].SOPInstanceUID)

    def test_back_to_old_content_creates_next_round(self):
        self.run_round()
        self.change(8.0)
        self.run_round()
        self.change(7.8)
        self.run_round()
        self.assertEqual(self.state['done'][self.key]['revision'], 3)
        self.assertEqual(self.radius(self.calls[-1]), 7.8)
        self.assertEqual(len(self.store), 18)

    def test_spool_write_failure_sends_nothing(self):
        with patch.object(P.revisions, 'publish_payload', side_effect=OSError('disk full')):
            self.assertEqual(self.run_round()['fail'], 1)
        self.assertFalse(self.calls)
        self.assertFalse(self.state['done'])

    def test_legacy_empty_and_identity_change_stop_without_sending(self):
        self.run_round()
        self.rows[0]['dob'] = '19710101'
        self.assertEqual(self.run_round()['blocked_cases'], 1)
        self.assertEqual(len(self.calls), 1)
        self.rows[0] = row()
        for eye in self.rows[0]['eyes'].values():
            eye.update(al=None, r1=None, r2=None, acd=None)
        self.run_round()
        self.assertEqual(len(self.calls), 1)
        self.assertIn(self.key, self.state['stale'])

    def test_legacy_partial_stops_without_losing_history(self):
        self.state['failed'][self.key] = {'fp': 'old', 'sent_uids': ['2.25.1'], 'next': 0}
        self.assertEqual(self.run_round()['blocked_cases'], 1)
        self.assertFalse(self.calls)
        self.assertEqual(self.state['failed'][self.key]['sent_uids'], ['2.25.1'])

    def test_duplicate_key_rejected_before_dispatch(self):
        self.rows.append(copy.deepcopy(self.rows[0]))
        self.assertEqual(self.run_round()['blocked'], 'duplicate_key')
        self.assertFalse(self.calls)

    def test_state_missing_with_spool_stops(self):
        self.run_round()
        Path(self.path).unlink()
        with self.assertRaisesRegex(ValueError, 'state 丢失'):
            P._load_state(self.path, self.lenses)

    def test_only_one_cli_writer_allowed(self):
        with open(self.path + '.lock', 'w') as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(P, 'cmd_run') as cmd:
                self.assertEqual(P.main(['run', '--state', self.path]), 2)
                cmd.assert_not_called()

    def test_pool_worker_prepares_and_parent_sends(self):
        class InlinePool:
            def __init__(self, **kw):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
            def submit(self, fn, *a):
                f = concurrent.futures.Future()
                f.set_result(fn(*a))
                return f
        self.args.workers = 2
        with patch.object(P.concurrent.futures, 'ProcessPoolExecutor', InlinePool):
            self.assertEqual(self.run_round()['ok'], 1)
            self.change(8.0)
            self.assertEqual(self.run_round()['ok'], 1)
        self.assertEqual(self.state['done'][self.key]['revision'], 2)
        self.assertEqual(len(self.store), 12)

    def test_worker_crash_recovers_same_round(self):
        class CrashingPool:
            def __init__(self, **kw):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
            def submit(self, fn, *a):
                f = concurrent.futures.Future()
                f.set_exception(RuntimeError('synthetic worker crash'))
                return f
        self.args.workers = 2
        with patch.object(P.concurrent.futures, 'ProcessPoolExecutor', CrashingPool):
            self.assertEqual(self.run_round()['fail'], 1)
        self.assertFalse(self.calls)
        self.assertEqual(self.state['pending'][self.key]['revision'], 1)
        self.due()
        self.args.workers = 1
        self.assertEqual(self.run_round(reload=True)['ok'], 1)
        self.assertEqual(self.state['done'][self.key]['revision'], 1)

    def test_audit_pending_is_nonzero_and_read_only(self):
        self.fail_mode = 'reject'
        self.run_round()
        before = Path(self.path).read_bytes()
        args = types.SimpleNamespace(config=None, mdb=self.args.mdb, state=self.path,
                                     limit=0, since='', show_ident=False)
        with patch.object(P.config, 'build', return_value=(self.cfg, self.lenses, self.meta)):
            self.assertEqual(P.cmd_audit(args), 1)
        self.assertEqual(Path(self.path).read_bytes(), before)

    def test_real_pool_renders_payload_parent_cstores(self):
        self.args.workers = 2
        self.assertEqual(self.run_round()['ok'], 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.store), 6)

    def test_loopback_dicom_scp_first_wins_receives_rec2_and_rec3(self):
        from pynetdicom import AE, evt
        from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian, JPEGBaseline8Bit
        stored = {}
        def receive(event):
            ds = event.dataset
            ds.file_meta = event.file_meta
            stored.setdefault(str(ds.SOPInstanceUID), copy.deepcopy(ds))
            return 0x0000
        ae = AE(ae_title='ORTHANC')
        for sop in (P.dicom.SOP_SC, P.oam.SOP_OAM, P.keratometry.SOP_KER):
            ae.add_supported_context(sop, [ExplicitVRLittleEndian, ImplicitVRLittleEndian, JPEGBaseline8Bit])
        server = ae.start_server(('127.0.0.1', 0), block=False,
                                 evt_handlers=[(evt.EVT_C_STORE, receive)])
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.target.update(host='127.0.0.1', port=server.server_address[1])
        # Use the real DIMSE client, captured outside all patches.
        with patch.object(P.dicom, 'push_cstore', REAL_CSTORE):
            self.assertEqual(self.run_round()['ok'], 1)
            self.change(8.0)
            self.assertEqual(self.run_round()['ok'], 1)
            self.change(8.2)
            self.assertEqual(self.run_round()['ok'], 1)
        self.assertEqual(len(stored), 18)
        self.assertEqual(len({str(ds.StudyInstanceUID) for ds in stored.values()}), 1)
        latest = [ds for ds in stored.values() if str(ds.SeriesDescription).endswith(' Rec 3')]
        self.assertEqual(len(latest), 6)
        self.assertEqual(self.radius(latest), 8.2)
        self.assertEqual(self.state['done'][self.key]['revision'], 3)

    def test_pending_uses_frozen_config_before_first_payload(self):
        with patch.object(P.revisions, 'publish_payload', side_effect=OSError('disk full')):
            self.run_round()
        self.due()
        self.lenses = [lens('new-name')]
        self.cfg['report'] = {'study_description': 'changed'}
        self.run_round()
        self.assertEqual(len(self.calls[-1]), 6)
        self.assertIn('IOL Repo DCB00', [str(ds.SeriesDescription) for ds in self.calls[-1]])

    def test_limit_does_not_strand_unstarted_work(self):
        other = copy.deepcopy(self.rows[0])
        other['guid'] = 'synthetic-guid-2'
        self.rows.append(other)
        self.args.limit = 1
        self.assertGreater(self.run_round()['next_ready'], 0)
        self.key = P._row_key(other)
        self.assertEqual(self.run_round()['ok'], 1)
        self.assertEqual(len(self.state['done']), 2)

    def test_unexpected_cstore_uid_does_not_commit(self):
        with patch.object(P.dicom, 'push_cstore', return_value=[('2.25.999', True, '0x0000', True)]):
            self.assertEqual(self.run_round()['fail'], 1)
        self.assertFalse(self.state['done'])
        self.assertIn(self.key, self.state['pending'])


if __name__ == '__main__':
    unittest.main()
