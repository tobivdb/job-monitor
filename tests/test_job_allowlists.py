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


if __name__ == "__main__":
    unittest.main()
