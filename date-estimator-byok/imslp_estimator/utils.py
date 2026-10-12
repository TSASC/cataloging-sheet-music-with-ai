"""Utility functions shared across the IMSLP estimator."""

import unicodedata
import re
import os
import requests
from bs4 import BeautifulSoup
from typing import List, Dict

BASE_IMSLP_URL = "https://imslp.org"
PUBLISHER_LIST_URL = f"{BASE_IMSLP_URL}/wiki/IMSLP:Music_Publishers"
_cached_publishers = None  # Memory cache for main table


def normalize(text):
    """Normalize text for comparison."""
    text = text.lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\b(and|et|und)\b", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def fetch_html(url):
    """Fetch HTML content from a URL."""
    try:
        print(f"🌐 Fetching: {url}")

        # Extract the page name from the URL
        page_name = url.split("/")[-1]

        # Try to find a matching cached file
        cached_file = f"templates/{page_name} - IMSLP.html"
        if os.path.exists(cached_file):
            print(f"📂 Using cached file: {cached_file}")
            with open(cached_file, "r", encoding="utf-8") as f:
                return f.read()

        # If no cached file, try to fetch from web
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        return res.text
    except Exception as e:
        print(f"❌ Failed to fetch {url}: {e}")
        return None


def extract_main_table_publishers(html: str) -> List[Dict]:
    """Extract publisher information from the main IMSLP table."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="wikitable")
    entries = []
    if not table:
        return entries

    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) >= 4:
            name_cell = cells[0]
            year_range = cells[2].get_text(strip=True)
            address = cells[3].get_text(strip=True)
            link = name_cell.find("a")
            href = BASE_IMSLP_URL + link["href"] if link else None
            name = name_cell.get_text(strip=True)
            entries.append(
                {
                    "name": name,
                    "normalized": normalize(name),
                    "href": href,
                    "address": address,
                    "year_range": year_range,
                    "linked": bool(link),
                }
            )
    return entries


def get_cached_publishers() -> List[Dict]:
    """Get or fetch the list of publishers from IMSLP."""
    global _cached_publishers
    if _cached_publishers is None:
        print("🗂️ Caching IMSLP main table publishers...")
        html = fetch_html(PUBLISHER_LIST_URL)
        if not html:
            return []
        _cached_publishers = extract_main_table_publishers(html)
    return _cached_publishers
