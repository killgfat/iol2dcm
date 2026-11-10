# iol2dcm

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

IOLMaster 活库（`IolmXP.MDB`）→ 四公式晶体度数计算 → A4 晶体单 + 生物测量 IOD → DICOM → PACS。

装完即跑，无需人工干预。不解读 `.iol`、不依赖 REST、不绑定特定 PACS。

> ## ⚠️ 适用范围
>
> **本项目仅对 Carl Zeiss IOLMaster 500 进行过测试。**
>
> 对 **IOLMaster 700** 及任何其他型号、其他厂商的生物测量设备，**不做任何可用性
> 保证**，也不保证读数、字段映射、单位换算或计算结果正确。
>
> 若需在其他设备上使用，**必须先**自行完成：① 数据库结构核对；② 单位量纲核对；
> ③ 与原厂报告单逐例数值比对；④ 屈光结果临床对照。
>
> 在这些验证完成之前，**不得**将本项目的输出用于任何临床决策。
>
> 完整声明与依据（含英文版）：[DISCLAIMER.md](DISCLAIMER.md)

## 环境要求

| 项 | 要求 |
|---|---|
| 操作系统 | Linux + systemd（Debian/Ubuntu、RHEL/Fedora、Arch 已适配装包命令） |
| Python | 3.9 或更高 |
| 权限 | root（要写 `/opt`、`/etc/systemd/system`、`/var/lib`） |
| 中文字体 | 需要 Noto Sans CJK + Liberation；`install.sh` 会尝试自动装 |
| 网络 | 安装时需访问 iolcon.org 拉常数表（可用 `--skip-constants` 跳过） |

装之前必须先把 `config.yaml` 里的 `mdb_path` 指到真实现场路径。
**没改之前 `pipeline.py check` 必然失败**（MDB 不存在），这是预期行为，
不是装坏了。

## 安装

```bash
sudo bash install.sh
```

逐项问现场参数，回车用默认值。也可以一次给全、全程不问：

```bash
sudo bash install.sh \
    --mdb /storage/iolmaster/IOLData/IolmXP.MDB \
    --pacs pacs.example.org:4242 \
    --institution 示例医院
```

只改一个字段（不碰型号清单与其他配置）：

```bash
sudo bash install.sh --pacs pacs.example.org:4242 --update-config
```

`install.sh` 会拉取常数表、生成 `config.yaml`、生成 systemd unit，
然后自检、`enable`、启动。

装完自动跑。自检不通过会退出，不带病启动 —— 但 **PACS 暂时不通不算失败**，
只警告。

开机时 DICOM 服务端（Orthanc / PACS）常常比本管线晚起来几十秒到几分钟。
管线发现目标不可达时**不会退出、也不会跳过这批数据**：它原地守候，每
`watchdog.probe_seconds`（默认 15s）探一次（TCP + C-ECHO），一通就立刻把积压
补推上去，等多久都不丢。想确认对端状态，单独跑：

```bash
/opt/iol2dcm/venv/bin/python /opt/iol2dcm/pipeline.py probe
# ✓ host:port AE=ORTHANC → up
#   结论：可推送 —— TCP+C-ECHO 正常（2ms）
```

单例推送失败按指数退避重试（默认 60s → 120 → … → 封顶 15 分钟），
PACS 半通时不会每轮把几百例重渲一遍。详见 `config.yaml` 的
`watchdog` / `retry` 两段。

```bash
systemctl status iol2dcm
tail -f /var/log/iol2dcm.log
```

`sudo bash install.sh --help` 看全部选项。

## 目录结构

按管线阶段分包，依赖方向严格单向：

```
iol2dcm/
├── pipeline.py          唯一入口：无参数即常驻运行
├── config.yaml          型号清单 + 计算口径 + 推送目标
│
├── core/                基础设施（零内部依赖）
│   ├── utils.py         日志、UID、数值清洗
│   ├── config.py        配置与常数装载
│   └── patient.py       PatientID 生成
├── sources/             取数
│   ├── mdb.py           IolmXP.MDB 解码 + 结构校验
│   └── constants.py     IOLCon XML → 常数表 JSON
├── formulas/            四公式
│   ├── srkt.py haigis.py hoffer_q.py holladay1.py
│   ├── common.py        共享工具
│   └── calc.py          按型号编排
├── render/              出片
│   ├── sheet.py         计算结果 → 报告单结构
│   └── report.py        A4 200dpi 渲染
└── dicomlib/            DICOM 封装与推送
    ├── dicom.py         SC 晶体单图 + C-STORE
    ├── oam.py           生物测量 IOD（眼轴 + 前房深度）
    └── keratometry.py   角膜曲率 IOD（K / r / 轴位）
```

`install.sh` 生成的文件不归仓，归现场：

```
/opt/iol2dcm/config.yaml                     ← 从模板生成 + 按参数打补丁
/opt/iol2dcm/iolcon_constants.json           ← 安装时从 IOLCon 拉取
/etc/systemd/system/iol2dcm.service          ← 主服务
/etc/logrotate.d/iol2dcm                     ← 日志轮转（copytruncate，必选）
```

配置只有一个来源：`/opt/iol2dcm/config.yaml`。没有 env 文件。

```
pipeline.py ──┬── sources/     ──┐
              ├── formulas/    ──┼── core/
              ├── render/      ──┤
              └── dicomlib/    ──┘
```

## 运维命令

平时用不到，排障时先 `systemctl stop iol2dcm`。

```bash
cd /opt/iol2dcm
P=/opt/iol2dcm/venv/bin/python

$P pipeline.py check      # 自检各环节
$P pipeline.py probe      # 只探 DICOM 目标（TCP + C-ECHO），看对端状态
$P pipeline.py lenses     # 看当前上表型号与常数
$P pipeline.py render --patient 张三 --date 20250101 --outdir /tmp/out
$P pipeline.py run --limit 5
$P pipeline.py constants  # 更新常数表
```

`pipeline.py` 不带子命令时等价于常驻模式：

```bash
python3 pipeline.py                                 # 常驻
python3 pipeline.py --mdb /path/x.MDB --interval 60  # 常驻 + 覆盖参数
```

## 换晶体型号

只改 `config.yaml`：

```yaml
lenses:
  - name: ZCB00                              # 显示名
    key: Johnson and Johnson Vision ZCB00    # 常数表全称，大小写敏感
```

⚠️ 型号名差一个字母就是另一个晶体：
`AR40e`（a₀=+0.472, n=368）≠ `AR40E`（a₀=−2.42, n=653）。

## 常数表

不随仓库分发（IOLCon 条款 8.1 禁止再分发），每次部署现场拉：

```bash
sudo bash install.sh          # 装的时候拉一次，之后不再自动更新
```

没有定时任务 —— 常数表装好就固定下来，要更新时手工跑：

```bash
cd /opt/iol2dcm
./venv/bin/python pipeline.py constants    # 拉新表（失败会保留旧表）
systemctl restart iol2dcm                  # 新常数要重启才生效
```

更新**只影响新检查**，已推送的历史病例保持当时的计算结果 —— 临床上可能
已经有手术决定基于那份单，不该被悄悄改写。

新表款数骤降（<50%）会拒绝替换，防止 IOLCon 页面改版把好表冲成残片。
详见 [docs/constants.md](docs/constants.md)。

## DICOM 输出

一次检查 = 1 个 study，N 款晶体各一个 SC series + 1 个 OAM + 1 个 KER：

```
study (PatientID + 检查日期)
  ├─ series "IOL Repo ZCB00"                 OT   SC   晶体单图
  ├─ ...
  ├─ series "Ophthalmic Axial Measurements"  OAM  OAM  眼轴 + 前房深度
  └─ series "Keratometry"                    OP   KER  角膜曲率 K / r / 轴位
```

| 类型 | SOP Class | 内容 |
|---|---|---|
| SC | `1.2.840.10008.5.1.4.1.1.7` | 晶体单 A4 图 |
| OAM | `1.2.840.10008.5.1.4.1.1.78.7` | 眼轴 AL + 前房深度 ACD（段测量） |
| KER | `1.2.840.10008.5.1.4.1.1.78.3` | 角膜曲率半径 r1/r2、K1/K2、散光轴位 |

**缺数据不丢数据**：AL 和 K 都测到才出晶体单。只测了眼轴（没测角膜曲率）
时度数算不出来，但眼轴是确定的实测值 —— 这类检查只发 OAM，不发晶体单。
反过来只有 K 没有 AL 就只发 KER。缺 AL 的眼不进 OAM，缺 r 的眼不进 KER。
详见 [docs/dicom.md](docs/dicom.md#studyseries-组织)。

K 值不能塞进 OAM —— 那个 IOD 里没有这些属性。标准给角膜曲率留的位置是
独立的 Keratometry Storage。ACD 走 OAM 的段测量（`SEGMENTAL LENGTH` +
`SCT 31636006 Anterior Chamber`），不用 (0022,1131)（那个属 IOL Calc IOD）。
详见 [docs/dicom.md](docs/dicom.md)。

只走 **DICOM C-STORE**，不依赖任何厂商 REST 接口。

## PatientID

完全本地生成，不查 PACS：

```
I + 生日(YYYYMMDD) + sha1(归一化姓名|生日) 前4位大写
张伟 / 1970-01-01 → I19700101EA08
```

同样输入永远同样输出，所以重复推送天然幂等，不需要映射缓存。

## 患者信息与隐私

本项目处理**真实患者身份信息**。部署方必须自行落实访问控制 —— 这一点
不因为代码是开源的而改变。

会落盘的位置：

| 位置 | 内容 | 建议权限 |
|---|---|---|
| `/var/lib/iol2dcm/state.json` | 姓名、生日、检查日期、PatientID | `0600`，仅服务账号可读 |
| `/var/log/iol2dcm.log` | 稳定的不可逆患者标签、检查日期、PatientID（不写姓名） | `0640`，logrotate 的 `create 0640` |
| `/opt/iol2dcm/config.yaml` | 院内 MDB 路径、PACS 地址、机构名 | `0644`，不含患者数据 |

推论：

- **不要把 `state.json` 或 `iol2dcm.log` 贴到 issue、论坛或任何外部服务**。state 仍含身份字段，日志仍含日期和 PatientID。
  排障需要贴日志时，先脱敏姓名与日期。
- 本仓库不含任何患者数据。`.gitignore` 已禁止 MDB、`.iol`、DICOM 实例、
  渲染出的 PNG、状态文件入库 —— 贡献代码前请确认 `git status` 里没有这些。
- 日志中的姓名已替换为稳定的一次性标签；日期和 PatientID 仍应按敏感信息管理。

## 文档

| 文件 | 内容 |
|---|---|
| [docs/architecture.md](docs/architecture.md) | 管线运行逻辑、七道关、耗时实测、设计取舍 |
| [docs/dicom.md](docs/dicom.md) | study/series 组织、SC tag 策略、OAM 字段、常见问题 |
| [docs/constants.md](docs/constants.md) | 常数来源优先级、覆盖率、型号匹配规则 |
| [docs/mdb.md](docs/mdb.md) | MDB 字段清单、K 换算、结构校验、数据质量 |
| [docs/operations.md](docs/operations.md) | 安装、状态文件、强制重推、故障排查、回填 |
| [DISCLAIMER.md](DISCLAIMER.md) | 适用范围声明的完整版（中英） |

## 公式名口径

全链路统一按 IOLCon 写法：`SRK/T` · `Haigis` · `Hoffer Q` · `Holladay 1`。

## 许可

[MIT](LICENSE) © 2026 iol2dcm contributors。

注意：MIT 只覆盖本仓库的代码。IOLCon 常数表不随仓库分发（其条款 8.1 禁止
再分发），部署时需自行从 iolcon.org 生成，那份数据的使用受 IOLCon 自身条款
约束，不受本许可影响。
