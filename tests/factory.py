from __future__ import annotations

from lp_ci_tools.models import MergeProposal


def make_mp(
    *,
    url: str = "https://code.launchpad.net/~user/project/+git/repo/+merge/1",
    source_git_repository: str = "~user/project/+git/repo",
    source_git_path: str = "refs/heads/feature",
    target_git_repository: str = "myproject",
    target_git_path: str = "refs/heads/main",
    status: str = "Needs review",
    commit_message: str | None = None,
    description: str | None = None,
) -> MergeProposal:
    return MergeProposal(
        url=url,
        source_git_repository=source_git_repository,
        source_git_path=source_git_path,
        target_git_repository=target_git_repository,
        target_git_path=target_git_path,
        status=status,
        commit_message=commit_message,
        description=description,
    )
