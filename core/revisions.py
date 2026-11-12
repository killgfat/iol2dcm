"""Immutable C-STORE payloads for numbered measurement rounds. No PACS REST."""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tempfile
from pathlib import Path

from core.utils import uid


def number(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("invalid measurement revision")
    return value


def apply_round(datasets, revision):
    """Round 1 preserves historical UIDs; Rec N gets distinct UIDs."""
    number(revision)
    if revision == 1:
        return
    suffix = f" Rec {revision}"
    for ds in datasets:
        ds.SeriesInstanceUID = uid("iol2dcm-rec-series", ds.SeriesInstanceUID, revision)
        ds.SOPInstanceUID = uid("iol2dcm-rec-instance", ds.SOPInstanceUID, revision)
        ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
        ds.SeriesDescription = str(ds.SeriesDescription)[:64 - len(suffix)] + suffix


def payload_path(state_path, key, revision):
    token = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return str(Path(state_path + ".spool") / token / str(number(revision)))


def _sync_dir(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_payload(path):
    import pydicom
    root = Path(path)
    with (root / "manifest.json").open(encoding="utf-8") as f:
        manifest = json.load(f)
    datasets = []
    files = manifest['files']
    if len({entry['name'] for entry in files}) != len(files):
        raise ValueError('duplicate spool filename')
    for entry in files:
        name = entry["name"]
        if Path(name).name != name:
            raise ValueError("invalid spool filename")
        data = (root / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ValueError("spool checksum mismatch; refusing to send")
        ds = pydicom.dcmread(io.BytesIO(data))
        if str(ds.SOPInstanceUID) != entry["sop"]:
            raise ValueError("spool SOP UID mismatch")
        if (str(ds.file_meta.MediaStorageSOPInstanceUID) != entry['sop'] or
                ds.file_meta.MediaStorageSOPClassUID != ds.SOPClassUID):
            raise ValueError('spool file meta mismatch')
        datasets.append(ds)
    info = manifest["info"]
    if not datasets or len(datasets) != info["instances"]:
        raise ValueError("incomplete spool")
    if len({str(ds.SOPInstanceUID) for ds in datasets}) != len(datasets):
        raise ValueError("duplicate SOP UID in spool")
    if len({str(ds.SeriesInstanceUID) for ds in datasets}) != len(datasets):
        raise ValueError('duplicate series UID in spool')
    number(info['revision'])
    for ds in datasets:
        if (str(ds.StudyInstanceUID) != info['study_uid'] or
                str(ds.PatientID) != info['pid'] or str(ds.StudyDate) != info['date']):
            raise ValueError('spool patient/study mismatch')
    info['sop_uids'] = sorted(str(ds.SOPInstanceUID) for ds in datasets)
    info['payload_sha256'] = {entry['sop']: entry['sha256'] for entry in files}
    return datasets, info


def validate_journal(info, rec, fp, seed):
    """Bind a validated payload to its durable source and original study."""
    from core import patient
    from dicomlib import dicom
    pid = patient.make_pid(rec.get('dob'), rec['name'])
    expected = {'fp': fp, 'revision': number(rec['_revision']), 'pid': pid,
                'date': rec['date'], 'uid_seed': seed,
                'study_uid': dicom.study_uid_for(pid, rec['date'], seed)}
    if any(info.get(k) != v for k, v in expected.items()):
        raise ValueError('spool journal identity/fingerprint mismatch')


def publish_payload(path, datasets, info):
    """Publish a complete, fsynced spool atomically before first C-STORE."""
    root = Path(path)
    if root.exists():
        return read_payload(path)
    root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root.parent.parent, 0o700)
    os.chmod(root.parent, 0o700)
    tmp = Path(tempfile.mkdtemp(prefix=".building-", dir=root.parent))
    try:
        entries = []
        for i, ds in enumerate(datasets):
            name = f"{i}.dcm"
            fd = os.open(str(tmp / name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                ds.save_as(f)
                f.flush()
                os.fsync(f.fileno())
            entries.append({"name": name, "sop": str(ds.SOPInstanceUID),
                            "sha256": hashlib.sha256((tmp / name).read_bytes()).hexdigest()})
        fd = os.open(str(tmp / "manifest.json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"info": info, "files": entries}, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        _sync_dir(tmp)
        os.rename(tmp, root)
        _sync_dir(root.parent)
        _sync_dir(root.parent.parent)
        _sync_dir(root.parent.parent.parent)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)
    return read_payload(path)
