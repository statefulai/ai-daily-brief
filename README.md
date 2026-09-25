# AI Daily Brief

从公开一手来源和已核验候选里筛出重要变更，写成同一期，供网页、邮件和群阅读。

公开入口：[https://brief.sanze.dev/](https://brief.sanze.dev/)。首页是报头、最新一期和往期。

## 产品原则

- 回到公开一手来源核对事实、时间和适用范围；核验缺口写进正文。
- 当天有多少条真正重要的变化，就写多少条；不为凑数灌版。
- 没有新价值就不发刊。来源或核验失败单独作为失败，不写成「今天没有新闻」。
- 网页、邮件、群是同一期：邮件发送完整 `email.html`，群发送完整纯文本。
- 公开期次由 `edition.json`、固定模板和 CI 生成，不手写 HTML。

## 如何运作

- 汇集候选：公开一手来源，并可并入已核验、标明可公开的供稿。窗口为北京时间 `[前一日 08:00, 当日 08:00)`。
- Jev 辅助判断有没有新变化、值不值得读；分数不能单独入选、淘汰或裁定空刊。调用与关闭见 [`generation/CA-JEV-ASSIST.md`](generation/CA-JEV-ASSIST.md)。
- Cloud Agent 回源核验并最终选稿，写成 `edition.json`，再用固定模板生成 HTML。
- Pull request 上 CI 校验 schema、来源、内容 hash 和 HTML；合入 `main` 后发布到 [https://brief.sanze.dev/](https://brief.sanze.dev/)。
- 投递同一期，契约在 [`delivery/`](delivery/)：邮件是完整 `email.html`，群是完整纯文本。
- `published_candidate` 只表示期次已写成可发布稿，才会写入 `editions/YYYY-MM-DD/`；不等于网页已上线，也不等于邮件或群已发出。`no_new_value` 与 `failed` 都不创建公开期次，也不能互相冒充。

## 本地开发

需要 Python 3.11+。在 `.env` 填写 `OPENAI_API_KEY`。08:00 前手动运行，仍指向最近一个已经闭合的北京时间窗口。

```bash
git clone https://github.com/statefulai/ai-daily-brief.git
cd ai-daily-brief
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

python main.py                  # 只有 published_candidate 才写 editions/<date>/
python main.py --sources-only  # 只读来源，不调用模型
python main.py --dry-run       # 不写期次
python -m unittest discover -s tests -v
python scripts/edition_ci.py validate
```

可选：`python main.py --config path/to/config.yaml`，`python main.py --contributions path/to/contributions.json`（不传则只用公开来源）。供稿桥接见 [`integrations/grok_bot/README.md`](integrations/grok_bot/README.md)。

仓库：`main.py` 本地运行；[`generation/`](generation/) 选稿辅助；[`delivery/`](delivery/) 邮件与群正文；`templates/`、`scripts/edition_ci.py` 渲染与校验；`editions/` 正式期次。

## License

[MIT](./LICENSE)
