"""English-only labels: every string a package carries is printable ASCII.

The release is English only and that is not an option: there is no language switch, so it
cannot be forgotten. pick_name() takes the first usable of name:en, int_name and the local
`name`; whatever comes out goes through ascii_label(), which transliterates Russian
(BGN/PCGN without diacritics: e -> ye at a word start or after a vowel, yo counted as e,
hard and soft signs dropped), strips accents from other scripts' Latin and drops the rest.
encode.label_bytes() refuses anything else, so a label that slips past this module fails
the package build instead of reaching the watch.
"""
import re
import unicodedata

MAX_LABEL_BYTES = 32

_RU = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh",
    "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "і": "i", "ї": "yi", "є": "ye", "ґ": "g", "ў": "u",  # Ukrainian, Belarusian
}
# after these (or at a word start) е/ё get the y
_Y_AFTER = set("аеёиоуыэюяйъь")
# typography that has an ASCII twin; other punctuation (guillemets, ...) is dropped
_PUNCT = {"–": "-", "—": "-", "−": "-", "‘": "'", "’": "'", "ʼ": "'"}
# Latin letters that do not decompose under NFKD
_LATIN = {"ß": "ss", "æ": "ae", "Æ": "Ae", "ø": "o", "Ø": "O", "đ": "d", "Đ": "D", "ł": "l", "Ł": "L"}


def transliterate(s):
    """Cyrillic -> Latin, keeping case; every other character passes through unchanged."""
    out = []
    for i, c in enumerate(s):
        low = c.lower()
        if low not in _RU:
            out.append(c)
            continue
        lat = _RU[low]
        prev = s[i - 1] if i else ""
        if low in "её" and (not prev.isalpha() or prev.lower() in _Y_AFTER):
            lat = "y" + lat
        if c.isupper() and lat:
            # a run of capitals stays capitals (ЮКСПОР -> YUKSPOR), a single one is Xx (Щука -> Shchuka)
            nxt = s[i + 1] if i + 1 < len(s) else ""
            lat = lat.upper() if prev.isupper() or nxt.isupper() else lat.capitalize()
        out.append(lat)
    return "".join(out)


def _unaccent(c):
    """Base characters of c: ä -> a, Kola Sami ӣ -> и, № -> No."""
    return "".join(x for x in unicodedata.normalize("NFKD", c) if not unicodedata.combining(x))


def clip(s, max_bytes=MAX_LABEL_BYTES):
    """Shorten an ASCII label to max_bytes at a word boundary. A single word longer than the
    budget, or a boundary in its first half, is cut hard: half a label beats none."""
    if len(s) <= max_bytes:
        return s
    head = s[:max_bytes + 1]  # + 1: a word ending exactly at the limit survives whole
    cut = max(head.rfind(" "), head.rfind("-"))
    head = s[:cut] if cut >= max_bytes // 2 else s[:max_bytes]
    return head.rstrip(" -.,:;'/")


def ascii_label(s, max_bytes=MAX_LABEL_BYTES):
    """Any text -> printable ASCII label of at most max_bytes bytes ("" if nothing survives)."""
    if not s:
        return ""
    s = unicodedata.normalize("NFC", s)  # и + combining breve -> й, so the table sees it
    s = "".join(_PUNCT.get(c) or _LATIN.get(c) or (c if c.lower() in _RU else _unaccent(c)) for c in s)
    s = transliterate(s)
    s = "".join(c for c in s if " " <= c <= "~" or c.isspace())
    return clip(re.sub(r"\s+", " ", s).strip(), max_bytes)


def is_ascii_label(s):
    return s.isascii() and s.isprintable()


def pick_name(tags, max_bytes=MAX_LABEL_BYTES):
    """Map label of an OSM object: name:en -> int_name -> transliterated `name`."""
    for key in ("name:en", "int_name", "name"):
        n = ascii_label(tags.get(key), max_bytes)
        if n:
            return n
    return None
