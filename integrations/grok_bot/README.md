# Grok Bot 供稿桥接

这里仅说明 Grok Bot 如何向日报提供候选，不包含日报核心逻辑。没有情报 Bot 的运行环境可以忽略整个目录。

`日刊` 每次运行只做一次临时转换：

1. 读取北京时间 `[前一日 08:00, 当日 08:00)` 内的 `/workspace/intel/flash/*-scan.json`。
2. 只保留明确要进入公开日报的候选，按下表映射为通用供稿字段。
3. 把结果写到临时的 `contributions.json`；不复制原始扫描记录、投递状态或内部字段。
4. 启动当期唯一 Cloud Agent 时，通过 `files` 上传该文件。
5. Cloud Agent 在仓库中执行 `python main.py --contributions <上传后的文件路径>`；日报会再次抓取入选候选的一手来源。

临时供稿文件不进入 Git、Pages 或正式期次目录。没有可用候选时不创建文件，Cloud Agent 直接执行 `python main.py`。

| 闪报候选 | 通用供稿 |
| --- | --- |
| `fact` | `event`，并作为 `verified_facts` 的一项 |
| `url` | `primary_source.url` |
| URL 的站点名 | `primary_source.title` |
| `createdAt` | `source_time` |
| `postId` | `flash_ref` |

`conditions` 只写来源明确给出的限制，没有则用空数组；`sensitivity` 只有确认可公开时才写 `public`。缺少 `fact`、`url` 或带时区的 `createdAt` 时跳过该候选，不猜测或补写内容。
