# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

import pytest

from common import MissingKey, get_dotted, load_yaml_str, replace_yaml_value


def test_get_dotted_walks_nested_mappings():
    data = load_yaml_str("image:\n  tag: 1.2.3\n")
    assert get_dotted(data, "image.tag") == "1.2.3"


@pytest.mark.parametrize("key", ["image.missing", "missing.tag", "image.tag.deeper"])
def test_get_dotted_raises_on_a_key_that_is_not_there(key):
    data = load_yaml_str("image:\n  tag: 1.2.3\n")
    with pytest.raises(MissingKey):
        get_dotted(data, key)


def test_only_the_target_line_changes():
    original = "# a comment\nimage:\n  tag: 1.2.3\n  pullPolicy: Always\n\nother: value\n"
    updated = replace_yaml_value(original, "image.tag", "1.3.0")
    assert updated == "# a comment\nimage:\n  tag: 1.3.0\n  pullPolicy: Always\n\nother: value\n"


def test_an_explicit_null_elsewhere_survives():
    # The reason this function exists instead of a ruamel round-trip.
    # Dumping the parsed document renders `alerting: null` as
    # `alerting:`, so bumping one image tag in trento-web/values.yaml
    # would silently rewrite seven unrelated keys.
    original = (
        "alerting:\n"
        "  smtpServer: null\n"
        "  smtpPort: null\n"
        "  senderEmail: null\n"
        "image:\n"
        "  tag: 1.2.3\n"
    )
    updated = replace_yaml_value(original, "image.tag", "1.3.0")
    assert "smtpServer: null" in updated
    assert "smtpPort: null" in updated
    assert "senderEmail: null" in updated
    assert updated.count("null") == 3


@pytest.mark.parametrize(
    "line,expected",
    [
        ("  tag: 1.2.3\n", "  tag: 1.3.0\n"),
        ('  tag: "1.2.3"\n', '  tag: "1.3.0"\n'),
        ("  tag: '1.2.3'\n", "  tag: '1.3.0'\n"),
    ],
)
def test_quoting_is_preserved(line, expected):
    original = f"image:\n{line}"
    assert replace_yaml_value(original, "image.tag", "1.3.0") == f"image:\n{expected}"


def test_a_trailing_comment_survives():
    original = "image:\n  tag: 1.2.3  # set by the release\n"
    updated = replace_yaml_value(original, "image.tag", "1.3.0")
    assert updated == "image:\n  tag: 1.3.0  # set by the release\n"


def test_a_missing_key_raises():
    with pytest.raises(MissingKey):
        replace_yaml_value("image:\n  tag: 1.2.3\n", "image.digest", "sha256:x")


def test_a_non_scalar_value_raises():
    original = "image:\n  tags:\n    - 1.2.3\n    - latest\n"
    with pytest.raises(MissingKey):
        replace_yaml_value(original, "image.tags", "1.3.0")


def test_the_file_keeps_its_final_newline_state():
    without = "image:\n  tag: 1.2.3"
    assert replace_yaml_value(without, "image.tag", "1.3.0") == "image:\n  tag: 1.3.0"


def test_a_flow_sequence_raises_without_touching_the_document():
    original = "tags: [a, b]\nafter: value\n"
    with pytest.raises(MissingKey):
        replace_yaml_value(original, "tags", "1.3.0")
    assert original == "tags: [a, b]\nafter: value\n"


def test_a_flow_mapping_raises_without_touching_the_document():
    original = "spec: {tag: 1.2.3}\nafter: value\n"
    with pytest.raises(MissingKey):
        replace_yaml_value(original, "spec", "1.3.0")
    assert original == "spec: {tag: 1.2.3}\nafter: value\n"


def test_an_implicit_null_with_a_same_line_comment_raises():
    # The mark for this null sits on the key's own line, in the
    # whitespace before the comment - the case the other implicit-null
    # guard (a mark on a different line) does not cover.
    original = "tag:  # pinned later\n"
    with pytest.raises(MissingKey):
        replace_yaml_value(original, "tag", "1.3.0")
