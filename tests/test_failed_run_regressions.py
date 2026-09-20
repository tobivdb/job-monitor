import unittest
from unittest.mock import MagicMock, patch
from bs4 import BeautifulSoup
from job_sources import candidates, is_detail_url
from site_crawl import Crawl, career_links, collect_board, fingerprint, NEXT, declared_job_count
from verify_scan import assess, operational_issues


class FailedRunRegressionTests(unittest.TestCase):
    def test_people_directory_load_more_is_not_job_pagination(self):
        page=MagicMock(); page.url='https://fund.test/people/'
        page.content.return_value='<a href="/people/jane">Jane Doe, Investment Director</a><button>Load More</button><footer><a href="/policy.pdf">Privacy Policy</a></footer>'
        page.get_by_role.return_value.first.count.return_value=0
        result=Crawl()
        with patch('site_crawl.next_control') as control:
            collect_board(page,result,10,float('inf'))
        control.assert_not_called()
        self.assertEqual(len(result.documents),1)
        self.assertEqual(result.warnings,[])

    def test_skeeled_links_and_card_title_enable_real_pagination(self):
        base='https://novastone-ca.careers/board/abc'
        one='https://app.skeeled.com/offer/c/6a75efa492b4765f606604b9?language=en'
        two='https://app.skeeled.com/offer/c/6a75f12c8e685014c5f6506a?language=en'
        html=f'<a href="{one}"><div class="v-card-title">CEO Through Acquisition France 2026</div><span>Full-Time Baar Switzerland</span></a>'
        self.assertTrue(is_detail_url(one))
        self.assertFalse(is_detail_url('https://unrelated.test/offer/c/6a75efa492b4765f606604b9'))
        self.assertNotEqual(fingerprint(html,base), fingerprint(html.replace(one,two),base))
        ads=list(candidates(BeautifulSoup(html,'lxml'),{'name':'NCA','url':base},base))
        self.assertEqual(ads[0][0],'CEO Through Acquisition France 2026')

    def test_skeeled_uses_its_published_title_element_and_still_rejects_mismatch(self):
        from job_sources import verify_detail
        from job_monitor import JobEntry
        url='https://app.skeeled.com/offer/c/6a75efa492b4765f606604b9'
        page=MagicMock();page.url=url;page.goto.return_value.status=200;page.goto.return_value.headers={}
        page.content.return_value='<div class="text-display-large">CEO Through Acquisition France 2026</div><main>'+('Responsibilities and requirements. '*20)+'Apply now</main>'
        cfg={'name':'NCA','url':'https://novastone-ca.careers/'}
        self.assertEqual(verify_detail(page,JobEntry('CEO Through Acquisition France 2026',url),cfg)[0],url)
        with self.assertRaises(ValueError):
            verify_detail(page,JobEntry('Investment Analyst',url),cfg)

    def test_navigation_can_publish_the_only_career_link(self):
        html='<nav><a href="/people/jane">Investment Director</a><a href="/careers/">Careers</a></nav>'
        self.assertEqual(career_links(html,'https://fund.test/'),['https://fund.test/careers/'])

    def test_teamtailor_more_count_is_a_pagination_control(self):
        self.assertTrue(NEXT.fullmatch('Show 7 more'))
        self.assertEqual(declared_job_count('<h2>27 jobs</h2>'),27)
        self.assertIsNone(declared_job_count('<p>Our companies created 500 jobs</p>'))

    def test_external_site_failure_stays_a_coverage_issue_not_execution_failure(self):
        results=[{'name':'Working','error':None,'warnings':[],'coverage_complete':True,
                  'verified_jobs':[{'title':'Associate','url':'https://fund.test/job/1'}]},
                 {'name':'Unavailable','error':None,'warnings':['Career page could not be read: https://other.test/ (HTTP 503).'],
                  'coverage_complete':False,'verified_jobs':[]}]
        self.assertTrue(assess(results,['Working','Unavailable']))
        self.assertEqual(operational_issues(results,['Working','Unavailable']),[])

    def test_operational_gate_still_rejects_missing_sources_and_worker_bugs(self):
        result={'name':'A','error':'TypeError: broken parser','verified_jobs':[],'warnings':[]}
        issues=operational_issues([result],['A','B'])
        self.assertTrue(any('inventory' in s for s in issues))
        self.assertTrue(any('worker failed' in s for s in issues))
        self.assertTrue(any('No verified adverts' in s for s in issues))


if __name__=='__main__': unittest.main()
