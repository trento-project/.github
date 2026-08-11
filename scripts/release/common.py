# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

"""Shared plumbing for the Trento release scripts.

Everything here is read-only against the outside world. The only module
that mutates anything is reconcile.py, and it does so through the
GitHub client defined at the bottom of this file, which refuses to
write while ``dry_run`` is set.
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_DIR = REPO_ROOT / "release"
COMPONENTS_FILE = RELEASE_DIR / "components.yaml"
MANIFEST_FILE = RELEASE_DIR / "manifest.yaml"
LABELS_FILE = RELEASE_DIR / "labels.yaml"

USER_AGENT = "trento-release-tooling"
HTTP_RETRIES = 3
HTTP_BACKOFF = 2.0

# This repository, where the manifest and the orchestration labels live.
# Overridable because a fork of `.github` cannot itself be called
# `.github`, and the whole cascade is worth rehearsing on forks first.
SELF_REPO = os.environ.get("TRENTO_SELF_REPO") or ".github"


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


def dump_yaml(data: Any, path: Path) -> None:
    with path.open("w", encoding="utf-8") as stream:
        yaml_handle().dump(data, stream)


def dump_yaml_str(data: Any) -> str:
    import io

    buffer = io.StringIO()
    yaml_handle().dump(data, buffer)
    return buffer.getvalue()


def load_yaml_str(text: str) -> Any:
    return yaml_handle().load(text)


class MissingKey(Exception):
    """A dotted key named in components.yaml is not in the target file."""


def get_dotted(data: Any, dotted_key: str) -> Any:
    """Read ``image.tag`` out of a nested mapping."""
    node = data
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            raise MissingKey(dotted_key)
        node = node[part]
    return node


# A plain scalar, optionally quoted, up to a comment or end of line. The
# value is non-greedy and the comment/end-of-line is a lookahead rather
# than part of the match: YAML only starts a comment at a `#` preceded by
# whitespace, so that whitespace is the tail's, not the value's. Consuming
# it here would leave a bumped `1.2.3  # pinned` as `1.3.0# pinned`, which
# is a different, un-commented scalar.
_SCALAR_RE = re.compile(r"""^(?P<quote>['"]?)(?P<value>[^'"#\n]*?)(?P=quote)(?=[ \t]*(?:#|$))""")


def replace_yaml_value(text: str, dotted_key: str, value: str) -> str:
    """Rewrite one scalar in a YAML document, touching nothing else.

    ruamel is used only to locate the key. Dumping the parsed document
    back out would be easier but it is not faithful: it renders an
    explicit ``null`` as an empty value, so bumping one image tag in
    trento-web/values.yaml would silently rewrite seven unrelated
    ``alerting`` keys. Editing the single line keeps the diff to the
    line that actually changed.
    """
    document = yaml_handle().load(text)

    parts = dotted_key.split(".")
    node = document
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            raise MissingKey(dotted_key)
        node = node[part]
    if not isinstance(node, dict) or parts[-1] not in node:
        raise MissingKey(dotted_key)

    # A mapping or sequence parses to a dict or list whether it is written
    # as a block or in flow style (`tags: [a, b]` is exactly as much a
    # list as an indented `- a`), and flow style puts its mark on the
    # key's own line, so the line check below cannot see it - the regex
    # matched `[a, b]` or `{tag: 1.2.3}` just as happily as it matched
    # `1.2.3`. Checking the parsed type first closes that off before any
    # position work happens.
    if isinstance(node[parts[-1]], (dict, list)):
        raise MissingKey(f"{dotted_key}: value is not a plain scalar")

    try:
        key_line, _ = node.lc.key(parts[-1])
        line_number, column = node.lc.value(parts[-1])
    except (AttributeError, KeyError, TypeError) as error:
        raise MissingKey(f"{dotted_key}: no source position") from error

    # An implicit null (`key:` with nothing after it) has no token of its
    # own, so ruamel marks it with the position of whatever comes next -
    # a different line, or one past the end of the document if the key is
    # last. An alias (`tag: *b`) is marked at its anchor's definition,
    # which can be a different line entirely too. Either way, a value
    # mark off the key's own line is not a place this function can edit.
    if line_number != key_line:
        raise MissingKey(f"{dotted_key}: value is not a plain scalar")

    lines = text.splitlines(keepends=True)
    line = lines[line_number]
    match = _SCALAR_RE.match(line[column:])
    if not match:
        raise MissingKey(f"{dotted_key}: value is not a plain scalar")

    quote = match.group("quote")
    if not quote and match.end() == 0:
        # An implicit null can still land on the key's own line when a
        # comment follows it (`key:  # note`): the mark sits in the
        # whitespace before the comment, so the line check above does not
        # catch it. A quoted empty string (`tag: ""`) also matches
        # zero-width for its value, but its quote group is not empty -
        # this only rejects the unquoted, tokenless case.
        raise MissingKey(f"{dotted_key}: value is not a plain scalar")

    head = line[:column]
    tail = line[column + match.end() :]
    lines[line_number] = f"{head}{quote}{value}{quote}{tail}"
    return "".join(lines)


# --------------------------------------------------------------------
# Semantic versions
# --------------------------------------------------------------------

SEMVER_RE = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?"
    r"(?:\+(?P<build>[0-9A-Za-z.-]+))?$"
)


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

    def bump(self, kind: str) -> "Version":
        if kind == "major":
            return Version(self.major + 1, 0, 0, raw=f"{self.major + 1}.0.0")
        if kind == "minor":
            return Version(
                self.major, self.minor + 1, 0, raw=f"{self.major}.{self.minor + 1}.0"
            )
        if kind == "patch":
            return Version(
                self.major,
                self.minor,
                self.patch + 1,
                raw=f"{self.major}.{self.minor}.{self.patch + 1}",
            )
        raise ValueError(f"unknown bump kind: {kind!r}")

    def _key(self) -> tuple:
        # A release outranks any of its pre-releases; build metadata is
        # ignored for ordering, as the specification requires.
        return (self.major, self.minor, self.patch, 1 if self.pre is None else 0, self.pre or "")

    def __lt__(self, other: "Version") -> bool:
        return self._key() < other._key()

    def __le__(self, other: "Version") -> bool:
        return self._key() <= other._key()

    def __gt__(self, other: "Version") -> bool:
        return self._key() > other._key()

    def __str__(self) -> str:
        return self.raw or f"{self.major}.{self.minor}.{self.patch}"


def bump_kind_between(old: Version, new: Version) -> str:
    if new.major != old.major:
        return "major"
    if new.minor != old.minor:
        return "minor"
    return "patch"


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
class Bump:
    path: str
    kind: str
    key: str | None = None
    source: str | None = None


@dataclass
class Component:
    name: str
    repo: str
    tier: int
    obs_package: str | None
    obs_version_file: str | None
    obs_image_package: str | None
    ghcr_image: str | None
    scc_packages: list[str]
    depends_on: list[str]
    bumps: list[Bump]
    version_file: str


@dataclass
class Config:
    raw: Any
    components: dict[str, Component]
    github_api: str
    github_org: str
    obs_api: str
    obs_projects: dict[str, str]
    scc_api: str
    scc_product_pattern: str
    scc_architectures: list[str]
    scc_max_products_per_major: int
    ghcr_registry: str
    ghcr_namespace: str
    release_branches: list[str]

    def component(self, name: str) -> Component:
        try:
            return self.components[name]
        except KeyError:
            known = ", ".join(sorted(self.components))
            raise KeyError(f"unknown component {name!r}; known components: {known}") from None

    def tiers(self) -> list[int]:
        return sorted({component.tier for component in self.components.values()})


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
        bumps = [
            Bump(
                path=entry["path"],
                kind=entry.get("kind", "plain"),
                key=entry.get("key"),
                source=entry.get("source"),
            )
            for entry in spec.get("bumps", [])
        ] or [Bump(path=defaults.get("version_file", "VERSION"), kind="plain")]

        components[name] = Component(
            name=name,
            repo=spec.get("repo", name),
            tier=int(spec.get("tier", defaults.get("tier", 1))),
            obs_package=obs_package,
            obs_version_file=obs_version_file,
            obs_image_package=spec.get("obs_image_package"),
            ghcr_image=spec.get("ghcr_image"),
            scc_packages=list(spec.get("scc_packages") or ([obs_package] if obs_package else [])),
            depends_on=list(spec.get("depends_on") or []),
            bumps=bumps,
            version_file=spec.get("version_file", defaults.get("version_file", "VERSION")),
        )

    # Overridable so the whole cascade can be rehearsed against a set of
    # forks before it is ever pointed at trento-project. Nothing else
    # needs to change: every repository, package and image name is
    # derived from the component list.
    org = os.environ.get("TRENTO_GITHUB_ORG") or sources["github"]["org"]
    ghcr_namespace = os.environ.get("TRENTO_GHCR_NAMESPACE") or sources["ghcr"]["namespace"]

    # The two OBS projects are organisation variables that the component
    # release workflows already build their matrix from. Prefer them, so
    # the table cannot describe a project nothing publishes to; the
    # values below are the fallback for a run with no variables in
    # scope, such as a fork or a local dry run.
    obs_projects = dict(sources["obs"]["projects"])
    for key, variable in (("stable", "OBS_PROJECT_STABLE"), ("rolling", "OBS_PROJECT_ROLLING")):
        override = os.environ.get(f"TRENTO_{variable}")
        if override:
            obs_projects[key] = override

    return Config(
        raw=raw,
        components=components,
        github_api=sources["github"]["api"].rstrip("/"),
        github_org=org,
        obs_api=sources["obs"]["api"].rstrip("/"),
        obs_projects=obs_projects,
        scc_api=scc["api"].rstrip("/"),
        scc_product_pattern=scc["product_pattern"],
        scc_architectures=list(scc.get("architectures") or ["x86_64"]),
        scc_max_products_per_major=int(scc.get("max_products_per_major", 3)),
        ghcr_registry=sources["ghcr"]["registry"],
        ghcr_namespace=ghcr_namespace,
        release_branches=list(raw.get("release_branches", ["main", "release"])),
    )


# --------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------


# Not `release/<version>`: git cannot hold both a `release` branch and a
# `release/` directory of refs, and every component repository has the
# former.
BUMP_BRANCH_PREFIX = "release-bump"


def bump_branch(version: "Version | str") -> str:
    return f"{BUMP_BRANCH_PREFIX}/{version}"


@dataclass
class ManifestEntry:
    name: str
    version: Version
    branch: str


@dataclass
class Manifest:
    train: str | None
    entries: dict[str, ManifestEntry] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.entries


def load_manifest(path: Path = MANIFEST_FILE) -> Manifest:
    raw = load_yaml(path) or {}
    entries: dict[str, ManifestEntry] = {}
    for name, spec in (raw.get("components") or {}).items():
        spec = spec or {}
        entries[name] = ManifestEntry(
            name=name,
            version=Version.parse(str(spec["version"])),
            branch=str(spec.get("branch", "main")),
        )
    train = raw.get("train")
    return Manifest(train=None if train in (None, "null", "") else str(train), entries=entries)


# --------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------


def github_token() -> str | None:
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        token = os.environ.get(name)
        if token:
            return token
    # Local convenience: fall back to the gh CLI's own credentials so
    # every script is runnable on a developer machine without exporting
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
    """Minimal GitHub REST client with a hard dry-run switch."""

    def __init__(self, api: str, org: str, *, dry_run: bool = True, token: str | None = None):
        self.api = api.rstrip("/")
        self.org = org
        self.dry_run = dry_run
        self.token = token if token is not None else github_token()
        self.performed: list[str] = []

    # -- reads --------------------------------------------------------

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def get(self, path: str, *, accept_missing: bool = False) -> Any:
        url = path if path.startswith("http") else f"{self.api}/{path.lstrip('/')}"
        return http_get_json(url, headers=self._headers(), accept_missing=accept_missing)

    def paginate(self, path: str, *, limit: int = 500) -> list[Any]:
        separator = "&" if "?" in path else "?"
        url = f"{self.api}/{path.lstrip('/')}{separator}per_page=100"
        collected: list[Any] = []
        while url and len(collected) < limit:
            request = urllib.request.Request(url, headers={**self._headers(), "User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=30) as response:
                collected.extend(json.loads(response.read()))
                link = response.headers.get("Link", "")
            match = re.search(r'<([^>]+)>;\s*rel="next"', link)
            url = match.group(1) if match else None
        return collected[:limit]

    def search_pulls(self, query: str, *, limit: int = 300) -> list[dict]:
        """Search is the cheapest way to get merged pull requests with labels.

        Walking the compare API instead would cost one extra request per
        commit just to map it back to its pull request.
        """
        collected: list[dict] = []
        page = 1
        while len(collected) < limit:
            url = (
                f"{self.api}/search/issues?q={urllib.parse.quote(query)}"
                f"&per_page=100&page={page}&advanced_search=true"
            )
            payload = http_get_json(url, headers=self._headers(), accept_missing=True) or {}
            items = payload.get("items", [])
            collected.extend(items)
            if len(items) < 100:
                break
            page += 1
        return collected[:limit]

    def commit_date(self, repo: str, ref: str) -> str | None:
        payload = self.get(
            f"repos/{self.org}/{repo}/commits/{urllib.parse.quote(ref)}", accept_missing=True
        )
        if not payload:
            return None
        return ((payload.get("commit") or {}).get("committer") or {}).get("date")

    def latest_release(self, repo: str) -> dict | None:
        return self.get(f"repos/{self.org}/{repo}/releases/latest", accept_missing=True)

    def release_by_tag(self, repo: str, tag: str) -> dict | None:
        return self.get(
            f"repos/{self.org}/{repo}/releases/tags/{urllib.parse.quote(tag)}", accept_missing=True
        )

    def file_text(self, repo: str, path: str, ref: str = "HEAD") -> str | None:
        payload = self.get(
            f"repos/{self.org}/{repo}/contents/{urllib.parse.quote(path)}?ref={urllib.parse.quote(ref)}",
            accept_missing=True,
        )
        if payload is None:
            return None
        import base64

        return base64.b64decode(payload["content"]).decode("utf-8")

    def branch_exists(self, repo: str, branch: str) -> bool:
        return (
            self.get(
                f"repos/{self.org}/{repo}/branches/{urllib.parse.quote(branch)}",
                accept_missing=True,
            )
            is not None
        )

    def pull(self, repo: str, number: int) -> dict | None:
        return self.get(f"repos/{self.org}/{repo}/pulls/{number}", accept_missing=True)

    def pull_files(self, repo: str, number: int) -> list[str]:
        return [
            entry["filename"] for entry in self.paginate(f"repos/{self.org}/{repo}/pulls/{number}/files")
        ]

    def open_pulls(self, repo: str) -> list[dict]:
        return self.paginate(f"repos/{self.org}/{repo}/pulls?state=open")

    def is_approved(self, repo: str, number: int) -> bool:
        """Approved, in the sense the branch protection means it.

        Only the last review of each reviewer counts, so an approval
        that was later replaced by a change request does not linger.
        """
        latest: dict[str, str] = {}
        for review in self.paginate(f"repos/{self.org}/{repo}/pulls/{number}/reviews"):
            state = review.get("state", "")
            if state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                latest[(review.get("user") or {}).get("login", "?")] = state
        states = set(latest.values())
        return "APPROVED" in states and "CHANGES_REQUESTED" not in states

    def labels_on(self, repo: str, number: int) -> set[str]:
        issue = self.get(f"repos/{self.org}/{repo}/issues/{number}", accept_missing=True) or {}
        return {label["name"] for label in issue.get("labels", [])}

    def pulls_for_head(self, repo: str, head_branch: str) -> list[dict]:
        return (
            self.get(
                f"repos/{self.org}/{repo}/pulls"
                f"?state=all&head={urllib.parse.quote(f'{self.org}:{head_branch}')}",
                accept_missing=True,
            )
            or []
        )

    # -- writes -------------------------------------------------------
    #
    # Every mutating call funnels through _write so that a dry run is a
    # property of the client, not something each caller must remember.

    def _write(self, description: str, perform) -> Any:
        self.performed.append(description)
        if self.dry_run:
            log(f"  [dry-run] {description}")
            return None
        log(f"  {description}")
        return perform()

    def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        url = path if path.startswith("http") else f"{self.api}/{path.lstrip('/')}"
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=body, method=method, headers=self._headers())
        request.add_header("Content-Type", "application/json")
        request.add_header("User-Agent", USER_AGENT)
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
        return json.loads(raw) if raw else None

    def create_branch(self, repo: str, branch: str, from_ref: str) -> Any:
        def perform():
            base = self.get(f"repos/{self.org}/{repo}/git/ref/heads/{urllib.parse.quote(from_ref)}")
            return self._request(
                "POST",
                f"repos/{self.org}/{repo}/git/refs",
                {"ref": f"refs/heads/{branch}", "sha": base["object"]["sha"]},
            )

        return self._write(f"create branch {repo}:{branch} from {from_ref}", perform)

    def put_file(self, repo: str, branch: str, path: str, content: str, message: str) -> Any:
        def perform():
            import base64

            existing = self.get(
                f"repos/{self.org}/{repo}/contents/{urllib.parse.quote(path)}"
                f"?ref={urllib.parse.quote(branch)}",
                accept_missing=True,
            )
            payload = {
                "message": message,
                "content": base64.b64encode(content.encode()).decode(),
                "branch": branch,
            }
            if existing:
                payload["sha"] = existing["sha"]
            return self._request(
                "PUT", f"repos/{self.org}/{repo}/contents/{urllib.parse.quote(path)}", payload
            )

        return self._write(f"write {repo}:{branch}/{path}", perform)

    def commit_files(
        self, repo: str, branch: str, base_branch: str, files: dict[str, str], message: str
    ) -> Any:
        """Put every file on ``branch`` in one commit, creating it if needed.

        One commit rather than one per file: the helm chart bump touches
        five files that are only correct together, and a half-applied
        branch would fail publish-oci.yaml's version equality check.
        """

        def perform():
            base = self.get(f"repos/{self.org}/{repo}/git/ref/heads/{urllib.parse.quote(base_branch)}")
            head = self.get(
                f"repos/{self.org}/{repo}/git/ref/heads/{urllib.parse.quote(branch)}",
                accept_missing=True,
            )
            parent_sha = head["object"]["sha"] if head else base["object"]["sha"]
            parent = self.get(f"repos/{self.org}/{repo}/git/commits/{parent_sha}")

            tree = self._request(
                "POST",
                f"repos/{self.org}/{repo}/git/trees",
                {
                    "base_tree": parent["tree"]["sha"],
                    "tree": [
                        {"path": path, "mode": "100644", "type": "blob", "content": content}
                        for path, content in sorted(files.items())
                    ],
                },
            )
            commit = self._request(
                "POST",
                f"repos/{self.org}/{repo}/git/commits",
                {"message": message, "tree": tree["sha"], "parents": [parent_sha]},
            )
            if head:
                return self._request(
                    "PATCH",
                    f"repos/{self.org}/{repo}/git/refs/heads/{urllib.parse.quote(branch)}",
                    {"sha": commit["sha"], "force": True},
                )
            return self._request(
                "POST",
                f"repos/{self.org}/{repo}/git/refs",
                {"ref": f"refs/heads/{branch}", "sha": commit["sha"]},
            )

        return self._write(
            f"commit {len(files)} file(s) to {repo}:{branch} (from {base_branch}): "
            + ", ".join(sorted(files)),
            perform,
        )

    def merge_pull(self, repo: str, number: int, method: str = "squash", title: str = "") -> Any:
        payload: dict[str, Any] = {"merge_method": method}
        if title:
            payload["commit_title"] = title
        return self._write(
            f"merge {repo}#{number} ({method})",
            lambda: self._request("PUT", f"repos/{self.org}/{repo}/pulls/{number}/merge", payload),
        )

    def remove_label(self, repo: str, number: int, label: str) -> Any:
        return self._write(
            f"remove label {label!r} from {repo}#{number}",
            lambda: self._request(
                "DELETE",
                f"repos/{self.org}/{repo}/issues/{number}/labels/{urllib.parse.quote(label)}",
            ),
        )

    def create_pull(self, repo: str, head: str, base: str, title: str, body: str) -> Any:
        return self._write(
            f"open pull request {repo} {head} -> {base}: {title}",
            lambda: self._request(
                "POST",
                f"repos/{self.org}/{repo}/pulls",
                {"head": head, "base": base, "title": title, "body": body},
            ),
        )

    def add_labels(self, repo: str, number: int, labels: Iterable[str]) -> Any:
        labels = list(labels)
        return self._write(
            f"label {repo}#{number} with {', '.join(labels)}",
            lambda: self._request(
                "POST", f"repos/{self.org}/{repo}/issues/{number}/labels", {"labels": labels}
            ),
        )

    def upsert_comment(self, repo: str, number: int, marker: str, body: str) -> Any:
        def perform():
            comments = self.paginate(f"repos/{self.org}/{repo}/issues/{number}/comments")
            for comment in comments:
                if marker in (comment.get("body") or ""):
                    return self._request(
                        "PATCH",
                        f"repos/{self.org}/{repo}/issues/comments/{comment['id']}",
                        {"body": body},
                    )
            return self._request(
                "POST", f"repos/{self.org}/{repo}/issues/{number}/comments", {"body": body}
            )

        return self._write(f"upsert status comment on {repo}#{number}", perform)


# --------------------------------------------------------------------
# OBS, SCC, GHCR
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


def ghcr_tag_exists(config: Config, image: str, tag: str) -> bool:
    """Anonymous existence check against the GHCR registry API."""
    token_url = (
        f"https://{config.ghcr_registry}/token"
        f"?scope=repository:{config.ghcr_namespace}/{image}:pull&service={config.ghcr_registry}"
    )
    try:
        token_payload = http_get_json(token_url, accept_missing=True) or {}
        token = token_payload.get("token")
        if not token:
            return False
        manifest_url = (
            f"https://{config.ghcr_registry}/v2/{config.ghcr_namespace}/{image}/manifests/"
            f"{urllib.parse.quote(tag)}"
        )
        accept = ",".join(
            [
                "application/vnd.oci.image.index.v1+json",
                "application/vnd.oci.image.manifest.v1+json",
                "application/vnd.docker.distribution.manifest.list.v2+json",
                "application/vnd.docker.distribution.manifest.v2+json",
            ]
        )
        payload = http_get(
            manifest_url,
            headers={"Authorization": f"Bearer {token}", "Accept": accept},
            accept_missing=True,
        )
        return payload is not None
    except HttpError:
        return False
