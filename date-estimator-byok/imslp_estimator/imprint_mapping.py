"""Module for managing IMSLP publisher imprint mappings."""

import json
import os
import re
from typing import Dict, Optional, List
from bs4 import BeautifulSoup
from .utils import normalize, fetch_html, get_cached_publishers

# Type aliases
ImprintInfo = Dict[str, str]
PublisherInfo = Dict[str, str]
ImprintMapping = Dict[str, Dict[str, dict]]


def load_imprint_mapping() -> ImprintMapping:
    """Load the imprint mapping from the JSON file."""
    mapping_file = os.path.join(
        os.path.dirname(__file__), "data", "imprint_mapping.json"
    )
    if not os.path.exists(mapping_file):
        return {"imprints": {}}

    with open(mapping_file, "r", encoding="utf-8") as f:
        return json.load(f)


def save_imprint_mapping(mapping: ImprintMapping) -> None:
    """Save the imprint mapping to the JSON file."""
    mapping_file = os.path.join(
        os.path.dirname(__file__), "data", "imprint_mapping.json"
    )
    os.makedirs(os.path.dirname(mapping_file), exist_ok=True)

    with open(mapping_file, "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2, ensure_ascii=False)


def extract_imprints_from_page(html: str) -> List[str]:
    """Extract all imprint names from a publisher's IMSLP page."""
    soup = BeautifulSoup(html, "html.parser")
    imprints = []

    def process_list_items(ul_element):
        """Helper function to process list items and extract imprints."""
        for li in ul_element.find_all("li", recursive=False):
            # Get text content, including any linked text
            text_parts = []
            for content in li.contents:
                if content.name == "a":
                    text_parts.append(content.get_text(strip=True))
                elif isinstance(content, str):
                    text_parts.append(content.strip())
            text = " ".join(text_parts).strip()

            # Extract name part before any dates or other annotations
            name_part = text.split("(")[0].strip()

            # Handle multiple names separated by /
            for variant in name_part.split("/"):
                clean_name = variant.strip().rstrip(" .,")
                if clean_name and clean_name not in imprints:
                    imprints.append(clean_name)

    def process_section_content(current):
        """Process all content under a section until the next section header."""
        while current and current.name not in ["h2", "h3", "h4"]:
            if current.name == "ul":
                process_list_items(current)
            elif current.name == "dl":
                for dd in current.find_all("dd", recursive=False):
                    for ul in dd.find_all("ul", recursive=False):
                        process_list_items(ul)
            current = current.find_next_sibling()

    # First try to find the main imprints section
    for header in soup.find_all("h2"):
        if "Imprint" in header.get_text(strip=True):
            current = header.find_next_sibling()
            found_subsection = False

            # Look through content
            while current and current.name != "h2":
                # If we find an imprints subsection, process it
                if current.name in ["h3", "h4"] and "Imprint" in current.get_text(
                    strip=True
                ):
                    found_subsection = True
                    process_section_content(current.find_next_sibling())
                # If it's a non-imprints subsection, skip its content
                elif current.name in ["h3", "h4"]:
                    while current and current.name not in ["h2", "h3", "h4"]:
                        current = current.find_next_sibling()
                # If it's content and we haven't found a subsection yet, process it
                elif not found_subsection:
                    if current.name == "ul":
                        process_list_items(current)
                    elif current.name == "dl":
                        for dd in current.find_all("dd", recursive=False):
                            for ul in dd.find_all("ul", recursive=False):
                                process_list_items(ul)

                current = current.find_next_sibling()

    # If no imprints found yet, look for any headers containing "Imprint"
    if not imprints:
        for header in soup.find_all(["h3", "h4"]):
            if "Imprint" in header.get_text(strip=True):
                process_section_content(header.find_next_sibling())

    return imprints


def build_imprint_mapping() -> ImprintMapping:
    """Build a complete mapping of imprints to their main publisher entries."""
    print("🌐 Fetching publishers from IMSLP main table...")
    publishers = get_cached_publishers()
    if not publishers:
        print("❌ Failed to fetch publishers from IMSLP")
        return {"imprints": {}}

    print(f"✅ Found {len(publishers)} publishers in main table")
    mapping = {"imprints": {}}
    processed = 0

    for publisher in publishers:
        if not publisher.get("href"):
            continue

        processed += 1
        print(f"\n📖 Processing {processed}/{len(publishers)}: {publisher['name']}...")

        # First add the main publisher name as an imprint of itself
        main_name = publisher["name"]
        normalized_main = normalize(main_name)
        if normalized_main:
            mapping["imprints"][normalized_main] = {
                "original_name": main_name,
                "main_publisher": {
                    "name": publisher["name"],
                    "href": publisher["href"],
                    "year_range": publisher.get("year_range", ""),
                },
            }

        # Then fetch and process the publisher's page for additional imprints
        html = fetch_html(publisher["href"])
        if not html:
            print(f"⚠️ Skipping {publisher['name']} - could not fetch page")
            continue

        # Get imprints - variants are already handled in extract_imprints_from_page
        imprints = extract_imprints_from_page(html)
        print(f"   Found {len(imprints)} imprints")

        for imprint in imprints:
            # Create normalized key for this imprint
            normalized_imprint = normalize(imprint)
            if normalized_imprint and normalized_imprint != normalized_main:
                mapping["imprints"][normalized_imprint] = {
                    "original_name": imprint,
                    "main_publisher": {
                        "name": publisher["name"],
                        "href": publisher["href"],
                        "year_range": publisher.get("year_range", ""),
                    },
                }
                print(f"   ✓ Added imprint: {imprint}")

    total_imprints = len(mapping["imprints"])
    print(
        f"\n✨ Mapping complete! Found {total_imprints} total imprints across {processed} publishers"
    )
    return mapping


def find_publisher_by_imprint(imprint_name: str) -> Optional[PublisherInfo]:
    """Find a publisher entry by searching through known imprints."""
    mapping = load_imprint_mapping()
    normalized_name = normalize(imprint_name)

    imprint_entry = mapping["imprints"].get(normalized_name)
    if imprint_entry:
        return imprint_entry["main_publisher"]

    return None


def refresh_imprint_mapping() -> None:
    """Rebuild and save the imprint mapping."""
    mapping = build_imprint_mapping()
    save_imprint_mapping(mapping)
