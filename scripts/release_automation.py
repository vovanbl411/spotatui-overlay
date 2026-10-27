#!/usr/bin/env python3
"""Discover and prepare stable spotatui releases for manual Gentoo review."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


UPSTREAM = "LargeModGames/spotatui"
API = "https://api.github.com"
PACKAGE_DIR = Path("media-sound/spotatui")
TAG_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
EBUILD_RE = re.compile(r"^spotatui-(\d+)\.(\d+)\.(\d+)(?:-r(\d+))?\.ebuild$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
CRATES_RE = re.compile(r'(?ms)^CRATES="\s*(.*?)\s*"')
CRATE_LICENSE_RE = re.compile(r'(?ms)^# Dependent crate licenses\s*\nLICENSE\+="(.*?)"')
NATIVE_CRATE_RE = re.compile(
    r"(?:^|[-_])(alsa|dbus|libdbus|libpulse|libudev|openssl|pipewire|pkg-config|sdl2)(?:[-_]|$)"
)
NATIVE_CRATES = frozenset(
    {
        "alsa",
        "alsa-sys",
        "dbus",
        "libdbus-sys",
        "libpulse-binding",
        "libpulse-simple-binding",
        "libpulse-simple-sys",
        "libpulse-sys",
        "libudev",
        "libudev-sys",
        "openssl",
        "openssl-sys",
        "pipewire",
        "pipewire-sys",
        "pkg-config",
        "sdl2",
        "sdl2-sys",
    }
)
ALLOWED_HANDOFF = {"README.md", str(PACKAGE_DIR / "Manifest")}


class AutomationError(RuntimeError):
    """Raised when a release requires a manual packaging decision."""


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: str) -> Version:
        match = TAG_RE.fullmatch(value.removeprefix("v"))
        if not match:
            raise AutomationError(f"Not a stable X.Y.Z version: {value}.")
        return cls(*(int(part) for part in match.groups()))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True)
class PackagedVersion:
    version: Version
    revision: int
    path: Path


def stable_release(payload: object) -> dict[str, str] | None:
    """Return a validated stable release, ignoring GitHub drafts and prereleases."""
    if not isinstance(payload, dict):
        raise AutomationError("GitHub returned an invalid release object.")
    if payload.get("draft") is True or payload.get("prerelease") is True:
        return None
    if payload.get("draft") is not False or payload.get("prerelease") is not False:
        raise AutomationError("GitHub release lacks explicit stable-release flags.")
    tag = payload.get("tag_name")
    url = payload.get("html_url")
    if not isinstance(tag, str) or not isinstance(url, str):
        raise AutomationError("GitHub release is missing its tag or URL.")
    version = Version.parse(tag)
    expected = f"https://github.com/{UPSTREAM}/releases/tag/{tag}"
    if url != expected:
        raise AutomationError("GitHub release URL does not match the upstream tag.")
    return {"tag": f"v{version}", "version": str(version), "url": url}


def packaged_versions(root: Path) -> list[PackagedVersion]:
    # Версия всегда выводится из имен ebuild-файлов; отдельного state-файла нет.
    versions: list[PackagedVersion] = []
    directory = root / PACKAGE_DIR
    for path in directory.glob("spotatui-*.ebuild"):
        match = EBUILD_RE.fullmatch(path.name)
        if match:
            versions.append(
                PackagedVersion(
                    Version(*(int(part) for part in match.groups()[:3])),
                    int(match.group(4) or 0),
                    path,
                )
            )
    if not versions:
        raise AutomationError(f"No versioned spotatui ebuild found in {directory}.")
    return sorted(versions, key=lambda item: (item.version, item.revision))


def crates_from_ebuild(content: str) -> set[str]:
    match = CRATES_RE.search(content)
    if match is None:
        raise AutomationError("Ebuild does not contain a parseable CRATES variable.")
    crates = {line.strip() for line in match.group(1).splitlines() if line.strip()}
    if not crates or any(not re.fullmatch(r"[A-Za-z0-9_-]+@[A-Za-z0-9.+_-]+", crate) for crate in crates):
        raise AutomationError("Ebuild CRATES contains an invalid or empty entry.")
    return crates


def check_manifest(root: Path) -> int:
    """Check that crate DIST entries and the current ebuild CRATES match exactly."""
    current = packaged_versions(root)[-1]
    expected = {f"{crate.replace('@', '-')}.crate" for crate in crates_from_ebuild(current.path.read_text())}
    manifest = root / PACKAGE_DIR / "Manifest"
    actual = {
        fields[1]
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if len(fields := line.split()) >= 2 and fields[0] == "DIST" and fields[1].endswith(".crate")
    }
    if expected != actual:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise AutomationError(f"CRATES/Manifest mismatch; missing={missing[:5]}, extra={extra[:5]}.")
    print(f"CRATES and Manifest agree: {len(expected)} crate archives.")
    return len(expected)


def read_cargo(source_dir: Path) -> tuple[dict, dict, list[dict]]:
    try:
        manifest = tomllib.loads((source_dir / "Cargo.toml").read_text(encoding="utf-8"))
        lock = tomllib.loads((source_dir / "Cargo.lock").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise AutomationError(f"Unable to read Cargo.toml/Cargo.lock: {error}.") from error
    package = manifest.get("package", {})
    if not package and isinstance(manifest.get("workspace"), dict):
        package = manifest.get("workspace", {}).get("package", {})
    if not isinstance(package, dict):
        raise AutomationError("Upstream Cargo.toml has no package metadata.")
    return package, manifest, lock.get("package", [])


def feature_map(manifest: dict) -> dict[str, object]:
    features = manifest.get("features", {})
    return features if isinstance(features, dict) else {}


def native_dependency_map(manifest: dict) -> dict[str, str]:
    result: dict[str, str] = {}

    def visit(value: object) -> None:
        if not isinstance(value, dict):
            return
        for name, specification in value.items():
            if name in NATIVE_CRATES or NATIVE_CRATE_RE.search(name):
                result[name] = json.dumps(specification, sort_keys=True)

    visit(manifest.get("dependencies"))
    visit(manifest.get("build-dependencies"))
    for target in manifest.get("target", {}).values() if isinstance(manifest.get("target"), dict) else ():
        if isinstance(target, dict):
            visit(target.get("dependencies"))
            visit(target.get("build-dependencies"))
    return result


def configured_cargo_features(ebuild: str) -> set[str]:
    block = re.search(r"myfeatures=\(([^)]*)\)", ebuild, re.DOTALL)
    if block is None:
        raise AutomationError("Cannot identify the ebuild's Cargo feature mapping.")
    features = set(re.findall(r"[A-Za-z0-9_-]+", block.group(1)))
    features.update(re.findall(r"myfeatures\+=\(\s*([A-Za-z0-9_-]+)\s*\)", ebuild))
    return features


def check_source_contract(
    current_source: Path,
    candidate_source: Path,
    current_version: Version,
    expected: Version,
    mapped_features: set[str],
) -> None:
    old_package, old_manifest, _ = read_cargo(current_source)
    new_package, new_manifest, lock_packages = read_cargo(candidate_source)
    new_version = new_package.get("version")
    if old_package.get("version") != str(current_version):
        raise AutomationError(
            f"Current ebuild version {current_version} does not match packaged upstream Cargo.toml "
            f"version {old_package.get('version')!r}."
        )
    if new_version != str(expected):
        raise AutomationError(
            f"Release tag v{expected} does not match Cargo package.version {new_version!r}."
        )
    for package, label in ((old_package, "packaged"), (new_package, "candidate")):
        rust_version = package.get("rust-version")
        if rust_version is not None and not isinstance(rust_version, str):
            raise AutomationError(f"The {label} Rust minimum version is invalid.")
    if old_package.get("rust-version") != new_package.get("rust-version"):
        raise AutomationError("Upstream rust-version changed; review the ebuild Rust requirement.")
    if feature_map(old_manifest) != feature_map(new_manifest):
        raise AutomationError("Upstream Cargo features changed; review the USE mapping manually.")
    if native_dependency_map(old_manifest) != native_dependency_map(new_manifest):
        raise AutomationError("Native/system Cargo dependencies changed; review Gentoo dependencies manually.")
    available = feature_map(new_manifest)
    missing = sorted(mapped_features - available.keys())
    if missing:
        raise AutomationError(f"Ebuild enables Cargo features missing upstream: {', '.join(missing)}.")
    git_dependencies = sorted(
        f"{item.get('name')}@{item.get('version')}"
        for item in lock_packages
        if isinstance(item, dict) and str(item.get("source", "")).startswith("git+")
    )
    if git_dependencies:
        raise AutomationError(
            "Cargo.lock contains Git dependencies; determine a Gentoo packaging method: "
            + ", ".join(git_dependencies)
            + "."
        )


def lock_crates(source_dir: Path) -> set[str]:
    _, _, packages = read_cargo(source_dir)
    crates: set[str] = set()
    for item in packages:
        if not isinstance(item, dict) or "source" not in item:
            continue
        source = str(item["source"])
        if source.startswith("git+"):
            raise AutomationError(f"Cargo.lock contains an unsupported Git dependency: {item.get('name')}.")
        if source.startswith("registry+"):
            crate = f"{item.get('name')}@{item.get('version')}"
            if not re.fullmatch(r"[A-Za-z0-9_-]+@[A-Za-z0-9.+_-]+", crate):
                raise AutomationError("Cargo.lock contains an invalid registry crate name or version.")
            crates.add(crate)
    return crates


def replace_crates(ebuild: str, crates: set[str]) -> str:
    if not crates:
        raise AutomationError("Cargo.lock does not contain any registry crates.")
    match = CRATES_RE.search(ebuild)
    if match is None:
        raise AutomationError("Cannot update CRATES in the current ebuild.")
    block = 'CRATES="\n' + "".join(f"\t{crate}\n" for crate in sorted(crates)) + '"'
    return ebuild[: match.start()] + block + ebuild[match.end() :]


def check_crate_licenses(ebuild: str, generated: str) -> None:
    declared = re.search(r'(?m)^LICENSE="([^"]*)"', ebuild)
    crate_licenses = CRATE_LICENSE_RE.search(generated)
    if declared is None or crate_licenses is None:
        raise AutomationError("Unable to compare generated Cargo license requirements with ebuild LICENSE.")
    declared_terms = set(re.findall(r"[A-Za-z0-9][A-Za-z0-9+_.-]*", declared.group(1)))
    generated_terms = set(re.findall(r"[A-Za-z0-9][A-Za-z0-9+_.-]*", crate_licenses.group(1)))
    generated_terms.difference_update({"AND", "OR", "WITH"})
    missing = sorted(generated_terms - declared_terms)
    if missing:
        raise AutomationError(
            "Cargo crate licenses are not covered by the current LICENSE; review manually: "
            + ", ".join(missing)
            + "."
        )


def release_dependencies(old_source: Path, new_source: Path) -> tuple[list[str], list[str]]:
    old = lock_crates(old_source)
    new = lock_crates(new_source)
    return sorted(new - old), sorted(old - new)


def _download_source(tag: str, destination: Path) -> Path:
    url = f"https://github.com/{UPSTREAM}/archive/refs/tags/{quote(tag)}.tar.gz"
    request = Request(url, headers={"User-Agent": "spotatui-overlay-release-automation"})
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / f"{tag}.tar.gz"
    try:
        with urlopen(request, timeout=60) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
        with tarfile.open(archive, "r:gz") as bundle:
            bundle.extractall(destination / tag, filter="data")
    except (HTTPError, URLError, OSError, tarfile.TarError) as error:
        raise AutomationError(f"Unable to download/extract upstream {tag}: {error}.") from error
    sources = list((destination / tag).glob("*/Cargo.toml"))
    if len(sources) != 1:
        raise AutomationError(f"Expected one upstream Cargo.toml in {tag} archive; found {len(sources)}.")
    return sources[0].parent


def _current_upstream_version(root: Path) -> Version:
    current = packaged_versions(root)[-1]
    return current.version


def _generated_crates(source_dir: Path, temp_dir: Path, base_ebuild: str) -> set[str]:
    generated = temp_dir / "generated.ebuild"
    input_ebuild = temp_dir / "input.ebuild"
    distdir = temp_dir / "distfiles"
    distdir.mkdir(parents=True, exist_ok=True)
    if "# Dependent crate licenses" not in base_ebuild:
        base_ebuild = base_ebuild.rstrip() + '\n\n# Dependent crate licenses\nLICENSE+=""\n'
    input_ebuild.write_text(base_ebuild, encoding="utf-8")
    portdir = Path(os.environ.get("PORTDIR", "/var/db/repos/gentoo"))
    license_mapping = portdir / "metadata/license-mapping.conf"
    if not license_mapping.is_file():
        raise AutomationError(f"Gentoo license mapping is unavailable: {license_mapping}.")
    try:
        subprocess.run(
            [
                "pycargoebuild",
                "--no-config",
                "--no-manifest",
                "--fill-crates",
                "--distdir",
                str(distdir),
                "--license-mapping",
                str(license_mapping),
                "--input",
                str(input_ebuild),
                "--output",
                str(generated),
                str(source_dir),
            ],
            check=True,
            cwd=temp_dir,
            text=True,
            capture_output=True,
        )
    except FileNotFoundError as error:
        raise AutomationError("Gentoo app-portage/pycargoebuild is required to update CRATES.") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        raise AutomationError(f"pycargoebuild failed: {detail[-1500:]}") from error
    generated_content = generated.read_text(encoding="utf-8")
    check_crate_licenses(base_ebuild, generated_content)
    generated_crates = crates_from_ebuild(generated_content)
    locked_crates = lock_crates(source_dir)
    if generated_crates != locked_crates:
        raise AutomationError("pycargoebuild output does not match registry packages in Cargo.lock.")
    return generated_crates


def candidate_plan(root: Path, release: dict[str, str]) -> dict[str, object]:
    version = Version.parse(release["version"])
    if release.get("tag") != f"v{version}" or release.get("url") != f"https://github.com/{UPSTREAM}/releases/tag/v{version}":
        raise AutomationError("Release tag, version and upstream URL do not agree.")
    existing = packaged_versions(root)
    if len(existing) > 2:
        raise AutomationError("Found more than two packaged ebuilds; resolve the existing rotation manually.")
    latest = existing[-1]
    if version <= latest.version:
        return {"outcome": "up-to-date", "version": str(version), "current": str(latest.version)}
    # При первом переходе с единственного 0.47.0-r1 он остаётся точным fallback.
    if len(existing) > 1:
        removed = existing[-2]
    else:
        removed = None
    return {
        "outcome": "release-available",
        "version": str(version),
        "fallback": latest.path.name,
        "removed": removed.path.name if removed else None,
        "candidate": f"spotatui-{version}.ebuild",
    }


def prepare_candidate(
    root: Path,
    release: dict[str, str],
    output: Path,
    *,
    source_dirs: tuple[Path, Path] | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    """Prepare only package ebuilds, README and Manifest in an isolated tree."""
    plan = candidate_plan(root, release)
    if plan["outcome"] != "release-available" or dry_run:
        return plan
    if output.exists():
        raise AutomationError(f"Candidate output directory already exists: {output}.")
    version = Version.parse(str(plan["version"]))
    existing = packaged_versions(root)
    current = existing[-1]
    work_dir = output.with_name(output.name + "-work")
    if work_dir.exists():
        raise AutomationError(f"Preparation work directory already exists: {work_dir}.")
    work_dir.mkdir(parents=True)
    mapped_features = configured_cargo_features(current.path.read_text(encoding="utf-8"))
    if source_dirs is None:
        current_source = _download_source(f"v{current.version}", work_dir / "current-source")
        candidate_source = _download_source(release["tag"], work_dir / "candidate-source")
        check_source_contract(current_source, candidate_source, current.version, version, mapped_features)
        return _materialize(root, release, output, plan, current, existing, current_source, candidate_source, work_dir)
    current_source, candidate_source = source_dirs
    check_source_contract(current_source, candidate_source, current.version, version, mapped_features)
    return _materialize(root, release, output, plan, current, existing, current_source, candidate_source, work_dir)


def _materialize(
    root: Path,
    release: dict[str, str],
    output: Path,
    plan: dict[str, object],
    current: PackagedVersion,
    existing: list[PackagedVersion],
    current_source: Path,
    candidate_source: Path,
    temporary: Path,
) -> dict[str, object]:
    # Изолированная копия содержит только данные, нужные для Manifest/pkgcheck.
    if output.exists():
        raise AutomationError(f"Candidate output directory already exists: {output}.")
    output.mkdir(parents=True)
    for relative in (
        Path("metadata/layout.conf"),
        Path("profiles/categories"),
        Path("profiles/repo_name"),
        PACKAGE_DIR / "metadata.xml",
        PACKAGE_DIR / "Manifest",
    ):
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / relative, destination)
    package_out = output / PACKAGE_DIR
    for entry in (root / PACKAGE_DIR).glob("spotatui-*.ebuild"):
        if entry.name != plan["removed"]:
            shutil.copy2(entry, package_out / entry.name)
    generated_crates = _generated_crates(
        candidate_source, temporary, current.path.read_text(encoding="utf-8")
    )
    archive_candidates = list(temporary.rglob(f"{release['tag']}.tar.gz"))
    if archive_candidates:
        shutil.copy2(archive_candidates[-1], temporary / "distfiles" / f"spotatui-{plan['version']}.tar.gz")
    candidate_content = replace_crates(current.path.read_text(encoding="utf-8"), generated_crates)
    candidate_path = package_out / str(plan["candidate"])
    candidate_path.write_text(candidate_content, encoding="utf-8")
    if plan["removed"]:
        (package_out / str(plan["removed"])).unlink(missing_ok=True)
    readme = (root / "README.md").read_text(encoding="utf-8")
    old_version = str(current.version)
    readme, count = re.subn(
        rf"(клиент `spotatui` версии ){re.escape(old_version)}(?=\.)",
        rf"\g<1>{plan['version']}",
        readme,
        count=1,
    )
    if count != 1:
        raise AutomationError("README package version line was not found exactly once.")
    (output / "README.md").write_text(readme, encoding="utf-8")
    report = {
        **plan,
        "tag": release["tag"],
        "release_url": release["url"],
        "fallback_version": str(current.version) + (f"-r{current.revision}" if current.revision else ""),
        "base_files": [str(path.relative_to(root)) for path in (root / PACKAGE_DIR / "Manifest", current.path)],
    }
    report["crate_added"], report["crate_removed"] = release_dependencies(current_source, candidate_source)
    (output / "release-handoff.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def validate_handoff(root: Path, handoff: Path, expected_base: str) -> dict[str, object]:
    """Reject changed base commits, unsafe archive paths, or unexpected files."""
    metadata_path = handoff / "release-handoff.json"
    try:
        if metadata_path.stat().st_size > 1024 * 1024:
            raise AutomationError("Release handoff metadata exceeds the size limit.")
        report = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AutomationError("Release handoff metadata is missing or invalid.") from error
    if not isinstance(report, dict) or report.get("base_commit") != expected_base:
        raise AutomationError("Release handoff base commit does not match the checked-out base.")
    version = Version.parse(str(report.get("version", "")))
    tag = report.get("tag")
    release_url = report.get("release_url")
    if tag != f"v{version}" or release_url != f"https://github.com/{UPSTREAM}/releases/tag/v{version}":
        raise AutomationError("Release handoff upstream tag or URL is invalid.")
    allowed = set(ALLOWED_HANDOFF)
    allowed.add(str(PACKAGE_DIR / f"spotatui-{version}.ebuild"))
    fallback = report.get("fallback")
    if not isinstance(fallback, str) or EBUILD_RE.fullmatch(fallback) is None:
        raise AutomationError("Release handoff has an invalid fallback ebuild.")
    removed = report.get("removed")
    if removed is not None:
        if not isinstance(removed, str) or EBUILD_RE.fullmatch(removed) is None:
            raise AutomationError("Release handoff has an invalid removed ebuild.")
    actual: set[str] = set()
    for path in handoff.rglob("*"):
        if path.is_symlink():
            raise AutomationError("Release handoff contains a symlink.")
        if path.is_file():
            relative = path.relative_to(handoff).as_posix()
            if relative != "release-handoff.json":
                actual.add(relative)
    if actual != allowed:
        raise AutomationError(f"Release handoff file allowlist mismatch: {sorted(actual ^ allowed)}.")
    if expected_base != "" and not re.fullmatch(r"[0-9a-f]{40}", expected_base):
        raise AutomationError("Expected base commit is not a full Git SHA.")
    return report


def apply_handoff(root: Path, handoff: Path, expected_base: str) -> dict[str, object]:
    report = validate_handoff(root, handoff, expected_base)
    release = {
        "tag": f"v{report['version']}",
        "version": str(report["version"]),
        "url": str(report["release_url"]),
    }
    expected_plan = candidate_plan(root, release)
    for key in ("outcome", "version", "candidate", "fallback", "removed"):
        if report.get(key) != expected_plan.get(key):
            raise AutomationError(f"Release handoff {key} does not match the current base tree.")
    for relative in ("README.md", str(PACKAGE_DIR / "Manifest"), str(PACKAGE_DIR / report["candidate"])):
        source = handoff / relative
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    if report.get("removed"):
        (root / PACKAGE_DIR / str(report["removed"])).unlink()
    report["branch"] = f"automation/spotatui-v{report['version']}"
    report["title"] = f"[release-bump] spotatui v{report['version']}"
    report["body"] = pull_request_body(report)
    return report


def pull_request_body(report: dict[str, object]) -> str:
    added = report.get("crate_added", [])
    removed = report.get("crate_removed", [])
    lines = [
        f"# spotatui release [{report['tag']}]({report['release_url']})",
        "",
        "## Package rotation",
        "",
        f"- Candidate: `{report['candidate']}`",
        f"- Fallback retained: `{report['fallback']}`",
    ]
    if report.get("removed"):
        lines.append(f"- Older fallback removed: `{report['removed']}`")
    lines.extend(["", "## Cargo dependency changes", ""])
    lines.append(f"- Added registry crates: {len(added)}")
    lines.append(f"- Removed registry crates: {len(removed)}")
    if added:
        lines.extend(["", "Added:", "", *[f"- `{item}`" for item in added]])
    if removed:
        lines.extend(["", "Removed:", "", *[f"- `{item}`" for item in removed]])
    lines.extend(
        [
            "",
            "## Automated checks",
            "",
            "- Stable GitHub release tag matches upstream `Cargo.toml` package.version.",
            "- Existing USE mapping, backend, Gentoo dependencies, patches, Cargo feature map, crate licenses, and native Cargo integration were checked for compatibility.",
            "- `pycargoebuild` refreshed CRATES from the release Cargo.lock.",
            "- Manifest generated; CRATES/Manifest consistency and `pkgcheck scan --profile default/linux/amd64/23.0 --exit` passed.",
            "- Python automation unit tests passed.",
            "- No client build or runtime test is claimed by these metadata checks.",
            "",
            "## Manual gates before accepting",
            "",
            "- [ ] Review upstream release notes and the full ebuild/Manifest diff.",
            "- [ ] Check whether Cargo changes require new Gentoo system dependencies or packaging decisions.",
            "- [ ] Run `emerge -pv media-sound/spotatui` and build the candidate with the intended USE flags.",
            "- [ ] Test Spotify and enabled optional features at runtime.",
            "- [ ] Manually accept the Draft PR when review is complete.",
            "",
            "GitHub Actions uses `GITHUB_TOKEN`; its push and Draft PR creation do not trigger a second workflow run. Preparation ran the required checks in this workflow.",
        ]
    )
    return "\n".join(lines)


def api_json(url: str, token: str | None = None) -> object:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "spotatui-overlay-release-automation"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        raise AutomationError(f"GitHub API request failed with HTTP {error.code}.") from error
    except (URLError, json.JSONDecodeError) as error:
        raise AutomationError(f"GitHub API request failed: {error}.") from error


def discover(root: Path, repository: str, token: str | None) -> dict[str, object]:
    if not REPOSITORY_RE.fullmatch(repository):
        raise AutomationError("GITHUB_REPOSITORY is not in owner/repository form.")
    payload = api_json(f"{API}/repos/{UPSTREAM}/releases/latest")
    release = stable_release(payload)
    if release is None:
        return {"outcome": "ignored-release"}
    plan = candidate_plan(root, release)
    if plan["outcome"] == "up-to-date":
        return {**plan, "release": release}
    branch = f"automation/spotatui-{release['tag']}"
    owner = repository.split("/", 1)[0]
    url = f"{API}/repos/{repository}/pulls?state=all&head={quote(owner + ':' + branch)}&per_page=100"
    pulls = api_json(url, token)
    if not isinstance(pulls, list):
        raise AutomationError("GitHub returned an invalid pull request list.")
    if pulls:
        return {"outcome": "already-processed", "release": release, "branch": branch}
    return {**plan, "release": release, "branch": branch}


def write_output(path: Path, key_values: dict[str, object]) -> None:
    path.write_text("".join(f"{key}={str(value).lower()}\n" for key, value in key_values.items()), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    discovery = subparsers.add_parser("discover")
    discovery.add_argument("--root", type=Path, default=Path.cwd())
    discovery.add_argument("--repository", required=True)
    discovery.add_argument("--token", default=os.environ.get("GH_TOKEN"))
    discovery.add_argument("--result", type=Path, required=True)
    discovery.add_argument("--summary", type=Path)
    discovery.add_argument("--github-output", type=Path)
    discovery.add_argument("--dry-run", action="store_true")
    check = subparsers.add_parser("check-manifest")
    check.add_argument("--root", type=Path, default=Path.cwd())
    prep = subparsers.add_parser("prepare")
    prep.add_argument("--root", type=Path, default=Path.cwd())
    prep.add_argument("--result", type=Path, required=True)
    prep.add_argument("--output", type=Path, required=True)
    prep.add_argument("--base-commit", required=True)
    prep.add_argument("--dry-run", action="store_true")
    apply = subparsers.add_parser("apply")
    apply.add_argument("--root", type=Path, default=Path.cwd())
    apply.add_argument("--handoff", type=Path, required=True)
    apply.add_argument("--expected-base", required=True)
    apply.add_argument("--pull-request-request", type=Path, required=True)
    apply.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "check-manifest":
            check_manifest(args.root)
            return 0
        if args.command == "discover":
            result = discover(args.root, args.repository, args.token)
            result["dry_run"] = args.dry_run
            args.result.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            if args.github_output:
                write_output(args.github_output, {"outcome": result["outcome"], "release_tag": result.get("release", {}).get("tag", "") if isinstance(result.get("release"), dict) else "", "available": result["outcome"] == "release-available"})
            if args.summary:
                args.summary.write_text(
                    "## spotatui release discovery\n\n"
                    f"- Outcome: `{result['outcome']}`\n"
                    + (f"- Upstream: {result['release']['url']}\n" if isinstance(result.get("release"), dict) else "")
                    + (f"- Candidate: `{result['version']}`\n- Fallback: `{result['fallback']}`\n" if result.get("outcome") == "release-available" else "")
                    + ("- Dry run: no source preparation or publication was performed.\n" if args.dry_run else ""),
                    encoding="utf-8",
                )
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "prepare":
            result = json.loads(args.result.read_text(encoding="utf-8"))
            release = result.get("release")
            if not isinstance(release, dict) or result.get("outcome") != "release-available":
                raise AutomationError("No new stable release is available for preparation.")
            report = prepare_candidate(args.root, release, args.output, dry_run=args.dry_run)
            report["base_commit"] = args.base_commit
            if not args.dry_run:
                handoff = args.output / "release-handoff.json"
                handoff.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(report, indent=2))
            return 0
        if args.command == "apply":
            report = apply_handoff(args.root, args.handoff, args.expected_base)
            request = {
                "base": "main",
                "body": report["body"],
                "draft": True,
                "head": report["branch"],
                "title": report["title"],
            }
            args.pull_request_request.write_text(json.dumps(request) + "\n", encoding="utf-8")
            if args.github_output:
                write_output(args.github_output, {"branch": report["branch"], "tag": report["tag"], "candidate": report["version"]})
            print(json.dumps({key: report[key] for key in ("branch", "title", "version")}, indent=2))
            return 0
    except (AutomationError, OSError, json.JSONDecodeError) as error:
        print(f"Release automation error: {error}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
