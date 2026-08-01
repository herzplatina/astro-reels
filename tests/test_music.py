#!/usr/bin/env python3
"""Tests for vocal screening of the music library.

The rule is absolute — background music must be pure instrumental — so these
lean towards catching a voice at the cost of the odd false positive. A wrongly
quarantined track is one `--restore` away; a reel that starts singing over the
text is already published.

    python3 -m pytest tests/ -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from audit_music import screen  # noqa: E402


def is_vocal(title="untitled", tags=None, genres=None) -> bool:
    flagged, _ = screen(title, tags or [], genres or [])
    return flagged


# ------------------------------------------------------------ obvious vocals


@pytest.mark.parametrize(
    "tag",
    [
        "vocal",
        "voice",
        "choir",
        "chant",
        "acapella",
        "lyrics",
        "speech",
        "spoken",
        "singer",
        "opera",
        "humming",
        "whisper",
        "narration",
    ],
)
def test_unambiguous_vocal_tags_are_caught(tag):
    assert is_vocal(tags=[tag])


@pytest.mark.parametrize("tag", ["chat", "chatter", "laughter", "applause", "crowd"])
def test_human_voice_without_singing_is_still_caught(tag):
    """The rule is no voices, not merely no lyrics."""
    assert is_vocal(tags=[tag])


def test_vocal_terms_in_the_title_are_caught():
    assert is_vocal(title="Ethereal Voices.mp3")


# --------------------------------------------------------------- normalising


def test_plural_tags_are_caught():
    """The real miss this was written for: a tag reading 'Peoples'."""
    assert is_vocal(tags=["Peoples", "Cars", "Street"])


def test_gerunds_are_caught():
    assert is_vocal(tags=["Talking"])


def test_tag_casing_is_irrelevant():
    assert is_vocal(tags=["VOCAL"])
    assert is_vocal(tags=["Vocal"])


def test_digits_in_filename_tokens_do_not_hide_a_term():
    assert is_vocal(title="chant23.wav", tags=[])


def test_genres_are_screened_too():
    assert is_vocal(genres=["Gospel"])


# ---------------------------------------------- instruments that sound vocal


def test_singing_bowls_survive():
    """A singing bowl is an instrument. Flagging these would gut the library."""
    assert not is_vocal(title="Tibetan Singing Bowl", tags=["bowl", "meditation"])
    assert not is_vocal(title="singing bowl sing.wav", tags=["singing bowl"])


def test_birdsong_survives():
    assert not is_vocal(title="Birds In Spring", tags=["birdsong", "nature"])


def test_song_alongside_an_instrument_tag_survives():
    """'song' is used loosely; a piano tag explains it."""
    assert not is_vocal(title="mellow_chill_song23.wav", tags=["piano", "ambient"])


def test_bare_song_with_no_instrument_context_is_flagged():
    assert is_vocal(title="a lovely song", tags=["pop"])


# ------------------------------------------------------- ordinary instrumentals


@pytest.mark.parametrize(
    "tags",
    [
        ["ambient", "drone", "pad", "meditation"],
        ["rain", "thunder", "nature"],
        ["sitar", "india", "string"],
        ["piano", "calm", "slow"],
        ["ocean", "waves", "beach"],
    ],
)
def test_instrumental_libraries_are_left_alone(tags):
    assert not is_vocal(tags=tags)


def test_empty_metadata_is_not_flagged():
    """No evidence of a voice is not evidence of a voice."""
    assert not is_vocal()


# --------------------------------------------------------------- diagnostics


def test_the_matched_terms_are_reported():
    flagged, reasons = screen("untitled", ["Peoples", "Chats"], [])
    assert flagged
    assert set(reasons) == {"people", "chat"}


# ------------------------------------------- regressions found by the review


@pytest.mark.parametrize(
    "tag",
    [
        "female vocal",
        "male voice",
        "mixed choir",
        "spoken word",
        "human voice",
        "choir singing",
        "vocal jazz",
        "a cappella",
    ],
)
def test_multi_word_vocal_tags_are_caught(tag):
    """Tags were joined character-wise, gluing 'female vocal' into one token
    that could never match. Every one of these passed as instrumental."""
    assert is_vocal(tags=[tag])


def test_multi_word_genres_are_caught():
    assert is_vocal(genres=["Spoken Word"])


def test_punctuated_tags_are_split():
    assert is_vocal(tags=["female-vocal"])
    assert is_vocal(tags=["voice/choir"])


def test_multi_word_instrument_tags_still_survive():
    """The fix must not start flagging instruments."""
    assert not is_vocal(
        title="Tibetan Singing Bowl", tags=["singing bowl", "meditation"]
    )
    assert not is_vocal(tags=["grand piano", "soft ambient"])
