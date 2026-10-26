# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Parse command options and wire host acquisition, review, and publication."""

import argparse
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from urllib.error import URLError

from github import GithubException
from lazr.restfulclient.errors import HTTPError

from maas_code_reviewer import review_commands
from maas_code_reviewer.agent_runner import AgentRunnerError
from maas_code_reviewer.git import GitClient
from maas_code_reviewer.github_client import GitHubClient, parse_pr_url
from maas_code_reviewer.launchpad_client import LaunchpadClient
from maas_code_reviewer.metrics import ReviewMetrics, write_metrics
from maas_code_reviewer.reviewer import review_diff, review_diff_structured
from maas_code_reviewer.workshop_runner import ConfiguredAgentRunner


def handle_list_lp_mps(args: argparse.Namespace) -> None:
    client = LaunchpadClient(credentials_file=args.launchpad_credentials)
    summaries = review_commands.list_merge_proposals(client, args.project, args.status)
    output = review_commands.format_merge_proposals(summaries)
    if output:
        print(output)


def handle_review_mp(args: argparse.Namespace) -> None:
    lp_client = LaunchpadClient(credentials_file=args.launchpad_credentials)
    runner = _configured_runner(args)
    metrics = ReviewMetrics()
    result = review_commands.review_merge_proposal(
        lp_client,
        GitClient(),
        runner,
        args.mp_url,
        max_diff_chars=args.max_diff_chars,
        metrics=metrics,
        model=args.model,
        timeout_seconds=args.review_timeout or 300,
        repair_attempts=args.repair_attempts,
    )
    if result is None:
        print("Already reviewed, skipping.")
        return
    if args.metrics is not None:
        write_metrics(metrics, args.metrics)
    if args.dry_run:
        print(result)
    else:
        mp = lp_client.get_merge_proposal(args.mp_url)
        lp_client.post_comment(mp, result, subject="Automated review")


def handle_review_diff(args: argparse.Namespace) -> None:
    diff = (
        sys.stdin.read() if args.diff_file == "-" else Path(args.diff_file).read_text()
    )
    description = (
        args.description_file.read_text(encoding="utf-8")
        if args.description_file is not None
        else None
    )
    repo_dir = args.repo_dir or Path.cwd()
    runner = _configured_runner(args)
    metrics = ReviewMetrics()
    agents_md = (
        review_commands.read_agents_context(repo_dir, metrics) if diff.strip() else None
    )
    if args.json_output is not None:
        result_dict = review_diff_structured(
            runner,
            diff,
            description,
            repo_dir,
            agents_md=agents_md,
            max_diff_chars=args.max_diff_chars,
            metrics=metrics,
            model=args.model,
            timeout_seconds=args.review_timeout or 300,
            diff_prefix=args.diff_prefix,
            repair_attempts=args.repair_attempts,
        )
        args.json_output.write_text(json.dumps(result_dict, indent=2))
    else:
        result = review_diff(
            runner,
            diff,
            description,
            repo_dir,
            agents_md=agents_md,
            max_diff_chars=args.max_diff_chars,
            metrics=metrics,
            model=args.model,
            timeout_seconds=args.review_timeout or 300,
            diff_prefix=args.diff_prefix,
            repair_attempts=args.repair_attempts,
        )
        print(result)
    if args.metrics is not None:
        write_metrics(metrics, args.metrics)


def handle_review_pr(args: argparse.Namespace) -> None:
    owner, repo, pr_number = parse_pr_url(args.pr_url)
    token = args.github_token or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ValueError(
            "GitHub token not provided. Use --github-token or set the "
            "GITHUB_TOKEN environment variable."
        )
    github_client = GitHubClient(token)
    metrics = ReviewMetrics()
    result = review_commands.review_pull_request(
        github_client,
        _configured_runner(args),
        owner,
        repo,
        pr_number,
        args.repo_dir or Path.cwd(),
        max_diff_chars=args.max_diff_chars,
        metrics=metrics,
        model=args.model,
        timeout_seconds=args.review_timeout or 300,
        repair_attempts=args.repair_attempts,
    )
    if args.metrics is not None:
        write_metrics(metrics, args.metrics)
    if args.dry_run:
        print(json.dumps(result, indent=2))
    else:
        review_commands.post_pull_request_review(
            github_client, owner, repo, pr_number, result
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
        elif args.command == "review-mp":
            handle_review_mp(args)
        elif args.command == "review-diff":
            handle_review_diff(args)
        elif args.command == "review-pr":
            handle_review_pr(args)
    except KeyboardInterrupt:
        print("Review cancelled.", file=sys.stderr)
        sys.exit(130)
    except (
        AgentRunnerError,
        GithubException,
        HTTPError,
        OSError,
        URLError,
        ValueError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        for note in getattr(exc, "__notes__", ()):
            print(note, file=sys.stderr)
        sys.exit(1)


def _configured_runner(args: argparse.Namespace) -> ConfiguredAgentRunner:
    return ConfiguredAgentRunner(
        args.runtime_config,
        model=args.model,
        timeout_seconds=args.review_timeout,
        otel_output=args.otel_output,
    )


def _positive_timeout(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "review timeout must be a finite positive number"
        )
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError(
            "review timeout must be a finite positive number"
        )
    return seconds


def _add_review_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--runtime-config",
        type=Path,
        metavar="FILE",
        help=(
            "Explicit trusted runtime configuration overriding individual "
            "built-in review runtime defaults."
        ),
    )
    parser.add_argument(
        "--model",
        help="Exact model identity to review with (default: runtime policy).",
    )
    parser.add_argument(
        "--review-timeout",
        type=_positive_timeout,
        metavar="SECONDS",
        help=(
            "Finite positive deadline for startup and agent work "
            "(default: runtime profile; cleanup has a separate bounded grace period)."
        ),
    )
    parser.add_argument(
        "--max-diff-chars",
        type=int,
        default=200_000,
        metavar="N",
        help="Maximum initial diff context in characters (default: 200000).",
    )
    parser.add_argument(
        "--repair-attempts",
        type=int,
        default=1,
        metavar="N",
        help="Repair rounds for an invalid review (default: 1; 0 disables repair).",
    )
    parser.add_argument(
        "--metrics",
        type=Path,
        metavar="FILE",
        help="Write reported agent usage and host diff statistics as JSON to FILE.",
    )
    parser.add_argument(
        "--otel-output",
        type=Path,
        metavar="DIR",
        help="Collect omp OpenTelemetry traces and metrics as OTLP JSONL files in DIR.",
    )


def _add_repo_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repo-dir",
        type=Path,
        help=(
            "Local checkout to snapshot and inspect (default: current directory). "
            "The caller must prepare the checkout for the reviewed revision. "
            "AGENTS.md is included as untrusted review context when present."
        ),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="maas-code-reviewer")
    subparsers = parser.add_subparsers(dest="command")
    list_parser = subparsers.add_parser(
        "list-lp-mps", help="List merge proposals for a project."
    )
    list_parser.add_argument(
        "--launchpad-credentials", help="Path to Launchpad credentials file."
    )
    list_parser.add_argument(
        "--status",
        default="Needs review",
        help="Filter merge proposals by status (default: 'Needs review').",
    )
    list_parser.add_argument("project", help="Launchpad project name.")

    review_parser = subparsers.add_parser(
        "review-mp", help="Review a single merge proposal."
    )
    _add_review_options(review_parser)
    review_parser.add_argument(
        "--launchpad-credentials", help="Path to Launchpad credentials file."
    )
    review_parser.add_argument(
        "--dry-run", action="store_true", help="Print review instead of posting it."
    )
    review_parser.add_argument("mp_url", help="URL of the merge proposal to review.")

    diff_parser = subparsers.add_parser(
        "review-diff", help="Review a unified diff file and print the result."
    )
    _add_review_options(diff_parser)
    _add_repo_option(diff_parser)
    diff_parser.add_argument(
        "--description-file",
        type=Path,
        metavar="FILE",
        help="Read the change description from a UTF-8 file (e.g. the PR body).",
    )
    diff_parser.add_argument(
        "--diff-prefix",
        metavar="PREFIX",
        help=(
            "Explicit new-side diff path prefix; inferred from the checkout by "
            "default. Use an empty string for ambiguous prefix-free diffs."
        ),
    )
    diff_parser.add_argument(
        "--json-output",
        type=Path,
        metavar="FILE",
        help="Write structured JSON to FILE instead of plain text to stdout.",
    )
    diff_parser.add_argument(
        "diff_file", help="Path to a unified diff file, or '-' to read stdin."
    )

    pr_parser = subparsers.add_parser(
        "review-pr", help="Review a GitHub pull request and post the review."
    )
    _add_review_options(pr_parser)
    _add_repo_option(pr_parser)
    pr_parser.add_argument(
        "--github-token",
        help="GitHub token (default: the GITHUB_TOKEN environment variable).",
    )
    pr_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print review JSON instead of posting it.",
    )
    pr_parser.add_argument(
        "pr_url", help="Full GitHub PR URL, e.g. https://github.com/owner/repo/pull/42."
    )
    return parser
