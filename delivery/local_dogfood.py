#!/usr/bin/env python3
"""Create one local review page without sending or writing delivery state."""

from __future__ import annotations

import argparse
from html import escape
import json
from pathlib import Path

from edition import validate_edition
from outputs import render_group_message


def render_dogfood_page(document: dict, public_url: str) -> str:
    edition = validate_edition(document)
    group = escape(render_group_message(edition, public_url))
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI 日报 · 本地 dogfood</title><style>
*{{box-sizing:border-box}}
body{{margin:0;background:#e7e3db;color:#45463f;font:16px/1.75 -apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif}}
main{{width:min(760px,calc(100% - 32px));margin:32px auto;padding:28px;background:#faf7f0;border:1px solid #b9b4a9}}
h1{{margin:0;color:#252621;font:400 34px/1.3 'Songti SC','STSong',serif}} .state{{margin:12px 0 24px;padding:10px 12px;border-left:2px solid #91472f;background:#f0ebe0}}
nav{{display:flex;flex-wrap:wrap;gap:10px 20px;margin:0 0 24px}} a{{color:#91472f}} pre{{max-width:100%;margin:0;white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;padding:16px;background:#fff;border:1px solid #c7c2b7;font:14px/1.7 ui-monospace,SFMono-Regular,Menlo,monospace}}
</style></head><body><main><h1>AI 日报 · 本地 dogfood</h1>
<p class="state">期次 {escape(edition['edition_id'])} · 本地预览，尚未部署或发送。</p>
<nav><a href="index.html">查看网页版</a><a href="email.html">查看邮件正文</a><a href="email.txt">邮件纯文本</a></nav>
<h2>群文字预览</h2><pre>{group}</pre></main></body></html>
"""


def write_dogfood_page(edition_path: Path, public_url: str, output_path: Path) -> Path:
    document = json.loads(Path(edition_path).read_text(encoding="utf-8"))
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_dogfood_page(document, public_url), encoding="utf-8")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("edition", type=Path)
    parser.add_argument("--public-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = write_dogfood_page(args.edition, args.public_url, args.output)
    print(output)


if __name__ == "__main__":
    main()
