# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

"""Shared plumbing for the release status table.

Everything here is read-only against the outside world: the GitHub
client has no mutating methods, and the OBS and SCC readers use the
public endpoints of each. Nothing in this file needs a credential.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_DIR = REPO_ROOT / "release"
COMPONENTS_FILE = RELEASE_DIR / "components.yaml"

USER_AGENT = "trento-release-tooling"
HTTP_RETRIES = 3
HTTP_BACKOFF = 2.0


def log(message: str) -> None:
    print(message, file=sys.stderr)


# --------------------------------------------------------------------
# YAML
# --------------------------------------------------------------------


def yaml_handle() -> YAML:
    """A round-trip YAML handle that leaves comments and quoting alone."""
    handle = YAML()
    handle.preserve_quotes = True
    handle.width = 4096
    handle.indent(mapping=2, sequence=4, offset=2)
    return handle


def load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as stream:
        return yaml_handle().load(stream)


# --------------------------------------------------------------------
# Semantic versions
# --------------------------------------------------------------------

SEMVER_RE = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?"
    r"(?:\+(?P<build>[0-9A-Za-z.-]+))?$"
)


def pre_release_key(pre: str | None) -> tuple:
    """Order pre-release identifiers the way the specification does.

    The dot-separated identifiers are compared one at a time. A numeric
    one compares numerically, so `beta.2` precedes `beta.11` rather than
    following it, and ranks below an alphanumeric one. Where every
    identifier matches, the longer pre-release is the greater.
    """
    if not pre:
        return ()
    return tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in pre.split("."))


@dataclass(frozen=True, order=False)
class Version:
    major: int
    minor: int
    patch: int
    pre: str | None = None
    build: str | None = None
    raw: str = ""

    @classmethod
    def parse(cls, text: str) -> "Version":
        match = SEMVER_RE.match(text.strip())
        if not match:
            raise ValueError(f"not a semantic version: {text!r}")
        return cls(
            major=int(match["major"]),
            minor=int(match["minor"]),
            patch=int(match["patch"]),
            pre=match["pre"],
            build=match["build"],
            raw=text.strip(),
        )

    @classmethod
    def parse_or_none(cls, text: str | None) -> "Version | None":
        if not text:
            return None
        try:
            return cls.parse(text)
        except ValueError:
            return None

    def _key(self) -> tuple:
        # A release outranks any of its pre-releases; build metadata is
        # ignored for ordering, as the specification requires.
        return (self.major, self.minor, self.patch, 1 if self.pre is None else 0, pre_release_key(self.pre))

    def __lt__(self, other: "Version") -> bool:
        return self._key() < other._key()

    def __le__(self, other: "Version") -> bool:
        return self._key() <= other._key()

    def __gt__(self, other: "Version") -> bool:
        return self._key() > other._key()

    def __str__(self) -> str:
        return self.raw or f"{self.major}.{self.minor}.{self.patch}"


# --------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} for {url}: {body[:400]}")
        self.status = status
        self.url = url
        self.body = body


def http_get(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    accept_missing: bool = False,
    timeout: int = 30,
) -> bytes | None:
    """GET with retries. Returns None on 404 when ``accept_missing``."""
    request_headers = {"User-Agent": USER_AGENT}
    request_headers.update(headers or {})
    last_error: Exception | None = None

    for attempt in range(HTTP_RETRIES):
        request = urllib.request.Request(url, headers=request_headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code == 404 and accept_missing:
                return None
            if error.code in (403, 429, 500, 502, 503, 504) and attempt < HTTP_RETRIES - 1:
                time.sleep(HTTP_BACKOFF * (attempt + 1))
                last_error = error
                continue
            raise HttpError(error.code, url, error.read().decode("utf-8", "replace")) from error
        except (urllib.error.URLError, TimeoutError) as error:
            last_error = error
            if attempt < HTTP_RETRIES - 1:
                time.sleep(HTTP_BACKOFF * (attempt + 1))
                continue
            raise

    raise RuntimeError(f"unreachable: {url} ({last_error})")


def http_get_json(url: str, **kwargs) -> Any:
    payload = http_get(url, **kwargs)
    return None if payload is None else json.loads(payload)


def http_get_text(url: str, **kwargs) -> str | None:
    payload = http_get(url, **kwargs)
    return None if payload is None else payload.decode("utf-8", "replace")


# --------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------


@dataclass
class Component:
    name: str
    repo: str
    obs_package: str | None
    obs_version_file: str | None
    scc_packages: list[str]


@dataclass
class Config:
    components: dict[str, Component]
    github_api: str
    github_org: str
    obs_api: str
    obs_projects: dict[str, str]
    scc_api: str
    scc_product_pattern: str
    scc_architectures: list[str]
    scc_max_products_per_major: int


def load_config(path: Path = COMPONENTS_FILE) -> Config:
    raw = load_yaml(path)
    sources = raw["sources"]
    defaults = raw.get("defaults", {})
    scc = sources["scc"]

    components: dict[str, Component] = {}
    for name, spec in raw["components"].items():
        spec = spec or {}
        obs_package = spec.get("obs_package")
        obs_version_file = spec.get("obs_version_file") or (
            defaults.get("obs_version_file", "{obs_package}.spec").format(obs_package=obs_package)
            if obs_package
            else None
        )
        components[name] = Component(
            name=name,
            repo=spec.get("repo", name),
            obs_package=obs_package,
            obs_version_file=obs_version_file,
            scc_packages=list(spec.get("scc_packages") or ([obs_package] if obs_package else [])),
        )

    # Overridable so a fork renders its own table rather than
    # trento-project's, and does not produce a diff that reads like
    # upstream regressed.
    org = os.environ.get("TRENTO_GITHUB_ORG") or sources["github"]["org"]

    # The OBS projects are named by the organisation variables the
    # component release workflows already build their obs-sync matrix
    # from, and by nothing else. A project cannot be inferred the way the
    # GitHub organisation can: OBS is keyed on an OBS account, which has
    # no relationship to a GitHub owner. So a run with no variables in
    # scope reports no OBS columns, rather than someone else's.
    obs_projects: dict[str, str] = {}
    for key, variable in (("stable", "OBS_PROJECT_STABLE"), ("rolling", "OBS_PROJECT_ROLLING")):
        override = os.environ.get(f"TRENTO_{variable}")
        if override:
            obs_projects[key] = override
    if not obs_projects:
        log("! no OBS project in scope: set TRENTO_OBS_PROJECT_STABLE and")
        log("! TRENTO_OBS_PROJECT_ROLLING, or the table has no OBS columns")

    return Config(
        components=components,
        github_api=sources["github"]["api"].rstrip("/"),
        github_org=org,
        obs_api=sources["obs"]["api"].rstrip("/"),
        obs_projects=obs_projects,
        scc_api=scc["api"].rstrip("/"),
        scc_product_pattern=scc["product_pattern"],
        scc_architectures=list(scc.get("architectures") or ["x86_64"]),
        scc_max_products_per_major=int(scc.get("max_products_per_major", 3)),
    )


# --------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------


def github_token() -> str | None:
    """A token, if one is to be had. Only ever raises the rate limit."""
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        token = os.environ.get(name)
        if token:
            return token
    # Local convenience: fall back to the gh CLI's own credentials so
    # the scripts are runnable on a developer machine without exporting
    # anything.
    gh = shutil.which("gh")
    if gh:
        try:
            result = subprocess.run(
                [gh, "auth", "token"], capture_output=True, text=True, timeout=15, check=False
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return None


class GitHub:
    """A read-only GitHub REST client. It has no mutating methods."""

    def __init__(self, api: str, org: str, *, token: str | None = None):
        self.api = api.rstrip("/")
        self.org = org
        self.token = token if token is not None else github_token()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def get(self, path: str, *, accept_missing: bool = False) -> Any:
        url = path if path.startswith("http") else f"{self.api}/{path.lstrip('/')}"
        return http_get_json(url, headers=self._headers(), accept_missing=accept_missing)

    def latest_release(self, repo: str) -> dict | None:
        return self.get(f"repos/{self.org}/{repo}/releases/latest", accept_missing=True)


# --------------------------------------------------------------------
# OBS and SCC
# --------------------------------------------------------------------

_VERSION_LINE_RE = re.compile(r"^version:\s*(\S+)", re.IGNORECASE | re.MULTILINE)


def obs_package_version(config: Config, project: str, package: str, version_file: str) -> str | None:
    """Read a package version straight out of the public OBS source API.

    The two-case rule (spec file, otherwise Chart.yaml) is the same one
    obs-sync.yaml already applies when it commits to OBS.
    """
    url = (
        f"{config.obs_api}/public/source/{urllib.parse.quote(project)}"
        f"/{urllib.parse.quote(package)}/{urllib.parse.quote(version_file)}"
    )
    try:
        text = http_get_text(url, accept_missing=True)
    except HttpError as error:
        log(f"  ! OBS {project}/{package}/{version_file}: {error}")
        return None
    if text is None:
        return None
    match = _VERSION_LINE_RE.search(text)
    return match.group(1) if match else None


def product_release(product: dict) -> tuple[int, int]:
    """The (major, minor) of a SCC product, taken from its identifier.

    The ``version`` field is a display string and is not always dotted:
    SLES_SAP/15.7 reports "15 SP7". The identifier is the stable one.
    """
    parts = product.get("identifier", "").split("/")
    version = parts[1] if len(parts) > 1 else "0"
    numbers = version.split(".")
    major = int(numbers[0]) if numbers[0].isdigit() else 0
    minor = int(numbers[1]) if len(numbers) > 1 and numbers[1].isdigit() else 0
    return major, minor


def product_label(product: dict) -> str:
    parts = product.get("identifier", "").split("/")
    return parts[1] if len(parts) > 1 else product.get("identifier", "?")


def scc_products(config: Config) -> list[dict]:
    payload = http_get_json(f"{config.scc_api}/api/package_search/products")
    pattern = re.compile(config.scc_product_pattern)
    products = [
        product
        for product in payload.get("data", [])
        if pattern.match(product.get("identifier", ""))
        and product.get("architecture") in config.scc_architectures
    ]
    return sorted(products, key=product_release, reverse=True)


def scc_packages_for_product(config: Config, product_id: int, query: str = "trento") -> list[dict]:
    """Every Trento package SCC knows about for one product.

    One request per product rather than per package: every Trento
    package name contains "trento", including supportutils-plugin-trento
    and mcp-server-trento, and SCC matches on substring.
    """
    url = f"{config.scc_api}/api/package_search/packages?product_id={product_id}&query={query}"
    payload = http_get_json(url, accept_missing=True) or {}
    return payload.get("data", []) or []
