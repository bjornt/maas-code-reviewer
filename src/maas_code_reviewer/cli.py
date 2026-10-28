# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import argparse
import json
import os
import sys
from pathlib import Path
from typing import NoReturn

from maas_code_reviewer.git import GitClient
from maas_code_reviewer.github_client import GitHubClient, parse_pr_url
from maas_code_reviewer.launchpad_client import LaunchpadClient
from maas_code_reviewer.llm_client import DEFAULT_MAX_TOOL_CALLS, GeminiClient
from maas_code_reviewer.metrics import ReviewMetrics, write_metrics
from maas_code_reviewer.review_commands import (
    format_merge_proposals,
    list_merge_proposals,
    post_pull_request_review,
    read_agents_context,
    review_merge_proposal,
    review_pull_request,
)
from maas_code_reviewer.reviewer import (
    NoReviewText,
    review_diff,
    review_diff_structured,
)

_MAX_TOOL_CALLS_HELP = (
    "Maximum number of automatic tool calls the LLM may make during a "
    "review (file reads, directory listings, searches). When the limit is "
    "reached the reviewer resumes the chat to elicit a text answer. "
    f"(default: {DEFAULT_MAX_TOOL_CALLS})."
)


def handle_list_lp_mps(args: argparse.Namespace) -> None:
    """Handle the list-lp-mps subcommand."""
    client = LaunchpadClient(credentials_file=args.launchpad_credentials)
    summaries = list_merge_proposals(client, args.project, args.status)
    output = format_merge_proposals(summaries)
    if output:
        print(output)


def handle_review_mp(args: argparse.Namespace) -> None:
    """Handle the review-mp subcommand."""
    api_key = _get_gemini_api_key(args)
    if api_key is None:
        _exit_missing_gemini_api_key()
    lp_client = LaunchpadClient(credentials_file=args.launchpad_credentials)
    git_client = GitClient()
    llm_client = GeminiClient(api_key=api_key, model=args.model)
    metrics = ReviewMetrics()
    result = review_merge_proposal(
        lp_client,
        git_client,
        llm_client,
        args.mp_url,
        max_diff_chars=args.max_diff_chars,
        metrics=metrics,
        max_tool_calls=args.max_tool_calls,
    )
    if args.metrics is not None and result is not None:
        write_metrics(metrics, Path(args.metrics))
    if result is None:
        print("Already reviewed, skipping.")
    elif args.dry_run:
        print(result)
    else:
        mp = lp_client.get_merge_proposal(args.mp_url)
        lp_client.post_comment(mp, result, subject="Automated review")


def handle_review_diff(args: argparse.Namespace) -> None:
    """Handle the review-diff subcommand."""
    api_key = _get_gemini_api_key(args)
    if api_key is None:
        _exit_missing_gemini_api_key()

    if args.diff_file == "-":
        diff = sys.stdin.read()
    else:
        diff = Path(args.diff_file).read_text()

    repo_dir = Path(args.repo_dir) if args.repo_dir else Path.cwd()

    llm_client = GeminiClient(api_key=api_key, model=args.model)

    tools, agents_md = read_agents_context(repo_dir)
    metrics = ReviewMetrics()

    if args.json_output:
        result_dict = review_diff_structured(
            llm_client,
            diff=diff,
            description=None,
            read_file=tools.read_file,
            list_directory=tools.list_directory,
            agents_md=agents_md,
            max_diff_chars=args.max_diff_chars,
            metrics=metrics,
            max_tool_calls=args.max_tool_calls,
        )
        Path(args.json_output).write_text(json.dumps(result_dict, indent=2))
    else:
        result = review_diff(
            llm_client,
            diff=diff,
            description=None,
            read_file=tools.read_file,
            list_directory=tools.list_directory,
            agents_md=agents_md,
            max_diff_chars=args.max_diff_chars,
            metrics=metrics,
            max_tool_calls=args.max_tool_calls,
        )
        print(result)

    metrics.files_read = tools.files_read_count
    metrics.agents_md_read = tools.agents_md_read
    if args.metrics is not None:
        write_metrics(metrics, Path(args.metrics))


def handle_review_pr(args: argparse.Namespace) -> None:
    """Handle the review-pr subcommand."""
    owner, repo, pr_number = parse_pr_url(args.pr_url)

    token = args.github_token or os.environ.get("GITHUB_TOKEN")
    if not token:
        print(
            "Error: GitHub token not provided. Use --github-token or set the "
            "GITHUB_TOKEN environment variable.",
            file=sys.stderr,
        )
        sys.exit(1)

    api_key = _get_gemini_api_key(args)
    if api_key is None:
        _exit_missing_gemini_api_key()

    github_client = GitHubClient(token)
    repo_dir = Path(args.repo_dir) if args.repo_dir else Path.cwd()
    llm_client = GeminiClient(api_key=api_key, model=args.model)
    metrics = ReviewMetrics()
    result_dict = review_pull_request(
        github_client,
        llm_client,
        owner,
        repo,
        pr_number,
        repo_dir,
        max_diff_chars=args.max_diff_chars,
        metrics=metrics,
        max_tool_calls=args.max_tool_calls,
    )
    if args.metrics is not None:
        write_metrics(metrics, Path(args.metrics))

    if args.dry_run:
        print(json.dumps(result_dict, indent=2))
        return

    post_pull_request_review(github_client, owner, repo, pr_number, result_dict)


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        sys.exit(1)
    try:
        if args.command == "list-lp-mps":
            handle_list_lp_mps(args)
        elif args.command == "review-mp":
            handle_review_mp(args)
        elif args.command == "review-diff":
            handle_review_diff(args)
        elif args.command == "review-pr":
            handle_review_pr(args)
    except NoReviewText as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


def _get_gemini_api_key(args: argparse.Namespace) -> str | None:
    """Return the Gemini API key from --gemini-api-key-file or the environment.

    Returns ``None`` if neither source provides a non-empty key.
    """
    if args.gemini_api_key_file:
        path = Path(args.gemini_api_key_file)
        if not path.is_file():
            return None
        return path.read_text().strip() or None
    return os.environ.get("GEMINI_API_KEY", "").strip() or None


def _exit_missing_gemini_api_key() -> NoReturn:
    """Print an error message about a missing Gemini API key and exit."""
    print(
        "Error: No Gemini API key configured. Use --gemini-api-key-file or set "
        "the GEMINI_API_KEY environment variable.",
        file=sys.stderr,
    )
    sys.exit(1)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="maas-code-reviewer")
    subparsers = parser.add_subparsers(dest="command")

    list_parser = subparsers.add_parser(
        "list-lp-mps",
        help="List merge proposals for a project.",
    )
    list_parser.add_argument(
        "--launchpad-credentials",
        type=str,
        default=None,
        help="Path to Launchpad credentials file.",
    )
    list_parser.add_argument(
        "--status",
        default="Needs review",
        help="Filter merge proposals by status (default: 'Needs review').",
    )
    list_parser.add_argument(
        "project",
        help="Launchpad project name.",
    )

    review_parser = subparsers.add_parser(
        "review-mp",
        help="Review a single merge proposal.",
    )
    review_parser.add_argument(
        "--launchpad-credentials",
        type=str,
        default=None,
        help="Path to Launchpad credentials file.",
    )
    review_parser.add_argument(
        "-g",
        "--gemini-api-key-file",
        type=str,
        default=None,
        help=(
            "Path to file containing the Gemini API key. If not provided, "
            "read from the GEMINI_API_KEY environment variable."
        ),
    )
    review_parser.add_argument(
        "--model",
        type=str,
        default="gemini-3-flash-preview",
        help="Gemini model to use (default: 'gemini-3-flash-preview').",
    )
    review_parser.add_argument(
        "--max-diff-chars",
        type=int,
        default=200_000,
        metavar="N",
        help=(
            "Maximum diff size in characters before truncation "
            "(default: 200000)."
        ),
    )
    review_parser.add_argument(
        "--max-tool-calls",
        type=int,
        default=DEFAULT_MAX_TOOL_CALLS,
        metavar="N",
        help=_MAX_TOOL_CALLS_HELP,
    )
    review_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print review to stdout instead of posting as a comment.",
    )
    review_parser.add_argument(
        "--metrics",
        type=str,
        default=None,
        metavar="FILE",
        help="Write review metrics (model, tokens, diff stats) as JSON to FILE.",
    )
    review_parser.add_argument(
        "mp_url",
        help="URL of the merge proposal to review.",
    )

    diff_parser = subparsers.add_parser(
        "review-diff",
        help="Review a unified diff file and print the result to stdout.",
    )
    diff_parser.add_argument(
        "-g",
        "--gemini-api-key-file",
        type=str,
        default=None,
        help=(
            "Path to file containing the Gemini API key. If not provided, "
            "read from the GEMINI_API_KEY environment variable."
        ),
    )
    diff_parser.add_argument(
        "--model",
        type=str,
        default="gemini-3-flash-preview",
        help="Gemini model to use (default: 'gemini-3-flash-preview').",
    )
    diff_parser.add_argument(
        "--max-diff-chars",
        type=int,
        default=200_000,
        metavar="N",
        help=(
            "Maximum diff size in characters before truncation "
            "(default: 200000)."
        ),
    )
    diff_parser.add_argument(
        "--max-tool-calls",
        type=int,
        default=DEFAULT_MAX_TOOL_CALLS,
        metavar="N",
        help=_MAX_TOOL_CALLS_HELP,
    )
    diff_parser.add_argument(
        "--repo-dir",
        type=str,
        default=None,
        help=(
            "Path to the local git repository (default: current working directory). "
            "Used for read_file and list_directory tool calls, and to proactively "
            "include the repository's AGENTS.md (if present) in the review context."
        ),
    )
    diff_parser.add_argument(
        "--json-output",
        type=str,
        default=None,
        metavar="FILE",
        help=(
            "Write structured JSON review output to FILE instead of plain text to "
            "stdout. The JSON contains a 'general_comment' and 'inline_comments' "
            "keyed by file path and line number."
        ),
    )
    diff_parser.add_argument(
        "--metrics",
        type=str,
        default=None,
        metavar="FILE",
        help="Write review metrics (model, tokens, diff stats) as JSON to FILE.",
    )
    diff_parser.add_argument(
        "diff_file",
        help="Path to a unified diff file, or '-' to read from stdin.",
    )

    pr_parser = subparsers.add_parser(
        "review-pr",
        help="Review a GitHub pull request and post the review.",
    )
    pr_parser.add_argument(
        "-g",
        "--gemini-api-key-file",
        type=str,
        default=None,
        help=(
            "Path to file containing the Gemini API key. If not provided, "
            "read from the GEMINI_API_KEY environment variable."
        ),
    )
    pr_parser.add_argument(
        "--github-token",
        type=str,
        default=None,
        help=(
            "GitHub personal access token. If not provided, read from the "
            "GITHUB_TOKEN environment variable."
        ),
    )
    pr_parser.add_argument(
        "--model",
        type=str,
        default="gemini-3-flash-preview",
        help="Gemini model to use (default: 'gemini-3-flash-preview').",
    )
    pr_parser.add_argument(
        "--max-diff-chars",
        type=int,
        default=200_000,
        metavar="N",
        help=(
            "Maximum diff size in characters before truncation "
            "(default: 200000)."
        ),
    )
    pr_parser.add_argument(
        "--max-tool-calls",
        type=int,
        default=DEFAULT_MAX_TOOL_CALLS,
        metavar="N",
        help=_MAX_TOOL_CALLS_HELP,
    )
    pr_parser.add_argument(
        "--repo-dir",
        type=str,
        default=None,
        help=(
            "Path to a local checkout of the repository (default: current working "
            "directory). Used for read_file and list_directory tool calls, and to "
            "proactively include the repository's AGENTS.md (if present) in the "
            "review context. The caller is responsible for having the repo checked "
            "out already."
        ),
    )
    pr_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print the review JSON to stdout instead of posting it.",
    )
    pr_parser.add_argument(
        "--metrics",
        type=str,
        default=None,
        metavar="FILE",
        help="Write review metrics (model, tokens, diff stats) as JSON to FILE.",
    )
    pr_parser.add_argument(
        "pr_url",
        help="Full GitHub PR URL, e.g. https://github.com/owner/repo/pull/42.",
    )

    return parser
