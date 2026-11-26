from __future__ import annotations

import gzip
import hashlib
import io
import json
import stat
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts import release_automation as automation


class ReleaseAutomationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.package = self.root / automation.PACKAGE_DIR
        self.package.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def ebuild(self, name: str, version: str, crates: set[str] | None = None) -> Path:
        path = self.package / name
        crate_entries = sorted(crates or {"serde@1.0.0"})
        crates_block = "\n".join(f"\t{crate}" for crate in crate_entries)
        path.write_text(
            f'EAPI=8\nCRATES="\n{crates_block}\n"\nIUSE="mpris"\n'
            f'{automation.SOURCE_DECLARATION}\nLICENSE="MIT"\n'
            f'RDEPEND="mpris? ( sys-apps/dbus )"\n'
            f'src_configure() {{\n local -a myfeatures=( tui streaming )\n'
            f' use mpris && myfeatures+=( mpris )\n}}\n# {version}\n',
            encoding="utf-8",
        )
        return path

    def test_manifest_accepts_union_of_fallback_and_candidate_crates(self) -> None:
        self.ebuild("spotatui-1.2.3.ebuild", "1.2.3", {"serde@1.0.0", "old-crate@2.0.0"})
        self.ebuild("spotatui-1.2.4.ebuild", "1.2.4", {"serde@1.0.1", "new-crate@3.0.0"})
        crate_archives = {
            "serde-1.0.0.crate",
            "old-crate-2.0.0.crate",
            "serde-1.0.1.crate",
            "new-crate-3.0.0.crate",
        }
        (self.package / "Manifest").write_text(
            "".join(f"DIST {archive} 1 BLAKE2B 0 SHA512 0\n" for archive in sorted(crate_archives)),
            encoding="utf-8",
        )

        self.assertEqual(automation.check_manifest(self.root), len(crate_archives))

    def release(self, version: str) -> dict[str, str]:
        return {
            "tag": f"v{version}",
            "version": version,
            "url": f"https://github.com/{automation.UPSTREAM}/releases/tag/v{version}",
        }

    def cargo_source(
        self,
        path: Path,
        version: str,
        *,
        features: str | None = None,
        native: bool = False,
        package_fields: str = "",
        license_file_content: str | None = None,
    ) -> Path:
        path.mkdir(parents=True)
        path.joinpath("Cargo.toml").write_text(
            f'[package]\nname="spotatui"\nversion="{version}"\nrust-version="1.91"\n'
            + package_fields
            + f'[features]\ntui=[]\nstreaming=[]\ncover-art=[]\ninternet-radio=[]\n'
            f'local-files=[]\nmpris=[]\nqobuz=[]\nsubsonic=[]\naudio-viz=[]\nyoutube=[]\n'
            + ('[dependencies]\nalsa-sys="0.3"\n' if native else "")
            + (features or ""),
            encoding="utf-8",
        )
        path.joinpath("Cargo.lock").write_text(
            'version = 3\n\n[[package]]\nname = "spotatui"\nversion = "'
            + version
            + '"\n\n[[package]]\nname = "serde"\nversion = "1.0.0"\n'
            'source = "registry+https://github.com/rust-lang/crates.io-index"\n',
            encoding="utf-8",
        )
        if license_file_content is not None:
            path.joinpath("LICENSE.txt").write_text(license_file_content, encoding="utf-8")
        return path

    def test_release_tag_parser_rejects_prerelease_and_non_semver(self) -> None:
        release = {
            "tag_name": "v1.2.3-rc.1",
            "html_url": f"https://github.com/{automation.UPSTREAM}/releases/tag/v1.2.3-rc.1",
            "draft": False,
            "prerelease": True,
        }
        self.assertIsNone(automation.stable_release(release))
        release["tag_name"] = "v1.2.4"
        release["html_url"] = f"https://github.com/{automation.UPSTREAM}/releases/tag/v1.2.4"
        release["draft"] = True
        release["prerelease"] = False
        self.assertIsNone(automation.stable_release(release))
        with self.assertRaises(automation.AutomationError):
            automation.Version.parse("1.2.3-rc.1")

    def test_stable_release_and_latest_version_are_recognized(self) -> None:
        release = {
            "tag_name": "v1.2.4",
            "html_url": f"https://github.com/{automation.UPSTREAM}/releases/tag/v1.2.4",
            "draft": False,
            "prerelease": False,
        }
        parsed = automation.stable_release(release)
        self.assertEqual(parsed["version"], "1.2.4")
        self.ebuild("spotatui-1.2.4.ebuild", "1.2.4")
        self.assertEqual(automation.candidate_plan(self.root, parsed)["outcome"], "up-to-date")

    def test_new_release_creates_candidate_plan(self) -> None:
        self.ebuild("spotatui-1.2.3.ebuild", "1.2.3")
        plan = automation.candidate_plan(self.root, self.release("1.2.4"))
        self.assertEqual(plan["outcome"], "release-available")
        self.assertEqual(plan["candidate"], "spotatui-1.2.4.ebuild")

    def test_already_processed_pull_request_is_not_recreated(self) -> None:
        self.ebuild("spotatui-1.2.3.ebuild", "1.2.3")
        old_api = automation.api_json
        automation.api_json = lambda url, token=None: (
            {
                "tag_name": "v1.2.4",
                "html_url": f"https://github.com/{automation.UPSTREAM}/releases/tag/v1.2.4",
                "draft": False,
                "prerelease": False,
            }
            if "/releases/latest" in url
            else [{"number": 4}]
        )
        try:
            result = automation.discover(self.root, "owner/repo", "read-token")
        finally:
            automation.api_json = old_api
        self.assertEqual(result["outcome"], "already-processed")

    def test_first_rotation_keeps_only_existing_revision_as_fallback(self) -> None:
        self.ebuild("spotatui-0.47.0-r1.ebuild", "0.47.0")
        plan = automation.candidate_plan(self.root, self.release("0.48.0"))
        self.assertIsNone(plan["removed"])
        self.assertEqual(plan["fallback"], "spotatui-0.47.0-r1.ebuild")

    def test_rotation_discards_old_fallback_and_keeps_current_revision(self) -> None:
        self.ebuild("spotatui-0.46.0.ebuild", "0.46.0")
        self.ebuild("spotatui-0.47.0-r1.ebuild", "0.47.0")
        plan = automation.candidate_plan(self.root, self.release("0.48.0"))
        self.assertEqual(plan["fallback"], "spotatui-0.47.0-r1.ebuild")
        self.assertEqual(plan["removed"], "spotatui-0.46.0.ebuild")

    def test_crates_are_replaced_from_cargo_lock_without_changing_contract(self) -> None:
        ebuild = self.ebuild("spotatui-1.2.3.ebuild", "1.2.3").read_text()
        updated = automation.replace_crates(ebuild, {"serde@1.0.1", "tokio@1.2.3"})
        self.assertEqual(automation.crates_from_ebuild(updated), {"serde@1.0.1", "tokio@1.2.3"})
        self.assertIn('IUSE="mpris"', updated)
        self.assertIn('RDEPEND="mpris? ( sys-apps/dbus )"', updated)

    def test_new_crate_license_requires_manual_packaging_review(self) -> None:
        current = 'LICENSE="MIT BSD-2"\n'
        generated = current + '\n# Dependent crate licenses\nLICENSE+="\n\tMIT Apache-2.0\n"\n'
        with self.assertRaisesRegex(automation.AutomationError, "not covered by the current LICENSE"):
            automation.check_crate_licenses(current, generated)

    def test_unrelated_upstream_feature_change_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            old = self.cargo_source(base / "old", "1.2.3")
            new = self.cargo_source(
                base / "new", "1.2.4", features="new-source=[]\n"
            )
    
            automation.check_source_contract(
                old, new,
                automation.Version(1, 2, 3),
                automation.Version(1, 2, 4),
                {"tui", "streaming"},
            )

    def test_transitive_feature_change_stops_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
    
            old = self.cargo_source(
                base / "old", "1.2.3", features="helper=[]\n"
            )
            new = self.cargo_source(
                base / "new", "1.2.4",
                features='helper=["streaming"]\n'
            )
    
            for source in (old, new):
                cargo = source / "Cargo.toml"
                cargo.write_text(
                    cargo.read_text().replace(
                        "tui=[]", 'tui=["helper"]'
                    )
                )

            with self.assertRaisesRegex(
                automation.AutomationError, "Cargo features changed"
            ):
                automation.check_source_contract(
                    old, new,
                    automation.Version(1, 2, 3),
                    automation.Version(1, 2, 4),
                    {"tui", "streaming"},
                )

    def test_changed_application_license_stops_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            old = self.cargo_source(base / "old", "1.2.3", package_fields='license="MIT"\n')
            new = self.cargo_source(base / "new", "1.2.4", package_fields='license="GPL-3.0-only"\n')
            with self.assertRaisesRegex(automation.AutomationError, "package.license changed"):
                automation.check_source_contract(
                    old, new, automation.Version(1, 2, 3), automation.Version(1, 2, 4), {"tui", "streaming"}
                )

    def test_changed_application_license_file_stops_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fields = 'license-file="LICENSE.txt"\n'
            old = self.cargo_source(
                base / "old", "1.2.3", package_fields=fields, license_file_content="MIT License\n"
            )
            new = self.cargo_source(
                base / "new", "1.2.4", package_fields=fields, license_file_content="GPL License\n"
            )
            with self.assertRaisesRegex(automation.AutomationError, "license-file contents changed"):
                automation.check_source_contract(
                    old, new, automation.Version(1, 2, 3), automation.Version(1, 2, 4), {"tui", "streaming"}
                )

    def test_native_dependency_change_stops_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            old = self.cargo_source(base / "old", "1.2.3")
            new = self.cargo_source(base / "new", "1.2.4", native=True)
            with self.assertRaisesRegex(automation.AutomationError, "Native/system Cargo dependencies changed"):
                automation.check_source_contract(
                    old, new, automation.Version(1, 2, 3), automation.Version(1, 2, 4), {"tui"}
                )

    def test_native_dependency_change_cannot_be_hidden_by_another_target_or_kind(self) -> None:
        for other_header in ('target."cfg(windows)".dependencies', 'target."cfg(unix)".build-dependencies'):
            with self.subTest(other_header=other_header), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                old = self.cargo_source(base / "old", "1.2.3")
                new = self.cargo_source(base / "new", "1.2.4")
                for source, version in ((old, "0.3"), (new, "0.4")):
                    with (source / "Cargo.toml").open("a", encoding="utf-8") as cargo:
                        cargo.write(
                            f'\n[target."cfg(unix)".dependencies]\nalsa-sys="{version}"\n'
                            f'[{other_header}]\nalsa-sys="0.5"\n'
                        )
                with self.assertRaisesRegex(automation.AutomationError, "Native/system Cargo"):
                    automation.check_source_contract(
                        old, new, automation.Version(1, 2, 3), automation.Version(1, 2, 4), {"tui"}
                    )

    def test_tag_and_package_version_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            old = self.cargo_source(base / "old", "1.2.3")
            new = self.cargo_source(base / "new", "1.2.5")
            with self.assertRaisesRegex(automation.AutomationError, "does not match Cargo"):
                automation.check_source_contract(
                    old, new, automation.Version(1, 2, 3), automation.Version(1, 2, 4), {"tui", "streaming"}
                )

    def test_missing_mapped_cargo_feature_stops_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            old = self.cargo_source(base / "old", "1.2.3")
            new = self.cargo_source(base / "new", "1.2.4")
            with self.assertRaisesRegex(automation.AutomationError, "features missing upstream"):
                automation.check_source_contract(
                    old, new, automation.Version(1, 2, 3), automation.Version(1, 2, 4), {"tui", "required-future"}
                )

    def test_git_cargo_dependency_requires_packaging_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            old = self.cargo_source(base / "old", "1.2.3")
            new = self.cargo_source(base / "new", "1.2.4")
            lock = new / "Cargo.lock"
            lock.write_text(
                lock.read_text().replace(
                    "registry+https://github.com/rust-lang/crates.io-index",
                    "git+https://github.com/example/custom-crate?rev=abc123#abc123",
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(automation.AutomationError, "Git dependencies"):
                automation.check_source_contract(
                    old, new, automation.Version(1, 2, 3), automation.Version(1, 2, 4), {"tui"}
                )

    def test_discover_resolves_lightweight_and_annotated_release_tags(self) -> None:
        tag = "v1.2.4"
        ref = f"refs/tags/{tag}"
        release = {"id": 42, "tag_name": tag, "html_url": self.release("1.2.4")["url"],
                   "draft": False, "prerelease": False}
        self.ebuild("spotatui-1.2.3.ebuild", "1.2.3")
        commit = {"type": "commit", "sha": "a" * 40}
        annotation = {"type": "tag", "sha": "b" * 40}
        for reference, objects in ((commit, []), (annotation, [{"sha": "b" * 40, "object": commit}])):
            with self.subTest(kind=reference["type"]), patch.object(
                automation, "api_json", side_effect=[release, [], {"ref": ref, "object": reference}, *objects],
            ) as api:
                result = automation.discover(self.root, "owner/repo", "read-token")
                self.assertEqual(result["release"]["commit"], commit["sha"])
                self.assertEqual(result["release"]["release_id"], "42")
                self.assertTrue(api.call_args_list[2].args[0].endswith("/git/ref/tags/v1.2.4"))

    def test_invalid_tag_objects_are_rejected(self) -> None:
        tag = "v1.2.4"
        ref = f"refs/tags/{tag}"
        for responses in (
            [{"ref": "refs/tags/v1.2.5", "object": {"type": "commit", "sha": "a" * 40}}],
            [{"ref": ref, "object": {"type": "commit", "sha": "123"}}],
            [{"ref": ref, "object": {"type": "tree", "sha": "a" * 40}}],
            [{"ref": ref, "object": {"type": "tag", "sha": "b" * 40}},
             {"sha": "c" * 40, "object": {"type": "commit", "sha": "a" * 40}}],
            [{"ref": ref, "object": {"type": "tag", "sha": "b" * 40}},
             {"sha": "b" * 40, "object": {"type": "tag", "sha": "b" * 40}}],
        ):
            with self.subTest(responses=responses), patch.object(automation, "api_json", side_effect=responses):
                with self.assertRaises(automation.AutomationError):
                    automation.resolve_tag(tag)

    def source_tar(self, extra: tarfile.TarInfo | None = None) -> bytes:
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w", pax_headers={"comment": "a" * 40}) as archive:
            for name in ("Cargo.toml", "Cargo.lock"):
                data = b"test"
                member = tarfile.TarInfo(f"spotatui-1.2.4/{name}")
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
            if extra is not None:
                archive.addfile(extra)
        return raw.getvalue()

    def test_source_archive_checks_commit_and_entire_stream(self) -> None:
        version = automation.Version.parse("1.2.4")
        archive = self.root / "source.tar.gz"
        raw = self.source_tar()
        compressed = gzip.compress(raw, mtime=0)
        archive.write_bytes(compressed)
        source = automation.extract_source(archive, self.root / "valid", version, "a" * 40)
        self.assertEqual((source / "Cargo.lock").read_bytes(), b"test")
        for data, commit, message in (
            (compressed, "b" * 40, "commit"),
            (compressed[:-4], "a" * 40, "gzip/tar"),
            (gzip.compress(raw[:-1024] + b"X" * 1024), "a" * 40, "terminator or trailing data"),
        ):
            with self.subTest(message=message):
                archive.write_bytes(data)
                with self.assertRaisesRegex(automation.AutomationError, message):
                    automation.extract_source(archive, self.root / "invalid", version, commit)
                self.assertFalse((self.root / "invalid").exists())

    def test_source_archive_rejects_unexpected_paths_and_file_types(self) -> None:
        version = automation.Version.parse("1.2.4")
        archive = self.root / "source.tar.gz"
        link = tarfile.TarInfo("spotatui-1.2.4/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "Cargo.toml"
        for entry in (tarfile.TarInfo("spotatui-1.2.4/../outside"), link,
                      tarfile.TarInfo("spotatui-1.2.4/Cargo.lock")):
            with self.subTest(name=entry.name):
                archive.write_bytes(gzip.compress(self.source_tar(entry), mtime=0))
                with self.assertRaisesRegex(automation.AutomationError, "Unexpected path or type"):
                    automation.extract_source(archive, self.root / "invalid", version, "a" * 40)
                self.assertFalse((self.root / "invalid").exists())

    def test_baseline_archive_must_match_accepted_manifest_before_extraction(self) -> None:
        archive = self.root / "source.tar.gz"
        archive.write_bytes(gzip.compress(self.source_tar(), mtime=0))
        result = type("Download", (), {"stdout": automation.source_url("v1.2.4")})()
        with patch.object(automation, "run_checked", return_value=result):
            with self.assertRaisesRegex(automation.AutomationError, "accepted Manifest"):
                automation.download_source(
                    automation.Version.parse("1.2.4"), "a" * 40, archive, self.root / "extract",
                    accepted=self.distfile_evidence(b"different"),
                )
        self.assertFalse((self.root / "extract").exists())

    def test_dry_run_does_not_download_or_create_a_candidate(self) -> None:
        self.ebuild("spotatui-1.2.3.ebuild", "1.2.3")
        output = self.root / "candidate"
        with patch.object(automation, "download_source") as download, patch.object(automation, "run_checked") as command:
            plan = automation.prepare_candidate(self.root, self.release("1.2.4"), output, dry_run=True)
        self.assertEqual(plan["outcome"], "release-available")
        self.assertFalse(output.exists())
        download.assert_not_called()
        command.assert_not_called()

    @staticmethod
    def distfile_evidence(data: bytes) -> dict:
        return {"size": len(data), "BLAKE2B": hashlib.blake2b(data).hexdigest(), "SHA512": hashlib.sha512(data).hexdigest()}

    @staticmethod
    def manifest(entries: dict) -> str:
        return "".join(
            f"DIST {name} {entry['size']} BLAKE2B {entry['BLAKE2B']} SHA512 {entry['SHA512']}\n"
            for name, entry in sorted(entries.items())
        )

    def make_handoff(self) -> tuple[Path, Path, dict, dict]:
        old = self.ebuild("spotatui-1.2.3.ebuild", "1.2.3")
        release = self.release("1.2.4")
        base = self.distfile_evidence(b"packaged source")
        shared = self.distfile_evidence(b"verified shared crate")
        source = self.distfile_evidence(b"verified source")
        previous = {"spotatui-1.2.3.tar.gz": base, "serde-1.0.0.crate": shared}
        (self.package / "Manifest").write_text(self.manifest(previous), encoding="utf-8")
        report = {
            **automation.candidate_plan(self.root, release),
            "tag": release["tag"],
            "release_id": "42",
            "release_url": release["url"],
            "upstream_commit": "b" * 40,
            "source_url": automation.source_url(release["tag"]),
            "archive": source,
            "cargo_toml_sha256": "c" * 64,
            "cargo_lock_sha256": "d" * 64,
            "crate_added": [],
            "crate_removed": [],
            "pycargoebuild_version": automation.PYCARGOEBUILD_VERSION,
            "validation": dict.fromkeys(automation.PREPARATION_CHECKS, "passed"),
        }
        context = automation.handoff_context("a" * 40, "111", "2")
        candidate = self.root / "candidate"
        package = candidate / automation.PACKAGE_DIR
        package.mkdir(parents=True)
        (package / "Manifest").write_text(
            self.manifest({**previous, "spotatui-1.2.4.tar.gz": source}), encoding="utf-8",
        )
        (package / report["candidate"]).write_text(
            automation.replace_crates(old.read_text(), {"serde@1.0.0"}), encoding="utf-8",
        )
        names = automation.ALLOWED_HANDOFF | {str(automation.PACKAGE_DIR / report["candidate"])}
        report["files"] = {name: automation.file_sha256(candidate / name) for name in names}
        return candidate, self.root / "handoff.zip", context, report

    def test_publisher_rejects_unallowlisted_handoff_file(self) -> None:
        candidate, bundle, context, report = self.make_handoff()
        automation.write_handoff(candidate, bundle, report, context, "success")
        for name in ("README.md", ".github/workflows.yml"):
            with self.subTest(name=name):
                extra = bundle.with_name(Path(name).name + ".zip")
                extra.write_bytes(bundle.read_bytes())
                with zipfile.ZipFile(extra, "a") as archive:
                    archive.writestr(name, "untrusted\n")
                with self.assertRaisesRegex(automation.AutomationError, "allowlist mismatch"):
                    automation.validate_handoff(self.root, extra, context, "77", automation.file_sha256(extra))

    def test_publisher_applies_only_validated_candidate_files(self) -> None:
        candidate, bundle, context, report = self.make_handoff()
        automation.write_handoff(candidate, bundle, report, context, "success")
        result, files = automation.apply_handoff(self.root, bundle, context, "77", automation.file_sha256(bundle))
        expected = {str(automation.PACKAGE_DIR / name) for name in (report["candidate"], "Manifest")}
        with zipfile.ZipFile(bundle) as archive:
            self.assertEqual(set(archive.namelist()), expected | {automation.HANDOFF_METADATA})
        self.assertEqual(set(files), expected)
        self.assertEqual(result["candidate"], "spotatui-1.2.4.ebuild")
        for name, contents in files.items():
            self.assertEqual((self.root / name).read_bytes(), contents)
        self.assertTrue((self.package / result["fallback"]).is_file())

    def test_publisher_rejects_wrong_artifact_digest_id_or_context(self) -> None:
        candidate, bundle, context, report = self.make_handoff()
        automation.write_handoff(candidate, bundle, report, context, "success")
        digest = automation.file_sha256(bundle)
        for expected, artifact_id, expected_digest, error in (
            (context, "", digest, "artifact ID"),
            (context, "0", digest, "artifact ID"),
            (context, "77", "e" * 64, "trusted artifact digest"),
            ({**context, "base_commit": "b" * 40}, "77", digest, "base commit, run ID or attempt"),
            ({**context, "run_id": "112"}, "77", digest, "base commit, run ID or attempt"),
            ({**context, "run_attempt": "3"}, "77", digest, "base commit, run ID or attempt"),
        ):
            with self.subTest(expected=expected, error=error):
                with self.assertRaisesRegex(automation.AutomationError, error):
                    automation.apply_handoff(self.root, bundle, expected, artifact_id, expected_digest)
                self.assertFalse((self.package / "spotatui-1.2.4.ebuild").exists())

    def test_handoff_requires_every_validation_result_and_unchanged_files(self) -> None:
        candidate, bundle, context, report = self.make_handoff()
        for status, validation in (("skipped", report["validation"]), ("success", {}),
                                    ("success", {**report["validation"], "pkgcheck": "failure"})):
            with self.subTest(status=status, validation=validation):
                with self.assertRaisesRegex(automation.AutomationError, "mandatory checks"):
                    automation.write_handoff(candidate, bundle, {**report, "validation": validation}, context, status)
                self.assertFalse(bundle.exists())
        (candidate / automation.PACKAGE_DIR / "Manifest").write_text("changed after pkgcheck")
        with self.assertRaisesRegex(automation.AutomationError, "changed after validation"):
            automation.write_handoff(candidate, bundle, report, context, "success")
        self.assertFalse(bundle.exists())

    def test_publisher_rejects_contract_changes_even_with_matching_content_digests(self) -> None:
        candidate, bundle, context, report = self.make_handoff()
        name = str(automation.PACKAGE_DIR / report["candidate"])
        with (candidate / name).open("a", encoding="utf-8") as ebuild:
            ebuild.write('IUSE+=" unexpected"\n')
        report["files"][name] = automation.file_sha256(candidate / name)
        automation.write_handoff(candidate, bundle, report, context, "success")
        with self.assertRaisesRegex(automation.AutomationError, "outside CRATES"):
            automation.apply_handoff(self.root, bundle, context, "77", automation.file_sha256(bundle))
        self.assertFalse((self.package / report["candidate"]).exists())

    def test_publisher_rejects_tampered_files_and_unexpected_zip_member_types(self) -> None:
        candidate, bundle, context, report = self.make_handoff()
        automation.write_handoff(candidate, bundle, report, context, "success")
        with zipfile.ZipFile(bundle) as original:
            contents = [(member, original.read(member)) for member in original.infolist()]
        manifest = str(automation.PACKAGE_DIR / "Manifest")
        for name, data, mode, error in (
            (manifest, b"changed", stat.S_IFREG | 0o644, "validated digests"),
            (manifest, b"Manifest", stat.S_IFLNK | 0o644, "Unexpected file type"),
            (manifest, b"Manifest", stat.S_IFREG | 0o755, "Unexpected file type"),
            ("../Manifest", b"changed", stat.S_IFREG | 0o644, "allowlist mismatch"),
            (automation.HANDOFF_METADATA, None, stat.S_IFREG | 0o644, "mandatory checks"),
        ):
            with self.subTest(name=name, error=error):
                mutated = self.root / f"mutated-{error.replace(' ', '-')}.zip"
                original_name = automation.HANDOFF_METADATA if data is None else manifest
                with zipfile.ZipFile(mutated, "w") as archive:
                    for member, original_data in contents:
                        if member.filename != original_name:
                            archive.writestr(member, original_data)
                    replaced = zipfile.ZipInfo(name)
                    replaced.create_system = 3
                    replaced.external_attr = mode << 16
                    if data is None:
                        data = json.dumps({**report, **context, "validation": {}}).encode()
                    archive.writestr(replaced, data)
                with self.assertRaisesRegex(automation.AutomationError, error):
                    automation.validate_handoff(self.root, mutated, context, "77", automation.file_sha256(mutated))

    def test_manifest_verifies_retained_candidate_and_shared_distfiles(self) -> None:
        old_source = self.distfile_evidence(b"old source")
        new_source = self.distfile_evidence(b"new source")
        shared = self.distfile_evidence(b"shared crate")
        retained = {"spotatui-1.2.3.tar.gz", "shared.crate"}
        candidate = {"spotatui-1.2.4.tar.gz", "shared.crate"}
        accepted = {"spotatui-1.2.3.tar.gz": old_source, "shared.crate": shared,
                    "spotatui-1.2.2.tar.gz": old_source}
        entries = {"spotatui-1.2.3.tar.gz": old_source, "shared.crate": shared,
                   "spotatui-1.2.4.tar.gz": new_source}
        automation.check_candidate_manifest(entries, accepted, retained, candidate, "spotatui-1.2.4.tar.gz", new_source)
        for changed, error in (
            ({**entries, "spotatui-1.2.2.tar.gz": old_source}, "unexpected or missing"),
            ({**entries, "shared.crate": new_source}, "accepted fallback"),
            ({**entries, "spotatui-1.2.4.tar.gz": old_source}, "verified source archive"),
        ):
            with self.subTest(error=error), self.assertRaisesRegex(automation.AutomationError, error):
                automation.check_candidate_manifest(changed, accepted, retained, candidate, "spotatui-1.2.4.tar.gz", new_source)
        (self.root / "shared.crate").write_bytes(b"shared crate")
        automation.verify_candidate_distfiles(self.root, entries, {"shared.crate": shared})
        (self.root / "shared.crate").write_bytes(b"shared cratE")
        with self.assertRaisesRegex(automation.AutomationError, "changed during Manifest generation"):
            automation.verify_candidate_distfiles(self.root, entries, {"shared.crate": shared})

    def test_cargo_lock_registry_entries_feed_crates_update(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = self.cargo_source(Path(temporary) / "source", "1.2.3")
            self.assertEqual(automation.lock_crates(source), {"serde@1.0.0"})
            original = self.ebuild("spotatui-1.2.3.ebuild", "1.2.3").read_text()
            updated = automation.replace_crates(original, automation.lock_crates(source))
            self.assertEqual(automation.crates_from_ebuild(updated), {"serde@1.0.0"})


if __name__ == "__main__":
    unittest.main()
