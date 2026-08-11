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
