from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import release_automation as automation


class ReleaseAutomationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.package = self.root / automation.PACKAGE_DIR
        self.package.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def ebuild(self, name: str, version: str) -> Path:
        path = self.package / name
        path.write_text(
            f'EAPI=8\nCRATES="\n\tserde@1.0.0\n"\nIUSE="mpris"\n'
            f'RDEPEND="mpris? ( sys-apps/dbus )"\n'
            f'src_configure() {{\n local -a myfeatures=( tui streaming )\n'
            f' use mpris && myfeatures+=( mpris )\n}}\n# {version}\n',
            encoding="utf-8",
        )
        return path

    def release(self, version: str) -> dict[str, str]:
        return {
            "tag": f"v{version}",
            "version": version,
            "url": f"https://github.com/{automation.UPSTREAM}/releases/tag/v{version}",
        }

    def cargo_source(
        self, path: Path, version: str, *, features: str | None = None, native: bool = False
    ) -> Path:
        path.mkdir(parents=True)
        path.joinpath("Cargo.toml").write_text(
            f'[package]\nname="spotatui"\nversion="{version}"\nrust-version="1.91"\n'
            f'[features]\ntui=[]\nstreaming=[]\ncover-art=[]\ninternet-radio=[]\n'
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

    def test_changed_upstream_features_stop_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            old = self.cargo_source(base / "old", "1.2.3")
            new = self.cargo_source(base / "new", "1.2.4", features="new-source=[]\n")
            with self.assertRaisesRegex(automation.AutomationError, "Cargo features changed"):
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

    def make_handoff(self) -> tuple[Path, Path, str]:
        self.ebuild("spotatui-1.2.3.ebuild", "1.2.3")
        release = self.release("1.2.4")
        report = {
            **automation.candidate_plan(self.root, release),
            "tag": release["tag"],
            "release_url": release["url"],
            "base_commit": "a" * 40,
        }
        handoff = self.root / "handoff"
        package = handoff / automation.PACKAGE_DIR
        package.mkdir(parents=True)
        (handoff / "README.md").write_text("candidate readme\n", encoding="utf-8")
        (package / "Manifest").write_text("candidate manifest\n", encoding="utf-8")
        candidate = package / str(report["candidate"])
        candidate.write_text("candidate ebuild\n", encoding="utf-8")
        (handoff / "release-handoff.json").write_text(json.dumps(report), encoding="utf-8")
        return self.root, handoff, "a" * 40

    def test_publisher_rejects_unallowlisted_handoff_file(self) -> None:
        root, handoff, base = self.make_handoff()
        (handoff / ".github").mkdir()
        (handoff / ".github/workflows.yml").write_text("untrusted\n", encoding="utf-8")
        with self.assertRaisesRegex(automation.AutomationError, "allowlist mismatch"):
            automation.validate_handoff(root, handoff, base)

    def test_publisher_applies_only_validated_candidate_files(self) -> None:
        root, handoff, base = self.make_handoff()
        report = automation.apply_handoff(root, handoff, base)
        self.assertTrue((self.package / "spotatui-1.2.4.ebuild").is_file())
        self.assertEqual((root / "README.md").read_text(), "candidate readme\n")
        self.assertEqual(report["branch"], "automation/spotatui-v1.2.4")
        self.assertIn("Manual gates", report["body"])

    def test_cargo_lock_registry_entries_feed_crates_update(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = self.cargo_source(Path(temporary) / "source", "1.2.3")
            self.assertEqual(automation.lock_crates(source), {"serde@1.0.0"})
            original = self.ebuild("spotatui-1.2.3.ebuild", "1.2.3").read_text()
            updated = automation.replace_crates(original, automation.lock_crates(source))
            self.assertEqual(automation.crates_from_ebuild(updated), {"serde@1.0.0"})


if __name__ == "__main__":
    unittest.main()
