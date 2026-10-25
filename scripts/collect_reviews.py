#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd. This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Collect model and human reviews for the last N first-parent landed commits.

The PR description is fetched now, not reconstructed at review time. The original
model is an operator label, not a model identity established by GitHub's review
API. Existing corpus files are immutable: reruns resume identical data and fail
rather than replacing changed descriptions, reviews, or selection manifests.
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

REVIEW_MARKER = "[maas-code-reviewer review]"
DEFAULT_MODEL = "gemini-3.1-pro-preview"
API_ROOT = "https://api.github.com"
SCHEMA_VERSION = 2


class CollectionError(RuntimeError):
    """An incomplete or conflicting collection that must not report success."""


class GitHubAPI:
    def __init__(self, token: str | None = None, *, opener=urlopen):
        self.token = token
        self.opener = opener

    def get(self, path: str) -> dict:
        data, _ = self._request(f"{API_ROOT}/{path}")
        if not isinstance(data, dict):
            raise CollectionError(f"Expected an API object for {path}")
        return data

    def pages(self, path: str) -> list[dict]:
        url = f"{API_ROOT}/{path}?per_page=100"
        items: list[dict] = []
        seen: set[str] = set()
        while url:
            if url in seen:
                raise CollectionError(f"Repeated GitHub pagination URL: {url}")
            seen.add(url)
            data, links = self._request(url)
            if not isinstance(data, list):
                raise CollectionError(f"Expected an API list for {url}")
            for item in data:
                if not isinstance(item, dict):
                    raise CollectionError(f"Expected API objects in list for {url}")
                items.append(item)
            url = ""
            for link in links.split(","):
                target, separator, relations = link.strip().partition(";")
                if separator and 'rel="next"' in relations:
                    url = target.strip().removeprefix("<").removesuffix(">")
        return items

    def _request(self, url: str) -> tuple[object, str]:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "api.github.com":
            raise CollectionError(f"Unsafe GitHub API URL: {url}")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "maas-code-reviewer-corpus-collector",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            response = self.opener(Request(url, headers=headers), timeout=60)
        except HTTPError as error:
            raise CollectionError(
                f"GitHub API HTTP {error.code} for {url}: {error.reason}"
            ) from error
        except URLError as error:
            raise CollectionError(
                f"GitHub API request failed for {url}: {error}"
            ) from error
        with response:
            try:
                data = json.load(response)
            except (ValueError, UnicodeError) as error:
                raise CollectionError(
                    f"Invalid GitHub JSON response for {url}"
                ) from error
            return data, response.headers.get("Link", "")


class GitCache:
    def __init__(self, path: Path, remote: str, token: str | None = None):
        self.path = path
        self.env = os.environ.copy()
        self.env.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_TERMINAL_PROMPT="0",
            GIT_NO_REPLACE_OBJECTS="1",
        )
        # Pass private-repository credentials via environment, never argv or URLs.
        self.env["GIT_CONFIG_COUNT"] = "0"
        if token and remote.startswith("https://github.com/"):
            encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            self.env.update(
                GIT_CONFIG_COUNT="1",
                GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                GIT_CONFIG_VALUE_0=f"Authorization: Basic {encoded}",
            )
        if path.is_symlink():
            raise CollectionError(f"Refusing symlink repository cache: {path}")
        if not path.exists():
            path.mkdir(parents=True)
            self.run("init", "--bare", ".")
            self.run("remote", "add", "origin", remote)
            self.run("config", "remote.origin.promisor", "true")
            self.run("config", "remote.origin.partialclonefilter", "blob:none")
        if self.run("rev-parse", "--is-bare-repository").strip() != b"true":
            raise CollectionError(f"Not a bare repository: {path}")
        if self.run("remote", "get-url", "origin").decode().strip() != remote:
            raise CollectionError(
                f"Existing repository cache has a different origin: {path}"
            )

    def run(self, *args: str) -> bytes:
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "credential.helper=",
                *args,
            ],
            cwd=self.path,
            env=self.env,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            message = result.stderr.decode(errors="replace").strip()
            raise CollectionError(f"git {' '.join(args)} failed: {message}")
        return result.stdout

    def fetch(self, ref: str) -> str:
        if (
            not ref
            or ref.startswith("-")
            or ":" in ref
            or any(c.isspace() for c in ref)
        ):
            raise CollectionError(f"Unsafe Git ref: {ref!r}")
        self.run("fetch", "--filter=blob:none", "--no-tags", "origin", ref)
        return self.run("rev-parse", "--verify", "FETCH_HEAD^{commit}").decode().strip()

    def cache_review_head(self, head: str) -> None:
        reference = f"refs/review-heads/{head}"
        try:
            cached = self.run("show-ref", "--verify", "--hash", reference)
        except CollectionError:
            fetched = self.fetch(head)
            if fetched != head:
                raise CollectionError(f"Fetched wrong review head: {head}") from None
            self.run("update-ref", reference, head)
        else:
            if cached.decode().strip() != head:
                raise CollectionError(f"Conflicting cached review head: {head}")

    def commits(self, head: str, count: int) -> list[str]:
        return (
            self.run("rev-list", "--first-parent", f"--max-count={count}", head)
            .decode()
            .splitlines()
        )

    def parents(self, commit: str) -> list[str]:
        return self.run("show", "-s", "--format=%P", commit).decode().split()

    def diff(self, base: str, head: str) -> bytes:
        return self.run(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            base,
            head,
            "--",
        )


def collect(
    output: Path,
    count: int,
    repository: str = "canonical/maas",
    ref: str | None = None,
    original_model: str = DEFAULT_MODEL,
    *,
    api: GitHubAPI | None = None,
    remote_url: str | None = None,
) -> dict:
    """Build an immutable corpus; injectable API/remote keep tests offline."""
    _validate_repository(repository)
    if count < 1:
        raise CollectionError("--count must be positive")
    if not original_model or original_model in {".", ".."}:
        raise CollectionError(
            "--original-model must be a nonempty, non-dot model label"
        )
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    api = api or GitHubAPI(token)
    selected_ref = ref or api.get(f"repos/{repository}")["default_branch"]
    _directory(output)
    cache = GitCache(
        output / "repository.git",
        remote_url or f"https://github.com/{repository}.git",
        token,
    )
    head = cache.fetch(selected_ref)
    cache.run("update-ref", "refs/collection/selected", head)
    commits = cache.commits(head, count)
    selection = {
        "repository": repository,
        "ref": selected_ref,
        "resolved_head": head,
        "count": count,
        "ordering": "newest-first, first-parent history",
        "original_model": original_model,
    }
    manifest_path = output / "manifest.json"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if existing.get("schema_version") != SCHEMA_VERSION:
            raise CollectionError(
                "Corpus schema changed; collect into a new output directory"
            )
        if existing.get("selection") != selection:
            raise CollectionError(
                "Existing manifest selects different commits or settings"
            )
    entries = []
    for commit in commits:
        entry: dict = {
            "landed_commit": commit,
            "cases": [],
            "human_reviews": [],
            "pull_requests": [],
        }
        entries.append(entry)
        parents = cache.parents(commit)
        if not parents:
            entry.update(status="skipped", skip_reason="root_commit_has_no_parent")
            continue
        associated = api.pages(f"repos/{repository}/commits/{commit}/pulls")
        numbers = sorted(
            {_positive_id(pr.get("number"), "PR number") for pr in associated}
        )
        for number in numbers:
            pr = api.get(f"repos/{repository}/pulls/{number}")
            base_repo = (pr.get("base") or {}).get("repo") or {}
            if not (
                pr.get("merged") is True
                and pr.get("merge_commit_sha") == commit
                and base_repo.get("full_name", "").lower() == repository.lower()
            ):
                continue
            pr_entry: dict = {"pr_number": number, "cases": [], "skipped_reviews": []}
            entry["pull_requests"].append(pr_entry)
            published = api.pages(f"repos/{repository}/pulls/{number}/reviews")
            reviews = [review for review in published if _matching_review(review)]
            comments = api.pages(f"repos/{repository}/pulls/{number}/comments")
            conversation = api.pages(f"repos/{repository}/issues/{number}/comments")
            pr_entry.update(
                _collect_human_reviews(
                    output,
                    cache,
                    repository,
                    pr,
                    parents[0],
                    published,
                    comments,
                    conversation,
                )
            )
            entry["human_reviews"].extend(pr_entry["human_reviews"])
            for review in sorted(
                reviews, key=lambda item: _positive_id(item.get("id"), "review ID")
            ):
                review_id = _positive_id(review.get("id"), "review ID")
                review_head = _sha(review.get("commit_id"))
                try:
                    cache.cache_review_head(review_head)
                except CollectionError as error:
                    pr_entry["skipped_reviews"].append(
                        {
                            "review_id": review_id,
                            "skip_reason": "review_head_unavailable",
                            "detail": str(error),
                        }
                    )
                    print(
                        f"warning: skipping review {review_id}: {error}",
                        file=sys.stderr,
                    )
                    continue
                base = cache.run("merge-base", parents[0], review_head).decode().strip()
                case_id = f"{commit}-pr{number}-review{review_id}"
                baseline, unsupported = _review_json(review, comments, review_head)
                if unsupported:
                    print(
                        f"warning: {case_id}: preserving {len(unsupported)} "
                        "unsupported inline findings in metadata.json "
                        "(not baseline JSON)",
                        file=sys.stderr,
                    )
                metadata = {
                    "repository": repository,
                    "landed_commit": commit,
                    "pr_number": number,
                    "review_id": review_id,
                    "head_sha": review_head,
                    "base_sha": base,
                    "original_model": original_model,
                    "original_model_source": (
                        "operator label; not verified per review by GitHub"
                    ),
                    "description_source": (
                        "latest fetched PR body; NOT the review-time body"
                    ),
                    "description_updated_at": pr.get("updated_at"),
                    "description_url": pr.get("html_url"),
                    "diff_source": (
                        "git diff of merge-base(landed first parent, review head) "
                        "to review head"
                    ),
                    "review_url": review.get("html_url"),
                    "review_submitted_at": review.get("submitted_at"),
                    "general_comment_source": (
                        "verbatim published review body, including marker/preamble"
                    ),
                    "duplicate_inline_comments": (
                        "joined in comment-ID order with a markdown horizontal rule"
                    ),
                    "unsupported_inline_comment_count": len(unsupported),
                    "unsupported_inline_comments": unsupported,
                }
                case_dir = output / "cases" / case_id
                _directory(case_dir)
                _write_once(case_dir / "diff.patch", cache.diff(base, review_head))
                body = pr.get("body")
                if body is not None and not isinstance(body, str):
                    raise CollectionError(f"Invalid description for PR {number}")
                _write_once(case_dir / "description.md", (body or "").encode())
                _write_once(case_dir / "metadata.json", _json_bytes(metadata))
                model_dir = output / "reviews" / quote(original_model, safe="")
                _directory(model_dir)
                _write_once(model_dir / f"{case_id}.json", _json_bytes(baseline))
                entry["cases"].append(case_id)
                pr_entry["cases"].append(case_id)
            if (
                pr_entry["cases"]
                or pr_entry["human_reviews"]
                or pr_entry["human_comment_count"]
            ):
                pr_entry["status"] = "collected"
            else:
                pr_entry.update(
                    status="skipped",
                    skip_reason=(
                        "review_heads_unavailable"
                        if reviews
                        else "no_matching_published_reviews"
                    ),
                )
        if entry["cases"] or any(
            pr["status"] == "collected" for pr in entry["pull_requests"]
        ):
            entry["status"] = "collected"
        else:
            reason = (
                "no_matching_merged_pull_request"
                if not entry["pull_requests"]
                else "no_collectable_published_reviews"
            )
            entry.update(status="skipped", skip_reason=reason)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "selection": selection,
        "commits": entries,
    }
    _write_once(manifest_path, _json_bytes(manifest))
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", required=True, type=_positive_count)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repository", default="canonical/maas")
    parser.add_argument(
        "--ref", help="branch, tag, or commit; defaults to the default branch"
    )
    parser.add_argument("--original-model", default=DEFAULT_MODEL)
    args = parser.parse_args(argv)
    try:
        manifest = collect(
            args.output, args.count, args.repository, args.ref, args.original_model
        )
    except (CollectionError, OSError, ValueError, KeyError) as error:
        print(f"collection failed: {error}", file=sys.stderr)
        return 1
    total = sum(len(commit["cases"]) for commit in manifest["commits"])
    human_total = sum(len(commit["human_reviews"]) for commit in manifest["commits"])
    print(
        f"Collected {total} model reviews and {human_total} human reviews across "
        f"{len(manifest['commits'])} first-parent commits"
    )
    return 0


def _collect_human_reviews(
    output: Path,
    cache: GitCache,
    repository: str,
    pr: dict,
    base_parent: str,
    reviews: list[dict],
    comments: list[dict],
    conversation: list[dict],
) -> dict:
    number = _positive_id(pr["number"], "PR number")
    commit = _sha(pr["merge_commit_sha"])
    directory = output / "human_reviews" / f"{commit}-pr{number}"
    _directory(directory)
    unpublished = {
        review["id"]
        for review in reviews
        if not review.get("submitted_at") or review.get("state") == "PENDING"
    }
    human_comments = sorted(
        (
            comment
            for comment in comments
            if _is_human(comment.get("user") or {})
            and comment.get("pull_request_review_id") not in unpublished
        ),
        key=lambda comment: _positive_id(comment.get("id"), "comment ID"),
    )
    human_conversation = sorted(
        (comment for comment in conversation if _is_human(comment.get("user") or {})),
        key=lambda comment: _positive_id(comment.get("id"), "comment ID"),
    )
    discussion = {
        "repository": repository,
        "landed_commit": commit,
        "pr_number": number,
        "pr_author": (pr.get("user") or {}).get("login"),
        "review_comments": human_comments,
        "conversation_comments": human_conversation,
    }
    _write_once(directory / "discussion.json", _json_bytes(discussion))
    records = []
    for review in sorted(
        reviews, key=lambda item: _positive_id(item.get("id"), "review ID")
    ):
        if not (
            _is_human(review.get("user") or {})
            and review.get("submitted_at")
            and review.get("state") != "PENDING"
        ):
            continue
        review_id = _positive_id(review["id"], "review ID")
        head = _sha(review.get("commit_id"))
        document, unsupported = _review_json(review, comments, head)
        record = directory / f"review{review_id}"
        _directory(record)
        author = review["user"]
        pr_author = pr.get("user") or {}
        metadata = {
            "repository": repository,
            "landed_commit": commit,
            "pr_number": number,
            "review_id": review_id,
            "head_sha": head,
            "author": author["login"],
            "author_id": author.get("id"),
            "is_pr_author": _same_author(author, pr_author) if pr_author else None,
            "state": review.get("state"),
            "review_url": review.get("html_url"),
            "review_submitted_at": review.get("submitted_at"),
            "description_source": "latest fetched PR body; NOT the review-time body",
            "description_updated_at": pr.get("updated_at"),
            "diff_source": (
                "git diff of merge-base(landed first parent, review head) "
                "to review head"
            ),
            "unsupported_inline_comment_count": len(unsupported),
            "unsupported_inline_comments": unsupported,
            "published_review": review,
        }
        try:
            cache.cache_review_head(head)
            base = cache.run("merge-base", base_parent, head).decode().strip()
            diff = cache.diff(base, head)
        except CollectionError as error:
            metadata.update(
                base_sha=None, diff_status="unavailable", diff_error=str(error)
            )
            print(
                f"warning: human review {review_id}: could not reconstruct diff: "
                f"{error}",
                file=sys.stderr,
            )
        else:
            _write_once(record / "diff.patch", diff)
            metadata.update(base_sha=base, diff_status="collected")
        body = pr.get("body")
        if body is not None and not isinstance(body, str):
            raise CollectionError(f"Invalid description for PR {number}")
        _write_once(record / "description.md", (body or "").encode())
        _write_once(record / "review.json", _json_bytes(document))
        _write_once(record / "metadata.json", _json_bytes(metadata))
        records.append(record.relative_to(output).as_posix())
        if unsupported:
            print(
                f"warning: human review {review_id}: preserving {len(unsupported)} "
                "unsupported inline findings in metadata.json",
                file=sys.stderr,
            )
    return {
        "human_reviews": records,
        "human_discussion": (directory / "discussion.json")
        .relative_to(output)
        .as_posix(),
        "human_comment_count": len(human_comments) + len(human_conversation),
    }


def _review_json(
    review: dict, comments: list[dict], head: str
) -> tuple[dict, list[dict]]:
    inline: dict[str, dict[str, str]] = {}
    unsupported = []
    own_comments = [
        comment
        for comment in comments
        if comment.get("pull_request_review_id") == review["id"]
        and comment.get("in_reply_to_id") is None
        and _same_author(comment.get("user") or {}, review["user"])
    ]
    for comment in sorted(
        own_comments, key=lambda item: _positive_id(item.get("id"), "comment ID")
    ):
        path = comment.get("path")
        line = comment.get("original_line")
        reason = ""
        if not _safe_path(path):
            reason = "unsupported or unsafe repository path"
        elif comment.get("side", "RIGHT") != "RIGHT":
            reason = (
                "left-side coordinate cannot be represented in right-side review JSON"
            )
        elif comment.get("original_commit_id") not in {None, head}:
            reason = "original coordinate belongs to a different review head"
        if line is None:
            line = comment.get("line")
            if comment.get("commit_id") not in {None, head}:
                reason = (
                    reason or "current coordinate belongs to a different review head"
                )
        if not isinstance(line, int) or isinstance(line, bool) or line < 1:
            reason = reason or "missing supported line coordinate"
        start = comment.get("original_start_line") or comment.get("start_line")
        if start is not None and start != line:
            reason = (
                reason
                or "multiline range cannot be represented in single-line review JSON"
            )
        body = comment.get("body")
        if not isinstance(body, str):
            raise CollectionError(f"Invalid comment body for {comment.get('id')}")
        if reason:
            unsupported.append({"reason": reason, "comment": comment})
            continue
        assert isinstance(path, str)
        lines = inline.setdefault(path, {})
        coordinate = str(line)
        if coordinate in lines:
            lines[coordinate] += "\n\n---\n\n" + body
        else:
            lines[coordinate] = body
    body = review.get("body")
    if body is not None and not isinstance(body, str):
        raise CollectionError(f"Invalid review body for {review['id']}")
    return {"general_comment": body or "", "inline_comments": inline}, unsupported


def _matching_review(review: dict) -> bool:
    user = review.get("user") or {}
    body = review.get("body")
    return (
        user.get("type") == "Bot"
        and isinstance(user.get("login"), str)
        and user["login"].endswith("[bot]")
        and isinstance(body, str)
        and body.startswith(REVIEW_MARKER)
        and bool(review.get("submitted_at"))
        and review.get("state") != "PENDING"
    )


def _is_human(user: dict) -> bool:
    return (
        user.get("type") == "User"
        and isinstance(user.get("login"), str)
        and bool(user["login"])
    )


def _same_author(author: dict, reviewer: dict) -> bool:
    return (
        author.get("type") in {"User", "Bot"}
        and author.get("type") == reviewer.get("type")
        and author.get("login") == reviewer.get("login")
        and (
            author.get("id") == reviewer.get("id")
            if author.get("id") is not None and reviewer.get("id") is not None
            else True
        )
    )


def _safe_path(path: object) -> bool:
    return (
        isinstance(path, str)
        and bool(path)
        and not path.startswith("/")
        and "\x00" not in path
        and all(part not in {"", ".", ".."} for part in path.split("/"))
    )


def _positive_id(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise CollectionError(f"Invalid {label}: {value!r}")
    return value


def _sha(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) not in {40, 64}
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CollectionError(f"Invalid review commit SHA: {value!r}")
    return value


def _validate_repository(repository: str) -> None:
    parts = repository.split("/")
    if len(parts) != 2 or any(
        not part
        or part in {".", ".."}
        or any(
            not character.isascii() or not (character.isalnum() or character in "-_.")
            for character in part
        )
        for part in parts
    ):
        raise CollectionError("--repository must be an owner/name GitHub repository")


def _positive_count(value: str) -> int:
    count = int(value)
    if count < 1:
        raise argparse.ArgumentTypeError("count must be positive")
    return count


def _directory(path: Path) -> None:
    if path.is_symlink():
        raise CollectionError(f"Refusing symlink directory: {path}")
    if path.parent != path and not path.parent.exists():
        _directory(path.parent)
    if path.parent.is_symlink():
        raise CollectionError(f"Refusing symlink parent directory: {path.parent}")
    path.mkdir(exist_ok=True)


def _json_bytes(data: object) -> bytes:
    return (
        json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode()


def _write_once(path: Path, content: bytes) -> None:
    if path.is_symlink():
        raise CollectionError(f"Refusing symlink output: {path}")
    if path.exists():
        if path.read_bytes() != content:
            raise CollectionError(f"Existing corpus data differs: {path}")
        return
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".collect-") as temporary:
        temporary.write(content)
        temporary.flush()
        try:
            os.link(temporary.name, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != content:
                raise CollectionError(f"Existing corpus data differs: {path}") from None


if __name__ == "__main__":
    raise SystemExit(main())
