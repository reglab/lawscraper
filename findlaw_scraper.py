"""
File to be used for scraping statutes from FindLaw. This was made to be used primarily for scraping the ND, KY, and PA statutes.

FindLaw requires interaction with Javascript to get to the actual statutes, so this scraper uses Playwright to handle that.


The output format is identical to the other scrapers, with each statute being a JSON object written to a JSONL file. For example:
{"url": "https://law.justia.com/codes/kansas/2023/chapter-1/article-2/section-1-201/", "state": "KS", "path": "Justia\u203aU.S. Law\u203aU.S. Codes and Statutes\u203aKansas Statutes\u203a2023 Kansas Statutes\u203aChapter 1 - Accountants, Certified Public\u203aArticle 2 - State Board Of Accountancy\u203a1-201 Membership; appointment; qualifications; term; vacancies; removal.", "title": "2023 Kansas Statutes \u203a Chapter 1 - Accountants, Certified Public \u203a Article 2 - State Board Of Accountancy \u203a 1-201 Membership; appointment; qualifications; term; vacancies; removal.", "univ_cite": true, "citation": "KS Stat \u00a7 1-201 (2023)", "content": "1-201.\nMembership; appointment; qualifications; term; vacancies; removal.\n(a) There is hereby created a board of accountancy,...", "lex_path": [0, 0, 0]}
"""

import requests
from bs4 import BeautifulSoup
import re
import time
import logging
import os
from urllib.parse import urljoin
from tqdm import tqdm
from typing import List, Dict, Optional
from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeoutError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

FL_BASE_URL = "https://codes.findlaw.com/{state}"
MISSING_STATES = ["nd", "ky", "pa"]

DEFAULT_DIR = "findlaw_codes" # Directory to save the scraped data, e.g. findlaw_codes/ND.jsonl

def scrape_state(state: str, output_dir: str) -> None:
    """
    Scrape statutes for a given state from FindLaw and save them to a JSONL file.

    Args:
    - state (str): The state abbreviation (e.g., 'nd', 'ky', 'pa').
    - output_dir (str): The directory to save the output JSONL file.

    At the first step (the state page), we can use requests and BeautifulSoup. The first sections of the code will have class fl-list-item-link within a div with class landingContent
    After that, we need to use Playwright to handle the Javascript.
    """
    
    state = state.lower()
    if state not in MISSING_STATES:
        raise ValueError(f"State {state} is not in the list of missing states: {MISSING_STATES}")

    state_url = FL_BASE_URL.format(state=state)

    # Use a session and realistic browser headers to reduce 403 responses from FindLaw
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Referer": "https://www.google.com/",
    })

    try:
        response = session.get(state_url, timeout=30)
    except requests.RequestException as e:
        logging.error(f"Network error retrieving state page for {state}: {e}")
        return
    if response.status_code != 200:
        logging.error(f"Failed to retrieve state page for {state}. Status code: {response.status_code}")
        return

    soup = BeautifulSoup(response.content, 'html.parser')
    # code_title = soup.select('div.landingContent h3')[0].get_text(strip=True)
    code_title = soup.select('div.fl-cases-content-list h3')[0].get_text(strip=True)
    # sections = soup.select('div.landingContent a.fl-list-item-link')
    sections = soup.select('div.fl-cases-content-list a.fl-list-item-link')
    section_urls = [a.get('href') for a in sections]
    
    if not sections:
        logging.warning(f"No sections found for state {state} at {state_url}")
        return

    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, f"{state.upper()}.jsonl")

    with sync_playwright() as p:
        # Use a persistent profile so cookies, localStorage and other state persist across pages/runs.
        profile_dir = os.path.join(output_dir, "playwright_profile")
        os.makedirs(profile_dir, exist_ok=True)

        user_agent = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        context = p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
            viewport={"width": 1366, "height": 768},
            user_agent=user_agent,
            locale="en-US",
            timezone_id="America/Chicago",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )

        page = context.new_page()

        # Add a small stealth script to make navigator properties look more like a real Chrome browser.
        stealth_js = """
        Object.defineProperty(navigator, 'webdriver', {get: () => false});
        window.navigator.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
        Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
        """
        page.add_init_script(stealth_js)

        # Block heavy third-party resources but allow FindLaw-owned images/fonts/media
        def _route_handler(route, request):
            url = request.url.lower()
            # Allow assets from FindLaw so icons, fonts, and site media load correctly.
            if request.resource_type in ("image", "media", "font"):
                if "findlaw.com" in url or "codes.findlaw.com" in url:
                    return route.continue_()
                return route.abort()
            return route.continue_()

        page.route("**/*", _route_handler)

        with open(output_file, 'w', encoding='utf-8') as f_out:
            for idx, section in enumerate(sections):
                section_name = section.get_text(strip=True)
                section_url = urljoin(state_url, section.get('href'))
                logging.info(f"Scraping section: {section_name} - {section_url}")

                try:
                    page.goto(section_url, timeout=60000)
                    # Wait for network to quiet down; sometimes FindLaw keeps long-polling connections.
                    page.wait_for_load_state(state="networkidle", timeout=60000)
                    print(f"Loaded section page: {section_url}")
                    scrape_section(page, state, section_name, section_url, [code_title, section_name], [idx+1],f_out)
                except PlaywrightTimeoutError:
                    logging.error(f"Timeout while loading page: {section_url}")
                except Exception as e:
                    logging.error(f"Error while scraping section {section_name}: {e}")

    context.close()


def scrape_section(page: Page, state: str, code_name: str, section_url: str, path_so_far: List[str], lex_order: List[int], f_out, parallel: bool=True) -> None:
    """
    Scrape a specific section of law from FindLaw.

    Args:
    - page (Page): The Playwright page object.
    - state (str): The state abbreviation.
    - section_name (str): The name of the section.
    - section_url (str): The URL of the section.
    - f_out: The output file handle.
    """
    # for btn in page.locator(".fl-expandable-tree-accordion-container button.fl-accordion-button").all():
    #     btn.click()
    for item in page.locator(".fl-accordion-item").all():
        btn = item.locator("button.fl-accordion-button")
        btn.click()

        # Wait until the recursive tree inside THIS item has at least one <a>
        page.wait_for_function(
            """el => {
                const tree = el.querySelector('.fl-recursive-tree-accordion');
                return tree && tree.querySelectorAll('a[href]').length > 0;
            }""",
            arg=item.element_handle(),
            timeout=10000,
    )
    page_content = page.content()
    soup = BeautifulSoup(page_content, 'html.parser')
    headers = soup.select('span.fl-text-left')
    headers = [h.get_text(strip=True) for h in headers]
    subelems = soup.select('div.fl-recursive-tree-accordion')
    subelems = [a.select('li a') for a in subelems]
    subsecs, suburls = [], []
    for e in subelems:
        subsecs.append([])
        suburls.append([])
        for a in e:
            subsecs[-1].append(a.get_text(strip=True))
            suburls[-1].append(urljoin(section_url, a.get('href')))
    if parallel:
        # Build work items for this section
        work = []
        for i, h in enumerate(headers):
            new_path = path_so_far + [h]
            lex_path_new = lex_order + [i+1]
            for idx, (sec, url) in enumerate(zip(subsecs[i], suburls[i])):
                work.append((sec, url, new_path + [sec], lex_path_new + [idx+1]))

        # Threaded fetch of leaves for THIS section
        MAX_WORKERS = 16 # good starting point; tune if needed
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Referer": "https://www.google.com/",
        })
                # Increase urllib3 pool sizes and add retries to match concurrency
        adapter = HTTPAdapter(
            pool_connections=MAX_WORKERS * 2,   # total pools across hosts
            pool_maxsize=MAX_WORKERS,           # per-host concurrent connections
            max_retries=Retry(
                total=5,
                backoff_factor=0.5,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=False  # retry on any method; set to frozenset(['GET']) if you prefer
            ),
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)


        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            bar = tqdm(total=len(work), desc=f"{code_name} - leaves", unit="leaf", dynamic_ncols=True)
            futures = [
                pool.submit(fetch_leaf_threadsafe, sec, url, p, lp, state, session, f_out)
                for (sec, url, p, lp) in work
            ]
            for _ in as_completed(futures):
                bar.update(1)
            bar.close()
    else:
        total_leaves = sum(len(lst) for lst in subsecs)
        bar = tqdm(total=total_leaves, desc=f"{code_name} - leaves", unit="leaf", dynamic_ncols=True)
        for i, h in enumerate(headers):
            new_path = path_so_far + [h]
            lex_order_new = lex_order + [i+1]
            for idx, (sec, url) in enumerate(zip(subsecs[i], suburls[i])):
                scrape_leaf(page, state, sec, url, new_path + [sec], lex_order_new + [idx+1], f_out)
                bar.update(1)
        bar.close()

def scrape_leaf(parent_page: Page, state: str, sec_name: str, sec_url: str, path_so_far: List[str], lex_order: List[int], f_out) -> None:
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
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Referer": "https://www.google.com/",
    })
    response = session.get(sec_url, timeout=30)
    if response.status_code != 200:
        logging.error(f"Failed to retrieve statute page at {sec_url}. Status code: {response.status_code}")
        return
    soup = BeautifulSoup(response.content, 'html.parser')
    statute_name = soup.select('h1')[0].get_text(strip=True)
    content_div = soup.select('div.codes-content p')[0].get_text(strip=True)
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

from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
import requests, time, random

WRITE_LOCK = Lock()

def fetch_leaf_threadsafe(sec_name, sec_url, path_so_far, lex_path, state, session, f_out):
    # light retry with backoff
    for attempt in range(4):
        try:
            r = session.get(sec_url, timeout=30)
            if r.status_code == 200:
                soup = BeautifulSoup(r.content, "html.parser")
                statute_name = soup.select_one("h1").get_text(strip=True)
                content_div = soup.select_one("div.codes-content p").get_text(strip=True)
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
    scrape_state("nd", DEFAULT_DIR)