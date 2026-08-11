<!--
SPDX-FileCopyrightText: SUSE LLC
SPDX-License-Identifier: Apache-2.0
-->

# Release Tooling Tests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `scripts/release/` a test suite — pytest for the rules, bats against a stub HTTP server for the cascade — so the release automation can be changed without running a release to find out what broke.

**Architecture:** Two layers. Unit tests call functions in-process with synthetic input and assert on return values, raised exceptions and `GitHub.performed`. End-to-end tests run each CLI as a subprocess against a local server replaying recorded API responses, asserting exit code and stdout against golden files. Three small production changes make the second layer possible without any test-awareness in the scripts.

**Tech Stack:** Python 3.11+, pytest, ruamel.yaml (already a dependency), bats-core 1.13.0, stdlib `http.server`.

## Global Constraints

- Design document: `docs/specs/2026-08-11-release-tooling-tests.md`. Read it before starting.
- No commit message or pull request body may mention Claude, AI assistance, or carry a `Co-Authored-By` trailer for one.
- Work in `/Volumes/workspace/trento-dot-github`, the `nelsonkopliku/trento-dot-github` fork. Never push to `trento-project`.
- Do not touch `/Volumes/workspace/trento-github`.
- Python 3.11 minimum — the code uses `X | None` syntax at runtime.
- `ruamel.yaml>=0.18` only. Do not add `pyyaml`; the repo deliberately uses ruamel for round-trip fidelity.
- Pin every GitHub Action by commit SHA with a `# vX.Y.Z` comment, matching `.github/workflows/release-table.yaml`.
- `BATS_VERSION: "1.13.0"`, matching `trento-project/helm-charts`.
- Runners are `ubuntu-24.04`.
- No test may reach the real network.
- No coverage threshold.
- Scope is the public `.github` repo only. `trento-release` is out of scope.

---

### Task 1: Test scaffolding and version algebra

Folds in all project configuration, because nothing can be run until pytest is wired up and there is no point reviewing configuration without a test that exercises it.

**Files:**
- Create: `pyproject.toml`
- Create: `scripts/release/requirements-dev.txt`
- Create: `scripts/release/tests/__init__.py` (empty)
- Create: `scripts/release/tests/conftest.py`
- Create: `scripts/release/tests/test_version.py`

**Interfaces:**
- Consumes: `common.Version`, `common.bump_kind_between` (existing).
- Produces: `tests/conftest.py` exporting the `components_file` fixture (a `pathlib.Path` to a temporary three-component `components.yaml`) and the `FakeGitHub` class. Tasks 3, 6 and 7 import both.

- [ ] **Step 1: Create the dependency file**

`scripts/release/requirements-dev.txt`:

```
-r requirements.txt
pytest>=8.0
```

- [ ] **Step 2: Create the project configuration**

`pyproject.toml` at the repository root:

```toml
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

# The release scripts are a flat directory of executables, not an
# installable package. pythonpath is what lets the tests import them
# without a src layout or a sys.path dance in every file.
[tool.pytest.ini_options]
pythonpath = ["scripts/release"]
testpaths = ["scripts/release/tests"]
addopts = "-q --strict-markers"

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]
```

- [ ] **Step 3: Create the shared fixtures**

`scripts/release/tests/conftest.py`:

```python
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

"""Fixtures shared by the unit tests.

The configuration below is deliberately not a copy of the real
components.yaml. Three components are enough to exercise tiers,
dependencies and every bump kind, and a small fixture makes a failing
assertion readable.
"""

from pathlib import Path

import pytest

from common import GitHub

COMPONENTS = """\
sources:
  github:
    api: https://github.invalid
    org: testorg
  obs:
    api: https://obs.invalid
    projects:
      stable: test:stable
      rolling: test:rolling
  scc:
    api: https://scc.invalid
    product_pattern: '^SLES_SAP/'
    architectures: [x86_64]
    max_products_per_major: 2
  ghcr:
    registry: ghcr.invalid
    namespace: testns

release_branches: [main, release]

defaults:
  version_file: VERSION
  tier: 1
  obs_version_file: '{obs_package}.spec'

components:
  alpha:
    obs_package: trento-alpha
    ghcr_image: trento-alpha
  beta:
    repo: beta-repo
    obs_package: trento-beta
    ghcr_image: trento-beta
  chart:
    tier: 2
    depends_on: [alpha, beta]
    bumps:
      - path: VERSION
      - path: Chart.yaml
        kind: yaml
        key: version
      - path: values.yaml
        kind: yaml
        key: alpha.image.tag
        source: alpha
"""


@pytest.fixture
def components_file(tmp_path: Path) -> Path:
    path = tmp_path / "components.yaml"
    path.write_text(COMPONENTS, encoding="utf-8")
    return path


class FakeGitHub(GitHub):
    """A GitHub client that answers from dictionaries instead of HTTP.

    Subclassing rather than mocking keeps the dry-run machinery real, so
    `performed` behaves exactly as it does in production and a test can
    assert that nothing was written.
    """

    def __init__(self, *, releases=None, pulls=None, files=None):
        super().__init__("https://github.invalid", "testorg", dry_run=True, token="test")
        self._releases = releases or {}
        self._pulls = pulls or {}
        self._files = files or {}

    def release_by_tag(self, repo: str, tag: str):
        return {"tag_name": tag} if tag in self._releases.get(repo, ()) else None

    def latest_release(self, repo: str):
        tags = self._releases.get(repo, ())
        return {"tag_name": tags[-1]} if tags else None

    def pulls_for_head(self, repo: str, head_branch: str):
        return self._pulls.get((repo, head_branch), [])

    def file_text(self, repo: str, path: str, ref: str = "HEAD"):
        return self._files.get((repo, path))
```

- [ ] **Step 4: Write the failing tests**

`scripts/release/tests/test_version.py`:

```python
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
```

- [ ] **Step 5: Run the tests to verify they fail**

Run:

```bash
cd /Volumes/workspace/trento-dot-github
python3 -m venv .venv-dev
.venv-dev/bin/pip install --quiet -r scripts/release/requirements-dev.txt
.venv-dev/bin/pytest
```

Expected: collection succeeds and every test passes **except** none — this task tests existing code, so all should pass immediately. If any fail, the failure is a real bug in `common.py`; stop and report it rather than editing the test to match.

- [ ] **Step 6: Verify the harness itself is not lying**

Temporarily change `test_bump`'s `("1.2.3", "patch", "1.2.4")` case to expect `"1.2.5"`. Run `.venv-dev/bin/pytest -k test_bump`. Expected: FAIL. Revert the change and re-run. Expected: PASS.

This step exists because a suite that passes against code it does not actually import is the most common way a first test task goes wrong.

- [ ] **Step 7: Ignore the dev virtualenv**

Append to `.gitignore` (create it if absent):

```
.venv-dev/
__pycache__/
.pytest_cache/
```

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml .gitignore scripts/release/requirements-dev.txt scripts/release/tests/
git commit -m "Give the release scripts somewhere to put tests

pytest configured with pythonpath rather than a src layout, because
the scripts are a flat directory of executables and turning them into
a package would change how the workflows invoke them.

The first tests cover the semver algebra: parsing, that a release
outranks its prereleases, that build metadata does not affect
ordering, and that 1.10.0 sorts above 1.9.0."
```

---

### Task 2: YAML line surgery

**Files:**
- Create: `scripts/release/tests/test_yaml_edit.py`

**Interfaces:**
- Consumes: `common.replace_yaml_value`, `common.get_dotted`, `common.MissingKey` (existing).
- Produces: nothing other tasks import.

- [ ] **Step 1: Write the failing tests**

`scripts/release/tests/test_yaml_edit.py`:

```python
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
```

- [ ] **Step 2: Run the tests**

Run: `.venv-dev/bin/pytest scripts/release/tests/test_yaml_edit.py -v`

Expected: all pass. If `test_a_non_scalar_value_raises` or `test_a_trailing_comment_survives` fails, that is a genuine defect in `_SCALAR_RE` — report it with the actual output rather than adjusting the assertion.

- [ ] **Step 3: Commit**

```bash
git add scripts/release/tests/test_yaml_edit.py
git commit -m "Prove the YAML edit touches one line

replace_yaml_value uses ruamel only to find a key's line and column,
then edits that line as text. The docstring says why; nothing proved
it. The load-bearing case is a document with explicit nulls, which a
round-trip dump would rewrite to empty values."
```

---

### Task 3: Configuration loading

**Files:**
- Create: `scripts/release/tests/test_config.py`

**Interfaces:**
- Consumes: `conftest.components_file`, `common.load_config`, `common.load_manifest`, `common.bump_branch`.

- [ ] **Step 1: Write the failing tests**

`scripts/release/tests/test_config.py`:

```python
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
    path.write_text("versions: {}\n", encoding="utf-8")
    assert load_manifest(path).is_empty()


def test_a_manifest_entry_is_read(tmp_path):
    path = tmp_path / "manifest.yaml"
    path.write_text("versions:\n  alpha: 1.2.3\n", encoding="utf-8")
    manifest = load_manifest(path)
    assert not manifest.is_empty()
    assert str(manifest.entries["alpha"].version) == "1.2.3"


def test_the_bump_branch_avoids_a_release_slash_prefix():
    # git cannot hold both a `release` branch and a `release/` ref
    # directory, and every component repository has the former.
    branch = bump_branch(Version.parse("1.2.3"))
    assert branch.startswith("release-bump")
    assert not branch.startswith("release/")
    assert "1.2.3" in branch
```

- [ ] **Step 2: Run the tests**

Run: `.venv-dev/bin/pytest scripts/release/tests/test_config.py -v`

Expected: all pass. `test_a_manifest_entry_is_read` depends on the manifest schema; if `load_manifest` expects a different top-level key, read `common.py:447` and correct the fixture text, not the assertion about behaviour.

- [ ] **Step 3: Commit**

```bash
git add scripts/release/tests/test_config.py
git commit -m "Cover the configuration the whole cascade reads from

Including the case a fork depends on: an organisation variable that
is not set renders as an empty string in a workflow, which must fall
back to the file rather than blanking the project name."
```

---

### Task 4: Label resolution

**Files:**
- Create: `scripts/release/tests/test_resolver.py`

**Interfaces:**
- Consumes: `propose.Resolver` (existing).

- [ ] **Step 1: Write the failing tests**

`scripts/release/tests/test_resolver.py`:

```python
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

import pytest

from propose import Resolver

DRAFTER = """\
version-resolver:
  major:
    labels: [breaking]
  minor:
    labels: [enhancement, feature]
  patch:
    labels: [bug, chore]
  default: patch
exclude-labels: [backport-as-hotfix, released-as-hotfix, skip-changelog]
"""


@pytest.fixture
def resolver(tmp_path):
    path = tmp_path / "release_drafter_main.yaml"
    path.write_text(DRAFTER, encoding="utf-8")
    return Resolver.load(path)


@pytest.mark.parametrize(
    "labels,expected",
    [
        ({"breaking"}, "major"),
        ({"enhancement"}, "minor"),
        ({"bug"}, "patch"),
        ({"chore"}, "patch"),
    ],
)
def test_a_single_label_resolves_to_its_kind(resolver, labels, expected):
    assert resolver.kind_for(labels) == expected


def test_the_highest_rank_wins(resolver):
    assert resolver.kind_for({"bug", "enhancement"}) == "minor"
    assert resolver.kind_for({"bug", "enhancement", "breaking"}) == "major"


def test_an_unknown_label_falls_back_to_the_default(resolver):
    assert resolver.kind_for({"documentation"}) == "patch"


def test_no_labels_at_all_falls_back_to_the_default(resolver):
    assert resolver.kind_for(set()) == "patch"


def test_an_excluded_label_beats_everything(resolver):
    assert resolver.kind_for({"skip-changelog"}) is None
    assert resolver.kind_for({"breaking", "skip-changelog"}) is None


def test_a_missing_resolver_section_still_loads(tmp_path):
    path = tmp_path / "empty.yaml"
    path.write_text("name-template: 'v$RESOLVED_VERSION'\n", encoding="utf-8")
    resolver = Resolver.load(path)
    assert resolver.kind_for({"anything"}) == "patch"


def test_the_real_drafter_config_resolves_enhancement_as_minor():
    # Regression guard. `enhancement` is used on 1433 merged pull
    # requests and was missing from the minor list, so every release
    # resolved as a patch. If this fails, that bug is back.
    from common import REPO_ROOT

    resolver = Resolver.load(REPO_ROOT / ".github" / "release_drafter_main.yaml")
    assert resolver.kind_for({"enhancement"}) == "minor"


def test_the_real_drafter_config_excludes_backport_as_hotfix():
    # Regression guard. `released-as-hotfix` was excluded and
    # `backport-as-hotfix`, used 44 times, was not, so hotfixes were
    # listed twice in the release notes.
    from common import REPO_ROOT

    resolver = Resolver.load(REPO_ROOT / ".github" / "release_drafter_main.yaml")
    assert resolver.kind_for({"backport-as-hotfix"}) is None
```

- [ ] **Step 2: Confirm the real config path before running**

Run:

```bash
ls .github/release_drafter_main.yaml
```

Expected: the file exists. If it is at a different path, correct the two regression guards to point at it — these two tests are the whole reason this task exists and must run against the real file, not a fixture.

- [ ] **Step 3: Run the tests**

Run: `.venv-dev/bin/pytest scripts/release/tests/test_resolver.py -v`

Expected: all pass.

- [ ] **Step 4: Prove the regression guards actually guard**

Run:

```bash
cp .github/release_drafter_main.yaml /tmp/drafter-backup.yaml
python3 - <<'PY'
from pathlib import Path
path = Path(".github/release_drafter_main.yaml")
path.write_text(path.read_text(encoding="utf-8").replace("enhancement", "feature-request"), encoding="utf-8")
PY
.venv-dev/bin/pytest scripts/release/tests/test_resolver.py -k real_drafter -v
```

Expected: `test_the_real_drafter_config_resolves_enhancement_as_minor` FAILS.

Then restore and re-run:

```bash
cp /tmp/drafter-backup.yaml .github/release_drafter_main.yaml
.venv-dev/bin/pytest scripts/release/tests/test_resolver.py -k real_drafter -v
git diff --exit-code .github/release_drafter_main.yaml
```

Expected: PASS, and `git diff` reports no change.

- [ ] **Step 5: Commit**

```bash
git add scripts/release/tests/test_resolver.py
git commit -m "Guard the two label bugs against coming back

Both were live before this tooling existed. enhancement, used on 1433
pull requests, was missing from the resolver's minor list, so every
release resolved as a patch. backport-as-hotfix, used 44 times, was
missing from the exclusion list while released-as-hotfix was present,
so hotfixes appeared twice in the notes.

These two assertions read the real release_drafter_main.yaml rather
than a fixture, because a fixture would keep passing while the file
that matters drifted."
```

---

### Task 5: SCC product parsing

**Files:**
- Create: `scripts/release/tests/test_scc.py`

**Interfaces:**
- Consumes: `common.product_release`, `common.product_label`, `collect_state.select_products`, `conftest.components_file`.

- [ ] **Step 1: Write the failing tests**

`scripts/release/tests/test_scc.py`:

```python
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

import pytest

from collect_state import select_products
from common import load_config, product_label, product_release


@pytest.mark.parametrize(
    "identifier,expected",
    [
        ("SLES_SAP/15.7", (15, 7)),
        ("SLES_SAP/15.6", (15, 6)),
        ("SLES_SAP/16.0", (16, 0)),
        ("SLES_SAP/15", (15, 0)),
    ],
)
def test_the_release_comes_from_the_identifier(identifier, expected):
    # SCC's `version` field is a display string: SLES_SAP/15.7 reports
    # "15 SP7", which does not parse as a dotted version. The
    # identifier is the stable one.
    product = {"identifier": identifier, "version": "15 SP7"}
    assert product_release(product) == expected


@pytest.mark.parametrize(
    "product",
    [
        {"identifier": ""},
        {},
        {"identifier": "SLES_SAP"},
        {"identifier": "SLES_SAP/notanumber"},
    ],
)
def test_an_unparseable_identifier_gives_zero_rather_than_raising(product):
    assert product_release(product) == (0, 0)


def test_the_label_is_the_second_half_of_the_identifier():
    assert product_label({"identifier": "SLES_SAP/15.7"}) == "15.7"


def test_the_label_falls_back_to_the_whole_identifier():
    assert product_label({"identifier": "SLES_SAP"}) == "SLES_SAP"
    assert product_label({}) == "?"


def test_products_that_ship_nothing_are_dropped(components_file):
    config = load_config(components_file)
    products = [
        {"id": 1, "identifier": "SLES_SAP/15.7"},
        {"id": 2, "identifier": "SLES_SAP/15.6"},
    ]
    selected = select_products(config, products, shipping={1})
    assert [entry["id"] for entry in selected] == [1]


def test_at_most_max_products_per_major_are_kept(components_file):
    # The fixture sets max_products_per_major to 2.
    config = load_config(components_file)
    products = [
        {"id": 1, "identifier": "SLES_SAP/15.7"},
        {"id": 2, "identifier": "SLES_SAP/15.6"},
        {"id": 3, "identifier": "SLES_SAP/15.5"},
        {"id": 4, "identifier": "SLES_SAP/16.0"},
    ]
    selected = select_products(config, products, shipping={1, 2, 3, 4})
    assert [entry["id"] for entry in selected] == [4, 1, 2]


def test_majors_come_out_newest_first(components_file):
    config = load_config(components_file)
    products = [
        {"id": 1, "identifier": "SLES_SAP/15.7"},
        {"id": 2, "identifier": "SLES_SAP/16.0"},
    ]
    selected = select_products(config, products, shipping={1, 2})
    assert [entry["label"] for entry in selected] == ["16.0", "15.7"]


def test_the_selected_shape_is_what_the_table_reads(components_file):
    config = load_config(components_file)
    selected = select_products(config, [{"id": 1, "identifier": "SLES_SAP/15.7"}], shipping={1})
    assert selected == [
        {"id": 1, "identifier": "SLES_SAP/15.7", "label": "15.7", "release": [15, 7]}
    ]
```

- [ ] **Step 2: Run the tests**

Run: `.venv-dev/bin/pytest scripts/release/tests/test_scc.py -v`

Expected: all pass. `test_at_most_max_products_per_major_are_kept` asserts the input order is preserved within a major, which `select_products` does not sort — if it fails, read `collect_state.py:87` and record the actual behaviour, because the ordering within a major comes from SCC.

- [ ] **Step 3: Commit**

```bash
git add scripts/release/tests/test_scc.py
git commit -m "Cover the SCC parsing the SLES columns depend on

SCC reports a product version as a display string, so SLES_SAP/15.7
calls itself \"15 SP7\". The identifier is parsed instead, and nothing
proved it stayed that way."
```

---

### Task 6: Rendering and the remaining pure functions

**Files:**
- Create: `scripts/release/tests/test_render.py`
- Create: `scripts/release/tests/test_variables.py`

**Interfaces:**
- Consumes: `render_table.em_dash_if_empty`, `render_table.without_timestamp`, `render_table.inject`, `render_table.BEGIN_MARKER`, `render_table.END_MARKER`, `check_variables.problems_for`, `common.load_config`.

- [ ] **Step 1: Write the rendering tests**

`scripts/release/tests/test_render.py`:

```python
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

from render_table import (
    BEGIN_MARKER,
    END_MARKER,
    em_dash_if_empty,
    inject,
    without_timestamp,
)


def test_em_dash_stands_in_for_nothing():
    assert em_dash_if_empty("1.2.3") == "1.2.3"
    assert em_dash_if_empty("") == "—"
    assert em_dash_if_empty(None) == "—"


def test_the_timestamp_footer_is_strippable():
    block = f"{BEGIN_MARKER}\n\n| a |\n\n<sub>Generated from x at 2026-01-01</sub>\n{END_MARKER}"
    assert "Generated from" not in without_timestamp(block)
    assert "| a |" in without_timestamp(block)


def test_injecting_into_a_file_with_markers_replaces_the_block(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text(f"# Title\n\n{BEGIN_MARKER}\nold\n{END_MARKER}\n\nfooter\n", encoding="utf-8")
    updated, changed = inject(readme, f"{BEGIN_MARKER}\nnew\n{END_MARKER}")
    assert changed
    assert "new" in updated
    assert "old" not in updated
    assert updated.startswith("# Title")
    assert updated.endswith("footer\n")


def test_injecting_the_same_content_is_not_a_change(tmp_path):
    block = f"{BEGIN_MARKER}\nsame\n{END_MARKER}"
    readme = tmp_path / "README.md"
    readme.write_text(f"# Title\n\n{block}\n", encoding="utf-8")
    _, changed = inject(readme, block)
    assert not changed


def test_only_the_timestamp_differing_is_not_a_change(tmp_path):
    # Otherwise the daily refresh commits a new timestamp every run and
    # --check fails on every pull request.
    old = f"{BEGIN_MARKER}\n| a |\n<sub>Generated from x at 2026-01-01</sub>\n{END_MARKER}"
    new = f"{BEGIN_MARKER}\n| a |\n<sub>Generated from x at 2026-06-30</sub>\n{END_MARKER}"
    readme = tmp_path / "README.md"
    readme.write_text(old, encoding="utf-8")
    _, changed = inject(readme, new)
    assert not changed


def test_a_real_content_change_is_a_change_despite_the_timestamp(tmp_path):
    old = f"{BEGIN_MARKER}\n| 1.2.3 |\n<sub>Generated from x at 2026-01-01</sub>\n{END_MARKER}"
    new = f"{BEGIN_MARKER}\n| 1.3.0 |\n<sub>Generated from x at 2026-01-01</sub>\n{END_MARKER}"
    readme = tmp_path / "README.md"
    readme.write_text(old, encoding="utf-8")
    _, changed = inject(readme, new)
    assert changed


def test_a_file_without_markers_gets_the_block_appended(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text("# Title\n", encoding="utf-8")
    updated, changed = inject(readme, f"{BEGIN_MARKER}\nnew\n{END_MARKER}")
    assert changed
    assert updated.startswith("# Title\n")
    assert BEGIN_MARKER in updated


def test_a_file_that_does_not_exist_yet_is_created_from_the_block(tmp_path):
    readme = tmp_path / "README.md"
    updated, changed = inject(readme, f"{BEGIN_MARKER}\nnew\n{END_MARKER}")
    assert changed
    assert updated.strip().startswith(BEGIN_MARKER)
```

- [ ] **Step 2: Write the variables tests**

`scripts/release/tests/test_variables.py`:

```python
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

from check_variables import problems_for
from common import load_config


def component(components_file, name="alpha"):
    return load_config(components_file).component(name)


def test_agreement_is_no_problem(components_file):
    problems = problems_for(
        component(components_file),
        {"OBS_PACKAGE": "trento-alpha", "OBS_ENABLED": "true"},
    )
    assert problems == []


def test_enabled_is_case_and_space_insensitive(components_file):
    problems = problems_for(
        component(components_file),
        {"OBS_PACKAGE": "trento-alpha", "OBS_ENABLED": "  True  "},
    )
    assert problems == []


def test_a_different_package_name_is_reported_with_both_values(components_file):
    problems = problems_for(
        component(components_file),
        {"OBS_PACKAGE": "trento-alfa", "OBS_ENABLED": "true"},
    )
    assert len(problems) == 1
    assert "trento-alpha" in problems[0]
    assert "trento-alfa" in problems[0]


def test_a_missing_repository_variable_is_reported(components_file):
    problems = problems_for(component(components_file), {"OBS_ENABLED": "true"})
    assert any("unset" in problem for problem in problems)


def test_a_package_nothing_is_submitted_for_is_reported(components_file):
    problems = problems_for(
        component(components_file),
        {"OBS_PACKAGE": "trento-alpha", "OBS_ENABLED": "false"},
    )
    assert any("OBS_ENABLED" in problem for problem in problems)


def test_a_component_with_no_obs_package_but_a_variable_is_reported(components_file, tmp_path):
    text = components_file.read_text(encoding="utf-8")
    components_file.write_text(text.replace("    obs_package: trento-alpha\n", ""), "utf-8")
    problems = problems_for(
        component(components_file),
        {"OBS_PACKAGE": "trento-alpha", "OBS_ENABLED": "true"},
    )
    assert len(problems) == 1
    assert "obs_package is unset" in problems[0]


def test_a_component_with_neither_is_no_problem(components_file):
    text = components_file.read_text(encoding="utf-8")
    components_file.write_text(text.replace("    obs_package: trento-alpha\n", ""), "utf-8")
    assert problems_for(component(components_file), {}) == []
```

- [ ] **Step 3: Run both files**

Run: `.venv-dev/bin/pytest scripts/release/tests/test_render.py scripts/release/tests/test_variables.py -v`

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add scripts/release/tests/test_render.py scripts/release/tests/test_variables.py
git commit -m "Cover the marker injection and the variable drift check

inject() is small and can corrupt profile/README.md if the markers
ever mismatch, and its timestamp handling is the reason the daily
refresh does not commit on every run.

problems_for is what keeps components.yaml's obs_package and the
repository's OBS_PACKAGE from parting company, which is otherwise a
silent failure: the table reports a package nothing publishes to and
calls it not submitted forever."
```

---

### Task 7: Reconciler states and write safety

**Files:**
- Create: `scripts/release/tests/test_reconcile.py`

**Interfaces:**
- Consumes: `reconcile.Cascade`, `conftest.FakeGitHub`, `conftest.components_file`, `common.load_config`.

- [ ] **Step 1: Write the failing tests**

`scripts/release/tests/test_reconcile.py`:

```python
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

import pytest

import reconcile
from common import load_config
from conftest import FakeGitHub


def observation(name="alpha", repo="alpha", version="1.2.3", depends_on=None):
    """The shape build_plan hands to Cascade.observe."""
    return {
        "name": name,
        "repo": repo,
        "version": version,
        "head_branch": f"release-bump-{version}",
        "depends_on": depends_on or [],
        "files": [],
    }


def cascade(components_file, github, monkeypatch, *, image_published=True):
    config = load_config(components_file)
    monkeypatch.setattr(reconcile, "ghcr_tag_exists", lambda *a, **k: image_published)
    return reconcile.Cascade(config, github, {"train": "3.2", "components": [], "problems": []})


def test_no_pull_and_no_dependencies_is_todo(components_file, monkeypatch):
    machine = cascade(components_file, FakeGitHub(), monkeypatch)
    assert machine.observe(observation())["state"] == "todo"


def test_an_open_pull_is_open(components_file, monkeypatch):
    github = FakeGitHub(
        pulls={("alpha", "release-bump-1.2.3"): [{"number": 7, "state": "open", "html_url": "u"}]}
    )
    machine = cascade(components_file, github, monkeypatch)
    observed = machine.observe(observation())
    assert observed["state"] == "open"
    assert observed["pull"] == 7


def test_a_merged_pull_without_a_tag_is_merged(components_file, monkeypatch):
    github = FakeGitHub(
        pulls={
            ("alpha", "release-bump-1.2.3"): [
                {"number": 7, "state": "closed", "merged_at": "2026-01-01T00:00:00Z", "html_url": "u"}
            ]
        }
    )
    machine = cascade(components_file, github, monkeypatch)
    assert machine.observe(observation())["state"] == "merged"


def test_a_closed_unmerged_pull_is_abandoned(components_file, monkeypatch):
    # Pinned, and a known gap: `abandoned` has no STATE_ICON entry, so
    # it renders as a blank cell in the progress comment. Giving it an
    # icon is a deliberate follow-up, not part of a testing change.
    github = FakeGitHub(
        pulls={
            ("alpha", "release-bump-1.2.3"): [
                {"number": 7, "state": "closed", "merged_at": None, "html_url": "u"}
            ]
        }
    )
    machine = cascade(components_file, github, monkeypatch)
    assert machine.observe(observation())["state"] == "abandoned"
    assert reconcile.STATE_ICON.get("abandoned", "") == ""


def test_a_tag_and_a_published_image_is_released(components_file, monkeypatch):
    github = FakeGitHub(releases={"alpha": ("1.2.3",)})
    machine = cascade(components_file, github, monkeypatch, image_published=True)
    observed = machine.observe(observation())
    assert observed["state"] == "released"
    assert observed["image_ready"] is True


def test_a_tag_without_its_image_stays_merged(components_file, monkeypatch):
    # The failure this whole tier exists to prevent: a chart pinned to
    # an image tag that does not exist installs and then breaks.
    github = FakeGitHub(releases={"alpha": ("1.2.3",)})
    machine = cascade(components_file, github, monkeypatch, image_published=False)
    observed = machine.observe(observation())
    assert observed["state"] == "merged"
    assert observed["image_ready"] is False


def test_an_unreleased_dependency_blocks(components_file, monkeypatch):
    machine = cascade(components_file, FakeGitHub(), monkeypatch)
    machine.states["alpha"] = {"state": "merged"}
    machine.states["beta"] = {"state": "released"}
    observed = machine.observe(observation(name="chart", repo="chart", depends_on=["alpha", "beta"]))
    assert observed["state"] == "blocked"
    assert observed["waiting_on"] == ["alpha"]


def test_all_dependencies_released_unblocks(components_file, monkeypatch):
    machine = cascade(components_file, FakeGitHub(), monkeypatch)
    machine.states["alpha"] = {"state": "released"}
    machine.states["beta"] = {"state": "released"}
    observed = machine.observe(observation(name="chart", repo="chart", depends_on=["alpha", "beta"]))
    assert observed["state"] == "todo"
    assert observed["waiting_on"] == []


def test_a_component_with_no_image_is_released_on_its_tag_alone(components_file, monkeypatch):
    text = components_file.read_text(encoding="utf-8")
    components_file.write_text(text.replace("    ghcr_image: trento-alpha\n", ""), "utf-8")
    github = FakeGitHub(releases={"alpha": ("1.2.3",)})
    machine = cascade(components_file, github, monkeypatch, image_published=False)
    observed = machine.observe(observation())
    assert observed["state"] == "released"
    assert observed["image_ready"] is None


@pytest.mark.parametrize("state", ["todo", "open", "merged", "released", "blocked"])
def test_every_state_except_abandoned_has_an_icon(state):
    assert reconcile.STATE_ICON.get(state)


def test_observing_writes_nothing(components_file, monkeypatch):
    # The gate is enforced elsewhere, but observation must never write
    # regardless of it, so this is asserted at the lowest level.
    github = FakeGitHub(releases={"alpha": ("1.2.3",)})
    machine = cascade(components_file, github, monkeypatch)
    machine.observe(observation())
    assert github.performed == []


def test_a_dry_run_client_records_instead_of_writing(components_file):
    github = FakeGitHub()
    assert github.dry_run is True
    github.create_branch("alpha", "release-bump-1.2.3", "main")
    assert github.performed != []
    assert "release-bump-1.2.3" in github.performed[0]
```

- [ ] **Step 2: Run the tests**

Run: `.venv-dev/bin/pytest scripts/release/tests/test_reconcile.py -v`

Expected: all pass. Two are likely to need adjustment to the real signatures — read `reconcile.py:76-133` for `Cascade.__init__` and `observe`, and `common.py:641` for `create_branch`. Adjust the call, never the asserted behaviour.

- [ ] **Step 3: Commit**

```bash
git add scripts/release/tests/test_reconcile.py
git commit -m "Cover all six reconciler states and the write seam

The state that matters most is a published tag whose image is still
building: it must read merged, not released, or the chart is bumped
to pin an image tag that does not exist yet.

Also records that `abandoned` has no STATE_ICON entry and therefore
renders as a blank cell. Left as it is; a testing change is the wrong
place to alter what the progress comment looks like."
```

---

### Task 8: Give GHCR an address

First of three production changes. Test-first.

**Files:**
- Modify: `scripts/release/common.py:326` (the `Config` dataclass), `common.py:409` (the `Config(...)` construction), `common.py:852-870` (`ghcr_tag_exists`)
- Modify: `scripts/release/tests/conftest.py` (add `api` to the fixture's `ghcr` block)
- Create: `scripts/release/tests/test_ghcr.py`

**Interfaces:**
- Produces: `Config.ghcr_api: str`, a full base URL with scheme, defaulting to `https://{ghcr_registry}`. Task 11's `components.yaml.template` sets it to the stub server.

- [ ] **Step 1: Write the failing test**

`scripts/release/tests/test_ghcr.py`:

```python
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

import common
from common import load_config


def test_the_api_defaults_to_https_on_the_registry(components_file):
    config = load_config(components_file)
    assert config.ghcr_registry == "ghcr.invalid"
    assert config.ghcr_api == "https://ghcr.invalid"


def test_an_explicit_api_wins(components_file):
    text = components_file.read_text(encoding="utf-8")
    components_file.write_text(
        text.replace("    namespace: testns", "    namespace: testns\n    api: http://127.0.0.1:9"),
        encoding="utf-8",
    )
    assert load_config(components_file).ghcr_api == "http://127.0.0.1:9"


def test_a_trailing_slash_is_stripped(components_file):
    text = components_file.read_text(encoding="utf-8")
    components_file.write_text(
        text.replace("    namespace: testns", "    namespace: testns\n    api: http://x/"),
        encoding="utf-8",
    )
    assert load_config(components_file).ghcr_api == "http://x"


def test_the_urls_are_built_from_the_api_and_the_service_stays_a_bare_host(
    components_file, monkeypatch
):
    # `service` is a query parameter of the token endpoint and must be
    # the registry host, not a URL. This is why the two cannot be one
    # setting.
    text = components_file.read_text(encoding="utf-8")
    components_file.write_text(
        text.replace("    namespace: testns", "    namespace: testns\n    api: http://127.0.0.1:9"),
        encoding="utf-8",
    )
    config = load_config(components_file)

    seen = []

    def fake_get_json(url, **kwargs):
        seen.append(url)
        return {"token": "t"}

    def fake_get(url, **kwargs):
        seen.append(url)
        return b"{}"

    monkeypatch.setattr(common, "http_get_json", fake_get_json)
    monkeypatch.setattr(common, "http_get", fake_get)

    assert common.ghcr_tag_exists(config, "trento-alpha", "1.2.3") is True
    assert seen[0].startswith("http://127.0.0.1:9/token")
    assert "service=ghcr.invalid" in seen[0]
    assert seen[1] == "http://127.0.0.1:9/v2/testns/trento-alpha/manifests/1.2.3"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv-dev/bin/pytest scripts/release/tests/test_ghcr.py -v`

Expected: FAIL with `AttributeError: 'Config' object has no attribute 'ghcr_api'`.

- [ ] **Step 3: Add the field to the dataclass**

In `scripts/release/common.py`, in the `Config` dataclass, after `ghcr_registry: str`:

```python
    ghcr_registry: str
    # The registry as an address. Kept apart from ghcr_registry because
    # that value is also the `service` parameter of the token endpoint,
    # which must be a bare host. Defaults to https on the registry, so
    # a components.yaml that does not mention it behaves as before.
    ghcr_api: str
    ghcr_namespace: str
```

- [ ] **Step 4: Populate it in load_config**

In `load_config`, immediately before the `return Config(`:

```python
    ghcr = sources["ghcr"]
    ghcr_api = (ghcr.get("api") or f"https://{ghcr['registry']}").rstrip("/")
```

and inside the `Config(...)` call, after `ghcr_registry=sources["ghcr"]["registry"],`:

```python
        ghcr_api=ghcr_api,
```

- [ ] **Step 5: Build the URLs from it**

In `ghcr_tag_exists`, replace the two URL expressions:

```python
    token_url = (
        f"{config.ghcr_api}/token"
        f"?scope=repository:{config.ghcr_namespace}/{image}:pull&service={config.ghcr_registry}"
    )
```

and

```python
        manifest_url = (
            f"{config.ghcr_api}/v2/{config.ghcr_namespace}/{image}/manifests/"
            f"{urllib.parse.quote(tag)}"
        )
```

- [ ] **Step 6: Run the tests**

Run: `.venv-dev/bin/pytest scripts/release/tests/test_ghcr.py scripts/release/tests/test_config.py -v`

Expected: all pass.

- [ ] **Step 7: Verify nothing regressed against the real configuration**

Run:

```bash
.venv-dev/bin/python -c "
from pathlib import Path
import sys; sys.path.insert(0, 'scripts/release')
from common import load_config
c = load_config(Path('release/components.yaml'))
print(c.ghcr_registry, c.ghcr_api)
"
```

Expected: `ghcr.io https://ghcr.io` — the default reproduces today's behaviour with no change to `release/components.yaml`.

- [ ] **Step 8: Commit**

```bash
git add scripts/release/common.py scripts/release/tests/test_ghcr.py scripts/release/tests/conftest.py
git commit -m "Let the container registry be addressed, not just named

ghcr_registry is a bare host, and the two registry URLs interpolate
it after a hardcoded https://. It is also the token endpoint's
service parameter, which has to stay a bare host, so the two cannot
be the same setting.

Adds ghcr_api alongside it, defaulting to https on the registry, so
release/components.yaml needs no change and the fallback is exactly
the current behaviour. The end-to-end tests set it to a local server."
```

---

### Task 9: Make the token lookup silenceable

**Files:**
- Modify: `scripts/release/common.py:466-485` (`github_token`)
- Create: `scripts/release/tests/test_token.py`

**Interfaces:**
- Produces: `github_token()` returns `None` when `GITHUB_TOKEN` or `GH_TOKEN` is present but empty, without consulting `gh`.

- [ ] **Step 1: Write the failing test**

`scripts/release/tests/test_token.py`:

```python
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

import common
from common import github_token


def test_github_token_is_used(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "from-env")
    assert github_token() == "from-env"


def test_gh_token_is_the_fallback(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "from-gh-env")
    assert github_token() == "from-gh-env"


def test_github_token_wins_over_gh_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "first")
    monkeypatch.setenv("GH_TOKEN", "second")
    assert github_token() == "first"


def test_the_cli_is_consulted_when_no_variable_is_set(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(common.shutil, "which", lambda name: "/usr/bin/gh")

    class Result:
        returncode = 0
        stdout = "from-cli\n"

    monkeypatch.setattr(common.subprocess, "run", lambda *a, **k: Result())
    assert github_token() == "from-cli"


def test_an_empty_variable_means_no_token_and_does_not_ask_the_cli(monkeypatch):
    # Without this the end-to-end suite ships a developer's real token
    # to the stub server while CI, which has no gh, sends none. The
    # tests that matter most here are exactly the unauthenticated ones.
    monkeypatch.setenv("GITHUB_TOKEN", "")
    monkeypatch.delenv("GH_TOKEN", raising=False)

    def explode(*args, **kwargs):
        raise AssertionError("the gh CLI must not be consulted")

    monkeypatch.setattr(common.shutil, "which", explode)
    assert github_token() is None


def test_an_empty_gh_token_also_silences_the_cli(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "")

    def explode(*args, **kwargs):
        raise AssertionError("the gh CLI must not be consulted")

    monkeypatch.setattr(common.shutil, "which", explode)
    assert github_token() is None


def test_no_variables_and_no_cli_gives_none(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(common.shutil, "which", lambda name: None)
    assert github_token() is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv-dev/bin/pytest scripts/release/tests/test_token.py -v`

Expected: `test_an_empty_variable_means_no_token_and_does_not_ask_the_cli` and `test_an_empty_gh_token_also_silences_the_cli` FAIL with `AssertionError: the gh CLI must not be consulted`.

- [ ] **Step 3: Make presence beat the CLI**

Replace the loop at the top of `github_token`:

```python
def github_token() -> str | None:
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        if name in os.environ:
            # Set but empty is a deliberate "no token": it is how a
            # test, or a workflow whose `${{ vars.X }}` rendered empty,
            # asks for an unauthenticated run. Falling through to the
            # CLI here would make a developer machine and a runner
            # behave differently.
            return os.environ[name] or None
```

Leave the rest of the function unchanged.

- [ ] **Step 4: Run the tests**

Run: `.venv-dev/bin/pytest scripts/release/tests/test_token.py -v`

Expected: all pass.

- [ ] **Step 5: Verify the real scripts still authenticate**

Run:

```bash
.venv-dev/bin/python scripts/release/check_variables.py
```

Expected: exit 0 and `0 disagreement(s)`, picking up credentials from `gh` exactly as before — no `GITHUB_TOKEN` is set in an interactive shell, so the CLI path is still taken.

- [ ] **Step 6: Commit**

```bash
git add scripts/release/common.py scripts/release/tests/test_token.py
git commit -m "Let a caller ask for an unauthenticated run

github_token() falls back to the gh CLI so the scripts are runnable
on a developer machine without exporting anything. That convenience
makes an unauthenticated run impossible to request: unsetting the
variables still finds the CLI.

Presence now beats the CLI, so GITHUB_TOKEN= means no token. Nothing
changes in CI, where the variable already renders empty and gh is not
installed, so both paths already produced None."
```

---

### Task 10: Let the status table be pointed at a configuration

**Files:**
- Modify: `scripts/release/render_table.py:41-48` (`collect`), `render_table.py:175-192` (`main`)
- Create: `scripts/release/tests/test_render_table_cli.py`

**Interfaces:**
- Produces: `render_table.py --components PATH`, forwarded to the `collect_state.py` subprocess. `collect(argv_extra)` keeps its signature; callers pass the flag in `argv_extra`.

- [ ] **Step 1: Write the failing test**

`scripts/release/tests/test_render_table_cli.py`:

```python
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

import json
import subprocess
import sys
from pathlib import Path

import render_table

SCRIPTS = Path(__file__).resolve().parents[1]

STATE = {
    "generated_at": "2026-01-01T00:00:00Z",
    "github": {},
    "obs": {},
    "scc": {"products": [], "availability": {}},
}


def test_components_is_forwarded_to_the_child(monkeypatch, tmp_path, components_file):
    seen = {}

    class Result:
        returncode = 0
        stdout = json.dumps(STATE)
        stderr = ""

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    render_table.main(
        ["--collect", "--components", str(components_file), "--state-out", str(tmp_path / "s.json")]
        if False
        else ["--collect", "--components", str(components_file)]
    )
    assert "--components" in seen["argv"]
    assert str(components_file) in seen["argv"]


def test_the_table_reads_the_given_configuration(components_file, tmp_path, capsys):
    state = tmp_path / "state.json"
    state.write_text(json.dumps(STATE), encoding="utf-8")
    render_table.main(["--state", str(state), "--components", str(components_file)])
    output = capsys.readouterr().out
    # The fixture's stable project, not the real devel:sap:trento.
    assert "test:stable" in output


def test_the_cli_still_runs_with_no_components_flag(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps(STATE), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "render_table.py"), "--state", str(state)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv-dev/bin/pytest scripts/release/tests/test_render_table_cli.py -v`

Expected: FAIL with `unrecognized arguments: --components`.

- [ ] **Step 3: Add the flag**

In `render_table.py`, in `main`, after the `--skip-scc` argument:

```python
    parser.add_argument(
        "--components",
        type=Path,
        default=COMPONENTS_FILE,
        help="component list; also passed to collect_state.py",
    )
```

Add `COMPONENTS_FILE` to the existing `from common import ...` line.

- [ ] **Step 4: Use it, and forward it**

Replace the `config = load_config()` and `state = collect(...)` lines with:

```python
    config = load_config(args.components)

    # The child reads the same file. Forwarding it rather than letting
    # the child fall back to the default is what makes the table
    # testable and rehearsable on a fork.
    passthrough = ["--components", str(args.components)]
    if args.skip_scc:
        passthrough.append("--skip-scc")
    state = (
        collect(passthrough)
        if args.collect
        else json.loads(args.state.read_text(encoding="utf-8"))
    )
```

- [ ] **Step 5: Run the tests**

Run: `.venv-dev/bin/pytest scripts/release/tests/test_render_table_cli.py -v`

Expected: all pass. Delete the dead `if False else` branch left in the first test by Step 1 and re-run — it is there only to make the intended call explicit while the flag does not exist yet.

- [ ] **Step 6: Verify the real table is unchanged**

Run:

```bash
.venv-dev/bin/python scripts/release/render_table.py --collect --inject profile/README.md --check
```

Expected: exit 0 and `up to date`. If it reports `would change`, inspect the diff before continuing — the flag must not alter output.

- [ ] **Step 7: Commit**

```bash
git add scripts/release/render_table.py scripts/release/tests/test_render_table_cli.py
git commit -m "Let the status table be pointed at a component list

Every other script takes --components. render_table.py did not: it
called load_config() on the default path and spawned collect_state.py
without forwarding anything, so it was the one script that could not
be aimed at a fork or a fixture."
```

---

### Task 11: The stub server

**Files:**
- Create: `scripts/release/tests/e2e/__init__.py` (empty)
- Create: `scripts/release/tests/e2e/server.py`
- Create: `scripts/release/tests/test_server.py`

**Interfaces:**
- Produces: `server.py` runnable as `python3 server.py --root DIR [--scenario DIR] --port-file FILE`, printing the chosen port to stdout. Serves `GET <path>` from `<root>/<host><path>.json`, or `.404`/`.401`/`.403` sidecars for error statuses, or `<path>.link` for a `Link` header. Task 13 onward consume it via `helpers.bash`.

- [ ] **Step 1: Write the server**

`scripts/release/tests/e2e/server.py`:

```python
#!/usr/bin/env python3
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

"""Replay recorded API responses over HTTP.

The end-to-end tests point every API in components.yaml at this, so the
scripts under test are exercised exactly as a workflow runs them, with
no patching and no test-awareness in the code.

A response lives at <root>/<host>/<path>.json. A sidecar with a status
code as its suffix - .404, .401, .403 - answers with that status
instead, which is how the refusal and drift cases are set up. A
<path>.link file supplies a Link header so pagination is walked for
real.

A scenario directory shadows the base: it is searched first, so a
scenario is a few files rather than a second recording.

An unknown path answers 404 and writes a line to stderr. That line is
what a test asserts on, because a silent 404 is indistinguishable from
"no release yet" and would turn a broken test green.
"""

from __future__ import annotations

import argparse
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

STATUS_SUFFIXES = ("401", "403", "404", "500")


class Replay(BaseHTTPRequestHandler):
    roots: list[Path] = []

    def _candidate(self, host: str, path: str) -> tuple[Path, int] | None:
        relative = f"{host}{path}".strip("/")
        for root in self.roots:
            base = root / relative
            if base.with_suffix(base.suffix + ".json").is_file():
                return base.with_suffix(base.suffix + ".json"), 200
            for status in STATUS_SUFFIXES:
                candidate = base.with_suffix(base.suffix + f".{status}")
                if candidate.is_file():
                    return candidate, int(status)
        return None

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
        split = urlsplit(self.path)
        # The Host header carries the port; the recording is keyed by
        # hostname alone so the same fixtures work on any port.
        host = self.headers.get("X-Trento-Host") or split.netloc.split(":")[0]
        found = self._candidate(host, split.path)

        if found is None:
            sys.stderr.write(f"MISS {host}{split.path}\n")
            sys.stderr.flush()
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"message":"Not Found"}')
            return

        path, status = found
        body = path.read_bytes()
        link = path.with_suffix(".link")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if link.is_file():
            self.send_header("Link", link.read_text(encoding="utf-8").strip())
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        # The default logs every request to stderr, which would drown
        # the MISS lines a test greps for.
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--scenario", type=Path, action="append", default=[])
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--port-file", type=Path)
    args = parser.parse_args(argv)

    Replay.roots = [*args.scenario, args.root]
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Replay)
    port = server.server_address[1]
    if args.port_file:
        args.port_file.write_text(str(port), encoding="utf-8")
    print(port, flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Write the tests for the server itself**

`scripts/release/tests/test_server.py`:

```python
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

"""The stub server is test infrastructure, so it gets tested too.

A replay server that quietly returns the wrong thing would make every
end-to-end result meaningless.
"""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parent / "e2e" / "server.py"


@pytest.fixture
def serve(tmp_path):
    processes = []

    def start(root, scenario=None):
        port_file = tmp_path / "port"
        argv = [sys.executable, str(SERVER), "--root", str(root), "--port-file", str(port_file)]
        if scenario:
            argv += ["--scenario", str(scenario)]
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(process)
        for _ in range(100):
            if port_file.is_file() and port_file.read_text(encoding="utf-8"):
                break
            time.sleep(0.02)
        else:
            raise RuntimeError("server did not start")
        return int(port_file.read_text(encoding="utf-8")), process

    yield start
    for process in processes:
        process.terminate()
        process.wait(timeout=5)


def write(root, relative, payload):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) if isinstance(payload, dict) else payload, "utf-8")
    return path


def get(port, path, host="api.github.invalid"):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"X-Trento-Host": host})
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read()), dict(response.headers)


def test_a_recorded_body_is_replayed(serve, tmp_path):
    root = tmp_path / "recorded"
    write(root, "api.github.invalid/repos/org/web/releases/latest.json", {"tag_name": "1.2.3"})
    port, _ = serve(root)
    status, body, _ = get(port, "/repos/org/web/releases/latest")
    assert status == 200
    assert body["tag_name"] == "1.2.3"


def test_a_status_sidecar_sets_the_status(serve, tmp_path):
    root = tmp_path / "recorded"
    write(root, "api.github.invalid/repos/org/web/actions/variables.403", '{"message":"no"}')
    port, _ = serve(root)
    with pytest.raises(urllib.error.HTTPError) as raised:
        get(port, "/repos/org/web/actions/variables")
    assert raised.value.code == 403


def test_a_link_sidecar_becomes_a_link_header(serve, tmp_path):
    root = tmp_path / "recorded"
    write(root, "api.github.invalid/repos/org/web/labels.json", [{"name": "bug"}])
    write(root, "api.github.invalid/repos/org/web/labels.link", '<http://x/page2>; rel="next"')
    port, _ = serve(root)
    _, _, headers = get(port, "/repos/org/web/labels")
    assert 'rel="next"' in headers["Link"]


def test_a_scenario_shadows_the_base(serve, tmp_path):
    root = tmp_path / "recorded"
    scenario = tmp_path / "scenario"
    write(root, "api.github.invalid/v.json", {"from": "base"})
    write(scenario, "api.github.invalid/v.json", {"from": "scenario"})
    port, _ = serve(root, scenario)
    _, body, _ = get(port, "/v")
    assert body["from"] == "scenario"


def test_the_base_still_answers_what_the_scenario_does_not_shadow(serve, tmp_path):
    root = tmp_path / "recorded"
    scenario = tmp_path / "scenario"
    write(root, "api.github.invalid/a.json", {"from": "base"})
    write(scenario, "api.github.invalid/b.json", {"from": "scenario"})
    port, _ = serve(root, scenario)
    _, body, _ = get(port, "/a")
    assert body["from"] == "base"


def test_an_unknown_path_is_a_loud_404(serve, tmp_path):
    root = tmp_path / "recorded"
    root.mkdir()
    port, process = serve(root)
    with pytest.raises(urllib.error.HTTPError) as raised:
        get(port, "/nothing/here")
    assert raised.value.code == 404
    process.terminate()
    _, stderr = process.communicate(timeout=5)
    assert "MISS api.github.invalid/nothing/here" in stderr


def test_a_query_string_does_not_change_the_lookup(serve, tmp_path):
    root = tmp_path / "recorded"
    write(root, "api.github.invalid/repos/org/web/labels.json", [{"name": "bug"}])
    port, _ = serve(root)
    status, _, _ = get(port, "/repos/org/web/labels?per_page=100")
    assert status == 200
```

- [ ] **Step 3: Run the tests**

Run: `.venv-dev/bin/pytest scripts/release/tests/test_server.py -v`

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add scripts/release/tests/e2e/ scripts/release/tests/test_server.py
git commit -m "Add the server the end-to-end tests replay through

Pointing components.yaml at it is what makes the cascade testable as
a black box: no patching, no mocking, the scripts run exactly as a
workflow runs them.

It is test infrastructure, so it is tested. A replay server that
quietly returns the wrong thing would make every result downstream
meaningless. An unknown path answers 404 and prints MISS, because a
silent 404 reads as \"no release yet\" and would turn a broken test
green."
```

---

### Task 12: Record the base fixtures

**Files:**
- Create: `scripts/release/tests/e2e/record.sh`
- Create: `scripts/release/tests/e2e/recorded/**` (generated, committed)
- Create: `scripts/release/tests/e2e/components.yaml.template`
- Create: `scripts/release/tests/e2e/helpers.bash`

**Interfaces:**
- Produces: `helpers.bash` exporting `start_server`, `stop_server` and `run_release` for every `.bats` file. `run_release <script> [args...]` runs `python3 scripts/release/<script>` with `--components` pointed at the templated config and `GITHUB_TOKEN=` set empty, capturing `status`, `output`.

- [ ] **Step 1: Write the configuration template**

`scripts/release/tests/e2e/components.yaml.template`:

```yaml
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

# The real component list with every endpoint pointed at the stub
# server. Kept as a template rather than generated from
# release/components.yaml so that a change to the real file shows up as
# a golden diff rather than silently changing what the tests exercise.

sources:
  github:
    api: http://127.0.0.1:__PORT__
    org: trento-project
  obs:
    api: http://127.0.0.1:__PORT__
    projects:
      stable: devel:sap:trento
      rolling: devel:sap:trento:factory
  scc:
    api: http://127.0.0.1:__PORT__
    product_pattern: '^SLES_SAP/'
    architectures: [x86_64]
    max_products_per_major: 3
  ghcr:
    registry: ghcr.io
    api: http://127.0.0.1:__PORT__
    namespace: trento-project

release_branches: [main, release]

defaults:
  version_file: VERSION
  tier: 1
  obs_version_file: '{obs_package}.spec'

components:
  web:
    obs_package: trento-web
    obs_image_package: trento-web-image
    ghcr_image: trento-web
  wanda:
    obs_package: trento-wanda
    obs_image_package: trento-wanda-image
    ghcr_image: trento-wanda
  checks:
    obs_package: trento-checks
  agent:
    obs_package: trento-agent
  helm-charts:
    tier: 2
    depends_on: [web, wanda, checks]
    obs_package: trento-server-helm
    bumps:
      - path: VERSION
      - path: charts/trento-server/Chart.yaml
        kind: yaml
        key: version
      - path: charts/trento-server/values.yaml
        kind: yaml
        key: trento-web.image.tag
        source: web
```

Note: this deliberately lists five components, not all eight. Adding the rest is a fixture change, not a code change, and five is enough to exercise both tiers and a dependency edge.

- [ ] **Step 2: Write the bats helpers**

`scripts/release/tests/e2e/helpers.bash`:

```bash
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

E2E_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$E2E_DIR/../../../.." && pwd)"
PYTHON="${PYTHON:-$REPO_ROOT/.venv-dev/bin/python}"

start_server() {
  local scenario="${1:-}"
  E2E_TMP="$(mktemp -d)"
  local argv=("$PYTHON" "$E2E_DIR/server.py" --root "$E2E_DIR/recorded" \
              --port-file "$E2E_TMP/port")
  if [ -n "$scenario" ]; then
    argv+=(--scenario "$E2E_DIR/scenarios/$scenario")
  fi

  "${argv[@]}" >"$E2E_TMP/stdout" 2>"$E2E_TMP/stderr" &
  E2E_SERVER_PID=$!

  local waited=0
  while [ ! -s "$E2E_TMP/port" ]; do
    sleep 0.05
    waited=$((waited + 1))
    if [ "$waited" -gt 100 ]; then
      echo "server did not start: $(cat "$E2E_TMP/stderr")" >&2
      return 1
    fi
  done

  E2E_PORT="$(cat "$E2E_TMP/port")"
  sed "s/__PORT__/$E2E_PORT/g" "$E2E_DIR/components.yaml.template" \
    > "$E2E_TMP/components.yaml"
  export E2E_TMP E2E_PORT E2E_SERVER_PID
}

stop_server() {
  [ -n "${E2E_SERVER_PID:-}" ] && kill "$E2E_SERVER_PID" 2>/dev/null
  wait "$E2E_SERVER_PID" 2>/dev/null
  return 0
}

# Run a release script against the stub. GITHUB_TOKEN is set and empty
# on purpose: it is how github_token() is told not to consult the gh
# CLI, so a developer machine and a runner behave identically.
run_release() {
  local script="$1"; shift
  run env GITHUB_TOKEN= GH_TOKEN= \
    "$PYTHON" "$REPO_ROOT/scripts/release/$script" \
    --components "$E2E_TMP/components.yaml" "$@"
}

# Fail if the server logged a fixture miss. A silent 404 is
# indistinguishable from "no release yet", so a missing recording would
# otherwise make a broken test pass.
assert_no_misses() {
  if grep -q '^MISS ' "$E2E_TMP/stderr" 2>/dev/null; then
    echo "fixture misses:" >&2
    grep '^MISS ' "$E2E_TMP/stderr" >&2
    return 1
  fi
}

assert_golden() {
  local name="$1"
  local actual="$2"
  local expected="$E2E_DIR/golden/$name"
  if [ -n "${UPDATE_GOLDEN:-}" ]; then
    mkdir -p "$(dirname "$expected")"
    printf '%s\n' "$actual" > "$expected"
    return 0
  fi
  if ! diff -u "$expected" <(printf '%s\n' "$actual"); then
    echo "golden mismatch for $name; re-run with UPDATE_GOLDEN=1 to accept" >&2
    return 1
  fi
}
```

- [ ] **Step 3: Write the recorder**

`scripts/release/tests/e2e/record.sh`:

```bash
#!/usr/bin/env bash
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

# Capture the responses the end-to-end tests replay.
#
# Run by hand, never in CI: reaching the live APIs on a schedule would
# turn an upstream change into a red build on an unrelated pull
# request. Fixtures going stale shows up as a diff when someone runs
# this.
#
# No credential is captured. All four APIs answer these paths
# anonymously, which the status table workflow already relies on.

set -euo pipefail

E2E_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$E2E_DIR/recorded"

GITHUB=https://api.github.com
OBS=https://api.opensuse.org
SCC=https://scc.suse.com
GHCR=https://ghcr.io

ORG=trento-project
COMPONENTS=(web wanda checks agent helm-charts)
PACKAGES=(trento-web trento-wanda trento-checks trento-agent trento-server-helm)
PROJECTS=(devel:sap:trento devel:sap:trento:factory)

capture() {
  local url="$1" target="$2"
  mkdir -p "$(dirname "$target")"
  local status
  status="$(curl -sS -o "$target.tmp" -w '%{http_code}' \
    -H 'User-Agent: trento-release-tooling' "$url" || echo 000)"
  if [ "$status" = "200" ]; then
    mv "$target.tmp" "$target.json"
    rm -f "$target".{401,403,404}
    echo "  200 $url"
  else
    rm -f "$target.tmp" "$target.json"
    : > "$target.$status"
    echo "  $status $url"
  fi
}

echo "GitHub"
for component in "${COMPONENTS[@]}"; do
  capture "$GITHUB/repos/$ORG/$component/releases/latest" \
          "$OUT/api.github.com/repos/$ORG/$component/releases/latest"
  capture "$GITHUB/repos/$ORG/$component/contents/VERSION" \
          "$OUT/api.github.com/repos/$ORG/$component/contents/VERSION"
  capture "$GITHUB/repos/$ORG/$component/labels" \
          "$OUT/api.github.com/repos/$ORG/$component/labels"
done

echo "OBS"
for project in "${PROJECTS[@]}"; do
  for package in "${PACKAGES[@]}"; do
    capture "$OBS/public/source/$project/$package" \
            "$OUT/api.opensuse.org/public/source/$project/$package"
  done
done

echo "SCC"
capture "$SCC/api/package_search/products" \
        "$OUT/scc.suse.com/api/package_search/products"

echo "GHCR"
for image in trento-web trento-wanda; do
  capture "$GHCR/token?scope=repository:$ORG/$image:pull&service=ghcr.io" \
          "$OUT/ghcr.io/token"
done

echo
echo "recorded into $OUT"
echo "review the diff before committing; these are the tests' ground truth"
```

- [ ] **Step 4: Make it executable and run it**

```bash
chmod +x scripts/release/tests/e2e/record.sh
scripts/release/tests/e2e/record.sh
```

Expected: a list of `200` lines and a populated `recorded/` tree. Any `000` means no network; any `403` from GitHub means rate limiting — wait and re-run rather than committing the error sidecar.

- [ ] **Step 5: Check the recording for anything that should not be committed**

```bash
grep -rl 'Authorization\|token\|ghp_\|gho_' scripts/release/tests/e2e/recorded/ || echo "clean"
du -sh scripts/release/tests/e2e/recorded/
```

Expected: `clean` apart from `ghcr.io/token.json`, which holds an anonymous pull token for a public image and is not a credential. Size should be well under 1 MB.

- [ ] **Step 6: Commit**

```bash
chmod +x scripts/release/tests/e2e/record.sh
git add scripts/release/tests/e2e/
git commit -m "Record what the end-to-end tests replay

One snapshot of the four APIs as they actually answer for
trento-project, captured anonymously because all these paths are
public - which the status table workflow already depends on.

record.sh is deliberately not wired into CI. Reaching live APIs on a
schedule would turn an upstream change into a red build on an
unrelated pull request; running it by hand makes staleness a diff
somebody reads."
```

---

### Task 13: The plan end to end

**Files:**
- Create: `scripts/release/tests/e2e/plan.bats`
- Create: `scripts/release/tests/e2e/manifests/train.yaml`
- Create: `scripts/release/tests/e2e/golden/plan.json` (generated in Step 4)

- [ ] **Step 1: Write the manifest fixture**

`scripts/release/tests/e2e/manifests/train.yaml`:

```yaml
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

# A train that bumps web and drags the chart in behind it. The versions
# must be ahead of whatever record.sh captured; bump them when the
# recording is refreshed and the plan reports "does not advance".
versions:
  web: 99.1.0
  helm-charts: 99.1.0
```

- [ ] **Step 2: Write the tests**

`scripts/release/tests/e2e/plan.bats`:

```bash
#!/usr/bin/env bats
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

load helpers

setup_file() {
  load helpers
  start_server
}

teardown_file() {
  load helpers
  stop_server
}

@test "a valid train plans cleanly" {
  run_release plan.py --manifest "$E2E_DIR/manifests/train.yaml" --json
  [ "$status" -eq 0 ]
  assert_no_misses
  assert_golden plan.json "$output"
}

@test "the human rendering mentions both components" {
  run_release plan.py --manifest "$E2E_DIR/manifests/train.yaml" --no-diff
  [ "$status" -eq 0 ]
  [[ "$output" == *"web"* ]]
  [[ "$output" == *"helm-charts"* ]]
}

@test "an unknown component fails and lists the known ones" {
  cat > "$E2E_TMP/unknown.yaml" <<'YAML'
versions:
  nonesuch: 1.0.0
YAML
  run_release plan.py --manifest "$E2E_TMP/unknown.yaml"
  [ "$status" -eq 1 ]
  [[ "$output" == *"nonesuch"* ]]
}

@test "a version that does not advance is refused" {
  cat > "$E2E_TMP/backwards.yaml" <<'YAML'
versions:
  web: 0.0.1
YAML
  run_release plan.py --manifest "$E2E_TMP/backwards.yaml"
  [ "$status" -eq 1 ]
  [[ "$output" == *"does not advance"* ]]
}

@test "a non-patch bump on the release branch is refused" {
  cat > "$E2E_TMP/hotfix.yaml" <<'YAML'
branch: release
versions:
  web: 99.2.0
YAML
  run_release plan.py --manifest "$E2E_TMP/hotfix.yaml"
  [ "$status" -eq 1 ]
  [[ "$output" == *"hotfix"* ]] || [[ "$output" == *"patch"* ]]
}

@test "leaving a dependent out of the train warns but passes" {
  cat > "$E2E_TMP/web-only.yaml" <<'YAML'
versions:
  web: 99.1.0
YAML
  run_release plan.py --manifest "$E2E_TMP/web-only.yaml" --no-diff
  [ "$status" -eq 0 ]
  [[ "$output" == *"helm-charts"* ]]
}

@test "the same train fails under --strict" {
  cat > "$E2E_TMP/web-only.yaml" <<'YAML'
versions:
  web: 99.1.0
YAML
  run_release plan.py --manifest "$E2E_TMP/web-only.yaml" --no-diff --strict
  [ "$status" -eq 1 ]
}

@test "an empty manifest plans nothing" {
  cat > "$E2E_TMP/empty.yaml" <<'YAML'
versions: {}
YAML
  run_release plan.py --manifest "$E2E_TMP/empty.yaml" --json
  [ "$status" -eq 0 ]
}
```

- [ ] **Step 3: Install bats locally**

```bash
brew install bats-core   # macOS; on Linux use the distribution package
bats --version
```

Expected: 1.11 or newer. CI pins 1.13.0.

- [ ] **Step 4: Generate the golden file, then read it before accepting**

```bash
UPDATE_GOLDEN=1 bats scripts/release/tests/e2e/plan.bats
cat scripts/release/tests/e2e/golden/plan.json
```

Read the generated plan. It must show `web` bumping to `99.1.0`, `helm-charts` bumping to `99.1.0` in tier 2, and a `values.yaml` edit setting the web image tag. If it shows an empty plan or a problem, the fixtures or the manifest are wrong — fix them rather than accepting the golden.

- [ ] **Step 5: Run without the update flag**

```bash
bats scripts/release/tests/e2e/plan.bats
```

Expected: 8 passing.

- [ ] **Step 6: Prove the golden actually guards**

```bash
sed -i.bak 's/99\.1\.0/99.1.1/' scripts/release/tests/e2e/manifests/train.yaml
bats scripts/release/tests/e2e/plan.bats
```

Expected: the first test FAILS with a diff. Restore:

```bash
mv scripts/release/tests/e2e/manifests/train.yaml.bak scripts/release/tests/e2e/manifests/train.yaml
bats scripts/release/tests/e2e/plan.bats
```

Expected: 8 passing.

- [ ] **Step 7: Commit**

```bash
git add scripts/release/tests/e2e/
git commit -m "Run the planner end to end against replayed responses

The planner is what turns a manifest into the exact file edits the
cascade will make, so its JSON output is the most valuable thing to
pin. Also covers the four ways it refuses: an unknown component, a
version that does not advance, a non-patch bump on release, and a
dependent left out of the train under --strict."
```

---

### Task 14: Proposing a release end to end

**Files:**
- Create: `scripts/release/tests/e2e/propose.bats`
- Create: `scripts/release/tests/e2e/golden/manifest.yaml` (generated)
- Modify: `scripts/release/tests/e2e/record.sh` (add the pull request search)

- [ ] **Step 1: Extend the recorder with what propose reads**

Append to `record.sh` before the final `echo`:

```bash
echo "GitHub search"
for component in "${COMPONENTS[@]}"; do
  query="repo:$ORG/$component+is:pr+is:merged+base:main"
  capture "$GITHUB/search/issues?q=$query&per_page=100&page=1" \
          "$OUT/api.github.com/search/issues"
done
```

Note: every component overwrites the same file, so the search fixture answers identically for all of them. That is a deliberate simplification — the proposal's per-component labels are exercised, the per-component *difference* is not. Recorded in the commit message so nobody mistakes it for full coverage.

- [ ] **Step 2: Re-record**

```bash
scripts/release/tests/e2e/record.sh
```

Expected: the new `search/issues` entry appears.

- [ ] **Step 3: Write the tests**

`scripts/release/tests/e2e/propose.bats`:

```bash
#!/usr/bin/env bats
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

load helpers

setup_file() {
  load helpers
  start_server
}

teardown_file() {
  load helpers
  stop_server
}

@test "a proposal is written and nothing else is touched" {
  run_release propose.py -o "$E2E_TMP/manifest.yaml"
  [ "$status" -eq 0 ]
  [ -f "$E2E_TMP/manifest.yaml" ]
  assert_no_misses
}

@test "the proposal matches the golden" {
  run_release propose.py -o "$E2E_TMP/manifest.yaml"
  [ "$status" -eq 0 ]
  assert_golden manifest.yaml "$(cat "$E2E_TMP/manifest.yaml")"
}

@test "the proposal is a manifest the planner accepts" {
  run_release propose.py -o "$E2E_TMP/manifest.yaml"
  [ "$status" -eq 0 ]
  run_release plan.py --manifest "$E2E_TMP/manifest.yaml" --no-diff
  [ "$status" -eq 0 ]
}

@test "the proposal keeps the header comment" {
  run_release propose.py -o "$E2E_TMP/manifest.yaml"
  head -1 "$E2E_TMP/manifest.yaml" | grep -q '^#'
}
```

- [ ] **Step 4: Generate the golden and read it**

```bash
UPDATE_GOLDEN=1 bats scripts/release/tests/e2e/propose.bats
cat scripts/release/tests/e2e/golden/manifest.yaml
```

Read it. It must be valid YAML with a `versions:` mapping. An empty proposal is a legitimate outcome if the recording shows no merged pull requests since the last release — if so, note it and keep going; the third test still proves the round trip.

- [ ] **Step 5: Run and commit**

```bash
bats scripts/release/tests/e2e/propose.bats
git add scripts/release/tests/e2e/
git commit -m "Run the proposal end to end, and back through the planner

The third test is the one that matters: a proposal must be a manifest
the planner accepts. Those two scripts agreeing is the whole contract
between step one and step four of a release.

The search fixture is shared across components, so per-component
label differences are not exercised - only that labels are read and
resolved at all."
```

---

### Task 15: The reconciler end to end, including the gate

**Files:**
- Create: `scripts/release/tests/e2e/reconcile.bats`
- Create: `scripts/release/tests/e2e/scenarios/blocked-chart/...`
- Modify: `scripts/release/tests/e2e/record.sh` (add the self-repo pull request reads)

- [ ] **Step 1: Extend the recorder**

Append to `record.sh` before the final `echo`:

```bash
echo "self repository"
capture "$GITHUB/repos/$ORG/.github/pulls?state=open" \
        "$OUT/api.github.com/repos/$ORG/.github/pulls"
```

Re-record with `scripts/release/tests/e2e/record.sh`.

- [ ] **Step 2: Create the blocked-chart scenario**

```bash
mkdir -p scripts/release/tests/e2e/scenarios/blocked-chart/ghcr.io/v2/trento-project/trento-web/manifests
touch scripts/release/tests/e2e/scenarios/blocked-chart/ghcr.io/v2/trento-project/trento-web/manifests/99.1.0.404
```

The empty `.404` file makes the stub answer 404 for that manifest, so `ghcr_tag_exists` reports the web image as unpublished and the chart must not be bumped.

- [ ] **Step 3: Write the tests**

`scripts/release/tests/e2e/reconcile.bats`:

```bash
#!/usr/bin/env bats
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

load helpers

setup_file() {
  load helpers
  start_server
}

teardown_file() {
  load helpers
  stop_server
}

@test "a dry run writes nothing" {
  run_release reconcile.py --manifest "$E2E_DIR/manifests/train.yaml" --json --no-comment
  assert_no_misses
  echo "$output" | "$PYTHON" -c '
import json, sys
result = json.load(sys.stdin)
assert result["actions"] == [], result["actions"]
'
}

@test "a dry run does not claim to be acting" {
  run_release reconcile.py --manifest "$E2E_DIR/manifests/train.yaml" --json --no-comment
  echo "$output" | "$PYTHON" -c '
import json, sys
assert json.load(sys.stdin)["acting"] is False
'
}

@test "the human output is the progress comment" {
  run_release reconcile.py --manifest "$E2E_DIR/manifests/train.yaml" --no-comment
  [[ "$output" == *"web"* ]]
  [[ "$output" == *"helm-charts"* ]]
}

@test "every component is reported with a state" {
  run_release reconcile.py --manifest "$E2E_DIR/manifests/train.yaml" --json --no-comment
  echo "$output" | "$PYTHON" -c '
import json, sys
result = json.load(sys.stdin)
known = {"todo", "open", "merged", "released", "blocked", "abandoned"}
for component in result["components"]:
    assert component["state"] in known, component
assert result["components"], "no components observed"
'
}

@test "--execute without the gate still writes nothing" {
  # The gate is the release authorisation. There is no approved,
  # labelled manifest pull request in the recording, so even an
  # explicit --execute must be inert.
  run_release reconcile.py --manifest "$E2E_DIR/manifests/train.yaml" \
    --json --no-comment --execute
  echo "$output" | "$PYTHON" -c '
import json, sys
result = json.load(sys.stdin)
assert result["acting"] is False, result["gate"]
assert result["actions"] == [], result["actions"]
'
}
```

- [ ] **Step 4: Write the blocked-chart test in its own file**

A scenario needs a different server, so it needs its own `setup_file`.

`scripts/release/tests/e2e/reconcile-blocked.bats`:

```bash
#!/usr/bin/env bats
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

load helpers

setup_file() {
  load helpers
  start_server blocked-chart
}

teardown_file() {
  load helpers
  stop_server
}

@test "an unpublished web image keeps the chart from being bumped" {
  run_release reconcile.py --manifest "$E2E_DIR/manifests/train.yaml" \
    --json --no-comment --execute
  echo "$output" | "$PYTHON" -c '
import json, sys
result = json.load(sys.stdin)
chart = next(c for c in result["components"] if c["name"] == "helm-charts")
assert chart["state"] in {"blocked", "todo"}, chart
assert not any("helm-charts" in action for action in result["actions"]), result["actions"]
'
}
```

- [ ] **Step 5: Run both**

```bash
bats scripts/release/tests/e2e/reconcile.bats scripts/release/tests/e2e/reconcile-blocked.bats
```

Expected: 6 passing. If `--execute` produces actions, **stop** — that is the approval gate failing open, which is the single most serious defect this suite could find. Report it before changing anything.

- [ ] **Step 6: Commit**

```bash
git add scripts/release/tests/e2e/
git commit -m "Prove the reconciler will not act without the gate

The most valuable assertion in the suite: --execute against a
recording with no approved, labelled manifest pull request must
produce an empty actions list. A gate that fails open opens eight
pull requests and merges them.

The blocked-chart scenario shadows one GHCR manifest with a 404, so
the web image reads as unpublished and the chart must stay put."
```

---

### Task 16: The status table and the state collector end to end

**Files:**
- Create: `scripts/release/tests/e2e/table.bats`
- Create: `scripts/release/tests/e2e/golden/state.json` (generated)
- Create: `scripts/release/tests/e2e/golden/table.md` (generated)

- [ ] **Step 1: Write the tests**

`scripts/release/tests/e2e/table.bats`:

```bash
#!/usr/bin/env bats
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

load helpers

setup_file() {
  load helpers
  start_server
}

teardown_file() {
  load helpers
  stop_server
}

@test "the collector produces the recorded state" {
  run_release collect_state.py
  [ "$status" -eq 0 ]
  assert_no_misses
  # generated_at is a wall clock and would differ every run.
  filtered="$(echo "$output" | "$PYTHON" -c '
import json, sys
state = json.load(sys.stdin)
state.pop("generated_at", None)
print(json.dumps(state, indent=2, sort_keys=True))
')"
  assert_golden state.json "$filtered"
}

@test "the table renders from a state file" {
  run_release collect_state.py
  [ "$status" -eq 0 ]
  echo "$output" > "$E2E_TMP/state.json"
  run_release render_table.py --state "$E2E_TMP/state.json"
  [ "$status" -eq 0 ]
  filtered="$(echo "$output" | sed '/^<sub>Generated from/d')"
  assert_golden table.md "$filtered"
}

@test "the table names the projects the configuration gives it" {
  run_release collect_state.py
  echo "$output" > "$E2E_TMP/state.json"
  run_release render_table.py --state "$E2E_TMP/state.json"
  [[ "$output" == *"devel:sap:trento"* ]]
}

@test "an OBS project override reaches the table" {
  run_release collect_state.py
  echo "$output" > "$E2E_TMP/state.json"
  run env GITHUB_TOKEN= TRENTO_OBS_PROJECT_STABLE=home:someone:trento \
    "$PYTHON" "$REPO_ROOT/scripts/release/render_table.py" \
    --components "$E2E_TMP/components.yaml" --state "$E2E_TMP/state.json"
  [ "$status" -eq 0 ]
  [[ "$output" == *"home:someone:trento"* ]]
}

@test "injecting twice is a no-op the second time" {
  run_release collect_state.py
  echo "$output" > "$E2E_TMP/state.json"
  printf '# Title\n' > "$E2E_TMP/README.md"

  run_release render_table.py --state "$E2E_TMP/state.json" --inject "$E2E_TMP/README.md"
  [ "$status" -eq 0 ]
  first="$(cat "$E2E_TMP/README.md")"

  run_release render_table.py --state "$E2E_TMP/state.json" --inject "$E2E_TMP/README.md"
  [ "$status" -eq 0 ]
  [ "$first" = "$(cat "$E2E_TMP/README.md")" ]
}

@test "--check passes against a file already up to date" {
  run_release collect_state.py
  echo "$output" > "$E2E_TMP/state.json"
  printf '# Title\n' > "$E2E_TMP/README.md"
  run_release render_table.py --state "$E2E_TMP/state.json" --inject "$E2E_TMP/README.md"
  run_release render_table.py --state "$E2E_TMP/state.json" --inject "$E2E_TMP/README.md" --check
  [ "$status" -eq 0 ]
}

@test "--check fails against a file that would change" {
  run_release collect_state.py
  echo "$output" > "$E2E_TMP/state.json"
  printf '# Title\n' > "$E2E_TMP/README.md"
  run_release render_table.py --state "$E2E_TMP/state.json" --inject "$E2E_TMP/README.md" --check
  [ "$status" -eq 1 ]
}
```

- [ ] **Step 2: Generate the goldens and read them**

```bash
UPDATE_GOLDEN=1 bats scripts/release/tests/e2e/table.bats
cat scripts/release/tests/e2e/golden/table.md
```

Read the table. Every component should have a GitHub version; OBS and SLES columns may hold em dashes where the recording had none. An entirely em-dashed table means the fixtures are not being found — check for `MISS` lines before accepting.

- [ ] **Step 3: Run and commit**

```bash
bats scripts/release/tests/e2e/table.bats
git add scripts/release/tests/e2e/
git commit -m "Run the status table end to end

Covers the two behaviours the daily workflow depends on: injecting
the same content twice must not produce a second commit, and --check
must distinguish a file that is up to date from one that would
change. Both hinge on the timestamp being stripped before comparison.

Also proves an OBS project organisation variable reaches the rendered
table, which is what keeps it from naming a project nothing publishes
to."
```

---

### Task 17: Variables, labels and the OBS fork, end to end

**Files:**
- Create: `scripts/release/tests/e2e/variables.bats`
- Create: `scripts/release/tests/e2e/variables-unreadable.bats`
- Create: `scripts/release/tests/e2e/fork-obs.bats`
- Create: `scripts/release/tests/e2e/scenarios/variables-unreadable/...`
- Modify: `scripts/release/tests/e2e/record.sh` (add the variables endpoint)

- [ ] **Step 1: Extend the recorder**

Append to `record.sh` before the final `echo`:

```bash
echo "repository variables"
for component in "${COMPONENTS[@]}"; do
  capture "$GITHUB/repos/$ORG/$component/actions/variables?per_page=100" \
          "$OUT/api.github.com/repos/$ORG/$component/actions/variables"
done
```

Re-record. Anonymous requests will return 403, so this writes `.403` sidecars — which is exactly the unreadable case. Hand-write the readable base instead:

```bash
for component in web wanda checks agent helm-charts; do
  case "$component" in
    web) package=trento-web ;;
    wanda) package=trento-wanda ;;
    checks) package=trento-checks ;;
    agent) package=trento-agent ;;
    helm-charts) package=trento-server-helm ;;
  esac
  target="scripts/release/tests/e2e/recorded/api.github.com/repos/trento-project/$component/actions/variables"
  rm -f "$target".403
  cat > "$target.json" <<JSON
{"total_count":2,"variables":[
  {"name":"OBS_PACKAGE","value":"$package"},
  {"name":"OBS_ENABLED","value":"true"}
]}
JSON
done
```

- [ ] **Step 2: Create the drift and unreadable scenarios**

```bash
mkdir -p scripts/release/tests/e2e/scenarios/variables-drift/api.github.com/repos/trento-project/web/actions
cat > scripts/release/tests/e2e/scenarios/variables-drift/api.github.com/repos/trento-project/web/actions/variables.json <<'JSON'
{"total_count":2,"variables":[
  {"name":"OBS_PACKAGE","value":"trento-webb"},
  {"name":"OBS_ENABLED","value":"true"}
]}
JSON

mkdir -p scripts/release/tests/e2e/scenarios/variables-unreadable/api.github.com/repos/trento-project/web/actions
touch scripts/release/tests/e2e/scenarios/variables-unreadable/api.github.com/repos/trento-project/web/actions/variables.403
```

- [ ] **Step 3: Write the variables tests**

`scripts/release/tests/e2e/variables.bats`:

```bash
#!/usr/bin/env bats
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

load helpers

setup_file() {
  load helpers
  start_server
}

teardown_file() {
  load helpers
  stop_server
}

@test "matching variables report no disagreement" {
  run_release check_variables.py
  [ "$status" -eq 0 ]
  [[ "$output" == *"0 disagreement(s)"* ]]
  assert_no_misses
}
```

`scripts/release/tests/e2e/variables-drift.bats`:

```bash
#!/usr/bin/env bats
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

load helpers

setup_file() {
  load helpers
  start_server variables-drift
}

teardown_file() {
  load helpers
  stop_server
}

@test "a package name that disagrees fails and names both values" {
  run_release check_variables.py
  [ "$status" -eq 1 ]
  [[ "$output" == *"trento-web"* ]]
  [[ "$output" == *"trento-webb"* ]]
}
```

`scripts/release/tests/e2e/variables-unreadable.bats`:

```bash
#!/usr/bin/env bats
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

load helpers

setup_file() {
  load helpers
  start_server variables-unreadable
}

teardown_file() {
  load helpers
  stop_server
}

@test "variables that cannot be read say so and do not fail the build" {
  # A fork has no permission to read repository variables. Failing
  # there would make the check useless exactly where the cascade is
  # rehearsed.
  run_release check_variables.py
  [ "$status" -eq 0 ]
  [[ "$output" == *"could not read"* ]]
  [[ "$output" == *"web"* ]]
}
```

- [ ] **Step 4: Write the fork-obs tests**

`scripts/release/tests/e2e/fork-obs.bats`:

```bash
#!/usr/bin/env bats
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

load helpers

setup_file() {
  load helpers
  start_server
}

teardown_file() {
  load helpers
  stop_server
}

@test "a dry run needs no credentials and writes nothing" {
  run env GITHUB_TOKEN= GH_TOKEN= \
    "$PYTHON" "$REPO_ROOT/scripts/release/fork_obs.py" \
    --components "$E2E_TMP/components.yaml" \
    --obs-api "http://127.0.0.1:$E2E_PORT" \
    --git-owner someone
  [ "$status" -eq 0 ]
  [[ "$output" != *"OBS_USER"* ]] || [[ "$output" == *"gh variable set"* ]]
}

@test "a dry run prints the variables the fork needs" {
  run env GITHUB_TOKEN= GH_TOKEN= \
    "$PYTHON" "$REPO_ROOT/scripts/release/fork_obs.py" \
    --components "$E2E_TMP/components.yaml" \
    --obs-api "http://127.0.0.1:$E2E_PORT" \
    --git-owner someone
  [[ "$output" == *"OBS_PROJECT_STABLE"* ]]
  [[ "$output" == *"OBS_PROJECT_ROLLING"* ]]
}

@test "a refusal is reported as a refusal, not as nothing to do" {
  # The distinction that cost a debugging session: IBS answers 404 on
  # /public where OBS answers 401, so an unauthenticated run looks
  # like an empty one unless the refusal is surfaced.
  mkdir -p "$E2E_TMP/refused/api.refused.invalid"
  run env GITHUB_TOKEN= GH_TOKEN= \
    "$PYTHON" "$REPO_ROOT/scripts/release/fork_obs.py" \
    --components "$E2E_TMP/components.yaml" \
    --obs-api "http://127.0.0.1:1" \
    --git-owner someone
  [ "$status" -ne 0 ]
}
```

- [ ] **Step 5: Run every bats file**

```bash
bats scripts/release/tests/e2e/*.bats
```

Expected: all pass. The `fork-obs` assertions are looser than the rest because that script's output depends on what the recording contains for the OBS source paths — if a test fails, read the actual output and tighten the assertion to what it genuinely proves rather than deleting it.

- [ ] **Step 6: Commit**

```bash
git add scripts/release/tests/e2e/
git commit -m "Cover the drift check and the OBS fork end to end

Three states for the variables check, because they have three
different right answers: agreement passes, drift fails and names both
values, and variables that cannot be read pass with an explanation.
That last one is what a fork does, and failing there would make the
check useless exactly where the cascade is rehearsed.

The fork script's refusal case is recorded because IBS answers 404 on
/public where OBS answers 401, so an unauthenticated run looks
identical to an empty one."
```

---

### Task 18: Wire it into CI and document it

**Files:**
- Create: `.github/workflows/release-tests.yaml`
- Modify: `docs/release-automation.md`

- [ ] **Step 1: Write the workflow**

`.github/workflows/release-tests.yaml`:

```yaml
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

name: Release tooling tests

on:
  pull_request:
    paths:
      - 'scripts/release/**'
      - 'release/**'
      - 'pyproject.toml'
      - '.github/workflows/release-tests.yaml'
  push:
    branches: [main]
    paths:
      - 'scripts/release/**'
      - 'release/**'
      - 'pyproject.toml'
      - '.github/workflows/release-tests.yaml'
  workflow_dispatch:

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

env:
  BATS_VERSION: "1.13.0" # https://github.com/bats-core/bats-core/releases

# Nothing here reaches the network or writes anything.
permissions:
  contents: read

jobs:
  unit:
    name: Unit
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6.0.0
      - name: Install dependencies
        run: |
          python3 -m venv .venv-dev
          .venv-dev/bin/pip install --quiet --disable-pip-version-check \
            -r scripts/release/requirements-dev.txt
      - name: Run pytest
        run: .venv-dev/bin/pytest

  e2e:
    name: End to end
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6.0.0
      - name: Install dependencies
        run: |
          python3 -m venv .venv-dev
          .venv-dev/bin/pip install --quiet --disable-pip-version-check \
            -r scripts/release/requirements-dev.txt
      - name: Setup BATS
        uses: bats-core/bats-action@77d6fb60505b4d0d1d73e48bd035b55074bbfb43 # v4.0.0
        with:
          bats-version: ${{ env.BATS_VERSION }}
      - name: Run bats
        run: bats scripts/release/tests/e2e/*.bats
```

- [ ] **Step 2: Check the checkout SHA matches the rest of the repository**

```bash
grep -h 'actions/checkout@' .github/workflows/*.yaml | sort -u
```

Expected: one line. If `release-tests.yaml` differs from the others, change it to match — a repository pinning two different checkout versions is a review finding waiting to happen.

- [ ] **Step 3: Validate the YAML**

```bash
.venv-dev/bin/python -c "
from ruamel.yaml import YAML
from pathlib import Path
for path in sorted(Path('.github/workflows').glob('*.yaml')):
    YAML(typ='safe').load(path.read_text(encoding='utf-8'))
    print('ok', path.name)
"
```

Expected: `ok` for every file including the new one.

- [ ] **Step 4: Run the whole suite once as CI will**

```bash
.venv-dev/bin/pytest
bats scripts/release/tests/e2e/*.bats
```

Expected: both green.

- [ ] **Step 5: Document it**

In `docs/release-automation.md`, add before the `## The status table` section:

```markdown
## Tests

```bash
python3 -m venv .venv-dev
.venv-dev/bin/pip install -r scripts/release/requirements-dev.txt

.venv-dev/bin/pytest                              # rules, in-process, seconds
bats scripts/release/tests/e2e/*.bats             # the cascade, black box
```

Two layers. The unit tests call functions directly with synthetic
input. The end-to-end tests run each script as a subprocess against a
stub HTTP server replaying recorded responses, with every API in a
test `components.yaml` pointed at it, so what is tested is the command
the workflow runs.

`scripts/release/tests/e2e/record.sh` re-captures the recorded
responses from the live APIs. It is run by hand, never in CI: reaching
upstream on a schedule would turn somebody else's change into a red
build here. `UPDATE_GOLDEN=1` regenerates the expected outputs — read
the diff before committing it, because a golden accepted without
reading proves nothing.

A scenario directory under `tests/e2e/scenarios/` shadows individual
recorded paths, which is how the states that do not exist in a
snapshot — a blocked chart, unreadable variables — are reached without
a second recording.
```

Also add a row to the dry-run table:

```markdown
| `tests/e2e/record.sh` | reads the live APIs and writes fixtures. The one script here that is not a dry run by default |
```

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/release-tests.yaml docs/release-automation.md
git commit -m "Run the tests on every change to the release tooling

Two jobs, matching how helm-charts already runs its CI script tests:
pytest for the rules and bats for the cascade, both offline.

record.sh is deliberately absent from the workflow. It reaches the
live APIs, and running it on a schedule would turn an upstream change
into a red build on an unrelated pull request."
```

---

## Self-Review

**Spec coverage.** Every section of `docs/specs/2026-08-11-release-tooling-tests.md` maps to a task:

| spec section | task |
| --- | --- |
| Layout | 1, 11, 12 |
| Stub server | 11 |
| Overlays | 15, 17 |
| Recording | 12 |
| Unit: version algebra | 1 |
| Unit: label resolution | 4 |
| Unit: YAML line surgery | 2 |
| Unit: SCC parsing | 5 |
| Unit: config loading | 3 |
| Unit: reconciler and write safety | 7 |
| Unit: remaining pure functions | 6 |
| Decisions pinned (prerelease bump, non-advancing guard) | 1, 13 |
| E2E: plan | 13 |
| E2E: propose | 14 |
| E2E: reconcile and blocked chart | 15 |
| E2E: table and collect_state | 16 |
| E2E: variables, three states | 17 |
| E2E: fork_obs | 17 |
| Determinism | 13 step 4, 16 step 1 |
| Production: GHCR address | 8 |
| Production: token silenceable | 9 |
| Production: render_table --components | 10 |
| Production: pyproject, requirements-dev | 1 |
| CI | 18 |
| Out of scope: trento-release | not planned, correctly |

**Known gaps, stated rather than hidden:**

- The `propose` search fixture is shared across all components, so per-component label differences are exercised only in the unit layer. Recorded in Task 14's commit message.
- `tests/e2e/components.yaml.template` lists five components, not eight. Both tiers and a dependency edge are covered; adding the rest is a fixture edit.
- `abandoned` gains a test but no `STATE_ICON` entry. Deliberate — a testing change is the wrong place to alter the progress comment.

**Type consistency.** `FakeGitHub` is defined in Task 1 and imported in Task 7 under the same name from `conftest`. `Config.ghcr_api` is introduced in Task 8 and consumed by Task 12's `components.yaml.template`. `start_server`, `stop_server`, `run_release`, `assert_no_misses` and `assert_golden` are defined in Task 12's `helpers.bash` and used unchanged in Tasks 13 through 17. `server.py`'s `--root`, `--scenario`, `--port-file` flags are defined in Task 11 and called with those exact names in Task 12.

**Ordering constraint.** Tasks 13 to 17 all depend on Tasks 11 and 12. Task 16 depends on Task 10, and Task 15 depends on Task 8. Tasks 1 to 7 are independent of everything after them and can be done in any order.
