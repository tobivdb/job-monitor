"""Automated source-health checks and a read-only live regression sample."""
import argparse
import json
import os
from pathlib import Path


def assess(results, expected_names):
    issues = []
    actual = [r['name'] for r in results]
    if len(actual) != len(set(actual)) or set(actual) != set(expected_names):
        issues.append('Source inventory differs from configured sources')
    for result in results:
        if result.get('error'):
            issues.append(result['name'] + ': source failed')
        for warning in result.get('warnings', []):
            if any(token in warning.lower() for token in ('pagination', 'next page', 'time budget', 'unvisited', 'configured page limit', 'rate limited', 'could not be read')):
                issues.append(result['name'] + ': ' + warning)
    return issues


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--report', default='scan_results.json')
    parser.add_argument('--config', default='config.github.json')
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding='utf-8'))
    if args.live:
        # Portal traversal, same-page advert discovery, title variants and closure
        # validation. Tests do not send email, touch state or write a Google Sheet.
        from job_monitor import scan_many, write_scan_audit
        names = {'Pinova Capital', 'Afinum', 'Egeria Group', 'ICG'}
        sites = [s for s in config['sites'] if s['name'] in names]
        if len(sites) != len(names):
            raise SystemExit('Live regression source configuration is incomplete')
        results = [r for _, r in scan_many(sites, 240, 15*60, 4)]
        write_scan_audit(results)
    else:
        sites = config['sites']
    results = json.loads(Path(args.report).read_text(encoding='utf-8'))
    issues = assess(results, [s['name'] for s in sites])
    if args.live:
        issues += [r['name'] + ': no verified adverts in live regression source' for r in results if not r['verified_jobs']]
    checked = sum(bool(r.get('coverage_complete')) for r in results)
    summary = {'sources': len(results), 'checked': checked, 'needs_review': len(results)-checked,
               'verified_jobs': sum(len(r['verified_jobs']) for r in results), 'failures': issues,
               'note': 'Checked means observed pages and candidates were processed; it does not prove all vacancies on the internet were discovered.'}
    Path('quality_report.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    text = (f"Sources: {summary['sources']}; checked: {checked}; needs review: {summary['needs_review']}; "
            f"verified adverts: {summary['verified_jobs']}\n" + '\n'.join(issues))
    print(text)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as f:
            f.write('\n### Coverage verification\n' + text + '\n' + summary['note'] + '\n')
    if issues:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
