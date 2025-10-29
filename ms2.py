import argparse
import json
import os
import queue
import threading
from io import TextIOWrapper
from typing import Optional

import requests
from bs4 import BeautifulSoup, PageElement
from tqdm import tqdm

from scraper_utils import (CODES_BASE_URL, FAILED_FAILPATH, HEADERS,
                           JUR_URL_MAP, JUSTIA_BASE_URL, REGULATIONS_BASE_URL)


def extract_links_from_content(content: PageElement) -> list:
    """
    Extract all links from the given BeautifulSoup PageElement.

    Args:
    - content (PageElement): The HTML content (usually the result of soup.find()).

    Returns:
    - List[Dict]: A list of dictionaries with link text and href.
    """
    links = []

    # Find all <a> tags in the content
    for a_tag in content.find_all("a", href=True):
        link_text = a_tag.get_text(strip=True)
        link_href = a_tag["href"]

        # Store the link text and URL in a dictionary
        links.append({"text": link_text, "href": link_href})

    return links


def process_code_leaf(
    state_name: str,
    url: str,
    jsonl_fp: Optional[TextIOWrapper],
    is_reg: bool = False,
    lex_path: Optional[list[int]] = None,
    lock: Optional[threading.Lock] = None,
    pbar: Optional[tqdm] = None,
) -> dict:
    """
    Process the content of a leaf node in the Justia website.

    Args:
    - url (str): The URL of the leaf node.
    - jsonl_fp (TextIOWrapper): The file pointer to write the JSONL records to.
    - is_reg (bool): Whether the URL is for a regulation or not.
    - lex_path (list[int]): The lexicographical path to the leaf node.
    - lock (threading.Lock): A lock to make file writes thread-safe.
    - pbar (tqdm): A tqdm progress bar to update.

    Returns:
    - dict: A dictionary containing the title and content of the leaf node.
    """
    response = requests.get(url, headers=HEADERS)
    if response.status_code == 200:
        soup: BeautifulSoup = BeautifulSoup(response.content, "html.parser")
        # title = soup.find('h1').get_text(strip=True)
        sep = soup.find("span", class_="breadcrumb-sep").get_text(strip=True)
        assert ord(sep) == 8250, "Separator is not the right character."
        path_str = soup.find("nav", class_="breadcrumbs").get_text(strip=True)
        path_arr = path_str.split(sep)
        title_arr = list(soup.find("h1").stripped_strings)
        title_str = soup.find("h1").get_text(f" {sep} ", strip=True)
        has_univ_cite = False
        citation = None
        if is_reg:
            if wrapper := soup.find("div", class_="has-margin-bottom-20"):
                has_univ_cite = (
                    wrapper.find("b").get_text(strip=True) == "Universal Citation:"
                )
            if cite_tag := soup.find(href="/citations.html"):
                citation = cite_tag.get_text(strip=True)
        else:
            if wrapper := soup.find("div", class_="citation-wrapper"):
                has_univ_cite = (
                    wrapper.find("strong").get_text(strip=True) == "Universal Citation:"
                )
            if cite_tag := soup.find("div", class_="citation"):
                citation = cite_tag.find("span").get_text(strip=True)
        content = soup.find(id="codes-content").get_text("\n", strip=True)
        record = {
            "url": url,
            "state": state_name,
            "path": path_str,
            "title": title_str,
            "univ_cite": has_univ_cite,
            "citation": citation,
            "content": content,
            "lex_path": lex_path,
        }

        if jsonl_fp:
            with lock:
                jsonl_fp.write(json.dumps(record))
                jsonl_fp.write("\n")
        if pbar is not None:
            pbar.update(1)
    else:
        print(
            f"Failed to retrieve content for {url}, Status Code: {response.status_code}"
        )
        with open(FAILED_FAILPATH, "a") as f:
            f.write(f"{url}\n")


def get_last_lex_path(state_abb: str, regs: bool = False) -> Optional[list[int]]:
    """
    Get the lexicographical path of the last successfully scraped entry.

    Args:
    - state_abb (str): The state abbreviation to check.
    - regs (bool): Whether to check for regulations or codes.

    Returns:
    - list[int] | None: The last lex_path, or None if the file doesn't exist/is empty.
    """
    save_dir = "regs" if regs else "codes"
    save_path = f"{save_dir}/{state_abb}.jsonl"
    if not os.path.exists(save_path) or os.stat(save_path).st_size == 0:
        return None

    with open(save_path, "rb") as f:
        try:  # catch OSError in case of a one line file
            f.seek(-2, os.SEEK_END)
            while f.read(1) != b"\n":
                f.seek(-2, os.SEEK_CUR)
        except OSError:
            f.seek(0)
        last_line = f.readline().decode()

    return json.loads(last_line).get("lex_path")


def scrape_branch(
    url: str,
    path: list[int],
    continue_from: Optional[list[int]],
    state_name: str,
    jsonl_fp: TextIOWrapper,
    regs: bool,
    site_url: str,
    internal_class: str,
    lock: threading.Lock,
    pbar: Optional[tqdm] = None,
):
    """
    Recursively scrapes a branch of the website.
    """
    response = requests.get(url, headers=HEADERS)
    if response.status_code == 200:
        soup: BeautifulSoup = BeautifulSoup(response.content, "html.parser")
        internal_links_element = soup.find(
            class_=internal_class
        )  # these will be URLs relative to the base_url
        if internal_links_element:  # This is a branch node
            links = extract_links_from_content(internal_links_element)

            start_idx = 0
            # If resuming and current path is a prefix of the target resume path
            if continue_from and path == continue_from[: len(path)]:
                # Set the starting index for links at this level
                if len(path) < len(continue_from):
                    start_idx = continue_from[len(path)]

            for i, link in enumerate(links):
                if i < start_idx:
                    continue

                href = link["href"]
                new_path = path + [i]

                # If we move past the resume index, disable resume logic for subsequent branches
                new_continue_from = continue_from
                if continue_from and i > start_idx:
                    new_continue_from = None

                scrape_branch(
                    f"{site_url}{href}",
                    new_path,
                    new_continue_from,
                    state_name,
                    jsonl_fp,
                    regs,
                    site_url,
                    internal_class,
                    lock,
                    pbar,
                )
        else:  # This is a leaf node
            # Skip the exact leaf node we are resuming from
            if continue_from and path == continue_from:
                return

            process_code_leaf(
                state_name, url, jsonl_fp, regs, lex_path=path, lock=lock, pbar=pbar
            )
    else:
        print(
            f"Failed to retrieve content for {url}, Status Code: {response.status_code}"
        )
        with open(FAILED_FAILPATH, "a") as f:
            f.write(f"{url}\n")


def worker(
    work_queue: queue.Queue,
    state_name: str,
    jsonl_fp: TextIOWrapper,
    regs: bool,
    site_url: str,
    internal_class: str,
    lock: threading.Lock,
    pbar: Optional[tqdm] = None,
):
    """
    Worker thread function to process tasks from the queue.
    """
    while not work_queue.empty():
        try:
            href, path, continue_from = work_queue.get_nowait()
        except queue.Empty:
            break

        scrape_branch(
            url=f"{site_url}{href}",
            path=path,
            continue_from=continue_from,
            state_name=state_name,
            jsonl_fp=jsonl_fp,
            regs=regs,
            site_url=site_url,
            internal_class=internal_class,
            lock=lock,
            pbar=pbar,
        )
        work_queue.task_done()


def collect_codes_for_state(
    state_name: str,
    year: int = 2023,
    regs: bool = False,
    resume: bool = False,
    num_threads: int = 4,
) -> None:
    """
    Collect all codes for the given state in parallel.
    """
    state_init_url = (
        f"{REGULATIONS_BASE_URL}/states/{JUR_URL_MAP[state_name]}/"
        if regs
        else f"{CODES_BASE_URL}{JUR_URL_MAP[state_name]}/{year}/"
    )
    save_dir = "regs" if regs else "codes"
    site_base_url = REGULATIONS_BASE_URL if regs else JUSTIA_BASE_URL
    internal_class = "codes-listing"

    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    continue_from = None
    mode = "w"
    if resume:
        continue_from = get_last_lex_path(state_name, regs)
        if continue_from is not None:
            mode = "a"

    with open(f"{save_dir}/{state_name}.jsonl", mode) as f:
        response = requests.get(state_init_url, headers=HEADERS)
        if response.status_code != 200:
            print(f"Failed to get initial page for {state_name}")
            return

        soup = BeautifulSoup(response.content, "html.parser")
        internal_links_element = soup.find(class_=internal_class)
        if not internal_links_element:
            print(f"No top-level branches found for {state_name}")
            return

        links = extract_links_from_content(internal_links_element)
        print(f"Found {len(links)} top-level titles to scrape for {state_name}.")

        work_queue = queue.Queue()
        file_lock = threading.Lock()

        start_branch_idx = 0
        if continue_from:
            start_branch_idx = continue_from[0]

        for i, link in enumerate(links):
            if i < start_branch_idx:
                continue

            branch_continue_from = None
            if i == start_branch_idx and continue_from:
                branch_continue_from = continue_from

            work_queue.put((link["href"], [i], branch_continue_from))

        threads = []
        pbar = tqdm(desc=f"Scraping {state_name}", unit="pages")
        for _ in range(num_threads):
            thread = threading.Thread(
                target=worker,
                args=(
                    work_queue,
                    state_name,
                    f,
                    regs,
                    site_base_url,
                    internal_class,
                    file_lock,
                    pbar,
                ),
            )
            thread.start()
            threads.append(thread)

        for thread in threads:
            thread.join()

        pbar.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="scraper.py", description="Scrape the Justia website for state codes."
    )
    parser.add_argument(
        "state", type=str, help="The state code to scrape.", choices=JUR_URL_MAP.keys()
    )
    parser.add_argument(
        "--year", type=int, help="The year to scrape the codes for.", default=2023
    )
    parser.add_argument(
        "-c",
        "--resume",
        action="store_true",
        help="Resume an interrupted scrape instead of starting over.",
    )
    parser.add_argument(
        "-r",
        "-regs",
        help="Scrape the regulations instead of the codes.",
        action=argparse.BooleanOptionalAction,
    )
    parser.add_argument(
        "-t",
        "--threads",
        type=int,
        default=4,
        help="The number of threads to use.",
    )
    args_ = parser.parse_args()
    collect_codes_for_state(
        args_.state,
        year=args_.year,
        regs=args_.r,
        resume=args_.resume,
        num_threads=args_.threads,
    )
