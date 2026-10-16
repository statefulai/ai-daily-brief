import json
import tempfile
import unittest
from pathlib import Path

from source_health import SourceAlert, observe_source_health
from source_status import SourceResult
from sources import NewsItem


def failed(error: str) -> list[SourceResult]:
    return [SourceResult("mistral", "Mistral", "failed", error=error)]


SUCCESS = [
    SourceResult(
        "mistral",
        "Mistral",
        "success",
        items=[NewsItem(title="A", url="https://mistral.ai/news/a/", source="Mistral")],
    )
]
SKIPPED = [SourceResult("mistral", "Mistral", "skipped")]


def threshold_alert(alert_id: str, since: str, days: int, error: str) -> SourceAlert:
    return SourceAlert(
        alert_id=alert_id,
        source_id="mistral",
        failing_since=since,
        consecutive_days=days,
        error=error,
        text=f"来源告警：Mistral（mistral）已连续 {days} 天抓取失败，自 {since} 起。原因：{error}",
    )


class SourceHealthTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "source_health"

    def tearDown(self):
        self._tmp.cleanup()

    def observe(self, edition_id: str, results: list[SourceResult], *, persist: bool = True):
        return observe_source_health(edition_id, results, persist=persist, root=self.root)

    def stored_files(self) -> list[str]:
        return sorted(
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file()
        )

    def test_same_day_reruns_alert_once_per_reason(self):
        self.assertEqual(self.observe("2026-10-12", failed("HTTP 404")), [], "day 1")
        self.assertEqual(self.observe("2026-10-13", failed("HTTP 404")), [], "day 2")
        self.assertEqual(
            self.observe("2026-10-14", failed("HTTP 404")),
            [threshold_alert("2026-10-14:mistral:1", "2026-10-12", 3, "HTTP 404")],
            "day 3 alerts",
        )
        self.assertEqual(
            json.loads((self.root / "alerts/2026-10-14/mistral.1.json").read_text(encoding="utf-8")),
            {
                "version": 1,
                "kind": "source_health_alert",
                "alert_id": "2026-10-14:mistral:1",
                "source_id": "mistral",
                "failing_since": "2026-10-12",
                "consecutive_days": 3,
                "error": "HTTP 404",
                "text": "来源告警：Mistral（mistral）已连续 3 天抓取失败，自 2026-10-12 起。原因：HTTP 404",
            },
            "bot reads the alert file",
        )
        self.assertEqual(self.observe("2026-10-14", failed("HTTP 404")), [], "day 3 rerun, same error")
        self.assertEqual(self.observe("2026-10-14", SUCCESS), [], "day 3 rerun, success")
        self.assertEqual(
            self.observe("2026-10-14", failed("HTTP 404")), [], "day 3 rerun, back to the alerted error"
        )
        self.assertEqual(
            self.observe("2026-10-14", failed("HTTP 410")),
            [
                SourceAlert(
                    alert_id="2026-10-14:mistral:2",
                    source_id="mistral",
                    failing_since="2026-10-12",
                    consecutive_days=3,
                    error="HTTP 410",
                    text="来源告警：Mistral（mistral）已连续 3 天抓取失败，原因有变化。之前：HTTP 404。现在：HTTP 410",
                )
            ],
            "day 3 rerun, new error",
        )
        self.assertEqual(self.observe("2026-10-14", failed("HTTP 410")), [], "day 3 rerun, new error again")
        self.assertEqual(
            self.observe("2026-10-14", failed("HTTP 404")),
            [
                SourceAlert(
                    alert_id="2026-10-14:mistral:3",
                    source_id="mistral",
                    failing_since="2026-10-12",
                    consecutive_days=3,
                    error="HTTP 404",
                    text="来源告警：Mistral（mistral）已连续 3 天抓取失败，原因有变化。之前：HTTP 410。现在：HTTP 404",
                )
            ],
            "day 3 rerun, reason returns to an earlier error",
        )
        self.assertEqual(self.observe("2026-10-15", SUCCESS), [], "day 4 success")
        self.assertEqual(self.observe("2026-10-16", failed("HTTP 404")), [], "day 5, streak restarts at 1")
        self.assertEqual(self.observe("2026-10-17", failed("HTTP 404")), [], "day 6, streak 2")
        self.assertEqual(
            self.observe("2026-10-18", failed("HTTP 404")),
            [threshold_alert("2026-10-18:mistral:1", "2026-10-16", 3, "HTTP 404")],
            "day 7, new streak reaches 3",
        )

    def test_calendar_gap_ends_streak(self):
        self.assertEqual(self.observe("2026-10-01", failed("HTTP 404")), [])
        self.assertEqual(self.observe("2026-10-02", failed("HTTP 404")), [])
        self.assertEqual(self.observe("2026-10-04", failed("HTTP 404")), [], "no run on 10-03")
        self.assertEqual(self.observe("2026-10-05", failed("HTTP 404")), [])
        self.assertEqual(
            self.observe("2026-10-06", failed("HTTP 404")),
            [threshold_alert("2026-10-06:mistral:1", "2026-10-04", 3, "HTTP 404")],
        )

    def test_skipped_day_is_not_a_failing_day(self):
        self.assertEqual(self.observe("2026-10-01", failed("HTTP 404")), [])
        self.assertEqual(self.observe("2026-10-02", SKIPPED), [])
        self.assertEqual(self.observe("2026-10-03", failed("HTTP 404")), [], "skip does not count as a failure")
        self.assertEqual(self.observe("2026-10-04", failed("HTTP 404")), [], "skip does not pause the streak")
        self.assertEqual(
            self.observe("2026-10-05", failed("HTTP 404")),
            [threshold_alert("2026-10-05:mistral:1", "2026-10-03", 3, "HTTP 404")],
        )

    def test_dry_run_returns_alert_and_writes_nothing(self):
        self.observe("2026-10-12", failed("HTTP 404"))
        self.observe("2026-10-13", failed("HTTP 404"))
        expected = [threshold_alert("2026-10-14:mistral:1", "2026-10-12", 3, "HTTP 404")]

        self.assertEqual(self.observe("2026-10-14", failed("HTTP 404"), persist=False), expected)
        self.assertEqual(
            self.stored_files(),
            ["days/2026-10-12/mistral.json", "days/2026-10-13/mistral.json"],
        )
        self.assertEqual(self.observe("2026-10-14", failed("HTTP 404")), expected, "dry run consumed nothing")

    def test_corrupt_day_file_counts_as_a_failing_day(self):
        self.assertEqual(self.observe("2026-10-12", failed("HTTP 404")), [])
        corrupt = self.root / "days" / "2026-10-13" / "mistral.json"
        corrupt.parent.mkdir(parents=True)
        corrupt.write_text("{not json", encoding="utf-8")
        self.assertEqual(
            self.observe("2026-10-14", failed("HTTP 404")),
            [threshold_alert("2026-10-14:mistral:1", "2026-10-12", 3, "HTTP 404")],
            "a corrupt day keeps the streak",
        )

    def test_failed_alert_write_warns_once_and_next_day_recovers(self):
        self.observe("2026-10-12", failed("HTTP 404"))
        self.observe("2026-10-13", failed("HTTP 404"))
        blocker = self.root / "alerts"
        blocker.write_text("not a directory", encoding="utf-8")

        with self.assertLogs("source_health", level="WARNING") as logs:
            self.assertEqual(self.observe("2026-10-14", failed("HTTP 404")), [])
        self.assertEqual(len(logs.records), 1)

        blocker.unlink()
        self.assertEqual(
            self.observe("2026-10-15", failed("HTTP 404")),
            [threshold_alert("2026-10-15:mistral:1", "2026-10-12", 4, "HTTP 404")],
            "day 3 still counted and its lost alert re-emits",
        )


if __name__ == "__main__":
    unittest.main()
