"""Screenshot one page of the approval UI in a headless browser. Only used with --screenshots.

    uv run --no-project --with playwright python shots.py '<json job>'

The job carries the UI's origin, its secret path prefix, the reviewer's session cookie, the page
(a path under the prefix) and the output file. The browser has the cookie of the session the
demo's reviewer already signed in with, and only loads pages (GET), so nothing is decided here.
It prints the notices the page showed (a notice is shown once, on the page a decision redirects
to) as a JSON list on the last line of stdout.

Playwright needs a browser: `uv run --no-project --with playwright playwright install chromium`
the first time.
"""

from __future__ import annotations

import json
import sys
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

WIDTH = 1100


def capture(job: dict) -> list[str]:
    origin, base = job["origin"], job["base"]
    name, value = job["cookie"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            context = browser.new_context(viewport={"width": WIDTH, "height": 700})
            context.add_cookies(
                [
                    {
                        "name": name,
                        "value": value,
                        "domain": urlsplit(origin).hostname,
                        "path": f"{base}/",
                        "httpOnly": True,
                        "sameSite": "Strict",
                    }
                ]
            )
            page = context.new_page()
            response = page.goto(f"{origin}{base}{job['path']}")
            if response is None or response.status != 200:
                raise RuntimeError(f"{job['path']} answered {response and response.status}")
            page.wait_for_load_state("networkidle")
            notices = page.locator(".flash").all_inner_texts()
            page.screenshot(path=job["out"], full_page=True)
            return [" ".join(text.split()) for text in notices]
        finally:
            browser.close()


if __name__ == "__main__":
    print(json.dumps(capture(json.loads(sys.argv[1]))))
