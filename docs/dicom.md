# DICOM 输出规范

本文面向需要在 PACS 中核对结果、或修改 DICOM 封装的读者。可以把输出理解为
两类内容：**给人看的报告图**，以及**给其他系统读取的结构化测量值**。

| 缩写 | 含义 | 在本项目中的作用 |
|---|---|---|
| SC | Secondary Capture，二次捕获图像 | 每款晶体一张 A4 报告图 |
| OAM | Ophthalmic Axial Measurements | 眼轴 AL 和可选的前房深度 ACD |
| KER / Keratometry | 角膜曲率测量 | K1/K2、r1/r2 和散光轴位 |
| AL | Axial Length，眼轴长度 | 生物测量输入，单位 mm |
| ACD | Anterior Chamber Depth，前房深度 | 生物测量输入，单位 mm |
| K | Keratometric Power，角膜曲率屈光力 | 单位 D；由半径 r 换算 |
| study / series / instance | 检查 / 序列 / 实例 | 一次检查包含多个 series，每个 series 在本项目中有一个实例 |

一次完整检查的输出是 `N 个 SC + 1 个 OAM + 1 个 KER`，N 为配置的晶体款数。
缺少测量值时不补 0、不造空实例，按下表只发有依据的内容。

## study / series 组织

一次检查 = 1 个 study：

```
study (PatientID + 检查日期)
  ├─ series "IOL Repo ZCB00"                 OT   SC   晶体单图
  ├─ series "IOL Repo MX60"                  OT   SC
  ├─ series "IOL Repo NS-60YG"               OT   SC
  ├─ series "IOL Repo Softec HD"             OT   SC
  ├─ series "Ophthalmic Axial Measurements"  OAM  OAM  眼轴 + 前房深度
  └─ series "Keratometry"                    OP   KER  角膜曲率 K / r / 轴位
```

**实例数是可变的**，取决于这次检查测到了什么：

| 情况 | 发什么 |
|---|---|
| 两眼 AL + K 齐全 | SC×N + OAM + KER（6 个，N=4 款晶体） |
| 有 AL、无 K | **OAM only** —— 度数算不出，不出晶体单 |
| 有 K、无 AL | **KER only** |
| 一眼 AL、另一眼 K | OAM（1 眼）+ KER（1 眼） |
| 一眼齐全、另一眼缺 K | SC×N + OAM（2 眼）+ KER（1 眼） |
| 两眼既无 AL 也无 r | 不发，检查记 `src: "empty"` |

缺 AL 的眼不进 OAM（它的 Measurements Sequence 是 Type 1，不能空），
缺 r 的眼不进 KER。**缺数据不等于丢数据**：能录的照录，只是不出晶体单。

| 类型 | SOP Class | Modality | Transfer Syntax |
|---|---|---|---|
| SC | `1.2.840.10008.5.1.4.1.1.7` | OT | JPEG Baseline `1.2.840.10008.1.2.4.50` |
| OAM | `1.2.840.10008.5.1.4.1.1.78.7` | OAM | Explicit VR LE |
| KER | `1.2.840.10008.5.1.4.1.1.78.3` | OP | Explicit VR LE |

**生物测量原始值用标准 IOD 承载，不自造 XML 格式。** 任何支持 Ophthalmic Axial
Measurements Storage / Keratometry Measurements Storage 的 PACS / 工作站都能
直接读，不用为本项目写适配层。

### 为什么 K 值单独一个实例

角膜曲率**不能**塞进 OAM。Ophthalmic Axial Measurements IOD 里根本没有
K 值、曲率半径、散光轴位这三个属性 —— 硬加就是非法属性，严格的 PACS
会整条拒收。标准给它们留的位置是独立的 Keratometry Measurements
Storage（78.3，C.8.25.10 模块）。详见 [KER 实例内容](#ker-实例内容)。

## SC 实例的 tag 策略

晶体单 SC 只写**能确定患者和检查**的标签 + 设备元数据，**不写任何眼部测量参数**：

```
患者      PatientName / PatientID / PatientBirthDate / PatientSex / SpecificCharacterSet
检查      StudyInstanceUID / SeriesInstanceUID / StudyDate / StudyID
          StudyDescription / SeriesDescription / SeriesNumber / InstanceNumber
          Modality / BodyPartExamined
设备      InstitutionName / InstitutionalDepartmentName / Manufacturer
          ManufacturerModelName / DeviceSerialNumber / StationName
          SoftwareVersions / OperatorsName（有值才写）
像素      Rows / Columns / BitsAllocated / BitsStored / HighBit
          SamplesPerPixel / PhotometricInterpretation / PlanarConfiguration
          PixelRepresentation / PixelData
```

共 **33 个元素**。

**明确不写**（这些曾写在标签区，已移除）：

| Tag | 属性 | 为什么去掉 |
|---|---|---|
| (0022,1007/1008) | Ophthalmic Axial Measurements Eye Sequence | 与 OAM 实例重复 |
| (0046,0070/0071) | Keratometry Eye Sequence | 归属 KER 实例 |
| (0022,1019) | Ophthalmic Axial Length | 归属 OAM 实例 |
| (0022,1131) | Anterior Chamber Depth | 归 IOL Calculations (78.8)；本项目改走 OAM 段测量 |
| (0046,0147) | Cylinder Power | 归 KER / IOL Calc 实例 |
| — | 全部 IOL 度数 / 晶体常数 | 术者决策，不是测量结果 |

理由：同一份数据在标签区和图里各存一遍，两处可能不一致，PACS 端分不清哪个是权威。
生物测量原始值的正规归宿是 OAM 与 KER 实例，SC 图只是给人看的报告单。

## OAM 实例内容

逐条对照 DICOM PS3.3 C.8.25.14（Ophthalmic Axial Measurements Module）。

**按眼写入**：`OphthalmicAxialLengthMeasurementsSequence` (0022,1050) 是
Type 1，里面必须有 TOTAL LENGTH 的实测值。所以没有 AL 的那只眼整眼不写 ——
不能填 0（0 mm 会被下游当成一个真实的荒谬测量值），也不能写空序列。两眼都
没有 AL 时整个 OAM 实例不发。

| 属性 | Tag | 取值 |
|---|---|---|
| Modality | (0008,0060) | `OAM` |
| Ophthalmic Axial Measurements Device Type | (0022,1009) | `OPTICAL` |
| Lens Status Code Sequence | (0022,1024) | MDB 原文（见下） |
| Vitreous Status Code Sequence | (0022,1025) | MDB 原文（见下） |
| Ophthalmic Axial Length Measurements Type | (0022,1010) | `TOTAL LENGTH` / `SEGMENTAL LENGTH` |
| Ophthalmic Axial Length | (0022,1019) | AL 或 ACD，单位 mm（VR = FL） |
| Ophthalmic Axial Length Measurement Modified | (0022,1140) | `NO` |
| Ophthalmic Axial Length Measurements Segment Name | (0022,1101) | `SCT 31636006` Anterior Chamber（仅 ACD 项） |
| Optical Selected Ophthalmic Axial Length Sequence | (0022,1255) | IOL 计算实际采用的那次测量 |
| Ophthalmic Axial Length Data Source | (0022,1150) | `111780` Measurement From This Device |

### 前房深度：走段测量，不用 (0022,1131)

`OphthalmicAxialLengthMeasurementsSequence` 里放**两个** item：

```
item[0]  Type = TOTAL LENGTH       → TotalLengthSequence      AL  = 23.51 mm
item[1]  Type = SEGMENTAL LENGTH   → SegmentalLengthSequence  ACD = 3.12 mm
                                      SegmentNameCodeSequence
                                        SCT 31636006 Anterior Chamber
```

依据 C.8.25.14.1.1.4：光学生物测量仪除了全长，也可以单独测某一段，
原文举例就是 "the Anterior Chamber depth"。段名取自 CID 4233。

**不用 `AnteriorChamberDepth` (0022,1131)** —— 那个属性的归属是
Intraocular Lens Calculations IOD（C.8.25.16.2 的
`AnteriorChamberDepthSequence` 0022,1128），本 IOD 里没有它。

ACD 缺失时**只发一个 item**，
不塞 0 也不塞空段 —— 没测就是没测。

### 眼状态：原文直封，不做翻译

MDB 的 `state_r` / `state_l` 是操作员录入的中文自由文本，**原样**写进 Code Sequence：

```
CodingSchemeDesignator = 99IOL2DCM    （DICOM PS3.3 §8.2 规定的本地方案）
CodeValue              = 原文（SH 16 字符上限，超长截断）
CodeMeaning            = 原文全文
```

为什么不映射成 SNOMED：映射必然要猜，而猜错比不猜更糟 ——

- 「硅油充填眼」只说了玻璃体，没说晶状体是天然还是人工
- 「人工晶体」漏了「状」字，和「人工晶状体」是不是同一件事要靠猜
- 白名单方案下，任何没见过的写法都会让整例失败

原文本身就是事实，直接落库最省事也最准确。下游要用 SNOMED 就自己按 CodeMeaning 去做。

字段归属：MDB 只有一列 `state` 混记晶状体与玻璃体状态。原文里提到玻璃体的
（含「硅油」「气体」「玻切」「玻璃体」等词）才写进 Vitreous 字段，否则写 `None`。
这是字段归属判断，不是语义翻译。

**刻意不写进 OAM 的内容**：

- 角膜曲率 K — 属于 Keratometry Measurements Storage (`78.3`)
- 晶体度数 — 属于 Intraocular Lens Calculations Storage (`78.8`)，本项目由晶体单图承载

## 推送

只用 **DICOM C-STORE**，不依赖任何厂商 REST 接口。

一次关联推完整个 study。数据齐全时是 N+2 个实例（N 张晶体单 + OAM + KER）；
缺数据时按有效测量确定应发集合；完整生成并持久化后才发，全部实例明确成功
才提交该轮。补测的 SC/OAM/KER 名称统一加 Rec N，为该轮派生新 Series/SOP
UID，Study UID 不变。传输语法策略：

```
先试 JPEG Baseline   → 约 640 KB/张
不行再降裸像素        → 约 11.6 MB/张（18 倍）
```

## KER 实例内容

逐条对照 DICOM PS3.3 C.8.25.10（Keratometry Measurements Module）。

**按眼写入**：`Keratometry Right/Left Eye Sequence` 是 Type 1C（"测了就写"），
缺 `r1` 或 `r2` 的那只眼整眼不写；两眼都没有 r 时整个 KER 实例不发。

| 属性 | Tag | Type | 取值 |
|---|---|---|---|
| Modality | (0008,0060) | 1 | `OP` |
| Keratometry Right Eye Sequence | (0046,0070) | 1C | 右眼测了就写 |
| Keratometry Left Eye Sequence | (0046,0071) | 1C | 左眼测了就写 |
| Steep Keratometric Axis Sequence | (0046,0074) | 1 | 最陡子午线 |
| Flat Keratometric Axis Sequence | (0046,0080) | 1 | 最平子午线 |
| Radius of Curvature | (0046,0075) | 1 | r1 / r2，mm（VR = FL） |
| Keratometric Power | (0046,0076) | 1 | K1 / K2，D（VR = FL） |
| Keratometric Axis | (0046,0077) | 1 | 轴位，度（VR = FL） |

### 陡 / 平按半径判，不假定 r1 恒为平

标准对两条子午线的语义是固定的：

```
Steep = greatest power of curvature / shortest radius of curvature
Flat  = least power of curvature  / longest radius of curvature
```

所以判据是**半径大小**，不是字段位置：

| 情况 | Steep | Flat |
|---|---|---|
| `r1 < r2` | r1, K1, axis | r2, K2, axis+90° |
| `r1 > r2` | r2, K2, axis+90° | r1, K1, axis |
| `r1 == r2` | r1, K1, axis | r2, K2, axis（球面，标准允许两条等价） |

⚠️ 设备数据里 **r1 可能大于 r2**；以下示例均为脱敏示意值，
也就是 **K1 是平的那条**。如果按「r1 恒为 Flat」写死，这一例的陡平就整个反了。

### 轴位归属

MDB 只存一列 `axis_r` / `axis_l`，它是 **K1（r1 那条子午线）的轴**，
已用真实报告单核对。K2 的轴恒为 K1 轴 +90°（主子午线互相垂直）。

于是轴位跟着半径一起走 —— 见上表。设备未测轴位时 `KeratometricAxis`
兜底填 0（Type 1 不能缺；0° 与 180° 等价，是无信息占位）。

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `/preview` 返 415，取回解码报 "number of bytes of pixel data is less" | PixelData 塞了 PNG 字节而非 JPEG 封装 | 必须 `encapsulate([jpg])` |
| 中文姓名落库成 `?` | 缺 `SpecificCharacterSet` | 设为 `ISO_IR 192` |
| `No presentation context ... has been accepted` | 内存 dataset 的 `original_encoding` 恒为 Implicit VR | 推前按 `file_meta` 设好 `_read_implicit` / `_read_little` |
| 花屏 | 目标端不认 JPEG | 回退裸像素重推 |
| 补测重推不生效 | 同 SOP UID 会静默去重 | v1.3.4 自动新增 Rec N，Series/SOP UID 同时更新；禁止删 state 强推，见 [补测轮次说明](rec-rounds.md) |
| 无像素实例推送抛 `AttributeError: TransferSyntaxUID` | OAM / KER 未声明传输语法 | 显式设 `ExplicitVRLittleEndian` |
| 一个实例失败后整个 study 都不推了 | `send_c_store` 在 SCP 拒绝传输语法时抛 `ValueError`，原先没捕获 | 已捕获并转入 JPEG → 裸像素回退 |
