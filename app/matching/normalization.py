"""
app/matching/normalization.py

Deterministic text, phone, and name normalization for Ethiopian business records.
Handles:
  - Phone normalization (+251 9... / 09... / 9...)
  - Bank account normalization (stripping spaces, hyphens, leading zeros)
  - Transliterated Ethiopian names (Amharic to Latin variations)
  - Token-level and character-level similarity metrics (deterministic, no external deps)
"""

from __future__ import annotations

import functools
import re
import unicodedata


# Common business suffixes to strip for canonical comparison
BUSINESS_SUFFIXES = {
    "shop",
    "suq",
    "souk",
    "store",
    "stores",
    "trading",
    "trader",
    "traders",
    "enterprise",
    "enterprises",
    "plc",
    "p.l.c",
    "p.l.c.",
    "llc",
    "ltd",
    "general",
    "import",
    "export",
    "importer",
    "exporter",
    "wholesaler",
    "distributor",
    "pharmacy",
    "electronics",
    "mini",
    "market",
    "boutique",
    "supermarket",
    "stationery",
    "cosmetics",
    "goods",
    "provisions",
    "bakery",
    "bookshop",
    "grocery",
    "depot",
    "commodity",
    "ንግድ",
    "ሱቅ",
    "ጅምላ",
    "ፋርማሲ",
    "ኤሌክትሮኒክስ",
    "ኃ/የተ/የግ/ማ",
    "የግል",
}

FIDEL_TO_LATIN = {
    # First names
    "አበበ": "abebe", "አልማዝ": "almaz", "ጭሎት": "chilot", "ዳዊት": "dawit",
    "ኤፍሬም": "ephrem", "ፍቅሩ": "fikru", "ግርማ": "girma", "ኃይሌ": "haile",
    "ካሳሁን": "kassahun", "ሙሉጌታ": "mulugeta", "ነፃነት": "netsanet",
    "ሰላማዊት": "selamawit", "ታደሰ": "tadesse", "ወርቁ": "worku",
    "ዮሐንስ": "yohannes", "ዘውዱ": "zewdu",
    # Last names / patronymics
    "በቀለ": "bekele", "አሰፋ": "assefa", "ከበደ": "kebede", "ጥላሁን": "tilahun",
    "መንግስቱ": "mengistu", "ተስፋዬ": "tesfaye", "አለሙ": "alemu", "ደሚሴ": "demissie",
    # Store types
    "ሱቅ": "shop", "ጅምላ": "wholesale", "ሚኒ": "mini", "ማርኬት": "market",
    "ፋርማሲ": "pharmacy", "ኤሌክትሮኒክስ": "electronics", "ንግድ": "trading",
}

ALIAS_MAP = {
    "ababa": "abebe", "abebech": "abebe", "abe": "abebe",
    "elmaz": "almaz", "chilote": "chilot", "david": "dawit", "davit": "dawit",
    "efrem": "ephrem", "ephraim": "ephrem", "fekru": "fikru", "fikre": "fikru",
    "germa": "girma", "grm": "girma", "hayle": "haile", "hailemariam": "haile",
    "kassa": "kassahun", "kassu": "kassahun", "mulu": "mulugeta", "mulu-geta": "mulugeta",
    "nesanet": "netsanet", "netsi": "netsanet", "selam": "selamawit",
    "taddese": "tadesse", "tad": "tadesse", "werku": "worku", "workneh": "worku",
    "john": "yohannes", "yohanes": "yohannes", "zewdie": "zewdu",
}


def normalize_phone(raw_phone: str | None) -> str | None:
    """
    Normalize Ethiopian phone numbers to national 9-digit format (e.g. '911234567').
    Handles:
      '+251 91 123 4567' -> '911234567'
      '0911234567'       -> '911234567'
      '0711234567'       -> '711234567'
      '251911234567'     -> '911234567'
    """
    if not raw_phone:
        return None

    digits = re.sub(r"\D", "", raw_phone)
    if digits.startswith("251") and len(digits) >= 12:
        digits = digits[3:]
    if digits.startswith("0") and len(digits) >= 10:
        digits = digits[1:]

    # Ethiopian mobile numbers are 9 digits starting with 9 or 7
    if len(digits) == 9 and digits[0] in ("9", "7"):
        return digits

    return digits if len(digits) >= 6 else None


def normalize_account_number(raw_account: str | None) -> str | None:
    """
    Normalize bank account numbers by removing spaces, hyphens, and leading zeros.
    """
    if not raw_account:
        return None

    cleaned = re.sub(r"[\s\-_]", "", str(raw_account)).strip()
    # Normalize alphanumeric accounts
    cleaned = cleaned.upper().lstrip("0")
    return cleaned if len(cleaned) >= 4 else None


@functools.lru_cache(maxsize=8192)
def normalize_ethiopian_name(name: str | None) -> str:
    """
    Normalize an Ethiopian personal or business name for robust comparison.
    1. Unicode NFKD normalization and lowercase.
    2. Strip common retail/business suffixes ('Shop', 'Traders', etc.).
    3. Normalize common Ethiopian transliteration variations:
       - Double consonants collapsed to single: 'bbe' -> 'be', 'ssa' -> 'sa', etc.
       - Vowel variants: 'ou' -> 'u', 'ee' -> 'e', 'aa' -> 'a'
       - Consonants: 'ph' -> 'f', 'kh' -> 'k', 'sh' -> 's', 'ch' -> 'c'
    """
    if not name:
        return ""

    # Unicode normalization
    s = unicodedata.normalize("NFKD", name).lower()

    # Transliterate Amharic / Ge'ez Fidel script to Latin
    for fid, lat in FIDEL_TO_LATIN.items():
        s = s.replace(fid, lat)

    # Remove non-alphanumeric except spaces
    s = re.sub(r"[^\w\s]", " ", s)

    # First strip business suffixes and map known aliases BEFORE character substitutions
    tokens = [ALIAS_MAP.get(t, t) for t in s.split()]
    tokens = [t for t in tokens if t not in BUSINESS_SUFFIXES and len(t) > 1]
    s = " ".join(tokens) if tokens else s

    # Phonetic and transliteration normalizations
    # Double consonants -> single
    s = re.sub(r"([bcdfghjklmnpqrstvwxyz])\1+", r"\1", s)
    s = s.replace("ou", "u")
    s = s.replace("ee", "e")
    s = s.replace("aa", "a")
    s = s.replace("ph", "f")
    s = s.replace("kh", "k")
    s = s.replace("sh", "s")
    s = s.replace("ch", "c")
    s = s.replace("ay", "ai")
    s = s.replace("ey", "ei")
    s = s.replace("ye", "ie")
    # Transliteration 6th order vowel e vs i in specific prefixes
    s = s.replace("telahun", "tilahun")
    s = s.replace("fekadu", "fikadu")
    s = s.replace("yohanis", "yonas")
    s = s.replace("hewan", "hiwot")

    final_tokens = [t for t in s.split() if t not in BUSINESS_SUFFIXES and len(t) > 1]
    return " ".join(final_tokens) if final_tokens else s.strip()


@functools.lru_cache(maxsize=32768)
def levenshtein_distance(s1: str, s2: str) -> int:
    """Standard Levenshtein edit distance."""
    if s1 == s2:
        return 0
    if len(s1) < len(s2):
        s1, s2 = s2, s1
    if not s2:
        return len(s1)

    previous_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


def string_similarity(s1: str, s2: str) -> float:
    """
    Compute similarity ratio in range [0.0, 1.0].
    Combines Levenshtein ratio and token overlap for multi-word retailer names.
    """
    n1 = normalize_ethiopian_name(s1)
    n2 = normalize_ethiopian_name(s2)

    if not n1 or not n2:
        return 0.0
    if n1 == n2:
        return 1.0

    # Token overlap check (Jaccard on tokens)
    tokens1 = set(n1.split())
    tokens2 = set(n2.split())
    overlap = 0.0
    if tokens1 and tokens2:
        overlap = len(tokens1 & tokens2) / len(tokens1 | tokens2)
        if overlap == 1.0:
            return 1.0
        # If one token set is a complete subset of another (e.g. "Abebe Kebede" and "Abebe")
        if tokens1.issubset(tokens2) or tokens2.issubset(tokens1):
            if min(len(tokens1), len(tokens2)) >= 1:
                return 0.96

    # Levenshtein ratio
    max_len = max(len(n1), len(n2))
    min_dist = abs(len(n1) - len(n2))
    if max_len > 0 and (1.0 - (min_dist / max_len)) <= overlap:
        return overlap

    dist = levenshtein_distance(n1, n2)
    lev_ratio = 1.0 - (dist / max_len)

    return max(lev_ratio, overlap)


# Common banking / transfer narrative noise words
NARRATIVE_NOISE_WORDS = {
    "transfer",
    "from",
    "to",
    "by",
    "payment",
    "deposit",
    "settlement",
    "cbe",
    "birr",
    "telebirr",
    "cash",
    "in",
    "out",
    "ref",
    "narration",
    "mobile",
    "wallet",
    "txn",
    "ft",
    "account",
    "acc",
}


@functools.lru_cache(maxsize=8192)
def clean_narrative_text(text: str | None) -> str:
    """Strip generic bank narrative words (e.g. 'Transfer from', 'Payment for')."""
    if not text:
        return ""
    norm = normalize_ethiopian_name(text)
    tokens = [t for t in norm.split() if t not in NARRATIVE_NOISE_WORDS and len(t) > 1]
    return " ".join(tokens) if tokens else norm


@functools.lru_cache(maxsize=16384)
def match_name_in_text(entity_name: str, narrative: str) -> float:
    """
    Evaluate if an entity name appears in a bank transaction narrative.
    Returns similarity in range [0.0, 1.0].
    """
    norm_entity = normalize_ethiopian_name(entity_name)
    norm_narrative = normalize_ethiopian_name(narrative)
    clean_narrative = clean_narrative_text(narrative)

    if not norm_entity:
        return 0.0

    # Direct containment
    if norm_entity in norm_narrative or norm_entity in clean_narrative:
        return 1.0

    entity_tokens = set(norm_entity.split())
    narrative_tokens = set(norm_narrative.split())
    clean_tokens = set(clean_narrative.split())

    # All entity tokens present in narrative
    if entity_tokens and entity_tokens.issubset(narrative_tokens | clean_tokens):
        return 0.98

    # Word-level matching with prefix & truncation tolerance for Ethiopian given/father names
    all_tokens = clean_tokens if clean_tokens else narrative_tokens
    if len(entity_tokens) >= 2 and all_tokens:
        matched_count = 0
        for et in entity_tokens:
            # Token exact match, or prefix match (e.g. Mulu for Mulugeta, Mese for Meseret)
            if any(
                et == nt
                or (len(et) >= 4 and nt.startswith(et[:4]))
                or (len(nt) >= 4 and et.startswith(nt[:4]))
                or (abs(len(et) - len(nt)) <= 1 and levenshtein_distance(et, nt) <= 1)
                for nt in all_tokens
            ):
                matched_count += 1
        if matched_count == len(entity_tokens):
            return 0.97
        if matched_count >= len(entity_tokens) - 1 and len(entity_tokens) >= 3:
            return 0.92

    # Partial token overlap with clean narrative
    if entity_tokens and all_tokens:
        overlap = len(entity_tokens & all_tokens) / len(entity_tokens)
        if overlap >= 0.8:
            return 0.95
        if overlap >= 0.5:
            return 0.85

    # Fallback to string similarity on clean narrative
    return string_similarity(norm_entity, clean_narrative)
