# Repository guidance

This repository runs a daily PE/investment career-page monitor on GitHub Actions.
Read README.md for the current architecture, extraction evidence rules, state
migration, configuration, read-only testing and CV-pipeline retry behavior.

Run checks with:

```bash
python -m py_compile job_monitor.py job_sources.py export_linkedin_cookies.py
python -m unittest discover -s tests -v
python job_monitor.py --config config.github.json --dry-run
```

Do not replace conservative vacancy evidence with arbitrary heading/keyword
matching. Test real regressions: LinkedIn search suggestions, employer mismatch,
redirects, nested cards, empty main elements, session-token deduplication, failed
SMTP, blocked sources and dry-run side effects. Preserve configured priority
filters. Unsupported sources must be marked for manual review, not reported as
zero vacancies. Keep credentials out of source, logs and artifacts.

All production changes go through a reviewed/tested branch and PR before main.
The scheduled workflow runs from main and commits state.json itself; do not
force-push main or overwrite state produced by a concurrent scan.

Claude Code pushes must use a branch named claude/<task>-<session-id>, consistent
with that client's existing repository permission restrictions.
