# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Merge-proposal and pull-request acquisition and publication."""

import base64
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from maas_code_reviewer.git import GitClient
from maas_code_reviewer.github_client import GitHubClient, parse_pr_url
from maas_code_reviewer.launchpad_client import LaunchpadClient
from maas_code_reviewer.models import Comment, MergeProposal
from maas_code_reviewer.repo_tools import RepoTools
from maas_code_reviewer.review_schema import validate_review_json
from maas_code_reviewer.reviewer import (
    REVIEW_MARKER,
    REVIEW_PREAMBLE,
)


@dataclass(frozen=True)
class MergeProposalSummary:
    url: str
    status: str
    last_reviewed: datetime | None


def list_merge_proposals(
    client: LaunchpadClient, project: str, status: str
) -> list[MergeProposalSummary]:
    """Fetch merge proposals and annotate each with its last review timestamp."""
    proposals = client.get_merge_proposals(project, status)
    bot_username = client.get_bot_username()
    summaries = []
    for mp in proposals:
        comments = client.get_comments(mp)
        last_reviewed = _find_last_review_date(comments, bot_username)
        summaries.append(
            MergeProposalSummary(
                url=mp.url,
                status=mp.status,
                last_reviewed=last_reviewed,
            )
        )
    return summaries


def format_merge_proposals(summaries: list[MergeProposalSummary]) -> str:
    """Format summaries as human-readable text, one line per proposal."""
    lines = []
    for s in summaries:
        reviewed = s.last_reviewed.isoformat() if s.last_reviewed else "never"
        lines.append(f"{s.url} {s.status} {reviewed}")
    return "\n".join(lines)


def has_existing_review(client: LaunchpadClient, mp: MergeProposal) -> bool:
    """Return True if the bot has already posted a review on this MP."""
    comments = client.get_comments(mp)
    bot_username = client.get_bot_username()
    return _find_last_review_date(comments, bot_username) is not None


def read_agents_context(repo_dir: Path) -> str | None:
    """Load the repository's AGENTS.md guidance as review context.

    Reads through the path-scoped RepoTools, so a symlinked AGENTS.md
    cannot pull content in from outside the repository tree. Returns
    None when the repository has no readable AGENTS.md.
    """
    return RepoTools(repo_dir).read_agents_md()


def fetch_merge_proposal(
    lp: LaunchpadClient,
    git: GitClient,
    mp_url: str,
    output_dir: Path,
    *,
    git_token: str | None = None,
) -> bool:
    """Fetch a merge proposal's review inputs into *output_dir*.

    Clones the target repository into ``output_dir/repo``, merges the source
    branch, and writes ``diff.patch``, ``description.md``, and ``metadata.json``
    (URLs, branch names, base/head SHAs, and the diff path prefix when it is
    not the standard ``a/`` + ``b/``). Returns True.

    When this tool has already reviewed the proposal, no clone happens:
    ``status.json`` (``{"status": "skipped", "reason": "already-reviewed"}``)
    is written instead and False is returned, so workflows can short-circuit
    before running a review.

    When *git_token* is given, the clone and the source fetch authenticate
    with an ``Authorization: Basic`` header built from the bot's Launchpad
    username and the token — never an on-disk credential helper.
    """
    mp = lp.get_merge_proposal(mp_url)

    if has_existing_review(lp, mp):
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "status.json").write_text(
            json.dumps({"status": "skipped", "reason": "already-reviewed"}, indent=2)
        )
        return False

    http_extra_header = None
    if git_token is not None:
        credentials = base64.b64encode(
            f"{lp.get_bot_username()}:{git_token}".encode()
        ).decode("ascii")
        http_extra_header = f"Authorization: Basic {credentials}"

    output_dir.mkdir(parents=True, exist_ok=True)
    repo_dir = output_dir / "repo"
    diff = _clone_merge_diff(git, mp, repo_dir, http_extra_header=http_extra_header)

    description = mp.description or mp.commit_message or ""
    (output_dir / "diff.patch").write_text(diff)
    (output_dir / "description.md").write_text(description)
    (output_dir / "metadata.json").write_text(
        json.dumps(
            {
                "mp_url": mp_url,
                "target_url": _lp_repo_url(mp.target_git_repository),
                "target_branch": _ref_to_branch(mp.target_git_path),
                "source_url": _lp_repo_url(mp.source_git_repository),
                "source_branch": _ref_to_branch(mp.source_git_path),
                "base_sha": git.rev_parse(repo_dir, "ORIG_HEAD"),
                "head_sha": git.rev_parse(repo_dir, "HEAD"),
                "diff_prefix": _detect_diff_prefix(diff),
            },
            indent=2,
        )
    )
    return True


class InvalidReviewError(Exception):
    """Raised when a candidate review document fails publish-side validation."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("\n".join(["Invalid review document:", *errors]))


def load_validated_review(review_path: Path, input_dir: Path) -> dict:
    """Parse a candidate review and validate it against the fetch output.

    The candidate JSON at *review_path* is validated against the authoritative
    ``diff.patch`` in *input_dir*, resolving inline coordinates with the diff
    path prefix ``fetch-mp`` recorded in ``metadata.json``. Deliberately no
    repository listing is passed: the recorded prefix replaces it on the
    trusted side.

    Returns the parsed review document, or raises InvalidReviewError when the
    candidate is invalid — including unparseable JSON and input-diff errors
    such as prefix mismatches. Nothing is posted.
    """
    try:
        review = json.loads(review_path.read_text())
    except json.JSONDecodeError as exc:
        raise InvalidReviewError([f"Review file is not valid JSON: {exc}"]) from exc
    diff = (input_dir / "diff.patch").read_text()
    metadata = json.loads((input_dir / "metadata.json").read_text())
    try:
        errors = validate_review_json(
            review, diff, path_prefix=metadata.get("diff_prefix")
        )
    except ValueError as exc:
        raise InvalidReviewError([str(exc)]) from exc
    if errors:
        raise InvalidReviewError(errors)
    return review


def render_merge_proposal_comment(review: dict) -> str:
    """Render a validated review document as Launchpad comment text.

    The general comment leads; every inline finding follows as its own
    ``### <path>:<line>`` section with the finding's relative path and line,
    in the document's key order. The review marker and preamble are
    prepended exactly once: the document itself never carries them, and any
    marker text the candidate did carry is removed first.
    """
    sections = [_decorate_review_text(review["general_comment"])]
    for file_path, line_comments in review["inline_comments"].items():
        for line, comment in line_comments.items():
            sections.append(f"### {file_path}:{line}\n\n{comment}")
    return "\n\n".join(sections)


def fetch_pull_request(
    github: GitHubClient,
    pr_url: str,
    output_dir: Path,
) -> None:
    """Fetch a pull request's review inputs into *output_dir*.

    The diff, description, and base/head SHAs are fetched through the
    GitHub API — no checkout is performed; the workflow or operator
    provides the agent's repository context separately. Writes
    ``diff.patch``, ``description.md``, and ``metadata.json`` (PR URL,
    owner/repo/number, base/head SHAs, and the diff path prefix when it
    is not the standard ``a/`` + ``b/``).
    """
    owner, repo, pr_number = parse_pr_url(pr_url)
    diff = github.get_pr_diff(owner, repo, pr_number)
    description = github.get_pr_description(owner, repo, pr_number)
    base_sha, head_sha = github.get_pr_shas(owner, repo, pr_number)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "diff.patch").write_text(diff)
    (output_dir / "description.md").write_text(description or "")
    (output_dir / "metadata.json").write_text(
        json.dumps(
            {
                "pr_url": pr_url,
                "owner": owner,
                "repo": repo,
                "number": pr_number,
                "base_sha": base_sha,
                "head_sha": head_sha,
                "diff_prefix": _detect_diff_prefix(diff),
            },
            indent=2,
        )
    )


def render_pull_request_review(review: dict) -> dict:
    """Render a validated review document for publication to GitHub.

    Returns a shallow copy of the document with the review marker and
    preamble prepended to the general comment exactly once; inline
    findings are unchanged — GitHub's review API takes the general
    comment as the review body and each finding as a structured comment,
    so no comment text is composed. Any marker text the candidate itself
    carried is removed first: the host owns this wrapper.
    """
    rendered = dict(review)
    rendered["general_comment"] = _decorate_review_text(review["general_comment"])
    return rendered


def post_pull_request_review(
    github: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    review: dict,
) -> None:
    """Adapt a structured review to GitHub's API and post it."""
    general_comment = review.get("general_comment", "")
    inline_comments = review.get("inline_comments", {})
    comments = [
        {"path": file_path, "line": int(line_str), "body": comment_body}
        for file_path, line_map in inline_comments.items()
        for line_str, comment_body in line_map.items()
    ]
    github.post_review(
        owner, repo, pr_number, body=general_comment, comments=comments
    )


_LP_GIT_BASE = "https://git.launchpad.net/"


def _lp_repo_url(unique_name: str) -> str:
    """Convert a Launchpad repo unique name to a git clone URL.

    If the provided name already looks like an absolute path or URL
    (leading '/', 'file://', 'http://', or 'https://'), return it unchanged.
    """
    if (
        unique_name.startswith("/")
        or unique_name.startswith("file://")
        or unique_name.startswith("http://")
        or unique_name.startswith("https://")
    ):
        return unique_name
    return _LP_GIT_BASE + unique_name


def _find_last_review_date(
    comments: list[Comment], bot_username: str
) -> datetime | None:
    """Find the timestamp of the most recent review comment by the bot."""
    review_dates = [
        comment.date
        for comment in comments
        if comment.author == bot_username and comment.body.startswith(REVIEW_MARKER)
    ]
    if not review_dates:
        return None
    return max(review_dates)


def _ref_to_branch(git_path: str) -> str:
    """Convert a refs/heads/branch-name path to just the branch name."""
    prefix = "refs/heads/"
    if git_path.startswith(prefix):
        return git_path[len(prefix) :]
    return git_path


def _clone_merge_diff(
    git: GitClient,
    mp: MergeProposal,
    repo_dir: Path,
    *,
    http_extra_header: str | None = None,
) -> str:
    """Clone the MP target into *repo_dir*, merge the source, diff the result."""
    git.clone(
        _lp_repo_url(mp.target_git_repository),
        repo_dir,
        _ref_to_branch(mp.target_git_path),
        http_extra_header=http_extra_header,
    )
    git.merge_into(
        repo_dir,
        _lp_repo_url(mp.source_git_repository),
        _ref_to_branch(mp.source_git_path),
        http_extra_header=http_extra_header,
    )
    return git.diff(repo_dir, "ORIG_HEAD", "HEAD")


def _diff_header_path(value: str) -> str:
    """Strip timestamps and C-style quoting from a file header path."""
    path = value.partition("\t")[0]
    if path.startswith('"'):
        path = path[1:].partition('"')[0]
    return path


def _detect_diff_prefix(diff_text: str) -> str | None:
    """Return the new-side diff path prefix of *diff_text*.

    Standard ``a/`` + ``b/`` diffs (including new-file headers whose old side
    is ``/dev/null``) return None — validation's ladder needs no override.
    Any other leading path component is returned with its slash (``w/``,
    ``YY/`` from custom src/dst prefixes), and prefix-free paths (``diff
    noprefix``, or identical old/new paths) return the empty string.
    """
    old_path: str | None = None
    for raw_line in diff_text.splitlines():
        if raw_line.startswith("diff --git "):
            old_path = None
        elif old_path is None and raw_line.startswith("--- "):
            old_path = _diff_header_path(raw_line[4:])
        elif old_path is not None and raw_line.startswith("+++ "):
            new_path = _diff_header_path(raw_line[4:])
            if new_path == "/dev/null":
                old_path = None
            elif new_path.startswith("b/") and (
                old_path.startswith("a/") or old_path == "/dev/null"
            ):
                return None
            elif old_path == new_path:
                return ""
            else:
                head, separator, _ = new_path.partition("/")
                return head + separator if separator else ""
    return None


def _decorate_review_text(comment: str) -> str:
    """Prepend the review marker and preamble exactly once.

    Marker or preamble text already present in *comment* — for instance
    repeated by the agent that produced the candidate — is removed first:
    the host owns this wrapper.
    """
    comment = (
        comment.replace(REVIEW_MARKER, "")
        .replace(REVIEW_PREAMBLE.strip(), "")
        .strip()
    )
    return f"{REVIEW_MARKER}\n\n{REVIEW_PREAMBLE}\n\n{comment}"
