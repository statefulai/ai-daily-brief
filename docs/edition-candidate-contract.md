# New edition contract

CI checks this shape only for a new or changed edition. An unchanged published file stays on the historical gate. The bot outside this repository has to emit these fields. This repository does not change that bot's private files.

`support` records what the writer claims about a page. It does not prove the page was read or that the excerpt supports the claim. `editorial_verifications` is a reviewer attestation bound to the edition, the attempt, the excerpt, and the claim text. A false attestation can still match this shape.

## Edition fields

An event may include `claims` and `editorial_verifications`. A source may include `support`. Historical editions omit all three.

`claims` must cover every number that has a unit or currency in the title, lede, or facts. Units include `%`, `token`, `美元`, `亿`, `billion`, `ms`, `GB`, `分`, and a trailing `x`, plus a `$` prefix. A year written as `2026 年` is not a claim. Each `claims[].value` must itself appear in that text.

## Example

```json
{
  "sources": [
    {
      "label": "Lab",
      "url": "https://lab.example/report",
      "kind": "primary",
      "published_at": "2026-11-11T01:00:00+00:00",
      "verified": false,
      "note": null,
      "support": {
        "relationship": "original",
        "reading": "read",
        "excerpt": "The lab reported 91.2 on the benchmark."
      }
    }
  ],
  "claims": [
    {"kind": "measured", "value": "91.2"}
  ],
  "editorial_verifications": [
    {
      "edition_id": "2026-11-12",
      "attempt": 1,
      "source_url": "https://lab.example/report",
      "excerpt": "The lab reported 91.2 on the benchmark.",
      "claim": "实验室报告基准得分 91.2。",
      "verdict": "supports"
    }
  ]
}
```

`relationship` is `original`, `retelling`, or `supporting`. `reading` is `read`, `not_fetched`, `http_403`, `http_429`, `timeout`, `timestamps_only`, or `other_failure`. `verdict` is `supports` or `does_not_support`.

`kind` on a claim is `measured`, `predicted`, or `attributed`. A predicted value and a prediction word have to appear in the same reader field, and a negated phrase such as `不是预测` does not count. A measured claim whose own field says `预计`, `预测`, or `projected` is rejected.

The edition is kept only when one source is `original` and `read`, the excerpt is at least 12 characters, the excerpt is not a copy of the title, lede, or a fact, and the excerpt shares a non-year number or at least six Chinese characters or two English words of five letters. One shared two-character word, one English word, or a year is not enough. A `supports` verification must name that excerpt, the same `edition_id` and `attempt`, and a claim string that appears in the event and contains a number from the text or at least six Chinese characters. `。` and `焦点` do not count.

A predicted value and an unnegated prediction word must share one reader field. Negation is the rest of the same clause. A decimal point inside `83.0` does not start a new clause. `非常`, `不久`, `不仅`, `不但`, and `不光` are not negations. `未`, `尚未`, `从未`, `别`, `never`, and `isn't` are. `并不属于…预测` in that clause is negated. A measured claim whose own field says `预计` or `预测` is rejected, including `非常接近预计`. A measured lede that says `实测` while the excerpt says `Projected` still passes.

Each `claims[].value` is one numeric span: at most 32 characters, no clause punctuation, and exactly one unit-number from the text. A whole sentence that contains two numbers is not a span. A number next to a unit or a magnitude is a claim, including Chinese numerals (`三千亿`, `百分之二十`), full-width digits, `70B`, `128K`, `美金`, `fps`, `秒`, `小时`, and `TB/s`. A year written as `2026 年`, a date, a version, or an id is not.

An English excerpt and a Chinese lede that share no number and no six-character span stay unrelated. A verification may add `source_span` (a verbatim piece of the excerpt) and `translated_claim` (text that appears in the event). A cookie notice or “all rights reserved” excerpt cannot use this path. Each `normalized_numbers` entry must place its source text inside `source_span` and its claim text inside `translated_claim`, and the two quantities must be equal after scaling (`1 million` equals `100 万`, `3 千亿` equals `300 billion`). That pair is still an attestation.

Chinese numerals count as claims only when they are a quantity: a digit combined with 十/百/千/万/亿, a magnitude such as 百万, a fraction such as 十分之一, or a measured unit such as 倍, 成, 家 when the digit is not 一, or a currency or clock unit. A lone 一 before 个, 次, 位, 款, 家, 名, 人, 年, or 种 is not a claim, and neither is 第 followed by a digit. Names and adverbs do not count: 千问, 千帆, 万相, 万物, 十分, 千万不要, 百分比, 二分, 一键. The fraction exclusion does not swallow 十分之一 or 二分之一.

A scale word counts only when it is the first word after the number. `trillion` is 10 to the 12th, the same as 万亿. A translated pair may differ by up to 2 percent, which allows 1.48 to match 1.5.

Negation is the rest of the clause. A decimal point inside a number is not a clause boundary. 未, 尚未, 从未, 别, 勿, never, isn't, is not, and not yet are negations. 不仅, 不但, and 不光 are not. The word list does not treat 并非常规预测 or 否认…预测 as negations. Those stay visible until an editor marks them.

A published edition directory on the base branch is frozen. Scope rejects a change to its `edition.json` unless a caller explicitly passes `allow_published_correction=True`. The workflow does not pass that flag. `revisions.json` is in scope only when the same pull request also changes that edition's `edition.json` and `index.html`. A `revisions.json`-only change is rejected. Records must use revision `1` or `2`, a matching `edition_id`, and status `attempted`, `applied`, or `blocked`.

`RevisionWorkspace.apply` requires the delivery ledger and the observed pull request head. The observed head is the `pull_request.head.sha` in the GitHub event file, or `git rev-parse` of the fetched PR ref. `edition_ci.py check-head` compares that file with the checkout. It does not call the GitHub API.

`RunLog.dispatch` requires `edition_id` and `expected_head` (40 hexadecimal digits) from that dispatch's own receipt. A completion must repeat `expected_head`. An older run of the same `edition_id` is a late wake. Two editions can complete in either order. `source` is still a string the caller reports.

Before opening a pull request, run preflight on the repository path, not on `output/`:

```bash
python scripts/edition_ci.py preflight --edition editions/<date>/edition.json --base origin/main
```

A file under `output/cloud-agent-check/` is not the published tree. Preflight there does not see that `editions/<date>/` is frozen.

On a pull request, GitHub checks out the merge commit. `scope` compares `pull_request.head.sha` with `HEAD^2`. On a push, it compares `HEAD` with `after`. An event file that names neither fails. Local `scope` without an event file does not perform that comparison.
