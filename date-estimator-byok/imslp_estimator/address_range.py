import re
from dataclasses import dataclass
from typing import Optional, List
from .date_range import DateRange


@dataclass
class AddressInfo:
    address: str
    city: str
    date_range: DateRange
    description: str

    @staticmethod
    def parse_date_info(text: str) -> Optional[DateRange]:
        """Parse date information from address text."""
        # Handle "in YYYY, YYYY" format
        if match := re.search(r"in (\d{4}),\s*(\d{4})", text):
            start_year, end_year = map(int, match.groups())
            return DateRange(
                start_year, end_year, text, "address date range", is_circa=False
            )

        # Handle "in YYYY" format
        if match := re.search(r"in (\d{4})", text):
            year = int(match.group(1))
            return DateRange(year, year, text, "address date", is_circa=False)

        # Handle "by YYYY" format
        if match := re.search(r"by (\d{4})", text):
            year = int(match.group(1))
            return DateRange(None, year, text, "address latest date")

        # Handle "until YYYY" format
        if match := re.search(r"until (\d{4})", text):
            year = int(match.group(1))
            return DateRange(None, year, text, "address latest date")

        # Handle "as early as YYYY" format
        if match := re.search(r"as early as (\d{4})", text):
            year = int(match.group(1))
            return DateRange(year, None, text, "address earliest date")

        # Handle "as late as YYYY" format
        if match := re.search(r"as late as (\d{4})", text):
            year = int(match.group(1))
            return DateRange(None, year, text, "address latest date")

        # Handle date range in parentheses with 4-digit years
        if match := re.search(r"\((\d{4})[-–](\d{4})\)", text):
            start, end = map(int, match.groups())
            return DateRange(start, end, text, "address date range")

        # Handle date range in parentheses with 4-digit start and 2-digit end
        if match := re.search(r"\((\d{4})[-–](\d{2})\)", text):
            start = int(match.group(1))
            end_year = int(match.group(2))
            # Assume end year is in the same century as start year
            century = start // 100
            end = (century * 100) + end_year
            return DateRange(start, end, text, "address date range")

        # Handle date range in parentheses with just start year and hyphen
        if match := re.search(r"\((\d{4})[-–]\s*\)", text):
            start = int(match.group(1))
            return DateRange(start, None, text, "address date range")

        # Handle single year in parentheses
        if match := re.search(r"\((\d{4})\)", text):
            year = int(match.group(1))
            return DateRange(year, year, text, "address date", is_circa=False)

        # Handle year range without parentheses
        if match := re.search(r"(\d{4})[-–](\d{4})", text):
            start, end = map(int, match.groups())
            return DateRange(start, end, text, "address date range")

        return None

    @staticmethod
    def parse_address_line(
        text: str, default_city: str = ""
    ) -> Optional["AddressInfo"]:
        """Parse a single address line into an AddressInfo object."""
        text = text.strip()
        if not text:
            return None

        # Extract city if present
        city_match = re.search(r",\s*([^,(]+)(?:\s*\(|$)", text)
        city = city_match.group(1) if city_match else default_city

        # Extract address
        address = text.split(",")[0].strip()

        # Parse date information
        date_range = AddressInfo.parse_date_info(text)

        if not date_range:
            return None

        return AddressInfo(
            address=address, city=city, date_range=date_range, description=text
        )


def extract_addresses_from_html(html: str) -> List[AddressInfo]:
    """Extract address information from IMSLP wiki page HTML."""
    from bs4 import BeautifulSoup

    addresses = []
    soup = BeautifulSoup(html, "html.parser")

    # Find the Addresses section
    addresses_header = soup.find("span", {"id": "Addresses"})
    if not addresses_header:
        return addresses

    # Get the content after the header
    current = addresses_header.parent
    while current and current.name != "ul":
        current = current.find_next_sibling()

    if not current:
        return addresses

    # Track the current city for nested addresses
    current_city = ""

    # Process each line
    for item in current.find_all("li"):
        text = item.get_text(strip=True)

        # Check if this is a city header
        if ":" in text and "(" not in text.split(":")[0]:
            current_city = text.split(":")[0].strip()
            continue

        # Try to parse the address
        address_info = AddressInfo.parse_address_line(text, current_city)
        if address_info:
            addresses.append(address_info)

    return addresses


def normalize_address(address: str) -> str:
    """Normalize address string for comparison."""
    # Convert to lowercase
    address = address.lower()
    # Normalize street abbreviations
    address = address.replace(" st.", " street")
    address = address.replace(" ave.", " avenue")
    address = address.replace(" rd.", " road")
    address = address.replace(" blvd.", " boulevard")
    address = address.replace(" sq.", " sq")
    address = address.replace(" square", " sq")
    # Normalize known spelling variations
    address = address.replace("chesnut", "chestnut")
    return address


def find_matching_addresses(addresses, query_address=None, query_city=None):
    """Find addresses that match the query address and/or city."""
    if not addresses:
        return []

    matching = []

    # Normalize queries if provided
    if query_address:
        # Remove any city part after comma and normalize
        query_address = normalize_address(query_address.split(",")[0].strip())
    if query_city:
        query_city = query_city.lower().strip()

    # If no queries provided, return all addresses
    if not query_address and not query_city:
        return addresses

    for addr in addresses:
        matches = True
        if query_address:
            addr_text = normalize_address(addr.address) if addr.address else ""
            if query_address not in addr_text and addr_text not in query_address:
                matches = False
        if query_city and matches:
            city_text = addr.city.lower() if addr.city else ""
            if query_city not in city_text:
                matches = False
        if matches:
            matching.append(addr)

    return matching
