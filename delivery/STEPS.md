# 正刊采集

08:00 的正刊 Cloud Agent 只运行下面这一条命令来采集公开来源。源清单只有 `config.yaml`。

## 运行采集

在仓库根目录运行：

```bash
python main.py --sources-only --dry-run
```

这条命令读取 `config.yaml` 的 `sources`。每个地址只请求一次。它不写期次，也不写源健康记录。

覆盖领域是从这次结果里选稿的范围。不要为了补一个领域另写地址。

带 `disabled` 的条目，命令不会请求。你也不要请求。

`config.yaml` 里没有的地址不要请求。Reuters 不在清单里。该站返回 HTTP 401，仓库不存放 Reuters 凭据。不要新增凭据，也不要请求 Reuters。

xAI 只使用 `config.yaml` 里 `xai.pages` 的单篇。不要请求列表页、RSS 或 X 账号页。

Anthropic 与 Microsoft AI 只使用 `config.yaml` 里现有的 `anthropic`、`anthropic_news` 和 `ms_ai`。

## 写入新期次

采集命令不写期次。新期次的 `edition.json` 还要带 `support`、`claims` 和 `editorial_verifications`。字段和示例见 `docs/edition-candidate-contract.md`。CI 只对相对基线有改动的期次跑这道结构门。已发布期次的字节不变时仍走原来的校验。这道门不证明摘录支撑主张。
