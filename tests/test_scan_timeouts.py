import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import job_monitor as monitor
from scan_process import run_bounded


class DeadlineTests(unittest.TestCase):
    def test_hung_cleanup_is_killed_and_next_worker_can_run(self):
        started = time.monotonic()
        # Simulates the UCP failure: navigation error followed by stuck cleanup.
        code = "try:\n raise ValueError('page is navigating')\nfinally:\n import time; time.sleep(60)"
        with self.assertRaises(subprocess.TimeoutExpired):
            run_bounded([sys.executable, '-c', code], 0.3)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(run_bounded([sys.executable, '-c', 'pass'], 5), 0)

    @unittest.skipUnless(os.name == 'posix', 'POSIX browser process groups')
    def test_timeout_also_stops_browser_descendants(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / 'leaked-browser'
            child = f"import time; from pathlib import Path; time.sleep(1); Path({str(marker)!r}).touch(); time.sleep(60)"
            parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}], start_new_session=True); time.sleep(60)"
            with self.assertRaises(subprocess.TimeoutExpired):
                run_bounded([sys.executable, '-c', parent], 0.3)
            time.sleep(1.2)
            self.assertFalse(marker.exists(), 'Browser survived the worker timeout')

    def test_timeout_becomes_error_not_empty_success(self):
        site = {'name': 'UCP', 'url': 'https://ucp.ch/en/career/'}
        with patch.object(monitor, 'run_bounded', side_effect=subprocess.TimeoutExpired('worker', 180)):
            result = monitor.scan_site(site, 180)
        self.assertIn('exceeded 180s', result.error)
        self.assertFalse(result.has_no_jobs_indicator)
        self.assertEqual(result.jobs, [])

    def test_worker_result_retains_verified_ad_text_and_uncertainty(self):
        site = {'name': 'Fund', 'url': 'https://example.test/careers'}
        job = monitor.JobEntry('Investment Associate', 'https://example.test/job/1')
        expected = monitor.SiteResult(**site, jobs=[job], descriptions={job.key: 'Verified ad text'},
                                      warnings=['Other ad blocked'], unverified_urls=['https://example.test/job/2'])
        def complete(command, timeout):
            Path(command[-1]).write_text(json.dumps(asdict(expected)))
            return 0
        with patch.object(monitor, 'run_bounded', side_effect=complete):
            self.assertEqual(monitor.scan_site(site, 180), expected)

    def test_worker_crash_is_reported(self):
        with patch.object(monitor, 'run_bounded', return_value=1):
            result = monitor.scan_site({'name': 'Fund', 'url': 'https://example.test'}, 180)
        self.assertIn('worker failed', result.error)

    def test_main_continues_checkpoints_and_preserves_failed_site_state(self):
        failed = {'name': 'UCP', 'url': 'https://ucp.ch/en/career/'}
        healthy = {'name': 'Healthy', 'url': 'https://example.test/careers'}
        old_job = monitor.JobEntry('Investment Associate', failed['url'] + 'job/1')
        state = {'sites': {}}
        monitor.update_state(state, monitor.SiteResult(**failed, jobs=[old_job]))
        previous = copy.deepcopy(state['sites']['UCP'])
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            config = base / 'config.json'
            config.write_text(json.dumps({'sites': [failed, healthy], 'tracker_feed': {'enabled': False}}))
            def scan(site, timeout):
                if site['name'] == 'UCP':
                    return monitor.SiteResult(**failed, error='Site scan exceeded 180s')
                self.assertEqual(json.loads((base / 'scan_results.json').read_text())[0]['name'], 'UCP')
                return monitor.SiteResult(**healthy, has_no_jobs_indicator=True)
            with (patch.object(monitor, 'BASE_DIR', base), patch.object(monitor, 'load_state', return_value=state),
                  patch.object(monitor, 'scan_site', side_effect=scan) as scans,
                  patch.object(monitor, 'send_email') as mail, patch.object(monitor, 'save_state') as save,
                  patch.object(monitor, 'feed_tracker') as feed, patch.object(monitor, 'validate_email_config'),
                  patch('sys.argv', ['monitor', '--config', str(config), '--always-email'])):
                monitor.main()
            self.assertEqual(scans.call_count, 2)
            mail.assert_called_once()
            save.assert_called_once()
            feed.assert_not_called()
            self.assertEqual(state['sites']['UCP'], previous)
            self.assertIn('Healthy', state['sites'])
            self.assertEqual(len(json.loads((base / 'scan_results.json').read_text())), 2)
            self.assertIn('exceeded 180s', (base / 'last_report.html').read_text())

    def test_total_budget_reports_unscanned_sources(self):
        site = {'name': 'Unscanned', 'url': 'https://example.test'}
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            config = base / 'config.json'
            config.write_text(json.dumps({'sites': [site], 'scan_timeout_seconds': 1}))
            with (patch.object(monitor, 'BASE_DIR', base), patch.object(monitor, 'load_state', return_value={'sites': {}}),
                  patch.object(monitor.time, 'monotonic', side_effect=[0, 2, 3]),
                  patch.object(monitor, 'scan_site') as scan, patch.object(monitor, 'save_state') as save,
                  patch('sys.argv', ['monitor', '--config', str(config), '--dry-run'])):
                monitor.main()
            scan.assert_not_called()
            save.assert_not_called()
            self.assertIn('Not scanned', json.loads((base / 'scan_results.json').read_text())[0]['error'])


if __name__ == '__main__':
    unittest.main()
