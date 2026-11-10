# -*- coding: utf-8 -*-
"""Keratometry Measurements Storage（SOP 1.2.840.10008.5.1.4.1.1.78.3）

角膜曲率单独成一个 series。K 值、曲率半径、散光轴位**只能**放这里 ——
Ophthalmic Axial Measurements IOD 里没有这些属性，塞进去就是非法实例。

模块依据（DICOM PS3.3 2026d）
-----------------------------
C.8.25.10  Keratometry Measurements Module
    Keratometry Right Eye Sequence                    (0046,0070) Type 1C
    Keratometry Left Eye Sequence                     (0046,0071) Type 1C
    └ Include Table C.8.25.10-2 Keratometric Measurements Macro：
        Steep Keratometric Axis Sequence              (0046,0074) Type 1
        └ Radius of Curvature                         (0046,0075) Type 1  mm
        └ Keratometric Power                          (0046,0076) Type 1  D
        └ Keratometric Axis                           (0046,0077) Type 1  度
        Flat Keratometric Axis Sequence               (0046,0080) Type 1
        └ 同上三个属性

陡/平怎么判定
-------------
标准对两条子午线有明确语义：
    Steep = greatest power of curvature / shortest radius of curvature
    Flat  = least power of curvature  / longest radius of curvature

所以**按半径大小判**，不假定 r1 恒为平、r2 恒为陡：

    r1 < r2  →  r1 是陡（K1 更大），r2 是平
    r1 > r2  →  r2 是陡，r1 是平
    r1 == r2 →  球面角膜，两条都填同一个值（标准 Note 明确允许：
                "values specified in the steep and flat Attributes are equivalent"）

轴位归属
--------
MDB 只存 axis_r / axis_l 一列，它是 **K1 的轴**（已用真实报告单核对）。
K2 的轴恒为 K1 轴 +90°（主子午线互相垂直）。

于是轴位要跟着半径一起走：r1 是陡 → axis 给 Steep；r2 是陡 → axis + 90
给 Steep，axis 给 Flat。

⚠️ 适用范围
-----------
仅对 Carl Zeiss IOLMaster 500 实测验证过（详见 DISCLAIMER.md）。
"""
from __future__ import annotations

from core.patient import norm_date, norm_sex
from core.utils import uid

SOP_KER = "1.2.840.10008.5.1.4.1.1.78.3"

SERIES_DESC = "Keratometry"

# 本实例**内容**的版本。改动写入的属性时必须提升 ——
# UID 种子含它，不提升的话 PACS 按 SOP Instance UID 去重，
# 改过的属性根本落不了库。
#   1.0  仅完整眼的 K
#   1.1  陡/平轴按半径判（r1<r2 时 K1 才是陡）
#   1.2  缺 k 不再整眼丢弃（以前 al/r1/r2 缺一项就丢整眼）；同一实例
#        现在可能包含与旧版不同的一只眼，UID 必须变。
LAYOUT_VERSION = "1.2"

# Ophthalmic Refractive Measurements 的 modality 是 OP
MODALITY = "OP"


def _perp(axis):
    """K1 轴 +90° = K2 轴。无轴位时返回 None。"""
    if axis is None or axis == "":
        return None
    try:
        return (float(axis) + 90.0) % 180.0
    except (TypeError, ValueError):
        return None


def _axis_or_zero(axis):
    """KeratometricAxis 是 Type 1，不能缺。

    走到这里说明 r1/r2 有值而 axis 缺 —— 属于极少数的数据质量问题。
    兜底填 0：0° 与 180° 等价，是散光轴的无信息占位，总比丢掉整条
    子午线（连半径和 K 一起丢）损失小。
    """
    if axis is None or axis == "":
        return 0.0
    return axis


def _axis_item(radius, power, axis):
    """一条子午线：Radius of Curvature / Keratometric Power / Keratometric Axis。

    三个都是 Type 1，缺一不可 —— 调用方保证只在本眼 r1/r2 都有效时进来。
    """
    from pydicom.dataset import Dataset

    it = Dataset()
    # FL（单精度浮点）。保留 2 位小数：设备本身只到 0.01
    it.RadiusOfCurvature = round(float(radius), 2)
    it.KeratometricPower = round(float(power), 2)
    it.KeratometricAxis = round(float(axis) % 180, 1)
    return it


def _eye_item(k1, k2, r1, r2, axis):
    """单眼的 Keratometric Measurements Macro。

    参数
    ----
    k1/k2  角膜屈光力 D（k1 对应 r1，k2 对应 r2）
    r1/r2  曲率半径 mm
    axis   MDB 的轴位，是 **r1 那条子午线**的轴，单位度

    返回 None 表示本眼缺 r1/r2，整眼不进序列（Type 1C，没测就不写）。
    """
    from pydicom.dataset import Dataset
    from pydicom.sequence import Sequence

    if not r1 or not r2 or not k1 or not k2:
        return None

    # 按半径判陡平：半径小的那条更陡（K 更大）
    if r1 < r2:
        steep = (r1, k1, axis)
        flat = (r2, k2, _perp(axis))
    elif r2 < r1:
        steep = (r2, k2, _perp(axis))
        flat = (r1, k1, axis)
    else:
        # 球面角膜：两条等价，轴位填同一个（标准 Note 明确允许）
        steep = (r1, k1, axis)
        flat = (r2, k2, axis)

    item = Dataset()
    item.SteepKeratometricAxisSequence = Sequence([_axis_item(*steep)])
    item.FlatKeratometricAxisSequence = Sequence([_axis_item(*flat)])
    return item


def build(rec, cfg, patient_id, study_uid, series_uid, sop_uid,
          series_num=1):
    """构造 Keratometry Measurements Storage 实例。

    参数
    ----
    rec       管线记录：eyes{OD,OS:{al,r1,r2,k1,k2,acd,axis}}

    返回 pydicom FileDataset；两只眼都没测则返回 None。
    """
    import pydicom
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.sequence import Sequence

    from core.utils import dicom_patient_name

    od_item = None
    od = rec["eyes"].get("OD")
    if od:
        od_item = _eye_item(od.get("k1"), od.get("k2"),
                            od.get("r1"), od.get("r2"),
                            _axis_or_zero(od.get("axis")))

    os_item = None
    os_ = rec["eyes"].get("OS")
    if os_:
        os_item = _eye_item(os_.get("k1"), os_.get("k2"),
                            os_.get("r1"), os_.get("r2"),
                            _axis_or_zero(os_.get("axis")))

    if od_item is None and os_item is None:
        return None

    ds = FileDataset(None, {}, file_meta=FileMetaDataset(), preamble=b"\0" * 128)
    rep = cfg.get("report") or {}

    fm = ds.file_meta
    fm.MediaStorageSOPClassUID = SOP_KER
    fm.MediaStorageSOPInstanceUID = sop_uid
    fm.ImplementationClassUID = uid("impl", "iol2dcm")
    # 无像素对象也必须声明传输语法，否则 pynetdicom 推送时取不到
    fm.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian

    ds.SOPClassUID = SOP_KER
    ds.SOPInstanceUID = sop_uid
    ds.is_implicit_VR = False
    ds.is_little_endian = True

    ds.PatientName = dicom_patient_name(rec.get("family"),
                                        rec.get("given") or rec.get("name"))
    ds.PatientID = patient_id
    # DA 只接受 YYYYMMDD；MDB 取出来是 1970-01-01 形态
    ds.PatientBirthDate = norm_date(rec.get("dob"))
    ds.PatientSex = norm_sex(rec.get("sex"))
    ds.SpecificCharacterSet = "ISO_IR 192"

    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.StudyID = rec.get("date") or ""
    ds.StudyDate = rec.get("date") or ""
    ds.StudyDescription = rep.get("study_description", "")
    ds.SeriesDescription = SERIES_DESC
    ds.SeriesNumber = series_num
    ds.InstanceNumber = 1
    ds.Modality = MODALITY
    ds.BodyPartExamined = "EYE"

    ds.InstitutionName = rep.get("institution", "")
    ds.InstitutionalDepartmentName = rep.get("department", "")
    ds.Manufacturer = rep.get("manufacturer", "")
    ds.ManufacturerModelName = rep.get("model", "")
    ds.DeviceSerialNumber = rep.get("device_serial", "")
    ds.StationName = rep.get("station_name", "")
    ds.SoftwareVersions = rep.get("software_version", "")
    ds.OperatorsName = rec.get("operator") or ""

    if od_item is not None:
        ds.KeratometryRightEyeSequence = Sequence([od_item])
    if os_item is not None:
        ds.KeratometryLeftEyeSequence = Sequence([os_item])

    return ds


def series_uid_for(study_uid):
    """角膜曲率自成一个 series，与晶体单、OAM 并列。"""
    from .dicom import PRIVATE_CREATOR
    return uid("iol2dcm-ker-series", study_uid, PRIVATE_CREATOR,
               LAYOUT_VERSION)


def sop_uid_for(study_uid):
    """KER 实例 UID。种子里带 LAYOUT_VERSION，理由见 LAYOUT_VERSION 注释。"""
    from .dicom import PRIVATE_CREATOR
    return uid("iol2dcm-ker", study_uid, PRIVATE_CREATOR, LAYOUT_VERSION)
