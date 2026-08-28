"""How titles are compared, which is where almost everything went wrong.

Every case here is one that actually happened. Two catalogues never spell a
record the same way, and each disagreement had the same shape: two names for
one album read as two albums, so a record already on the shelf was listed as
missing and offered for download beside its own copy.

The one exception is deliberate. `Demo` and `Demos` are held apart on purpose:
folding differences is only safe while the difference is spelling, and a rule
loose enough to join those would join records that really are different.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bridge import is_studio, norm_title, owned_match, owns_title, split_parts  # noqa: E402


def owns(have: str, catalogue: str) -> bool:
    """Does a library holding `have` already have `catalogue`?"""
    return owns_title({norm_title(have)}, catalogue)


SAME = [
    # Abbreviations. The library writes "Mr Patate"; MusicBrainz has "M. Patate".
    ("Mr Patate", "M. Patate"),
    ("St. Anger", "Saint Anger"),
    ("Vol. 2", "Volume 2"),
    ("Pt. 1", "Part 1"),
    # The symbol against the word. Deleting it left "rock roll" against
    # "rock and roll".
    ("Rock & Roll", "Rock and Roll"),
    # Accents differ between catalogues: Discogs has Xibalbá, MusicBrainz
    # Xibalba. Folded rather than stripped — stripping made "xibalb a".
    ("Xibalbá", "Xibalba"),
    # Case and punctuation.
    ("OBJECTIF : THUNES", "Objectif: Thunes"),
    # A bracketed group standing on its own is an edition note.
    ("Live in Paris (01-09-05)", "Live in Paris"),
    ("Abismo (Remastered)", "Abismo"),
    ("Revenge [Deluxe Edition]", "Revenge"),
    # One written inside a word is part of the title. Stripping it turned
    # "Pussy(De)Luxe" into "pussy luxe" and listed one album twice.
    ("Pussy(De)Luxe", "Pussy De Luxe"),
    # A split names two acts and no two catalogues order them the same way.
    ("Mizar vs Spasm", "Spasm / Mizar"),
    ("Spasm / Gutalax", "Gutalax vs Spasm"),
]

DIFFERENT = [
    # Not a spelling difference. A rule loose enough to join these would join
    # records that really are different.
    ("Demo", "Demos"),
    ("Spasm / Mizar", "Spasm / Gutalax"),
    ("Abismo", "Abismo II"),
    # The greedy prefix rule ran the other way once: MusicBrainz files Ultra
    # Vomit's 1999 demo as "Ultra Vomit", which is a prefix of their 2024 album.
    # Holding only the demo must not satisfy the album.
    ("Ultra Vomit", "Ultra Vomit et le pouvoir de la puissance"),
]


@pytest.mark.parametrize("left,right", SAME)
def test_the_same_record_spelled_two_ways(left, right):
    assert norm_title(left) == norm_title(right) or owns(left, right), (
        f"{left!r} and {right!r} are one record and did not match"
    )


@pytest.mark.parametrize("left,right", DIFFERENT)
def test_records_that_only_look_alike(left, right):
    assert not owns(left, right), f"{left!r} was taken to satisfy {right!r}"


def test_an_edition_suffix_on_disk_satisfies_the_plain_catalogue_title():
    # Owned copies carry what the pressing added. The prefix rule exists for
    # this and only this direction.
    owned = {norm_title("Raping Uranus: The Lost Tracks Of Alien Fucker")}
    assert owns_title(owned, "Raping Uranus")
    assert not owns_title({norm_title("Raping Uranus")},
                          "Raping Uranus: The Lost Tracks Of Alien Fucker")


def test_owned_match_reports_which_copy_matched():
    owned = {norm_title("Mr Patate"), norm_title("Objectif : Thunes")}
    assert owned_match(owned, "M. Patate") == norm_title("Mr Patate")
    assert owned_match(owned, "Nothing Like It") is None


def test_owns_title_and_owned_match_never_disagree():
    # owns_title is written in terms of owned_match so the two cannot drift.
    # When they did, an artist whose only shared record carried an edition
    # suffix read as a different band entirely.
    owned = {norm_title(t) for t in ("Mr Patate", "Abismo (Remastered)",
                                     "Mizar vs Spasm")}
    for title in ("M. Patate", "Abismo", "Spasm / Mizar", "Something Else"):
        assert owns_title(owned, title) == (owned_match(owned, title) is not None)


def test_only_titles_naming_more_than_one_act_are_compared_unordered():
    # Matching without regard to order is looser than matching by string, and
    # is safe only where the order carries no meaning.
    assert split_parts(norm_title("Spasm / Mizar")) is not None
    assert split_parts(norm_title("Objectif : Thunes")) is None
    assert split_parts(norm_title("Abismo")) is None


def test_an_empty_library_owns_nothing():
    assert not owns_title(set(), "Anything At All")
    assert owned_match(set(), "Anything At All") is None


STUDIO = [
    ({"albumType": "Album", "secondaryTypes": []}, True),
    ({"albumType": "Album", "secondaryTypes": None}, True),
    ({"albumType": "Album", "secondaryTypes": ["Live"]}, False),
    ({"albumType": "Album", "secondaryTypes": ["Compilation"]}, False),
    ({"albumType": "Album", "secondaryTypes": ["Demo"]}, False),
    ({"albumType": "Single", "secondaryTypes": []}, False),
    ({"albumType": "EP", "secondaryTypes": []}, False),
    ({"albumType": "Other", "secondaryTypes": ["Demo"]}, False),
    ({}, False),
]


@pytest.mark.parametrize("album,expected", STUDIO)
def test_what_counts_as_a_gap(album, expected):
    # Everything is catalogued so an owned copy can carry a badge. Only a studio
    # album counts as missing — listing singles as gaps buried the four records
    # that really were absent under forty that never had been.
    assert is_studio(album) is expected
