#!/usr/bin/env python3
"""
Export LinkedIn session cookies using Playwright.

Opens a real browser window for you to log into LinkedIn manually,
then exports the cookies as JSON for use by the job monitor.

Usage:
    python export_linkedin_cookies.py
"""

import json
from urllib.parse import urlsplit
from pathlib import Path
from playwright.sync_api import sync_playwright

COOKIES_FILE = Path(__file__).parent / "linkedin_cookies.json"


def main():
    print("Opening browser — please log into LinkedIn...")
    print("(The script will wait for you to complete login, including any 2FA.)\n")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)  # Visible browser!
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="de-CH",
        )
        page = context.new_page()
        page.goto("https://www.linkedin.com/login")

        # Wait until the user has logged in (URL changes away from login)
        print("Waiting for you to log in...")
        while True:
            page.wait_for_timeout(2000)
            url = page.url
            parts = urlsplit(url)
            if parts.hostname in {"www.linkedin.com", "linkedin.com"} and parts.path.rstrip("/") in {"/feed", "/mynetwork"}:
                break

        print(f"Logged in! Current URL: {page.url}")

        # Get all cookies
        cookies = context.cookies()

        # Filter to LinkedIn cookies only
        linkedin_cookies = [c for c in cookies if c.get("domain", "").lstrip(".") in {"linkedin.com", "www.linkedin.com"}]

        # Save to file
        COOKIES_FILE.write_text(json.dumps(linkedin_cookies, indent=2), encoding="utf-8")
        print(f"\nExported {len(linkedin_cookies)} LinkedIn cookies to: {COOKIES_FILE}")
        print("\nYou can now close this browser window.")

        browser.close()


if __name__ == "__main__":
    main()
