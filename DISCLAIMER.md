# 适用范围声明 / Scope of Use

## 中文

**本项目（iol2dcm）仅对 Carl Zeiss IOLMaster 500 进行过测试。**

对于 **IOLMaster 700** 以及**任何其他型号、其他厂商**的生物测量设备，
本项目**不做任何可用性保证**，也不保证读数、字段映射、单位换算或计算结果正确。

具体而言，以下均**未经测试、未经验证**：

- IOLMaster 700 / 700 SWEPT-SOURCE / 800 等后续机型
- 其他厂商的光学生物测量仪（Lenstar、Argos、Pentacam AXL 等）
- 任何非 `IolmXP.MDB` 的数据库结构（表名、字段名、编码、单位可能完全不同）
- 任何未经 README 明确列出的设备软件版本

若需在其他设备上使用，**必须**先自行完成：

1. **数据库结构核对** —— 表名与字段是否存在、语义是否一致
2. **单位与量纲核对** —— 眼轴 mm、曲率半径 mm、前房深度 mm
3. **与设备原厂报告单逐例比对** —— 至少覆盖正常眼、长眼轴、短眼轴、
   术后眼、角膜异常各若干例
4. **屈光结果与临床实际结果对照**

在这些验证完成之前，**不得**将本项目的输出用于任何临床决策。

### 患者数据

本仓库**不含**任何患者数据：没有 MDB 活库、没有 `.iol`、没有 DICOM 实例、
没有渲染出的报告单。`.gitignore` 对这些类型做了强制排除。

运行时会把患者身份信息（姓名、生日、检查日期、PatientID）写入
`/var/lib/iol2dcm/state.json`。常驻流程日志使用稳定的一次性患者标签替代姓名，
但仍包含检查日期和 PatientID。`audit` 默认同样只输出标签，加 `--show-ident`
才打印明文姓名；`render`、`check` 是交互式排障命令，会在终端输出姓名（渲染
出的 PNG 文件名使用标签）。部署方必须自行落实访问控制与备份保护，并确保
排障时外传的状态文件、命令输出和日志均已脱敏。详见 README「患者数据与隐私」。

### 关于本声明所依据的事实

本项目的数据通路、字段映射与常数换算，全部基于对 IOLMaster 500
设备软件（V. 5.5）产生的 `IolmXP.MDB` 的实测：

| 项目 | 依据 |
|---|---|
| 表名 `IolmMeasurements` | 实测该库存在此表 |
| 字段 `al_r/al_l`、`r1_r/r1_l`、`r2_r/r2_l`、`acd_r/acd_l`、`axis_r/axis_l` | 与设备原厂报告单逐例核对一致 |
| K = 337.5 / r | 用真实报告单反算验证（两眼 K1/K2 共 4 个值全部吻合到 0.01 D） |
| 未测出字段存 0 而非 NULL | 验证样例库实测（acd 仅约三分之一有值，lens_thickness 全为 0） |

**这些事实不自动适用于其他机型。** IOLMaster 700 使用不同的数据库结构
（`CZM_*.MDB` 系列而非 `IolmXP.MDB`），字段名、单位、编码方式均可能不同，
本项目未对其做任何适配。

---

## English

**This project (iol2dcm) has only been tested against the Carl Zeiss IOLMaster 500.**

No guarantee of usability is made for the **IOLMaster 700** or **any other model
or manufacturer** of optical biometry devices. Correctness of readings, field
mapping, unit conversion, or calculation results is **not** guaranteed for such
devices.

Specifically, the following are **untested and unverified**:

- IOLMaster 700 / 700 SWEPT-SOURCE / 800 and later models
- Optical biometers from other manufacturers (Lenstar, Argos, Pentacam AXL, etc.)
- Any database schema other than `IolmXP.MDB` (table names, column names,
  encodings, and units may differ entirely)
- Any device software version not explicitly listed in the README

To use this project on other devices, you **must** first independently verify:

1. **Database schema** — that the expected tables and columns exist and carry
   the same semantics
2. **Units and dimensions** — axial length in mm, corneal radius in mm,
   anterior chamber depth in mm
3. **Case-by-case comparison against the device's native report** — covering at
   minimum normal eyes, long axial lengths, short axial lengths, post-surgical
   eyes, and corneas with abnormalities
4. **Refractive outcome correlation** against actual clinical results

**Until these verifications are complete, output from this project must not be
used for any clinical decision.**

### Patient data

This repository contains **no patient data**: no MDB live database, no `.iol`
files, no DICOM instances, no rendered reports. `.gitignore` enforces these
exclusions.

At runtime, patient identifiers (name, date of birth, exam date, PatientID)
are written to `/var/lib/iol2dcm/state.json`. Service log messages use a stable
one-way patient label instead of the name, but still contain dates and PatientIDs.
`audit` also prints labels by default and only shows names with `--show-ident`.
`render` and `check` are interactive troubleshooting commands and do print names
to the terminal (rendered PNG filenames use labels).
Deployers remain responsible for access control, backup protection, and
de-identifying any state, command output, or log excerpt shared for troubleshooting.

### Basis for this statement

All data pathways, field mappings, and constant conversions in this project are
derived from measurements taken on an IOLMaster 500 running software V. 5.5,
producing `IolmXP.MDB`:

| Item | Evidence |
|---|---|
| Table `IolmMeasurements` | Observed to exist in that database |
| Columns `al_r/al_l`, `r1_r/r1_l`, `r2_r/r2_l`, `acd_r/acd_l`, `axis_r/axis_l` | Verified item-by-item against native device reports |
| K = 337.5 / r | Back-computed from real reports (all 4 K1/K2 values across both eyes matched to 0.01 D) |
| Unmeasured fields stored as 0, not NULL | Observed in the validation sample (acd present in roughly one third of rows, lens_thickness all zero) |

**These facts do not automatically transfer to other models.** The IOLMaster 700
uses a different database schema (the `CZM_*.MDB` series rather than
`IolmXP.MDB`); column names, units, and encodings may all differ. This project
has not been adapted to it in any way.
