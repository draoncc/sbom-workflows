import pytest

from image_map import resolve_version

VERSIONS = ["1.0.0", "1.2.0", "1.2.5", "1.10.1", "2.0.0-rc.1", "2.0.0", "2.3.1", "3.0.0-beta.2", "v4.0.0"]


@pytest.mark.parametrize(
    ("constraint", "expected"),
    [
        ("1.2.0", "1.2.0"),
        ("1.x", "1.10.1"),
        ("1.2.*", "1.2.5"),
        ("*", "v4.0.0"),
        ("", "v4.0.0"),
        ("~1.2", "1.2.5"),
        ("~1.2.1", "1.2.5"),
        ("^1.2", "1.10.1"),
        (">=1.2.0 <2.0.0", "1.10.1"),
        (">=1.2.0, <2.0.0", "1.10.1"),
        ("<1.2.0 || 2.x", "2.3.1"),
        ("!=2.3.1, 2.x", "2.0.0"),
        (">=3.0.0-0, <4.0.0", "3.0.0-beta.2"),
        ("2.0.0-rc.1", "2.0.0-rc.1"),
        ("4.0.0", "v4.0.0"),
    ],
)
def test_picks_highest_matching_version(constraint, expected):
    assert resolve_version(constraint, VERSIONS) == expected


def test_returns_none_when_nothing_matches():
    assert resolve_version(">=5.0.0", VERSIONS) is None


def test_skips_versions_that_are_not_semver():
    assert resolve_version("*", ["latest", "1.0.0", "main"]) == "1.0.0"
