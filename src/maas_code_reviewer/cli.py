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
    InvalidReviewError,
    fetch_merge_proposal,
    fetch_pull_request,
    format_merge_proposals,
    list_merge_proposals,
    load_validated_review,
    post_pull_request_review,
    read_agents_context,
    render_merge_proposal_comment,
    render_pull_request_review,
    review_merge_proposal,
    review_pull_request,
)
from maas_code_reviewer.review_schema import repository_files
from maas_code_reviewer.reviewer import (
    NoReviewText,
    render_review_document,
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


def handle_fetch_mp(args: argparse.Namespace) -> None:
    """Handle the fetch-mp subcommand."""
    lp_client = LaunchpadClient(credentials_file=args.launchpad_credentials)
    git_client = GitClient()
    fetched = fetch_merge_proposal(
        lp_client,
        git_client,
        args.mp_url,
        Path(args.output_dir),
        git_token=os.environ.get("LP_GIT_TOKEN", "").strip() or None,
    )
    if not fetched:
        print("Already reviewed, skipping.")


def handle_publish_mp(args: argparse.Namespace) -> None:
    """Handle the publish-mp subcommand."""
    review = load_validated_review(Path(args.review), Path(args.input_dir))
    comment = render_merge_proposal_comment(review)
    if args.dry_run:
        print(comment)
        return
    lp_client = LaunchpadClient(credentials_file=args.launchpad_credentials)
    mp = lp_client.get_merge_proposal(args.mp_url)
    lp_client.post_comment(mp, comment, subject="Automated review")


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
            repository_files=repository_files(repo_dir),
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


def handle_fetch_pr(args: argparse.Namespace) -> None:
    """Handle the fetch-pr subcommand."""
    token = _get_github_token(args)
    if token is None:
        _exit_missing_github_token()
    github_client = GitHubClient(token)
    fetch_pull_request(github_client, args.pr_url, Path(args.output_dir))


def handle_publish_pr(args: argparse.Namespace) -> None:
    """Handle the publish-pr subcommand."""
    review = load_validated_review(Path(args.review), Path(args.input_dir))
    review = render_pull_request_review(review)
    if args.dry_run:
        print(json.dumps(review, indent=2))
        return
    owner, repo, pr_number = parse_pr_url(args.pr_url)
    token = _get_github_token(args)
    if token is None:
        _exit_missing_github_token()
    github_client = GitHubClient(token)
    post_pull_request_review(github_client, owner, repo, pr_number, review)


def handle_review_pr(args: argparse.Namespace) -> None:
    """Handle the review-pr subcommand."""
    owner, repo, pr_number = parse_pr_url(args.pr_url)

    token = _get_github_token(args)
    if token is None:
        _exit_missing_github_token()

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


def handle_review(args: argparse.Namespace) -> None:
    """Handle the review subcommand."""
    api_key = _get_gemini_api_key(args)
    if api_key is None:
        _exit_missing_gemini_api_key()

    if args.diff_file == "-":
        diff = sys.stdin.read()
    else:
        diff = Path(args.diff_file).read_text()

    if args.description_file is None:
        description = None
    else:
        description = Path(args.description_file).read_text(encoding="utf-8")

    repo_dir = Path(args.repo_dir) if args.repo_dir else Path.cwd()

    llm_client = GeminiClient(api_key=api_key, model=args.model)

    tools, agents_md = read_agents_context(repo_dir)
    metrics = ReviewMetrics()

    try:
        document = review_diff_structured(
            llm_client,
            diff=diff,
            description=description,
            read_file=tools.read_file,
            list_directory=tools.list_directory,
            agents_md=agents_md,
            metrics=metrics,
            diff_prefix=args.diff_prefix,
            repository_files=repository_files(repo_dir),
        )
    except ValueError as exc:
        raise InvalidReviewError([str(exc)]) from exc

    if args.json_output:
        Path(args.json_output).write_text(json.dumps(document, indent=2))
    else:
        print(render_review_document(document))

    metrics.files_read = tools.files_read_count
    metrics.agents_md_read = tools.agents_md_read
    if args.metrics is not None:
        write_metrics(metrics, Path(args.metrics))


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        sys.exit(1)
    try:
        if args.command == "list-lp-mps":
            handle_list_lp_mps(args)
        elif args.command == "fetch-mp":
            handle_fetch_mp(args)
        elif args.command == "publish-mp":
            handle_publish_mp(args)
        elif args.command == "review-mp":
            handle_review_mp(args)
        elif args.command == "review-diff":
            handle_review_diff(args)
        elif args.command == "fetch-pr":
            handle_fetch_pr(args)
        elif args.command == "publish-pr":
            handle_publish_pr(args)
        elif args.command == "review-pr":
            handle_review_pr(args)
        elif args.command == "review":
            handle_review(args)
    except (NoReviewText, InvalidReviewError) as exc:
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


def _get_github_token(args: argparse.Namespace) -> str | None:
    """Return the GitHub token from --github-token or the environment."""
    token = args.github_token or os.environ.get("GITHUB_TOKEN")
    return token or None


def _exit_missing_github_token() -> NoReturn:
    """Print an error message about a missing GitHub token and exit."""
    print(
        "Error: GitHub token not provided. Use --github-token or set the "
        "GITHUB_TOKEN environment variable.",
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

    fetch_mp_parser = subparsers.add_parser(
        "fetch-mp",
        help="Fetch a merge proposal's review inputs into a directory.",
    )
    fetch_mp_parser.add_argument(
        "--launchpad-credentials",
        type=str,
        default=None,
        help="Path to Launchpad credentials file.",
    )
    fetch_mp_parser.add_argument(
        "--output-dir",
        type=str,
        required=True,
        metavar="DIR",
        help=(
            "Directory to write diff.patch, description.md, metadata.json, "
            "and the merged repository tree (repo/) into."
        ),
    )
    fetch_mp_parser.add_argument(
        "mp_url",
        help="URL of the merge proposal to fetch.",
    )

    publish_mp_parser = subparsers.add_parser(
        "publish-mp",
        help="Validate a candidate review and post it to a merge proposal.",
    )
    publish_mp_parser.add_argument(
        "--launchpad-credentials",
        type=str,
        default=None,
        help="Path to Launchpad credentials file.",
    )
    publish_mp_parser.add_argument(
        "--review",
        type=str,
        required=True,
        metavar="FILE",
        help="Path to the candidate review JSON document to publish.",
    )
    publish_mp_parser.add_argument(
        "--input-dir",
        type=str,
        required=True,
        metavar="DIR",
        help=(
            "Directory holding the fetch-mp output (diff.patch and "
            "metadata.json) to validate against."
        ),
    )
    publish_mp_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print the Launchpad comment to stdout instead of posting it.",
    )
    publish_mp_parser.add_argument(
        "mp_url",
        help="URL of the merge proposal to publish the review to.",
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

    fetch_pr_parser = subparsers.add_parser(
        "fetch-pr",
        help="Fetch a pull request's review inputs into a directory.",
    )
    fetch_pr_parser.add_argument(
        "--github-token",
        type=str,
        default=None,
        help=(
            "GitHub personal access token. If not provided, read from the "
            "GITHUB_TOKEN environment variable."
        ),
    )
    fetch_pr_parser.add_argument(
        "--output-dir",
        type=str,
        required=True,
        metavar="DIR",
        help=(
            "Directory to write diff.patch, description.md, and "
            "metadata.json into."
        ),
    )
    fetch_pr_parser.add_argument(
        "pr_url",
        help="Full GitHub PR URL, e.g. https://github.com/owner/repo/pull/42.",
    )

    publish_pr_parser = subparsers.add_parser(
        "publish-pr",
        help="Validate a candidate review and post it to a pull request.",
    )
    publish_pr_parser.add_argument(
        "--github-token",
        type=str,
        default=None,
        help=(
            "GitHub personal access token. If not provided, read from the "
            "GITHUB_TOKEN environment variable."
        ),
    )
    publish_pr_parser.add_argument(
        "--review",
        type=str,
        required=True,
        metavar="FILE",
        help="Path to the candidate review JSON document to publish.",
    )
    publish_pr_parser.add_argument(
        "--input-dir",
        type=str,
        required=True,
        metavar="DIR",
        help=(
            "Directory holding the fetch-pr output (diff.patch and "
            "metadata.json) to validate against."
        ),
    )
    publish_pr_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print the review JSON to stdout instead of posting it.",
    )
    publish_pr_parser.add_argument(
        "pr_url",
        help="Full GitHub PR URL to publish the review to.",
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

    review_pipeline_parser = subparsers.add_parser(
        "review",
        help="Review a unified diff and produce the structured review document.",
    )
    review_pipeline_parser.add_argument(
        "-g",
        "--gemini-api-key-file",
        type=str,
        default=None,
        help=(
            "Path to file containing the Gemini API key. If not provided, "
            "read from the GEMINI_API_KEY environment variable."
        ),
    )
    review_pipeline_parser.add_argument(
        "--model",
        type=str,
        default="gemini-3-flash-preview",
        help="Gemini model to use (default: 'gemini-3-flash-preview').",
    )
    review_pipeline_parser.add_argument(
        "--diff-file",
        type=str,
        required=True,
        metavar="FILE",
        help="Path to a unified diff file, or '-' to read from stdin.",
    )
    review_pipeline_parser.add_argument(
        "--description-file",
        type=str,
        default=None,
        metavar="FILE",
        help=(
            "Path to a UTF-8 file with the change description (e.g. the "
            "merge proposal or pull request description)."
        ),
    )
    review_pipeline_parser.add_argument(
        "--diff-prefix",
        type=str,
        default=None,
        metavar="PREFIX",
        help=(
            "Explicit new-side path prefix in the diff (e.g. 'w/'). Without "
            "it, the prefix is inferred from the diff and the --repo-dir "
            "listing; pass an empty string for prefix-free paths."
        ),
    )
    review_pipeline_parser.add_argument(
        "--repo-dir",
        type=str,
        default=None,
        help=(
            "Path to the local git repository (default: current working "
            "directory). Used for read_file and list_directory tool calls, "
            "to proactively include the repository's AGENTS.md (if present) "
            "in the review context, and to resolve diff paths during "
            "validation."
        ),
    )
    review_pipeline_parser.add_argument(
        "--json-output",
        type=str,
        default=None,
        metavar="FILE",
        help=(
            "Write the raw structured JSON review document to FILE instead "
            "of printing human-readable text to stdout. The document is "
            "undecorated: publish-mp/publish-pr add the review marker when "
            "posting."
        ),
    )
    review_pipeline_parser.add_argument(
        "--metrics",
        type=str,
        default=None,
        metavar="FILE",
        help="Write review metrics (model, tokens, diff stats) as JSON to FILE.",
    )

    return parser
