from imslp_estimator.address_range import (
    AddressInfo,
    extract_addresses_from_html,
    find_matching_addresses,
)
from imslp_estimator.date_range import DateRange


def test_parse_date_info():
    cases = [
        ("107 Washington Street, Boston, MA (in 1835)", 1835, 1835, False),
        ("449 and 451 Washington Street (by 1876)", None, 1876, True),
        ("277 Washington Street (as early as 1858, as late as 1872)", 1858, None, True),
        ("No date info here", None, None, True),
    ]

    for text, expected_start, expected_end, is_circa in cases:
        date_range = AddressInfo.parse_date_info(text)
        if expected_start is None and expected_end is None:
            assert date_range is None
        else:
            assert date_range is not None
            assert date_range.start_year == expected_start
            assert date_range.end_year == expected_end
            assert date_range.is_circa == is_circa


def test_parse_address_line():
    cases = [
            (
                "107 Washington Street, Boston, MA (in 1835)",
                "107 Washington Street",
                "MA ",
                1835,
                1835,
            ),
            (
                "449 and 451 Washington Street (by 1876)",
                "449 and 451 Washington Street (by 1876)",
                "",
                None,
                1876,
        ),
    ]

    for text, exp_addr, exp_city, exp_start, exp_end in cases:
        info = AddressInfo.parse_address_line(text)
        assert info is not None
        assert info.address == exp_addr
        if exp_city:
            assert exp_city in info.city
        else:
            assert info.city == exp_city
        assert info.date_range.start_year == exp_start
        assert info.date_range.end_year == exp_end


def test_extract_addresses_from_first_address_list():
    html = """
    <h2><span class="mw-headline" id="Addresses">Addresses</span></h2>
    <p>Boston offices:</p>
    <ul>
        <li>107 Washington Street, Boston, MA (in 1835)</li>
        <li>449 and 451 Washington Street (by 1876)</li>
    </ul>
    <p>New York office:</p>
    <ul>
        <li>711 Broadway (in 1876)</li>
    </ul>
    """

    addresses = extract_addresses_from_html(html)
    assert len(addresses) == 2

    assert addresses[0].address == "107 Washington Street"
    assert addresses[0].city == "MA "
    assert addresses[0].date_range.start_year == 1835
    assert addresses[0].date_range.end_year == 1835


def test_find_matching_addresses():
    addresses = [
        AddressInfo(
            address="107 Washington Street",
            city="Boston, MA",
            date_range=DateRange(1835, 1835, "", "test"),
            description="test",
        ),
        AddressInfo(
            address="711 Broadway",
            city="New York",
            date_range=DateRange(1876, 1876, "", "test"),
            description="test",
        ),
    ]

    # Test exact match
    matches = find_matching_addresses(addresses, "107 Washington Street", "Boston")
    assert len(matches) == 1
    assert matches[0].address == "107 Washington Street"

    # Test partial match
    matches = find_matching_addresses(addresses, "Washington", "Boston")
    assert len(matches) == 1

    # Test city only
    matches = find_matching_addresses(addresses, "", "New York")
    assert len(matches) == 1
    assert matches[0].address == "711 Broadway"

    # Test no match
    matches = find_matching_addresses(addresses, "123 Main St", "Chicago")
    assert len(matches) == 0
