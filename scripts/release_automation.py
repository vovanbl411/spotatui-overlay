#!/usr/bin/env python3
"""Discover and prepare stable spotatui releases for manual Gentoo review."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
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
ALLOWED_HANDOFF = {str(PACKAGE_DIR / "Manifest")}
HANDOFF_METADATA = "release-handoff.json"
FULL_SHA = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
REGISTRY = "registry+https://github.com/rust-lang/crates.io-index"
PYCARGOEBUILD_VERSION = "0.16.0"
SOURCE_DECLARATION = (
    f'SRC_URI="https://github.com/{UPSTREAM}/archive/refs/tags/v${{PV}}.tar.gz'
    ' -> ${PN}-${PV}.tar.gz\n\t${CARGO_CRATE_URIS}"'
)
PREPARATION_CHECKS = (
    "Source verification",
    "Cargo contract",
    "CRATES generation and integrity",
    "Manifest generation",
    "Manifest verification",
    "pkgcheck",
)


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
    if tag != f"v{version}" or url != expected:
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
    """Check that crate DIST entries cover CRATES from every saved ebuild."""
    versions = packaged_versions(root)
    expected = {
        f"{crate.replace('@', '-')}.crate"
        for packaged in versions
        for crate in crates_from_ebuild(packaged.path.read_text(encoding="utf-8"))
    }
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

def reachable_cargo_features(manifest: dict, roots: set[str]) -> set[str]:
    features = feature_map(manifest)
    reachable = set()
    pending = list(roots)

    while pending:
        name = pending.pop()

        if name in reachable or name not in features:
            continue

        dependencies = features[name]
        if not isinstance(dependencies, list) or not all(
            isinstance(item, str) for item in dependencies
        ):
            raise AutomationError(f"Invalid Cargo feature definition: {name}.")

        reachable.add(name)

        for dependency in dependencies:
            if dependency in features and dependency not in reachable:
                pending.append(dependency)

    return reachable

def native_dependency_map(manifest: dict) -> dict[tuple[str, str, str], str]:
    result: dict[tuple[str, str, str], str] = {}

    def visit(target: str, tables: dict) -> None:
        for kind in ("dependencies", "build-dependencies"):
            dependencies = tables.get(kind)
            if not isinstance(dependencies, dict):
                continue
            for name, specification in dependencies.items():
                package = specification.get("package", name) if isinstance(specification, dict) else name
                if any(
                    item in NATIVE_CRATES or NATIVE_CRATE_RE.search(item)
                    for item in (name, package) if isinstance(item, str)
                ):
                    result[target, kind, name] = json.dumps(specification, sort_keys=True)

    visit("", manifest)
    targets = manifest.get("target")
    if isinstance(targets, dict):
        for target, tables in targets.items():
            if isinstance(tables, dict):
                visit(target, tables)
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
    old_features = feature_map(old_manifest)
    new_features = feature_map(new_manifest)
    if old_package.get("version") != str(current_version):
        raise AutomationError(
            f"Current ebuild version {current_version} does not match packaged upstream Cargo.toml "
            f"version {old_package.get('version')!r}."
        )
    if new_version != str(expected):
        raise AutomationError(
            f"Release tag v{expected} does not match Cargo package.version {new_version!r}."
        )
    check_upstream_license(old_package, new_package, current_source, candidate_source)
    for package, label in ((old_package, "packaged"), (new_package, "candidate")):
        rust_version = package.get("rust-version")
        if rust_version is not None and not isinstance(rust_version, str):
            raise AutomationError(f"The {label} Rust minimum version is invalid.")
    if old_package.get("rust-version") != new_package.get("rust-version"):
        raise AutomationError("Upstream rust-version changed; review the ebuild Rust requirement.")
    missing = sorted(mapped_features - new_features.keys())
    if missing:
        raise AutomationError(
            f"Ebuild enables Cargo features missing upstream: {', '.join(missing)}."
        )
    
    reachable = (
        reachable_cargo_features(old_manifest, mapped_features)
        | reachable_cargo_features(new_manifest, mapped_features)
    )
    
    changed = sorted(
        name for name in reachable
        if old_features.get(name) != new_features.get(name)
    )
    
    if changed:
        raise AutomationError(
            "Upstream Cargo features changed; review the USE mapping manually: "
            + ", ".join(changed) + "."
        )
    old_native, new_native = native_dependency_map(old_manifest), native_dependency_map(new_manifest)
    if old_native != new_native:
        changed_native = [
            f"{target or 'all targets'} / {kind} / {name}"
            for target, kind, name in sorted(old_native.keys() | new_native.keys())
            if old_native.get((target, kind, name)) != new_native.get((target, kind, name))
        ]
        raise AutomationError(
            "Native/system Cargo dependencies changed; review Gentoo dependencies manually: "
            + ", ".join(changed_native) + "."
        )
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


def check_upstream_license(
    old_package: dict,
    new_package: dict,
    current_source: Path,
    candidate_source: Path,
) -> None:
    """Stop when the application's declared license or license file changes."""
    old_license = old_package.get("license")
    new_license = new_package.get("license")
    if old_license != new_license:
        raise AutomationError("Upstream package.license changed; review the ebuild LICENSE manually.")

    old_file = old_package.get("license-file")
    new_file = new_package.get("license-file")
    if old_file != new_file:
        raise AutomationError("Upstream package.license-file changed; review the ebuild LICENSE manually.")
    if old_file is None:
        return
    if not isinstance(old_file, str) or not old_file:
        raise AutomationError("Upstream package.license-file is invalid; review the ebuild LICENSE manually.")

    relative = Path(old_file)
    if relative.is_absolute() or ".." in relative.parts:
        raise AutomationError(
            "Upstream package.license-file is unsafe to compare; review the ebuild LICENSE manually."
        )
    try:
        old_path = (current_source / relative).resolve()
        new_path = (candidate_source / relative).resolve()
        old_path.relative_to(current_source.resolve())
        new_path.relative_to(candidate_source.resolve())
        old_content = old_path.read_bytes()
        new_content = new_path.read_bytes()
    except (OSError, RuntimeError, ValueError) as error:
        raise AutomationError(
            "Unable to compare upstream package.license-file; review the ebuild LICENSE manually."
        ) from error
    if old_content != new_content:
        raise AutomationError(
            "Upstream package.license-file contents changed; review the ebuild LICENSE manually."
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
        if source != REGISTRY:
            raise AutomationError(f"Cargo.lock contains an unsupported crate source for {item.get('name')}.")
        name, version = item.get("name"), item.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            raise AutomationError("Cargo.lock contains an invalid registry crate name or version.")
        crate = f"{name}@{version}"
        if not re.fullmatch(r"[A-Za-z0-9_-]+@[A-Za-z0-9.+_-]+", crate) or crate in crates:
            raise AutomationError(f"Cargo.lock contains an invalid or duplicate registry crate: {crate}.")
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


def source_url(tag: str) -> str:
    return f"https://github.com/{UPSTREAM}/archive/refs/tags/{tag}.tar.gz"


def resolve_tag(tag: str) -> str:
    if tag != f"v{Version.parse(tag)}":
        raise AutomationError(f"Noncanonical upstream tag: {tag}.")
    endpoint = f"{API}/repos/{UPSTREAM}/git"
    ref = f"refs/tags/{tag}"
    payload = api_json(f"{endpoint}/ref/tags/{tag}")
    if not isinstance(payload, dict) or payload.get("ref") != ref:
        raise AutomationError(f"Upstream tag API did not return {ref}.")
    obj = payload.get("object")
    seen = set()
    while isinstance(obj, dict):
        sha = obj.get("sha")
        if not isinstance(sha, str) or FULL_SHA.fullmatch(sha) is None or sha in seen:
            break
        if obj.get("type") == "commit":
            return sha
        if obj.get("type") != "tag" or len(seen) >= 8:
            break
        seen.add(sha)
        payload = api_json(f"{endpoint}/tags/{sha}")
        if not isinstance(payload, dict) or payload.get("sha") != sha:
            break
        obj = payload.get("object")
    raise AutomationError(f"Upstream {tag} does not resolve to an unambiguous commit SHA.")


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def distfile_integrity(path: Path) -> dict[str, int | str]:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise AutomationError(f"Distfile is not a regular file: {path.name}.")
    hashes = {"BLAKE2B": hashlib.blake2b(), "SHA512": hashlib.sha512()}
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(block)
            for digest in hashes.values():
                digest.update(block)
    return {"size": size, **{key: digest.hexdigest() for key, digest in hashes.items()}}


def valid_distfile_evidence(entry: object) -> bool:
    return (
        isinstance(entry, dict)
        and entry.keys() == {"size", "BLAKE2B", "SHA512"}
        and type(entry["size"]) is int
        and entry["size"] > 0
        and all(isinstance(entry[key], str) and re.fullmatch(r"[0-9a-f]{128}", entry[key])
                for key in ("BLAKE2B", "SHA512"))
    )


def manifest_distfiles(text: str) -> dict[str, dict[str, int | str]]:
    entries = {}
    for line in text.splitlines():
        fields = line.split()
        if (len(fields) != 7 or fields[0] != "DIST"
                or re.fullmatch(r"[A-Za-z0-9_.+-]+", fields[1]) is None
                or fields[1] in entries or re.fullmatch(r"[0-9]{1,20}", fields[2]) is None):
            raise AutomationError("Invalid or duplicate DIST entry in thin Manifest.")
        entry = {
            "size": int(fields[2]),
            fields[3]: fields[4].lower(),
            fields[5]: fields[6].lower(),
        }
        if not valid_distfile_evidence(entry):
            raise AutomationError(f"Invalid Gentoo Manifest digests for {fields[1]}.")
        entries[fields[1]] = entry
    return entries


def source_distfile(version: str | Version) -> str:
    return f"spotatui-{version}.tar.gz"


def extract_source(archive: Path, destination: Path, version: Version, commit: str) -> Path:
    prefix = f"spotatui-{version}"
    try:
        with tempfile.TemporaryFile(dir=destination.parent) as raw, gzip.open(archive, "rb") as compressed:
            for block in iter(lambda: compressed.read(1024 * 1024), b""):
                if raw.tell() + len(block) > 256 * 1024 * 1024:
                    raise AutomationError(f"Source tar for v{version} is too large.")
                raw.write(block)
            raw.seek(0)
            with tarfile.open(fileobj=raw, mode="r:") as bundle:
                if bundle.pax_headers.get("comment") != commit:
                    raise AutomationError(f"Source archive commit does not match v{version} ({commit}).")
                paths = set()
                for member in bundle:
                    parts = member.name.split("/")
                    if (parts[0] != prefix or set(parts) & {"", ".", ".."}
                            or member.name in paths
                            or member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE)
                            or "\\" in member.name or (len(parts) == 1 and not member.isdir())):
                        raise AutomationError(f"Unexpected path or type in source archive v{version}: {member.name!r}.")
                    paths.add(member.name)
                raw.seek(bundle.offset)
                if raw.read(1024) != bytes(1024) or any(
                    any(block) for block in iter(lambda: raw.read(65536), b"")
                ):
                    raise AutomationError(f"Source tar for v{version} has an invalid terminator or trailing data.")
                if bundle.pax_headers.get("comment") != commit:
                    raise AutomationError(f"Source archive commit changed within v{version}.")
                for name in ("Cargo.toml", "Cargo.lock"):
                    if not bundle.getmember(f"{prefix}/{name}").isfile():
                        raise AutomationError(f"Source archive v{version} does not contain a regular {name}.")
                bundle.extractall(destination, filter="data")
    except (EOFError, KeyError, OSError, tarfile.TarError, zlib.error) as error:
        raise AutomationError(f"Source archive v{version} failed gzip/tar verification: {error}.") from error
    return destination / prefix


def download_source(
    version: Version, commit: str, archive: Path, destination: Path, *, accepted: dict | None = None,
) -> tuple[Path, dict[str, int | str]]:
    tag = f"v{version}"
    url = source_url(tag)
    effective = run_checked(
        f"Download upstream {tag}",
        ["curl", "--disable", "--fail", "--silent", "--show-error", "--location", "--max-redirs", "3",
         "--proto", "=https", "--proto-redir", "=https", "--max-time", "180", "--max-filesize", "67108864",
         "--output", str(archive), "--write-out", "%{url_effective}", url],
        capture_output=True,
    ).stdout
    if effective not in {
        url, f"https://codeload.github.com/{UPSTREAM}/tar.gz/refs/tags/{tag}",
        f"https://codeload.github.com/{UPSTREAM}/tar.gz/{tag}",
    }:
        raise AutomationError(f"Noncanonical GitHub archive URL for {tag}.")
    evidence = distfile_integrity(archive)
    if accepted is not None and evidence != accepted:
        raise AutomationError(f"Packaged source {tag} does not match the accepted Manifest.")
    return extract_source(archive, destination, version, commit), evidence


def run_checked(operation: str, command: list[str], **kwargs) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, check=True, text=True, **kwargs)
    except FileNotFoundError as error:
        raise AutomationError(f"{operation}: {command[0]} is unavailable.") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        context = f": {detail[-2000:]}" if detail else "."
        raise AutomationError(f"{operation} failed (exit {error.returncode}){context}") from error


def summarize(summary: Path | None, message: str) -> None:
    print(message, flush=True)
    if summary is not None:
        with summary.open("a", encoding="utf-8") as stream:
            stream.write(f"- {message}\n")


@contextmanager
def preparation_check(summary: Path | None, name: str):
    print(f"{name}: starting", flush=True)
    try:
        yield
    except (AutomationError, OSError) as error:
        summarize(summary, f"{name}: failed ({error})")
        raise
    summarize(summary, f"{name}: passed")


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
    version = run_checked(
        "Read pycargoebuild version",
        [sys.executable, "-c", "from pycargoebuild import __version__; print(__version__)"],
        cwd=temp_dir, capture_output=True,
    ).stdout.strip()
    if version != PYCARGOEBUILD_VERSION:
        raise AutomationError(f"Unreviewed pycargoebuild version {version}; expected {PYCARGOEBUILD_VERSION}.")
    # 0.16.0 verifies registry SHA-256 from Cargo.lock with these flags.
    run_checked(
        "pycargoebuild CRATES generation and checksum verification",
        [
            "pycargoebuild", "--no-config", "--no-manifest", "--distdir", str(distdir),
            "--license-mapping", str(license_mapping), "--input", str(input_ebuild),
            "--output", str(generated), str(source_dir),
        ],
        cwd=temp_dir, capture_output=True,
    )
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


def crate_distfiles(crates: set[str]) -> set[str]:
    names = {f"{crate.replace('@', '-')}.crate" for crate in crates}
    if len(crates) != len(names):
        raise AutomationError("Cargo crate entries collide on a distfile name.")
    return names


def version_distfiles(version: PackagedVersion) -> set[str]:
    content = version.path.read_text(encoding="utf-8")
    if re.findall(r'^SRC_URI="[^"]*"', content, re.MULTILINE) != [SOURCE_DECLARATION]:
        raise AutomationError(f"{version.path.name}: unexpected source URL or distfile declaration.")
    return crate_distfiles(crates_from_ebuild(content)) | {source_distfile(version.version)}


def check_candidate_manifest(
    entries: dict, accepted: dict, retained: set[str], candidate: set[str], archive: str, archive_evidence: dict,
) -> None:
    expected_names = retained | candidate
    if entries.keys() != expected_names:
        raise AutomationError(f"Candidate Manifest has unexpected or missing distfiles: {sorted(entries.keys() ^ expected_names)[:8]}.")
    for name in sorted(retained):
        if name not in accepted or entries[name] != accepted[name]:
            raise AutomationError(f"Candidate Manifest changed the accepted fallback digest for {name}.")
    if entries.get(archive) != archive_evidence:
        raise AutomationError(f"Candidate Manifest does not match the verified source archive {archive}.")


def verify_candidate_distfiles(distdir: Path, entries: dict, verified: dict) -> None:
    for name, evidence in verified.items():
        if entries.get(name) != evidence or distfile_integrity(distdir / name) != evidence:
            raise AutomationError(f"Candidate distfile changed during Manifest generation: {name}.")


def prepare_candidate(
    root: Path, release: dict[str, str], output: Path, *, dry_run: bool = False, summary: Path | None = None,
) -> dict[str, object]:
    root, output = root.resolve(), output.resolve()
    plan = candidate_plan(root, release)
    if plan["outcome"] != "release-available" or dry_run:
        return plan
    if output.exists():
        raise AutomationError(f"Candidate output directory already exists: {output}.")
    commit = release.get("commit", "")
    release_id = release.get("release_id", "")
    if (not isinstance(commit, str) or FULL_SHA.fullmatch(commit) is None
            or not isinstance(release_id, str) or re.fullmatch(r"[1-9][0-9]*", release_id) is None):
        raise AutomationError("Candidate release is missing its immutable discovery identity.")
    version = Version.parse(release["version"])
    current = packaged_versions(root)[-1]
    current_ebuild = current.path.read_text(encoding="utf-8")
    work_dir = output.with_name(output.name + "-work")
    if work_dir.exists():
        raise AutomationError(f"Preparation work directory already exists: {work_dir}.")
    distdir = work_dir / "distfiles"
    distdir.mkdir(parents=True)
    archive_name = source_distfile(version)
    archive_path = distdir / archive_name
    manifest_path = PACKAGE_DIR / "Manifest"

    with preparation_check(summary, "Source verification"):
        retained = version_distfiles(current)
        accepted = manifest_distfiles((root / manifest_path).read_text(encoding="utf-8"))
        current_archive = source_distfile(current.version)
        if not retained <= accepted.keys():
            raise AutomationError("Accepted Manifest is missing current package distfiles.")
        current_commit = resolve_tag(f"v{current.version}")
        old_source, _ = download_source(
            current.version, current_commit, work_dir / current_archive, work_dir / "current",
            accepted=accepted[current_archive],
        )
        source, archive_evidence = download_source(version, commit, archive_path, work_dir / "candidate")
        cargo_toml_sha256 = file_sha256(source / "Cargo.toml")
        cargo_lock_sha256 = file_sha256(source / "Cargo.lock")
        summarize(summary, f"Verified v{version} at {commit}, archive SHA512 {archive_evidence['SHA512']}")

    with preparation_check(summary, "Cargo contract"):
        check_source_contract(old_source, source, current.version, version, configured_cargo_features(current_ebuild))
        current_crates = crates_from_ebuild(current_ebuild)
        if current_crates != lock_crates(old_source):
            raise AutomationError("The accepted CRATES do not match the verified comparison Cargo.lock.")
        locked_crates = lock_crates(source)

    with preparation_check(summary, "CRATES generation and integrity"):
        generated = _generated_crates(source, work_dir, current_ebuild)
        if (generated != locked_crates or file_sha256(source / "Cargo.lock") != cargo_lock_sha256
                or file_sha256(source / "Cargo.toml") != cargo_toml_sha256):
            raise AutomationError("CRATES generation did not preserve the verified Cargo inputs.")
        candidate_names = crate_distfiles(generated) | {archive_name}
        # Bridge pycargoebuild's successful lock checksum check to Portage's Manifest algorithms.
        verified = {name: distfile_integrity(distdir / name) for name in sorted(candidate_names)}
        if verified[archive_name] != archive_evidence:
            raise AutomationError("The verified candidate source archive changed during CRATES generation.")
        summarize(summary, f"pycargoebuild {PYCARGOEBUILD_VERSION} verified {len(generated)} registry crates")

    for relative in (
        Path("metadata/layout.conf"), Path("profiles/categories"), Path("profiles/repo_name"),
        PACKAGE_DIR / "metadata.xml", manifest_path, current.path.relative_to(root),
    ):
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / relative, destination)

    candidate_path = output / PACKAGE_DIR / str(plan["candidate"])
    candidate_path.write_text(replace_crates(current_ebuild, generated), encoding="utf-8")
    with preparation_check(summary, "Manifest generation"):
        run_checked(
            "Portage ebuild manifest", ["ebuild", str(candidate_path), "manifest"],
            env={**os.environ, "DISTDIR": str(distdir)}, cwd=output,
        )

    with preparation_check(summary, "Manifest verification"):
        check_manifest(output)
        entries = manifest_distfiles((output / manifest_path).read_text(encoding="utf-8"))
        check_candidate_manifest(entries, accepted, retained, candidate_names, archive_name, archive_evidence)
        verify_candidate_distfiles(distdir, entries, verified)

    files = {
        name: file_sha256(output / name)
        for name in sorted(ALLOWED_HANDOFF | {str(candidate_path.relative_to(output))})
    }
    with preparation_check(summary, "pkgcheck"):
        run_checked(
            "pkgcheck metadata scan",
            ["pkgcheck", "scan", "--repo", str(output), "--profile", "default/linux/amd64/23.0", "--exit"],
            cwd=output,
        )
        if any(file_sha256(output / name) != digest for name, digest in files.items()):
            raise AutomationError("pkgcheck changed the files awaiting handoff.")

    return {
        **plan,
        "tag": release["tag"],
        "release_id": release_id,
        "release_url": release["url"],
        "upstream_commit": commit,
        "source_url": source_url(release["tag"]),
        "archive": archive_evidence,
        "cargo_toml_sha256": cargo_toml_sha256,
        "cargo_lock_sha256": cargo_lock_sha256,
        "crate_added": sorted(generated - current_crates),
        "crate_removed": sorted(current_crates - generated),
        "pycargoebuild_version": PYCARGOEBUILD_VERSION,
        "validation": dict.fromkeys(PREPARATION_CHECKS, "passed"),
        "files": files,
    }


def handoff_context(base: str, run_id: str, run_attempt: str) -> dict[str, str]:
    if FULL_SHA.fullmatch(base) is None:
        raise AutomationError("Expected base commit is not a full Git SHA.")
    if any(re.fullmatch(r"[1-9][0-9]*", value) is None for value in (run_id, run_attempt)):
        raise AutomationError("Handoff requires the workflow run ID and attempt.")
    return {"base_commit": base, "run_id": run_id, "run_attempt": run_attempt}


def mandatory_results() -> dict[str, str]:
    return dict.fromkeys(("Python unit tests", *PREPARATION_CHECKS), "passed")


def write_handoff(candidate: Path, bundle: Path, report: dict, context: dict, unit_tests: str) -> None:
    if unit_tests != "success" or report.get("validation") != dict.fromkeys(PREPARATION_CHECKS, "passed"):
        raise AutomationError("Handoff requires successful results from all mandatory checks.")
    names = ALLOWED_HANDOFF | {str(PACKAGE_DIR / report["candidate"])}
    files = {name: (candidate / name).read_bytes() for name in sorted(names)}
    if {name: hashlib.sha256(data).hexdigest() for name, data in files.items()} != report["files"]:
        raise AutomationError("Candidate files changed after validation.")
    metadata = {**report, **context, "validation": mandatory_results()}
    files[HANDOFF_METADATA] = (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode()
    with zipfile.ZipFile(bundle, "x") as archive:
        for name, data in files.items():
            member = zipfile.ZipInfo(name)
            member.create_system = 3
            member.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(member, data, compress_type=zipfile.ZIP_DEFLATED)


def check_upload(bundle: Path, artifact_id: str, artifact_digest: str) -> bytes:
    if re.fullmatch(r"[1-9][0-9]*", artifact_id) is None:
        raise AutomationError("Upload did not return a valid artifact ID.")
    expected = artifact_digest.removeprefix("sha256:")
    if SHA256.fullmatch(expected) is None:
        raise AutomationError("Upload did not return a valid artifact SHA-256.")
    if not stat.S_ISREG(bundle.lstat().st_mode):
        raise AutomationError("Downloaded handoff is not a regular file.")
    size_limit = 8 * 1024 * 1024
    with bundle.open("rb") as stream:
        payload = stream.read(size_limit + 1)
    if len(payload) > size_limit or hashlib.sha256(payload).hexdigest() != expected:
        raise AutomationError("Handoff archive does not match the trusted artifact digest.")
    return payload


def validate_handoff(root: Path, bundle: Path, context: dict, artifact_id: str, artifact_digest: str) -> tuple[dict, dict[str, bytes]]:
    payload = check_upload(bundle, artifact_id, artifact_digest)
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = archive.infolist()
            if len(members) != 3 or len({member.filename for member in members}) != 3:
                raise AutomationError("Release handoff file allowlist mismatch.")
            for member in members:
                if (member.orig_filename != member.filename or member.create_system != 3
                        or (member.external_attr >> 16) != (stat.S_IFREG | 0o644)
                        or member.file_size > 4 * 1024 * 1024 or member.flag_bits & 1):
                    raise AutomationError(f"Unexpected file type or size in handoff: {member.filename!r}.")
            report = json.loads(archive.read(HANDOFF_METADATA))
            if not isinstance(report, dict) or any(report.get(key) != value for key, value in context.items()):
                raise AutomationError("Handoff base commit, run ID or attempt does not match Prepare.")
            if report.get("validation") != mandatory_results():
                raise AutomationError("Handoff is missing successful mandatory checks.")
            version = Version.parse(str(report.get("version", "")))
            tag = f"v{version}"
            release = {"tag": tag, "version": str(version), "url": str(report.get("release_url", ""))}
            expected_plan = candidate_plan(root, release)
            if expected_plan["outcome"] != "release-available" or any(
                report.get(key) != value for key, value in expected_plan.items()
            ):
                raise AutomationError("Release handoff rotation plan does not match the current base tree.")
            allowed = ALLOWED_HANDOFF | {str(PACKAGE_DIR / expected_plan["candidate"])}
            if {member.filename for member in members} != allowed | {HANDOFF_METADATA}:
                raise AutomationError("Release handoff file allowlist mismatch.")
            files = {name: archive.read(name) for name in sorted(allowed)}
    except (KeyError, UnicodeError, json.JSONDecodeError, zipfile.BadZipFile, NotImplementedError, zlib.error) as error:
        raise AutomationError(f"Invalid release handoff ZIP or metadata: {error}.") from error

    if (report.get("tag") != tag or report.get("source_url") != source_url(tag)
            or FULL_SHA.fullmatch(str(report.get("upstream_commit", ""))) is None
            or re.fullmatch(r"[1-9][0-9]*", str(report.get("release_id", ""))) is None
            or report.get("pycargoebuild_version") != PYCARGOEBUILD_VERSION
            or any(SHA256.fullmatch(str(report.get(key, ""))) is None
                   for key in ("cargo_toml_sha256", "cargo_lock_sha256"))
            or not valid_distfile_evidence(report.get("archive"))):
        raise AutomationError("Handoff upstream or Cargo identity is invalid.")
    if report.get("files") != {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}:
        raise AutomationError("Handoff file contents do not match the validated digests.")

    current = packaged_versions(root)[-1]
    current_ebuild = current.path.read_text(encoding="utf-8")
    candidate_ebuild = files[str(PACKAGE_DIR / expected_plan["candidate"])].decode()
    crates = crates_from_ebuild(candidate_ebuild)
    if candidate_ebuild != replace_crates(current_ebuild, crates):
        raise AutomationError("Handoff ebuild changes the accepted packaging outside CRATES.")
    current_crates = crates_from_ebuild(current_ebuild)
    if report.get("crate_added") != sorted(crates - current_crates) or report.get("crate_removed") != sorted(current_crates - crates):
        raise AutomationError("Handoff crate changes do not match the ebuild diff.")
    manifest_path = PACKAGE_DIR / "Manifest"
    archive_name = source_distfile(version)
    check_candidate_manifest(
        manifest_distfiles(files[str(manifest_path)].decode()),
        manifest_distfiles((root / manifest_path).read_text(encoding="utf-8")),
        version_distfiles(current), crate_distfiles(crates) | {archive_name}, archive_name, report["archive"],
    )
    return report, files


def apply_handoff(root: Path, bundle: Path, context: dict, artifact_id: str, artifact_digest: str) -> tuple[dict, dict[str, bytes]]:
    report, files = validate_handoff(root, bundle, context, artifact_id, artifact_digest)
    candidate = root / PACKAGE_DIR / report["candidate"]
    manifest = root / PACKAGE_DIR / "Manifest"
    if not stat.S_ISREG(manifest.lstat().st_mode):
        raise AutomationError("Handoff target Manifest is not a regular file.")
    # Exclusive creation also rejects ignored files and dangling symlinks.
    with candidate.open("xb") as stream:
        stream.write(files[str(candidate.relative_to(root))])
    manifest.write_bytes(files[str(manifest.relative_to(root))])
    if report["removed"]:
        (root / PACKAGE_DIR / report["removed"]).unlink()
    return report, files


def api_json(url: str, token: str | None = None) -> object:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "spotatui-overlay-release-automation"}
    request = Request(url, headers=headers)
    if token:
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        raise AutomationError(f"GitHub GET {url} failed with HTTP {error.code}.") from error
    except (URLError, json.JSONDecodeError) as error:
        raise AutomationError(f"GitHub GET {url} failed: {error}.") from error


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
    release_id = payload.get("id")
    if type(release_id) is not int or release_id < 1:
        raise AutomationError("Upstream releases/latest did not return a valid release ID.")
    release.update(release_id=str(release_id), commit=resolve_tag(release["tag"]))
    return {**plan, "release": release}


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
    prep.add_argument("--handoff", type=Path)
    prep.add_argument("--summary", type=Path)
    prep.add_argument("--unit-tests", default="")
    upload = subparsers.add_parser("check-upload")
    upload.add_argument("--handoff", type=Path, required=True)
    upload.add_argument("--artifact-id", required=True)
    upload.add_argument("--artifact-digest", required=True)
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
                args.github_output.write_text(f"outcome={result['outcome']}\n", encoding="utf-8")
            if args.summary:
                args.summary.write_text(
                    "## spotatui release discovery\n\n"
                    f"- Outcome: `{result['outcome']}`\n"
                    + (f"- Upstream: {result['release']['url']}\n" if isinstance(result.get("release"), dict) else "")
                    + (f"- Candidate: `{result['version']}` at `{result['release']['commit']}`\n"
                       f"- Fallback: `{result['fallback']}`\n" if result.get("outcome") == "release-available" else "")
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
            dry_run = result.get("dry_run") is True
            if FULL_SHA.fullmatch(args.base_commit) is None:
                raise AutomationError("Prepare requires a full base commit SHA.")
            report = prepare_candidate(args.root, release, args.output, dry_run=dry_run, summary=args.summary)
            if args.handoff is not None and not dry_run:
                context = handoff_context(args.base_commit, os.getenv("GITHUB_RUN_ID", ""), os.getenv("GITHUB_RUN_ATTEMPT", ""))
                with preparation_check(args.summary, "Handoff ZIP"):
                    write_handoff(args.output, args.handoff, report, context, args.unit_tests)
            print(json.dumps(report, indent=2))
            return 0
        if args.command == "check-upload":
            check_upload(args.handoff, args.artifact_id, args.artifact_digest)
            return 0
    except (AutomationError, OSError, UnicodeError, json.JSONDecodeError) as error:
        if args.command == "discover" and args.summary:
            summarize(args.summary, f"Discover: failed ({error})")
        print(f"Release automation {args.command}: {error}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
