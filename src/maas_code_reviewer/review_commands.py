# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Repository acquisition and publication adapters, independent of CLI parsing."""

import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from maas_code_reviewer.agent_runner import AgentRunner
from maas_code_reviewer.git import GitClient
from maas_code_reviewer.github_client import GitHubClient
from maas_code_reviewer.launchpad_client import LaunchpadClient
from maas_code_reviewer.metrics import ReviewMetrics
from maas_code_reviewer.models import Comment, MergeProposal
from maas_code_reviewer.repo_tools import RepoTools
from maas_code_reviewer.reviewer import (
    REVIEW_MARKER,
    review_diff,
    review_diff_structured,
)

_LP_GIT_BASE = "https://git.launchpad.net/"


@dataclass(frozen=True)
class MergeProposalSummary:
    url: str
    status: str
    last_reviewed: datetime | None


def list_merge_proposals(
    client: LaunchpadClient, project: str, status: str
) -> list[MergeProposalSummary]:
    """Fetch proposals and annotate each with its last bot review timestamp."""
    proposals = client.get_merge_proposals(project, status)
    bot_username = client.get_bot_username()
    return [
        MergeProposalSummary(
            url=mp.url,
            status=mp.status,
            last_reviewed=_find_last_review_date(client.get_comments(mp), bot_username),
        )
        for mp in proposals
    ]


def format_merge_proposals(summaries: list[MergeProposalSummary]) -> str:
    """Format summaries as human-readable text, one line per proposal."""
    return "\n".join(
        f"{s.url} {s.status} "
        f"{s.last_reviewed.isoformat() if s.last_reviewed else 'never'}"
        for s in summaries
    )


def has_existing_review(client: LaunchpadClient, mp: MergeProposal) -> bool:
    """Return whether this bot has already posted a marked review."""
    return (
        _find_last_review_date(client.get_comments(mp), client.get_bot_username())
        is not None
    )


def read_agents_context(
    repo_dir: Path, metrics: ReviewMetrics | None = None
) -> str | None:
    """Load repository guidance as review context, never executable policy."""
    tools = RepoTools(repo_dir)
    agents_md = tools.read_agents_md()
    if metrics is not None:
        metrics.agents_md_read = tools.agents_md_read
    return agents_md


def review_merge_proposal(
    lp: LaunchpadClient,
    git: GitClient,
    runner: AgentRunner | None,
    mp_url: str,
    *,
    max_diff_chars: int = 200_000,
    metrics: ReviewMetrics | None = None,
    model: str | None = None,
    timeout_seconds: float = 300,
    repair_attempts: int = 1,
) -> str | None:
    """Review a merged target/source tree; return None for duplicate reviews.

    The acquired tree stays alive until the synchronous runner has finished,
    including its input snapshot and cleanup. Publication remains host-owned.
    """
    mp = lp.get_merge_proposal(mp_url)
    if has_existing_review(lp, mp):
        return None

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_dir = Path(tmpdir) / "repo"
        git.clone(
            _lp_repo_url(mp.target_git_repository),
            repo_dir,
            _ref_to_branch(mp.target_git_path),
        )
        git.merge_into(
            repo_dir,
            _lp_repo_url(mp.source_git_repository),
            _ref_to_branch(mp.source_git_path),
        )
        diff = git.diff(repo_dir, "ORIG_HEAD", "HEAD")
        return review_diff(
            runner,
            diff,
            mp.description or mp.commit_message,
            repo_dir,
            agents_md=read_agents_context(repo_dir, metrics) if diff.strip() else None,
            max_diff_chars=max_diff_chars,
            metrics=metrics,
            model=model,
            timeout_seconds=timeout_seconds,
            repair_attempts=repair_attempts,
        )


def review_pull_request(
    github: GitHubClient,
    runner: AgentRunner | None,
    owner: str,
    repo: str,
    pr_number: int,
    repo_dir: Path,
    *,
    max_diff_chars: int = 200_000,
    metrics: ReviewMetrics | None = None,
    model: str | None = None,
    timeout_seconds: float = 300,
    repair_attempts: int = 1,
) -> dict:
    """Acquire PR metadata and review the caller's already prepared checkout."""
    diff = github.get_pr_diff(owner, repo, pr_number)
    description = github.get_pr_description(owner, repo, pr_number)
    return review_diff_structured(
        runner,
        diff,
        description,
        repo_dir,
        agents_md=read_agents_context(repo_dir, metrics) if diff.strip() else None,
        max_diff_chars=max_diff_chars,
        metrics=metrics,
        model=model,
        timeout_seconds=timeout_seconds,
        repair_attempts=repair_attempts,
    )


def post_pull_request_review(
    github: GitHubClient, owner: str, repo: str, pr_number: int, review: dict
) -> None:
    """Adapt a host-validated structured review to GitHub's publication API."""
    comments = [
        {"path": path, "line": int(line), "body": body}
        for path, lines in review["inline_comments"].items()
        for line, body in lines.items()
    ]
    github.post_review(
        owner, repo, pr_number, body=review["general_comment"], comments=comments
    )


def _lp_repo_url(unique_name: str) -> str:
    if unique_name.startswith(("/", "file://", "http://", "https://")):
        return unique_name
    return _LP_GIT_BASE + unique_name


def _find_last_review_date(
    comments: list[Comment], bot_username: str
) -> datetime | None:
    return max(
        (
            comment.date
            for comment in comments
            if comment.author == bot_username and comment.body.startswith(REVIEW_MARKER)
        ),
        default=None,
    )


def _ref_to_branch(git_path: str) -> str:
    return git_path.removeprefix("refs/heads/")
