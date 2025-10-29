#!/usr/bin/env python3
import glob
import os
from collections import defaultdict


def merge_jsonl_parts_in_directory(directory: str):
    part_files = glob.glob(os.path.join(directory, "*.part*.jsonl"))
    if not part_files:
        print(f"No part files found in {directory}")
        return

    groups = defaultdict(list)
    for path in part_files:
        filename = os.path.basename(path)
        prefix = filename.split(".part")[0]
        groups[prefix].append(path)

    for prefix, files in groups.items():
        files.sort()
        merged_path = os.path.join(directory, f"{prefix}.merged.jsonl")
        with open(merged_path, "w", encoding="utf-8") as outfile:
            for f in files:
                with open(f, "r", encoding="utf-8") as infile:
                    for line in infile:
                        outfile.write(line)
        print(f"Merged {len(files)} parts into {merged_path}")


def main():
    # This line makes it look in the same directory as this script file:
    current_dir = os.path.dirname(os.path.abspath(__file__))
    merge_jsonl_parts_in_directory(current_dir)


if __name__ == "__main__":
    main()
