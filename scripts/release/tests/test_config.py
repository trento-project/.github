# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

import pytest

from common import Version, bump_branch, load_config, load_manifest


def test_components_are_read(components_file):
    config = load_config(components_file)
    assert sorted(config.components) == ["alpha", "beta", "chart"]


def test_repo_defaults_to_the_component_name(components_file):
    config = load_config(components_file)
    assert config.component("alpha").repo == "alpha"
    assert config.component("beta").repo == "beta-repo"


def test_tiers_are_sorted_and_deduplicated(components_file):
    assert load_config(components_file).tiers() == [1, 2]


def test_obs_version_file_is_interpolated(components_file):
    config = load_config(components_file)
    assert config.component("alpha").obs_version_file == "trento-alpha.spec"


def test_a_component_with_no_obs_package_has_no_spec_file(components_file, tmp_path):
    text = components_file.read_text(encoding="utf-8")
    components_file.write_text(text.replace("    obs_package: trento-alpha\n", ""), "utf-8")
    assert load_config(components_file).component("alpha").obs_version_file is None


def test_scc_packages_default_to_the_obs_package(components_file):
    assert load_config(components_file).component("alpha").scc_packages == ["trento-alpha"]


def test_a_component_with_no_bumps_gets_the_default_version_file(components_file):
    bumps = load_config(components_file).component("alpha").bumps
    assert [(bump.path, bump.kind) for bump in bumps] == [("VERSION", "plain")]


def test_declared_bumps_are_read_in_order(components_file):
    bumps = load_config(components_file).component("chart").bumps
    assert [(bump.path, bump.kind, bump.key, bump.source) for bump in bumps] == [
        ("VERSION", "plain", None, None),
        ("Chart.yaml", "yaml", "version", None),
        ("values.yaml", "yaml", "alpha.image.tag", "alpha"),
    ]


def test_an_unknown_component_names_the_known_ones(components_file):
    config = load_config(components_file)
    with pytest.raises(KeyError, match="alpha, beta, chart"):
        config.component("delta")


@pytest.mark.parametrize(
    "variable,attribute,value",
    [
        ("TRENTO_GITHUB_ORG", "github_org", "someone"),
        ("TRENTO_GHCR_NAMESPACE", "ghcr_namespace", "someone"),
    ],
)
def test_an_environment_override_wins(components_file, monkeypatch, variable, attribute, value):
    monkeypatch.setenv(variable, value)
    assert getattr(load_config(components_file), attribute) == value


@pytest.mark.parametrize(
    "variable,key,value",
    [
        ("TRENTO_OBS_PROJECT_STABLE", "stable", "home:someone:trento"),
        ("TRENTO_OBS_PROJECT_ROLLING", "rolling", "home:someone:trento:factory"),
    ],
)
def test_an_obs_project_override_wins(components_file, monkeypatch, variable, key, value):
    monkeypatch.setenv(variable, value)
    assert load_config(components_file).obs_projects[key] == value


def test_an_empty_override_falls_back_to_the_file(components_file, monkeypatch):
    # A workflow renders ${{ vars.X }} as an empty string when the
    # variable is not set, which must not blank the project name.
    monkeypatch.setenv("TRENTO_OBS_PROJECT_STABLE", "")
    assert load_config(components_file).obs_projects["stable"] == "test:stable"


def test_trailing_slashes_are_stripped_from_every_api(components_file):
    text = components_file.read_text(encoding="utf-8")
    components_file.write_text(text.replace("https://scc.invalid", "https://scc.invalid/"), "utf-8")
    assert load_config(components_file).scc_api == "https://scc.invalid"


def test_an_empty_manifest_says_so(tmp_path):
    path = tmp_path / "manifest.yaml"
    path.write_text("components: {}\n", encoding="utf-8")
    assert load_manifest(path).is_empty


def test_a_manifest_entry_is_read(tmp_path):
    path = tmp_path / "manifest.yaml"
    path.write_text("components:\n  alpha:\n    version: 1.2.3\n", encoding="utf-8")
    manifest = load_manifest(path)
    assert not manifest.is_empty
    assert str(manifest.entries["alpha"].version) == "1.2.3"


def test_the_bump_branch_avoids_a_release_slash_prefix():
    # git cannot hold both a `release` branch and a `release/` ref
    # directory, and every component repository has the former.
    branch = bump_branch(Version.parse("1.2.3"))
    assert branch.startswith("release-bump")
    assert not branch.startswith("release/")
    assert "1.2.3" in branch
