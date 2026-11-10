# MDB 解码

## 数据源

设备活库 `IolmXP.MDB`（Jet 4 / Microsoft Access 2000 格式），
用 `access_parser` 纯 Python 读取，不依赖 JRE 或 ODBC。

表：`IolmMeasurements`，一次测量一行。

## 字段清单

| 字段 | 含义 | 实测有值率 |
|---|---|---|
| `family_name` / `given_name` | 姓 / 名 | ~100% |
| `birth_date` / `exam_date` | 生日 / 检查日期 | ~100% |
| `al_r` / `al_l` | 眼轴 mm | 91% |
| `r1_r` `r1_l` / `r2_r` `r2_l` | 角膜曲率半径 mm（**不是 K**） | 91% |
| `acd_r` / `acd_l` | 前房深度 mm | **仅 33%** |
| `axis_r` / `axis_l` | 角膜散光轴位（K1 的轴） | 91% |
| `state_r` / `state_l` | 晶状体 / 玻璃体状态，中文自由文本 | 44% |
| `patient_sex` | 性别 → DICOM PatientSex | **实测全空** |
| `lens_thickness_*` | 晶状体厚度 | **全为 0** |
| `used_r` / `used_l` | — | **全空** |
| `guid` | 记录 ID | 100% |

全库实测。`acd` 只有三分之一有值，不是读取错误 —— 设备只在这部分检查里测 ACD。
`patient_sex` 字段确实存在，但实测库里**全部为 NULL** —— 这台设备
从来不写这一列。本项目照样读、照样往下传，并在 DICOM 里写成 `PatientSex`；
值是空的时候标签就留空，不猜、不填 `O`。设备哪天开始写了，不用改代码就能落库。

性别归一化在 `core.patient.norm_sex()`：`M/m/Male/男/1` → `M`，
`F/female/女/2` → `F`，`O/Other/未知/unknown` → `O`，
**认不出的值返回空串**。把 `unknown` 硬映射成 `O`（Other）会让 PACS 端
把「没录」误读成「确实是不明性别」，比留空更糟。
`lens_thickness` 全 0 意味着 Haigis 的晶状体厚度修正常数用不上（本项目也不用）。

## 关键换算

### K = 337.5 / r

`.iol` 和 MDB 都只存**角膜曲率半径 r (mm)**，不存 K (D)。

```
K(D) = 337.5 / r(mm)
```

这个 337.5 是角膜折射率 1.3375 的百分之一，也是 IOLMaster 打印机的换算基准。
字段换算应使用设备记录的半径与项目约定的换算常数逐项核对；文档不保留真实报告单数值。

> ⚠️ 历史 bug：曾写成 `0.3375 / r`，差 1000 倍，度数全错且不报错。
> 用 5 例实测 K 回归才发现（最大偏差 0.02 D 才对）。

### 0 不是 NULL

**MDB 里未测出的字段存的是 0，不是 NULL。** 直接采信会让公式算出荒唐结果 ——
AL=0 时 SRK/T 会吐出几百 D。

`core/utils.num()` 把 `0` 和 `"0.0"` 一律当无效值：

```python
def num(v):
    s = str(v).strip()
    if s in ("", "0", "0.0", "0.00", "None", "NULL", "-"):
        return None
    ...
    return None if f == 0.0 else f
```

## 分组逻辑

按 `(姓, 名, 检查日期)` 三元组分组，同一天多次检查保留全部，取 `rows[-1]`
（最后一次）出单。

自检行（姓名以 `!` 开头，或含 `CHECK`）跳过 —— 那是设备开机自检写进去的，
不包含真实测量。

## 为什么不用 `.iol` 文件

设备每次检查还会导出一个 `.iol`，但有两个硬伤：

1. **`.iol` 里完全没有散光轴位**，轴位只能从 MDB 补。
2. **`.iol` 的公式表是厂商算的**，本项目要自己的四公式，还得重新算一遍。

而 MDB 一次检查一行、含轴位、天然可按日期筛选。以 MDB 为唯一数据源更干净。

## 落盘判定：mtime 变了不等于写完了

`watch` 靠 mtime 判断有没有新检查，但**设备写 Access 库时 mtime 先变、
页还在刷**。此时去 `access_parser` 解析，最坏情况会读出半写入的浮点字节，
然后按错误度数出单推给 PACS —— 而且不会报任何错。

所以读之前先等文件「指纹」稳定（`sources/mdb.wait_settled`）：

```
指纹 = (st_mtime_ns, st_size)
连续 settle_seconds 秒指纹不变  ->  认为写完，可以读
超过 settle_timeout_seconds 还在变  ->  本轮跳过，下轮再看
```

用 `st_mtime_ns` 而不是 `st_mtime`：秒级精度下同一秒内的两次写入指纹相同，
会被误判成"已稳定"。size 一起比是因为原地改写页时某些挂载的 mtime 精度粗，
但页数变化会体现在 size 上。

配置在 `config.yaml` 的 `watcher.settle_seconds`（默认 5）与
`watcher.settle_timeout_seconds`（默认 120）。代价是每次 MDB 更新后
出单延迟多 5 秒。

**为什么不用 `.ldb` 锁文件**：设备机器开着，`.ldb` 就一直存在。它标记的是
"库被 Access 引擎打开"，不是"正在写"，无法区分空闲与写入。

实测行为（`sources/mdb.wait_settled`）：

| 场景 | 结果 |
|---|---|
| 文件静止 | settle 秒后判定稳定 |
| 持续写入 | 超时返回 False，本轮跳过并打日志 |
| 写入停止后 | settle 秒后判定稳定，恢复处理 |
| 文件不存在 | 立即返回 (False, None) |
| 同秒内两次写入 | mtime_ns 能区分 |
| size 不变但内容变 | mtime_ns 能区分 |

读库失败（`load_measurements` 抛异常）现在显式返回空统计并打日志，
不再冒泡到 `loop()` 的 `except` —— 否则只会打一行 traceback，
看不出"这一轮白跑了"。

## 结构校验

启动时 `mdb.assert_supported()` 检查 10 个必需字段。

| 层 | 字段 |
|---|---|
| 必需（缺一即停） | `family_name given_name birth_date exam_date al_r al_l r1_r r1_l r2_r r2_l` |
| 可选（缺则跳过对应内容） | `acd_r acd_l`（缺 → Haigis 不出）<br>`axis_r axis_l`（缺 → 报告单无轴位）<br>`patient_sex`（缺或空 → PatientSex 留空）<br>`guid` |

**为什么必须校验**：IOLMaster 700 用的是 `CZM_*.MDB` 系列（表名 `GeneralStudy_M` 这类
DICOM 风格），结构完全不同。指错了库时 `access_parser` 要么抛
`'NoneType' object has no attribute 'parse'`、要么读出全 None，然后公式算出
荒唐的度数 —— **而且不会报任何错**。

实测拦截效果：

```
✗ 库结构与本项目验证过的 IOLMaster 500 结构不符，已停止。
  库文件   : /path/CZM_latest.MDB
  表       : IolmMeasurements
  原因     : 表 IolmMeasurements 不存在
  存在的表 : ClinicalTrialSeries_M, GeneralStudy_M, GeneralSeries_M, ...
  缺少字段 : family_name, given_name, birth_date, exam_date, al_r, al_l, ...
```

## 眼状态字段的坑

`state_r` / `state_l` 是操作员录入的中文自由文本，实测 13 种取值，
术语完全不统一：

```
有晶状体 / None / 硅油充填眼 / 硅油充填眼，有晶状体 / 无晶状体
硅油填充眼，无晶状体 / Phakic / 无晶状体硅油填充眼
人工晶状体PMMA / 假晶状体硅油充填眼 / 硅油充填眼，人工晶状体
人工晶状体丙烯酸酯
```

连「充填」和「填充」、「人工晶体」和「人工晶状体」都混用。

**处理方式：原文直封进 DICOM，不做翻译。** 映射必然要猜（「硅油充填眼」
没说晶状体状态，「人工晶体」少个「状」字），猜错比不猜更糟。详见
[dicom.md](dicom.md#眼状态原文直封不做翻译)。

报告单上的「眼状态」行同样印原文；设备未填时印 `None`
（不是「测不出」—— `测不出` 是留给 AL/K/ACD 这类仪器测不出来的场景）。

## 数据质量备忘

| 问题 | 影响 | 处理 |
|---|---|---|
| 空检查（三次测量全失败、8 张公式表全空） | 出单通篇「测不出」无临床价值 | 判据：双眼全无 AL/K/ACD → 跳过 |
| 姓名可能被录成拼音 | DICOM PN 拼装规则不同 | `is_cjk()` 判断字符而非字段位置 |
| 同一患者同日多次检查 | 会重复出单 | 取最后一次 |
| 生日常见 1909-09-09 这类占位值 | PatientID 撞车 | 无解，只能靠姓名摘要区分 |
