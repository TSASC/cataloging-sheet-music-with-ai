# tests/test_core.py
import unittest
import warnings

from imslp_estimator.core import extract_estimated_year_from_plate_table, normalize


class TestNormalize(unittest.TestCase):
    def test_basic_normalization(self):
        self.assertEqual(normalize("B&H. (Ltd)"), "bh ltd")
        self.assertEqual(normalize("Breitkopf & Härtel"), "breitkopf hartel")
        self.assertEqual(normalize("G. Ricordi & C."), "g ricordi c")

    def test_json_plate_arrays_with_escaped_slashes_do_not_warn(self):
        html = r"""
        ["100", "", "https:\/\/imslp.org\/wiki\/Example", "", "", "", "1900"]
        ["200", "", "https:\/\/imslp.org\/wiki\/Example", "", "", "", "1910"]
        """

        with warnings.catch_warnings():
            warnings.simplefilter("error", SyntaxWarning)
            ranges = extract_estimated_year_from_plate_table(
                html, "150", "Example Publisher"
            )

        self.assertEqual(len(ranges), 1)
        self.assertEqual(ranges[0].start_year, 1900)
        self.assertEqual(ranges[0].end_year, 1910)


if __name__ == "__main__":
    unittest.main()
