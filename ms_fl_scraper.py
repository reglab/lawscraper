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
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import get_context
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


def _chunk_list(seq, n):
    """Yield n contiguous chunks from seq (as balanced as possible)."""
    if n <= 1 or len(seq) == 0:
        yield seq
        return
    k, m = divmod(len(seq), n)
    start = 0
    for i in range(n):
        end = start + k + (1 if i < m else 0)
        if start < end:
            yield seq[start:end]
        start = end


def _worker_scrape_sections(
    state: str,
    code_title: str,
    state_url: str,
    sections_slice: list,
    output_part_path: str,
    progress_queue=None,
    record_queue=None,
):
    # Worker runs without rendering tqdm bars; parent process owns console progress output
    """
    Worker process: launches its own Playwright browser and threadpool, scrapes only the given sections.
    `sections_slice` is a list of dicts: {"idx": int, "name": str, "url": str}
    Writes JSONL lines into `output_part_path`.
    """
    import json
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from urllib.parse import urljoin

    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    # leaf HTTP session for this process
    MAX_WORKERS = 8
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

    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    futures = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/Chicago",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        page = context.new_page()
        page.wait_for_timeout(200)
        stealth_js = """
        Object.defineProperty(navigator, 'webdriver', {get: () => false});
        window.navigator.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
        Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
        """
        page.add_init_script(stealth_js)

        for s in sections_slice:
            section_name = s["name"]
            section_url = s["url"]
            idx = s["idx"]
            logging.info(
                f"[pid={os.getpid()}] Scraping section: {section_name} - {section_url}"
            )
            # lightweight retry nav
            ok = False
            for attempt in range(3):
                try:
                    page.goto(section_url, timeout=60000, wait_until="domcontentloaded")
                    page.wait_for_selector("body", timeout=15000)
                    ok = True
                    break
                except PlaywrightTimeoutError:
                    logging.warning(
                        f"[pid={os.getpid()}] Timeout loading {section_url} (attempt {attempt+1}/3)"
                    )
                    try:
                        page.reload(timeout=30000, wait_until="domcontentloaded")
                    except Exception:
                        pass
                    time.sleep(2 * (attempt + 1))
                except Exception as e:
                    logging.warning(f"[pid={os.getpid()}] Navigation error: {e}")
                    time.sleep(2 * (attempt + 1))
            if not ok:
                continue

            try:
                scrape_section(
                    page,
                    state,
                    section_name,
                    section_url,
                    [code_title, section_name],
                    [idx + 1],
                    None,
                    parallel=True,
                    executor=executor,
                    session=leaf_session,
                    futures=futures,
                    return_work=False,
                    tqdm_position=0,
                    tqdm_disable=True,
                    record_queue=record_queue,
                )
                # notify parent that this section finished scheduling
                try:
                    if progress_queue is not None:
                        progress_queue.put(1)
                except Exception:
                    pass
                # workers do not report progress directly; parent updates on future completion
            except Exception as e:
                logging.error(
                    f"[pid={os.getpid()}] Error while scraping section {section_name}: {e}"
                )

        # drain futures
        if futures:
            for _ in as_completed(futures):
                pass
        executor.shutdown(wait=True)
        context.close()
        browser.close()


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
    MAX_WORKERS = 12  # tune 8–12 for this host
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
    MAX_BROWSERS = int(os.environ.get("FINDLAW_MAX_BROWSERS", "1"))

    if MAX_BROWSERS <= 1:
        # === single-browser path (existing behavior) ===
        with sync_playwright() as p:
            user_agent = (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            browser = p.chromium.launch(
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            context = browser.new_context(
                viewport={"width": 1366, "height": 768},
                user_agent=user_agent,
                locale="en-US",
                timezone_id="America/Chicago",
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            page = context.new_page()
            page.wait_for_timeout(300)
            stealth_js = """
            Object.defineProperty(navigator, 'webdriver', {get: () => false});
            window.navigator.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            """
            page.add_init_script(stealth_js)

            from tqdm import tqdm

            sections_bar = tqdm(
                total=len(sections),
                desc="Sections",
                unit="section",
                dynamic_ncols=True,
                position=0,
                leave=True,
            )
            with open(output_file, "w", encoding="utf-8") as f_out:
                for idx, section in enumerate(sections):
                    section_name = section.get_text(strip=True)
                    section_url = urljoin(state_url, section.get("href"))
                    logging.info(f"Scraping section: {section_name} - {section_url}")
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
                                tqdm_position=1,
                            )
                            sections_bar.update(1)
                        except Exception as e:
                            logging.error(
                                f"Error while scraping section {section_name}: {e}"
                            )
                    else:
                        continue
                sections_bar.close()

                if all_futures:
                    from concurrent.futures import as_completed

                    for _ in as_completed(all_futures):
                        pass
                executor.shutdown(wait=True)
            context.close()
            browser.close()
    else:
        # === multi-browser path (N processes, each with its own browser) ===
        # Precompute the sections list we’ll distribute to workers
        sections_info = []
        for idx, section in enumerate(sections):
            name = section.get_text(strip=True)
            href = section.get("href")
            if not href:
                continue
            sections_info.append(
                {"idx": idx, "name": name, "url": urljoin(state_url, href)}
            )

        from tqdm import tqdm

        sections_bar = tqdm(
            total=len(sections_info),
            desc="Sections",
            unit="section",
            dynamic_ncols=True,
            position=0,
            leave=True,
        )
        # Create queues and writer thread for single-file output
        from queue import Empty
        from threading import Thread

        # Cap processes to number of sections
        proc_count = min(MAX_BROWSERS, max(1, len(sections_info)))
        # Improve perceived progress by splitting into more chunks than processes
        chunk_factor = max(1, int(os.environ.get("FINDLAW_CHUNKS_PER_PROC", "4")))
        total_chunks = min(len(sections_info), proc_count * chunk_factor)
        ctx = get_context("spawn")
        manager = ctx.Manager()
        progress_q = manager.Queue(maxsize=1000)
        record_q = manager.Queue(maxsize=10000)

        # Writer thread
        def _writer():
            with open(output_file, "w", encoding="utf-8") as fout:
                while True:
                    item = record_q.get()
                    if item is None:
                        break
                    try:
                        fout.write(item)
                    except Exception:
                        pass

        writer_t = Thread(target=_writer, daemon=True)
        writer_t.start()

        with ProcessPoolExecutor(max_workers=proc_count, mp_context=ctx) as pool:
            futures = []
            for pi, chunk in enumerate(_chunk_list(sections_info, total_chunks)):
                # part_path kept for arg shape but unused in worker when record_queue is provided
                fut = pool.submit(
                    _worker_scrape_sections,
                    state,
                    code_title,
                    state_url,
                    chunk,
                    "",
                    progress_q,
                    record_q,
                )
                futures.append(fut)
            # Drain per-section progress from queue while workers run
            total_expected = len(sections_info)
            processed = 0
            done_workers = 0
            total_workers = len(futures)
            while processed < total_expected and done_workers < total_workers:
                try:
                    inc = progress_q.get(timeout=0.5)
                    if isinstance(inc, int) and inc > 0:
                        sections_bar.update(inc)
                        processed += inc
                except Empty:
                    pass
                # update done_workers snapshot
                done_workers = sum(1 for f in futures if f.done())
            # Drain any remaining queued increments without blocking
            try:
                while True:
                    inc = progress_q.get_nowait()
                    if isinstance(inc, int) and inc > 0:
                        sections_bar.update(inc)
                        processed += inc
            except Empty:
                pass
            # Ensure all workers have completed
            for fut in as_completed(futures):
                fut.result()
        # stop writer
        record_q.put(None)
        writer_t.join()
        try:
            manager.shutdown()
        except Exception:
            pass
        sections_bar.close()


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
    tqdm_position: int = 0,
    tqdm_disable: bool = False,
    record_queue=None,
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
            position=tqdm_position if tqdm_position is not None else 0,
            leave=False,
            disable=tqdm_disable,
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
                fetch_leaf_threadsafe,
                sec,
                url,
                p,
                lp,
                state,
                session,
                f_out,
                record_queue,
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
            position=tqdm_position if tqdm_position is not None else 0,
            leave=False,
            disable=tqdm_disable,
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
    sec_name, sec_url, path_so_far, lex_path, state, session, f_out, record_queue=None
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
                import json

                line = json.dumps(data, ensure_ascii=False)
                if record_queue is not None:
                    # Send to parent writer thread
                    record_queue.put(line + "\n")
                else:
                    with WRITE_LOCK:
                        f_out.write(line + "\n")
                return
            time.sleep(1.5 * (attempt + 1))
        except requests.RequestException:
            time.sleep(1.5 * (attempt + 1) + random.random())
    # (optional) log failure here


if __name__ == "__main__":
    # test with ND
    scrape_state("nd", DEFAULT_DIR)
