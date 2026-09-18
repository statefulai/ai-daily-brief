<div align="center">

<img src="assets/cover-readme-21x9.png" width="100%" alt="AI Daily Brief" />

# AI Daily Brief

从公开来源生成可核验、可变篇数的中文 AI 日报，并以固定 HTML 模板输出网页、邮件正文和群消息。

[查看本地样刊](./samples/2026-09-16/index.html) · [手机预览](./samples/2026-09-16/mobile.png) · [本地 dogfood](./samples/2026-09-16/dogfood.html)

</div>

> 当前状态：新版期次、网页渲染、CI／Pages 骨架和本地投递预览已经完成；`【开源】AI日报` 项目群与 `日刊` Bot 已建立，日例程保持停用。GitHub Pages、邮件和群投递尚未启用，旧 Markdown 日更在首期切换前继续运行。

## 本地运行链路

一次运行完成以下步骤：

1. 并发读取已配置的公开来源，分别记录 `success`、`no_candidates`、`failed` 或 `skipped`。
2. 合并可选的结构化供稿，过滤非公开内容。
3. 使用一个 OpenAI 兼容模型完成可变篇数策展。
4. 回到一手来源核对发布时间和事实依据。
5. 生成严格的 `edition.json`，再由固定模板确定性渲染 `index.html`。

生成结果只有三种：

- `published_candidate`：有可发布内容，写入期次目录。
- `no_new_value`：来源读取成功但没有值得发布的新内容，不创建期次目录。
- `failed`：来源、模型或核验失败；不能伪装成“今天没有新闻”。

每个公开期次只包含：

```text
editions/YYYY-MM-DD/
├── edition.json
└── index.html
```

原始供稿、发送凭据、收件人、群聊信息和投递账本不进入公开期次。

托管生产由 `日刊` 启动一个 Cloud Agent。Agent 直接使用自身模型完成公开采集、供稿合并和策展，写出结构化 `edition.json`，再调用仓库的固定渲染与校验命令；Python 不需要取得 Cloud Agent 的模型 API Key。`main.py` 的 OpenAI 兼容模型配置只用于本地独立运行。

## 本地运行

需要 Python 3.11 或更高版本。

```bash
git clone https://github.com/statefulai/ai-daily-brief.git
cd ai-daily-brief
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

在 `.env` 中至少填写：

```env
OPENAI_API_KEY=your-api-key
```

若使用其他 OpenAI 兼容服务，再设置端点和模型：

```env
OPENAI_BASE_URL=https://your-provider.example/v1
AI_NEWS_MODEL=your-model-name
```

本项目尚未对所有兼容服务做全新 clone 验收；如遇到响应格式差异，请提交可复现问题。

运行命令：

```bash
# 完整流程；仅 published_candidate 会写 editions/<date>/
python main.py

# 只读取来源，不调用模型，也不写期次
python main.py --sources-only

# 打印结构化结果，不写文件
python main.py --dry-run

# 使用另一份配置
python main.py --config path/to/config.yaml

# 合并一份可选供稿；不传该参数时只使用公开来源
python main.py --contributions path/to/contributions.json
```

每天的正式期次采用北京时间 `[前一日 08:00, 当日 08:00)` 半开区间；08:00 前手动运行时仍指向最近一个已经闭合的窗口。`config.yaml` 控制数据源、模型和输出目录，环境变量优先于其中的模型端点与模型名。

## 可选供稿

`--contributions` 接收任意外部系统整理出的 JSON 文件，与公开采集进入同一次策展。不传参数时只使用公开来源；显式传入的文件如果缺失、格式错误或不符合契约，生成会直接失败。只有 `sensitivity: public` 且 `source_time` 落在同一日报窗口内的记录会进入候选，入选后仍会重新抓取一手来源。

最小记录包含事件、一手来源、带时区的来源时间、已核对事实、适用范围和敏感性：

```json
{
  "event": "某项模型能力开放",
  "primary_source": {
    "title": "官方公告",
    "url": "https://example.com/announcement",
    "published_at": "2026-09-16T01:00:00+08:00"
  },
  "source_time": "2026-09-16T01:00:00+08:00",
  "verified_facts": ["官方开放了该能力。"],
  "conditions": ["仅适用于已开放账号。"],
  "sensitivity": "public"
}
```

## 页面与投递输出

- 网页：单页报纸版式；内容少时省略空栏，内容多时继续单页分组，不固定篇数或分页。
- 邮件：同一期次生成内嵌 HTML 和纯文本兜底，不发送 HTML 附件或长图。发送时 `htmlBody` 必须是完整 `email.html`，禁止改成重点／摘要卡片加网页链接；纯文本部分同样携带全部文章。
- 群消息：与邮件纯文本同一内容族，发送完整可读正文（项目群不能渲染 HTML），禁止只发三条重点加链接，也不发送截图或原始 HTML 标签。
- 投递状态：以 `edition_id + content_hash + channel + recipient_scope` 分渠道记录；`sent` 和 `unknown` 都阻止直接重发。

发送入口是 `delivery.payload.build_email_send_parts`（`htmlBody` + 纯文本）和 `outputs.render_group_message`。仓库只提供渲染器、状态契约和本地 dogfood。真实群与邮件凭据由外部托管环境持有，不写入仓库。

## CI 与 Pages

`.github/workflows/edition-ci-pages.yml` 只负责：

- 校验 schema、来源状态、一手来源、内容 hash 和确定性 HTML；
- 限制一次期次变更只能包含一个日期的 `edition.json + index.html`；
- 阻止删除已经发布的期次；
- 在 `main` 存在正式期次时构建并部署 Pages。

它不调用模型、不设置日报 cron，也不发送群或邮件。Pages 目前尚未启用；首次合并、部署和真实投递需要单独验收。

## 迁移边界

- `.github/workflows/daily-news.yml` 仍运行旧 Markdown 日更，首期新版成功前不会停用。
- `.github/workflows/feishu-push.yml` 仍保留手动入口。
- `daily-brief.md` 与 `archives/` 仍由旧链路更新；新版 Pages 不回填这些文件。
- `config.yaml` 在迁移期继续启用旧 Markdown 与归档，避免代码合并后提前停掉旧日报；首期成功并正式切换时再关闭。

## 项目结构

```text
edition.py                 # 期次 schema、hash、状态与同日期互斥
curate.py                  # 把模型结果装配为可变篇数事件
verify.py                  # 一手来源核验
source_status.py           # 来源四态
contributions.py           # 通用供稿契约、过滤与策展接入
templates/                 # 固定网页与邮件模板
delivery/                  # 发送正文契约、本地 dogfood 与私有投递状态
integrations/grok_bot/     # Grok Bot 到通用供稿文件的桥接说明
scripts/edition_ci.py      # CI 校验与 Pages 构建
samples/2026-09-16/        # 本地验收样刊，不是生产期次
tests/                     # schema、渲染、CI、来源与投递测试
```

## 验证

```bash
python -m unittest discover -s tests -v
python scripts/edition_ci.py render --edition editions/<date>/edition.json
python scripts/edition_ci.py validate
python scripts/edition_ci.py build --output _site
```

`validate` 在没有正式期次时会成功并报告 0 期；`_site/` 是本地构建目录，不进入版本库。

## License

[MIT](./LICENSE)
