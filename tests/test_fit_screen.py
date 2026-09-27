import io
import json
import os
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import fit_screen
import job_monitor as monitor
from job_sources import verify_detail
from tracker_fields import clean_job_title, employer_title_key, normalize_location, posted_date, posting_location


def response(fit='High', **changes):
    result = dict(fit=fit, clean_title='Investment Associate', employer='Example',
                  location='Zürich, Switzerland', summary='Direct small-cap investments. Strong deal fit; sector gap.',
                  reason='DACH small-cap role at appropriate seniority.')
    result.update(changes)
    return {'status': 'completed', 'output': [{'type': 'message', 'content': [
        {'type': 'output_text', 'text': json.dumps(result)}]}]}


def opener_for(data):
    return MagicMock(side_effect=lambda *a, **k: io.StringIO(json.dumps(data)))


class ScreenTests(unittest.TestCase):
    def call(self, opener):
        return fit_screen.screen_job(title='Associate', company='Example', tier='EU', notes='Small cap',
                                     location='Zurich', description='x' * 8000, opener=opener)

    @patch.dict(os.environ, {'OPENAI_API_KEY': 'test-only', 'OPENAI_SCREEN_MODEL': 'gpt-5-mini'})
    def test_request_contract_and_bounded_input(self):
        opener = opener_for(response())
        self.assertEqual(self.call(opener)['fit'], 'High')
        request = opener.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(opener.call_args.kwargs, {'timeout': 60})
        self.assertEqual(payload['model'], 'gpt-5-mini')
        self.assertFalse(payload['store'])
        self.assertNotIn('tools', payload)
        self.assertEqual(payload['max_output_tokens'], 600)
        self.assertTrue(payload['text']['format']['strict'])
        self.assertFalse(payload['text']['format']['schema']['additionalProperties'])
        self.assertEqual(len(json.loads(payload['input'])['verified_ad']), 6000)
        self.assertIn('EUR 37m', payload['instructions'])
        self.assertIn('Small cap', payload['input'])
        opener.assert_called_once()

    def test_missing_key_never_calls_api(self):
        opener = MagicMock()
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(fit_screen.ScreenError, 'missing'):
            self.call(opener)
        opener.assert_not_called()

    @patch.dict(os.environ, {'OPENAI_API_KEY': 'test-only'})
    def test_failed_incomplete_refused_and_malformed_results_fail_closed_without_retry(self):
        for data in ({'status': 'incomplete'}, {'status': 'completed', 'output': []},
                     response(fit='Maybe'), response(employer=''), response(extra='unexpected')):
            opener = opener_for(data)
            with self.subTest(data=data), self.assertRaises(fit_screen.ScreenError):
                self.call(opener)
            opener.assert_called_once()
        opener = MagicMock(side_effect=TimeoutError('SECRET request body'))
        with self.assertRaises(fit_screen.ScreenError) as ctx:
            self.call(opener)
        self.assertNotIn('SECRET', str(ctx.exception))
        opener.assert_called_once()

    def test_all_requested_exclusions_match_case_insensitively(self):
        for term in fit_screen.PREFILTER_TERMS:
            with self.subTest(term=term):
                self.assertTrue(fit_screen.prefilter_reason(term.upper() + ' Specialist'))
        for title in ('Praktikant Investments', 'Recruiting Manager', 'Internship', 'Geschäftsführer'):
            self.assertTrue(fit_screen.prefilter_reason(title))
        for title in ('Investment Associate', 'Senior Investment Manager', 'International M&A Manager',
                      'Head of M&A', 'Vice President Investments'):
            self.assertFalse(fit_screen.prefilter_reason(title))


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.state = {'sites': {}}
        self.job = monitor.JobEntry('Investment Associate (m/w/d) - Zürich', 'https://example.test/job/1',
                                    'Zurich', date_posted='2026-09-20T09:00:00Z')
        self.candidate = {'job': self.job, 'site': monitor.SiteDiff('Example', 'https://example.test/jobs', tier='EU'),
                          'description': 'Verified responsibilities\nand requirements. ' * 30, 'notes': 'Small-cap fund'}
        self.drive, self.sheets = MagicMock(), MagicMock()
        self.values = self.sheets.spreadsheets().values()
        self.values.get.return_value.execute.return_value = {'values': []}
        self.services = patch.object(monitor, '_google_services', return_value=(self.drive, self.sheets))
        self.sheet_id = patch.object(monitor, '_find_tracker_sheet_id', return_value='sheet-id')
        self.services.start(); self.sheet_id.start()
        self.addCleanup(self.services.stop); self.addCleanup(self.sheet_id.stop)
        self.env = patch.dict(os.environ, {'OPENAI_API_KEY': 'test-only'})
        self.env.start(); self.addCleanup(self.env.stop)

    def feed(self, payload=None, candidates=None, **kwargs):
        self.opener = opener_for(payload or response())
        with patch.object(fit_screen, 'urlopen', self.opener):
            return monitor.feed_tracker(candidates or [self.candidate], self.state, **kwargs)

    def test_ten_columns_high_full_text_real_newlines_and_comment(self):
        for month, zone in ((7, 'CEST'), (1, 'CET')):
            self.state = {'sites': {}}
            with patch.object(monitor, 'datetime') as clock:
                clock.now.return_value = datetime(2026, month, 1, 10, 5, tzinfo=ZoneInfo('Europe/Zurich'))
                report = self.feed(response(summary='LinkedIn screening is an untrusted ad fragment.'))
                clock.now.assert_called_once_with(ZoneInfo('Europe/Zurich'))
            row = self.values.append.call_args.kwargs['body']['values'][0]
            self.assertEqual(len(row), monitor.TRACKER_COLUMNS)
            self.assertEqual(len(row), 10)
            self.assertEqual(row[2:5], ['Example', 'Investment Associate', 'NEW'])
            self.assertEqual(row[5], f'2026-{month:02d}-01 10:05 {zone}')
            self.assertEqual(row[6:9], ['', 'High', 'Zürich, Switzerland'])
            self.assertTrue(row[1].endswith('\n\n' + self.candidate['description']))
            self.assertIn('Posted 2026-09-20 · Source: Career page https://example.test/job/1\n\n', row[1])
            self.assertTrue(row[9].startswith(f'Career page monitor 2026-{month:02d}-01 | Fit: High |'))
            self.assertIn('Source: Example https://example.test/jobs | Posted 2026-09-20', row[9])
            self.assertNotIn('LinkedIn screening', row[9])
            self.assertEqual(report[0]['fit'], 'High')
            self.assertNotIn(self.job.key, self.state['tracker_pending'])
        self.values.get.assert_called_with(spreadsheetId='sheet-id', range='A:D')

    def test_thin_medium_ad_and_unknown_metadata(self):
        self.candidate['description'] = 'Verified ad\n' * 30
        self.job.location = ''; self.job.date_posted = ''
        self.feed(response('Medium'))
        row = self.values.append.call_args.kwargs['body']['values'][0]
        self.assertEqual(row[4], 'NEEDS_REVIEW')
        self.assertEqual(row[7:9], ['Medium', ''])
        self.assertIn('Posted unknown', row[1])

    def test_low_is_remembered_and_never_rescreened(self):
        rejected = []
        self.assertEqual(self.feed(response('Low'), screened_out=rejected), [])
        self.assertEqual(self.state['tracker_screened_out'][self.job.key]['fit'], 'Low')
        self.assertEqual(len(rejected), 1)
        self.values.append.assert_not_called()
        self.assertEqual(self.feed(), [])
        self.opener.assert_not_called()

    def test_prefilter_records_low_without_api(self):
        self.job.title = 'Commercial Mortgage Specialist'
        self.assertEqual(self.feed(), [])
        self.opener.assert_not_called()
        self.assertIn(self.job.key, self.state['tracker_screened_out'])

    def test_failure_and_missing_key_remain_pending_without_append(self):
        for missing in (True, False):
            errors = []
            with patch.dict(os.environ, {'OPENAI_API_KEY': '' if missing else 'test-only'}):
                self.assertEqual(self.feed({'status': 'incomplete'}, errors=errors), [])
            self.assertTrue(errors)
            self.assertIn(self.job.key, self.state['tracker_pending'])
            self.assertNotIn(self.job.key, self.state['tracker_fed'])
            self.values.append.assert_not_called()
            self.assertEqual(self.opener.call_count, 0 if missing else 1)
        self.feed()
        self.assertIn(self.job.key, self.state['tracker_fed'])

    def test_url_and_employer_equivalent_title_dedupe_across_all_statuses(self):
        for row in ([self.job.url + '?utm_source=old', '', 'Other', 'Other'],
                    ['https://different.test/jobs/2', '', 'EXAMPLE', 'Associate Investment Team (f/m/d) - Zurich']):
            self.state = {'sites': {}}
            self.values.get.return_value.execute.return_value = {'values': [row]}
            self.assertEqual(self.feed(), [])
            self.opener.assert_not_called()
            self.values.append.assert_not_called()
            self.assertEqual(self.state['tracker_fed'][self.job.key]['date'], 'already in sheet')

    def test_append_failure_does_not_mark_batch_or_alias_fed(self):
        other = dict(self.candidate, job=monitor.JobEntry('Associate Investment Team', 'https://example.test/job/2'))
        self.values.append.return_value.execute.side_effect = RuntimeError('write failed')
        with self.assertRaises(RuntimeError):
            self.feed(candidates=[self.candidate, other])
        self.assertFalse(self.state['tracker_fed'])
        self.assertEqual(len(self.state['tracker_pending']), 2)

    def test_batch_duplicate_needs_only_one_screen_and_one_row(self):
        other = dict(self.candidate, job=monitor.JobEntry('Associate Investment Team', 'https://example.test/job/2'))
        self.feed(candidates=[self.candidate, other])
        self.opener.assert_called_once()
        self.assertEqual(len(self.values.append.call_args.kwargs['body']['values']), 1)
        self.assertEqual(len(self.state['tracker_fed']), 2)

    def test_budget_counts_failed_attempts_and_prefilter_still_runs_after_limit(self):
        candidates = [dict(self.candidate, job=monitor.JobEntry(f'Associate {i}', f'https://example.test/job/{i}')) for i in range(42)]
        excluded = dict(self.candidate, job=monitor.JobEntry('Tax Manager', 'https://example.test/job/tax'))
        errors = []
        self.feed({'status': 'incomplete'}, candidates=candidates + [excluded], errors=errors)
        self.assertEqual(self.opener.call_count, 40)
        self.assertEqual(len(self.state['tracker_pending']), 42)
        self.assertIn(excluded['job'].key, self.state['tracker_screened_out'])
        self.assertTrue(any('40-screening' in error for error in errors))
        self.values.append.assert_not_called()

    def test_stage_budget_reserves_a_full_timeout_and_keeps_overflow_pending(self):
        other = dict(self.candidate, job=monitor.JobEntry('Investment Manager', 'https://example.test/job/2'))
        errors = []
        with patch.object(monitor.time, 'monotonic', side_effect=[0, 0, 541]):
            self.feed(candidates=[self.candidate, other], errors=errors)
        self.opener.assert_called_once()
        self.assertIn(self.job.key, self.state['tracker_fed'])
        self.assertIn(other['job'].key, self.state['tracker_pending'])
        self.assertTrue(any('time budget' in error for error in errors))

    def test_report_contains_fit_location_reason_screened_out_and_transfer_errors(self):
        fed = self.feed()
        body, changes, _ = monitor.build_email_html([], tracker_fed=fed,
            tracker_screened_out=[dict(company='Other', title='Tax Manager', reason='Title exclusion: Tax')],
            tracker_error='API missing; candidates pending')
        for text in ('High', 'Zürich, Switzerland', fed[0]['reason'], 'Screened out', 'Tax Manager', 'API missing'):
            self.assertIn(text, body)
        self.assertTrue(changes)


class MetadataTests(unittest.TestCase):
    def test_locations_and_unknowns(self):
        for raw, expected in [('Zurich', 'Zürich, Switzerland'), ('Frankfurt Germany', 'Frankfurt, Germany'),
                              ('Vienna, AT', 'Wien, Austria'), ('Remote (DE)', 'Remote (Germany)'),
                              ('Remote', ''), ('Unknown', ''), ('Paris, DE', ''), ('Berlin / Munich', ''),
                              ('Nürnberg, Germany', 'Nürnberg, Germany')]:
            self.assertEqual(normalize_location(raw), expected)
        self.assertEqual(posting_location({'jobLocation': {'address': {'addressLocality': 'Zurich',
                         'addressCountry': {'name': 'CH'}}}}), 'Zürich, Switzerland')
        self.assertEqual(posting_location({'jobLocationType': 'TELECOMMUTE',
                         'applicantLocationRequirements': {'name': 'Germany'}}), 'Remote (Germany)')

    def test_dates_titles_and_seniority(self):
        self.assertEqual(posted_date('2026-02-30'), 'unknown')
        self.assertEqual(clean_job_title('Investment Associate (m/w/d) - Zürich'), 'Investment Associate')
        self.assertEqual(employer_title_key('Exämple AG', 'Investment Associate'),
                         employer_title_key('Example AG', 'Associate Investment Team (f/m/d), Zurich'))
        self.assertNotEqual(employer_title_key('Example', 'Associate'), employer_title_key('Example', 'Senior Associate'))

    def test_detail_metadata_and_full_description_survive_without_weakening_evidence(self):
        posting = {'@type': 'JobPosting', 'title': 'Investment Associate', 'datePosted': '2026-09-20T00:00:00Z',
                   'description': '<p>Responsibilities and qualifications.</p>' * 500,
                   'jobLocation': {'address': {'addressLocality': 'Frankfurt', 'addressCountry': 'DE'}}}
        page = MagicMock(); page.url = 'https://example.test/job/1'
        page.goto.return_value.status = 200; page.goto.return_value.headers = {}
        page.content.return_value = '<script type="application/ld+json">' + json.dumps(posting) + '</script>'
        job = monitor.JobEntry('Investment Associate', page.url)
        _, description = verify_detail(page, job, {'name': 'Example', 'url': 'https://example.test/jobs'})
        self.assertEqual(job.location, 'Frankfurt, Germany')
        self.assertEqual(job.date_posted, '2026-09-20')
        self.assertGreater(len(description), 12000)
        self.assertIn('\n', description)
        posting['jobLocation'] = [posting['jobLocation'], {'address': {'addressLocality': 'Berlin', 'addressCountry': 'DE'}}]
        page.content.return_value = '<script type="application/ld+json">' + json.dumps(posting) + '</script>'
        verify_detail(page, job, {'name': 'Example', 'url': 'https://example.test/jobs'})
        self.assertEqual(job.location, '')

    def test_explicit_feed_overrides_fallback_and_default_is_false(self):
        self.assertFalse(monitor.site_feed_enabled({'tier': 'A'}, {}))
        self.assertTrue(monitor.site_feed_enabled({'tier': 'A'}, {'tiers': ['A', 'B']}))
        self.assertFalse(monitor.site_feed_enabled({'tier': 'A', 'feed': False}, {'tiers': ['A']}))
        self.assertTrue(monitor.site_feed_enabled({'tier': 'EU', 'feed': True}, {'tiers': ['A']}))
        self.assertFalse(monitor.site_feed_enabled({'feed': 'true'}, {}))


if __name__ == '__main__':
    unittest.main()
