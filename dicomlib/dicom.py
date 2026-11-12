# -*- coding: utf-8 -*-
"""DICOM 封装与 C-STORE 推送。

实例类型
--------
**SC**（Secondary Capture，SOP 1.2.840.10008.5.1.4.1.1.7）—— 晶体单图片。

生物测量原始值不走 Encapsulated Document，而是标准 IOD
Ophthalmic Axial Measurements Storage（见 oam.py）。

组织方式
--------
    一次检查 = 1 个 study
      ├─ series "IOL Repo <型号A>"       → SC，第 1 个实例
      ├─ series "IOL Repo <型号B>"       → SC，第 1 个实例
      ├─ ...
      └─ series "Ophthalmic Axial Measurements" → OAM，第 1 个实例

实现要点
--------
* PixelData 必须是 JPEG 封装，不能塞 PNG 字节；否则 Orthanc 照收 200 但
  /preview 返 415、取回解码报 "number of bytes of pixel data is less"。
* SpecificCharacterSet 必须 ISO_IR 192，否则中文姓名落库成 "?"。
* 确定性 UID（sha256 派生）重复推幂等；补测由 core.revisions 为 Rec N
  派生新的 Series/SOP UID，原 study 保持不变、历史轮次保留。
* PatientID 绝不可为空（空 ID = 所有患者并成一个）。
* 写入只走标准 C-STORE；管线不调用 REST，也不删除 PACS 数据。
"""
from __future__ import annotations

import io

from core.patient import norm_date, norm_sex
from core.utils import dicom_patient_name, uid

SOP_SC = "1.2.840.10008.5.1.4.1.1.7"

# 改版时提升版本号，UID 随之变化，避开 Orthanc 对同 SOP UID 的静默去重
PRIVATE_CREATOR = "IOL2DCM 1.0"

SERIES_PREFIX = "IOL Repo"


def _common(ds, rec, cfg, study_uid, series_uid, sop_uid, sop_class,
            series_desc, series_num, instance_num, patient_id):
    """填两个实例共用的标签。"""
    import pydicom
    from pydicom.dataset import FileMetaDataset

    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = sop_class
    fm.MediaStorageSOPInstanceUID = sop_uid
    fm.ImplementationClassUID = uid("impl", "iol2dcm")
    ds.file_meta = fm
    ds.SOPClassUID = sop_class
    ds.SOPInstanceUID = sop_uid
    ds.is_implicit_VR = False
    ds.is_little_endian = True

    rep = cfg.get("report") or {}
    ds.PatientName = dicom_patient_name(rec.get("family"),
                                        rec.get("given") or rec.get("name"))
    ds.PatientID = patient_id
    # ★ DA 只接受 YYYYMMDD。MDB 的 birth_date 取出来是 "1970-01-01"
    #   （sources/mdb.py 按前 10 字符切），直接赋给 DA 会写出 10 字节的
    #   非法值 —— pydicom 只发一句 UserWarning，照样落库。
    ds.PatientBirthDate = norm_date(rec.get("dob"))
    ds.PatientSex = norm_sex(rec.get("sex"))
    ds.SpecificCharacterSet = "ISO_IR 192"

    # ---- 检查信息 ----
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.StudyID = rec.get("date") or ""
    ds.StudyDate = rec.get("date") or ""
    ds.StudyDescription = rep.get("study_description", "IOLMaster 晶体单")
    ds.SeriesDescription = series_desc
    ds.SeriesNumber = series_num
    ds.InstanceNumber = instance_num
    ds.Modality = "OT"
    ds.BodyPartExamined = "EYE"

    # ---- 设备元数据（保留，PACS 端排查与溯源要用）----
    ds.InstitutionName = rep.get("institution", "")
    ds.InstitutionalDepartmentName = rep.get("department", "")
    ds.Manufacturer = rep.get("manufacturer", "")
    ds.ManufacturerModelName = rep.get("model", "")
    ds.DeviceSerialNumber = rep.get("device_serial", "")
    ds.StationName = rep.get("station_name", "")
    ds.SoftwareVersions = rep.get("software_version", "V. 5.5")
    if rec.get("operator"):
        ds.OperatorsName = rec["operator"]
    return ds


def study_uid_for(patient_id, exam_date, case=""):
    """同一患者多次检查落同一 study；不同患者绝不撞 UID。

    case
    ----
    「一行一个案例」之后，同一患者同一天可能有两轮独立测量。若两轮都用
    同一个 study_uid，SC/OAM/KER 的 SOP UID 也会完全相同（它们的种子里
    没有 guid），第二轮推上去会被 PACS 静默去重 —— 看起来成功，实际什么
    都没进库。

    所以第二轮起要换一个 study。case 就是那个区分因子（传该行的 guid）。

    ★ 默认 "" 时**完全等于旧公式**，一个字节都不差 —— 这是迁移的兼容锚点：
      已推过的病例 uid_seed=""，UID 与当年推的一致，重算也不会变，零重推。
      只有新案例才传 guid，拿到一个全新的 study。
    """
    if case:
        return uid("iol2dcm-study", patient_id, exam_date, case)
    return uid("iol2dcm-study", patient_id, exam_date)


def series_uid_for(study_uid, lens_name):
    return uid("iol2dcm-series", study_uid, PRIVATE_CREATOR, lens_name)


def build_sc(rec, image, cfg, patient_id, lens, series_num=1, instance_num=1,
             study_uid=None):
    """晶体单图片 → Secondary Capture 实例。

    image  可以是 **PIL Image**（推荐，见 report.render_image），
           也可以是 PNG 字节（向后兼容，内部会解码）。
    """
    import pydicom
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.encaps import encapsulate
    from PIL import Image

    # ★ study_uid 可以由调用方给（「一行一个案例」要按 uid_seed 派生）。
    #   不给时保持旧行为，向后兼容。
    if study_uid is None:
        study_uid = study_uid_for(patient_id, rec.get("date"))
    series_uid = series_uid_for(study_uid, lens["name"])
    sop_uid = uid("iol2dcm-sc", study_uid, lens["name"], PRIVATE_CREATOR)

    if isinstance(image, (bytes, bytearray)):
        # 向后兼容：传的是 PNG 字节。多一次解码（约 34ms/张），
        # 新代码请直接传 Image 走上面那条路。
        im = Image.open(io.BytesIO(image)).convert("RGB")
    else:
        im = image if image.mode == "RGB" else image.convert("RGB")

    buf = io.BytesIO()
    # ★ optimize=False：实测快 39%（10.5→6.4ms/张），体积 +21.7%
    #   （217→264KB）。optimize=True 要多趟重算 Huffman 表，且**不释放 GIL**，
    #   在并发场景下尤其不划算。JPEG q92 已经远高于报告单可读需求。
    im.save(buf, format="JPEG", quality=92, optimize=False)
    jpg = buf.getvalue()

    ds = FileDataset(None, {}, file_meta=FileMetaDataset(), preamble=b"\0" * 128)
    _common(ds, {"family": "", "given": rec.get("name"), **rec}, cfg,
            study_uid, series_uid, sop_uid, SOP_SC,
            f"{SERIES_PREFIX} {lens['name']}", series_num, instance_num, patient_id)

    ds.file_meta.TransferSyntaxUID = pydicom.uid.JPEGBaseline8Bit
    ds.PhotometricInterpretation = "YBR_FULL_422"
    ds.Rows, ds.Columns = im.height, im.width
    ds.SamplesPerPixel = 3
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.PlanarConfiguration = 0
    ds.PixelData = encapsulate([jpg])
    ds["PixelData"].VR = "OB"
    ds["PixelData"].is_undefined_length = True

    # ★ 不写任何眼部测量参数（AL / ACD / K1 / K2 / Cyl / 轴位）。
    #   这些数值只出现在晶体单图上，标签区不再重复。
    #   理由：
    #     · 同一份数据在标签区和图里各存一遍，两处可能不一致，
    #       PACS 端根本分不清哪个是权威；
    #     · 生物测量原始值的正规归宿是 Ophthalmic Axial Measurements
    #       Storage 实例（见 oam.py），SC 图只是给人看的报告单；
    #     · 标签区塞一堆数值只会让标签列表变脏，且 Orthanc 无私有字典时
    #       部分标签显示成 Unknown。
    return ds


# ---------------------------------------------------------------- 推送

def _has_frames(ds):
    return "PixelData" in ds and int(getattr(ds, "Rows", 0) or 0) > 0


def _fix_encoding_flags(ds):
    """让 pynetdicom 相信这份 dataset 就是按 file_meta 的传输语法编码的。

    ⚠️ 坑：pydicom 用 `FileDataset(None, {}, ...)` 构造的内存 dataset，
    `original_encoding` 恒为 (True, True)（即 Implicit VR LE）。
    pynetdicom 3.x 拿这个值和 (0002,0010) 比对，发现对不上就强行改成
    ImplicitVRLittleEndian，然后去要一个没协商过的 presentation context，
    报 "No presentation context ... has been accepted by the peer"。
    像素明明是 JPEG，声明却是 Implicit VR —— 只能对上真实传输语法。
    """
    ts = ds.file_meta.TransferSyntaxUID
    if ts is None:
        return ds
    ds._read_implicit = bool(ts.is_implicit_VR)
    ds._read_little = bool(ts.is_little_endian)
    ds.is_implicit_VR = bool(ts.is_implicit_VR)
    ds.is_little_endian = bool(ts.is_little_endian)
    return ds


def _to_native_le(ds):
    """把封装 JPEG 的 SC 解成裸像素 + Explicit VR LE，供不支持 JPEG 的端使用。

    ⚠️ 必须**深拷贝 file_meta**。pydicom 的 `ds.copy()` 是浅拷贝，
    `out.file_meta` 与原 ds 指向同一个对象 —— 于是改 out 的传输语法会
    把**原 dataset** 也改掉，导致第一轮拿着已改成 Explicit VR 的 dataset
    去要 JPEG 上下文，报 "No presentation context ... accepted"。
    """
    import numpy as np
    from pydicom.dataset import FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian

    out = ds.copy()
    fm = FileMetaDataset()
    for e in ds.file_meta:
        fm.add(e)
    out.file_meta = fm
    arr = ds.pixel_array
    if arr.ndim == 2:
        arr = np.stack([arr] * 3, axis=-1)
    out.PixelData = arr.tobytes()
    out.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    out.is_implicit_VR = False
    out.is_little_endian = True
    out.SamplesPerPixel = 3
    out.PhotometricInterpretation = "RGB"
    out.PlanarConfiguration = 0
    out.BitsAllocated = 8
    out.BitsStored = 8
    out.HighBit = 7
    out.Rows, out.Columns = arr.shape[0], arr.shape[1]
    if "PixelData" in out:
        out["PixelData"].VR = "OB"
        out["PixelData"].is_undefined_length = False
    return out


def push_cstore(datasets, host, port, aet, timeout=60, prefer_jpeg=True):
    """标准 DICOM C-STORE。DICOM 协议无认证。

    传输语法选择
    -----------
    报告单是 A4 版式文字图，JPEG Baseline 就够：
      · JPEG Baseline(1.2.840.10008.1.2.4.50)：约 640 KB/张
      · Explicit VR LE（裸 RGB）            ：约 11.6 MB/张   ← 18 倍
    实测 Orthanc SCP 接受 JPEG Baseline 协商，所以优先走 JPEG。

    返回 [(SOPInstanceUID, ok, msg, known), ...]

    ★ 第一个元素是**实例** UID（不是 SOP Class UID）：调用方要靠它记录
      「哪些实例已经被对方收下了」，用于判断内容变更后是否需要人工处理。
      以前返回 SOPClassUID，同一次检查的多张 SC 会得到同一个值，无法
      定位到具体实例。

    ★ known 表示**这次的结果是否确定**：
        True  = 明确知道收没收到（0x0000 成功 / 明确的失败状态码）
        False = 结果未知（关联中断拿到空响应、无应答）
      「未知」绝不能被当成「没收到」—— PACS 可能已经存下了。调用方
      必须按「可能已接收」保守处理。
    """
    from pynetdicom import AE
    from pydicom.uid import (ExplicitVRLittleEndian, ImplicitVRLittleEndian,
                             JPEGBaseline8Bit)

    results = []
    for ds in datasets:
        has_px = _has_frames(ds)

        # ★ 候选必须**惰性构建**。_to_native_le(ds) 会解码像素并改写传输语法，
        #   在循环外先算出来，原 ds 的 file_meta 就已被污染。
        def _candidates():
            if not has_px:
                yield ds, [ExplicitVRLittleEndian, ImplicitVRLittleEndian]
                return
            if prefer_jpeg:
                yield ds, [JPEGBaseline8Bit, ExplicitVRLittleEndian,
                           ImplicitVRLittleEndian]
                yield _to_native_le(ds), [ExplicitVRLittleEndian,
                                          ImplicitVRLittleEndian]
            else:
                yield _to_native_le(ds), [ExplicitVRLittleEndian,
                                          ImplicitVRLittleEndian]

        ok, msg = False, "no attempt"
        # ★ known 是**整个实例**的结论，不能只留最后一次尝试的状态。
        #   同一实例可能有多次尝试（JPEG → 裸像素回退）。第一次空响应
        #   （结果未知，PACS 可能已存下）之后，第二次关联被拒**不能**把
        #   "未知"洗成"确定没发出去"—— 那样内容一变就会普通重试，
        #   同 UID 被静默去重。所以这里用「一旦未知就永远未知」的累积。
        unknown = False
        for send, tss in _candidates():
            _fix_encoding_flags(send)
            # 兜底那轮可能已退成裸像素（TS 变了），请求上下文要按**实际**的报
            ts_now = str(send.file_meta.TransferSyntaxUID)
            req_tss = list(tss)
            if ts_now not in req_tss:
                req_tss = [ts_now] + req_tss
            ae = AE(ae_title="IOL2DCM")
            ae.connection_timeout = timeout
            ae.acse_timeout = timeout
            ae.network_timeout = timeout
            ae.dimse_timeout = timeout
            ae.add_requested_context(send.SOPClassUID, req_tss)
            try:
                assoc = ae.associate(host, port, ae_title=aet)
            except Exception as e:
                msg = f"association error: {e}"
                continue
            if not assoc.is_established:
                # 这一次确实没发出去（但之前的尝试可能已经发出去了，
                # 所以不能重置 unknown）
                msg = "association rejected"
                continue
            try:
                st = assoc.send_c_store(send)
                # ⚠️ pynetdicom 3.x 在关联中断时可能返回「空 Dataset」而不是
                #    None —— 直接取 st.Status 会抛 AttributeError。
                code = getattr(st, "Status", None) if st is not None else None
                if isinstance(code, int):
                    msg = f"0x{code:04X}"
                    # ★ 0xB000~0xBFFF 是 Warning：SCP **已经存下**了，只是
                    #   做了属性修正。不能当成"没收到"——否则内容变更后会
                    #   重推，同 UID 被去重，账目与 PACS 对不上。
                    ok = code == 0x0000 or (0xB000 <= code <= 0xBFFF)
                else:
                    # 没有状态码 = 关联中途断了。请求可能已经到达对方并被
                    # 存下，只是响应没回来 —— 这是「未知」，不是「失败」。
                    msg = "no response"
                    ok = False
                    unknown = True
            except ValueError as e:
                # ★ 协商成功但**没接受我们要的传输语法**时，pynetdicom 在
                #   send_c_store 里抛 ValueError（"No presentation context
                #   ... accepted by the peer with '<TS>' ..."），不是返回失败。
                #   以前没捕获，异常直接冒到调用方，整个 study 的推送中断，
                #   下面的 JPEG → 裸像素回退永远走不到。
                #   典型场景：storescp 这类只收 Explicit VR LE 的 SCP。
                # 传输语法没被接受 → 这一次确实没发出去
                msg = f"context rejected: {e}"
                ok = False
            except Exception as e:
                # Return all preceding acknowledgements, especially Warning:
                # a later network error must not erase coercion evidence.
                msg = f"send error: {e}"
                ok = False
                unknown = True
            finally:
                try:
                    assoc.release()
                except Exception:
                    # A received response remains evidence even if release fails.
                    pass
            if ok:
                break
        results.append((ds.SOPInstanceUID, ok, msg, not unknown))
    return results


def probe(host, port, aet="", timeout=3, cecho_timeout=5,
          calling_aet="IOL2DCM"):
    """探测 DICOM 服务端是否在线。返回 (state, detail)。

    三态
    ----
    "up"      端口通，且 C-ECHO 收到 Status 0x0000 —— DICOM 层在应答。
    "partial" 端口通，但 C-ECHO 没成功（关联被拒 / 超时 / SCP 只讲存储不讲
              Verification）。**不能直接判死**：有些老 SCP 不实现
              Verification SOP Class，照样能收 C-STORE。
    "down"    端口不通（连接被拒 / 超时 / 主机名解析失败）。

    ★ 为什么分两级探：开机时 Orthanc 的进程可能刚 exec，端口还没 listen；
      也可能 listen 了但 storage 还没 ready。TCP 只说明「进程在」，
      C-ECHO 才说明「DICOM 层在应答」。而「能不能收单」最终只有真实
      C-STORE 说了算，所以 partial 由调用方决定是否继续尝试。

    ★ 这里刻意不抛异常：探测是本项目的控制流（决定「现在跑不跑一轮」），
      不是错误路径。异常一律折算成 "down"/"partial"。
    """
    import socket
    import time as _time

    t0 = _time.monotonic()
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
    except OSError as e:
        return "down", f"TCP 连接失败（{host}:{port}）: {e}"
    except Exception as e:                       # 主机名解析等
        return "down", f"TCP 连接异常（{host}:{port}）: {e}"
    tcp_ms = int((_time.monotonic() - t0) * 1000)

    try:
        from pynetdicom import AE
        from pynetdicom.sop_class import Verification

        ae = AE(ae_title=calling_aet)
        ae.add_requested_context(Verification)
        # ★ 探测必须**有界**：默认 acse_timeout 30s、network_timeout 60s，
        #   在「PACS 刚 listen 但没应答」这种半起状态下一次探测就能卡两分钟，
        #   守候循环形同虚设。
        try:
            ae.connection_timeout = timeout
            ae.acse_timeout = cecho_timeout
            ae.network_timeout = cecho_timeout
            ae.dimse_timeout = cecho_timeout
        except Exception:
            pass                                  # 老版本没有这些属性
        assoc = ae.associate(host, port, ae_title=aet or "ANY-SCP")
        if not assoc.is_established:
            return "partial", (f"TCP 通（{tcp_ms}ms），关联未建立"
                               f"（被拒或 {cecho_timeout}s 超时）")
        try:
            st = assoc.send_c_echo()
            code = getattr(st, "Status", None) if st is not None else None
        finally:
            try:
                assoc.release()
            except Exception:
                pass
    except Exception as e:
        return "partial", f"TCP 通（{tcp_ms}ms），C-ECHO 异常: {e}"

    if code == 0x0000:
        return "up", f"TCP+C-ECHO 正常（{tcp_ms}ms）"
    if code is None:
        return "partial", f"TCP 通（{tcp_ms}ms），C-ECHO 无响应"
    return "partial", f"TCP 通（{tcp_ms}ms），C-ECHO Status=0x{code:04X}"
