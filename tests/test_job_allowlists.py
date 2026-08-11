import unittest

import job_monitor


class JobAllowlistTests(unittest.TestCase):
    HTML = """
    <html><body>
      <a href="https://harbourvest.wd5.myworkdayjobs.com/en-US/HVP/job/Boston/Associate--Primary-Investments_R1">
        Associate, Primary Investments
      </a>
      <a href="https://harbourvest.wd5.myworkdayjobs.com/en-US/HVP/job/London/Associate--Infrastructure_R2">
        Associate, Infrastructure and Real Assets
      </a>
      <a href="https://harbourvest.wd5.myworkdayjobs.com/en-US/HVP/job/Dublin/Director--Fund-Oversight_R3">
        Director, Fund Oversight
      </a>
      <a href="https://harbourvest.wd5.myworkdayjobs.com/en-US/HVP/job/Singapore/Senior-Associate--Performance_R4">
        Senior Associate, Performance Measurement
      </a>
    </body></html>
    """

    def test_allowlist_keeps_only_matching_job_urls(self):
        config = {
            "name": "HarbourVest Partners",
            "url": "https://harbourvest.wd5.myworkdayjobs.com/HVP",
            "include_job_patterns": [
                "/job/london/",
                "/job/dublin/",
                "/job/frankfurt/",
                "/job/zurich/",
            ],
        }

        result = job_monitor.extract_jobs_from_page(self.HTML, config)

        self.assertEqual(
            {job.title for job in result.jobs},
            {
                "Associate, Infrastructure and Real Assets",
                "Director, Fund Oversight",
            },
        )
        self.assertTrue(all("/Boston/" not in job.url for job in result.jobs))
        self.assertTrue(all("/Singapore/" not in job.url for job in result.jobs))

    def test_sites_without_allowlist_keep_all_matching_jobs(self):
        config = {
            "name": "Global Workday",
            "url": "https://harbourvest.wd5.myworkdayjobs.com/HVP",
        }

        result = job_monitor.extract_jobs_from_page(self.HTML, config)

        self.assertEqual(len(result.jobs), 4)

    def test_global_filter_rejects_explicit_non_european_workday_locations(self):
        config = {
            "name": "Global Workday",
            "url": "https://harbourvest.wd5.myworkdayjobs.com/HVP",
            "global_excluded_location_patterns": ["Boston", "Singapore"],
        }

        result = job_monitor.extract_jobs_from_page(self.HTML, config)

        self.assertEqual(
            {job.title for job in result.jobs},
            {
                "Associate, Infrastructure and Real Assets",
                "Director, Fund Oversight",
            },
        )

    def test_oracle_location_labels_are_filtered(self):
        html = """
        <html><body>
          <a href="https://example.oraclecloud.com/sites/CX_2/job/1853">
            Client Director - Wealth, Hong Kong Locations Hong Kong Posting Date 08/06/2026
          </a>
          <a href="https://example.oraclecloud.com/sites/CX_2/job/1854">
            Investment Associate Locations Paris Posting Date 08/06/2026
          </a>
        </body></html>
        """
        config = {
            "name": "Global Oracle",
            "url": "https://example.oraclecloud.com/sites/CX_2/jobs",
            "global_excluded_location_patterns": ["Hong Kong", "Singapore"],
        }

        result = job_monitor.extract_jobs_from_page(html, config)

        self.assertEqual(len(result.jobs), 1)
        self.assertIn("Paris", result.jobs[0].title)

    def test_job_title_region_does_not_override_european_office(self):
        html = """
        <html><body>
          <a href="https://example.wd5.myworkdayjobs.com/HVP/job/London/Investment-Associate-United-States-Coverage_R5">
            Investment Associate, United States Coverage
          </a>
        </body></html>
        """
        config = {
            "name": "Global Workday",
            "url": "https://example.wd5.myworkdayjobs.com/HVP",
            "global_excluded_location_patterns": ["Americas", "United States"],
        }

        result = job_monitor.extract_jobs_from_page(html, config)

        self.assertEqual(
            [job.title for job in result.jobs],
            ["Investment Associate, United States Coverage"],
        )


if __name__ == "__main__":
    unittest.main()
