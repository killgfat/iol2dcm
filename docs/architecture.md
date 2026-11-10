# 架构

## 目录结构与依赖分层

按**管线阶段**分包，依赖方向严格单向：

```
iol2dcm/
├── pipeline.py              唯一入口：无参数即常驻运行
├── config.yaml              型号清单 + 计算口径 + 推送目标
├── iolcon_constants.json    常数表（生成物）
├── pyproject.toml           依赖清单
├── install.sh               安装：生成配置与 systemd → 装依赖 → 启动
├── tools/
│   └── patch_config.py      按 key 改写 config.yaml（保留注释）
│
├── core/                    基础设施（零内部依赖，谁都能用）
│   ├── utils.py             日志、UID 派生、数值清洗、姓名拼装
│   ├── config.py            config.yaml + 常数表装载
│   └── patient.py           PatientID 生成
│
├── sources/                 取数
│   ├── mdb.py               IolmXP.MDB 解码 + 结构校验
│   └── constants.py         IOLCon XML → 常数表 JSON
│
├── formulas/                四公式
│   ├── common.py            公式共享工具
│   ├── srkt.py  haigis.py  hoffer_q.py  holladay1.py
│   └── calc.py              按型号编排：分配常数、收集档位表
│
├── render/                  出片
│   ├── sheet.py             计算结果 → 报告单结构（2×2 布局）
│   └── report.py            A4 竖版 200dpi 渲染
│
└── dicomlib/                DICOM 封装与推送
    ├── dicom.py             SC 晶体单图 + C-STORE
    ├── oam.py               Ophthalmic Axial Measurements Storage（AL + ACD）
    └── keratometry.py       Keratometry Measurements Storage（K / r / 轴位）
```

```
pipeline.py ──┬── sources/     ──┐
              ├── formulas/    ──┼── core/
              ├── render/      ──┤
              └── dicomlib/    ──┘
```

`core` 被所有人用但自己不依赖任何人；`pipeline.py` 在最外层，只负责串起来。

## 包名为什么这么取

| 名字 | 不用什么 | 理由 |
|---|---|---|
| `dicomlib` | `dicom` | `dicom` 是 pydicom 生态惯用名，栈回溯里分不清是 pydicom 还是本项目 |
| `sources` | `io` / `readers` | `io` 撞标准库 |
| `render` | `report` / `output` | 已有 `report.py`，且 `render` 明确是「出片」这一阶段 |

## 整条管线的运行逻辑

```
            ┌──────────────┐
            │  MDB 文件    │  唯一数据源
            └──────┬───────┘
                   │ 只监视 mtime
            ┌──────▼───────┐
            │  watch 循环  │  每 60s stat 一次
            └──────┬───────┘
                   │ mtime 变了 → 跑一轮
   ┌───────────────┼───────────────┐
   ▼               ▼               ▼
 取数            计算            出片
 MDB 解码      四公式×N型号    报告单+OAM
   │               │               │
   └───────────────┴───────────────┘
                   │
            ┌──────▼───────┐
            │  DICOM 封装  │  N 个 SC + 1 个 OAM
            └──────┬───────┘
                   │ 一次 C-STORE 关联
            ┌──────▼───────┐
            │     PACS     │
            └──────────────┘
```

**单一数据源：`IolmXP.MDB`。** 不读 `.iol`，不查 REST，不建映射缓存。

### 启动阶段（一次）

`pipeline.py` → `loop()` → `_prepare()`

1. `core.config.build()` 读 `config.yaml` + `iolcon_constants.json`（约 4 ms）
2. `_push_target()` 解析 host / port / aet，两级回落：命令行 > `config.yaml` 的 `push` 段
3. `_load_state()` 读 state.json，比对签名（签名 = 各型号常数表全称拼接）

启动日志：

```
常数表 646 款（IOLCon 2026-11-06）
口径：逐公式取 optimized > ulib > nominal 的第一个非空值
目标屈光 -0.5 D，步进 0.5 D
上表型号：ZCB00(n=1579) | MX60(n=765) | NS-60YG | Softec HD(n=5208)
常驻启动 | MDB=/storage/.../IolmXP.MDB | 间隔=60s
```

### 循环阶段

```
while True:
    sig = (mtime_ns, size) of MDB
    new_data = sig 变了 或 首次启动
    due      = failed 队列里有到期的重试
    if not new_data and not due:
        sleep(按最近一次 next 到期时间，不超过 interval)
        continue
    wait_settled()                 # mtime 变了 != 写完了
    wait_push_target()             # ★ 原地守候，直到 PACS 在线
    run_once()
    if 本轮没消费（MDB/PACS/结构受阻）:
        last_sig 保持不动           # 下次针对同一批数据重来
    else:
        last_sig = sig
```

**只认 mtime。** 不做目录扫描、不用 inotify（源在 CIFS 上不产生事件）。

**三个触发条件**：MDB 变了 / failed 里有到期的重试 / 首次启动（处理历史积压）。
签名 `last_sig` 只在**一轮真的消费过数据**之后才推进 —— 这是「开机时 PACS
比管线上线慢」不再丢单的根本。

### run_once() 的七道关

**关 1 · 库结构校验** — `mdb.assert_supported()`

必查 10 个字段：`family_name given_name birth_date exam_date al_r al_l r1_r r1_l r2_r r2_l`。
另读可选的 `acd_*` / `axis_*` / `patient_sex` / `guid`（缺则对应内容留空）。
缺一个就整轮退出。因为结构不对时 `access_parser` 会读出全 None，公式算出荒唐度数且不报错 —— 静默错数据比崩溃危险。

**关 2 · 推送目标预检（守候）** — `dicom.probe()` + `_wait_push_target()`

两级探测：TCP 连接（`connect_timeout`）+ C-ECHO（`cecho_timeout`，**必须
有界**，否则 pynetdicom 默认 30s 超时会让守候形同虚设）。三态：

| 状态 | 含义 | 处理 |
|---|---|---|
| `up` | TCP 通 + C-ECHO Status 0x0000 | 可推送 |
| `partial` | TCP 通，C-ECHO 被拒/超时 | **默认按可推送处理**（老 SCP 可能不实现 Verification）；`require_cecho: true` 时视为不可达 |
| `down` | 端口不通 | 不可推送 |

不可达时**不退出、不跳过**，而是原地守候：每 `watchdog.probe_seconds` 探一次，
通了立刻往下走，把积压一次补推。日志按 `log_every` 节流（否则一晚上刷几千行）。

为什么阻塞守候而不是退出重来：退出要靠 `Restart=always` 重启，`RestartSec=30`
意味着每 30s 冷启动一次（重载常数表、读全库），日志里只看到「服务在重启」，
看不出真正原因。

为什么等多久都不丢数据：每轮都是**基于全库重新筛选** todo（`state["done"]` 去重），
不做增量游标。

**关 3 · 全表读入** — `mdb.load_measurements()`

```
约 1.2 s → 全库 → 按 (姓, 名, 检查日期) 分组
```

每行拆成双眼 `{al, r1, r2, acd, axis, state}`。`num()` 把 0 当无效 —— MDB 未测出存的是 0 不是 NULL，直接采信会让 AL=0 时 SRK/T 吐出几百 D。自检行（姓名以 `!` 开头或含 `CHECK`）跳过。

**关 4 · 待办筛选**

```
取最新一行                     按 guid 内嵌创建时刻，**不是**物理末行
_record(row, name)             组装成内部记录（缺项不再丢整眼）
has_measurement() 为假 → 跳过  两眼既无 AL 也无 r1+r2，没有任何可录内容
done[key].fp == 当前指纹 → 跳过  比对内容，不是比对 key 存在与否
done 是 dry/empty → 清掉重来  从没推过实例，补指纹会永久冻结
done[key] 无 fp → 补写指纹后跳过 升级兼容，不触发全量重推
stale 里有该 key → 跳过         持续生效的人工闸门，直到内容回到已推版本
done[key].fp 不符 → 进 stale    设备补测过，留痕待人工确认，不自动重推
failed 的 perm_fp 与当前不符 → 解除永久失败，重新评估
failed 已发出过旧内容且内容变了/缺 fp → 进 stale（不普通重试）
failed 里有永久失败 → 跳过     永久失败不再重试
failed 退避未到期 → 暂缓
冷却期内 → 暂缓                 起点 = 行创建时刻（缺失时用本版首次被看到）
todo.sort(按日期倒序)           新检查优先
```

**关 5 · 逐例处理**

```
pid = patient.make_pid(dob, name)
calc.calc_record(rec, lenses)      # 缺 AL / 缺 K 的格就是 None
if calc.calcable(rec):             # 至少算得出一个度数
    for 每款晶体:
        sheet.build(rec, L)        → 报告单结构
        report.render_image(s)     → A4 图
        dicom.build_sc(...)        → SC 实例
if 任一眼有 AL:   oam.build(...)    → OAM 实例（只写有 AL 的眼）
if 任一眼有 r1+r2: ker.build(...)   → KER 实例（只写有 r 的眼）
```

**关 5.1 · 缺数据怎么办**

度数算不出（缺 AL 或缺 K）时**不出晶体单**，但结构化实例照发 ——
「只测眼轴、没测角膜曲率」的检查，眼轴本身是确定的实测值，不该因为
算不出度数就一起丢掉。这条以前是反的：`_record()` 里
`al/r1/r2` 缺一项就 `continue`，整只眼（含 AL、ACD、眼状态）
全部丢弃，实测 91% 行有 al/r1/r2 之外的残缺组合都受影响。

判据分三层，互不代偿：

| 判据 | 决定 |
|---|---|
| `calc.calcable(rec)` | 任一公式算出度数 → 才出 SC |
| `expected` vs `expect` vs `sent` | 生成阶段少一个就按失败退避（否则会被误标 done，缺的 series 永远补不上） |
| `calc.has_measurement(rec)` | 任一眼有 AL 或 r1+r2 → 才进 todo |
| `_fingerprint(rec)` vs `done[key].fp` | 内容变了才重推；变了先进 stale |
| `cooling_seconds` | 新检查/新内容等满窗口再推，避免推半成品 |
| OAM / KER 各自按眼判断 | 缺 AL 的眼不写 OAM；缺 r 的眼不写 KER |

series 号按**实际生成的实例**顺排（不出晶体单时 OAM 从 1 开始），PACS 里
不会看到跳号的 series。

**关 6 · 封装** — 见 [dicom.md](dicom.md)

**关 7 · 推送**

一次关联推完整个 study（N 个实例），省 N 次握手。成功判据是
`sent == expect`，其中 expect 是**本轮实际生成**的实例数
（`SC×n_sc + OAM + KER`），**少一个就算失败**，进 `failed` 队列按退避
重试（全量重推，不做部分成功）。

### 耗时实测

单例 4 款晶体（16 核 / Python 3.12 / pillow 13 / 本地 SCP）：

| 环节 | 优化前 | 优化后 |
|---|---|---|
| 四公式计算 | 1.8 ms | 1.8 ms |
| 报告结构组装 | 0.01 ms × 4 | 0.01 ms × 4 |
| 渲染（字号自适应） | 18 ms × 4 | 18 ms × 4 |
| ~~PNG 编码 + 落盘 + 读回~~ | ~~68 ms × 4~~ | **0** |
| ~~PNG 解码（build_sc 内）~~ | ~~34 ms × 4~~ | **0** |
| JPEG 编码（q92） | 10.5 ms × 4（optimize=True） | **6.4 ms × 4**（optimize=False） |
| **渲染 + 封装小计** | **约 595 ms** | **约 136 ms** |
| OAM 封装 | 2.1 ms | 2.1 ms |
| KER 封装 | 0.6 ms | 0.6 ms |
| **推送（5 实例）** | **480 ms** | **480 ms** |
| **单例合计** | **约 1376 ms** | **约 406 ms** |

瓶颈是**推送的网络等待**（480 ms，占优化后的 68%），不是计算，也不是渲染。

**为什么砍掉 PNG 中转**：最终塞进 DICOM 的是 JPEG，PNG 从头到尾没人要。
`render_png()` 把 Image 编码成 PNG、写磁盘、读回字节，`build_sc()` 再解码回
Image —— 中间两次编码纯属白做。DICOM 路径现在直接走
`report.render_image()` → `build_sc(image)`，省掉 68 ms 编码 + 34 ms 解码 + 磁盘往返。

`render_png()` 保留给 `pipeline.py render` 子命令（那里确实要 PNG 落盘）。

**`optimize=False` 的取舍**：体积 +17~22%（每张约 +55 KB），换 39% 的编码提速。
JPEG q92 已远高于报告单可读需求，多出的 55 KB 对 PACS 存储无实际影响。

### 并发处理

`run_once` 用 **`ProcessPoolExecutor`** 并发，默认 4 进程（`config.yaml` 的
`performance.workers`，`--workers` 可覆盖）。

**为什么是进程不是线程**：链路优化掉 PNG 中转后，剩下的全是 FreeType 文本绘制
+ JPEG 编码 + pydicom 封装，**一点 GIL 都不放**。实测线程池 **0.83×**（比串行
还慢），进程池 **4.13×**。

实测（40 例，4 款晶体，含真实 C-STORE 推送）：

| workers | 单例 | 加速比 | 全量 |
|---|---|---|---|
| 1（串行） | 572 ms | 1.00× | 2.4 h |
| **4（默认）** | **178 ms** | **3.21×** | **45 min** |
| 8 | 122 ms | 4.69× | 31 min |

瓶颈已是**推送的网络等待**，所以 4→8 只有 1.46×，不是线性的。

**并发下的三个不变量**（改动前请先读）：

1. **state 的读-改-写加锁**。`done`/`failed` 的更新与 `% 25` 落盘在同一个
   `threading.Lock` 内 —— 否则并发下会丢更新，且 `(ok+fail) % 25` 可能永远
   不命中。`_save_state` 的 `.tmp`+`fsync`+`os.replace` 本身原子，不需要额外保护。
2. **`datasets` 不可跨线程/进程共享**。`push_cstore` 里 `_to_native_le(ds)` 会
   **就地改写** `file_meta` 与像素，一个调用方的 fallback 会污染另一个正在推的 ds。
   当前"一例一个 worker 推完整 study"的粒度天然安全，别改成按实例并发。
3. **预算检查在提交点**。`todo` 按日期倒序，`fill()` 只维持 `workers` 个在飞，
   所以新检查不会被老病例堵住。墙钟会超出 `max_seconds` 约一个单例时长。

**子进程崩溃的降级**：OOM killer 杀进程或段错误会让 `ProcessPoolExecutor` 进入
`BrokenProcessPool` 且**永久不可用**（之后任何 `submit` 都抛）。代码捕获它，
把在飞任务记入 `failed`（下轮重试），**剩余任务改为主进程串行跑完**。
实测 4/40 子进程被杀时，36 例成功、4 例进 failed、0 例丢失。

**日志**：子进程把日志攒成行返回，主进程按完成顺序统一 `print` —— 避免多进程
写同一 stdout 交错。实测 8 进程 43 行日志，零撕裂。

全库回填：**串行 2.4 小时 → 默认 4 进程约 45 分钟**（链路优化前 4.8 小时）。

> 实测环境：Python 3.12 / pillow 13 / pynetdicom 3.0.4 / 16 核，本地 pynetdicom SCP。

### 失败处理分层

| 层级 | 行为 |
|---|---|
| 库结构不对 | 本轮不消费，签名不推进，下轮重试 |
| MDB 读不出来 | 同上（本轮空转，绝不推进签名） |
| 推送目标不可达 | **原地守候**，每 `probe_seconds` 探测，通了立刻补推 |
| 单例计算失败 / 库里没生日 | 进 `failed` 标 `permanent`，不再重试（重试一万次也不会有不同结果） |
| 单例渲染失败 | 该款晶体跳过，其余继续；整例失败则按退避重试 |
| 部分实例推送成功 | 整例算失败，按退避**全量**重推 |
| `failed` 指向的检查已不在库中 | 从队列清理（设备归档后无从重试，留着会让「到期重试」永远为真） |

**单例退避**：`state["failed"][key] = {t, err, tries, next}`，第一次失败等
`retry.initial_seconds`（默认 60s），之后每次 ×2，封顶 `retry.max_seconds`
（默认 900s）。PACS 半通（端口开、关联被拒）时不会每轮把几百例重渲一遍。

**签名不推进的意义**：`run_once()` 返回 `blocked`（"pacs"/"mdb"/"schema"）时，
`loop()` 不更新 `last_sig` —— 下次针对同一批数据重来。这是修掉「开机时
DICOM 服务端起得比管线慢，管线发现不可达后直接没下文」的那一刀。

## 设计取舍

| 决策 | 理由 |
|---|---|
| 只认 MDB | `.iol` 没有散光轴位；MDB 一次检查一行、含轴位 |
| PatientID 本地生成 | 不需要 REST、不需要缓存、重复推幂等 |
| 眼部参数只进 OAM | 标签区和图各存一份会不一致，PACS 分不清权威 |
| OAM 用标准 IOD | 不自造 XML，任何支持该 SOP 的 PACS 都能读 |
| 眼状态原文直封 | 映射必然要猜（「硅油充填眼」没说晶状体），猜错比不猜糟 |
| 一次关联推 N+1 实例 | 省 N 次 DICOM 握手 |
| 全有或全无的推送判据 | 部分成功会让 PACS 里出现半个 study |
