# 运维

## 安装

```bash
git clone https://github.com/killgfat/iol2dcm /root/iol2dcm && cd /root/iol2dcm
sudo bash install.sh          # 逐项提问，回车用默认值
```

装完**自动启动**，不需要任何人工干预 —— 这是一条自动运行的管线。
自检不通过会 `exit 1`，不带病启动。

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
  ✓ 库结构: IolmMeasurements 表校验通过，必需字段齐全
  ✓ MDB 解码: 全库检查记录
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

```json
{
  "sig": "Johnson and Johnson Vision ZCB00|Bausch + Lomb enVista|...",
  "done": {
    "张|伟|20250101": {
      "t": "2026-11-06 21:49:37",
      "pid": "I19700101EA08",
      "src": "generated",
      "date": "20250101",
      "sc": 4,
      "lenses": ["ZCB00", "MX60", "NS-60YG", "Softec HD"],
      "oam_eyes": 2,
      "ker_eyes": 2,
      "instances": 6,
      "fp": "97e3faa9f2311176"
    },
    "李|娜|20250102": {
      "t": "2026-11-06 21:49:40",
      "pid": "I19800202B31C",
      "src": "generated",
      "date": "20250102",
      "sc": 0,
      "lenses": [],
      "oam_eyes": 2,
      "ker_eyes": 0,
      "instances": 1
    },
    "王|五|20250103": {
      "t": "2026-11-06 21:49:41",
      "pid": "I19900303D77A",
      "src": "empty",
      "date": "20250103",
      "n": 0
    }
  },
  "failed": {
    "李|娜|20250102": {
      "t": "2026-11-07 08:12:04",
      "err": "部分成功 3/5: 1.2.840… 0x0122",
      "tries": 3,
      "next": 1730956800.0
    },
    "王|五|20250103": {
      "t": "2026-11-07 08:12:05",
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
- 每 25 例原子写一次（`tmp` + `fsync` + `rename`），断电最多丢 25 例进度
- `failed` 里的记录按指数退避重试：`tries` 是失败次数，`next` 是下次
  可重试的绝对时间戳（epoch 秒）。到点之前**不会**重新渲染那一例。
- `permanent: true` 表示确定失败（库里没生日、计算失败），不再重试。
  改配置或改型号会让 `sig` 变化、state 整体重置，它们会重新尝试一次。
- `failed` 指向的检查如果已从设备库里归档掉，下一轮会被自动清理。
  `cooling` / `stale` 同样清理，避免 state 无限膨胀。
- `cooling`：`key -> {fp, first, ready}`，冷却中的检查。只放窗口内的条目，
  推成功后立即删除，稳态规模很小。`ready` 是到期时刻，`loop()` 靠它唤醒。
- `stale`：`key -> {was_fp, now_fp, was, now_instances}`，设备补测过但
  PACS 里还是旧版的病例，等人工确认。**这是需要人看的**，不是自动重试队列。
- `sig` 只由 `INSTANCE_LAYOUT` + 型号 key 拼成（**不含** `PRIVATE_CREATOR` /
  `LAYOUT_VERSION`，也不含数据内容）。换型号、或升级到「缺数据只出结构化
  实例」这类改动时，全部作废重推一次（旧记录里的 `done` 是在旧口径下判的）

**怎么让它立刻重试**：把某条的 `next` 改成 0（或删掉这条记录），重启服务即可。

### 冷却期与补测（v1.3.1 起）

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
- 现场实测：某例 11:02 首测 → 11:13 补眼轴，跨度 11 分钟。

**2. 补测后不能被永久冻结 —— 内容指纹**

`done` 里每条记录带 `fp`（姓|名|日期 + 规范化后的实测值 + 眼状态）。
下一轮比对的是**内容**，不再是「这例处理过没有」。

- 指纹一样 → 真·已推过，跳过（幂等照旧）
- 指纹不同 → 设备补测过，写进 `state.stale` 并告警，**不自动重推**

为什么不自动重推：OAM/KER 的 SOP Instance UID 只由 study + 布局版本派生，
不含内容。同 UID 不同内容推上去，PACS 默认保留首个并**返回成功** —— 结果
是「推送成功、state 记 6 实例齐全、PACS 里还是旧版单眼图」，比卡住更难查。

**升级兼容**：本改动之前写的 `done` 没有 `fp`。首次扫描时会把**当前**
内容就地补写成指纹、**不重推**（否则升级即全量重推，而 PACS 会
按同 SOP UID 静默去重，白打网络且给不出新内容）。代价：升级前已经发生的
补测查不出来，用 `audit` 核对。

### 审计：找出 state 与真实数据不一致的病例

```bash
# 只读，不推 PACS、不改 state，可在生产上随便跑
/opt/iol2dcm/venv/bin/python /opt/iol2dcm/pipeline.py audit
```

输出四类：

| 分类 | 含义 |
|---|---|
| 内容已变 | 设备补测过，PACS 里是旧版（本次事故的形态） |
| 实例数不符 | 按当前数据重算，与 `done` 里记的对不上 |
| 无指纹 | 老记录，无法判断是否陈旧（退化到只比实例数） |
| 库中已无对应检查 | 设备归档/清库，可安全清理 |
| 从未真正发送过（dry/empty） | 历史污染记录，一个实例都没发过。**下次扫描会自动清掉重扫**，不需要人工介入，也不计入结论。列入本表只为说明「PACS 里没有它」 |

退出码 0 = 未发现不一致，1 = 有问题。适合放进 cron 每天跑一次。

### 想强制重推

```bash
systemctl stop iol2dcm
rm /var/lib/iol2dcm/state.json      # 或只删某几条
systemctl start iol2dcm
```

只删部分记录：编辑 JSON，删掉 `done` 里对应的 key（以及 `stale` 里
同一 key）。

⚠️ **注意 Orthanc 对同 SOP UID 会静默去重**：同 UID 不同内容推上去，
它保留首个并返回成功。所以**只删 state 再重推是拿不到新内容的**，
必须先删 PACS 旧实例。

⚠️ **提升版本号不是定点修复手段，而且两种版本号后果不同**：

| 改什么 | `sig` 变吗 | 实例 UID 变吗 | 后果 |
|---|---|---|---|
| `INSTANCE_LAYOUT` | **变** | 不变 | state 整体作废 → 全量重推；但 UID 不变，PACS 全部静默去重 → **白跑一遍，什么也没改** |
| `PRIVATE_CREATOR` / `LAYOUT_VERSION` | 不变 | **变** | 已推的不会自动重推；但以后任何重推都产生新 UID → PACS 里**凭空多出整套同名 series**（实测：同一 study 下第 2 个 OAM + 第 2 个 KER） |

（`sig` 实际只由 `INSTANCE_LAYOUT` + 型号 key 拼成，见 `_load_state`。）

### 定点修复单个病例（推荐）

这正是 `stale` 的处理流程。`stale` 是**持续生效的人工闸门**：只要内容还没
变回 PACS 里已有的那一版，管线就不会自动重发它（不管触发源是别的患者的
MDB 更新、手动 `run` 还是服务重启）。

```bash
# 1. 看清单（只读）
/opt/iol2dcm/venv/bin/python /opt/iol2dcm/pipeline.py audit

# 2. 在 PACS 上删掉该 study 的旧实例
#    Orthanc：DELETE /studies/<studyUID>（或按 series 精确删）

# 3. 删掉 state.json 里该 key 的 done 与 stale 条目，重启服务
systemctl restart iol2dcm

# 4. 再跑一次 audit 确认结论变成「未发现不一致」
```

**为什么必须先删旧实例**：OAM/KER 的 SOP Instance UID 只由 study + 布局
版本派生，不含内容。不删的话重推会被静默去重，账目却记成成功。

## 常见故障

| 现象 | 排查 |
|---|---|
| 服务不断重启 | `journalctl -u iol2dcm -n 50`；多半是 MDB 路径不对 |
| 日志「库结构不符」 | MDB 路径指错，或换成了非 IOLMaster 500 的库 |
| 日志「DICOM 目标不可达 … 原地守候」 | **正常**：PACS 还没起来，管线在等，不会丢数据。先 `pipeline.py probe` 看对端状态，再查 `config.yaml` 的 `push` 段 |
| 日志「… 仍在守候」 | 按 `watchdog.log_every` 节流的进度行（默认每 20 次 ≈ 5 分钟一行） |
| 日志「本轮未消费（pacs）」 | 守候通过但预检又失败（探测与推送之间掉了）。签名未推进，下轮重来 |
| 日志「第 N 次失败，Xs 后重试」 | 单例退避中，见 `retry` 段；不到点不会重新渲染那一例 |
| 日志「failed 队列清理 N 条」 | 那些检查已被设备归档、库里读不到，无从重试 |
| 日志「度数不可算（缺 AL 或缺 K），跳过晶体单」 | **正常**：只发 OAM/KER 结构化实例，PACS 里看得到测量值但没有晶体单 |
| 日志「实例不齐 X/Y（生成阶段失败）」 | 渲染或封装抛异常了。按退避重试，不会误标完成 |
| PACS 里只有 OAM 没有晶体单 | 这条检查没测角膜曲率（或没测眼轴），算不出度数。见 `state.json` 的 `sc: 0` |
| 日志「常数表不存在」 | 常数表被删了：`$P pipeline.py constants` 或重跑 install.sh |
| 「待处理 0 例」但确实有新数据 | MDB mtime 没变？或该检查在 state 的 `done` 里且指纹一致 |
| 「N 例冷却中」 | **正常**：还没满 `cooling_seconds`，到点自动推 |
| 「N 例数据已更新待确认」 | 设备补测过，PACS 里是旧版。跑 `pipeline.py audit` 看清单，按「定点修复」处理 |
| 中文变问号 | 缺中文字体，`apt install fonts-noto-cjk` |
| 部分实例推送失败 | 看 `state.json` 的 `failed` 字段，有具体 SOP UID 与错误码 |

## 全量回填

首次部署时 state 为空，会自动把所有历史数据推一遍：

```
全库病例 × 约 1.15 s ≈ 数小时
```

`watch` 模式每轮有 `--max-seconds 300`（5 分钟）预算，到点就回到循环开头
重新检查 mtime。配合「按日期倒序」排序，保证新拍的片子不会排在几千例积压后面。

要一次性推完不中断：

```bash
systemctl stop iol2dcm
nohup /opt/iol2dcm/venv/bin/python /opt/iol2dcm/pipeline.py run \
      --max-seconds 0 > /tmp/backfill.log 2>&1 &
```

## 备份

要备份的只有三样：

```
/opt/iol2dcm/config.yaml            型号清单与口径
/opt/iol2dcm/iolcon_constants.json  常数表（可重建，但重建要联网）
/var/lib/iol2dcm/state.json         进度（丢了就重推，不致命）
```

代码可以用 `install.sh` 重装，不需要备份。重装不会碰 `config.yaml`。

`/etc/systemd/system/iol2dcm.service` 和 `/etc/logrotate.d/iol2dcm` 是生成的，
不要手改 —— 下次重装会被覆盖。要改服务参数改 `install.sh` 里的 `UNIT_LINES`。
