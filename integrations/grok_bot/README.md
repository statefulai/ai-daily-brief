# Grok Bot 供稿桥接

这里仅说明 Grok Bot 如何向日报提供候选，不包含日报核心逻辑。没有情报 Bot 的运行环境可以忽略整个目录。

`日刊` 每次运行只做一次临时转换：

1. 读取北京时间 `[前一日 08:00, 当日 08:00)` 内的 `/workspace/intel/flash/*-scan.json`。
2. 只保留明确要进入公开日报的候选，按下表映射为通用供稿字段。
3. 把结果写到临时的 `contributions.json`；不复制原始扫描记录、投递状态或内部字段。
4. 启动当期唯一 Cloud Agent 时，通过 `files` 上传该文件。
5. Cloud Agent 使用自身模型采集公开来源，与上传的候选合并，并检查必填字段。
6. 日报在最终选稿之前接入 Jev 辅助判断（有没有新变化、值不值得读）。调用、密钥、关闭（`JEV_ASSIST=0`）与额度交接见 [`generation/CA-JEV-ASSIST.md`](../../generation/CA-JEV-ASSIST.md)。Jev 不裁定入选或空刊；失败时继续原选稿。
7. Cloud Agent 回源核验并做最终选稿，然后写入 `output/cloud-agent-check/editions/<日期>/edition.json`。
8. Cloud Agent 用仓库的固定渲染器生成 HTML，并执行校验：

   ```bash
   python scripts/edition_ci.py render --edition output/cloud-agent-check/editions/<日期>/edition.json
   python scripts/edition_ci.py validate --editions output/cloud-agent-check/editions
   ```

临时供稿和验证产物不进入 Git、Pages 或正式期次目录。没有可用候选时不创建供稿文件，Cloud Agent 只使用自身采集的公开来源。Cloud Agent 不需要把自身模型作为 API 提供给 Python。

| 闪报候选 | 通用供稿 |
| --- | --- |
| `fact` | `event`，并作为 `verified_facts` 的一项 |
| `url` | `primary_source.url` |
| URL 的站点名 | `primary_source.title` |
| `createdAt` | `source_time` |
| `postId` | `flash_ref` |

`conditions` 只写来源明确给出的计划、平台、阶段、地区或权限边界，没有则用空数组。未实测、仅确认到日期、页面未直接取得等核验缺口写进 `sources[].note` 或 `background`，不要写入 `conditions`。`sensitivity` 只有确认可公开时才写 `public`。缺少 `fact`、`url` 或带时区的 `createdAt` 时跳过该候选，不猜测或补写内容。

面向读者的核验说明写成「核验说明（来源名）：…」，只保留事实边界。不要出现「本环境」「JSON-LD」「抓取失败接口」、Cloud Agent、`workspace` 路径或其他实现细节。`适用范围`、背景和来源说明如有重叠，只保留一处。

`placement: desk` 只放明确的操作步骤、清单、工具用法或问答。普通公告和行业新闻放 `story`；例如 PR 上的 AI Scan 能力变更属于新闻速览，不进实操栏。

## 面向读者的正文

每条新闻写一段正文，字段是 `lede`。这一段同时是首页头条摘录、邮件和群文本的正文、网页的首段，只写一份，不要另做摘录。

- 建议 80–120 字。字数按去掉空白后的字符数计，中文、英文和数字各算 1，链接不算。写清谁、做了什么、何时或者在哪能用。来源里有关键数字就写上，没有就不要硬凑。
- 语言平实，像真人写的新闻。不要 AI 腔，不要「值得注意的是」「综上所述」「标志着」「赋能」这类套话。不能漏掉这条新闻的关键信息。
- 政策全文、多方说法互相冲突、或者关键条件很多时，正文可以超过 120 字，但不得超过 200 字，并在该条写 `length_exception`（一句原因）。每一期最多 2 条。
- 正文里不要出现 `datePublished`、`dateModified`、`createdAt`、`lastmod`（忽略大小写），也不要写带钟点的时间。日期和时间之间用 `T` 或空格都算，时区可以不写，也可以是 `Z`、`±HH:MM`、`±HHMM`、`UTC` 或 `GMT`。纯日期（`2026-10-09`、「10 月 9 日」）和普通数字可以写。`±HHMM`、`±HH:MM` 只在紧跟钟点时算偏移，例如 `16:09+0800`、`16:09:00+08:00`；`+1500`、`-1200`、`+12:30` 和单独的 `17:00 UTC` 可以写。发布时间只放在 `sources[].published_at` 和核验说明里。

`caveat` 是单独的一行限定，不要默认取 `conditions` 的第一条。写读者最容易误会的那一点，例如还没上线、只限企业、数字是公司自己测的。建议不超过 40 字，而且必须针对这一条，不要写放在哪条新闻上都成立的套话。限定里同样不能出现上面的时间字段或带钟点的时间戳。没有正文就不能只写限定。

每条保留一个原文链接，用第一个一手来源的 `url`。`facts`、`conditions`、`sources` 和 `sources[].note` 仍按原来的契约写全：网页把正文、限定和原文链接放在上面，其余内容放进默认折叠的「详细与核验」。邮件和群文本只展示标题、正文、限定和原文链接，不展开更多 facts、适用范围、来源列表和核验说明。

校验只卡硬线，而且只在写出 `lede`（以及随之出现的 `caveat`、`length_exception`）时生效。没有这些字段的往期 edition.json 仍按原样解析和渲染，不会因为新规则失败。硬线是：正文超过 200 字；正文超过 120 字且没有 `length_exception`；一期破例超过 2 条；正文或限定里出现上述时间字段或带钟点的时间戳。正好 120 字通过且不提醒；正好 200 字必须有 `length_exception` 才通过。空格和换行不计入字数。有限定就必须有正文。正文超过 120 字但写了原因、限定超过 40 字，只提醒，不失败。
