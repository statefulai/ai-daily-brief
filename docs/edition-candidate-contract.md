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

A predicted value and an unnegated prediction word must share one reader field. The eight characters before the word are the negation window, so `非预测`, `不是 预测`, and `not a forecast` do not count. A measured claim whose own field says `预计`, `预测`, or `projected` is rejected. A measured lede that says `实测` while the excerpt says `Projected` still passes.

A published edition directory on the base branch is frozen. Scope rejects a change to its `edition.json` unless a caller explicitly passes `allow_published_correction=True`. The workflow does not pass that flag. `revisions.json` is in scope only when the same pull request also changes that edition's `edition.json` and `index.html`. A `revisions.json`-only change is rejected. Records must use revision `1` or `2`, a matching `edition_id`, and status `attempted`, `applied`, or `blocked`.

`RevisionWorkspace.apply` requires the delivery ledger and the observed pull request head. The observed head is the `pull_request.head.sha` in the GitHub event file, or `git rev-parse` of the fetched PR ref. `edition_ci.py check-head` compares that file with the checkout. It does not call the GitHub API.

Before opening a pull request, run:

```bash
python scripts/edition_ci.py preflight --edition editions/<date>/edition.json --base origin/main
```
