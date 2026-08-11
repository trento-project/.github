#!/usr/bin/env python3
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

"""Stand up a personal copy of the OBS side, for a rehearsal.

The GitHub half of the cascade can already be pointed at a set of forks
with TRENTO_GITHUB_ORG. The OBS half could not, so a rehearsal stopped
at the GitHub release and the rest was taken on trust.

It turns out to be forkable, because the OBS projects are not where the
sources live. Every package in `devel:sap:trento` and its `:factory`
carries a `_scmsync.obsinfo` naming a git repository and a branch:

    url: https://src.opensuse.org/SAP-trento/trento-web
    revision: stable          # `main` in the factory project

So OBS mirrors git, and a personal copy is two things: a fork of each
git repository, and a project whose packages scmsync to the fork
instead. Point the fork's OBS_PROJECT_STABLE and OBS_PROJECT_ROLLING at
those projects and obs-sync.yaml commits to them exactly as it does to
the real ones.

Nothing is named by hand. The repositories and branches are read from
the real projects at run time, so a package that moves elsewhere is
followed rather than mirrored wrongly.

    ./fork_obs.py                      # what it would do
    ./fork_obs.py --execute
    ./fork_obs.py --execute --cleanup  # take it all down again

Needs two credentials, neither of which this can invent:

    SRC_TOKEN            a token for the git forge, to fork
    OBS_USER, OBS_PASS   an OBS account, to create the projects
                         (read from ~/.config/osc/oscrc when unset)

`--obs-api` and `--project` point the same thing at another build
service, for the half of the release that a public repository cannot
describe. Which git forge the forks land on is not a setting: it is
whatever the packages there say they are synced from.
"""

from __future__ import annotations

import argparse
import base64
import configparser
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from pathlib import Path

from common import COMPONENTS_FILE, USER_AGENT, Config, HttpError, load_config, log

# The public instance. --obs-api points the whole thing at another
# one; nothing else in this file names a host.
DEFAULT_OBS_API = "https://api.opensuse.org"
OSCRC = Path.home() / ".config" / "osc" / "oscrc"


@dataclass
class Mirror:
    """One package, and the git repository the real OBS project reads."""

    package: str
    project: str
    git_api: str
    git_owner: str
    git_repo: str
    branch: str

    @property
    def upstream(self) -> str:
        return f"{self.git_api}/{self.git_owner}/{self.git_repo}"


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    accept_missing: bool = False,
) -> bytes | None:
    call = urllib.request.Request(url, method=method, data=body)
    call.add_header("User-Agent", USER_AGENT)
    for name, value in (headers or {}).items():
        call.add_header(name, value)
    try:
        with urllib.request.urlopen(call, timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        if error.code == 404 and accept_missing:
            return None
        raise HttpError(error.code, url, error.read().decode("utf-8", "replace")) from error


# --------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------


def obs_credentials(required: bool) -> tuple[str | None, str | None]:
    """The OBS account, from the environment or osc's own configuration.

    A dry run needs the user name only, to say what the projects would
    be called, and manages without even that. Only ``--execute`` insists
    on both.
    """
    user, password = os.environ.get("OBS_USER"), os.environ.get("OBS_PASS")

    # osc keeps the same credentials, and a maintainer running this by
    # hand has already configured it. Only the plain-text form is read;
    # a keyring entry is not something to go rummaging in.
    if OSCRC.exists() and not (user and password):
        parser = configparser.ConfigParser()
        parser.read(OSCRC)
        for section in parser.sections():
            if "opensuse.org" not in section:
                continue
            user = user or parser.get(section, "user", fallback=None)
            password = password or parser.get(section, "pass", fallback=None)

    if required and not (user and password):
        raise SystemExit("no OBS credentials: set OBS_USER and OBS_PASS, or configure osc")
    return user, password


def obs_headers(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def git_headers() -> dict[str, str]:
    token = os.environ.get("SRC_TOKEN") or os.environ.get("SRC_OPENSUSE_TOKEN")
    if not token:
        raise SystemExit("no git forge token: set SRC_TOKEN")
    return {"Authorization": f"token {token}", "Content-Type": "application/json"}


# --------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------


def packages_of(config: Config) -> list[str]:
    """Every OBS package the components file knows about.

    Both the source package and the image package: an image is a
    separate OBS package built from the same repository, and a
    rehearsal that skipped them would not exercise the chart's wait for
    published images.
    """
    packages: list[str] = []
    for component in config.components.values():
        for name in (component.obs_package, component.obs_image_package):
            if name and name not in packages:
                packages.append(name)
    return sorted(packages)


def read_source(api: str, path: str, headers: dict[str, str]) -> bytes | None:
    """Read a source file, preferring the endpoint that needs no login.

    The public instance serves /public to anyone, which is what lets a
    dry run work without credentials. An internal one does not, and
    answers 404 rather than 401 there, so a missing file and a refused
    one look alike; /source is tried whenever there are credentials to
    try it with.
    """
    try:
        payload = request("GET", f"{api}/public/source/{path}", accept_missing=True)
        if payload is not None or not headers:
            return payload
    except HttpError as error:
        if error.status not in (401, 403) or not headers:
            raise
    return request("GET", f"{api}/source/{path}", headers=headers, accept_missing=True)


def refused(api: str, status: int) -> SystemExit:
    """The one thing to say when an instance will not talk to us."""
    return SystemExit(
        f"{api} answered {status}. Set OBS_USER and OBS_PASS for that instance, "
        "and check the VPN"
    )


def mirror_of(api: str, project: str, package: str, headers: dict[str, str]) -> Mirror | None:
    """Where the real project reads this package from, or None."""
    path = f"{urllib.parse.quote(project)}/{package}/_scmsync.obsinfo"
    payload = read_source(api, path, headers)
    if payload is None:
        return None

    fields = dict(
        line.split(": ", 1)
        for line in payload.decode("utf-8", "replace").splitlines()
        if ": " in line
    )
    origin, branch = fields.get("url"), fields.get("revision")
    if not origin or not branch:
        return None

    split = urllib.parse.urlsplit(origin)
    owner, _, repo = split.path.strip("/").partition("/")
    return Mirror(
        package=package,
        project=project,
        git_api=f"{split.scheme}://{split.netloc}",
        git_owner=owner,
        git_repo=repo,
        branch=branch,
    )


def discover(
    api: str, config: Config, projects: dict[str, str], headers: dict[str, str]
) -> list[Mirror]:
    mirrors: list[Mirror] = []
    for package in packages_of(config):
        for project in projects.values():
            try:
                mirror = mirror_of(api, project, package, headers)
            except HttpError as error:
                if error.status not in (401, 403):
                    raise
                # Stop at the first refusal rather than reporting the
                # rest as "not scmsynced", which is what a package we
                # are not allowed to read looks like from here.
                raise refused(api, error.status) from None
            if mirror is None:
                log(f"  ! {project}/{package}: not scmsynced, skipped")
                continue
            mirrors.append(mirror)
    return mirrors


# --------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------


class Forker:
    def __init__(self, *, api: str, execute: bool, obs_user: str, obs_password: str | None):
        self.api = api
        self.execute = execute
        self.obs_user = obs_user
        # A dry run reads only /public and the fork's own existence, so
        # it holds no credentials and cannot be made to write by
        # accident.
        self.obs = obs_headers(obs_user, obs_password) if execute else {}
        self.git = git_headers() if execute else {}
        self.done: list[str] = []

    def _do(self, description: str, perform) -> None:
        self.done.append(description)
        if not self.execute:
            log(f"  would {description}")
            return
        perform()
        log(f"  {description}")

    # -- git ----------------------------------------------------------

    def fork_exists(self, mirror: Mirror, owner: str) -> bool:
        url = f"{mirror.git_api}/api/v1/repos/{owner}/{mirror.git_repo}"
        return request("GET", url, headers=self.git, accept_missing=True) is not None

    def fork(self, mirror: Mirror, owner: str) -> None:
        if self.fork_exists(mirror, owner):
            log(f"  {owner}/{mirror.git_repo} already forked")
            return
        self._do(
            f"fork {mirror.git_owner}/{mirror.git_repo} to {owner}",
            lambda: request(
                "POST",
                f"{mirror.git_api}/api/v1/repos/{mirror.git_owner}/{mirror.git_repo}/forks",
                headers=self.git,
                body=json.dumps({}).encode(),
            ),
        )

    def unfork(self, mirror: Mirror, owner: str) -> None:
        if not self.fork_exists(mirror, owner):
            return
        self._do(
            f"delete the fork {owner}/{mirror.git_repo}",
            lambda: request(
                "DELETE",
                f"{mirror.git_api}/api/v1/repos/{owner}/{mirror.git_repo}",
                headers=self.git,
            ),
        )

    # -- obs ----------------------------------------------------------

    def create_project(self, project: str, title: str) -> None:
        # No <repository> elements on purpose. The rehearsal is about
        # whether the release cascade reaches OBS and whether the table
        # reads the versions back; building is neither, and one of these
        # packages carries a 250 MB node_modules archive that would be
        # rebuilt on every commit.
        meta = ElementTree.Element("project", {"name": project})
        ElementTree.SubElement(meta, "title").text = title
        ElementTree.SubElement(meta, "description").text = (
            "Rehearsal copy of the Trento release train. Sources are forks; "
            "no repositories, so nothing is built."
        )
        ElementTree.SubElement(meta, "person", {"userid": self.obs_user, "role": "maintainer"})
        body = ElementTree.tostring(meta)
        self._do(
            f"create the project {project}",
            lambda: request("PUT", f"{self.api}/source/{project}/_meta", headers=self.obs, body=body),
        )

    def link_package(self, project: str, mirror: Mirror, owner: str) -> None:
        meta = ElementTree.Element("package", {"name": mirror.package, "project": project})
        ElementTree.SubElement(meta, "title").text = mirror.package
        ElementTree.SubElement(meta, "description").text = ""
        scmsync = ElementTree.SubElement(meta, "scmsync")
        scmsync.text = f"{mirror.git_api}/{owner}/{mirror.git_repo}#{mirror.branch}"
        body = ElementTree.tostring(meta)
        self._do(
            f"point {project}/{mirror.package} at {scmsync.text}",
            lambda: request(
                "PUT",
                f"{self.api}/source/{project}/{mirror.package}/_meta",
                headers=self.obs,
                body=body,
            ),
        )

    def delete_project(self, project: str) -> None:
        exists = read_source(self.api, f"{project}/_meta", self.obs)
        if exists is None:
            log(f"  {project} is not there")
            return
        self._do(
            f"delete the project {project}",
            lambda: request(
                "DELETE", f"{self.api}/source/{project}?force=1", headers=self.obs
            ),
        )


def target_projects(user: str, prefix: str) -> dict[str, str]:
    """The personal project standing in for each real one."""
    return {
        "stable": f"home:{user}:{prefix}",
        "rolling": f"home:{user}:{prefix}:factory",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--components", type=Path, default=COMPONENTS_FILE)
    parser.add_argument(
        "--prefix", default="trento", help="names home:<user>:<prefix> and its :factory"
    )
    parser.add_argument("--git-owner", help="who owns the forks, defaults to the OBS user")
    parser.add_argument(
        "--obs-api",
        default=DEFAULT_OBS_API,
        help="the build service to copy from and into, for an instance other than the public one",
    )
    parser.add_argument(
        "--project",
        action="append",
        default=[],
        metavar="KEY=NAME",
        help="override a project, as stable=... or rolling=..., repeatable",
    )
    parser.add_argument("--cleanup", action="store_true", help="take the copy down again")
    parser.add_argument("--execute", action="store_true", help="without it nothing is written")
    args = parser.parse_args(argv)

    config = load_config(args.components)
    obs_user, obs_password = obs_credentials(required=args.execute)
    if not obs_user:
        # Enough to render the names a real run would use, so the dry
        # run is still worth reading on a machine with no osc at all.
        obs_user = "OBS_USER"
    git_owner = args.git_owner or obs_user
    targets = target_projects(obs_user, args.prefix)

    # The two project names come from components.yaml or the
    # organisation variables. --project is for an instance those do not
    # describe, where the same packages live under different names.
    sources = dict(config.obs_projects)
    for override in args.project:
        key, _, name = override.partition("=")
        if key not in sources or not name:
            allowed = " or ".join(f"{known}=NAME" for known in sorted(sources))
            raise SystemExit(f"--project takes {allowed}, got {override!r}")
        sources[key] = name

    headers = obs_headers(obs_user, obs_password) if obs_password else {}

    log(f"copying {', '.join(sources.values())} on {args.obs_api}")
    log(f"     to {', '.join(targets.values())}")
    log(f"  forks to {git_owner}")
    log("")

    mirrors = discover(args.obs_api, config, sources, headers)
    if not mirrors:
        # An instance that hides what it will not serve returns 404 as
        # readily as 401, so say what the other explanation is.
        hint = "" if headers else f", and no credentials were given for {args.obs_api}"
        print(f"nothing to copy: no package is scmsynced{hint}", file=sys.stdout)
        return 1

    forker = Forker(
        api=args.obs_api, execute=args.execute, obs_user=obs_user, obs_password=obs_password
    )
    # Which of the two real projects a mirror came from decides which
    # personal project it lands in, so the stable/rolling split and its
    # branches survive the copy.
    reverse = {project: key for key, project in sources.items()}

    if args.cleanup:
        for project in targets.values():
            forker.delete_project(project)
        # One fork backs both projects, so delete each repository once.
        for repo in sorted({mirror.git_repo: mirror for mirror in mirrors}.values(), key=lambda m: m.git_repo):
            forker.unfork(repo, git_owner)
    else:
        for repo in sorted({mirror.git_repo: mirror for mirror in mirrors}.values(), key=lambda m: m.git_repo):
            forker.fork(repo, git_owner)
        for key, project in targets.items():
            forker.create_project(project, f"Trento {key} rehearsal")
            for mirror in mirrors:
                if reverse.get(mirror.project) == key:
                    forker.link_package(project, mirror, git_owner)

    print(file=sys.stdout)
    verb = "applied" if args.execute else "pending"
    print(f"{len(forker.done)} change(s) {verb}", file=sys.stdout)
    if not args.cleanup:
        print(
            "set these on the fork so the cascade uses them:\n"
            f"  gh variable set OBS_PROJECT_STABLE --body {targets['stable']}\n"
            f"  gh variable set OBS_PROJECT_ROLLING --body {targets['rolling']}",
            file=sys.stdout,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
