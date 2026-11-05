"""
tests/matching/test_normalization.py

Tests for Ethiopian phone, account, and transliteration normalization.
"""

from app.matching.normalization import (
    normalize_account_number,
    normalize_ethiopian_name,
    normalize_phone,
    string_similarity,
)


class TestNormalization:
    def test_normalize_phone_formats(self):
        assert normalize_phone("+251 91 123 4567") == "911234567"
        assert normalize_phone("0911234567") == "911234567"
        assert normalize_phone("251911234567") == "911234567"
        assert normalize_phone("911234567") == "911234567"
        assert normalize_phone("0711234567") == "711234567"
        assert normalize_phone("invalid") is None
        assert normalize_phone(None) is None

    def test_normalize_account(self):
        assert normalize_account_number("1000-1234-567") == "10001234567"
        assert normalize_account_number(" 10001234567 ") == "10001234567"
        assert normalize_account_number("00012345") == "12345"
        assert normalize_account_number(None) is None

    def test_normalize_ethiopian_names(self):
        # Strips retail suffixes
        assert normalize_ethiopian_name("Abebe Kebede General Trading") == "abebe kebede"
        assert normalize_ethiopian_name("Almaz Shop") == "almaz"
        assert normalize_ethiopian_name("Chala Bekele PLC") == "cala bekele"

        # Transliteration equivalence: double consonants collapsed
        assert normalize_ethiopian_name("Abebe") == normalize_ethiopian_name("Abebbe")
        assert normalize_ethiopian_name("Kassahun") == normalize_ethiopian_name("Kasahun")

    def test_string_similarity(self):
        # Identical
        assert string_similarity("Abebe Kebede", "Abebe Kebede") == 1.0

        # With shop suffix variation
        assert string_similarity("Abebe Kebede", "Abebe Kebede Shop") >= 0.95

        # Transliteration variation
        assert string_similarity("Kassahun Store", "Kasahun Shop") >= 0.95
        assert string_similarity("Mohammed Ali", "Muhamed Ali Trading") >= 0.90

        # Completely different names
        assert string_similarity("Abebe Kebede", "Aster Aweke") < 0.40
