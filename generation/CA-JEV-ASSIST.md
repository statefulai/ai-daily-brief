# Jev assist (operators + day-刊)

Assist-only scoring after same-event merge and before source re-verification / final selection.
This file is wording and ops notes for the code PR. It does not change day-刊's
`delivery/CA-GENERATE-TEMPLATE.md` (that update stays out of this repo).

## Operator steps

1. **Key.** Code reads **only** the environment variable `TYPESAFE_API_KEY`.
   Bind it as a Cursor **Cloud Agents → Secrets → Runtime Secret** named
   `TYPESAFE_API_KEY`. Prefer a saved Environment bound to
   `statefulai/ai-daily-brief` (day-刊 will later pass
   `environment: {"type":"environment","name":"…"}` on `CloudAgent` launch).
   Alternate: apply the secret to this repo only.
2. **Secrets apply only to new CA runs.** Day-刊 Bot `box-secrets` do **not**
   inject into Cursor-managed CA VMs. Do not put the key in prompts, uploads,
   `editions/`, or this repo.
3. **Disable.** `JEV_ASSIST=0` (or `JEV_ASSIST_DISABLED=1`, or
   `jev.enabled: false` in `config.yaml`). When off, no Jev HTTP; the daily
   continues.
4. **Private state.** Quota and reuse live in gitignored
   `runs/jev/quota-<Beijing-date>.json`. For a new CA / new attempt to keep
   the same Beijing-day counter, set `AI_DAILY_PRIVATE_RUN_DIR` to a durable
   host path (or persist `runs/jev/`). If that state cannot be restored, Jev
   is skipped and the daily continues. Never write this into `editions/` or
   delivery ledgers.
5. **Quota.** Asia/Shanghai calendar day 00:00–24:00, at most 100 real HTTP
   requests. Failures count. No automatic retries. No auto top-up.

Print the CA paste block: `python scripts/jev_assist.py --print-ca-notes`.

## Day-刊 / CA template wording (copy)

Use after gather + merge same-event + required fields, before re-verify and
final pick. Production launch today: Cursor `CloudAgent` `action=launch`,
title `YYYY-MM-DD-ai-daily`, repo `https://github.com/statefulai/ai-daily-brief`,
`starting_ref` usually `main` (SHA pinned in the prompt).

```
合并同事件并检查必填字段后，调用：

python scripts/jev_assist.py \
  --candidates output/cloud-agent-check/candidates.json \
  --out output/cloud-agent-check/jev-assist.json \
  --editions editions

Jev 只辅助两个维度：相对既往公开报道是否有明确新事实、对目标读者的阅读价值。
分数不得单独入选、淘汰或裁定空刊；不得用高分跳过回源核验，也不得用低分写成 no_new_value。
缺 TYPESAFE_API_KEY、JEV_ASSIST=0、配额用尽、超时、鉴权失败、限流、坏响应或私有 runs/jev 状态读不回时：跳过 Jev，继续原选稿。
未打分候选仍可入选。Jev 失败不是「今天没有新闻」。零入选仍走既有同次补查规则，不循环凑稿。
密钥只来自 Cloud Agents Runtime Secret TYPESAFE_API_KEY；日刊 box-secrets 不会注入 CA 虚拟机。
```
