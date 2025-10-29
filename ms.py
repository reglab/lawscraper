import argparse
import json
import os
import queue
import threading
from io import TextIOWrapper

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

from scraper_utils import (CODES_BASE_URL, FAILED_FAILPATH, HEADERS,
                           JUR_URL_MAP, JUSTIA_BASE_URL, REGULATIONS_BASE_URL)

# Queue to send progress updates from worker threads to the main thread
progress_queue = queue.Queue()


def _href_to_path(href: str, jur: str) -> list[str]:
    """
    Converts a URL to a list of path components.

    Sample Input:
    - href: "/codes/alabama/title-1/chapter-1"
    - jur: "alabama"

    Sample Output:
    - ["title-1", "chapter-1"]

    Args:
    - href (str): The URL to convert.
    - jur (str): The jurisdiction to remove from the URL.

    Returns:
    - list[str]: The list of path components.
    """
    return href.split(f"{JUR_URL_MAP[jur]}/")[1][:-1].split("/")


def get_last_path(state_abb: str, regs: bool = False) -> list[str]:
    """
    Get the set of URL _prefixes_ that have already been scraped for the given
    state.

    For example, if you have already scraped Title I of the Alabama code, and
    Title II, Chapter 1, it should return something like {"alabama/title-i",
    "alabama/title-ii/chapter-1"}. What is returned should correspond to the URL
    prefixes of the pages that have already been scraped.  Additionally, nothing
    of the returned outputs should be a prefix of the other returned outputs,
    e.g. we should not have both "title-i/chapter-1" and "title-i" but instead
    only "title-i" (only the most encompassing).

    We assume that the URL tree is scraped in the same order every time. We also
    assume that if one has reached "alabama/title-ii", then "alabama/title-i"
    has already been scraped.

    Args: - state_abb (str): The state abbreviation to check.

    Returns: - set: The set of URLs that have already been scraped for the given
    state.

    NOTE: the "URL" returned should been with the information with the state,
    e.g. "https://law.justia.com/codes/mississippi/title-21/chapter-7/" should
    be returned as "mississippi/title-21/chapter-7", and
    "https://regulations.justia.com/states/arizona/title-1/chapter-1/" should be
    returned as "arizona/title-1/chapter-1".
    """
    save_dir = "regs" if regs else "codes"
    # split_str = JUR_URL_MAP[state_abb] + "/"
    save_path = f"{save_dir}/{state_abb}.jsonl"
    with open(save_path, "rb") as f:
        try:  # catch OSError in case of a one line file
            f.seek(-2, os.SEEK_END)
            while f.read(1) != b"\n":
                f.seek(-2, os.SEEK_CUR)
        except OSError:
            f.seek(0)
        last_line = f.readline().decode()
    last_url = json.loads(last_line)["url"]
    last_path_ = _href_to_path(last_url, state_abb)
    return last_path_


def num_lines(state_abb: str, regs: bool = False) -> int:
    """
    Get the number of lines in the file for the given state.

    Args:
    - state_abb (str): The state abbreviation to check.

    Returns:
    - int: The number of lines in the file.
    """
    save_dir = "regs" if regs else "codes"
    save_path = f"{save_dir}/{state_abb}.jsonl"
    with open(save_path, "r") as f:
        line_count = sum(1 for _ in f)
    return line_count


def extract_links_from_content(content: BeautifulSoup) -> list[dict[str, str]]:
    links = []
    for a_tag in content.find_all("a", href=True):
        link_text = a_tag.get_text(strip=True)
        link_href = a_tag["href"]
        links.append({"text": link_text, "href": link_href})
    return links


def extract_links_after(
    content: BeautifulSoup, state_name: str, continue_from: list
) -> list[dict[str, str]]:
    """
    Extracts all links from the content of a page.

    If `continue_from` is not None, the function should only return links that occur after the last link in `continue_from` is encountered in the DFS.

    Sample Input:
    - content: (contains links: 'alabama/title-1/chapter-1', 'alabama/title-1/chapter-2', 'alabama/title-1/chapter-3', 'alabama/title-1/chapter-4')
    - continue_from: ["title-1", "chapter-3", "section-2", "subsection-1"]

    Sample Output:
    - [
        {"text": "Chapter 3", "href": "alabama/title-1/chapter-3"},
        {"text": "Chapter 4", "href": "alabama/title-1/chapter-4"}
      ]

    Args:
    - content (BeautifulSoup): The content of the page.
    - continue_from (list[str] | None): The last link encountered in the previous attempt at scraping, i.e. the link in the DFS after which we should start scraping.
    """
    download = False
    links = []
    for a_tag in content.find_all("a", href=True):
        link_text = a_tag.get_text(strip=True)
        link_href = a_tag["href"]
        if download:
            links.append({"text": link_text, "href": link_href})
            continue
        if continue_from is not None:
            link_path = _href_to_path(link_href, state_name)
            check_against = continue_from[: len(link_path)]
            if link_path == check_against:
                links.append({"text": link_text, "href": link_href})
                download = True
    return links


def process_code_leaf(state_name, url, jsonl_fp=None, is_reg=False):
    try:
        response = requests.get(url, headers=HEADERS)
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, "html.parser")
            sep = soup.find("span", class_="breadcrumb-sep").get_text(strip=True)
            assert ord(sep) == 8250, "Separator is not the right character."
            path_str = soup.find("nav", class_="breadcrumbs").get_text(strip=True)
            title_str = soup.find("h1").get_text(f" {sep} ", strip=True)
            content = soup.find(id="codes-content").get_text("\n", strip=True)

            record = {
                "url": url,
                "state": state_name,
                "path": path_str,
                "title": title_str,
                "content": content,
            }

            if jsonl_fp:
                jsonl_fp.write(json.dumps(record))
                jsonl_fp.write("\n")

            # Send progress update to the main thread (completed)
            progress_queue.put((state_name, "completed"))
            progress_queue.put((state_name, f"last:{url}"))
        else:
            # Send progress update to the main thread (failed)
            progress_queue.put((state_name, "failed"))
            progress_queue.put((state_name, f"last:{url}"))
            with open(FAILED_FAILPATH, "a") as f:
                f.write(f"{url}\n")
    except Exception as e:
        # Send progress update to the main thread (failed)
        progress_queue.put((state_name, "failed"))
        progress_queue.put((state_name, f"last:{url}"))
        with open(FAILED_FAILPATH, "a") as f:
            f.write(f"{url}\n")


def collect_leaf_urls(
    state_name: str,
    init_url: str,
    jsonl_fp=TextIOWrapper | None,
    internal_class: str = "codes-listing",
    site_url: str = JUSTIA_BASE_URL,
    regs: bool = False,
    continue_from: list[str] | None = None,
) -> list:
    collected_urls = []

    def helper(url):
        nonlocal continue_from
        try:
            response = requests.get(url, headers=HEADERS)
            if response.status_code == 200:
                soup = BeautifulSoup(response.content, "html.parser")
                internal_links = soup.find(class_=internal_class)
                if internal_links:
                    internal_links = (
                        extract_links_from_content(internal_links)
                        if continue_from is None
                        else extract_links_after(
                            internal_links, state_name, continue_from
                        )
                    )
                    for link in internal_links:
                        href = link["href"]
                        helper(f"{site_url}{href}")
                else:
                    if continue_from is not None:
                        # we can turn the continue_from flag off if our path is the same as last_path
                        if continue_from == _href_to_path(url, state_name):
                            continue_from = None
                            progress_queue.put((state_name, "completed_batch"))
                    else:
                        collected_urls.append(url)
                        process_code_leaf(state_name, url, jsonl_fp, regs)
            else:
                # Notify failure via the progress queue
                progress_queue.put((state_name, "failed"))
                progress_queue.put((state_name, f"last:{url}"))
                with open(FAILED_FAILPATH, "a") as f:
                    f.write(f"{url}\n")
        except Exception as e:
            # Notify failure via the progress queue
            progress_queue.put((state_name, "failed"))
            progress_queue.put((state_name, f"last:{url}"))
            with open(FAILED_FAILPATH, "a") as f:
                f.write(f"{url}\n")

    helper(init_url)
    return collected_urls


def collect_codes_for_state(
    state_name, year: int = 2023, regs=False, overwrite: bool = False
):
    state_init_url = (
        f"{REGULATIONS_BASE_URL}/states/{JUR_URL_MAP[state_name]}/"
        if regs
        else f"{CODES_BASE_URL}{JUR_URL_MAP[state_name]}/{year}/"
    )
    save_dir = "regs" if regs else "codes"
    site_base_url = REGULATIONS_BASE_URL if regs else JUSTIA_BASE_URL

    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    state_path = f"{save_dir}/{state_name}.jsonl"
    continue_from = None
    mode = "w"
    if (
        os.path.exists(state_path)
        and os.stat(state_path).st_size != 0
        and not overwrite
    ):
        continue_from = get_last_path(state_name, regs)
        mode = "a"
    with open(state_path, mode) as f:
        collect_leaf_urls(
            state_name,
            state_init_url,
            f,
            regs=regs,
            site_url=site_base_url,
            continue_from=continue_from,
        )
        progress_queue.put((state_name, "finished"))


def worker_thread(state_name, year, regs):
    """
    Worker thread function that processes one state.
    """
    collect_codes_for_state(state_name, year, regs)


def process_states_in_parallel(
    states, year=2023, regs=False, overwrite=False, max_threads=4
):
    n = len(states)
    threads: dict[str, threading.Thread] = {}
    remaining_states = None
    if max_threads < len(states):
        states, remaining_states = states[:max_threads], states[max_threads:]

    # Start the worker threads for each state
    for state in states:
        thread = threading.Thread(target=worker_thread, args=(state, year, regs))
        threads[state] = thread
        thread.start()

    positions = {state: i + 1 for i, state in enumerate(states)}
    # Initialize tqdm progress bars for each state
    progress_bars: dict[str, tqdm] = {
        state: tqdm(
            desc=f"{state}", total=None, position=positions[state], dynamic_ncols=True
        )
        for state in states
    }
    progress_bars["finished_states"] = tqdm(
        desc=f"Finished States (0/{n}):",
        total=0,
        position=0,
        dynamic_ncols=True,
        smoothing=0,
    )
    state_progress = {
        state: {"completed": 0, "failed": 0, "last": ""} for state in states
    }
    finished_states, available_pos = [], set(
        range(
            len(states) + 1,
            len(states) + (len(remaining_states) if remaining_states else 0) + 1,
        )
    )

    # Main loop for updating progress bars
    while (
        any(thread.is_alive() for thread in threads.values())
        or not progress_queue.empty()
    ):
        try:
            # Get progress updates from the queue
            state_name, status = progress_queue.get(timeout=1)

            # Update progress counts based on the message received
            if status == "completed":
                state_progress[state_name]["completed"] += 1
            elif status == "completed_batch":
                state_progress[state_name]["completed"] += num_lines(state_name, regs)
            elif status == "failed":
                state_progress[state_name]["failed"] += 1
            elif status.startswith("last:"):
                # 5 is for the "last:" at the beginning of the string
                # 29 or 38 is for the url up to the state name
                state_progress[state_name]["last"] = status[43 if regs else 34 :]
            elif status == "finished":
                finished_states.append(state_name)
                progress_bars["finished_states"].set_description(
                    f"Finished States ({len(finished_states)}/{n}): {', '.join(finished_states if len(finished_states) < 20 else ['...'] + finished_states[-19:])}"
                )
                state_progress[state_name][
                    "last"
                ] = f"{state_progress[state_name]['last'][:-13]}...[FINISHED]"
                thread: threading.Thread = threads.pop(state_name)
                thread.join()
                if remaining_states:
                    next_state = remaining_states.pop(0)
                    state_progress[next_state] = {
                        "completed": 0,
                        "failed": 0,
                        "last": "",
                    }
                    threads[next_state] = threading.Thread(
                        target=worker_thread,
                        args=(next_state, year, regs),
                    )
                    available_pos.add(positions.pop(state_name))
                    positions[next_state] = min(available_pos)
                    available_pos.remove(positions[next_state])
                    progress_bars[next_state] = tqdm(
                        desc=f"{next_state}",
                        total=None,
                        position=positions[next_state],
                        dynamic_ncols=True,
                    )
                    threads[next_state].start()
                    state_name = next_state

            # Update the progress bar description with counts
            progress_bars[state_name].set_description(
                f"{state_name}: Completed: {state_progress[state_name]['completed']}; Failed: {state_progress[state_name]['failed']}; Last: {state_progress[state_name]['last'][-60:]}"
            )
            if status == "completed" or status == "failed":
                progress_bars[state_name].update(1)
                progress_bars["finished_states"].update(1)

        except queue.Empty:
            # Continue checking for updates
            continue

    # Close the progress bars when done
    for state in states:
        progress_bars[state].close()

    # Wait for all threads to finish
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="ms.py", description="Scrape the Justia website for state codes."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "-s",
        "--states",
        nargs="+",
        type=str,
        help="The state codes to scrape.",
        choices=JUR_URL_MAP.keys(),
    )
    group.add_argument(
        "--range",
        nargs=2,
        type=str,
        help="The range of states to scrape.",
    )
    group.add_argument(
        "--all",
        default=False,
        help="Scrape all states.",
        action=argparse.BooleanOptionalAction,
    )
    parser.add_argument(
        "--year", type=int, help="The year to scrape the codes for.", default=2023
    )
    parser.add_argument(
        "-o",
        "--overwrite",
        default=False,
        help="Overwrite the files specified.",
    )
    parser.add_argument(
        "-r",
        "--regs",
        default=False,
        help="Scrape the regulations instead of the codes.",
        action=argparse.BooleanOptionalAction,
    )
    parser.add_argument(
        "-t",
        "--threads",
        type=int,
        default=4,
        help="The number of threads to use for scraping.",
    )
    args_ = parser.parse_args()
    if args_.range:
        all_jurs = list(JUR_URL_MAP.keys())
        s0, s1 = args_.range
        assert all_jurs.index(s0) <= all_jurs.index(s1)
        args_.states = all_jurs[all_jurs.index(s0) : all_jurs.index(s1) + 1]
    elif args_.all:
        args_.states = list(JUR_URL_MAP.keys())
        if not args_.regs:  # remove KY, ND, and VI from
            args_.states.remove("KY")
            args_.states.remove("ND")
            args_.states.remove("VI")
        else:
            args_.states.remove("DC")
            args_.states.remove("PR")
            args_.states.remove("VI")

    # Process states in parallel with progress displayed in the main thread
    process_states_in_parallel(
        args_.states,
        year=args_.year,
        regs=args_.regs,
        overwrite=args_.overwrite,
        max_threads=args_.threads,
    )
