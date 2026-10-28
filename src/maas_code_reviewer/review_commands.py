# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Merge-proposal and pull-request acquisition, review, and publication."""

import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from maas_code_reviewer.git import GitClient
from maas_code_reviewer.github_client import GitHubClient
from maas_code_reviewer.launchpad_client import LaunchpadClient
from maas_code_reviewer.llm_client import DEFAULT_MAX_TOOL_CALLS, GeminiClient
from maas_code_reviewer.metrics import ReviewMetrics
from maas_code_reviewer.models import Comment, MergeProposal
from maas_code_reviewer.repo_tools import RepoTools
from maas_code_reviewer.reviewer import (
    REVIEW_MARKER,
    review_diff,
    review_diff_structured,
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


def read_agents_context(repo_dir: Path) -> tuple[RepoTools, str | None]:
    """Load the repository's AGENTS.md guidance as review context.

    Returns the path-scoped RepoTools (used for the read_file /
    list_directory agent callbacks) together with the AGENTS.md
    contents, or None when the repository has no AGENTS.md.
    """
    tools = RepoTools(repo_dir)
    agents_md = tools.read_agents_md()
    return tools, agents_md


def review_merge_proposal(
    lp: LaunchpadClient,
    git: GitClient,
    llm: GeminiClient,
    mp_url: str,
    max_diff_chars: int = 200_000,
    metrics: ReviewMetrics | None = None,
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
) -> str | None:
    """Review a single merge proposal end to end.

    Returns the review comment body, or ``None`` if the MP was already
    reviewed.  The caller is responsible for posting or printing the result.
    """
    mp = lp.get_merge_proposal(mp_url)

    if has_existing_review(lp, mp):
        return None

    target_branch = _ref_to_branch(mp.target_git_path)
    source_branch = _ref_to_branch(mp.source_git_path)
    target_url = _lp_repo_url(mp.target_git_repository)
    source_url = _lp_repo_url(mp.source_git_repository)

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_dir = Path(tmpdir) / "repo"
        git.clone(target_url, repo_dir, target_branch)
        git.merge_into(repo_dir, source_url, source_branch)

        diff = git.diff(repo_dir, "ORIG_HEAD", "HEAD")

        tools, agents_md = read_agents_context(repo_dir)
        description = mp.description or mp.commit_message
        review_comment = review_diff(
            llm,
            diff=diff,
            description=description,
            read_file=tools.read_file,
            list_directory=tools.list_directory,
            agents_md=agents_md,
            max_diff_chars=max_diff_chars,
            metrics=metrics,
            max_tool_calls=max_tool_calls,
        )

        if metrics is not None:
            metrics.files_read = tools.files_read_count
            metrics.agents_md_read = tools.agents_md_read

    return review_comment


def review_pull_request(
    github: GitHubClient,
    llm: GeminiClient,
    owner: str,
    repo: str,
    pr_number: int,
    repo_dir: Path,
    max_diff_chars: int = 200_000,
    metrics: ReviewMetrics | None = None,
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
) -> dict:
    """Acquire a pull request's diff and description and review them.

    Returns the structured review document; the caller decides whether
    to print or post it.
    """
    diff = github.get_pr_diff(owner, repo, pr_number)
    description = github.get_pr_description(owner, repo, pr_number)
    tools, agents_md = read_agents_context(repo_dir)
    result_dict = review_diff_structured(
        llm,
        diff=diff,
        description=description,
        read_file=tools.read_file,
        list_directory=tools.list_directory,
        agents_md=agents_md,
        max_diff_chars=max_diff_chars,
        metrics=metrics,
        max_tool_calls=max_tool_calls,
    )
    if metrics is not None:
        metrics.files_read = tools.files_read_count
        metrics.agents_md_read = tools.agents_md_read
    return result_dict


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
