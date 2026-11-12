# -*- coding: utf-8 -*-
"""Ophthalmic Axial Measurements Storage（SOP 1.2.840.10008.5.1.4.1.1.78.7）

替代之前的 Encapsulated Document XML —— 用 DICOM 标准 IOD 承载生物测量原始值，
不再自造 XML 格式。下游任何支持该 SOP Class 的 PACS / 工作站都能直接读。

模块依据（DICOM PS3.3 2026d，逐条对照实现）
------------------------------------------
C.8.25.13  Ophthalmic Axial Measurements Series Module
    Modality = OAM                                    (0008,0060) Type 1
C.8.25.14  Ophthalmic Axial Measurements Module
    Ophthalmic Axial Measurements Device Type         (0022,1009) Type 1  OPTICAL
    Ophthalmic Axial Measurements {Right,Left} Eye Sequence
                                                      (0022,1007/1008) Type 1C
    └ Per-eye Macro（C.8.25.14.1，Table C.8.25.14-2）：
        Lens Status Code Sequence                     (0022,1024) Type 1
        Vitreous Status Code Sequence                 (0022,1025) Type 1
        Ophthalmic Axial Length Measurements Sequence  (0022,1050) Type 1
        └ Ophthalmic Axial Length Measurements Type    (0022,1010) Type 1 TOTAL LENGTH
        └ Ophthalmic Axial Length Measurements Total Length Sequence
                                                      (0022,1210) Type 1C
            Ophthalmic Axial Length                   (0022,1019) Type 1
            Ophthalmic Axial Length Measurement Modified (0022,1140) Type 1
        └ 相关设备信息（C.8.25.14.3）：
            Optical Ophthalmic Axial Length Measurements Sequence
                                                      (0022,1225) Type 1C
            └ Ophthalmic Axial Length Data Source Code Sequence (0022,1150) Type 1
    └ Selected Macro（C.8.25.14.4，Table C.8.25.14-5）：
        Optical Selected Ophthalmic Axial Length Sequence  (0022,1255) Type 1C
        └ Selected Total Ophthalmic Axial Length Sequence  (0022,1260) Type 1C
            Ophthalmic Axial Length                   (0022,1019) Type 1

⚠️ 本模块**不写**的信号
---------------------
* Keratometry：K 值不属于 Ophthalmic Axial Measurements IOD 的必填内容。
  角膜曲率有自己的 SOP Class（Keratometry Measurements Storage, 78.3），
  硬塞进 OAM 会变成非法属性。本项目的 K 值只出现在晶体单图与计算结果里。
* 晶体度数：那是 Intraocular Lens Calculations Storage（78.8）的内容。
  IOL 计算结果由晶体单图承载，不进 OAM。

⚠️ 适用范围
-----------
仅对 Carl Zeiss IOLMaster 500 实测验证过（详见 DISCLAIMER.md）。
## 眼状态怎么处理

**原样封装，不做翻译。** MDB 的 `state_r` / `state_l` 是操作员录入的中文自由
文本，直接写进 Lens Status / Vitreous Status Code Sequence：

    CodingSchemeDesignator = 99IOL2DCM   （DICOM PS3.3 §8.2 规定的本地方案）
    CodeValue              = 原文（SH 16 字符上限，超长截断）
    CodeMeaning            = 原文全文

为什么不映射成 SNOMED：映射必然要猜，而猜错比不猜更糟 ——

  · 「硅油充填眼」只说了玻璃体，没说晶状体是天然还是人工
  · 「人工晶体」漏了「状」字，和「人工晶状体」是不是同一件事要靠猜
  · 白名单方案下，任何没见过的写法都可能让整例失败

原文本身就是事实，直接落库最省事也最准确。下游要用 SNOMED 就自己按
CodeMeaning 去做，不必在本项目里预置一套可能过时或错误的映射。

"""
from __future__ import annotations

from core.patient import norm_date, norm_sex
from core.utils import uid

SOP_OAM = "1.2.840.10008.5.1.4.1.1.78.7"

SERIES_DESC = "Ophthalmic Axial Measurements"

# 本实例**内容**的版本。改动写入的属性时必须提升 ——
# UID 种子含它，不提升的话 PACS 按 SOP Instance UID 去重，
# 新属性根本落不了库。
#   1.0  仅 AL
#   1.1  增加前房深度段测量（SEGMENTAL LENGTH + SCT 31636006）
#   1.2  缺 AL 的眼不再整例跳过（以前 al/r1/r2 缺一项就丢整眼，导致
#        「有 AL 无 K」的检查连眼轴都录不进去）；同一实例现在可能包含
#        与旧版不同的一只眼，UID 必须变，否则 PACS 静默去重。
LAYOUT_VERSION = "1.2"


# ---------------------------------------------------------------- 编码

# 本地编码方案。DICOM PS3.3 §8.2：以 "99" 开头的 Coding Scheme Designator
# 表示本地/私有编码方案（HL7 v2 约定），不需要注册。
LOCAL_SCHEME = "99IOL2DCM"

# 判断原文是否提到玻璃体腔。纯关键词匹配，只决定原文该进哪个字段，
# 不做语义翻译。MDB 的 state 一列混合记录晶状体与玻璃体状态，
# 没提玻璃体的（如"有晶状体"）就不往 Vitreous 字段里塞。
VITREOUS_WORDS = ("硅油", "气体", "玻切", "玻璃体", "silicone", "gas", "vitrectomy")


def code(value, scheme, meaning):
    """Code Sequence 宏（Table 8.8-1）的最小实现。

    pydicom 只校验 VR 与长度，语义由调用方保证：
    CodeValue / CodingSchemeDesignator / CodeMeaning 三者缺一不可。
    """
    from pydicom.dataset import Dataset

    d = Dataset()
    d.CodeValue = value
    d.CodingSchemeDesignator = scheme
    d.CodeMeaning = meaning
    return d


def truncate_sh(text, limit=16):
    """把字符串截到 SH 的字节上限内，且不切碎多字节字符。

    SH 的上限是 16 **字节**（DICOM PS3.5 Table 6.2-1），中文 UTF-8 下
    3 字节/字，直接 len() 按字符截会超出。这里逐字符累加字节数，放不下
    就停 —— 宁可少一个字，也不写出半个汉字（那会让整个数据集解码失败）。
    """
    out = []
    used = 0
    for ch in text:
        n = len(ch.encode("utf-8"))
        if used + n > limit:
            break
        out.append(ch)
        used += n
    return "".join(out)


def state_code(raw_text):
    """MDB 的状态原文 → Code Sequence Item，**原样封装，不做翻译**。

    设计决定：不把中文自由文本映射成 SNOMED 概念。
    理由：映射必然要猜（「硅油充填眼」没说晶状体状态，「人工晶体」少个
    状字），猜错比不猜更糟；而且 MDB 里就这一列原文，它本身就是事实。

    编码方式：
      CodingSchemeDesignator = 99IOL2DCM   本地方案
      CodeValue              = 原文（SH 上限 16 **字节**，超长按字节截断）
      CodeMeaning            = 原文全文

    ★ SH 的 16 是**字节**上限，不是字符数。中文在 UTF-8 下 3 字节/字，
      所以最多只放得下 5 个汉字，较长的状态组合会超限
      （"硅油充填眼，人工晶状体" = 11 字 33 字节）。按字符截断会写出
      34 字节的 SH 元素，pydicom 不拦、不警告，直接落库成非法 DICOM。

      truncate_sh() 按字节截，且不把一个汉字劈成半个；CodeMeaning 是 LO
      （64 字节），原文全文照旧完整保留，信息不丢。
    """
    txt = str(raw_text or "").strip()
    if not txt:
        return None
    return code(truncate_sh(txt), LOCAL_SCHEME, txt)


def _eye_item(rec, side, state_text):
    """单眼的 Ophthalmic Axial Measurements Macro + Selected Macro。"""
    from pydicom.dataset import Dataset
    from pydicom.sequence import Sequence

    e = rec["eyes"][side]
    c = state_code(state_text)

    # ★ 缺 AL 的这眼写不出合法的 OAM per-eye item：
    #   Ophthalmic Axial Length Measurements Sequence (0022,1050) 是 Type 1，
    #   里面必须有 TOTAL LENGTH 的实测值。没测眼轴就是没有，不能填 0
    #   （0 mm 会被下游当成一个真实的荒谬测量值）。
    #   所以整眼跳过，由 build 决定这个实例还发不发。
    al = e.get("al")
    if not al:
        return None

    item = Dataset()

    # ---- Lens / Vitreous status（Type 1）----
    # ★ MDB 只有一列 state，原文写进两个序列，但不做任何翻译/映射。
    #   两个序列都是 Type 1（必填），所以：
    #     有原文 → 写原文
    #     没原文 → 写 "None"（表示设备未录入，不是推断出的状态）
    #
    #   原文里没提玻璃体的（如"有晶状体"），Vitreous 也写 "None" 而不是
    #   塞一个"有晶状体"进去 —— 那是晶状体状态，放进玻璃体字段会是错的信息。
    #   判据只是"原文里有没有玻璃体相关词"，属于字段归属，不是语义翻译。
    none_item = code("None", LOCAL_SCHEME, "None")
    item.LensStatusCodeSequence = Sequence([c if c is not None else none_item])

    raw = str(state_text or "").strip()
    mentions_vitreous = any(k in raw for k in VITREOUS_WORDS)
    if c is not None and mentions_vitreous:
        item.VitreousStatusCodeSequence = Sequence([c])
    else:
        item.VitreousStatusCodeSequence = Sequence([none_item])

    # ---- 实际测量值（Type 1 序列）----
    # Type 用的是 TOTAL LENGTH：IOLMaster 是光学生物测量，一次测量得到全长，
    # 不是分段求和（那是超声的 LENGTH SUMMATION）。
    al_item = Dataset()
    al_item.OphthalmicAxialLengthMeasurementsType = "TOTAL LENGTH"

    total = Dataset()
    total.OphthalmicAxialLength = round(float(al), 2)
    # 我们没让人工改过峰值，设备输出原样入库
    total.OphthalmicAxialLengthMeasurementModified = "NO"
    al_item.OphthalmicAxialLengthMeasurementsTotalLengthSequence = Sequence([total])

    # ---- 设备来源信息（C.8.25.14.3，OPTICAL 时 Type 1C）----
    # (0022,1150) 取 CID 4240 的 111780 = "Measurement From This Device"。
    # ★ 不能取 111782 "Axial Measurements SOP Instance" —— 那个编码表示
    #   「引用另一个 OAM SOP 实例」，会强制要求配 Referenced SOP Sequence
    #   指向真实的 SOP Instance UID。数据来源是设备活库，没有这个引用。
    src = Dataset()
    src.OphthalmicAxialLengthDataSourceCodeSequence = Sequence([
        code("111780", "DCM", "Measurement From This Device")
    ])
    # (0022,1159) Ophthalmic Axial Length Data Source Description（Type 3）
    src.OphthalmicAxialLengthDataSourceDescription = "IOLMaster 500, IolmXP.MDB"
    al_item.OpticalOphthalmicAxialLengthMeasurementsSequence = Sequence([src])

    # ★ 前房深度走「段测量」。
    #   C.8.25.14.1.1.4 明确：光学生物测量仪也可以单独测某一段，例如
    #   "the Anterior Chamber depth"。做法是在同一个 Measurements Sequence
    #   里再放一个 item，Type = SEGMENTAL LENGTH，并用
    #   Segment Name Code Sequence (0022,1101) 标出是前房（CID 4233）。
    #
    #   ⚠️ 不要用 AnteriorChamberDepth (0022,1131) —— 那个属性的归属是
    #      Intraocular Lens Calculations IOD（C.8.25.16.2 的
    #      AnteriorChamberDepthSequence 0022,1128），不在本 IOD 里。
    measurements = [al_item]
    acd = e.get("acd")
    if acd:
        seg = Dataset()
        seg.OphthalmicAxialLengthMeasurementsType = "SEGMENTAL LENGTH"
        seg_item = Dataset()
        seg_item.OphthalmicAxialLength = round(float(acd), 2)
        seg_item.OphthalmicAxialLengthMeasurementModified = "NO"
        # CID 4233「Ophthalmic Axial Length Measurements Segment Name」里的
        # 前房条目：SCT 31636006 = Anterior Chamber
        seg_item.OphthalmicAxialLengthMeasurementsSegmentNameCodeSequence = \
            Sequence([code("31636006", "SCT", "Anterior Chamber")])
        seg.OphthalmicAxialLengthMeasurementsSegmentalLengthSequence = \
            Sequence([seg_item])
        measurements.append(seg)

    item.OphthalmicAxialLengthMeasurementsSequence = Sequence(measurements)

    # ---- Selected Macro（C.8.25.14.4）：IOL 计算实际采用的那一次测量 ----
    sel = Dataset()
    sel.OphthalmicAxialLengthMeasurementsType = "TOTAL LENGTH"
    sel_total = Dataset()
    sel_total.OphthalmicAxialLength = round(float(al), 2)
    sel.SelectedTotalOphthalmicAxialLengthSequence = Sequence([sel_total])
    item.OpticalSelectedOphthalmicAxialLengthSequence = Sequence([sel])

    return item


def build(rec, cfg, patient_id, study_uid, series_uid, sop_uid,
          series_num=1):
    """构造 Ophthalmic Axial Measurements Storage 实例。

    参数
    ----
    rec       管线记录：name/dob/date/eyes{OD,OS:{al,r1,r2,k1,k2,acd,axis}}
    cfg       配置（report 段取机构名与设备信息）
    patient_id / study_uid / series_uid / sop_uid  由调用方按幂等规则算好

    返回 pydicom FileDataset（未设置传输语法，由 dicom.push 决定）；
    两眼都没有眼轴时返回 None（没有任何 Measurements 内容可写）。
    """
    import pydicom
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.sequence import Sequence

    # ★ 先判「这实例还发不发得出去」，再构造 —— 免得白白拼半个 dataset。
    #   两眼都没 AL = Ophthalmic Axial Length Measurements Sequence 一个
    #   item 都写不出来 → 整个实例没有内容，不如不发。
    if not any(s in rec["eyes"] and rec["eyes"][s].get("al")
               for s in ("OD", "OS")):
        return None

    ds = FileDataset(None, {}, file_meta=FileMetaDataset(), preamble=b"\0" * 128)
    rep = cfg.get("report") or {}

    fm = ds.file_meta
    fm.MediaStorageSOPClassUID = SOP_OAM
    fm.MediaStorageSOPInstanceUID = sop_uid
    fm.ImplementationClassUID = uid("impl", "iol2dcm")
    # ★ 无像素对象的实例也必须声明传输语法，否则 pynetdicom 推送时
    #   push_cstore 里 _fix_encoding_flags 取 file_meta.TransferSyntaxUID
    #   会抛 AttributeError。OAM 没有像素，用 Explicit VR LE。
    fm.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian

    ds.SOPClassUID = SOP_OAM
    ds.SOPInstanceUID = sop_uid
    ds.is_implicit_VR = False
    ds.is_little_endian = True

    # ---- 患者 / 检查（患者模块 + 一般检查模块）----
    from core.utils import dicom_patient_name

    # pipeline._record 只填了 rec["name"]（中文全名），没有拆 family/given。
    # 直接把它当 given 传，dicom_patient_name 会原样返回。
    ds.PatientName = dicom_patient_name(rec.get("family"),
                                        rec.get("given") or rec.get("name"))
    ds.PatientID = patient_id
    # ★ DA 只接受 YYYYMMDD。MDB 的 birth_date 取出来是 "1970-01-01"
    #   （sources/mdb.py 按前 10 字符切），直接赋给 DA 会写出 10 字节的
    #   非法值 —— pydicom 只发一句 UserWarning，照样落库。
    #   norm_date() 本来就写了，只是没在这里调用。
    ds.PatientBirthDate = norm_date(rec.get("dob"))
    ds.PatientSex = norm_sex(rec.get("sex"))
    ds.SpecificCharacterSet = "ISO_IR 192"

    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.StudyID = rec.get("date") or ""
    ds.StudyDate = rec.get("date") or ""
    ds.StudyTime = ""
    ds.StudyDescription = rep.get("study_description", "")
    ds.SeriesDescription = SERIES_DESC
    ds.SeriesNumber = series_num
    ds.InstanceNumber = 1

    # ---- Ophthalmic Axial Measurements Series Module（C.8.25.13）----
    ds.Modality = "OAM"

    ds.BodyPartExamined = "EYE"
    ds.InstitutionName = rep.get("institution", "")
    ds.InstitutionalDepartmentName = rep.get("department", "")
    ds.Manufacturer = rep.get("manufacturer", "")
    ds.ManufacturerModelName = rep.get("model", "")
    ds.DeviceSerialNumber = rep.get("device_serial", "")
    ds.StationName = rep.get("station_name", "")
    ds.SoftwareVersions = rep.get("software_version", "")
    ds.OperatorsName = rec.get("operator") or ""

    # ---- Ophthalmic Axial Measurements Module（C.8.25.14）----
    # 光学生物测量仪
    ds.OphthalmicAxialMeasurementsDeviceType = "OPTICAL"

    # Lens Status 与测量值按眼分组。MDB 里 state 存在 state_r / state_l。
    for side, attr, state_key in (
        ("OD", "OphthalmicAxialMeasurementsRightEyeSequence", "state_r"),
        ("OS", "OphthalmicAxialMeasurementsLeftEyeSequence", "state_l"),
    ):
        if side not in rec["eyes"]:
            continue
        # state_r / state_l 由 pipeline._record 写在 rec 顶层（那一列的原文
        # 是整眼一份，不放在 eyes 里）。退一步从眼对象里取，兼容旧调用方。
        st = rec.get(state_key)
        if st is None:
            st = (rec["eyes"].get(side) or {}).get("state")
        item = _eye_item(rec, side, st)
        # ★ 缺 AL 的眼 _eye_item 返回 None：该眼没有可写的 Measurements，
        #   直接跳过 —— 不能把 None 塞进 Sequence（pydicom 会抛 TypeError）。
        if item is None:
            continue
        setattr(ds, attr, Sequence([item]))

    return ds


def series_uid_for(study_uid):
    """OAM 自成一个 series，与晶体单、角膜曲率并列在同一 study 下。

    ★ 种子里带 LAYOUT_VERSION：本实例的内容一旦变（例如 1.1.0 加了
      前房深度段测量），UID 必须跟着变。否则 PACS 会把新实例当成
      已存在的同一个实例静默去重，新加的属性永远进不去库。
    """
    from .dicom import PRIVATE_CREATOR
    return uid("iol2dcm-oam-series", study_uid, PRIVATE_CREATOR,
               LAYOUT_VERSION)


def sop_uid_for(study_uid):
    """OAM 实例 UID。同样带 LAYOUT_VERSION，理由见 series_uid_for。"""
    from .dicom import PRIVATE_CREATOR
    return uid("iol2dcm-oam", study_uid, PRIVATE_CREATOR, LAYOUT_VERSION)
