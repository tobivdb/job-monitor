import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import job_monitor as monitor
from job_sources import EXTRACTOR_VERSION


class StateTests(unittest.TestCase):
    def setUp(self):
        self.job = monitor.JobEntry("Investment Associate", "https://example.test/job/123")
        self.result = monitor.SiteResult("Example", "https://example.test/careers/", jobs=[self.job])
        self.state = {"sites": {}}
        monitor.update_state(self.state, self.result)

    def test_old_parser_cleanup_does_not_report_filled_jobs(self):
        self.state["sites"]["Example"].pop("extractor_version")
        empty = monitor.SiteResult("Example", self.result.url)
        diff = monitor.compute_diff(empty, self.state)
        self.assertEqual(diff.removed_jobs, [])
        self.assertTrue(diff.parser_migrated)

    def test_canonical_old_url_does_not_become_new_during_migration(self):
        self.state["sites"]["Example"]["job_keys"] = {"legacy-hash": dict(title="Investment Associate Zurich", url=self.job.url + "?utm_source=old", location="", detail="")}
        self.state["sites"]["Example"].pop("extractor_version")
        self.assertEqual(monitor.compute_diff(self.result, self.state).new_jobs, [])

    def test_uncertain_empty_scan_preserves_previous_jobs(self):
        empty = monitor.SiteResult("Example", self.result.url, warnings=["No reliable extraction"])
        for _ in range(3):
            self.assertEqual(monitor.compute_diff(empty, self.state).removed_jobs, [])
            monitor.update_state(self.state, empty)
        self.assertIn(self.job.key, self.state["sites"]["Example"]["job_keys"])

    def test_missing_job_needs_two_successful_scans(self):
        empty = monitor.SiteResult("Example", self.result.url, has_no_jobs_indicator=True)
        self.assertEqual(monitor.compute_diff(empty, self.state).removed_jobs, [])
        monitor.update_state(self.state, empty)
        self.assertEqual(len(monitor.compute_diff(empty, self.state).removed_jobs), 1)
        monitor.update_state(self.state, empty)
        self.assertEqual(self.state["sites"]["Example"]["job_keys"], {})

    def test_blocked_detail_preserves_previous_job(self):
        empty = monitor.SiteResult("Example", self.result.url, unverified_urls=[self.job.url])
        monitor.update_state(self.state, empty)
        self.assertIn(self.job.key, self.state["sites"]["Example"]["job_keys"])

    def test_page_error_does_not_report_jobs_removed(self):
        empty = monitor.SiteResult("Example", self.result.url, error="HTTP 503")
        self.assertEqual(monitor.compute_diff(empty, self.state).removed_jobs, [])

    def run_main(self, args, *, mail_error=False):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            config = base / "config.json"
            config.write_text(json.dumps({"sites": [{"name": "Example", "url": self.result.url, "tier": "A"}], "tracker_feed": {"enabled": True}}), encoding="utf-8")
            state_path = base / "state.json"
            state_path.write_text(json.dumps({"sites": {"Example": {"job_keys": {}, "extractor_version": EXTRACTOR_VERSION}}, "last_run": None}), encoding="utf-8")
            before = state_path.read_bytes()
            with (patch.object(monitor, "BASE_DIR", base), patch.object(monitor, "STATE_FILE", state_path),
                  patch("sys.argv", ["job_monitor", "--config", str(config)] + args),
                  patch.object(monitor, "sync_playwright"), patch.object(monitor, "fetch_page", return_value=('<a href="/job/123">Investment Associate</a>', self.result.url)),
                  patch.object(monitor, "validate_jobs"),
                  patch.object(monitor, "google_sheets_available", return_value=True),
                  patch.object(monitor, "validate_email_config"), patch.object(monitor, "feed_tracker") as feed,
                  patch.object(monitor, "send_email", side_effect=RuntimeError("SMTP unavailable") if mail_error else None) as send):
                if mail_error:
                    with self.assertRaisesRegex(RuntimeError, "Email delivery failed"):
                        monitor.main()
                else:
                    monitor.main()
                self.assertEqual(state_path.read_bytes(), before)
                if not mail_error:
                    feed.assert_not_called()
                    send.assert_not_called()
                self.assertTrue((base / "last_report.html").exists())
                self.assertTrue((base / "scan_results.json").exists())

    def test_dry_run_never_writes_state_email_or_tracker(self):
        self.run_main(["--dry-run"])

    def test_smtp_failure_does_not_advance_state(self):
        self.run_main(["--always-email"], mail_error=True)

    def test_dry_run_rejects_mutating_mode_combinations(self):
        for mode in ("--reset", "--email-test"):
            with patch("sys.argv", ["job_monitor", "--dry-run", mode]):
                with self.assertRaises(SystemExit):
                    monitor.main()

    def test_feed_retry_reads_existing_urls_before_append(self):
        services = MagicMock(), MagicMock()
        drive, sheets = services
        sheets.spreadsheets().values().get().execute.return_value = {"values": [[self.job.url + "?utm_source=old"]]}
        candidate = {"job": self.job, "site": monitor.SiteDiff("Example", self.result.url), "description": "a" * 900}
        with patch.object(monitor, "_google_services", return_value=services), patch.object(monitor, "_find_tracker_sheet_id", return_value="sheet-id"):
            self.assertEqual(monitor.feed_tracker([candidate], self.state), [])
        sheets.spreadsheets().values().append.assert_not_called()
        self.assertIn(self.job.key, self.state["tracker_fed"])

    def test_feed_failure_does_not_mark_jobs_fed(self):
        drive, sheets = MagicMock(), MagicMock()
        sheets.spreadsheets().values().get().execute.return_value = {"values": []}
        sheets.spreadsheets().values().append().execute.side_effect = RuntimeError("invalid_grant")
        candidate = {"job": self.job, "site": monitor.SiteDiff("Example", self.result.url), "description": "a" * 900}
        with patch.object(monitor, "_google_services", return_value=(drive, sheets)), patch.object(monitor, "_find_tracker_sheet_id", return_value="sheet-id"):
            with self.assertRaises(RuntimeError):
                monitor.feed_tracker([candidate], self.state)
        self.assertNotIn(self.job.key, self.state["tracker_fed"])


if __name__ == "__main__":
    unittest.main()
