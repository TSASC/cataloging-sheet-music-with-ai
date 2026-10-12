"""Module for handling publisher name mappings and normalization."""

from typing import Dict, Optional

# Map of normalized publisher names to their canonical IMSLP publisher entry
PUBLISHER_MAPPINGS: Dict[str, str] = {
    # Firth, Pond & Co. variants
    "firth pond co": "john firth",
    "firth hall": "john firth",
    "firth hall pond": "john firth",
    "firth pond co": "john firth",
    "firth son co": "john firth",
    # Schott variants
    "schott freres": "b schotts sohne",
    "schott co": "b schotts sohne",
    "schott": "b schotts sohne",
    "b schott": "b schotts sohne",
    "bernhard schott": "b schotts sohne",
    "schott music": "b schotts sohne",
    "schott musik": "b schotts sohne",
    "schott international": "b schotts sohne",
    "verlag schott": "b schotts sohne",
    "musikverlag schott": "b schotts sohne",
    "edition schott": "b schotts sohne",
    "schott edition": "b schotts sohne",
    # Birchall variants
    "birchall andrews": "robert birchall",
    "birchall co": "robert birchall",
    "birchall lonsdale mills": "robert birchall",
    # Goulding variants
    "goulding co": "goulding co",
    "goulding dalmaine": "goulding co",
    "goulding phipps dalmaine": "goulding co",
    "goulding dalmaine potter co": "goulding co",
    "george goulding": "goulding co",
    # Breitkopf variants
    "breitkopf hartel": "breitkopf hartel",
    "breitkopf": "breitkopf hartel",
    # Ricordi variants
    "g ricordi": "g ricordi co",
    "ricordi": "g ricordi co",
    "giovanni ricordi": "g ricordi co",
    "tito ricordi": "g ricordi co",
    # Peters variants
    "c f peters": "c f peters",
    "peters": "c f peters",
    "edition peters": "c f peters",
    # Novello variants
    "novello co": "novello co",
    "novello ewer co": "novello co",
    "novello ewer": "novello co",
    # Boosey variants
    "boosey co": "boosey co",
    "boosey hawkes": "boosey hawkes",
    # Leon Grus variants
    "leon grus": "grus",
}


def get_canonical_publisher(normalized_name: str) -> Optional[str]:
    """Get the canonical publisher name for a normalized publisher name.

    Args:
        normalized_name: The normalized publisher name to look up

    Returns:
        The canonical publisher name if a mapping exists, otherwise None
    """
    return PUBLISHER_MAPPINGS.get(normalized_name)
