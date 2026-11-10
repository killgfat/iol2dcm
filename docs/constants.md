# 常数表

## 公式名口径

一律按 IOLCon 写法，全链路统一（报告单、XML、state、日志）：

| 公式 | 常数 | IOLCon XML 标签 |
|---|---|---|
| SRK/T | A | `<SRKt>` |
| Haigis | a₀ / a₁ / a₂ | `<Haigis><a0/><a1/><a2/>` |
| Hoffer Q | pACD | `<HofferQ>` |
| Holladay 1 | SF | `<Holladay1>` |

## 数据源

只用一处：IOLCon 的匿名 XML 全表导出

```
https://iolcon.org/downloadLenses.php?action=download
```

约 1 MB / 646 款 / 下载约 100 秒。**不要抓网页**——项目早期抓 `printLenses.php`
解析 HTML，页面改版就崩，而且慢。

```bash
python3 pipeline.py constants                  # 联网抓取并重建
python3 pipeline.py constants --from-xml x.xml # 离线解析本地 XML
python3 pipeline.py constants --show "Nidek SZ-1"  # 只看某款
```

## 来源优先级

同一款晶体在 IOLCon XML 里可能有三个 `<Constants>` 块，**逐公式**取第一个非空值：

```
optimized  >  ULIB  >  nominal
```

**逐字段合并，不是整块二选一。** 有些晶体 optimized 只给了部分公式，ULIB 反而给了完整四个；整块选择会丢掉能拿到的值。

生成的 JSON 里每款都记录 `sources`，逐公式标明取自哪个来源：

```json
{
  "key": "Johnson and Johnson Vision ZCB00",
  "srkt_a": 119.4142,
  "haigis": {"a0": -1.1426, "a1": 0.2331, "a2": 0.2428},
  "hoffer_q_pacd": 5.8135,
  "holladay1_sf": 2.0582,
  "results": 1579,
  "sources": {
    "SRK/T": "optimized",
    "Haigis": "optimized",
    "Hoffer Q": "optimized",
    "Holladay 1": "optimized"
  },
  "missing_formulas": [],
  "complete": true
}
```

## 实测覆盖率

```
646 款 | 四公式齐全 578 / 缺值 68
字段来源：optimized 152 款 / ulib 60 款 / nominal 414 款
```

大部分晶体只有厂商名义值（nominal），有优化数据的不到三分之一。这是 IOLCon 的临床样本量决定的，不是抓取问题。

## 缺值处理

某个公式在三个来源里都拿不到 → 该字段写 `null`，`missing_formulas` 列出，报告单该格显示「该公式未计算」。

**不做任何换算兜底。** 用 A 常数反推 pACD 这类操作会隐式改变口径，日后无法追溯。

## 换晶体型号

只改 `config.yaml`：

```yaml
lenses:
  - name: ZCB00                              # 显示名（系列描述、报告单）
    key: Johnson and Johnson Vision ZCB00    # 常数表全称，大小写敏感
```

`key` 写全称最稳。也可以只写型号（如 `SZ-1`），唯一命中即可；**多命中会报错并列出候选**，不猜。

### 型号名差一个字母就是另一个晶体

| 写法 | 实际型号 | Haigis a₀ | 样本量 |
|---|---|---|---|
| `AR40e` | Sensar **1-piece** | **+0.472** | 368 |
| `AR40E` | Sensar **3-piece** | **−2.42** | 653 |
| `Softec HD` | 有 optimized 常数 | — | 5208 |
| `Softec HDO` | 另一个型号，无 optimized | — | — |

匹配器按「精确 > 型号精确 > 忽略大小写 > 型号忽略大小写 > 子串唯一」分级，
大小写敏感优先，避免 `AR40e` 静默取到 `AR40E` 的常数。

### 同名不同款：必须用 @id 指定

IOLCon 上有 7 组 key 完全相同、但 id 与常数不同的条目：

| key | id | A 常数 | Haigis a0 |
|---|---|---|---|
| `Ophtec AC 205` | 653 | 115.7 | −0.16 |
| `Ophtec AC 205` | 654 | **116.9** | −0.25 |

差 1.2 的 A 常数会算出不同的度数，而报告单上看不出来。所以匹配器遇到
同名多条目会**报错**，而不是取第一个：

```
✗ 'Ophtec AC 205' 对应 2 个同名条目，常数不同，不能自动选：
    id=653 A=115.7 a0=-0.16, id=654 A=116.9 a0=-0.25
  请在 config.yaml 里写成 Ophtec AC 205@<id> 指定其中一个。
```

指定方式：

```yaml
lenses:
  - name: AC205
    key: Ophtec AC 205@654
```

⚠️ 分隔符是 `@` 不是 `#` —— YAML 里 `#` 是注释起始符，
`Ophtec AC 205#654` 会被截成 `Ophtec AC 205`，写进 config 等于白写。

## 本地编码约定

`config.yaml` 里的 `constants_file` 指向 JSON。装到 `/opt/iol2dcm` 后：

```
/opt/iol2dcm/iolcon_constants.json     ← 生成物，可随时重建
```

`constants_file` 是**相对 config.yaml 所在目录**解析的。config 在
`/opt/iol2dcm/`，常数表就必须也在那里 —— 两个一起挪，别只挪一个。

## 何时更新、怎么更新

| 场景 | 做法 |
|---|---|
| 首次安装 | `install.sh` 从 iolcon.org 拉（无表且拉不到就拒绝安装） |
| 之后想更新 | `$P pipeline.py constants`，然后 `systemctl restart iol2dcm` |
| 只想查型号 | `$P pipeline.py constants --show <关键词> --local`（毫秒，不联网） |
| 离线机器 | 手工拷入 JSON，或 `--from-xml` 解析手工下载的 XML |
| 装的时候不想拉 | `install.sh --skip-constants` |

### 下载速度：必须走 gzip

`fetch_xml()` 发 `Accept-Encoding: gzip`。这不是优化，是必需：

| 方式 | 耗时 | 传输量 |
|---|---|---|
| 不发 gzip（旧实现） | **93 秒** | 1.1 MB |
| 发 gzip（现在） | **1.5~10 秒** | 34 KB |

iolcon.org 出数据只有 ~12 KB/s，裸传 1.1MB 的 XML 要一分半；gzip 把传输量
压到 34KB，快 10~30 倍。`--save-xml` 可以另存原始 XML（1.1MB），
以后想取 `Specifications` 里那 21 个字段（Toric / 材质 / 直径…）不用重新下载。

### 更新时怎么知道上游变了

每款晶体的 JSON 记录里带一个 `fp` 指纹（四公式数值的 sha1 前 12 位）。
更新后自动对比旧表并打印：

```
  与旧表相比：新增 3 / 变更 2 / 移除 0 / 不变 641
    + Newlens XY-1#2310
    ~ Johnson and Johnson Vision ZCB00#696
```

以前只有 `count` 相同就当没事发生 —— 实际上某款常数可能被悄悄修正了，
算出来的度数会变，而报告单上完全看不出来。

差异按 `key@id` 比对而不是按 key：IOLCon 上有 **7 组同名不同 id** 的条目
（`Ophtec AC 205` 的 id 653 A=115.7 / id 654 A=116.9 是两款晶体），
按 key 归并会把 646 款压成 639。

**没有定时任务。** 常数表装好就固定下来，不自动更新 —— 晶体常数一年也未必
变一次，为了那点变化每月半夜拉表并重启服务不划算，还多一个会悄悄改数据的
活动部件。要更新就手工跑一次，跑完记得重启：

```bash
cd /opt/iol2dcm
./venv/bin/python pipeline.py constants    # 拉新表
systemctl restart iol2dcm                  # ★ 不重启不生效
```

**为什么必须重启**：`loop()` 只在进程启动时读一次常数表（`_prepare()`
之后就常驻了），常驻循环不会重读。所以 `pipeline.py constants` 单独跑完，
正在运行的服务用的还是内存里那份旧表。

两个保护：

* **款数骤减保护**：新表不足旧表的 50% 时拒绝替换（`--min-ratio`）。
  IOLCon 页面改版会让解析出一堆残片却不报错，没有这道闸门就会把好表冲掉，
  整条管线随即因为「型号在常数表里找不到」而全线停摆。
* `atomic_write`：先写 `.tmp` 再 rename，拉一半断网不会留下半个 JSON。
  拉取失败时旧表原样保留，服务不受影响。

### 更新会影响已推送的病历吗

**不会。** state 的签名只含型号 key，不含常数数值：

```python
sig = "|".join(L["key"] for L in lenses)
```

所以常数表更新后 state 不重置，已推送的历史病例保持当时的计算结果，只有
**新检查**用新常数。这是刻意的：临床上可能已经有手术决定基于那份单，
不该被一次常数表更新悄悄改写。

确实需要按新常数重算历史病例时，手工来：

```bash
systemctl stop iol2dcm
mv /var/lib/iol2dcm/state.json /var/lib/iol2dcm/state.json.old
systemctl start iol2dcm            # 空 state → 全量重推，15000 例约 4.8 小时
```

注意 PACS 对同 SOP UID 会静默去重，内容改版还要提升
`dicomlib/dicom.py` 里的 `PRIVATE_CREATOR` 版本号。

## 合规

IOLCon 条款 8.1 只允许为自身计算目的使用数据、禁止再分发。
生成的 JSON 是本机自用缓存，不进公开仓库、不转交第三方。
