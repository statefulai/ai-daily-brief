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

`kind` on a claim is `measured`, `predicted`, or `attributed`. A predicted value and a prediction word have to appear in the same reader field, and a negated phrase such as `不是预测`, `否认`, or `rather than` does not count. A measured claim is rejected when its field, or the excerpt clause that contains its number, uses a prediction word.

The edition is kept only when one source is `original` and `read`, the excerpt is at least 12 characters, the excerpt is not a copy of the title, lede, or a fact, and the excerpt shares a non-year number of the same quantity or at least six Chinese characters or two English words of five letters. A cookie notice, “all rights reserved”, or “privacy policy” excerpt cannot use this path. One shared bare digit with a different scale, such as `3000` against `3000 亿美元`, is not enough, and neither is one English word or a year. A `supports` verification must name that excerpt, the same `edition_id` and `attempt`, and a claim string that appears in the event and contains a number from the text or at least six Chinese characters. `。` and `焦点` do not count.

A predicted value and an unnegated prediction word must share one reader field. The word list is shared with the measured check: 预测, 预计, 预期, 预估, 估计, 有望, 将达, 计划, 目标, predicted, forecast, forecasted, projected, expected, estimate, estimated, guidance, outlook, and target, plus close variants such as expects and targeted. Negation is the rest of the same clause. A decimal point inside `83.0` does not start a new clause. `非常`, `不久`, `不仅`, `不但`, and `不光` are not negations. `未`, `尚未`, `从未`, `别`, `否认`, `rather than`, `never`, and `isn't` are. `并不属于…预测` in that clause is negated. A measured claim is rejected when its field uses one of those words, including `非常接近预计`, and when the excerpt clause that contains the measured number does. A measured lede that says `实测` while the excerpt says `Projected` is rejected.

Each `claims[].value` is one numeric span: at most 32 characters, no clause punctuation, and exactly one number from the text. A whole sentence that contains two numbers is not a span. Every Arabic number is a claim, including `40 毫秒`, `30 天`, `83.0 tok/s`, `700 W`, `500 台`, `91.2`, `6 个月`, `1.2 PB`, `96 层`, and a bare rank such as `第 3`. A year, a date such as `10月9日` or `2026-11-06`, a clock time, a dotted version such as `2.1.313` or `Claude 3.5`, a model token such as `GPT4`, `GPT-4`, or `Llama 3.1`, or an id is not. `70B` beside `Llama 3.1` is still a claim. Chinese numerals (`三千亿`, `百分之二十`), full-width digits, `70B`, `128K`, `美金`, `fps`, `秒`, `小时`, and `TB/s` stay claims.

An English excerpt and a Chinese lede that share no number and no six-character span stay unrelated. A verification may add `source_span` (a verbatim piece of the excerpt) and `translated_claim` (text that appears in the event). A cookie notice or “all rights reserved” excerpt cannot use this path. Each `normalized_numbers` entry must place its source text inside `source_span` and its claim text inside `translated_claim`, and the two quantities must be equal after scaling (`1 million` equals `100 万`, `3 千亿` equals `300 billion`). That pair is still an attestation.

Chinese numerals count as claims only when they are a quantity. A lone 一 before 个, 位, 次, 款, 家, 名, 人, 年, or 种 is not a claim, and neither is 第, 另一, 上一, 下一, 又一, 任何一, 唯一, or 同一. Those classifiers count after 十, 百, 千, or 万, and after a digit run such as 三四, so 十家, 百人, 十个, and 三四家 are claims. A single other digit takes 家, so 六家 is a claim, and does not take 个, 位, 次, 款, 名, 人, 年, or 种. 倍, 成, currency, and clock units count for every digit. 十分之一 and 二分之一 are fractions, not the excluded words 十分 and 二分. Names and adverbs do not count: 千问, 千帆, 万相, 万物, 十分, 千万不要, 百分比, 二分, 一键. A stated translation pair is checked even when both sides share a digit, so 3 千万 does not match 3 thousand.

A scale word counts only when it is the first word after the number. `千万` is 10 to the 7th, `百亿` is 10 to the 10th, `十亿` is 10 to the 9th, and `trillion` is 10 to the 12th. Two integers must be the same value. A decimal may match only when the coarser side keeps at least two significant digits and rounding the finer side to the coarser side's written decimal places gives that side. `1.48` matches `1.5`. `1.5` does not match `2`, and `0.6` does not match `1`. `1 million` does not match `102 万`. Percentage points and percent are different kinds: `1.5 个百分点` matches `1.5 percentage points` and does not match `1.5%`.

Negation is the rest of the clause. A decimal point inside a number is not a clause boundary. 未, 尚未, 从未, 别, 勿, 否认, rather than, never, isn't, is not, and not yet are negations. 不仅, 不但, and 不光 are not. The word list does not treat 并非常规预测 as a negation. That phrase stays visible until an editor marks it.

A published edition directory on the base branch is frozen. Scope rejects a change to its `edition.json` unless a caller explicitly passes `allow_published_correction=True`. The workflow does not pass that flag. `revisions.json` is in scope only when the same pull request also changes that edition's `edition.json` and `index.html`. A `revisions.json`-only change is rejected. Records must use revision `1` or `2`, a matching `edition_id`, and status `attempted`, `applied`, or `blocked`.

`RevisionWorkspace.apply` requires the delivery ledger and the observed pull request head. The observed head is the `pull_request.head.sha` in the GitHub event file, or `git rev-parse` of the fetched PR ref. `edition_ci.py check-head` compares that file with the checkout. It does not call the GitHub API.

`RunLog.dispatch` requires `edition_id` and `base_head` (40 hexadecimal digits). An older receipt may send that head as `expected_head`; the stored field is `base_head`, and `expected_head` is kept as the same value. `RunLog.complete` stores the head the run produced as `result_head` (input key `result_head` or `head`). That head may differ from `base_head`. `RunLog.require_result_head(run_id, head_ref_oid)` compares `result_head` with the remote pull request `headRefOid` the caller passes in. It blocks when `result_head` is missing or differs, and it does not call GitHub. An older run of the same `edition_id` is a late wake. Two editions can complete in either order. `source` is still a string the caller reports. `bc-`, an unknown prefix, `latestRunId`, an undispatched run, a duplicate wake, a missing field, and source `agent_get` stay rejected.

Before opening a pull request, run preflight on the repository path, not on `output/`:

```bash
python scripts/edition_ci.py preflight --edition editions/<date>/edition.json --base origin/main
```

A file under `output/cloud-agent-check/` is not the published tree. Preflight there does not see that `editions/<date>/` is frozen.

On a pull request, GitHub checks out the merge commit. `scope` compares `pull_request.head.sha` with `HEAD^2`. On a push, it compares `HEAD` with `after`. An event file that names neither fails. Local `scope` without an event file does not perform that comparison.
