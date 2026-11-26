#!/usr/bin/env python3
"""Publish an independently verified spotatui candidate as a Draft PR."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlencode

if __package__:
    from . import release_automation as preparation
else:
    import release_automation as preparation


REPOSITORY = "vovanbl411/spotatui-overlay"
# actions/checkout sets origin to the HTTPS URL without a .git suffix.
REMOTE_URL = f"https://github.com/{REPOSITORY}"
MAIN_REF = "refs/heads/main"
WORKFLOW_REF = f"{REPOSITORY}/.github/workflows/prepare-release.yml@{MAIN_REF}"
PULLS = f"/repos/{REPOSITORY}/pulls"


def git(root: Path, *args: str, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=",
             "-c", "http.extraHeader=", "-c", "http.followRedirects=false", *args], cwd=root,
            capture_output=True, env=env, timeout=45,
        )
    except subprocess.TimeoutExpired as error:
        raise preparation.AutomationError(f"git {args[0]} timed out.") from error
    if check and result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        raise preparation.AutomationError(f"git {args[0]} failed (exit {result.returncode}): {detail}")
    return result


def worktree_status(root: Path) -> set[bytes]:
    raw = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all",
              "--no-renames", "--ignore-submodules=none").stdout
    return set(raw.split(b"\0")) - {b""}


def preflight(root: Path, env: dict) -> dict[str, str]:
    for key, expected in {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_REF": MAIN_REF,
        "GITHUB_WORKFLOW_REF": WORKFLOW_REF,
        "DRY_RUN": "false",
        "PREPARE_RESULT": "success",
        "PUBLISH_READY": "true",
    }.items():
        if env.get(key) != expected:
            raise preparation.AutomationError(f"Publication preflight rejected {key}.")
    if env.get("GITHUB_EVENT_NAME") not in {"schedule", "workflow_dispatch"}:
        raise preparation.AutomationError("Publication preflight rejected GITHUB_EVENT_NAME.")
    base = env.get("GITHUB_WORKFLOW_SHA", "")
    context = preparation.handoff_context(base, env.get("GITHUB_RUN_ID", ""), env.get("GITHUB_RUN_ATTEMPT", ""))
    if env.get("GITHUB_SHA") != base or git(root, "rev-parse", "HEAD").stdout.strip() != base.encode():
        raise preparation.AutomationError("Publish checkout does not match the trusted workflow revision.")
    if worktree_status(root):
        raise preparation.AutomationError("Publish checkout is not clean.")
    for flags in ((), ("--push",)):
        urls = git(root, "remote", "get-url", "--all", *flags, "origin").stdout.splitlines()
        if urls != [REMOTE_URL.encode()]:
            raise preparation.AutomationError("Origin fetch and push must both target the expected repository.")
    if not re.fullmatch(r"[1-9][0-9]*", env.get("ARTIFACT_ID", "")):
        raise preparation.AutomationError("Prepare did not provide a valid artifact ID.")
    if not preparation.SHA256.fullmatch(env.get("ARTIFACT_DIGEST", "").removeprefix("sha256:")):
        raise preparation.AutomationError("Prepare did not provide a valid artifact SHA-256.")
    if not env.get("GH_TOKEN"):
        raise preparation.AutomationError("Publication requires GH_TOKEN.")
    return context


def commit_candidate(root: Path, report: dict, files: dict[str, bytes]) -> str:
    base = report["base_commit"]
    candidate = str(preparation.PACKAGE_DIR / report["candidate"])
    manifest = str(preparation.PACKAGE_DIR / "Manifest")
    paths = {manifest: "M", candidate: "A"}
    expected = {f" M {manifest}".encode(), f"?? {candidate}".encode()}
    if report["removed"]:
        removed = str(preparation.PACKAGE_DIR / report["removed"])
        paths[removed] = "D"
        expected.add(f" D {removed}".encode())
    if worktree_status(root) != expected:
        raise preparation.AutomationError("Applied handoff has an unexpected or empty working-tree diff.")

    git(root, "add", "-A", "--", *paths)
    git(root, "diff", "--cached", "--check")
    tree = git(root, "write-tree").stdout.decode().strip()

    # Check the immutable tree that the commit must use, including modes and deletions.
    raw = git(root, "diff-tree", "--no-commit-id", "-r", "--raw", "--no-renames",
              "-z", base, tree).stdout.split(b"\0")
    actual = {}
    for header, path in zip(raw[0:-1:2], raw[1:-1:2], strict=True):
        old_mode, new_mode, _, _, status = header.decode().removeprefix(":").split()
        actual[path.decode()] = (old_mode, new_mode, status)
    expected_changes = {
        path: ("000000" if status == "A" else "100644",
               "000000" if status == "D" else "100644", status)
        for path, status in paths.items()
    }
    if actual != expected_changes:
        raise preparation.AutomationError("Staged tree has unexpected paths, file modes or change types.")
    for path, contents in files.items():
        if git(root, "cat-file", "blob", f"{tree}:{path}").stdout != contents:
            raise preparation.AutomationError(f"Staged content differs from the verified handoff: {path}.")

    git(root, "-c", "user.name=github-actions[bot]",
        "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
        "commit", "--no-gpg-sign", "-m", f"update: spotatui {report['tag']}")
    commit, *parents = git(root, "rev-list", "--parents", "-1", "HEAD").stdout.decode().split()
    if parents != [base] or git(root, "rev-parse", f"{commit}^{{tree}}").stdout.decode().strip() != tree:
        raise preparation.AutomationError("Local commit parent or tree differs from the validated base or staged tree.")
    return commit


def remote_ref(root: Path, ref: str, env: dict) -> str | None:
    result = git(root, "ls-remote", "--exit-code", "--refs", "origin", ref, check=False, env=env)
    if result.returncode == 2:
        return None
    if result.returncode != 0:
        raise preparation.AutomationError(f"Remote lookup for {ref} failed (Git exit {result.returncode}).")
    match = re.fullmatch(rb"([0-9a-f]{40})\t" + re.escape(ref.encode()) + rb"\n", result.stdout)
    if match is None:
        raise preparation.AutomationError(f"Remote lookup returned an unexpected result for {ref}.")
    return match[1].decode()


def require_ref(root: Path, ref: str, expected: str, env: dict) -> None:
    observed = remote_ref(root, ref, env)
    if observed != expected:
        raise preparation.AutomationError(f"Remote {ref} changed: expected {expected}, observed {observed or 'absent'}.")


def push_new_branch(root: Path, commit: str, ref: str, env: dict) -> None:
    result = git(
        root, "push", "--porcelain", "--no-follow-tags", "--recurse-submodules=no",
        f"--force-with-lease={ref}:", "origin", f"{commit}:{ref}", check=False, env=env,
    )
    updates = [line.split(b"\t") for line in result.stdout.splitlines() if b"\t" in line]
    expected = [[b"*", f"{commit}:{ref}".encode(), b"[new branch]"]]
    if result.returncode != 0 or updates != expected:
        detail = {b"=": "ref already at candidate SHA", b"!": "update rejected",
                  b" ": "fast-forward reported", b"+": "forced update reported"}
        reason = detail.get(updates[0][0], "unexpected porcelain result") if len(updates) == 1 else "no unique ref update"
        raise preparation.AutomationError(f"Git push did not confirm creation of {ref} (exit {result.returncode}, {reason}).")


class PullRequestRejected(preparation.AutomationError):
    """GitHub returned a definite rejection of the POST."""


def github_request(token: str, path: str, *, request: dict | None = None) -> object:
    method = "GET" if request is None else "POST"
    expected_status = 200 if request is None else 201
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "spotatui-overlay-release-automation",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body = None
    if request is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(request)
    # A fixed HTTPS host and a single request keep credentials and POSTs off redirects.
    connection = http.client.HTTPSConnection("api.github.com", timeout=25)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        if response.status != expected_status:
            message = f"GitHub {method} {path} returned HTTP {response.status}."
            if request is not None and response.status in {400, 401, 403, 404, 405, 410, 422, 429}:
                raise PullRequestRejected(message)
            raise preparation.AutomationError(message)
        return json.load(response)
    except (OSError, http.client.HTTPException, ValueError) as error:
        raise preparation.AutomationError(f"GitHub {method} {path} did not return a usable response ({type(error).__name__}).") from error
    finally:
        connection.close()


def github_list(token: str, path: str, **filters: object) -> list[dict]:
    page = github_request(token, f"{path}?{urlencode(filters)}")
    if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
        raise preparation.AutomationError(f"GitHub returned an invalid list for {path}.")
    return page


def exact_head_pulls(token: str, branch: str) -> list[dict]:
    owner = REPOSITORY.split("/")[0]
    return github_list(token, PULLS, state="all", head=f"{owner}:{branch}", per_page=100)


def pr_identity(pull: object) -> str | None:
    number = pull.get("number") if isinstance(pull, dict) else None
    if type(number) is int and number > 0:
        return f"https://github.com/{REPOSITORY}/pull/{number}"
    return None


def check_pr_conflicts(token: str, branch: str, version: str) -> None:
    pulls = exact_head_pulls(token, branch)
    if pulls:
        identity = pr_identity(pulls[0]) or "PR number unavailable"
        raise preparation.AutomationError(f"A pull request already exists for {branch}: {identity}.")

    # A maintained branch can propose the same version under a different head or revision.
    ebuild = re.compile(rf"{re.escape(str(preparation.PACKAGE_DIR))}/spotatui-{re.escape(version)}(?:-r[0-9]+)?\.ebuild")
    page_number = 1
    while True:
        pulls = github_list(token, PULLS, state="open", base="main", per_page=100, page=page_number)
        for pull in pulls:
            identity = pr_identity(pull)
            if identity is None:
                raise preparation.AutomationError("GitHub returned an open PR without a valid number.")
            for file_page in range(1, 31):
                files = github_list(token, f"{PULLS}/{pull['number']}/files", per_page=100, page=file_page)
                if any(ebuild.fullmatch(str(file.get("filename", "")))
                       and file.get("status") in {"added", "copied", "renamed"} for file in files):
                    raise preparation.AutomationError(f"An open PR already proposes spotatui {version}: {identity}.")
                if len(files) < 100:
                    break
            else:
                raise preparation.AutomationError(f"Cannot rule out a release conflict at GitHub's 3000-file limit: {identity}.")
        if len(pulls) < 100:
            return
        page_number += 1


def verified_pr(pull: object, request: dict, base: str, commit: str) -> str | None:
    identity = pr_identity(pull)
    if (identity is None or pull.get("html_url") != identity or pull.get("state") != "open"
            or pull.get("draft") is not True or pull.get("title") != request["title"]
            or pull.get("body") != request["body"]):
        return None
    for side, sha in (("head", commit), ("base", base)):
        ref = pull.get(side)
        if not isinstance(ref, dict) or ref.get("ref") != request[side] or ref.get("sha") != sha:
            return None
        repo = ref.get("repo")
        if not isinstance(repo, dict) or repo.get("full_name") != REPOSITORY:
            return None
    return identity


def pull_request_body(report: dict) -> str:
    added = report["crate_added"]
    removed = report["crate_removed"]
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
            f"- Base `{report['base_commit']}`, candidate `{report['candidate_commit']}`, upstream `{report['upstream_commit']}`",
            f"- Verified source SHA512: `{report['archive']['SHA512']}`",
            f"- Cargo.lock SHA-256: `{report['cargo_lock_sha256']}`",
            f"- [Workflow run {report['run_id']}](https://github.com/{REPOSITORY}/actions/runs/{report['run_id']}), "
            f"attempt {report['run_attempt']}, artifact {report['artifact_id']}",
            f"- Artifact SHA-256: `{report['artifact_digest']}`",
            *[f"- {check}: {result}" for check, result in report["validation"].items()],
            "- Publish handoff digest, contents and rotation: passed",
            "- Publish staged paths, modes, tree and commit parent: passed",
            "- Client build and runtime testing are manual acceptance checks.",
            "",
            "## Manual gates before accepting",
            "",
            "- [ ] Review upstream release notes and the full ebuild/Manifest diff.",
            "- [ ] Check whether Cargo changes require new Gentoo system dependencies or packaging decisions.",
            "- [ ] Run `emerge -pv media-sound/spotatui` and build the candidate with the intended USE flags.",
            "- [ ] Test Spotify and enabled optional features at runtime.",
            "- [ ] Manually accept the Draft PR when review is complete.",
            "",
            "GitHub may require approval to run pull request CI for a PR opened with `GITHUB_TOKEN`.",
        ]
    )
    return "\n".join(lines)


def git_auth(env: dict, directory: Path) -> dict:
    askpass = directory / "askpass"
    askpass.write_text(
        """#!/bin/sh
case "$1" in
  *Username*) printf '%s\\n' x-access-token ;;
  *Password*) printf '%s\\n' "$GH_TOKEN" ;;
  *) exit 1 ;;
esac
""",
        encoding="utf-8",
    )
    askpass.chmod(0o700)
    return {**env, "GIT_ASKPASS": str(askpass), "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}


def publish(root: Path, download: Path, env: dict, summary: Path | None) -> int:
    outcome = "no remote write occurred"
    try:
        context = preflight(root, env)
        archives = list(download.iterdir())
        if len(archives) != 1:
            raise preparation.AutomationError("Expected exactly one downloaded handoff archive.")
        base = context["base_commit"]
        with tempfile.TemporaryDirectory(prefix="spotatui-publish-") as temporary:
            git_env = git_auth(env, Path(temporary))
            require_ref(root, MAIN_REF, base, git_env)
            artifact_id = env["ARTIFACT_ID"]
            digest = env["ARTIFACT_DIGEST"]
            report, files = preparation.apply_handoff(root, archives[0], context, artifact_id, digest)
            commit = commit_candidate(root, report, files)
            branch = f"automation/spotatui-{report['tag']}"
            ref = f"refs/heads/{branch}"
            request = {
                "base": "main",
                "head": branch,
                "draft": True,
                "title": f"[release-bump] spotatui {report['tag']}",
                "body": pull_request_body({
                    **report, "candidate_commit": commit, "artifact_id": artifact_id,
                    "artifact_digest": digest.removeprefix("sha256:"),
                }),
            }
            token = env["GH_TOKEN"]
            preparation.summarize(summary, f"Verified local commit `{commit}` for `{ref}` from `{MAIN_REF}` at `{base}`.")

            check_pr_conflicts(token, branch, report["version"])
            existing = remote_ref(root, ref, git_env)
            if existing is not None:
                raise preparation.AutomationError(f"Automation branch {ref} already exists at {existing}.")
            require_ref(root, MAIN_REF, base, git_env)

            try:
                push_new_branch(root, commit, ref, git_env)
            except (preparation.AutomationError, OSError) as error:
                outcome = "branch creation outcome unknown"
                try:
                    observed = remote_ref(root, ref, git_env)
                    preparation.summarize(summary, f"Read-only lookup observed `{ref}` at `{observed or 'absent'}`.")
                except (preparation.AutomationError, OSError) as lookup_error:
                    preparation.summarize(summary, f"Could not determine the remote branch state. {lookup_error}")
                raise preparation.AutomationError(f"{error} Branch ownership is unconfirmed.") from error

            outcome = "branch creation confirmed"
            preparation.summarize(summary, f"Branch creation confirmed for `{ref}` at `{commit}`.")
            check_pr_conflicts(token, branch, report["version"])
            require_ref(root, MAIN_REF, base, git_env)
            require_ref(root, ref, commit, git_env)

            try:
                pull = github_request(token, PULLS, request=request)
                identity = verified_pr(pull, request, base, commit)
                if identity is None:
                    reported = pr_identity(pull)
                    detail = f" Reported PR {reported}." if reported else ""
                    raise preparation.AutomationError(f"GitHub did not confirm the expected Draft PR identity.{detail}")
            except (preparation.AutomationError, OSError) as error:
                outcome = (
                    "branch creation confirmed but Draft PR creation failed"
                    if isinstance(error, PullRequestRejected)
                    else "branch creation confirmed; Draft PR creation outcome unknown"
                )
                try:
                    pulls = exact_head_pulls(token, branch)
                    if not pulls:
                        preparation.summarize(summary, f"Read-only lookup found no PR for `{ref}`.")
                    for pull in pulls:
                        observed = pr_identity(pull) or "PR number unavailable"
                        if verified_pr(pull, request, base, commit):
                            preparation.summarize(summary, f"Read-only lookup found the expected Draft PR {observed}.")
                        else:
                            preparation.summarize(summary, f"Read-only lookup found an exact-head PR with different identity or state: {observed}.")
                except (preparation.AutomationError, OSError) as lookup_error:
                    preparation.summarize(summary, f"Could not determine the PR state. {lookup_error}")
                raise

        preparation.summarize(summary, f"Draft PR creation confirmed: {identity}. Manual acceptance is required.")
        return 0
    except (preparation.AutomationError, OSError, UnicodeError) as error:
        preparation.summarize(summary, f"Publication stopped ({outcome}). {error}")
        if outcome != "no remote write occurred":
            preparation.summarize(
                summary, "Owner intervention is required. Compare the remote refs and exact-head PR "
                "with the reported commit before completing or abandoning publication manually.",
            )
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--download", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args()
    return publish(args.root, args.download, dict(os.environ), args.summary)


if __name__ == "__main__":
    sys.exit(main())
