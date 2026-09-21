# Jev assist (operators + day-刊)

Assist-only scoring after same-event merge and before source re-verification / final selection.
This file is wording and ops notes for the code PR. It does not change day-刊's
`delivery/CA-GENERATE-TEMPLATE.md` (that update stays out of this repo).

## Disable (one real entry)

**Canonical kill switch: `JEV_ASSIST=0`.**

Code checks `JEV_ASSIST` before any store I/O or HTTP. Values `0` / `false` / `off` / `no` / `disabled` turn assist off; the daily continues with the original candidates.

Aliases (only if `JEV_ASSIST` is unset): `JEV_ASSIST_DISABLED=1`, or `jev.enabled: false` in `config.yaml`. Do not treat those as the operator entry — use `JEV_ASSIST=0`.

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
3. **Disable.** `JEV_ASSIST=0` (see above). When off, no Jev HTTP; the daily
   continues.
4. **Private state.** Quota and reuse live in a private JSON file, never in
   `editions/` or delivery ledgers.
   - **Durable (cross-CA):** set `AI_DAILY_PRIVATE_RUN_DIR` to a host path that
     every generate / re-search / approved re-run / live acceptance for that
     Beijing day can see. The file is
     `$AI_DAILY_PRIVATE_RUN_DIR/jev/quota-<Beijing-date>.json`.
     Optional alias: `JEV_ASSIST_STORE` (full file path) or `--store`.
   - **Same-workspace resume only:** if none of those are set, the default is
     gitignored `runs/jev/quota-<Beijing-date>.json`. A missing default file
     does **not** start a new 100-call day.
   - **Unrestorable → skip Jev.** Missing file (workspace), leftover lock with
     no JSON, permission error, corrupt/empty JSON, lock failure: skip Jev,
     log `store_unavailable`, continue curation. **Never** re-init counters
     to zero for that Beijing day. A new CA VM that cannot restore
     `AI_DAILY_PRIVATE_RUN_DIR` must skip, not mint another 100.
5. **Quota.** Asia/Shanghai calendar day 00:00–24:00, at most 100 real HTTP
   requests across the whole assist chain. Failures and retries count. No
   automatic retries. No auto top-up. Concurrent writers use `fcntl` + atomic
   replace on the same durable file.

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
关闭开关只认 JEV_ASSIST=0（在任何 HTTP 之前检查）。
缺 TYPESAFE_API_KEY、JEV_ASSIST=0、配额用尽、超时、鉴权失败、限流、坏响应或私有配额状态读不回（缺文件、损坏、无权限、锁失败）时：跳过 Jev，继续原选稿。不得把读不回的状态当成当日配额从零开始。
默认 runs/jev 只用于同一工作区续跑。跨独立 CA 必须共用 AI_DAILY_PRIVATE_RUN_DIR；新虚拟机拿不到这份状态就跳过 Jev。
未打分候选仍可入选。Jev 失败不是「今天没有新闻」。零入选仍走既有同次补查规则，不循环凑稿。
密钥只来自 Cloud Agents Runtime Secret TYPESAFE_API_KEY；日刊 box-secrets 不会注入 CA 虚拟机。
```
