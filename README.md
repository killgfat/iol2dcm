# iol2dcm

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**把 IOLMaster 500 的检查自动送进 PACS。**

程序在后台监视设备的 `IolmXP.MDB` 数据库。发现新检查后，它读取双眼测量值，
按配置的人工晶状体型号计算 **SRK/T、Haigis、Hoffer Q、Holladay 1** 四种结果，
为每款晶体生成一张报告，再通过 **DICOM C-STORE** 把报告和可用的原始测量数据
发往 PACS。安装并启动后，日常无需手工执行命令。

## 它能做什么，不能做什么

| 能做 | 不做 |
|---|---|
| 读取 IOLMaster 500 的 `IolmXP.MDB` | 不解析 `.iol`，不读取 IOLMaster 700 的 `CZM_*.MDB` |
| 按 `config.yaml` 中的晶体清单计算四公式 | 不推荐晶体，不替代医生判断或原厂报告 |
| 每款晶体生成一张 A4 报告图 | 不把报告图当作结构化测量值的替代品 |
| 发送报告图（SC）、眼轴/前房深度（OAM）、角膜曲率（KER） | 不依赖厂商 REST API，不自动连接 HIS |
| 监视文件变化、断点续跑、失败重试 | 不保证未经实测的设备和数据库可用 |

> [!WARNING]
> **目前仅对 Carl Zeiss IOLMaster 500（软件 V. 5.5）进行过测试。**
> IOLMaster 700、其他型号和其他厂商设备均未经验证。在更换设备或数据库前，
> 必须核对结构与单位、逐例比对原厂报告，并完成临床结果对照。
> 验证完成前，不得把输出用于临床决策。完整声明见 [DISCLAIMER.md](DISCLAIMER.md)。

## 一次检查会经历什么

~~~text
IOLMaster 500 完成测量
        ↓ 写入 IolmXP.MDB
等待文件稳定（默认连续 5 秒不变）+ 新检查冷却期（默认 20 分钟）
        ↓
读取双眼 AL、角膜曲率半径、ACD、轴位等可用字段
        ↓
按每款晶体计算四种公式，并生成对应报告
        ↓
报告图 SC + 眼轴/前房深度 OAM + 角膜曲率 KER
        ↓ DICOM C-STORE
PACS 中出现一个 study；每款晶体一个 series，测量数据各占一个 series
~~~

例如配置 4 款晶体且双眼数据齐全时，一次检查生成 **6 个实例**：4 张报告、
1 个 OAM、1 个 KER。缺少部分测量值时只发送能够确定的内容，详见
[缺数据时会发什么](#缺数据时会发什么)。

## 快速开始

### 1. 准备现场参数

安装前准备以下信息：

- `IolmXP.MDB` 的实际路径，且运行服务的机器能持续读取它；
- PACS 的主机名或 IP、DICOM 端口、Called AE Title；
- 可选：报告单页眉的机构名；
- 安装阶段可访问 iolcon.org，用于生成晶体常数表。

运行环境：

| 项目 | 要求 |
|---|---|
| 操作系统 | Linux + systemd |
| Python | 3.9 或更高 |
| 安装权限 | root；需要写 `/opt`、`/var/lib` 和 `/etc/systemd/system` |
| 字体 | Noto Sans CJK + Liberation；安装脚本会尝试安装 |
| 网络 | 安装时下载 Python 依赖和 IOLCon 常数表；运行推送时需要连接 PACS |

> 仓库里的 `config.yaml` 是模板。必须把 `mdb_path` 改成现场真实路径；
> 保留示例路径时，自检提示“MDB 不存在”是预期结果。

### 2. 安装

在仓库目录运行交互式安装：

~~~bash
sudo bash install.sh
~~~

也可以一次给全参数：

~~~bash
sudo bash install.sh \
    --mdb /storage/iolmaster/IOLData/IolmXP.MDB \
    --pacs pacs.example.org:4242 \
    --aet ORTHANC \
    --institution 示例医院
~~~

安装脚本会把程序放到 `/opt/iol2dcm`，创建虚拟环境、生成现场配置和
systemd 服务，拉取常数表，执行自检，然后启用并启动 `iol2dcm`。
已有 `config.yaml` 默认保留，不会因重装代码而被覆盖。

PACS 暂时不可达只会产生警告，不阻止服务启动；程序会等待 PACS 恢复后补发。
全部安装选项见 `sudo bash install.sh --help`。

### 3. 验证安装

~~~bash
cd /opt/iol2dcm
P=/opt/iol2dcm/venv/bin/python

$P pipeline.py check       # 配置、字体、MDB、计算、封装和 PACS 全链路自检
$P pipeline.py lenses      # 核对晶体型号与四公式常数
systemctl status iol2dcm   # 确认后台服务正在运行
tail -f /var/log/iol2dcm.log
~~~

只检查 PACS 连通性：

~~~bash
$P pipeline.py probe
# ✓ host:port AE=ORTHANC → up
#   结论：可推送 —— TCP+C-ECHO 正常（2ms）
~~~

只生成 PNG、不向 PACS 推送，便于与原厂报告逐例核对：

~~~bash
$P pipeline.py render --patient 张三 --date 20250101 --outdir /tmp/out
~~~

`--patient` 是姓名子串，`--date` 格式为 `YYYYMMDD`。姓名和日期按现场
检查记录填写，不要把上面的示例当作测试数据。

> [!IMPORTANT]
> **首次启动会处理库中的历史检查，不只是新检查。** 回填量和耗时取决于历史
> 数据量、进程数及 PACS 性能。上线前应决定是否需要全量回填，并先用少量
> 脱敏或测试数据完成验收。限量 dry-run 不向 PACS 推送，但仍需避免与常驻服务
> 同时读写状态：
>
> ```bash
> systemctl stop iol2dcm
> $P pipeline.py run --dry-run --limit 5
> systemctl start iol2dcm
> ```

## 日常使用与配置

安装完成后服务开机自启，正常流程不需要人工操作：

- MDB 默认每 60 秒检查一次；文件稳定后才读取；
- 新检查默认等待 20 分钟冷却期，减少补测前的半成品报告；
- PACS 不可达时持续等待，恢复后处理积压；
- 已推送内容通过状态文件去重；补测自动在同一 study 新增 Rec 2 / Rec 3
  series（SC/OAM/KER 同轮），保留历史；失败复用同轮 UID 补齐，不调用 REST。
  见 [补测轮次与升级说明](docs/rec-rounds.md)。

所有现场配置都在 `/opt/iol2dcm/config.yaml`，改完重启服务：

~~~bash
systemctl restart iol2dcm
~~~

| 要修改的内容 | 配置位置 | 详细说明 |
|---|---|---|
| 晶体型号 | `lenses` | [常数表与型号匹配](docs/constants.md#换晶体型号) |
| 目标屈光度、度数步长 | `calculation` | 配置文件内注释 |
| MDB 路径 | `mdb_path` | [MDB 解码](docs/mdb.md) |
| PACS 地址、端口、AE Title | `push` | [运维](docs/operations.md#配置只有一个来源) |
| 页眉、科室、设备信息 | `report` | 配置文件内注释 |
| 并发进程数 | `performance.workers` | [性能与并发](docs/architecture.md#并发处理) |
| 轮询、稳定等待、冷却期 | `watcher` | [冷却期与补测](docs/operations.md#冷却期与补测v131-起) |
| PACS 探测策略 | `watchdog` | [PACS 连通性](docs/operations.md#只看-pacs-通不通probe) |
| 单例失败重试 | `retry` | [失败处理](docs/architecture.md#失败处理分层) |

更换晶体只需修改清单。例如：

~~~yaml
lenses:
  - name: ZCB00                              # 报告单与 series 的显示名
    key: Johnson and Johnson Vision ZCB00    # IOLCon 常数表全称
~~~

`key` 必须准确匹配常数表。`AR40e` 与 `AR40E` 是不同晶体；同名不同
`id` 的条目要用 `key@id` 明确指定。修改后用 `pipeline.py lenses` 核对。

只更新 PACS 等安装参数、保留其他配置：

~~~bash
sudo bash install.sh --pacs pacs.example.org:4242 --update-config
~~~

## 在 PACS 里会看到什么

一次检查对应一个 study。配置 N 款晶体且数据齐全时：

~~~text
study（PatientID + 检查日期）
  ├─ IOL Repo <型号 1>                 SC   人工晶状体计算报告图
  ├─ ...
  ├─ IOL Repo <型号 N>                 SC   人工晶状体计算报告图
  ├─ Ophthalmic Axial Measurements    OAM  眼轴 AL + 前房深度 ACD
  └─ Keratometry                      KER  角膜曲率 K、半径 r、散光轴位
~~~

### 缺数据时会发什么

| 测量情况 | 报告图 SC | OAM | KER |
|---|---:|---:|---:|
| AL 和角膜曲率齐全 | 每款晶体 1 个 | 有 | 有 |
| 只有 AL，没有角膜曲率 | 不生成 | 有 | 无 |
| 只有角膜曲率，没有 AL | 不生成 | 无 | 有 |
| 一眼有 AL，另一眼有角膜曲率 | 不生成 | 只含有 AL 的眼 | 只含有曲率的眼 |
| 两眼均无有效 AL 和角膜曲率 | 不生成 | 无 | 无 |

缺少 AL 或角膜曲率时不能计算晶体度数，但已测得的数据仍会按对应类型发送。
AL、ACD、K、r 等缩写及完整 SOP Class 见 [DICOM 输出规范](docs/dicom.md)。

### PatientID

PatientID 在本地根据“归一化姓名 + 生日”确定，不查询 PACS，也不是 HIS 登记号：

~~~text
I + 生日(YYYYMMDD) + sha1(归一化姓名|生日) 前 4 位大写
示例：张伟 / 1970-01-01 → I19700101EA08
~~~

相同输入得到相同 ID；同名同生日仍可能冲突，录入质量需要现场保证。

## 常用维护命令

~~~bash
cd /opt/iol2dcm
P=/opt/iol2dcm/venv/bin/python
~~~

| 目的 | 命令 |
|---|---|
| 全链路自检 | `$P pipeline.py check` |
| 只探测 PACS | `$P pipeline.py probe`（`--json` 可供监控使用） |
| 查看晶体与常数 | `$P pipeline.py lenses` |
| 本地渲染，不推送 | `$P pipeline.py render --patient 张三 --date 20250101 --outdir /tmp/out` |
| 只读审计补测和实例差异 | `$P pipeline.py audit` |
| 手动处理一轮 | `$P pipeline.py run --limit 5` |
| 更新常数表 | `$P pipeline.py constants`，然后重启服务 |

> 除 `check`、`probe`、`lenses`、`audit` 等只读操作外，手工运行
> `run`、更新 state 或处理回填前，先执行 `systemctl stop iol2dcm`，
> 避免两个进程同时写状态文件。常数表更新详见
> [何时更新、怎么更新](docs/constants.md#何时更新怎么更新)。

## 患者数据与隐私

运行过程会接触真实患者信息。部署方负责访问控制、备份保护和合规使用。

| 位置 | 内容 | 注意事项 |
|---|---|---|
| `/var/lib/iol2dcm/state.json` | 姓名、生日、检查日期、PatientID、处理状态 | 默认权限 `0600`；不要对外发送原文件 |
| `/var/log/iol2dcm.log` | 常驻流程中的稳定患者标签、检查日期、PatientID、错误信息（不含姓名） | 按敏感信息管理；外传前脱敏 |
| `/opt/iol2dcm/config.yaml` | MDB 路径、PACS 地址、机构名 | 不含患者测量值 |
| `render` 输出目录 | 含患者信息的报告 PNG | 用后按院内制度清理，不要提交到仓库 |

仓库不包含患者数据，`.gitignore` 已排除 MDB、`.iol`、DICOM、渲染图和
状态文件。不要把真实患者数据放入 issue、测试 fixture 或公开日志。

## 文档导航

| 我想…… | 阅读 |
|---|---|
| 安装、查日志、处理补测、回填或备份 | [docs/operations.md](docs/operations.md) |
| 理解 MDB 字段、单位和取行规则 | [docs/mdb.md](docs/mdb.md) |
| 更换晶体、理解常数来源与更新影响 | [docs/constants.md](docs/constants.md) |
| 核对 PACS 中的 series 和 DICOM 字段 | [docs/dicom.md](docs/dicom.md) |
| 修改处理流程、重试、状态或并发 | [docs/architecture.md](docs/architecture.md) |
| 确认适用设备和责任边界 | [DISCLAIMER.md](DISCLAIMER.md) |

## 许可

代码采用 [MIT](LICENSE) 许可，© 2026 iol2dcm contributors。

IOLCon 常数表不随仓库分发；部署时需在本地生成，其使用受 IOLCon 自身条款约束。
