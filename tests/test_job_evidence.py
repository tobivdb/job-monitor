import json
import unittest
from unittest.mock import MagicMock, patch

from bs4 import BeautifulSoup

import job_monitor as monitor
from job_sources import canonical_url, is_detail_url, verify_detail


class JobEvidenceTests(unittest.TestCase):
    config = {"name": "Example", "url": "https://example.test/careers/"}

    def extract(self, body, config=None, final_url=""):
        return monitor.extract_jobs_from_page(body, config or self.config, final_url)

    def test_today_linkedin_suggestion_is_not_seven_jobs(self):
        body = '<a href="/jobs/search-results/?keywords=Private+Equity+Investment+Professional&amp;origin=SUGGESTION">Private Equity Investment Professional<span>Jobs</span></a>'
        for slug in ("calibrium-ag", "equistone-partners-europe", "genui", "gyrus-capital-s-a", "hbm-partners-ag", "inflexion-private-equity", "rianta-capital-limited"):
            with self.subTest(slug=slug):
                result = self.extract(body, {"name": slug, "url": f"https://www.linkedin.com/company/{slug}/jobs/"})
                self.assertEqual(result.jobs, [])
                self.assertTrue(result.warnings)

    def test_team_bios_testimonials_and_generic_headings_are_not_jobs(self):
        result = self.extract('''<div class="career"><h2>Richard Bauer Project Associate, 2025</h2></div>
            <h2>Investment Director</h2><h3>Senior Advisors</h3><h2>The founders of ARENIT</h2>
            <h3>Private Equity Investment ProfessionalJobs</h3>''')
        self.assertEqual(result.jobs, [])

    def test_own_link_title_wins_over_unrelated_page_heading(self):
        result = self.extract('''<h2>Investment Manager</h2>
          <a href="/job/1">Analyst</a><a href="/job/2">Associate</a>''')
        self.assertEqual({j.title for j in result.jobs}, {"Analyst", "Associate"})

    def test_apply_link_uses_single_job_card_heading(self):
        result = self.extract('<article><h3>Investment Associate</h3><a href="/job/123">Apply now</a></article>')
        self.assertEqual(result.jobs[0].title, "Investment Associate")

    def test_multi_job_container_does_not_assign_first_title_to_second_ad(self):
        result = self.extract('<div><h3>Analyst</h3><h3>Associate</h3><a href="/job/1">Apply</a><a href="/job/2">Apply</a></div>')
        self.assertEqual(result.jobs, [])

    def test_relative_links_use_redirect_destination_and_document_base(self):
        result = self.extract('<a href="job/123">Investment Associate</a>', final_url="https://ats.test/board/")
        self.assertEqual(result.jobs[0].url, "https://ats.test/board/job/123")
        result = self.extract('<base href="https://ats.test/board/"><a href="job/123">Investment Associate</a>')
        self.assertEqual(result.jobs[0].url, "https://ats.test/board/job/123")

    def test_duplicate_rows_and_tracking_parameters_are_one_job(self):
        result = self.extract('''<a href="/job/123?utm_source=email">Investment Associate</a>
          <a href="/job/123?utm_source=web">Investment Associate Zurich</a>''')
        self.assertEqual(len(result.jobs), 1)
        self.assertEqual(result.jobs[0].title, "Investment Associate")

    def test_successfactors_session_changes_do_not_create_new_jobs(self):
        first = monitor.JobEntry("Associate", "https://ats.test/career?career_job_req_id=123&_s.crb=old&browserTimeZone=UTC")
        second = monitor.JobEntry("Associate Zurich", "https://ats.test/career?browserTimeZone=Europe%2FZurich&career_job_req_id=123&_s.crb=new")
        self.assertEqual(first.key, second.key)
        self.assertIn("career_job_req_id=123", canonical_url(first.url))

    def test_non_web_schemes_and_search_links_are_rejected(self):
        for url in ("javascript:alert(1)", "mailto:test@example.test", "#apply", "data:text/html,hello"):
            self.assertEqual(canonical_url(url, self.config["url"]), "")
        for url in ("https://www.linkedin.com/jobs/search-results/?keywords=Associate", "https://example.test/jobs/search", "https://example.test/careers/"):
            self.assertFalse(is_detail_url(url))

    def test_linkedin_job_requires_matching_employer(self):
        config = {"name": "Example (LinkedIn)", "url": "https://www.linkedin.com/company/example/jobs/"}
        body = '<li><a href="/jobs/view/investment-associate-123456">Investment Associate</a><a href="/company/{company}/">Employer</a></li>'
        self.assertEqual(self.extract(body.format(company="other"), config).jobs, [])
        result = self.extract(body.format(company="example"), config)
        self.assertEqual(result.jobs[0].url, "https://www.linkedin.com/jobs/view/123456/")

    def test_jsonld_graph_is_read_before_scripts_are_removed(self):
        posting = {"@context": "https://schema.org", "@graph": [{"@type": "JobPosting", "title": "Investment Associate", "url": "/vacancy/123", "description": "Responsibilities and qualifications", "jobLocation": {"address": {"addressLocality": "Zurich"}}}]}
        result = self.extract('<script type="application/ld+json">' + json.dumps(posting) + '</script>')
        self.assertEqual(len(result.jobs), 1)
        self.assertEqual(result.jobs[0].location, "Zurich")

    def test_expired_jsonld_is_excluded(self):
        posting = {"@type": "JobPosting", "title": "Associate", "url": "/job/123", "validThrough": "2000-01-01"}
        self.assertEqual(self.extract('<script type="application/ld+json">' + json.dumps(posting) + '</script>').jobs, [])

    def test_hidden_no_jobs_template_does_not_discard_valid_links(self):
        config = dict(self.config, no_jobs_indicators=["No jobs found"])
        result = self.extract('<div hidden>No jobs found</div><a href="/job/123">Associate</a>', config)
        self.assertEqual(len(result.jobs), 1)
        self.assertFalse(result.has_no_jobs_indicator)

    def test_allowlist_does_not_borrow_location_from_another_job(self):
        config = dict(self.config, include_job_patterns=["Zurich"])
        result = self.extract('<a href="/job/1">Associate Boston</a><a href="/job/2">Associate Zurich</a>', config)
        self.assertEqual([j.title for j in result.jobs], ["Associate Zurich"])

    def test_company_description_does_not_make_support_roles_pe_jobs(self):
        for title in ("IT Engineer", "Legal Associate", "Fund Services Legal Specialist", "Fund Analyst Operations"):
            self.assertFalse(monitor.is_pe_relevant(title, "We are a private equity investment firm"))
        self.assertTrue(monitor.is_pe_relevant("Private Equity Investment Associate"))

    def detail_page(self, text, status=200, url="https://example.test/job/123"):
        page = MagicMock()
        page.url = url
        page.goto.return_value.status = status
        page.goto.return_value.headers = {"content-type": "text/html"}
        page.content.return_value = f'<main><h1>Investment Associate</h1><p>{text}</p></main>'
        return page

    def test_detail_verification_requires_matching_title_and_application_evidence(self):
        job = monitor.JobEntry("Investment Associate", "https://example.test/job/123")
        page = self.detail_page("Responsibilities and qualifications. Apply now. " * 20)
        self.assertEqual(verify_detail(page, job, self.config)[0], job.url)
        job.title = "Investment Director"
        with self.assertRaisesRegex(ValueError, "title not confirmed"):
            verify_detail(page, job, self.config)

    def test_closed_404_and_login_pages_are_not_accepted(self):
        job = monitor.JobEntry("Investment Associate", "https://example.test/job/123")
        for page in (self.detail_page("No longer accepting applications"), self.detail_page("Apply now", status=404), self.detail_page("", url=self.config["url"])):
            with self.assertRaises(ValueError):
                verify_detail(page, job, self.config)

    def test_failed_detail_is_suppressed_and_reported(self):
        job = monitor.JobEntry("Investment Associate", "https://example.test/job/123")
        result = monitor.SiteResult("Example", self.config["url"], jobs=[job])
        context = MagicMock()
        context.new_page.return_value = self.detail_page("", status=404)
        monitor.validate_jobs(result, self.config, context)
        self.assertEqual(result.jobs, [])
        self.assertEqual(result.unverified_urls, [job.url])
        self.assertTrue(result.warnings)

    def test_email_escapes_attributes_and_has_working_plain_text_fallback(self):
        card = monitor._job_card(monitor.JobEntry('<img src=x> & Associate', 'javascript:alert(1)'), '<Company>', 'https://example.test/?x="onmouseover="bad')
        soup = BeautifulSoup(card, "html.parser")
        self.assertIsNone(soup.find("img"))
        self.assertNotIn('href="javascript:', card)
        self.assertNotIn(' onmouseover=', card)
        self.assertIn("Direct link unavailable", card)

    def test_eqt_nested_flip_card_resolves_actual_greenhouse_ad(self):
        result = self.extract('''<div><div><h3>Investment Associate</h3><p>London</p></div>
          <div><div><div><a href="https://job-boards.eu.greenhouse.io/eqtpartners/jobs/123">Learn more</a></div></div></div></div>''')
        self.assertEqual(len(result.jobs), 1)
        self.assertEqual(result.jobs[0].title, "Investment Associate")

    def test_empty_main_does_not_hide_partners_group_detail(self):
        job = monitor.JobEntry("Investment Associate", "https://example.test/job/123")
        page = self.detail_page("")
        page.content.return_value = '<main></main><section><h1>Investment Associate</h1><p>' + 'Responsibilities and qualifications. Apply now. ' * 20 + '</p></section>'
        self.assertEqual(verify_detail(page, job, self.config)[0], job.url)

    def test_unigestion_contract_suffix_is_not_required_in_detail_heading(self):
        job = monitor.JobEntry("Associate - Permanent Contract (100%)", "https://example.test/jobs/123")
        page = self.detail_page("")
        page.content.return_value = '<h3>Associate</h3><p>' + 'Qualifications and responsibilities. Apply now. ' * 20 + '</p>'
        self.assertTrue(verify_detail(page, job, self.config)[1])

    def test_closed_page_cannot_be_revived_by_stale_jsonld(self):
        job = monitor.JobEntry("Investment Associate", "https://example.test/job/123")
        page = self.detail_page("")
        posting = {"@type": "JobPosting", "title": job.title, "description": "Responsibilities. " * 50}
        page.content.return_value = '<p>No longer accepting applications</p><script type="application/ld+json">' + json.dumps(posting) + '</script>'
        with self.assertRaisesRegex(ValueError, "closed"):
            verify_detail(page, job, self.config)

    def test_pdf_requires_ad_text_and_matching_title(self):
        job = monitor.JobEntry("Investment Associate", "https://example.test/vacancy.pdf")
        page = MagicMock()
        response = page.request.get.return_value
        response.status = 200
        response.headers = {"content-type": "application/pdf"}
        response.body.return_value = b"%PDF-test"
        response.url = job.url
        pdf = MagicMock()
        pdf.pages = [MagicMock()]
        pdf.pages[0].extract_text.return_value = "Investment Associate. Apply now. Responsibilities. " * 20
        with patch("job_sources.PdfReader", return_value=pdf):
            self.assertEqual(verify_detail(page, job, self.config)[0], job.url)
            pdf.pages[0].extract_text.return_value = "Company brochure"
            with self.assertRaises(ValueError):
                verify_detail(page, job, self.config)

    def test_http_error_during_listing_fetch_is_a_failure(self):
        context = MagicMock()
        context.new_page().goto().status = 503
        with self.assertRaisesRegex(ValueError, "503"):
            monitor.fetch_page(self.config["url"], context)
        context.new_page().close.assert_called()


if __name__ == "__main__":
    unittest.main()
