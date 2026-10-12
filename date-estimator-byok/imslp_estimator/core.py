import requests
from bs4 import BeautifulSoup
import re
import json
from difflib import get_close_matches, SequenceMatcher
import unicodedata
from typing import List, Optional, Tuple, Dict
from .date_range import DateRange, combine_date_ranges
from .address_range import extract_addresses_from_html, find_matching_addresses
from .publisher_mapping import get_canonical_publisher
from .utils import normalize, fetch_html, get_cached_publishers, BASE_IMSLP_URL
import os
from .imprint_mapping import (
    find_publisher_by_imprint,
    load_imprint_mapping,
    save_imprint_mapping,
    extract_imprints_from_page,
)

PUBLISHER_LIST_URL = f"{BASE_IMSLP_URL}/wiki/IMSLP:Music_Publishers"
_cached_publishers = None  # Memory cache for main table


def get_cached_publishers():
    global _cached_publishers
    if _cached_publishers is None:
        print("🗂️ Caching IMSLP main table publishers...")
        html = fetch_html(PUBLISHER_LIST_URL)
        if not html:
            return []
        _cached_publishers = extract_main_table_publishers(html)
    return _cached_publishers


def fetch_html(url):
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


def normalize(text):
    text = text.lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\b(and|et|und)\b", "", text)
    text = re.sub(r"\s+", " ", text).strip()

    canonical = get_canonical_publisher(text)
    if canonical:
        return canonical

    return text


_IMPRINT_PREFIX_RE = re.compile(
    r"^(printed|published|engraved|sold|edited|chez|bei|verlag)\s+"
    r"(for|by|par|chez|von|de)?\s*",
    re.IGNORECASE,
)


def strip_imprint_phrases(text):
    """Extract a plausible publisher name from verbose imprint text."""
    text = _IMPRINT_PREFIX_RE.sub("", text)
    text = re.sub(
        r",?\s+(music sellers?|booksellers?|to their|to his|to her|"
        r"successors?|of? \w+ street|opposite|near|no\.\s*\d).*$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return text.strip()


def tight_normalize(name):
    """Lowercase, strip periods, preserve ampersands and collapse spaces."""
    return re.sub(r"\.", "", name.lower()).strip()


def extract_main_table_publishers(html):
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




def fetch_table_json(var_name):
    """Fetch table JSON data with proper authentication."""
    try:
        # Create a session with consistent headers
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Referer": "https://imslp.org/",
                "X-Requested-With": "XMLHttpRequest",
            }
        )

        # Set the authentication cookie
        session.cookies.set("BOT_DETECT_CLEARED", "3", domain="imslp.org", path="/")

        # Use IMSLP's API endpoint structure
        api_url = f"https://imslp.org/api.php"
        params = {"action": "ftbl", "format": "json", "var": var_name}

        # Fetch the data
        print(f"📡 Fetching JSON with params: {params}")
        response = session.get(api_url, params=params)
        response.raise_for_status()

        try:
            return response.json()
        except json.JSONDecodeError:
            print(f"❌ Invalid JSON response. Response text: {response.text[:200]}...")
            return None

    except requests.RequestException as e:
        print(f"❌ Request failed: {str(e)}")
        return None


def extract_estimated_year_from_plate_table(
    html, plate_number, target_name, publisher_url=None
) -> List[DateRange]:
    """Extract date ranges from plate number tables. Returns multiple possible ranges.

    Supports both traditional HTML tables and JSON-like array formats used by some publishers.
    """
    ranges = []
    found_plate_years = []

    print("\n=== Starting Plate Number Analysis ===")
    print(f"🔍 Looking for plate number: {plate_number}")

    # First try to extract from JSON-like arrays
    start_idx = 0
    while True:
        array_start = html.find('["', start_idx)
        if array_start == -1:
            break
        array_end = html.find("]", array_start)
        if array_end == -1:
            break

        array_content = html[array_start : array_end + 1]
        try:
            parts = json.loads(array_content)
            # Check if it's a valid plate entry (should have at least 7 elements)
            if len(parts) >= 7 and parts[0].strip().isdigit():
                plate_num = int(parts[0])
                year = (
                    parts[-1] if parts[-1] else parts[-2]
                )  # Try last element, then second-to-last
                if year and str(year).strip().isdigit():
                    found_plate_years.append((plate_num, int(year)))
        except:
            pass
        start_idx = array_end + 1

    if found_plate_years:
        print("✔️  Found plate numbers in JSON-like array format")
    else:
        # If no JSON arrays found, try traditional HTML tables
        soup = BeautifulSoup(html, "html.parser")

        # Find any mw-headline span containing "plate"
        plate_span = None
        for span in soup.find_all("span", class_="mw-headline"):
            if "plate" in span.get_text(strip=True).lower():
                plate_span = span
                print("✔️  plate_span text:", span.get_text(strip=True))
                break

        if not plate_span:
            print("⚠️ No plate-span found—HTML may have changed.")
            return ranges

        # Get the parent header
        header = plate_span.find_parent(["h2", "h3", "h4"])
        if not header:
            print("⚠️ Couldn't find parent header for plate-span")
            return ranges

        print(
            "✔️  Found header tag:",
            header.name,
            "with text:",
            plate_span.get_text(strip=True),
        )

        # Look for plate number table
        table = None

        # Find the first table with plate number information
        candidate = header.find_next("table", class_="wikitable")
        while candidate:
            first_th = candidate.find("th")
            if first_th and "plate" in first_th.get_text(strip=True).lower():
                table = candidate
                break
            candidate = candidate.find_next("table", class_="wikitable")

        if not table:
            print(
                "⚠️ Could not locate any table with a 'plate' header after the heading."
            )
            return ranges

        print("✔️  Found table:", table.get("class", []))

        # Process table rows
        headers = []
        header_row = table.find("tr")
        if header_row:
            headers = [
                th.get_text(strip=True).lower()
                for th in header_row.find_all(["th", "td"])
            ]
            print(f"   • Table headers: {headers}")

            # Find relevant column indices
            plate_col = next(
                (
                    i
                    for i, h in enumerate(headers)
                    if (h == "plate" or "plate #" in h) and "full" not in h
                ),
                None,
            )
            year_col = next(
                (i for i, h in enumerate(headers) if "year" in h and "full" not in h),
                None,
            )
            full_year_col = next(
                (i for i, h in enumerate(headers) if "full year" in h),
                None,
            )

            if year_col is None and full_year_col is not None:
                year_col = full_year_col

            if plate_col is not None and year_col is not None:
                print(f"   • Found cols: plate_col={plate_col}, year_col={year_col}")
                # Process table rows
                for row in table.find_all("tr")[1:]:  # Skip header row
                    cells = row.find_all(["td", "th"])
                    if len(cells) > max(plate_col, year_col):
                        plate_str = cells[plate_col].get_text(strip=True)
                        year_str = cells[year_col].get_text(strip=True)

                        if plate_str and plate_str.strip() != "n.d.":
                            try:
                                # Extract digits from plate number
                                plate_digits = re.sub(r"\D", "", plate_str)
                                if plate_digits:
                                    plate_num = int(plate_digits)

                                    # Clean and validate year
                                    if year_str and year_str.strip() != "n.d.":
                                        year_match = re.search(r"\d{4}", year_str)
                                        if year_match:
                                            year = int(year_match.group())
                                            found_plate_years.append((plate_num, year))
                            except ValueError as e:
                                print(f"Error parsing row data: {e}")
                print(f"   • Extracted {len(found_plate_years)} plate-year pairs")

    # Process found plate-year pairs
    print(f"\n📝 Total plate-year pairs = {len(found_plate_years)}")

    # Extract plate number for comparison
    if plate_number and found_plate_years:
        # First try exact match with spaces removed
        plate_digits = re.sub(r"\s", "", plate_number)

        # Handle cases like '1979-7' or '1979. 7' by taking the first number
        if not plate_digits.isdigit():
            # Try to find the first number in the string
            first_number_match = re.search(r"\d+", plate_digits)
            if first_number_match:
                plate_digits = first_number_match.group()
            else:
                # If no first number found, try removing non-digits
                plate_digits = re.sub(r"\D", "", plate_number)

        # Additional validation for Excel date format
        if not plate_digits and " 00:00:00" in plate_number:
            # Handle Excel date format
            date_part = plate_number.split(" ")[0]
            parts = date_part.split("-")
            if len(parts) >= 2:
                plate_digits = parts[0]  # Use the year part as plate number

        plate_int = int(plate_digits) if plate_digits.isdigit() else None
        print(f"🔢 Target plate number: {plate_int} (from {plate_number})")

        if plate_int is None:
            print(f"⚠️ Could not extract valid plate number from: {plate_number}")
            return ranges

        # Sort by plate number
        found_plate_years.sort(key=lambda x: x[0])
        print("\n📊 All found plate-year pairs (sorted):")
        for plate, year in found_plate_years:
            print(f"   Plate #{plate} -> Year {year}")

        # Find nearest lower and upper bounds
        lower = None
        upper = None
        for plate, year in found_plate_years:
            if plate <= plate_int:
                if not lower or plate > lower[0]:
                    lower = (plate, year)
            if plate >= plate_int:
                if not upper or plate < upper[0]:
                    upper = (plate, year)

        if lower:
            print(f"⬇️ Lower bound: Plate #{lower[0]} -> Year {lower[1]}")
        if upper:
            print(f"⬆️ Upper bound: Plate #{upper[0]} -> Year {upper[1]}")

        # Create date ranges based on bounds
        if lower and upper:
            try:
                # Sort years chronologically
                earlier_year = min(lower[1], upper[1])
                later_year = max(lower[1], upper[1])
                print(f"💡 Range: {earlier_year}-{later_year}")
                ranges.append(
                    DateRange(
                        start_year=earlier_year,
                        end_year=later_year,
                        description=f"Plate numbers from {lower[0]} ({lower[1]}) to {upper[0]} ({upper[1]})",
                        source=f"plate number bounds from plate {lower[0]} ({lower[1]}) to {upper[0]} ({upper[1]})",
                        lower_bound_plate=str(lower[0]),
                        lower_bound_year=lower[1],
                        upper_bound_plate=str(upper[0]),
                        upper_bound_year=upper[1],
                    )
                )
            except ValueError as e:
                print(f"⚠️ Failed to create date range: {e}")
        elif lower:
            try:
                print(f"💡 Only lower bound found: {lower[1]} or later")
                ranges.append(
                    DateRange(
                        start_year=lower[1],
                        end_year=lower[1],
                        description=f"After plate number {lower[0]} ({lower[1]})",
                        source=f"plate number lower bound from plate {lower[0]} ({lower[1]})",
                        lower_bound_plate=str(lower[0]),
                        lower_bound_year=lower[1],
                    )
                )
            except ValueError as e:
                print(f"⚠️ Failed to create date range: {e}")
        elif upper:
            try:
                print(f"💡 Only upper bound found: {upper[1]} or earlier")
                ranges.append(
                    DateRange(
                        start_year=upper[1],
                        end_year=upper[1],
                        description=f"Before plate number {upper[0]} ({upper[1]})",
                        source="plate number upper bound",
                        upper_bound_plate=str(upper[0]),
                        upper_bound_year=upper[1],
                    )
                )
            except ValueError as e:
                print(f"⚠️ Failed to create date range: {e}")
        else:
            print("⚠️ No valid plate-year pairs found to estimate from")
    else:
        print("⚠️ No valid plate-year pairs found to estimate from")

    return ranges


def extract_year_from_main_table(publisher_name, publishers) -> Optional[DateRange]:
    """Extract date range from the main publisher table."""
    norm_name = normalize(publisher_name)
    for pub in publishers:
        if pub["normalized"] == norm_name:
            try:
                year_range = pub["year_range"]
                if not year_range or year_range.strip() == "":
                    return None

                # Handle open-ended ranges like "1783-"
                if year_range.endswith("-"):
                    start_year = int(year_range[:-1])
                    return DateRange(
                        start_year=start_year,
                        end_year=None,
                        description=f"Main table entry: {year_range}",
                        source="main publisher table",
                    )

                # Parse year range in format "YYYY-YYYY" or just "YYYY"
                # Also handles formats like "cYYYY" or "YYYY-cYYYY"
                if "-" in year_range:
                    start_str, end_str = year_range.split("-")
                    # Remove 'c' prefix if present and convert to int
                    start_year = int("".join(c for c in start_str if c.isdigit()))
                    end_year = int("".join(c for c in end_str if c.isdigit()))
                else:
                    try:
                        # Remove 'c' prefix if present
                        year_str = "".join(c for c in year_range if c.isdigit())
                        start_year = end_year = int(year_str)
                    except ValueError:
                        # Try to extract first year if the format is unexpected
                        match = re.search(r"\d{4}", year_range)
                        if match:
                            start_year = end_year = int(match.group())
                        else:
                            return None

                return DateRange(
                    start_year=start_year,
                    end_year=end_year,
                    description=f"Main table entry: {year_range}",
                    source="main publisher table",
                )
            except (ValueError, TypeError):
                return None
    return None


def find_imprint_year_ranges(imprints_block_html, target_imprint) -> List[DateRange]:
    """Find all matching imprint date ranges."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(imprints_block_html, "html.parser")
    target_norm = tight_normalize(target_imprint)
    ranges = []

    print("🔍 Normalized target imprint for matching:", target_norm)

    # Get all section headers (h2, h3, etc)
    section_headers = soup.find_all(["h2", "h3", "h4"])

    # Find the Imprints header
    imprints_header = None
    for header in section_headers:
        if header.find("span", {"id": "Imprints"}):
            imprints_header = header
            break

    if not imprints_header:
        return ranges

    # Get all content until the next section header
    imprint_content = []
    current = imprints_header.find_next_sibling()
    while current and current.name not in ["h2", "h3", "h4"]:
        if current.name == "ul":
            imprint_content.extend(current.find_all("li"))
        current = current.find_next_sibling()

    # Process each list item in the imprints section
    for li in imprint_content:
        text = li.get_text(strip=True)

        # Split text into individual imprint entries if they're all on one line
        entries = text.split(")")
        for entry in entries:
            entry = entry.strip()
            if not entry:  # Skip empty entries
                continue

            # Add back the closing parenthesis if it was removed by split
            if not entry.endswith(")"):
                entry = entry + ")"

            imprint_name = entry
            start_year = None
            end_year = None

            # Try different date formats
            # Format 1: "Name (YYYY-YYYY)"
            match = re.search(r"^(.*?)\s*\((\d{4})[-–](\d{4})\)", entry)
            if match:
                imprint_name, start_year, end_year = match.groups()
                start_year, end_year = int(start_year), int(end_year)
            else:
                # Format 2: "Name (YYYY)"
                match = re.search(r"^(.*?)\s*\((\d{4})\)", entry)
                if match:
                    imprint_name, year = match.groups()
                    start_year = end_year = int(year)
                else:
                    # Format 3: "Name, YYYY-YYYY"
                    match = re.search(r"^(.*?),\s*(\d{4})[-–](\d{4})", entry)
                    if match:
                        imprint_name, start_year, end_year = match.groups()
                        start_year, end_year = int(start_year), int(end_year)
                    else:
                        # Format 4: "Name, YYYY"
                        match = re.search(r"^(.*?),\s*(\d{4})", entry)
                        if match:
                            imprint_name, year = match.groups()
                            start_year = end_year = int(year)

            if start_year is not None and end_year is not None:
                imprint_norm = tight_normalize(imprint_name)
                print(f"🔎 Checking imprint: {imprint_name} → {imprint_norm}")
                if imprint_norm == target_norm:
                    try:
                        ranges.append(
                            DateRange(
                                start_year=start_year,
                                end_year=end_year,
                                description=f"Imprint: {entry}",
                                source="imprint match",
                            )
                        )
                    except ValueError:
                        continue

    return ranges


def clean_html(html_str):
    """Clean HTML from a string, extracting text from links and removing other tags."""
    if not html_str:
        return ""
    # Extract text between >text</a>
    match = re.search(r">([^<]+)</a>", html_str)
    if match:
        return match.group(1)
    # Remove any remaining HTML tags
    text = re.sub(r"<[^>]+>", "", html_str)
    # Remove any escaped characters
    text = text.replace('\\"', '"').replace("\\'", "'").replace("\\", "")
    return text.strip()


def estimate_year(publisher_input, plate_input, address_input, city_input=None):
    """Estimate publication year using all available sources."""
    publishers = get_cached_publishers()
    date_ranges = []
    address_matches = []

    # Try to find publisher in main table
    found_publisher = None
    norm_input = normalize(publisher_input)

    # Also try with verbose imprint phrases stripped
    stripped = strip_imprint_phrases(publisher_input)
    norm_stripped = normalize(stripped) if stripped != publisher_input else norm_input

    candidates = [norm_input]
    if norm_stripped != norm_input:
        candidates.append(norm_stripped)

    # 1. Exact match
    for candidate in candidates:
        for pub in publishers:
            if pub["normalized"] == candidate:
                found_publisher = pub
                break
        if found_publisher:
            break

    # 2. Substring match — check if a known publisher name appears inside the input
    if not found_publisher:
        for candidate in candidates:
            best_len = 0
            for pub in publishers:
                pn = pub["normalized"]
                if len(pn) > 2 and pn in candidate and len(pn) > best_len:
                    best_len = len(pn)
                    found_publisher = pub
            if found_publisher:
                break

    # 3. Fuzzy matching with 0.7 cutoff
    if not found_publisher:
        best_score = 0
        for candidate in candidates:
            for pub in publishers:
                score = SequenceMatcher(None, candidate, pub["normalized"]).ratio()
                if score >= 0.7 and score > best_score:
                    best_score = score
                    found_publisher = pub

    # 4. Imprint mapping lookup
    if not found_publisher:
        for name in [publisher_input, stripped]:
            imprint_match = find_publisher_by_imprint(name)
            if imprint_match:
                for pub in publishers:
                    if pub["href"] == imprint_match["href"]:
                        found_publisher = pub
                        break
            if found_publisher:
                break

    if not found_publisher:
        return {
            "date_ranges": [],
            "address_matches": [],
            "found_publisher": None,
            "error": "Publisher not found",
        }

    # Collect all possible date ranges
    date_ranges = []
    address_matches = []

    # 1. Try plate number and imprint ranges from wiki page
    if found_publisher["href"]:
        wiki_html = fetch_html(found_publisher["href"])
        if wiki_html:
            # Get dates from plate numbers
            plate_ranges = extract_estimated_year_from_plate_table(
                wiki_html, plate_input, publisher_input, found_publisher["href"]
            )
            date_ranges.extend(plate_ranges)

            # Get dates from imprints
            imprint_ranges = find_imprint_year_ranges(wiki_html, publisher_input)
            date_ranges.extend(imprint_ranges)

            # Get dates from addresses if address is provided
            if address_input or city_input:
                addresses = extract_addresses_from_html(wiki_html)
                matching_addresses = find_matching_addresses(
                    addresses, address_input, city_input
                )
                for addr in matching_addresses:
                    date_ranges.append(addr.date_range)
                    address_matches.append(
                        {
                            "address": addr.address,
                            "city": addr.city,
                            "date": addr.date_range.format(),
                            "description": addr.description,
                        }
                    )

    # 2. Add main table range
    main_table_range = extract_year_from_main_table(found_publisher["name"], publishers)
    if main_table_range:
        date_ranges.append(main_table_range)

    # Combine all ranges to find the most precise estimate
    final_range = combine_date_ranges(date_ranges) if date_ranges else None

    # Group ranges by type for the structured output
    all_ranges = []
    main_table_entry = None
    imprint_entries = []
    address_entries = []
    plate_entries = []

    for r in date_ranges:
        range_entry = {
            "range": r.format(),
            "source": r.source,
            "description": r.description,
        }
        all_ranges.append(range_entry)

        if "main publisher table" in r.source:
            main_table_entry = range_entry
        elif "imprint match" in r.source:
            imprint_entries.append(range_entry)
        elif "address" in r.source:
            address_entries.append(range_entry)
        elif "plate" in r.source:
            plate_entries.append(range_entry)

    return {
        "publisher": found_publisher["name"],
        "link": found_publisher["href"],
        "plate": plate_input,
        "address": address_input,
        "city": city_input,
        "estimated_year": (
            "Dates found, but no intersection"
            if all_ranges and not final_range
            else (final_range.format() if final_range else None)
        ),
        "source": final_range.source if final_range else None,
        "all_ranges": all_ranges,
        "address_matches": address_matches,
        "date_estimation_analysis": {
            "main_table": (
                main_table_entry["range"] if main_table_entry else "No range provided"
            ),
            "imprint": (
                f"{imprint_entries[0]['range']}, match found with imprint: {imprint_entries[0]['description']}"
                if imprint_entries
                else "No close imprint match"
            ),
            "address": (
                f"{address_entries[0]['range']}, match found with address: {address_entries[0]['description']}"
                if address_entries
                else "No date extracted"
            ),
            "plate": (
                f"{plate_entries[0]['range']}, {plate_entries[0]['source']}"
                if plate_entries
                else "Not enough information on table"
            ),
        },
    }


def format_results(
    publisher_name: str,
    publisher_url: str,
    plate_number: str,
    address: str,
    city: str,
    final_estimate: str,
    date_ranges: List[DateRange],
) -> str:
    """Format the results into a nice HTML string."""
    result = "<h1>Result</h1>\n\n"
    result += f"<b>Publisher:</b> {publisher_name}\n\n"
    result += f"<b>Link:</b> {publisher_url}\n\n"
    result += f"<b>Plate Number:</b> {plate_number}\n\n"
    result += f"<b>Address:</b> {address}\n\n"
    result += f"<b>City:</b> {city}\n\n"
    result += "<h2>Date Estimation</h2>\n\n"
    result += f"<b>Final Estimate:</b> {final_estimate}\n\n"
    result += "<b>Based on:</b> "

    # Add explanation for each date range
    if date_ranges:
        result += date_ranges[0].source + "\n\n"
        result += "<b>All Found Date Ranges</b>\n"

        # Group ranges by source type
        main_table_range = None
        imprint_ranges = []
        address_ranges = []
        plate_ranges = []

        for date_range in date_ranges:
            if "main publisher table" in date_range.source:
                main_table_range = date_range
            elif "imprint match" in date_range.source:
                imprint_ranges.append(date_range)
            elif "address" in date_range.source:
                address_ranges.append(date_range)
            elif "plate" in date_range.source:
                plate_ranges.append(date_range)

        # Format main table range
        if main_table_range:
            result += f"• {main_table_range.format()}\n"
        else:
            result += "• No range provided in main table\n"

        # Format imprint matches
        if imprint_ranges:
            for r in imprint_ranges:
                result += f"• {r.format()} ({r.description})\n"
        else:
            result += "• No close imprint match\n"

        # Format address matches
        if address_ranges:
            for r in address_ranges:
                result += f"• {r.format()} ({r.description})\n"
        elif address:
            result += "• Address supplied but date not extracted\n"
        else:
            result += "• No address supplied\n"

        # Format plate number matches
        if plate_ranges:
            for r in plate_ranges:
                if r.lower_bound_plate and r.upper_bound_plate:
                    result += f"• plate number bounds from plate {r.lower_bound_plate}({r.lower_bound_year}) to {r.upper_bound_plate}({r.upper_bound_year})\n"
                elif r.lower_bound_plate:
                    result += f"• plate number match to {r.lower_bound_plate}({r.lower_bound_year})\n"
                else:
                    result += f"• {r.format()} ({r.source})\n"
        elif plate_number:
            result += "• Not enough information in plate number table\n"
        else:
            result += "• No plate number provided\n"
    else:
        result += "no data found\n"

    result += "\n<a href='javascript:void(0)' onclick='window.electronAPI.tryAnotherSearch()'>Try Another Search</a>"
    return result


def get_date_range_from_plate_bounds(
    plate_number: str,
    below: Optional[Tuple[int, int]],
    above: Optional[Tuple[int, int]],
) -> Optional[DateRange]:
    """Get a date range from plate number bounds."""
    if not below or not above:
        return None

    below_plate, below_year = below
    above_plate, above_year = above

    # Create date range with the bounding plate numbers and years
    return DateRange(
        start_year=min(below_year, above_year),
        end_year=max(below_year, above_year),
        description=f"Plate numbers from {below_plate} to {above_plate}",
        source="plate number bounds",
        lower_bound_plate=str(below_plate),
        lower_bound_year=below_year,
        upper_bound_plate=str(above_plate),
        upper_bound_year=above_year,
    )


def rebuild_single_publisher_mapping(publisher_url: str) -> None:
    """Rebuild the imprint mapping for a single publisher."""
    mapping = load_imprint_mapping()
    html = fetch_html(publisher_url)

    if not html:
        print(f"⚠️ Could not fetch page {publisher_url}")
        return

    # Extract publisher name from URL
    publisher_name = publisher_url.split("/")[-1].replace("_", " ")

    # First add the main publisher name as an imprint
    normalized_main = normalize(publisher_name)
    if normalized_main:
        mapping["imprints"][normalized_main] = {
            "original_name": publisher_name,
            "main_publisher": {
                "name": publisher_name,
                "href": publisher_url,
                "year_range": "",
            },
        }

    # Then process additional imprints
    imprints = extract_imprints_from_page(html)
    print(f"Found {len(imprints)} imprints")

    for imprint in imprints:
        normalized_imprint = normalize(imprint)
        if normalized_imprint and normalized_imprint != normalized_main:
            mapping["imprints"][normalized_imprint] = {
                "original_name": imprint,
                "main_publisher": {
                    "name": publisher_name,
                    "href": publisher_url,
                    "year_range": "",
                },
            }
            print(f"✓ Added imprint: {imprint}")

    save_imprint_mapping(mapping)
