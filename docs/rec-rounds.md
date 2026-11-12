# 补测轮次与升级说明

## 行为

同一 guid|exam_date 的测量内容改变后，在原 study 新增 Rec 2、Rec 3，
保留明确标注的历史轮次。原轮次保持原名称；后续所有 SC/OAM/KER 的
SeriesDescription 均加 Rec N，例如 IOL Repo DCB00 Rec 2。
实例数相同也检测内容变化。只使用 DICOM C-STORE，不访问 REST、不删除 PACS。
失败重试复用同一轮 UID 与已冻结载荷，不因重启或网络重试增加轮次。

## 为什么不能直接重发

同 SOP UID 的不同内容可能被 PACS 去重并返回成功，仅重新发送或修改名称
无法确保新测量入库。旧实现因此使用持续生效的 stale 闸门。
新实现为补测轮次派生新的 Series/SOP UID，并保存不可变载荷以保证重试幂等。

原 study 与第一轮 UID 规则保持不变，无需修改旧轮次或提高全局 UID 版本。

## 持久化与失败保护

1. KEY_SCHEME、INSTANCE_LAYOUT、PRIVATE_CREATOR、OAM/KER LAYOUT_VERSION
   及 state.sig 公式保持不变。旧 done 无 revision 按 1，不重推未变记录。
   保留旧 uid_seed，包括缺失/空串，确保补测仍在原 study。
2. 发送前持久化 pending，冻结轮次、输入、配置、指纹和目标。worker 只生成
   完整文件，原子发布并 fsync 到 state.json.spool；父进程校验身份、study、
   哈希和 UID 唯一性，再持久化 ready 后才发送。
3. 只有预期全部不同 SOP 明确返回 0x0000，才持久化新的 done 与载荷证据，
   并在同次原子提交中清 stale/pending/failed。OK 日志在提交后输出。
4. 部分接收、断连和响应丢失保留旧 done、stale 与 pending，按退避重放相同
   载荷。源数据又变化时先补齐冻结轮次，再生成下一轮。
5. 写盘失败停止发送并退出；坏载荷、属性修正 Warning、身份或 study 冲突
   暂停告警。归档不能证明收敛，不自动清除 stale 或接收证据。
6. watch/run 持有同一 state 的独占锁。损坏 state、丢失 state 且存在 spool、
   非空历史签名变化均安全停止，禁止自动重置。

没有 PACS 删除路径，也不新增配置项。只有 done 已持久化后才清理该轮本地
spool；本地清理失败不影响 PACS，保留载荷不会产生新轮次。

## 旧 stale 自动迁移

触发条件、成功判据、失败状态与运维操作见
[旧版补测 stale 自动迁移](operations.md#旧版补测-stale-自动迁移)。
普通旧 stale 即使缺 from、revision 或 uid_seed，也不需手动删 state。
watch 启动扫描可为有效变化记录自动分配 Rec 2；实例数不变不影响触发。
全部实例成功且台账提交后，才逐例清 stale。其他未变 done 不重推。

## 验证

在项目根目录运行：

    .venv/bin/python -m unittest discover -s tests -p test_revisions.py -v
    .venv/bin/python -m unittest discover -s tests -v
    .venv/bin/python -m compileall -q pipeline.py core dicomlib tests
    git diff --check

tests/test_revisions.py 使用合成记录、虚构常数和真实 DICOM 构建器，图像
渲染使用小图替身。PACS 替身按首个 SOP 永久保留，后续同 UID 返回成功，
测试直接检查保存的 OAM/KER 新测量值。另用本地随机端口 DICOM SCP 验证
真实 C-STORE 客户端；不依赖真实设备 MDB、生产 PACS 或患者数据。

覆盖内容：

- 实例数相同的数值改写和补另一只眼，SC/OAM/KER 同轮命名与 UID 隔离。
- 大批历史 done 中仅变化记录发送，旧格式 stale 自动 Rec 2，旧对象不变。
- 部分接收、响应丢失、提交失败后重启复用同轮载荷，不产生重复实例。
- 生成、写盘、载荷校验、Warning、目标切换和身份冲突的安全失败与告警。
- 子进程恢复、配置冻结、限额调度、独占锁、dry-run 和只读 audit。

依赖外部 MDB fixture 的旧测试在缺少 IOL2DCM_TEST_MDB 时明确跳过，不能
将跳过当作通过；其中旧人工闸门的历史期望由独立合成轮次测试覆盖。

## 运维边界

- C-STORE 逐实例发送，没有 study 原子提交。中断期间可暂显不完整 Rec N，
  该轮仍 pending，补齐前不记 done。
- C-STORE 接收确认不是远程逐字节核验或 Storage Commitment。无远程读取
  无法发现外部删除、改写或 PACS 回滚；audit 只核对 MDB 与本地账本。
- 历史轮次增加 PACS 空间。未完成 spool 含敏感信息，应保护权限、监控磁盘，
  并与 state 一致备份；空 spool 目录是 state 丢失保护标记，勿删。
- 不可单独回退 state 或直接降级不理解轮次的旧程序，避免复用已有 UID。
- 缺身份、空测量或无法重构的旧部分接收仍需排障，不能删除账本强行清警。
