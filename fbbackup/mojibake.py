"""Repair Facebook's UTF-8-as-latin-1 mojibake.

Facebook's JSON export encodes every non-ASCII string by taking the real
UTF-8 *bytes* and emitting each byte as a separate U+0000–U+00FF code point.
So Korean "별" (UTF-8 EB B3 84) arrives as "ë³\x84", and 🔥 (F0 9F 94 A5)
arrives as "ð\x9f\x94¥". The repair is the inverse: re-encode the string as
latin-1 to recover the original bytes, then decode them as UTF-8.

This is applied uniformly — the guard makes it safe for already-clean text:
a string containing a genuine code point > U+00FF (a real emoji FB stored
correctly, etc.) cannot be latin-1-encoded, so it's returned untouched. A
byte sequence that isn't valid UTF-8 also falls through unchanged.
"""
from __future__ import annotations


def fix(s: str | None) -> str | None:
    """Return ``s`` with Facebook's double-encoding undone, or ``s`` as-is
    when it isn't mojibake (already clean, or not recoverable)."""
    if not s:
        return s
    try:
        repaired = s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s
    return repaired
