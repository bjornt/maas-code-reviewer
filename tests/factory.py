from __future__ import annotations

from lp_ci_tools.models import MergeProposal


def _web_link_to_api_url(web_link: str) -> str:
    """Derive a plausible API URL from a web link.

    Example:
        https://code.launchpad.net/~user/project/+git/repo/+merge/1
        -> https://api.launchpad.net/devel/~user/project/+git/repo/+merge/1
    """
    prefix = "https://code.launchpad.net/"
    if web_link.startswith(prefix):
        return "https://api.launchpad.net/devel/" + web_link[len(prefix) :]
    return web_link


def make_mp(
    *,
    url: str = "https://code.launchpad.net/~user/project/+git/repo/+merge/1",
    api_url: str | None = None,
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
        api_url=api_url if api_url is not None else _web_link_to_api_url(url),
        source_git_repository=source_git_repository,
        source_git_path=source_git_path,
        target_git_repository=target_git_repository,
        target_git_path=target_git_path,
        status=status,
        commit_message=commit_message,
        description=description,
    )
