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


def extract_links_from_content(content):
    links = []
    for a_tag in content.find_all("a", href=True):
        link_text = a_tag.get_text(strip=True)
        link_href = a_tag["href"]
        links.append({"text": link_text, "href": link_href})
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
    state_name,
    init_url,
    jsonl_fp=None,
    internal_class="codes-listing",
    site_url=JUSTIA_BASE_URL,
    regs=False,
):
    collected_urls = []

    def helper(url):
        try:
            response = requests.get(url, headers=HEADERS)
            if response.status_code == 200:
                soup = BeautifulSoup(response.content, "html.parser")
                internal_links = soup.find(class_=internal_class)
                if internal_links:
                    internal_links = extract_links_from_content(internal_links)
                    for link in internal_links:
                        href = link["href"]
                        helper(f"{site_url}{href}")
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


def collect_codes_for_state(state_name, year=2023, regs=False):
    state_init_url = (
        f"{REGULATIONS_BASE_URL}/states/{JUR_URL_MAP[state_name]}/"
        if regs
        else f"{CODES_BASE_URL}{JUR_URL_MAP[state_name]}/{year}/"
    )
    save_dir = "regs" if regs else "codes"
    site_base_url = REGULATIONS_BASE_URL if regs else JUSTIA_BASE_URL

    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    with open(f"{save_dir}/{state_name}.jsonl", "w") as f:
        collect_leaf_urls(
            state_name, state_init_url, f, regs=regs, site_url=site_base_url
        )


def worker_thread(state_name, year, regs):
    """
    Worker thread function that processes one state.
    """
    collect_codes_for_state(state_name, year, regs)


def process_states_in_parallel(states, year=2023, regs=False):
    threads = []

    # Start the worker threads for each state
    for state in states:
        thread = threading.Thread(target=worker_thread, args=(state, year, regs))
        threads.append(thread)
        thread.start()

    # Initialize tqdm progress bars for each state
    progress_bars = {
        state: tqdm(desc=f"{state}", total=0, position=i, dynamic_ncols=True)
        for i, state in enumerate(states)
    }
    state_progress = {
        state: {"completed": 0, "failed": 0, "last": ""} for state in states
    }

    # Main loop for updating progress bars
    while any(thread.is_alive() for thread in threads) or not progress_queue.empty():
        try:
            # Get progress updates from the queue
            state_name, status = progress_queue.get(timeout=1)

            # Update progress counts based on the message received
            if status == "completed":
                state_progress[state_name]["completed"] += 1
            elif status == "failed":
                state_progress[state_name]["failed"] += 1
            elif status.startswith("last:"):
                state_progress[state_name]["last"] = status[5 + 29 :]

            # Update the progress bar description with counts
            progress_bars[state_name].set_description(
                f"{state_name}: Completed: {state_progress[state_name]['completed']}; Failed: {state_progress[state_name]['failed']}; Last: {state_progress[state_name]['last']}"
            )
            progress_bars[state_name].update(1)

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
        prog="scraper.py", description="Scrape the Justia website for state codes."
    )
    parser.add_argument(
        "states",
        nargs="+",
        type=str,
        help="The state codes to scrape.",
        choices=JUR_URL_MAP.keys(),
    )
    parser.add_argument(
        "--year", type=int, help="The year to scrape the codes for.", default=2023
    )
    parser.add_argument(
        "-r",
        "-regs",
        help="Scrape the regulations instead of the codes.",
        action=argparse.BooleanOptionalAction,
    )
    args_ = parser.parse_args()
    args_.states = ["SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY"]

    # Process states in parallel with progress displayed in the main thread
    process_states_in_parallel(args_.states, year=args_.year, regs=args_.r)
