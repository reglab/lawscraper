# State Law Scraper

This repo contains a scraper for collecting the relevant state **codes** and **regulations** from Justia.

There are two scrapers available:
- `scraper.py`: A simple, single-threaded scraper for one state at a time.
- `ms.py`: A multi-threaded scraper capable of scraping multiple states in parallel, with progress bars and the ability to resume interrupted downloads.

## `ms_fl_scraper.py` (FindLaw scraper)

Scrapes state codes from FindLaw (JS-driven pages). It launches real browsers via Playwright and streams results into a single JSONL file per state.

Usage:

```bash
python ms_fl_scraper.py <state> \
	[-o OUTPUT_DIR] [-p PROCESSES] [-t THREADS] [-c CHUNKS_PER_PROC]
```

Flags:
- `-o, --output-dir` Directory for output (default: `findlaw_codes`). Writes `<STATE>.jsonl` inside it.
- `-p, --processes` Number of browser processes (default: 6). Set to `1` for single-browser mode.
- `-t, --threads` Threads per process for fetching leaf pages (default: 8).
- `-c, --chunks-per-proc` Work chunk factor per process to improve progress responsiveness (default: 4).

Examples:
```bash
# Scrape New York to default folder with defaults
python ms_fl_scraper.py NY

# Scrape Pennsylvania with custom concurrency and output directory
python ms_fl_scraper.py PA -p 6 -t 8 -c 4 -o findlaw_codes

# Single-browser (more granular per-section leaf bars in the console)
python ms_fl_scraper.py KY -p 1
```

Notes:
- Requires Playwright with Chromium installed. If needed: `pip install playwright` then `playwright install chromium`.
- Progress bars: parent process shows a "Sections" bar; with `-p 1` you’ll also see per-section leaf bars.

## `scraper.py` Usage

To download the code for a single state, use 
```bash
> python scraper.py CA
```

To download regulations, we use:
```bash
> python scraper.py CA -r
```

## `ms.py` (Multi-threaded Scraper) Usage

It is recommended to use `ms.py` for scraping multiple states or for large states.

### Scraping Specific States
To download codes for multiple states in parallel:
```bash
python ms.py --states CA TX NY
```

### Scraping a Range of States
To scrape a range of states (alphabetically):
```bash
python ms.py --range AL AZ
```

### Scraping All States
To scrape all available states:
```bash
python ms.py --all
```

### Scraping Regulations
Add the `-r` or `--regs` flag to any of the above commands to download regulations instead of codes.
```bash
python ms.py --states CA TX -r
```

### Specifying Number of Threads
You can control the number of parallel threads with the `-t` or `--threads` flag:
```bash
python ms.py --all -t 8
```
