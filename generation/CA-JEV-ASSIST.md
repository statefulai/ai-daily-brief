# Jev 辅助打分（操作说明）

Jev 是 TypeSafe 提供的 AI 评估模型。本项目把已采集的候选新闻和既往报道交给它，辅助判断“有没有新变化、值不值得读”。生成日报的 Cloud Agent 仍负责核实来源和最终选稿；Jev 出错或额度用完时，日报继续原有流程。

官方说明：https://docs.typesafe.ai/introduction

本文件是调用方式、密钥、关闭开关和额度交接的唯一说明。Jev 分数不得单独入选、淘汰或裁定 `no_new_value`。

## 关闭（唯一正式入口）

**Canonical kill switch: `JEV_ASSIST=0`.**

代码在任何 store I/O 或 HTTP 之前检查 `JEV_ASSIST`。`0` / `false` / `off` / `no` / `disabled` 关闭辅助，日报继续原候选。

仅当未设置 `JEV_ASSIST` 时，下列别名才生效（第二入口，不是操作员主开关）：`JEV_ASSIST_DISABLED=1`，或 `config.yaml` 里 `jev.enabled: false`。关闭请只用 `JEV_ASSIST=0`。

## 如何调用

合并同事件并检查必填字段之后、回源核验和最终选稿之前：

```
python scripts/jev_assist.py \
  --candidates output/cloud-agent-check/candidates.json \
  --out output/cloud-agent-check/jev-assist.json \
  --editions editions
```

日刊交接额度时，再加 `--store <CA 工作副本路径>`（见下方）。不要依赖两个 CA 都写默认 `runs/jev/`。

打印本页：`python scripts/jev_assist.py --print-ca-notes`。

## 密钥

代码只读环境变量 `TYPESAFE_API_KEY`。

生产环境在 **Cursor Cloud Agents → Secrets → Runtime Secret** 绑定同名 `TYPESAFE_API_KEY`。优先绑到本仓库的 saved Environment。日刊 Bot 的 `box-secrets` **不会**注入 CA 虚拟机；密钥只对**之后新启动**的 CA 生效。不要把密钥写进 prompt、uploads、`editions/` 或本仓库。

## 额度交接（真实落地）

北京自然日 `00:00–24:00`（`Asia/Shanghai`），全链路最多 **100** 次真实 HTTP。失败和重试都计数。默认不自动重试，无自动加购。不要把配额写进 `editions/` 或投递账本。

### 本仓库代码如何找文件

1. `--store` / `jev.store_path` / `JEV_ASSIST_STORE`：完整文件路径。CA 工作副本走这里。
2. `AI_DAILY_PRIVATE_RUN_DIR`：目录，文件为 `$AI_DAILY_PRIVATE_RUN_DIR/jev/quota-<北京日期>.json`。只有该目录对**当前进程**真的可读时，才是持久源。
3. 默认 gitignored `runs/jev/quota-<北京日期>.json`：只续跑**同一次工作区 / 同一次 CA**。新虚拟机里这个路径是空的。

缺文件、损坏、空文件、无权限、只剩 lock、锁失败：跳过 Jev，记 `store_unavailable`，继续原选稿。**不得从零再计 100 次**。

### 日刊生产：机上持久 + `files` 附带

持久源只在日刊机：`delivery/jev-private/jev/quota-<date>.json`。日刊机把该目录当作 `AI_DAILY_PRIVATE_RUN_DIR`，**不**把该目录挂进 CA 虚拟机。

跨 CA 唯一有效路径：

1. 日刊 `prepare_for_ca_launch()` 读 store + `meta-<date>.json`，决定本次是否给 Jev。
2. `mark_launch(agent_id)` 把 meta 标为 `pending_ingest`。
3. 启动 Cloud Agent 时用 `files` 附带额度文件；CA 拷到 `--store` 工作副本后再调用上面的命令。
4. CA 终报带回 `jev_quota_store`。
5. 回收成功：`ingest_from_ca_json`（拒绝 `request_count` 回退，合并 `evaluations`）→ meta=`ok`。
6. 回收失败：`mark_ingest_failed` → meta=`failed_ingest`。

`pending_ingest` / `failed_ingest` / store 不可读时，同日再 launch 必须 **skip_jev**，禁止按缺文件从零开计。

两个独立 CA 配置相同的 `runs/jev/` **不等于**已经共享状态。CA 内默认 `runs/jev` 只是同次工作副本。仓内若写「跨 CA 须 `AI_DAILY_PRIVATE_RUN_DIR`」，日刊落地是「机上持久 + `files` 附带」，不是「两台 CA 配同一个路径」。

### 复用（省额度，不是新请求）

同一事件在事实未变时复用上次结果：仅事实顺序变化、重复相同事实、或事实不变只改摘要措辞 → 复用。出现实质性新事实或事实更正 → 重新评估。用于判断“有没有新变化”的内容必须进入稳定的 `facts` 字段；摘要里的新信息会先折入 `facts`，避免误复用。

## 日刊 / CA 可粘贴模板

Use after gather + merge same-event + required fields, before re-verify and
final pick. Production launch today: Cursor `CloudAgent` `action=launch`,
title `YYYY-MM-DD-ai-daily`, repo `https://github.com/statefulai/ai-daily-brief`,
`starting_ref` usually `main` (SHA pinned in the prompt).

```
合并同事件并检查必填字段后，调用：

python scripts/jev_assist.py \
  --candidates output/cloud-agent-check/candidates.json \
  --out output/cloud-agent-check/jev-assist.json \
  --editions editions \
  --store <日刊 files 附带后的工作副本>

Jev 只辅助两个维度：相对既往公开报道是否有明确新事实、对目标读者的阅读价值。
分数不得单独入选、淘汰或裁定空刊；不得用高分跳过回源核验，也不得用低分写成 no_new_value。
关闭开关只认 JEV_ASSIST=0（在任何 HTTP 之前检查）。
缺 TYPESAFE_API_KEY、JEV_ASSIST=0、配额用尽、超时、鉴权失败、限流、坏响应或私有配额状态读不回（缺文件、损坏、无权限、锁失败）时：跳过 Jev，继续原选稿。不得把读不回的状态当成当日配额从零开始。
默认 runs/jev 只用于同一次 CA 的工作副本，不是跨 CA 共享。跨 CA 由日刊在机上持久 delivery/jev-private，经 CloudAgent files 附带后再用 --store；不要假设两台 CA 配相同路径就已经共享。新虚拟机拿不到这份状态就跳过 Jev。
未打分候选仍可入选。Jev 失败不是「今天没有新闻」。零入选仍走既有同次补查规则，不循环凑稿。
密钥只来自 Cloud Agents Runtime Secret TYPESAFE_API_KEY；日刊 box-secrets 不会注入 CA 虚拟机。
```
