import argparse
import json
import os
from io import TextIOWrapper

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
    jsonl_fp: TextIOWrapper | None,
    is_reg: bool = False,
    lex_path: list[int] | None = None,
) -> dict:
    """
    Process the content of a leaf node in the Justia website.

    Args:
    - url (str): The URL of the leaf node.
    - jsonl_fp (TextIOWrapper): The file pointer to write the JSONL records to.
    - is_reg (bool): Whether the URL is for a regulation or not.
    - lex_path (list[int]): The lexicographical path to the leaf node.

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
            jsonl_fp.write(json.dumps(record))
            jsonl_fp.write("\n")
    else:
        print(
            f"Failed to retrieve content for {url}, Status Code: {response.status_code}"
        )
        with open(FAILED_FAILPATH, "a") as f:
            f.write(f"{url}\n")


def get_last_lex_path(state_abb: str, regs: bool = False) -> list[int] | None:
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


def collect_leaf_urls(
    state_name: str,
    init_url: str,
    jsonl_fp: TextIOWrapper | None,
    internal_class: str = "codes-listing",
    site_url: str = JUSTIA_BASE_URL,
    regs: bool = False,
    write_jsonl=True,
    continue_from: list[int] | None = None,
) -> list:
    """
    Collect all leaf URLs from the given site URL.

    Args:
    - state_name (str): The state code to scrape.
    - init_url (str): The initial URL to start scraping from.
    - site_url (str): The base URL of the site.
    - internal_class (str): A class name which contains the internal links. Leaf nodes will not have this class.
    - jsonl_fp (TextIOWrapper): The file pointer to write the JSONL records to.
    - regs (bool): Whether to scrape the regulations instead of the codes.
    - write_jsonl (bool): Whether to write the JSONL records to the file.
    - continue_from (list[int] | None): The lex_path to continue scraping from.

    Returns:
    - List[str]: A list of all leaf URLs.
    """
    collected_urls = []

    def helper(url: str, path: list[int], continue_from: list[int] | None):
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

                    helper(f"{site_url}{href}", new_path, new_continue_from)
            else:  # This is a leaf node
                # Skip the exact leaf node we are resuming from
                if continue_from and path == continue_from:
                    return

                collected_urls.append(url)
                print(url)
                process_code_leaf(state_name, url, jsonl_fp, regs, lex_path=path)
        else:
            print(
                f"Failed to retrieve content for {url}, Status Code: {response.status_code}"
            )
            with open(FAILED_FAILPATH, "a") as f:
                f.write(f"{url}\n")

    helper(init_url, [], continue_from)
    return collected_urls


def collect_codes_for_state(
    state_name: str, year: int = 2023, regs: bool = False, overwrite: bool = False
) -> None:
    """
    Collect all codes for the given state.

    Args:
    - state_name (str): The state code to scrape.
    - year (int): The year to scrape the codes for.
    - regs (bool): Whether to scrape the regulations instead of the codes.
    - overwrite (bool): Whether to overwrite existing files.

    Returns:
    - None
    """
    state_init_url = (
        f"{REGULATIONS_BASE_URL}/states/{JUR_URL_MAP[state_name]}/"
        if regs
        else f"{CODES_BASE_URL}{JUR_URL_MAP[state_name]}/{year}/"
    )
    save_dir = "regs" if regs else "codes"
    site_base_url = REGULATIONS_BASE_URL if regs else JUSTIA_BASE_URL
    # if {save_dir} does not exist, create it
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    continue_from = None
    mode = "w"
    if not overwrite:
        continue_from = get_last_lex_path(state_name, regs)
        if continue_from is not None:
            mode = "a"

    # Create a new 'codes/{state_name}.jsonl' file
    with open(f"{save_dir}/{state_name}.jsonl", mode) as f:
        collect_leaf_urls(
            state_name,
            state_init_url,
            f,
            regs=regs,
            site_url=site_base_url,
            continue_from=continue_from,
        )


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
        "-o",
        "--overwrite",
        action="store_true",
        help="Overwrite existing files instead of resuming.",
    )
    parser.add_argument(
        "-r",
        "-regs",
        help="Scrape the regulations instead of the codes.",
        action=argparse.BooleanOptionalAction,
    )
    args_ = parser.parse_args()
    collect_codes_for_state(
        args_.state, year=args_.year, regs=args_.r, overwrite=args_.overwrite
    )
