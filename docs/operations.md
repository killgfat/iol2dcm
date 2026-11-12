# 运维

本文按现场任务组织。新部署先完成 [README 的快速开始](../README.md#快速开始)，
再用本页处理维护与异常。

| 我要做什么 | 看哪一节 |
|---|---|
| 安装、重装或只改一个安装参数 | [安装](#安装) |
| 查看服务、日志或运行自检 | [日常命令](#日常命令)、[运维子命令](#运维子命令) |
| 确认 PACS 是否可推送 | [probe](#只看-pacs-通不通probe) |
| 修改晶体或更新常数 | [配置](#配置只有一个来源)、[常数表更新](#常数表更新手工没有定时任务) |
| 新检查迟迟没进 PACS | [冷却期与补测](#冷却期与补测v131-起)、[常见故障](#常见故障) |
| 补测后 PACS 仍是旧内容 | [只读审计](#审计找出-state-与真实数据不一致的病例)、[定点修复](#定点修复单个病例推荐) |
| 首次上线处理历史检查 | [全量回填](#全量回填) |
| 备份现场数据与进度 | [备份](#备份) |

以下命令默认安装在 `/opt/iol2dcm`。涉及患者姓名、日期、state 或日志的输出
均属于敏感信息，不要在 issue 或聊天工具中直接外发。

## 安装

```bash
git clone https://github.com/killgfat/iol2dcm /root/iol2dcm && cd /root/iol2dcm
sudo bash install.sh          # 逐项提问，回车用默认值
```

装完**自动启动**，日常不需要手工运行命令。自检不通过会 `exit 1`，不带病启动。

> 首次启动时 state 为空，会自动处理 MDB 中全部历史检查，并非只等待新检查。
> 上线前请先阅读[全量回填](#全量回填)，评估数据量与 PACS 压力。

### 三种装法

**现场装机（推荐）**：逐项问，回车取默认值。

```bash
sudo bash install.sh
```

**无人值守**：所有参数一次给全，一个都不问。

```bash
sudo bash install.sh \
    --mdb /storage/iolmaster/IOLData/IolmXP.MDB \
    --pacs pacs.example.org:4242 \
    --aet ORTHANC \
    --institution 示例医院 \
    --interval 60
```

**只改一个字段**：不碰其他任何配置。

```bash
sudo bash install.sh --pacs pacs.example.org:4242 --update-config
```

| 选项 | 作用 |
|---|---|
| `--mdb PATH` | `IolmXP.MDB` 路径 |
| `--pacs HOST[:PORT]` | DICOM 目标，端口默认 4242 |
| `--aet TITLE` | 目标 AE Title，默认 ORTHANC |
| `--institution NAME` | 报告单页眉机构名，留空则不印 |
| `--interval SEC` | MDB 轮询间隔，默认 60 |
| `--update-config` | 只把上面给的参数写进已有 `config.yaml` |
| `--force-config` | 备份后丢弃现有 `config.yaml`，按参数重建 |
| `-y, --yes` | 不提问，缺的用默认值 |
| `--no-start` | 装好但不启动服务 |

不给 `--update-config` / `--force-config` 时，**已存在的 `config.yaml` 原样保留**
（重装代码不会冲掉现场配置）。`--force-config` 会先备份成
`config.yaml.bak.<时间戳>`。

### 脚本做的事

1. 建 `/opt/iol2dcm`、`/var/lib/iol2dcm`
2. 装系统依赖（`python3-venv`、中文字体 Noto CJK / 解放黑体）
3. 复制代码与六个包
4. 建 venv、`pip install -e`（依赖清单在 `pyproject.toml`；先试清华镜像，失败回退官方源）
5. **拉取常数表** `iolcon_constants.json`（只此一次，之后不再自动更新；
   够新就不拉，拉不到又没旧表 → 拒绝安装）
6. **生成** `config.yaml`：从模板铺一份，再用 `tools/patch_config.py`
   按现场参数打补丁 —— **原有中文注释全部保留**，只改被点名的值
7. **生成** `/etc/systemd/system/iol2dcm.service` + `/etc/logrotate.d/iol2dcm`
   （顺带清理残留的常数表定时任务）
8. 自检 → `enable` + `restart`

### 没有 env 文件

PACS 目标只认 `config.yaml` 的 `push` 段，不存在 `EnvironmentFile=`。

两处配同一个东西必然出「改了不生效」的坑：优先级一乱，改 `config.yaml`
就彻底白改，而这个现象极难查。只剩一处，改了就是改了。

### 为什么用生成的，不是仓库里放死文件

unit 文件里 `/opt/iol2dcm` 出现四次。放死文件意味着改安装路径要同步改
unit、改 install.sh、改 constants 的相对路径解析——总会漏一处。生成的话三个
地方共用同一组变量。

### 日志轮转

unit 用 `StandardOutput=append:` 一直持有文件句柄，所以 `/etc/logrotate.d/iol2dcm`
必须用 `copytruncate`：rename 之后服务会继续往已删除的 inode 里写，日志实际丢了。

systemd < 240 不支持 `append:`，脚本会自动退回 journald 并删掉 logrotate 文件。
systemd < 231 不支持 `MemoryMax`，也会自动省略。

## 日常命令

```bash
systemctl status iol2dcm
systemctl restart iol2dcm
systemctl stop iol2dcm
tail -f /var/log/iol2dcm.log
```

系统单位文件：`/etc/systemd/system/iol2dcm.service`（**由 `install.sh` 生成，不要手改**）

```ini
[Unit]
Description=iol2dcm — IOLMaster 晶体单自动生成与推送
Documentation=file:///opt/iol2dcm/README.md
After=network-online.target local-fs.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/iol2dcm
Environment=IOL2DCM_CONFIG=/opt/iol2dcm/config.yaml
ExecStart=/opt/iol2dcm/venv/bin/python /opt/iol2dcm/pipeline.py
Restart=always
RestartSec=30

# 2C/2G 的小机器，别让它吃满
Nice=10
IOSchedulingClass=idle
MemoryMax=800M
StandardOutput=append:/var/log/iol2dcm.log
StandardError=append:/var/log/iol2dcm.log

[Install]
WantedBy=multi-user.target
```

`WorkingDirectory` 是必需的 —— `config.yaml` 与常数表都按相对路径解析。
`MemoryMax` 与 `append:` 两行会按 systemd 版本自动取舍（<231 / <240 时省略）。

## 运维子命令

平时用不到，排障与维护时才跑。**跑之前先 `systemctl stop iol2dcm`**，
否则两个进程会同时写 state 文件。

```bash
cd /opt/iol2dcm
P=/opt/iol2dcm/venv/bin/python

$P pipeline.py check                    # 自检各环节
$P pipeline.py probe                    # 只探 DICOM 目标（TCP + C-ECHO）
$P pipeline.py probe --json             # 同上，JSON（给监控/cron 用）
$P pipeline.py lenses                   # 看当前上表型号与常数
$P pipeline.py audit                    # 只读审计：核对 state 与当前 MDB
$P pipeline.py render --patient 张三 --date 20250101 --outdir /tmp/out
$P pipeline.py run --limit 5            # 手动补一轮
$P pipeline.py constants                # 更新常数表
```

### check 检查什么

```
配置与常数
  ✓ 装载: 646 款常数 / 上表 4 款 / 目标屈光 -0.5 D
  ✓ 常数完整性: 四公式 × 4 款常数齐全
报告单渲染
  ✓ 中文字体: 思源黑体
数据源
  ✓ 库结构: IolmMeasurements，必需字段齐全
  ✓ MDB 解码: 检查记录读取成功
计算与封装
  ✓ 端到端试跑: PNG 203KB, SC 2338×1654, OAM Modality=OAM
推送目标
  ✓ DICOM 端口: pacs.example.org:4242 AE=ORTHANC 可达
```

任一项 ✗ 都会指出具体原因，并返回非 0 退出码。

### 哪一项拦住启动

`install.sh` 调用的是 `check --soft-push`：

| 项 | 失败时 |
|---|---|
| 装载 / 常数完整性 | ✗ 拦住，不启动 |
| 中文字体 | ✗ 拦住，不启动 |
| 库结构 / MDB 解码 | ✗ 拦住，不启动 |
| 端到端试跑 | ✗ 拦住，不启动 |
| **DICOM 端口** | **⚠ 只警告，照常启动** |

PACS 不可达不该拦住安装：管线是 `watch` 常驻，启动后**原地守候**，
每 `watchdog.probe_seconds`（默认 15s）探一次（TCP + C-ECHO），一通就立刻
把积压补推上去。等多久都不丢 —— 每轮都按全库重新筛选待办，不做增量游标。
医院现场 PACS 往往是最后才配好的，开机顺序也不受控。

手工跑 `check` 不带 `--soft-push` 时仍然是严格的（端口不通就返回非 0），
排障时一眼能看出真实状态。

### 只看 PACS 通不通：`probe`

`check` 会跑端到端试跑（读库、渲染、封装），慢。只想确认对端状态时：

```bash
/opt/iol2dcm/venv/bin/python /opt/iol2dcm/pipeline.py probe
/opt/iol2dcm/venv/bin/python /opt/iol2dcm/pipeline.py probe --json
# 退出码：0=可推送，1=不可推送，2=配置错误
```

三态输出：`up`（TCP + C-ECHO 都正常）／`partial`（端口通、C-ECHO 没应答 ——
老 SCP 常见，按可推送处理）／`down`（端口不通）。cron 或监控可以直接看退出码。

### render 只出图不推送

排障时最有用的命令。可以指定患者姓名子串、检查日期、单个型号：

```bash
$P pipeline.py render --patient 李娜 --date 20230101 --outdir /tmp/out
$P pipeline.py render --patient 李娜 --lens ZCB00 --outdir /tmp/out
```

输出 PNG + 屏幕上的关键数值，同时打印 OAM 的眼状态解析结果。

## 常数表更新（手工，没有定时任务）

常数表**只在安装时拉一次**，之后固定下来。晶体常数一年也未必变一次，
为那点变化挂个每月半夜拉表并重启服务的定时器不划算。

要更新时手工跑：

```bash
cd /opt/iol2dcm
./venv/bin/python pipeline.py constants    # 拉新表（失败保留旧表）
systemctl restart iol2dcm                  # ★ 不重启不生效
```

`loop()` 只在进程启动时读一次常数表，常驻循环不重读 —— 所以拉完必须重启。

拉取失败不会动旧表（`atomic_write` + 款数骤减保护），服务继续用现有那份。
新表款数不足旧表 50% 时拒绝替换，防止 IOLCon 页面改版把好表冲成残片。

更新只影响**新检查**，已推送的历史病例保持当时的计算结果。
详见 [constants.md](constants.md#更新会影响已推送的病历吗)。


## 配置只有一个来源

所有配置都在 `/opt/iol2dcm/config.yaml`：

| 要改什么 | 改哪一段 |
|---|---|
| 晶体型号 | `lenses` |
| 计算口径 | `calculation` |
| MDB 路径 | `mdb_path` |
| PACS 目标 | `push` |
| 报告单页眉 | `report` |
| 轮询间隔 | `watcher` |
| PACS 晚启动时等多久、怎么探测 | `watchdog` |
| 推送失败的退避节奏 | `retry` |

改完 `systemctl restart iol2dcm` 生效，不用重装。

**没有环境变量文件。** PACS 目标只认 `config.yaml` 的 `push` 段
（两处配同一个东西会出「改了不生效」的坑）。

也可以用 install.sh 改单个字段，省得手滑改错缩进：

```bash
sudo bash install.sh --pacs pacs.example.org:4242 --update-config
```

## 状态文件

`/var/lib/iol2dcm/state.json`：

以下为合成的旧版状态结构示例，标识、日期及指纹均为虚构，不可作为实际 state 使用。

```json
{
  "key_scheme": "row-v1",
  "sig": "layout-version||synthetic:Test A|synthetic:Test B|synthetic:Test C|synthetic:Test D",
  "done": {
    "synthetic-guid-a|20000102": {
      "t": "2000-01-02 00:00:00",
      "pid": "SYNTHETIC-PATIENT-A",
      "src": "generated",
      "date": "20000102",
      "sc": 4,
      "lenses": [
        "Test A",
        "Test B",
        "Test C",
        "Test D"
      ],
      "oam_eyes": 2,
      "ker_eyes": 2,
      "instances": 6,
      "fp": "0000000000000001"
    },
    "synthetic-guid-b|20000102": {
      "t": "2000-01-02 00:00:01",
      "pid": "SYNTHETIC-PATIENT-B",
      "src": "generated",
      "date": "20000102",
      "sc": 0,
      "lenses": [],
      "oam_eyes": 2,
      "ker_eyes": 0,
      "instances": 1
    },
    "synthetic-guid-c|20000102": {
      "t": "2000-01-02 00:00:02",
      "pid": "SYNTHETIC-PATIENT-C",
      "src": "empty",
      "date": "20000102",
      "n": 0
    }
  },
  "failed": {
    "synthetic-guid-d|20000102": {
      "t": "2000-01-02 00:01:00",
      "err": "部分成功 3/5: <synthetic-sop-uid> 0x0122",
      "tries": 3,
      "next": 946684920.0
    },
    "synthetic-guid-e|20000102": {
      "t": "2000-01-02 00:01:01",
      "err": "无生日，无法生成 PatientID",
      "permanent": true,
      "tries": 1,
      "next": 0
    }
  }
}
```

- `src`：`generated` = 正常出单/结构化实例；`empty` = 该检查两眼既无 AL
  也无 r1+r2，没有任何可录入的测量值（不出任何实例，但也不该反复重试）
- `fp`：这一版**实测值**的内容指纹。下一轮比对它而不是比对 key 是否存在
  —— 设备补测会改写同一行（guid 不变），只有内容能区分。老记录没有它，
  首次扫描时会就地补写（不重推）
- `perm_fp`（只在 `failed` 里）：造成「永久失败」的那份**输入**的指纹。
  与 `fp` 是两个不同的东西 —— `fp` 表示上次尝试发送的版本，`perm_fp`
  表示让这条记录永久失败的那版输入。分开存放是因为「数据被清空」会让两者
  分叉：`fp` 还是上一版，`perm_fp` 是空记录。输入变化（含补回测量值、
  补上生日）后就会解除永久失败重新评估；没有 `perm_fp` 的老记录按「未知」
  处理，重新评估一次
- 历史遗留的 `dry: true` 与 `src: "empty"` 记录：它们都没真正推过实例，
  扫描时会被清掉重新处理（**不**补指纹），无论有没有 `fp`
- `sc` / `oam_eyes` / `ker_eyes` 记录本条实际发了什么。缺 AL 或缺 K
  的病例 `sc` 为 0（不出晶体单），能从 state 一眼看出来
- v1.3.4 在发送前持久化 pending（冻结输入、配置、轮次和目标），完整 DICOM
  载荷原子发布到 <state路径>.spool/；父进程再持久化 ready 后才 C-STORE。
  全部实例明确返回 0x0000 后立即持久化新的 done。
- done.revision 是最新完整轮次；旧记录没有该字段时按第 1 轮处理。
  done.history 留下此前完整轮次的指纹和实例数，不用于重复发送。
- failed 的 tries / next 沿用指数退避；有 pending 时重试相同载荷，
  不分配新轮次，也不拿当前 MDB 覆盖待完成版本。
- permanent 表示当前轮次已暂停；载荷丢失/损坏、C-STORE Warning 保留 pending，
  告警且不能通过设备继续补测绕过。
- pending 对应行即使从设备库归档也继续补齐；仅无载荷且无接收历史的孤儿失败可清理。旧 stale/接收历史保留告警。
- cooling 除新检查冷却外，也用于唤醒完成冻结版本后的最新数据扫描，
  以及 --limit / 时间预算没轮到的检查，避免等不到下一次 MDB 更新。
- stale 中普通补测（含无 from 的旧记录）在下轮自动转入 Rec N；
  空测量值、身份冲突、旧部分接收而缺少原始载荷仍告警安全暂停。
- sig 仍只由 INSTANCE_LAYOUT + 型号 key 拼成，本次没有修改该签名、
  KEY_SCHEME、PRIVATE_CREATOR、OAM/KER LAYOUT_VERSION。
  签名变化现在报错停止，禁止自动丢弃历史后全量重推。

重试遵循 next；需立刻重试时先停服务并备份一致的 state 与 spool，再将
对应 failed.next 设为 0。不要删除 done / pending / stale 或载荷。
对永久失败先修复根因；载荷丢失应恢复一致备份，不能删除条目强行跳过。

### 冷却期与补测（v1.3.1 起）

> v1.3.2 补充：本节说的「补测改写同一行、guid 不变」是冷却期成立的前提。
> 反过来说，**同组多行就不可能是版本序列**——那是多条独立记录。选行不能用
> 「谁后建」，见下文的[同组多行与选行口径](#同组多行与选行口径v132-起)。

MDB 是**可变快照**：设备没有「这次检查已结束」这个事件。操作员常常先测
角膜曲率、十几分钟后再补眼轴，而补测是**改写同一行**（guid 不变）。

由此产生两个必须分开处理的问题：

**1. 别推半成品 —— 冷却期**

`watcher.cooling_seconds`（默认 1200 = 20 分钟）内不推。起点取该行在设备
上的**创建时刻**（guid 内嵌的 `YYMMDDHHMMSSmmm`）；只有在创建时刻无法
解析（guid 格式变了）时，才退回用「本版内容首次被扫描看到的时刻」兜底。

⚠️ 由此带来一个**必须知道的语义**：冷却保证的是「距首次测量至少 20 分钟」，
**不**保证「每一版内容都稳定 20 分钟」。第 19 分钟补测的检查，第 20 分钟
就会发出去。这正是它挡不住、要靠内容指纹 + `audit` 兜底的那部分。

⚠️ 默认值写在代码里（`pipeline.DEFAULT_COOLING_SECONDS`），不是只写在
config 模板里 —— 已装机器的旧 `config.yaml` 没有这个字段，若运行时默认
成 0，这次修复对生产等于没生效。显式配 `0` 才是关闭。

- 历史积压不受影响：几个月前的检查创建时刻早已超窗，照常立刻推。
- 代价：新检查进 PACS 晚 20 分钟。设 0 可关闭（不推荐）。

**2. 补测后不能被永久冻结 —— 内容指纹**

`done` 里每条记录带 `fp`（姓|名|日期 + 规范化后的实测值 + 眼状态）。
下一轮比对的是**内容**，不再是「这例处理过没有」。

- 指纹一样 → 真·已推过，跳过（幂等照旧）
- 指纹不同 → 同一 study 新增完整的 Rec 2 / Rec 3，SC、OAM、KER 的
  SeriesDescription 都加相同后缀，例如 IOL Repo DCB00 Rec 2。
  SeriesInstanceUID 和 SOPInstanceUID 同时按轮次派生，旧轮次留在 PACS。
- 补测过程中再次变化 → 先补齐持久化的当前轮次，再扫描并排下一轮。
  数据回到更早的历史版本仍新增下一轮，不能把历史版误当成最新轮次。

本策略不调用 REST、不删除或覆盖 PACS 数据。首轮 UID 保持历史公式。
网络失败重试复用同轮 UID 和持久化载荷，避免静默去重吞掉新版。

**升级兼容**：本改动之前写的 `done` 没有 `fp`。首次扫描时会把**当前**
内容就地补写成指纹、**不重推**（否则升级可能触发历史记录全量重推，而 PACS 会
按同 SOP UID 静默去重，白打网络且给不出新内容）。代价：升级前已经发生的
补测查不出来，用 `audit` 核对。

### 审计：找出 state 与真实数据不一致的病例

```bash
# 只读，不推 PACS、不改 state，可在生产上随便跑
/opt/iol2dcm/venv/bin/python /opt/iol2dcm/pipeline.py audit
```

输出：

| 分类 | 含义 | 计入结论 |
|---|---|---|
| 内容已变 | 设备补测过，PACS 里是旧版 | ✔ |
| 实例数不符 | 按当前数据重算，与 `done` 里记的对不上 | ✔ |
| 已推内容现在没有有效测量值 | 记录发过实例，但当前选中的行没有 AL、也没有成对 r1+r2。明细里标注**有/无指纹**：无指纹表示「PACS 里是哪一版未知」 | ✔ |
| PatientID 与当前选中的行不一致 | 同组多行生日不同，PACS 里已有的是旧 PID。再推会新建一个患者档案 | ✔ |
| 无指纹 | 老记录，无法判断是否陈旧（退化到只比实例数） | ✘ |
| 同日同名多行 | 列出组内每行的空/有值、`当天` 或采集日、生日、`*` 标记实际选中的行。**只读提示** | ✘ |
| 库中已无对应检查 | 普通 done 仅提示；pending 继续补齐，旧 stale/接收历史保留告警 | 有异常记录则 ✔ |
| 从未真正发送过（dry/empty） | 历史污染记录，一个实例都没发过。**下次扫描会自动清掉重扫**，不需要人工介入 | ✘ |

退出码 0 = 未发现不一致，1 = 有问题。适合放进 cron 每天跑一次。

### 一行一个案例（v1.3.3 起）

**幂等键从 `姓|名|检查日期` 改成 `guid|检查日期`。**

旧键把「同一天」当成一次检查，同一天测两轮只能留一行，另一轮被丢掉或被覆盖。
新口径：**一行 = 一个独立案例**。同天两轮各自成案例、各自出单，互不覆盖。

选键原则：同 guid 可以对应多个 exam_date；姓名与生日也不能唯一标识一条
检查记录。因此使用 guid|exam_date 区分案例，避免以姓名分组覆盖独立记录。

#### 迁移（自动，一次性）

`run_once` 与 `audit` 都会调 `_migrate_keys()`，靠 state 里的 `key_scheme`
标记判断是否已迁移。规则：

| 情况 | 处理 |
|---|---|
| 单行组 | 那一行直接继承原 done 记录（fp / pid / instances 全保留） |
| 多行组 | 用「PACS 里那份来自哪一行」找出继承行，其余行作为全新案例 |
| 库中找不到（孤儿） | **保留原键不动**，计数上报，绝不删除 |

**为什么不会重推**：继承的记录带 `uid_seed: ""`，`study_uid_for(pid, date, "")`
与旧公式完全一致 —— 算出来的 study / series / SOP UID 与当年推的一模一样。
指纹也对得上，于是走「内容没变」分支跳过。

**为什么新案例不会撞 UID**：新案例的 `uid_seed` 是它自己的 guid，
`study_uid_for(pid, date, guid)` 是一个全新 study，SC/OAM/KER 的 UID 全部跟着变。

#### 部署前先预演

```bash
cp /var/lib/iol2dcm/state.json /tmp/verify_new/state_copy.json
python3 /tmp/verify_new/tools/verify_before_deploy.py \
    --mdb /storage/iolmaster/IOLData/IolmXP.MDB \
    --state /tmp/verify_new/state_copy.json --src /tmp/verify_new
```

预演只读，不写 state、不推 PACS。核对单行组是否原样继承、多行组是否只
选择正确来源行，以及新增案例数量是否符合待迁移数据；数量以各部署为准。
部署后核对迁移日志中的改名、拆分及新增案例计数，不使用固定预期值。

⚠️ **回滚代价**：迁移会改写 state 的键空间。要回滚到 1.3.2，必须同时还原
state 备份，否则旧代码认不出新键、会把整库当成没推过而全量重推。

---

### 同组多行与选行口径（v1.3.2 起，v1.3.3 已由「一行一案例」取代）

旧版按组选择最新记录时，可能选中创建较晚的空行，导致有数据的已推记录
无法补写指纹。以下说明保留的旧选行接口；当前管线按行处理独立案例。

v1.3.2 起选行改用 `sources.mdb.pick_row()`，返回 `(行, 结论)`：

| 结论 | 什么时候 | 管线怎么做 |
|---|---|---|
| `OWN` | 当天采集、有测量值 | 直接出单 |
| `OTHER` | 当天有记录但没测出值 | 判「本次就诊尚无数据」，不出单 |
| `EMPTY` | 整组没有可录入的测量值 | 跳过 |
| `PUSHED` | **仅限已推记录**：PACS 里那份的来源行 | 只比对 / 补写指纹，永不推送 |

**判断「当天」看 guid 内嵌的采集日期是否等于 `exam_date`**，不是看
`measure_ts` 谁大。日期不同可能表示同次采集挂到不同业务日期，不能只按创建时间判断来源。

⚠️ **已推过的记录走另一套口径**：问的不是「该用哪行出单」，而是「PACS 里那份
是哪行推出去的」——空行产不出实例，所以必然是**有测量值**那行。这条路径只用于
比对/补写指纹，永不触发推送。

需要知道的四件事：

1. `pick_latest()` 与 `pick_best()` 的行为保持兼容，供外部脚本调用；
   当前管线逐行处理，审计与旧键迁移使用 `pick_row()` 判断来源。
2. 同组两行都有测量值、**生日又不同**时，按采集时刻选最新、照常处理，
   并在日志里留一行「已推记录与当前行的 PatientID 不同」。
   使用者口径：数据对不上就不硬核对，**当两条独立记录**处理。
   所以这种情况不进 `stale`、不进 `audit` 的结论项，也不需要人跟进。
3. `audit` 有一节「采集日期与检查日期不同」—— **只读提示，不计入结论**。
   设备的正常行为（把一次测量挂到某个就诊日期下），不再因此拦单。
4. 同日同名两行**生日不同**，当两条独立记录处理，不做核对、不进待办。
   `audit` 的「同日同名多行」会把两行的生日都列出来，仅供人自己看。

**v1.3.3 已删除 `max_stale_days`。** 旧模型按 (姓,名,日期) 归组选一行，可能
选到几个月前采的那行，才需要「跨天就别推」的闸门。一行一个案例之后，这一行
就是它自己那次检查，采集日与 `exam_date` 不同是设备的业务行为 —— 按每行自己的
数据走既有的「信息是否充足」判定即可。该闸门可能误拦独立案例，因此已移除。

### 补测自动成轮（v1.3.4）

日常补测无需手工操作。旧 done + stale 存在且当前有效测量已变化、身份
一致时，服务启动扫描就生成 Rec 2；成功后清理该 stale，保留历史实例。
后续变化生成 Rec 3，以此类推。相同指纹不增加轮次，失败重试不增加轮次。

不要沿用旧“四步修复”：不删 PACS、不删 state、不提升全局 UID 版本号。
详细触发条件、恢复策略和验收证据见 [补测轮次与升级说明](rec-rounds.md)。

升级操作：

1. systemctl stop iol2dcm，备份 /var/lib/iol2dcm/state.json 及存在的
   state.json.spool，确认目录可写、有磁盘空间。
2. 更新代码至 v1.3.4；保留现场 config.yaml 和全部 state，无新配置项。
3. systemctl start iol2dcm，观察 Rec 2 ... 实例齐全 和失败告警。
4. 可运行只读 audit 检查台账；它不读 PACS，不能证明 PACS 文件实际存在。
   现场在 PACS 查看相同 study 中的新轮次及 OAM/KER 数值，完成最终验收。

watch / run 对同一路径 state 加进程锁。运行服务期间的手动 run 会拒绝
并行写入；只读 audit 不受影响。不要使用另一份 state 指向同一个设备/PACS。
run 有未完成、暂停或 stale 时返回 1；配置、state 损坏、持久化失败返回 2。
目标不可达返回 3。watch 遇到 state 写盘失败会退出，交给服务管理器告警/重启；
恢复存储后以原 UID 重试。

限制：C-STORE 逐实例发送，不是原子 study 事务。网络中断期间 PACS 可以暂时
只显示某轮的一部分；该轮不会记 done，恢复后补齐。无 REST/查询模式不能发现
PACS 被外部删改、备份回滚或服务端错误返回成功；必须保全 state 与 PACS 的
备份一致性。查看者应采用最高完整轮次；Rec N 是版本标识，不是诊断结论。

### 旧版补测 stale 自动迁移

**触发条件**：保留原配置、常数及 state 升级启动后，watch 首次启动扫描
MDB，无需等待文件再次变化。原 guid|exam_date 可读、有有效变化测量、
PatientID/日期/study 归属一致时自动排队。普通旧 stale 缺 from 也接受；
done 缺 revision 按 1，继承 uid_seed（缺失按空串），在原 study 生成 Rec 2。
实例数相同也按内容指纹判定。其他未变 done 不重推。--since 不应排除待迁移
记录；--limit/时间预算仅延迟，watch 后续继续。

**成功判据**：完整生成该轮全部 DICOM，校验哈希、患者/study、SOP/Series
唯一性后发送。SC/OAM/KER 名称均加 Rec N，Series/SOP UID 区别于历史轮次。
只有全部预期 SOP 明确返回 C-STORE 0x0000，才原子提交 done 的最新指纹、
revision、实例数、study_uid 与载荷证据，并清除该例 stale/pending/failed。
完成日志包含“实例齐全，done 已持久化”。旧轮次保留，done 的案例数不增加。

自动判据是 C-STORE 接收确认加持久化账本。audit 不读取 PACS，不能证明
远端逐字节保存；部署验证可在 PACS 查看器检查同 study 的新轮次与测量值，
这不构成自动迁移的人工前置条件。

**失败状态**：生成不齐不发送；部分接收、断网或响应丢失保留旧 done、stale
及冻结 pending，failed 记录错误、接收 UID 和 next。watch 自动退避重放同轮
载荷，失败重试不递增轮次。日志 ERROR“Rec N 未完成 X/Y”，audit/run 非零。
PACS 全收但 done 写盘失败时，不输出已提交成功；进程退出 2，重启复用原轮。
坏载荷、Warning、身份/study 冲突、目标切换或无法重构的旧部分接收暂停告警，
修复根因或恢复一致备份，不通过删 state/PACS 清警。

**运维操作**：不提升全局 UID 版本、不删 done/stale、不删 PACS 旧实例。
audit 只读，watch/run 执行迁移；查看完成日志与台账，失败时保全 state/spool。
没有新增配置项、REST 凭据或自动删除 PACS 的路径。

**回滚**：不能直接启动不理解轮次的旧版本，也不能只恢复升级前 state。
代码、配置、state、spool 与 PACS 的恢复必须保持一致，避免复用已存在的 UID。

## 常见故障

| 现象 | 排查 |
|---|---|
| 服务不断重启 | `journalctl -u iol2dcm -n 50`；多半是 MDB 路径不对 |
| 日志「库结构不符」 | MDB 路径指错，或换成了非 IOLMaster 500 的库 |
| 日志「DICOM 目标不可达 … 原地守候」 | **正常**：PACS 还没起来，管线在等，不会丢数据。先 `pipeline.py probe` 看对端状态，再查 `config.yaml` 的 `push` 段 |
| 日志「… 仍在守候」 | 按 `watchdog.log_every` 节流的进度行（默认每 20 次 ≈ 5 分钟一行） |
| 日志「本轮未消费（pacs）」 | 守候通过但预检又失败（探测与推送之间掉了）。签名未推进，下轮重来 |
| 日志「第 N 次失败，Xs 后重试」 | 单例退避中，见 `retry` 段；不到点不会重新渲染那一例 |
| 日志「failed 队列清理 N 条」 | 仅清理无接收历史/无 pending 的归档失败；有载荷继续补齐，stale/接收历史保留告警 |
| 日志「度数不可算（缺 AL 或缺 K），跳过晶体单」 | **正常**：只发 OAM/KER 结构化实例，PACS 里看得到测量值但没有晶体单 |
| 日志「实例不齐 X/Y（生成阶段失败）」 | 渲染或封装抛异常了。按退避重试，不会误标完成 |
| PACS 里只有 OAM 没有晶体单 | 这条检查没测角膜曲率（或没测眼轴），算不出度数。见 `state.json` 的 `sc: 0` |
| 日志「常数表不存在」 | 常数表被删了：`$P pipeline.py constants` 或重跑 install.sh |
| 「待处理 0 例」但确实有新数据 | MDB mtime 没变？或该检查在 state 的 `done` 里且指纹一致 |
| 「N 例冷却中」 | **正常**：还没满 `cooling_seconds`，到点自动推 |
| 「N 例数据已更新待确认」 | 普通有效补测自动新增 Rec N；空测量、身份冲突、旧部分接收等暂停告警。只读 audit 看原因，禁止删 state 强推 |
| 中文变问号 | 缺中文字体，`apt install fonts-noto-cjk` |
| 部分实例推送失败 | 看 `state.json` 的 `failed` 字段，有具体 SOP UID 与错误码 |

## 全量回填

首次部署时 state 为空，会自动把所有历史数据推一遍。耗时取决于病例数、
每例晶体款数、并发进程和 PACS 响应速度。应先用合成或脱敏数据测量吞吐量，再评估全量任务耗时。
选择并发数与查看实测条件见[架构说明](architecture.md#并发处理)。

`watch` 模式每轮有 `--max-seconds 300`（5 分钟）预算，到点就回到循环开头
重新检查 mtime。配合「按日期倒序」排序，保证新拍的片子不会排在几千例积压后面。

要一次性推完不中断：

```bash
systemctl stop iol2dcm
nohup /opt/iol2dcm/venv/bin/python /opt/iol2dcm/pipeline.py run \
      --max-seconds 0 > /tmp/backfill.log 2>&1 &
```

## 备份

停止服务后一起备份，state 与 spool 必须是同一时点：

```
/opt/iol2dcm/config.yaml            型号清单与口径
/opt/iol2dcm/iolcon_constants.json  常数表（可重建，但重建要联网）
/var/lib/iol2dcm/state.json         轮次账本与未完成事务，不可丢弃
/var/lib/iol2dcm/state.json.spool/  未完成轮次的不可变 DICOM，存在时必须保全
```

代码可以用 `install.sh` 重装，不需要备份。重装不会碰 `config.yaml`。

`/etc/systemd/system/iol2dcm.service` 和 `/etc/logrotate.d/iol2dcm` 是生成的，
不要手改 —— 下次重装会被覆盖。要改服务参数改 `install.sh` 里的 `UNIT_LINES`。

spool 含患者信息，目录 0700、文件 0600，按 state 同等级保护、加密备份。
完成轮次在 done 持久化后才清本地载荷；空 spool 目录是 state 丢失保护标记，勿删。
state 与 spool 同时丢失或回退时，C-STORE 无法重建历史轮次；不能以空 state
重启已有 PACS 的生产任务。
