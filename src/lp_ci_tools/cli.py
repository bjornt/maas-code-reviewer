from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime

from lp_ci_tools.launchpad_client import LaunchpadClient
from lp_ci_tools.models import Comment

REVIEW_MARKER = "[lp-ci-tools review]"


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
        comments = client.get_comments(mp.url)
        last_reviewed = _find_last_review_date(comments, bot_username)
        summaries.append(
            MergeProposalSummary(
                url=mp.url,
                status=mp.status,
                last_reviewed=last_reviewed,
            )
        )
    return summaries


def _find_last_review_date(
    comments: list[Comment], bot_username: str
) -> datetime | None:
    """Find the timestamp of the most recent review comment by the bot."""
    review_dates = [
        c.date
        for c in comments
        if c.author == bot_username and c.body.startswith(REVIEW_MARKER)
    ]
    if not review_dates:
        return None
    return max(review_dates)


def format_merge_proposals(summaries: list[MergeProposalSummary]) -> str:
    """Format summaries as human-readable text, one line per proposal."""
    lines = []
    for s in summaries:
        reviewed = s.last_reviewed.isoformat() if s.last_reviewed else "never"
        lines.append(f"{s.url} {s.status} {reviewed}")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lp-ci-tools")
    subparsers = parser.add_subparsers(dest="command")

    list_parser = subparsers.add_parser(
        "list-merge-proposals",
        help="List merge proposals for a project.",
    )
    list_parser.add_argument(
        "--credentials",
        type=str,
        default=None,
        help="Path to Launchpad credentials file.",
    )
    list_parser.add_argument(
        "--status",
        required=True,
        help="Filter merge proposals by status (e.g. 'Needs review').",
    )
    list_parser.add_argument(
        "project",
        help="Launchpad project name.",
    )

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "list-merge-proposals":
        # Real LaunchpadClient wiring will be added in a later chunk.
        raise NotImplementedError("Real LaunchpadClient is not yet available")
