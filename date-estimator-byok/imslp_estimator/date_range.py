import re
from dataclasses import dataclass
from typing import Optional, Tuple, List


@dataclass
class DateRange:
    start_year: Optional[int]
    end_year: Optional[int]
    description: str
    source: str
    is_circa: bool = True
    lower_bound_plate: Optional[str] = None
    lower_bound_year: Optional[int] = None
    upper_bound_plate: Optional[str] = None
    upper_bound_year: Optional[int] = None

    @staticmethod
    def parse(date_str: str, source: str) -> "DateRange":
        """Parse a date string into a DateRange object."""
        date_str = date_str.lower().strip()

        # Handle "or later" cases
        if "or later" in date_str:
            year = int(re.search(r"\d{4}", date_str).group())
            return DateRange(year, None, date_str, source)

        # Handle "or earlier" cases
        if "or earlier" in date_str:
            year = int(re.search(r"\d{4}", date_str).group())
            return DateRange(None, year, date_str, source)

        # Handle ranges like "1850-1870"
        range_match = re.search(r"(\d{4})[-–](\d{4})", date_str)
        if range_match:
            start, end = map(int, range_match.groups())
            return DateRange(start, end, date_str, source)

        # Handle single years
        year_match = re.search(r"\d{4}", date_str)
        if year_match:
            year = int(year_match.group())
            return DateRange(year, year, date_str, source, is_circa=False)

        raise ValueError(f"Could not parse date string: {date_str}")

    def intersect(self, other: "DateRange") -> Optional["DateRange"]:
        """Find the intersection of two date ranges."""
        # Calculate the intersection bounds
        start = max(self.start_year or float("-inf"), other.start_year or float("-inf"))
        end = min(self.end_year or float("inf"), other.end_year or float("inf"))

        # Check if the intersection is valid
        if start == float("-inf"):
            start = None
        if end == float("inf"):
            end = None
        if start is not None and end is not None and start > end:
            return None

        # Create description of the intersection
        desc = f"Intersection of {self.description} and {other.description}"
        # Combine sources
        source = f"{self.source} + {other.source}"
        # If both ranges are exact (not circa), the intersection is exact
        is_circa = self.is_circa or other.is_circa

        return DateRange(start, end, desc, source, is_circa)

    def format(self) -> str:
        """Format the date range as a string."""
        if self.start_year is None and self.end_year is None:
            return "Unknown date"

        prefix = "circa " if self.is_circa else ""

        if self.start_year == self.end_year and self.start_year is not None:
            return f"{prefix}{self.start_year}"

        if self.start_year is None:
            return f"{prefix}{self.end_year} or earlier"

        if self.end_year is None:
            return f"{prefix}{self.start_year} or later"

        return f"{prefix}{self.start_year}-{self.end_year}"

    @property
    def range_str(self) -> str:
        """Get a string representation of the range."""
        if self.start_year == self.end_year:
            return str(self.start_year)
        return f"circa {self.start_year}-{self.end_year}"


def combine_date_ranges(ranges: List[DateRange]) -> Optional[DateRange]:
    """Combine multiple date ranges to find the most precise intersection."""
    if not ranges:
        return None

    print("\n🔍 Found date ranges:")
    for r in ranges:
        print(f"  - {r.format()} ({r.source})")

    # Split ranges into main table and other sources
    main_table_ranges = [r for r in ranges if "main publisher table" in r.source]
    other_ranges = [r for r in ranges if "main publisher table" not in r.source]

    # Try intersection with all ranges first
    result = ranges[0]
    intersection_found = True

    for r in ranges[1:]:
        intersection = result.intersect(r)
        if intersection:
            result = intersection
        else:
            intersection_found = False
            break

    # If no intersection found and we have a main table range, try without it
    if not intersection_found and main_table_ranges and other_ranges:
        print("⚠️ No intersection found with main table date, trying without it")
        result = other_ranges[0]
        intersection_found = True

        for r in other_ranges[1:]:
            intersection = result.intersect(r)
            if intersection:
                result = intersection
            else:
                intersection_found = False
                break

    if not intersection_found:
        print("❌ No intersection found between date ranges")
        return None

    return result
