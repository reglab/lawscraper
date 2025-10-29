"""
File to be used for scraping statutes from FindLaw. This was made to be used primarily for scraping the ND, KY, and PA statutes.

FindLaw requires interaction with Javascript to get to the actual statutes, so this scraper uses Playwright to handle that.


The output format is identical to the other scrapers, with each statute being a JSON object written to a JSONL file. For example:
{"url": "https://law.justia.com/codes/kansas/2023/chapter-1/article-2/section-1-201/", "state": "KS", "path": "Justia\u203aU.S. Law\u203aU.S. Codes and Statutes\u203aKansas Statutes\u203a2023 Kansas Statutes\u203aChapter 1 - Accountants, Certified Public\u203aArticle 2 - State Board Of Accountancy\u203a1-201 Membership; appointment; qualifications; term; vacancies; removal.", "title": "2023 Kansas Statutes \u203a Chapter 1 - Accountants, Certified Public \u203a Article 2 - State Board Of Accountancy \u203a 1-201 Membership; appointment; qualifications; term; vacancies; removal.", "univ_cite": true, "citation": "KS Stat \u00a7 1-201 (2023)", "content": "1-201.\nMembership; appointment; qualifications; term; vacancies; removal.\n(a) There is hereby created a board of accountancy,...", "lex_path": [0, 0, 0]}
"""

import logging
import os
import re
import time
from typing import Dict, List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from requests.adapters import HTTPAdapter
from tqdm import tqdm
from urllib3.util.retry import Retry

FL_BASE_URL = "https://codes.findlaw.com/{state}"
MISSING_STATES = ["nd", "ky", "pa"]

DEFAULT_DIR = (
    "findlaw_codes"  # Directory to save the scraped data, e.g. findlaw_codes/ND.jsonl
)


def scrape_state(state: str, output_dir: str) -> None:
    """
    Scrape statutes for a given state from FindLaw and save them to a JSONL file.

    Args:
    - state (str): The state abbreviation (e.g., 'nd', 'ky', 'pa').
    - output_dir (str): The directory to save the output JSONL file.

    At the first step (the state page), we can use requests and BeautifulSoup. The first sections of the code will have class fl-list-item-link within a div with class landingContent
    After that, we need to use Playwright to handle the Javascript.
    """

    def _goto_with_retry(page: Page, url: str, attempts: int = 3) -> bool:
        """
        Navigate to a URL with retries, using looser wait conditions than networkidle.
        Returns True on success, False on repeated timeout.
        """
        import logging

        for i in range(attempts):
            try:
                # Add a console logger to see page-side errors in Python logs
                # try:
                #     page.on("console", lambda msg: logging.warning(f"PAGE CONSOLE: {msg.type} :: {msg.text}"))
                # except Exception:
                #     pass
                # 'domcontentloaded' is more reliable for JS-heavy pages than 'load' or 'networkidle'
                page.goto(url, timeout=60000, wait_until="domcontentloaded")
                # Wait for a generic body element as a lighter signal the page has painted
                page.wait_for_selector("body", timeout=15000)
                return True
            except PlaywrightTimeoutError:
                logging.warning(
                    f"Timeout loading {url} (attempt {i+1}/{attempts}); backing off and retrying…"
                )
                # Try a soft reload once after a failed attempt
                try:
                    page.reload(timeout=30000, wait_until="domcontentloaded")
                except Exception:
                    pass
                time.sleep(2 * (i + 1))
            except Exception as e:
                logging.warning(
                    f"Navigation error on {url} (attempt {i+1}/{attempts}): {e}"
                )
                time.sleep(2 * (i + 1))
        logging.error(f"Timeout while loading page after {attempts} attempts: {url}")
        return False

    state = state.lower()
    if state not in MISSING_STATES:
        raise ValueError(
            f"State {state} is not in the list of missing states: {MISSING_STATES}"
        )

    state_url = FL_BASE_URL.format(state=state)

    # Use a session and realistic browser headers to reduce 403 responses from FindLaw
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Referer": "https://www.google.com/",
        }
    )

    try:
        response = session.get(state_url, timeout=30)
    except requests.RequestException as e:
        logging.error(f"Network error retrieving state page for {state}: {e}")
        return
    if response.status_code != 200:
        logging.error(
            f"Failed to retrieve state page for {state}. Status code: {response.status_code}"
        )
        return

    soup = BeautifulSoup(response.content, "html.parser")
    # code_title = soup.select('div.landingContent h3')[0].get_text(strip=True)
    code_title = soup.select("div.fl-cases-content-list h3")[0].get_text(strip=True)
    # sections = soup.select('div.landingContent a.fl-list-item-link')
    sections = soup.select("div.fl-cases-content-list a.fl-list-item-link")
    section_urls = [a.get("href") for a in sections]

    if not sections:
        logging.warning(f"No sections found for state {state} at {state_url}")
        return

    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, f"{state.upper()}.jsonl")

    # Global executor and session (producer → consumer streaming)
    MAX_WORKERS = 16  # tune 8–12 for this host
    leaf_session = requests.Session()
    leaf_session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Referer": "https://www.google.com/",
        }
    )
    adapter = HTTPAdapter(
        pool_connections=MAX_WORKERS * 2,
        pool_maxsize=MAX_WORKERS,
        max_retries=Retry(
            total=5,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=False,
        ),
    )
    leaf_session.mount("https://", adapter)
    leaf_session.mount("http://", adapter)

    from concurrent.futures import ThreadPoolExecutor

    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    all_futures = []

    with sync_playwright() as p:
        # Disable persistent profile to rule out corrupted profile state
        user_agent = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        context = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = context.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent=user_agent,
            locale="en-US",
            timezone_id="America/Chicago",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )

        page = context.new_page()

        # Add a very short unconditional wait to let extensions/flags settle
        page.wait_for_timeout(300)

        # Add a small stealth script to make navigator properties look more like a real Chrome browser.
        stealth_js = """
        Object.defineProperty(navigator, 'webdriver', {get: () => false});
        window.navigator.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
        Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
        """
        page.add_init_script(stealth_js)

        # Block heavy third-party resources but allow FindLaw-owned images/fonts/media
        """
        def _route_handler(route, request):
            url = request.url.lower()
            # Allow assets from FindLaw so icons, fonts, and site media load correctly.
            if request.resource_type in ("image", "media", "font"):
                if "findlaw.com" in url or "codes.findlaw.com" in url:
                    return route.continue_()
                return route.abort()
            return route.continue_()

        page.route("**/*", _route_handler)
        """

        with open(output_file, "w", encoding="utf-8") as f_out:
            for idx, section in enumerate(sections):
                section_name = section.get_text(strip=True)
                section_url = urljoin(state_url, section.get("href"))
                logging.info(f"Scraping section: {section_name} - {section_url}")
                # if "Title 30" not in section_name:
                #     continue

                if _goto_with_retry(page, section_url, attempts=3):
                    try:
                        scrape_section(
                            page,
                            state,
                            section_name,
                            section_url,
                            [code_title, section_name],
                            [idx + 1],
                            f_out,
                            parallel=True,
                            executor=executor,
                            session=leaf_session,
                            futures=all_futures,
                            return_work=False,
                        )
                    except Exception as e:
                        logging.error(
                            f"Error while scraping section {section_name}: {e}"
                        )
                else:
                    # Skip this section after repeated timeouts
                    continue

            # Join all futures now
            if all_futures:
                from concurrent.futures import as_completed

                for _ in as_completed(all_futures):
                    pass

            executor.shutdown(wait=True)
        context.close()


def _wait_links_or_subaccordions(scope, timeout=6000):
    """
    Wait (briefly) for either links (leaves) or nested accordion-items to appear under `scope`.
    Returns True if something is present/attaches, False on soft-timeout.
    """
    sel = ".fl-recursive-tree-accordion a[href], .fl-recursive-tree-accordion-list .fl-accordion .fl-accordion-item"
    # fast path: anything already there?
    try:
        if scope.locator(sel).count() > 0:
            return True
    except Exception:
        pass
    # slow path: wait briefly for first match to attach
    try:
        scope.locator(sel).first.wait_for(state="attached", timeout=timeout)
        return True
    except Exception:
        return False


def scrape_section(
    page: Page,
    state: str,
    code_name: str,
    section_url: str,
    path_so_far: List[str],
    lex_order: List[int],
    f_out,
    parallel: bool = True,
    executor=None,
    session=None,
    futures=None,
    return_work: bool = False,
):
    """
    Scrape a specific section of law from FindLaw.

    Args:
    - page (Page): The Playwright page object.
    - state (str): The state abbreviation.
    - section_name (str): The name of the section.
    - section_url (str): The URL of the section.
    - f_out: The output file handle.
    """
    # Load & ensure top-level accordion items exist
    page.wait_for_load_state("domcontentloaded", timeout=30000)
    page.wait_for_selector(
        ".fl-expandable-tree-accordion > .fl-accordion-item", timeout=30000
    )

    def _collect_links(scope, base_path, base_lex, section_url):
        """DFS over any number of accordion layers; collect (sec_name, url, path, lex)."""
        results = []

        # If nothing is present yet, wait briefly for either links or nested accordions.
        if (
            scope.locator(
                ".fl-recursive-tree-accordion a[href], .fl-recursive-tree-accordion-list .fl-accordion .fl-accordion-item"
            ).count()
            == 0
        ):
            _wait_links_or_subaccordions(scope, timeout=6000)

        # Case A: links directly under this scope
        link_list = scope.locator(".fl-recursive-tree-accordion a[href]")
        direct_count = 0
        try:
            direct_count = link_list.count()
        except Exception:
            direct_count = 0

        if direct_count > 0:
            for k in range(direct_count):
                a = link_list.nth(k)
                sec_name = a.inner_text().strip()
                href = a.get_attribute("href")
                if not href:
                    continue
                url = urljoin(section_url, href)
                results.append(
                    (sec_name, url, base_path + [sec_name], base_lex + [k + 1])
                )
            return results  # done at this depth

        # Case B: deeper accordions under this scope
        nested_items = scope.locator(
            ".fl-recursive-tree-accordion-list .fl-accordion .fl-accordion-item"
        )
        nested_count = 0
        try:
            nested_count = nested_items.count()
        except Exception:
            nested_count = 0

        for j in range(nested_count):
            n_item = nested_items.nth(j)
            n_btn = n_item.locator("button.fl-accordion-button")
            try:
                n_btn.wait_for(state="attached", timeout=3000)
            except Exception:
                continue

            # label for this node
            try:
                label = n_btn.locator(".fl-text-left").inner_text(timeout=2000).strip()
            except Exception:
                label = f"Section {j+1}"

            # expand if collapsed
            if (n_btn.get_attribute("aria-expanded") or "").lower() != "true":
                n_btn.evaluate("el => el.click()")

            # After expanding, wait briefly for content under this node to show up (links or more accordions)
            _wait_links_or_subaccordions(n_item, timeout=6000)

            # Recurse
            results.extend(
                _collect_links(
                    n_item, base_path + [label], base_lex + [j + 1], section_url
                )
            )
        return results

    items = page.locator(".fl-expandable-tree-accordion > .fl-accordion-item")
    count = items.count()

    # Build work for this section using DFS helper
    work = []
    for i in range(count):
        item = items.nth(i)
        btn = item.locator(":scope > h2 .fl-accordion-button")
        btn.wait_for(state="attached", timeout=10000)
        btn.scroll_into_view_if_needed()

        # Get the visible header text for top-level
        try:
            top_label = btn.locator(".fl-text-left").inner_text(timeout=3000).strip()
        except Exception:
            top_label = f"Section {i+1}"

        # Expand if needed
        if (btn.get_attribute("aria-expanded") or "").lower() != "true":
            btn.click()
        # After expanding, wait briefly for either links or nested items under this top-level
        _wait_links_or_subaccordions(item, timeout=6000)

        # Collect all links at any depth under this top-level item
        work.extend(
            _collect_links(
                item,
                base_path=path_so_far + [top_label],
                base_lex=lex_order + [i + 1],
                section_url=section_url,
            )
        )

    # Guard: optionally just return the work list for upper-level management
    if return_work:
        return work

    # Stream to shared executor (minimal change to your existing parallel path)
    if parallel:
        if executor is None or session is None or futures is None:
            raise RuntimeError(
                "Parallel mode requires shared executor, session, and futures list."
            )

        section_bar = tqdm(
            total=len(work),
            desc=f"{code_name} - leaves",
            unit="leaf",
            dynamic_ncols=True,
        )
        from threading import Lock

        _bar_lock = Lock()

        def _mk_done_cb(bar):
            def _done_cb(_future):
                with _bar_lock:
                    if bar.disable:
                        return
                    bar.update(1)
                    if bar.n >= bar.total:
                        try:
                            bar.close()
                        except Exception:
                            pass

            return _done_cb

        done_cb = _mk_done_cb(section_bar)

        for sec, url, p, lp in work:
            fut = executor.submit(
                fetch_leaf_threadsafe, sec, url, p, lp, state, session, f_out
            )
            fut.add_done_callback(done_cb)
            futures.append(fut)
    else:
        total_leaves = len(work)
        bar = tqdm(
            total=total_leaves,
            desc=f"{code_name} - leaves",
            unit="leaf",
            dynamic_ncols=True,
        )
        for sec, url, p, lp in work:
            scrape_leaf(page, state, sec, url, p, lp, f_out)
            bar.update(1)
        bar.close()


def scrape_leaf(
    parent_page: Page,
    state: str,
    sec_name: str,
    sec_url: str,
    path_so_far: List[str],
    lex_order: List[int],
    f_out,
) -> None:
    """
    Scrape a leaf node (actual statute) from FindLaw.

    Args:
    - parent_page (Page): The Playwright page object from the parent section.
    - state (str): The state abbreviation.
    - sec_name (str): The name of the statute.
    - sec_url (str): The URL of the statute.
    - path_so_far (List[str]): The hierarchical path to this statute.
    - f_out: The output file handle.
    """

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Referer": "https://www.google.com/",
        }
    )
    response = session.get(sec_url, timeout=30)
    if response.status_code != 200:
        logging.error(
            f"Failed to retrieve statute page at {sec_url}. Status code: {response.status_code}"
        )
        return
    soup = BeautifulSoup(response.content, "html.parser")
    statute_name = soup.select("h1")[0].get_text(strip=True)
    content_div = soup.select("div.codes-content p")[0].get_text(strip=True)
    statute_data = {
        "url": sec_url,
        "state": state.upper(),
        "path": "›".join(path_so_far),
        "title": f"{state.upper()} Statutes › {' › '.join(path_so_far)}",
        "univ_cite": False,
        "citation": f"{state.upper()} Stat § {sec_name} (2023)",
        "content": content_div,
        "lex_path": lex_order,
    }
    # we write to a jsonl with the state abbreviation as the filename in the folder output_dir
    import json

    f_out.write(json.dumps(statute_data, ensure_ascii=False) + "\n")


import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

import requests

WRITE_LOCK = Lock()


def fetch_leaf_threadsafe(
    sec_name, sec_url, path_so_far, lex_path, state, session, f_out
):
    # light retry with backoff
    for attempt in range(4):
        try:
            r = session.get(sec_url, timeout=30)
            if r.status_code == 200:
                soup = BeautifulSoup(r.content, "html.parser")
                statute_name = soup.select_one("h1").get_text(strip=True)
                content_div = soup.select_one("div.codes-content p").get_text(
                    strip=True
                )
                data = {
                    "url": sec_url,
                    "state": state.upper(),
                    "path": "›".join(path_so_far),
                    "title": f"{state.upper()} Statutes › {' › '.join(path_so_far)}",
                    "univ_cite": False,
                    "citation": f"{state.upper()} Stat § {sec_name} (2023)",
                    "content": content_div,
                    "lex_path": lex_path,
                }
                with WRITE_LOCK:
                    import json

                    f_out.write(json.dumps(data, ensure_ascii=False) + "\n")
                return
            time.sleep(1.5 * (attempt + 1))
        except requests.RequestException:
            time.sleep(1.5 * (attempt + 1) + random.random())
    # (optional) log failure here


if __name__ == "__main__":
    # test with ND
    scrape_state("pa", DEFAULT_DIR)
