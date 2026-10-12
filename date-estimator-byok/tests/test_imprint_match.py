import pytest

from imslp_estimator.imprint_mapping import find_publisher_by_imprint


@pytest.mark.parametrize(
    ("imprint", "expected_name"),
    [
        ("Addison & Hodson", "Addison & Beale"),
        ("Oliver Ditson & Co.", "Oliver Ditson"),
        ("Goulding & D'Almaine", "Goulding & Co."),
    ],
)
def test_imprint_mapping_matches_known_variants(imprint, expected_name):
    result = find_publisher_by_imprint(imprint)

    assert result is not None
    assert result["name"] == expected_name
