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
    state_name: str, url: str, jsonl_fp: TextIOWrapper | None, is_reg: bool = False
) -> dict:
    """
    Process the content of a leaf node in the Justia website.

    Args:
    - url (str): The URL of the leaf node.
    - jsonl_fp (TextIOWrapper): The file pointer to write the JSONL records to.
    - is_reg (bool): Whether the URL is for a regulation or not.

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


def collect_leaf_urls(
    state_name: str,
    init_url: str,
    jsonl_fp: TextIOWrapper | None,
    internal_class: str = "codes-listing",
    site_url: str = JUSTIA_BASE_URL,
    regs: bool = False,
    write_jsonl=True,
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

    Returns:
    - List[str]: A list of all leaf URLs.
    """
    collected_urls = []

    def helper(url: str):
        response = requests.get(url, headers=HEADERS)
        if response.status_code == 200:
            soup: BeautifulSoup = BeautifulSoup(response.content, "html.parser")
            internal_links = soup.find(
                class_=internal_class
            )  # these will be URLs relative to the base_url
            if internal_links:
                internal_links = extract_links_from_content(internal_links)
                for link in internal_links:
                    href = link["href"]
                    helper(f"{site_url}{href}")
            else:
                collected_urls.append(url)
                print(url)
                leaf_record = process_code_leaf(state_name, url, jsonl_fp, regs)
        else:
            print(
                f"Failed to retrieve content for {url}, Status Code: {response.status_code}"
            )
            with open(FAILED_FAILPATH, "a") as f:
                f.write(f"{url}\n")

    helper(init_url)
    return collected_urls


def collect_codes_for_state(
    state_name: str, year: int = 2023, regs: bool = False
) -> None:
    """
    Collect all codes for the given state.

    Args:
    - state_name (str): The state code to scrape.
    - year (int): The year to scrape the codes for.
    - regs (bool): Whether to scrape the regulations instead of the codes.

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
    # Create a new 'codes/{state_name}.jsonl' file
    with open(f"{save_dir}/{state_name}.jsonl", "w") as f:
        collect_leaf_urls(
            state_name, state_init_url, f, regs=regs, site_url=site_base_url
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
        "-r",
        "-regs",
        help="Scrape the regulations instead of the codes.",
        action=argparse.BooleanOptionalAction,
    )
    args_ = parser.parse_args()
    collect_codes_for_state(args_.state, year=args_.year, regs=args_.r)
