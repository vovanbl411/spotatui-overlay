from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from scripts import publish_release as publish
from scripts import release_automation as preparation


CHECKOUT_REMOTE_URL = publish.REMOTE_URL
VERSION = "0.48.1"
TAG = f"v{VERSION}"
BRANCH = f"automation/spotatui-{TAG}"
REF = f"refs/heads/{BRANCH}"
CANDIDATE = preparation.PACKAGE_DIR / f"spotatui-{VERSION}.ebuild"
MANIFEST = preparation.PACKAGE_DIR / "Manifest"


class PublishReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = temporary / "checkout"
        self.remote = temporary / "remote.git"
        self.download = temporary / "download"
        self.summary = temporary / "summary"
        self.download.mkdir()
        self.enterContext(patch.dict(os.environ, {
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_AUTHOR_DATE": "2026-11-27T10:00:00Z", "GIT_COMMITTER_DATE": "2026-11-27T10:00:00Z",
        }))
        self.git("init", "-q", "-b", "main", str(self.root), cwd=temporary)
        self.git("init", "-q", "--bare", str(self.remote), cwd=temporary)
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "core.filemode", "true")
        self.git("remote", "add", "origin", str(self.remote))
        actual_package = Path(__file__).resolve().parents[1] / preparation.PACKAGE_DIR
        shutil.copytree(actual_package, self.root / preparation.PACKAGE_DIR)
        (self.root / "README.md").write_text("Fixture documentation.\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("push", "-q", "origin", "HEAD:refs/heads/main")
        self.git("switch", "-q", "--detach", self.base)

        bundle = self.make_handoff(temporary)
        self.env = {
            **os.environ,
            "GITHUB_ACTIONS": "true",
            "GITHUB_REPOSITORY": publish.REPOSITORY,
            "GITHUB_REF": publish.MAIN_REF,
            "GITHUB_WORKFLOW_REF": publish.WORKFLOW_REF,
            "GITHUB_EVENT_NAME": "schedule",
            "GITHUB_SHA": self.base,
            "GITHUB_WORKFLOW_SHA": self.base,
            "GITHUB_RUN_ID": "111",
            "GITHUB_RUN_ATTEMPT": "2",
            "DRY_RUN": "false",
            "PREPARE_RESULT": "success",
            "PUBLISH_READY": "true",
            "ARTIFACT_ID": "77",
            "ARTIFACT_DIGEST": f"sha256:{preparation.file_sha256(bundle)}",
            "GH_TOKEN": "fixture-token",
        }
        self.calls = []
        self.posts = []
        self.exact_pulls = []
        self.other_pulls = {}
        self.on_post = self.draft
        self.on_push = None
        self.before_push = None
        self.after_push = None
        self.enterContext(patch.object(publish, "REMOTE_URL", str(self.remote)))
        self.enterContext(patch.object(publish, "github_request", side_effect=self.api))
        self.enterContext(patch.object(publish, "git", side_effect=self.git_spy))

    def git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(["git", *args], cwd=cwd or self.root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def make_handoff(self, temporary: Path) -> Path:
        release = {
            "tag": TAG, "version": VERSION,
            "url": f"https://github.com/{preparation.UPSTREAM}/releases/tag/{TAG}",
        }
        plan = preparation.candidate_plan(self.root, release)
        current = preparation.packaged_versions(self.root)[-1]
        entries = preparation.manifest_distfiles((self.root / MANIFEST).read_text())
        source = temporary / "source.tar.gz"
        source.write_bytes(b"source fixture")
        evidence = preparation.distfile_integrity(source)
        retained = {name: entries[name] for name in preparation.version_distfiles(current)}
        retained[preparation.source_distfile(VERSION)] = evidence

        candidate = temporary / "candidate"
        (candidate / preparation.PACKAGE_DIR).mkdir(parents=True)
        (candidate / CANDIDATE).write_bytes(current.path.read_bytes())
        (candidate / MANIFEST).write_text("".join(
            f"DIST {name} {item['size']} BLAKE2B {item['BLAKE2B']} SHA512 {item['SHA512']}\n"
            for name, item in sorted(retained.items())
        ))
        report = {
            **plan,
            "tag": TAG,
            "release_id": "42",
            "release_url": release["url"],
            "upstream_commit": "a" * 40,
            "source_url": preparation.source_url(TAG),
            "archive": evidence,
            "cargo_toml_sha256": "b" * 64,
            "cargo_lock_sha256": "c" * 64,
            "crate_added": [],
            "crate_removed": [],
            "pycargoebuild_version": preparation.PYCARGOEBUILD_VERSION,
            "validation": dict.fromkeys(preparation.PREPARATION_CHECKS, "passed"),
            "files": {str(path): preparation.file_sha256(candidate / path) for path in (MANIFEST, CANDIDATE)},
        }
        bundle = self.download / "handoff.zip"
        preparation.write_handoff(candidate, bundle, report, preparation.handoff_context(self.base, "111", "2"), "success")
        return bundle

    def git_spy(self, root, *args, **kwargs):
        self.calls.append(args)
        if args[0] != "push":
            return REAL_GIT(root, *args, **kwargs)
        if self.before_push:
            self.before_push()
        result = self.on_push(root, *args, **kwargs) if self.on_push else REAL_GIT(root, *args, **kwargs)
        if self.after_push:
            self.after_push()
        return result

    def draft(self, request: dict) -> dict:
        return {
            "number": 42,
            "html_url": f"https://github.com/{publish.REPOSITORY}/pull/42",
            "state": "open",
            "draft": True,
            "title": request["title"],
            "body": request["body"],
            "head": {"ref": BRANCH, "sha": self.git("rev-parse", "HEAD"),
                     "repo": {"full_name": publish.REPOSITORY}},
            "base": {"ref": "main", "sha": self.base, "repo": {"full_name": publish.REPOSITORY}},
        }

    def api(self, token: str, path: str, *, request: dict | None = None):
        self.assertEqual(token, self.env["GH_TOKEN"])
        url = urlsplit(path)
        query = parse_qs(url.query)
        self.calls.append(("POST" if request is not None else "GET", url.path, query))
        if request is not None:
            self.assertEqual(path, publish.PULLS)
            self.posts.append(request)
            return self.on_post(request)
        if url.path.endswith("/files"):
            return self.other_pulls[int(url.path.split("/")[-2])]
        if "head" in query:
            self.assertEqual(query["head"], [f"vovanbl411:{BRANCH}"])
            self.assertEqual(query["state"], ["all"])
            return self.exact_pulls
        self.assertEqual(query["state"], ["open"])
        self.assertEqual(query["base"], ["main"])
        return [{"number": number} for number in self.other_pulls]

    def run_publish(self, expected: int = 1, **overrides) -> str:
        with redirect_stdout(io.StringIO()) as output:
            result = publish.publish(self.root, self.download, {**self.env, **overrides}, self.summary)
        self.assertEqual(result, expected, output.getvalue())
        self.assertTrue(self.summary.read_text().endswith("\n"))
        for line in output.getvalue().splitlines():
            self.assertIn(line, self.summary.read_text())
        return output.getvalue()

    def ref(self, ref: str = REF) -> str:
        return self.git("for-each-ref", "--format=%(objectname)", ref, cwd=self.remote)

    def push_calls(self) -> list:
        return [call for call in self.calls if call[0] == "push"]

    def assert_no_writes(self, branch: str = "") -> None:
        self.assertEqual(self.push_calls(), [])
        self.assertEqual(self.posts, [])
        self.assertEqual(self.ref(), branch)

    def test_preflight_rejects_invalid_context_and_trusted_evidence(self) -> None:
        for key, value in {
            "GITHUB_ACTIONS": "", "GITHUB_REPOSITORY": "fork/spotatui-overlay",
            "GITHUB_REF": "refs/heads/fix/change-ci", "GITHUB_WORKFLOW_REF": "another-workflow",
            "GITHUB_EVENT_NAME": "pull_request_target", "GITHUB_SHA": "f" * 40,
            "GITHUB_WORKFLOW_SHA": "wrong", "GITHUB_RUN_ID": "",
            "GITHUB_RUN_ATTEMPT": "0", "DRY_RUN": "true", "PREPARE_RESULT": "skipped",
            "PUBLISH_READY": "", "ARTIFACT_ID": "0", "ARTIFACT_DIGEST": "wrong", "GH_TOKEN": "",
        }.items():
            with self.subTest(key=key):
                self.assertIn("no remote write occurred", self.run_publish(**{key: value}))
                self.assertFalse((self.root / CANDIDATE).exists())
        self.assert_no_writes()
        self.assertFalse(any(call[0] == "GET" for call in self.calls))

    def test_preflight_accepts_pinned_checkout_https_origin(self) -> None:
        origin = "https://github.com/vovanbl411/spotatui-overlay"
        self.git("remote", "set-url", "origin", origin)
        with patch.object(publish, "REMOTE_URL", CHECKOUT_REMOTE_URL):
            self.assertEqual(publish.preflight(self.root, self.env)["base_commit"], self.base)

    def test_preflight_rejects_checkout_and_remote_mismatches(self) -> None:
        self.git("remote", "set-url", "--push", "origin", str(self.remote) + "-wrong")
        self.assertIn("Origin fetch and push", self.run_publish())
        self.git("remote", "set-url", "--push", "origin", str(self.remote))
        self.assertIn("trusted workflow revision", self.run_publish(GITHUB_WORKFLOW_SHA="e" * 40, GITHUB_SHA="e" * 40))
        (self.root / "README.md").write_text("dirty")
        self.assertIn("checkout is not clean", self.run_publish())
        self.assert_no_writes()

    def test_rejects_wrong_artifact_digest_and_mismatched_run(self) -> None:
        self.assertIn("trusted artifact digest", self.run_publish(ARTIFACT_DIGEST="e" * 64))
        self.assertIn("run ID or attempt", self.run_publish(GITHUB_RUN_ATTEMPT="3"))
        with (self.download / "handoff.zip").open("ab") as archive:
            archive.write(b"tampered")
        self.assertIn("trusted artifact digest", self.run_publish())
        self.assertFalse((self.root / CANDIDATE).exists())
        self.assert_no_writes()

    def change_applied_candidate(self, change) -> None:
        apply = preparation.apply_handoff

        def apply_then_change(*args):
            result = apply(*args)
            change()
            return result

        self.enterContext(patch.object(preparation, "apply_handoff", side_effect=apply_then_change))

    def test_rejects_unexpected_worktree_paths(self) -> None:
        self.change_applied_candidate(lambda: (self.root / "README.md").write_text("unexpected\n"))
        self.assertIn("working-tree diff", self.run_publish())
        self.assert_no_writes()

    def test_rejects_unexpected_modes_in_staged_tree(self) -> None:
        self.change_applied_candidate(lambda: (self.root / CANDIDATE).chmod(0o755))
        self.assertIn("Staged tree has unexpected paths, file modes or change types", self.run_publish())
        self.assert_no_writes()

    def test_rejects_staged_content_differing_from_verified_bytes(self) -> None:
        self.change_applied_candidate(lambda: (self.root / CANDIDATE).write_bytes(b"unexpected\n"))
        self.assertIn("Staged content differs", self.run_publish())
        self.assert_no_writes()

    def test_rejects_extra_staged_paths(self) -> None:
        def add_extra_file(root, *args, **kwargs):
            if args[0] == "write-tree":
                (self.root / "README.md").write_text("unexpected staged edit\n")
                self.git("add", "README.md")
            return self.git_spy(root, *args, **kwargs)

        with patch.object(publish, "git", side_effect=add_extra_file):
            self.assertIn("Staged tree has unexpected paths", self.run_publish())
        self.assert_no_writes()

    def test_rejects_empty_candidate_application(self) -> None:
        with patch.object(preparation, "apply_handoff", side_effect=preparation.validate_handoff):
            self.assertIn("empty working-tree diff", self.run_publish())
        self.assert_no_writes()

    def replace_commit(self, *, wrong_parent: bool) -> None:
        def git_with_replacement(root, *args, **kwargs):
            result = self.git_spy(root, *args, **kwargs)
            if "commit" in args:
                tree = self.git("rev-parse", "HEAD^{tree}" if wrong_parent else f"{self.base}^{{tree}}")
                parent = "HEAD" if wrong_parent else self.base
                replacement = self.git("commit-tree", tree, "-p", parent, "-m", "wrong commit")
                self.git("update-ref", "HEAD", replacement)
            return result

        self.enterContext(patch.object(publish, "git", side_effect=git_with_replacement))

    def test_rejects_commit_with_wrong_parent(self) -> None:
        self.replace_commit(wrong_parent=True)
        self.assertIn("Local commit parent or tree differs", self.run_publish())
        self.assert_no_writes()

    def test_rejects_commit_with_wrong_tree(self) -> None:
        self.replace_commit(wrong_parent=False)
        self.assertIn("Local commit parent or tree differs", self.run_publish())
        self.assert_no_writes()

    def set_remote_ref(self, ref: str, sha: str) -> None:
        self.git("update-ref", ref, sha, cwd=self.remote)

    def test_existing_branch_is_never_reused(self) -> None:
        self.set_remote_ref(REF, self.base)
        self.assertIn("already exists", self.run_publish())
        self.assert_no_writes(self.base)

    def conflict_blocks(self, state: str) -> None:
        self.exact_pulls = [{"number": 8, "state": state}]
        self.assertIn("/pull/8", self.run_publish())
        self.assert_no_writes()

    def test_closed_exact_head_pr_blocks(self) -> None:
        self.conflict_blocks("closed")

    def test_open_exact_head_pr_blocks(self) -> None:
        self.conflict_blocks("open")

    def test_open_pr_with_same_release_on_another_head_blocks(self) -> None:
        ebuild = CANDIDATE.with_name(f"spotatui-{VERSION}-r1.ebuild")
        self.other_pulls = {8: [{"filename": str(ebuild), "status": "added"}]}
        self.assertIn("already proposes spotatui", self.run_publish())
        self.assert_no_writes()

    def test_remote_lookup_failure_does_not_count_as_absence(self) -> None:
        def git_with_failure(root, *args, **kwargs):
            if args[0] == "ls-remote" and args[-1] == REF:
                return subprocess.CompletedProcess(args, 128, b"", b"authentication failed")
            return self.git_spy(root, *args, **kwargs)

        with patch.object(publish, "git", side_effect=git_with_failure):
            self.assertIn("Remote lookup for", self.run_publish())
        self.assert_no_writes()

    def test_remote_main_already_stale_blocks_candidate_admission(self) -> None:
        self.git("update-ref", "-d", publish.MAIN_REF, cwd=self.remote)
        self.assertIn("refs/heads/main changed", self.run_publish())
        self.assertFalse((self.root / CANDIDATE).exists())
        self.assert_no_writes()

    def test_stale_main_before_push_stops_all_writes(self) -> None:
        original = self.api

        def move_main(token, path, **kwargs):
            result = original(token, path, **kwargs)
            if "state=open" in path:
                self.git("update-ref", "-d", publish.MAIN_REF, cwd=self.remote)
            return result

        with patch.object(publish, "github_request", side_effect=move_main):
            self.assertIn("refs/heads/main changed", self.run_publish())
        self.assertTrue((self.root / CANDIDATE).exists())
        self.assert_no_writes()

    def test_expected_absent_lease_rejects_branch_race_without_overwriting(self) -> None:
        self.before_push = lambda: self.set_remote_ref(REF, self.base)
        output = self.run_publish()
        self.assertIn("branch creation outcome unknown", output)
        self.assertIn(f"at `{self.base}`", output)
        self.assertIn("Branch ownership is unconfirmed", output)
        self.assertIn("Owner intervention", output)
        self.assertEqual(self.ref(), self.base)
        self.assertEqual(len(self.push_calls()), 1)
        self.assertIn(f"--force-with-lease={REF}:", self.push_calls()[0])
        self.assertEqual(self.posts, [])

    def test_other_successful_push_statuses_are_rejected(self) -> None:
        for flag in (b" ", b"+"):
            with self.subTest(flag=flag):
                commit = "d" * 40
                row = b"\t".join((flag, f"{commit}:{REF}".encode(), b"successful update"))
                result = subprocess.CompletedProcess([], 0, row + b"\n", b"")
                with patch.object(publish, "git", return_value=result):
                    with self.assertRaisesRegex(preparation.AutomationError, "did not confirm creation"):
                        publish.push_new_branch(self.root, commit, REF, self.env)

    def test_already_up_to_date_porcelain_does_not_confirm_creation(self) -> None:
        def race_with_same_sha(root, *args, **kwargs):
            self.git("push", "-q", "origin", f"HEAD:{REF}")
            return REAL_GIT(root, *args, **kwargs)

        self.on_push = race_with_same_sha
        self.assertIn("Branch ownership is unconfirmed", self.run_publish())
        self.assertEqual(self.ref(), self.git("rev-parse", "HEAD"))
        self.assertEqual(len(self.push_calls()), 1)
        self.assertEqual(self.posts, [])

    def test_read_back_after_ambiguous_push_never_assumes_ownership(self) -> None:
        def lose_response(root, *args, **kwargs):
            result = REAL_GIT(root, *args, **kwargs)
            self.assertEqual(result.returncode, 0)
            return subprocess.CompletedProcess(args, 0, b"Done\n", b"")

        self.on_push = lose_response
        output = self.run_publish()
        commit = self.git("rev-parse", "HEAD")
        self.assertIn(f"Read-only lookup observed `{REF}` at `{commit}`", output)
        self.assertIn("Branch ownership is unconfirmed", output)
        self.assertEqual(self.ref(), commit)
        self.assertEqual(len(self.push_calls()), 1)
        self.assertEqual(self.posts, [])

    def assert_stopped_after_branch(self, reason: str) -> None:
        output = self.run_publish()
        self.assertIn("branch creation confirmed", output)
        self.assertIn("Owner intervention", output)
        self.assertIn(reason, output)
        self.assertEqual(len(self.push_calls()), 1)
        self.assertEqual(self.posts, [])

    def test_base_change_before_post_leaves_candidate_branch_for_owner(self) -> None:
        self.after_push = lambda: self.set_remote_ref(publish.MAIN_REF, self.ref())
        self.assert_stopped_after_branch("refs/heads/main changed")
        self.assertEqual(self.ref(), self.git("rev-parse", "HEAD"))

    def test_candidate_ref_change_before_post_is_not_overwritten(self) -> None:
        self.after_push = lambda: self.set_remote_ref(REF, self.base)
        self.assert_stopped_after_branch(f"{REF} changed")
        self.assertEqual(self.ref(), self.base)

    def test_pr_appearing_after_push_blocks_post(self) -> None:
        self.after_push = lambda: self.exact_pulls.append({"number": 123, "state": "open"})
        self.assert_stopped_after_branch("/pull/123")
        self.assertEqual(self.ref(), self.git("rev-parse", "HEAD"))

    def test_definite_post_failure_leaves_branch_for_owner(self) -> None:
        def reject(_request):
            raise publish.PullRequestRejected("GitHub POST returned HTTP 422.")

        self.on_post = reject
        output = self.run_publish()
        self.assertIn("Draft PR creation failed", output)
        self.assertIn("Read-only lookup found no PR", output)
        self.assertIn("Owner intervention", output)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(len(self.push_calls()), 1)
        self.assertEqual(self.ref(), self.git("rev-parse", "HEAD"))

    def test_ambiguous_post_reads_back_expected_draft_without_retry(self) -> None:
        def lose_response(request):
            self.exact_pulls.append(self.draft(request))
            raise preparation.AutomationError("GitHub POST did not return a usable response.")

        self.on_post = lose_response
        output = self.run_publish()
        self.assertIn("Draft PR creation outcome unknown", output)
        self.assertIn(f"found the expected Draft PR https://github.com/{publish.REPOSITORY}/pull/42", output)
        self.assertIn("Owner intervention", output)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(len(self.push_calls()), 1)

    def test_post_response_identity_must_match(self) -> None:
        self.on_post = lambda request: {**self.draft(request), "draft": False}
        output = self.run_publish()
        self.assertIn("Draft PR creation outcome unknown", output)
        self.assertIn("did not confirm the expected Draft PR identity", output)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(len(self.push_calls()), 1)

    def test_success_confirms_branch_and_draft_after_all_checks(self) -> None:
        similar = preparation.PACKAGE_DIR / "spotatui-0.48.10.ebuild"
        self.other_pulls = {17: [{"filename": str(similar), "status": "added"}]}
        output = self.run_publish(expected=0, GITHUB_EVENT_NAME="workflow_dispatch")
        self.assertIn("Draft PR creation confirmed", output)
        commit = self.git("rev-parse", "HEAD")
        self.assertEqual(self.ref(), commit)
        self.assertEqual(self.ref(publish.MAIN_REF), self.base)
        self.assertEqual(self.git("rev-parse", "HEAD^"), self.base)
        self.assertEqual(set(self.git("diff", "--name-status", "--no-renames", self.base, commit).splitlines()), {
            f"M\t{MANIFEST}", f"A\t{CANDIDATE}", "D\tmedia-sound/spotatui/spotatui-0.47.0-r1.ebuild",
        })
        push, = self.push_calls()
        self.assertEqual(push[-3:], (f"--force-with-lease={REF}:", "origin", f"{commit}:{REF}"))
        push_index = self.calls.index(push)
        self.assertEqual(self.calls[push_index - 1][-1], publish.MAIN_REF)
        self.assertEqual([call[-1] for call in self.calls[-3:-1]], [publish.MAIN_REF, REF])
        request, = self.posts
        self.assertIs(request["draft"], True)
        self.assertEqual(request["head"], BRANCH)
        self.assertEqual(request["base"], "main")
        for evidence in (commit, self.base, "a" * 40, "attempt 2, artifact 77", "actions/runs/111",
                         self.env["ARTIFACT_DIGEST"].removeprefix("sha256:"), "Manual gates",
                         "spotatui-0.48.0.ebuild", "spotatui-0.47.0-r1.ebuild",
                         "Added registry crates: 0", "Removed registry crates: 0",
                         "Publish handoff digest, contents and rotation: passed",
                         "Publish staged paths, modes, tree and commit parent: passed",
                         *(f"{check}: passed" for check in preparation.mandatory_results())):
            self.assertIn(evidence, request["body"])


REAL_GIT = publish.git


class GitHubRequestTests(unittest.TestCase):
    def test_askpass_passes_token_through_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env = publish.git_auth({**os.environ, "GH_TOKEN": "fixture-token"}, Path(temporary))
            askpass = Path(env["GIT_ASKPASS"])
            self.assertNotIn("fixture-token", askpass.read_text())
            subprocess.run(["bash", "-n", str(askpass)], check=True)
            for prompt, response in (("Username:", "x-access-token"), ("Password:", "fixture-token")):
                result = subprocess.run([str(askpass), prompt], env=env, capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout.strip(), response)

    def test_post_transport_checks_status_without_replaying(self) -> None:
        for status, exception in ((302, preparation.AutomationError), (422, publish.PullRequestRejected),
                                  (503, preparation.AutomationError), (201, preparation.AutomationError)):
            with self.subTest(status=status), patch.object(publish.http.client, "HTTPSConnection") as https:
                connection = https.return_value
                response = io.BytesIO(b"invalid json")
                response.status = status
                connection.getresponse.return_value = response
                with self.assertRaises(exception) as error:
                    publish.github_request("fixture-token", publish.PULLS, request={"draft": True})
                self.assertIs(type(error.exception), exception)
                self.assertEqual(connection.request.call_count, 1)
                self.assertEqual(connection.request.call_args.args, ("POST", publish.PULLS))
                self.assertEqual(json.loads(connection.request.call_args.kwargs["body"]), {"draft": True})
                connection.close.assert_called_once()
