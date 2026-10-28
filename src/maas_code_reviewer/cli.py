# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import NoReturn

from maas_code_reviewer import review_mcp
from maas_code_reviewer.acp_runner import ConfiguredAgentRunner
from maas_code_reviewer.agent_runner import AgentRunnerError
from maas_code_reviewer.git import GitClient
from maas_code_reviewer.github_client import GitHubClient, parse_pr_url
from maas_code_reviewer.launchpad_client import LaunchpadClient
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
)
from maas_code_reviewer.review_schema import repository_files
from maas_code_reviewer.reviewer import (
    REVIEW_STAGING_RELATIVE_PATH,
    render_review_document,
    review_agent,
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


def handle_review(args: argparse.Namespace) -> None:
    """Handle the review subcommand."""
    if args.diff_file == "-":
        diff = sys.stdin.read()
        diff_file = None
    else:
        diff = Path(args.diff_file).read_text()
        diff_file = Path(args.diff_file).absolute()

    if args.description_file is None:
        description = None
    else:
        description = Path(args.description_file).read_text(encoding="utf-8")

    repo_dir = (Path(args.repo_dir) if args.repo_dir else Path.cwd()).resolve()

    runner = ConfiguredAgentRunner(
        Path(args.runtime_config) if args.runtime_config else None,
        model=args.model,
        timeout_seconds=args.review_timeout,
        otel_output=Path(args.otel_output) if args.otel_output else None,
    )
    metrics = ReviewMetrics()
    agents_md = read_agents_context(repo_dir)

    try:
        document = review_agent(
            runner,
            diff,
            description,
            repo_dir,
            diff_file=diff_file,
            agents_md=agents_md,
            metrics=metrics,
            model=args.model,
            diff_prefix=args.diff_prefix,
            repair_attempts=args.repair_attempts,
        )
    except ValueError as exc:
        raise InvalidReviewError([str(exc)]) from exc

    if args.json_output:
        Path(args.json_output).write_text(json.dumps(document, indent=2))
    else:
        print(render_review_document(document))

    if args.metrics is not None:
        write_metrics(metrics, Path(args.metrics))


def handle_review_mcp(args: argparse.Namespace) -> None:
    """Serve the in-session review validation tool over MCP stdio."""
    diff = Path(args.diff_file).read_text(encoding="utf-8")
    repo_dir = (Path(args.repo_dir) if args.repo_dir else Path.cwd()).resolve()
    review_mcp.serve(
        diff,
        path_prefix=args.diff_prefix,
        repository_files=(repository_files(repo_dir) - {REVIEW_STAGING_RELATIVE_PATH}),
        reader=sys.stdin,
        writer=sys.stdout,
    )


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
        elif args.command == "fetch-pr":
            handle_fetch_pr(args)
        elif args.command == "publish-pr":
            handle_publish_pr(args)
        elif args.command == "review":
            handle_review(args)
        elif args.command == "review-mcp":
            handle_review_mcp(args)
    except (InvalidReviewError, AgentRunnerError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        for note in getattr(exc, "__notes__", ()):
            print(note, file=sys.stderr)
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


def _positive_timeout(value: str) -> float:
    """Parse a finite positive --review-timeout value (argparse type)."""
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "review timeout must be a finite positive number of seconds"
        ) from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError(
            "review timeout must be a finite positive number of seconds"
        )
    return seconds


def _repair_attempt_count(value: str) -> int:
    """Parse a non-negative --repair-attempts value (argparse type)."""
    try:
        count = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "repair attempts must be a non-negative integer"
        ) from None
    if count < 0:
        raise argparse.ArgumentTypeError(
            "repair attempts must be a non-negative integer"
        )
    return count


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

    review_pipeline_parser = subparsers.add_parser(
        "review",
        help="Review a unified diff and produce the structured review document.",
    )
    review_pipeline_parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Exact model identity to review with (default: the runtime profile).",
    )
    review_pipeline_parser.add_argument(
        "--runtime-config",
        type=str,
        default=None,
        metavar="FILE",
        help=(
            "Explicit trusted TOML runtime configuration overriding "
            "individual built-in review runtime defaults."
        ),
    )
    review_pipeline_parser.add_argument(
        "--review-timeout",
        type=_positive_timeout,
        default=None,
        metavar="SECONDS",
        help=(
            "Finite positive deadline in seconds for startup and the "
            "agent turn of each attempt, including repair rounds "
            "(default: the runtime profile; cleanup has a separate "
            "bounded grace period)."
        ),
    )
    review_pipeline_parser.add_argument(
        "--repair-attempts",
        type=_repair_attempt_count,
        default=1,
        metavar="N",
        help=(
            "Repair rounds for an invalid review document: when the "
            "agent's terminal JSON fails host validation, re-run it up to "
            "N more times with the rejected output and the failure list "
            "as data (default: 1; 0 disables repair)."
        ),
    )
    review_pipeline_parser.add_argument(
        "--otel-output",
        type=str,
        default=None,
        metavar="DIR",
        help="Collect omp OpenTelemetry diagnostics as OTLP JSONL files in DIR.",
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
            "directory). The agent inspects it with its native read, grep, "
            "and glob tools, its AGENTS.md (if present) is included as "
            "untrusted review conventions, and validation resolves diff "
            "paths against this checkout."
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

    mcp_parser = subparsers.add_parser(
        "review-mcp",
        help="Serve the in-session review validation tool over MCP stdio.",
    )
    mcp_parser.add_argument(
        "--diff-file",
        type=str,
        required=True,
        metavar="FILE",
        help="Unified diff the review documents are validated against.",
    )
    mcp_parser.add_argument(
        "--diff-prefix",
        type=str,
        default=None,
        metavar="PREFIX",
        help="Explicit new-side diff path prefix used to resolve file paths.",
    )
    mcp_parser.add_argument(
        "--repo-dir",
        type=str,
        default=None,
        help=(
            "Path to the local git repository whose file listing validates "
            "review paths (default: current working directory)."
        ),
    )

    return parser
