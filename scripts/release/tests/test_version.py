# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

import pytest

from common import Version, bump_kind_between


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1.2.3", (1, 2, 3, None, None)),
        ("  1.2.3  ", (1, 2, 3, None, None)),
        ("1.2.3-rc1", (1, 2, 3, "rc1", None)),
        ("1.2.3+build.5", (1, 2, 3, None, "build.5")),
        ("1.2.3-rc1+build.5", (1, 2, 3, "rc1", "build.5")),
    ],
)
def test_parse_accepts_semver(text, expected):
    version = Version.parse(text)
    assert (version.major, version.minor, version.patch, version.pre, version.build) == expected


@pytest.mark.parametrize("text", ["v1.2.3", "1.2", "1.2.3.4", "", "main", "1.2.x"])
def test_parse_rejects_everything_else(text):
    # A leading `v` is rejected on purpose: VERSION files hold a bare
    # version, and a tag name is normalised before it gets here.
    with pytest.raises(ValueError):
        Version.parse(text)


def test_parse_or_none_swallows_the_error():
    assert Version.parse_or_none("nonsense") is None
    assert Version.parse_or_none(None) is None
    assert Version.parse_or_none("") is None
    assert Version.parse_or_none("1.2.3") == Version.parse("1.2.3")


def test_a_release_outranks_its_prerelease():
    assert Version.parse("1.2.3-rc1") < Version.parse("1.2.3")
    assert Version.parse("1.2.3") > Version.parse("1.2.3-rc1")


def test_build_metadata_is_ignored_for_ordering():
    assert not Version.parse("1.2.3+a") < Version.parse("1.2.3+b")
    assert not Version.parse("1.2.3+b") < Version.parse("1.2.3+a")


def test_ordering_is_numeric_not_lexical():
    assert Version.parse("1.9.0") < Version.parse("1.10.0")
    assert Version.parse("2.0.0") > Version.parse("1.99.99")


@pytest.mark.parametrize(
    "current,kind,expected",
    [
        ("1.2.3", "major", "2.0.0"),
        ("1.2.3", "minor", "1.3.0"),
        ("1.2.3", "patch", "1.2.4"),
    ],
)
def test_bump(current, kind, expected):
    assert str(Version.parse(current).bump(kind)) == expected


def test_bump_rejects_an_unknown_kind():
    with pytest.raises(ValueError, match="unknown bump kind"):
        Version.parse("1.2.3").bump("enormous")


def test_bumping_a_prerelease_skips_the_release_it_precedes():
    # Pinned, not endorsed. Semver puts 1.2.3 immediately after
    # 1.2.3-rc1, so promoting the candidate would arguably give 1.2.3.
    # No Trento VERSION file holds a prerelease today, so this records
    # the current behaviour; changing it should be a visible failure
    # rather than a silent difference.
    assert str(Version.parse("1.2.3-rc1").bump("patch")) == "1.2.4"


@pytest.mark.parametrize(
    "old,new,expected",
    [
        ("1.2.3", "2.0.0", "major"),
        ("1.2.3", "1.3.0", "minor"),
        ("1.2.3", "1.2.4", "patch"),
        ("1.2.3", "2.1.0", "major"),
    ],
)
def test_bump_kind_between(old, new, expected):
    assert bump_kind_between(Version.parse(old), Version.parse(new)) == expected
