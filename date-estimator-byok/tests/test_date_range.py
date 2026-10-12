import pytest
from imslp_estimator.date_range import DateRange, combine_date_ranges


def test_parse_single_year():
    dr = DateRange.parse("1850", "test")
    assert dr.start_year == 1850
    assert dr.end_year == 1850
    assert not dr.is_circa


def test_parse_range():
    dr = DateRange.parse("1850-1870", "test")
    assert dr.start_year == 1850
    assert dr.end_year == 1870
    assert dr.is_circa


def test_parse_or_later():
    dr = DateRange.parse("1850 or later", "test")
    assert dr.start_year == 1850
    assert dr.end_year is None
    assert dr.is_circa


def test_parse_or_earlier():
    dr = DateRange.parse("1850 or earlier", "test")
    assert dr.start_year is None
    assert dr.end_year == 1850
    assert dr.is_circa


def test_intersect_ranges():
    r1 = DateRange.parse("1850-1870", "test1")
    r2 = DateRange.parse("1860-1880", "test2")
    intersection = r1.intersect(r2)
    assert intersection.start_year == 1860
    assert intersection.end_year == 1870
    assert intersection.is_circa


def test_intersect_with_unbounded():
    r1 = DateRange.parse("1850 or later", "test1")
    r2 = DateRange.parse("1860-1880", "test2")
    intersection = r1.intersect(r2)
    assert intersection.start_year == 1860
    assert intersection.end_year == 1880
    assert intersection.is_circa


def test_no_intersection():
    r1 = DateRange.parse("1850-1860", "test1")
    r2 = DateRange.parse("1870-1880", "test2")
    intersection = r1.intersect(r2)
    assert intersection is None


def test_exact_dates():
    r1 = DateRange.parse("1850", "test1")
    r2 = DateRange.parse("1850", "test2")
    intersection = r1.intersect(r2)
    assert intersection.start_year == 1850
    assert intersection.end_year == 1850
    assert not intersection.is_circa


def test_combine_multiple_ranges():
    ranges = [
        DateRange.parse("1840 or later", "test1"),
        DateRange.parse("1850-1870", "test2"),
        DateRange.parse("1860 or earlier", "test3"),
    ]
    result = combine_date_ranges(ranges)
    assert result.start_year == 1850
    assert result.end_year == 1860
    assert result.is_circa


def test_format_output():
    cases = [
        ("1850", "1850"),
        ("1850-1870", "circa 1850-1870"),
        ("1850 or later", "circa 1850 or later"),
        ("1850 or earlier", "circa 1850 or earlier"),
    ]
    for input_str, expected in cases:
        dr = DateRange.parse(input_str, "test")
        assert dr.format() == expected
