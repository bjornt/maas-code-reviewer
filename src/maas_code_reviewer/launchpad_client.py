# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

from datetime import datetime
from typing import Any

from launchpadlib.launchpad import Launchpad

from maas_code_reviewer.models import Comment, MergeProposal

_SERVICE_ROOT = "https://api.launchpad.net/devel/"
_WEB_ROOT = "https://code.launchpad.net/"


def web_url_to_api_url(url: str) -> str:
    """Convert a Launchpad web URL to its API equivalent.

    If the URL is already an API URL or doesn't match the web root,
    return it unchanged.
    """
    if url.startswith(_WEB_ROOT):
        return _SERVICE_ROOT + url[len(_WEB_ROOT) :]
    return url


class LaunchpadClient:
    """Launchpad client backed by launchpadlib."""

    def __init__(self, credentials_file: str | None = None) -> None:
        self._lp = Launchpad.login_with(
            "maas-code-reviewer",
            "production",
            credentials_file=credentials_file,
            version="devel",
        )

    def get_merge_proposal(self, mp_url: str) -> MergeProposal:
        api_url = web_url_to_api_url(mp_url)
        lp_mp = self._lp.load(api_url)
        return _to_merge_proposal(lp_mp)

    def get_merge_proposals(self, project: str, status: str) -> list[MergeProposal]:
        lp_project = self._lp.load(_SERVICE_ROOT + project)
        lp_proposals = lp_project.getMergeProposals(status=status)
        return [_to_merge_proposal(lp_mp) for lp_mp in lp_proposals]

    def get_comments(self, mp: MergeProposal) -> list[Comment]:
        return [_to_comment(lp_comment) for lp_comment in mp._lp_object.all_comments]

    def post_comment(self, mp: MergeProposal, content: str, subject: str) -> None:
        mp._lp_object.createComment(subject=subject, content=content)

    def get_bot_username(self) -> str:
        return self._lp.me.name


def _get_git_unique_name(repo_link: str) -> str:
    """Extract git repository unique name from a self_link URL.

    Given a URL like https://api.launchpad.net/devel/~user/project/+git/repo,
    returns ~user/project/+git/repo.
    """
    assert repo_link.startswith(_SERVICE_ROOT), (
        f"Expected repo_link to start with {_SERVICE_ROOT}, got {repo_link}"
    )
    return repo_link[len(_SERVICE_ROOT) :]


def _get_person_name_from_link(person_link: str) -> str:
    """Extract person name from a person_link URL.

    Given a URL like https://api.launchpad.net/devel/~maas-lander,
    returns maas-lander.
    """
    assert person_link.startswith(_SERVICE_ROOT), (
        f"Expected person_link to start with {_SERVICE_ROOT}, got {person_link}"
    )
    name = person_link[len(_SERVICE_ROOT) :]
    assert name.startswith("~"), f"Expected name to start with ~, got {name}"
    return name[1:]


def _to_merge_proposal(lp_mp: Any) -> MergeProposal:
    return MergeProposal(
        url=lp_mp.web_link,
        api_url=lp_mp.self_link,
        source_git_repository=_get_git_unique_name(lp_mp.source_git_repository_link),
        source_git_path=lp_mp.source_git_path,
        target_git_repository=_get_git_unique_name(lp_mp.target_git_repository_link),
        target_git_path=lp_mp.target_git_path,
        status=lp_mp.queue_status,
        commit_message=lp_mp.commit_message or None,
        description=lp_mp.description or None,
        _lp_object=lp_mp,
    )


def _to_comment(lp_comment: Any) -> Comment:
    date_created: datetime = lp_comment.date_created
    return Comment(
        author=_get_person_name_from_link(lp_comment.author_link),
        body=lp_comment.message_body,
        date=date_created,
    )
