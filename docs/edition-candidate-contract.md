# New edition contract

CI checks this shape only for a new or changed edition. An unchanged published file stays on the historical gate. The bot outside this repository has to emit these fields. This repository does not change that bot's private files.

`support` records what the writer claims about a page. It does not prove the page was read or that the excerpt supports the claim. `editorial_verifications` is a reviewer attestation bound to the edition, the attempt, the excerpt, and the claim text. A false attestation can still match this shape.

## Edition fields

An event may include `claims` and `editorial_verifications`. A source may include `support`. Historical editions omit all three.

`claims` is required when the title, lede, or a fact contains a number with a unit, such as `%`, `token`, `美元`, or `亿`.

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

The edition is kept only when one source is `original` and `read`, the excerpt is at least 12 characters and shares a number or token with the claim text, and a `supports` verification names that excerpt, a claim string that appears in the event, the same `edition_id`, and the same `attempt`.

A published edition directory on the base branch is frozen. Scope rejects a change to its `edition.json` unless a caller explicitly passes `allow_published_correction=True`. The workflow does not pass that flag. `revisions.json` may be added beside an edition. Its revision numbers must strictly increase, and records already on the base must not be rewritten.
