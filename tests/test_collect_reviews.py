# Copyright 2026 Canonical Ltd. This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import io
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote

import pytest

from scripts import collect_reviews

BOT = {"id": 1, "login": "github-actions[bot]", "type": "Bot"}
HUMAN = {"id": 2, "login": "developer", "type": "User"}
REPOSITORY = "canonical/maas"
ROOT = f"repos/{REPOSITORY}"


class Response(io.BytesIO):
    def __init__(self, data: object, link: str = ""):
        super().__init__(json.dumps(data).encode())
        self.headers = {"Link": link}


class FakeTransport:
    def __init__(self):
        self.responses: dict[str, tuple[object, str] | HTTPError] = {}
        self.requests = []

    def __call__(self, request, *, timeout):
        self.requests.append(request)
        assert timeout > 0
        result = self.responses[request.full_url]
        if isinstance(result, HTTPError):
            raise result
        return Response(*result)

    def object(self, path: str, data: dict):
        self.responses[f"{collect_reviews.API_ROOT}/{path}"] = (data, "")

    def pages(self, path: str, *pages: list[dict]):
        for index, data in enumerate(pages, start=1):
            url = f"{collect_reviews.API_ROOT}/{path}?per_page=100"
            if index > 1:
                url += f"&page={index}"
            link = ""
            if index < len(pages):
                following = (
                    f"{collect_reviews.API_ROOT}/{path}?per_page=100&page={index + 1}"
                )
                link = f'<{following}>; rel="next", <{following}>; rel="last"'
            self.responses[url] = (data, link)

    def api(self):
        return collect_reviews.GitHubAPI(opener=self)


@dataclass
class History:
    repository: Path
    root: str
    reviewed: str
    final_head: str
    landed: str
    after: str


def git(repository: Path, *args: str) -> str:
    env = os.environ.copy()
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "user.name=Collector Test",
            "-c",
            "user.email=test@example.invalid",
            *args,
        ],
        cwd=repository,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def history(tmp_path: Path) -> History:
    repository = tmp_path / "source"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    (repository / "change.txt").write_text("base\n")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "root")
    root = git(repository, "rev-parse", "HEAD")
    git(repository, "checkout", "-b", "feature")
    (repository / "change.txt").write_text("base\nreview-only\n")
    git(repository, "commit", "-am", "review head before force push")
    reviewed = git(repository, "rev-parse", "HEAD")
    (repository / "change.txt").write_text("base\nfinal-only\n")
    git(repository, "commit", "--amend", "--no-edit", "-a")
    final_head = git(repository, "rev-parse", "HEAD")
    git(repository, "checkout", "main")
    git(repository, "merge", "--no-ff", "--no-edit", "feature")
    landed = git(repository, "rev-parse", "HEAD")
    (repository / "unreviewed.txt").write_text("unreviewed commit\n")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "direct unreviewed commit")
    after = git(repository, "rev-parse", "HEAD")
    return History(repository, root, reviewed, final_head, landed, after)


def published_review(history: History, review_id: int = 11, **changes) -> dict:
    review = {
        "id": review_id,
        "user": BOT,
        "body": collect_reviews.REVIEW_MARKER
        + "\n\nFinding in review "
        + str(review_id),
        "state": "COMMENTED",
        "submitted_at": "2026-10-01T10:00:00Z",
        "commit_id": history.reviewed,
        "html_url": f"https://github.com/{REPOSITORY}/pull/7#pullrequestreview-{review_id}",
    }
    review.update(changes)
    return review


def inline(history: History, comment_id: int, review_id: int = 11, **changes) -> dict:
    comment = {
        "id": comment_id,
        "pull_request_review_id": review_id,
        "user": BOT,
        "path": "change.txt",
        "body": f"finding {comment_id}",
        "side": "RIGHT",
        "line": 2,
        "original_line": 2,
        "original_commit_id": history.reviewed,
        "commit_id": history.reviewed,
    }
    comment.update(changes)
    return comment


@pytest.fixture
def transport(history: History) -> FakeTransport:
    transport = FakeTransport()
    transport.object(ROOT, {"default_branch": "main"})
    transport.pages(f"{ROOT}/commits/{history.after}/pulls", [])
    transport.pages(f"{ROOT}/commits/{history.landed}/pulls", [{"number": 7}])
    transport.object(
        f"{ROOT}/pulls/7",
        {
            "number": 7,
            "merged": True,
            "merge_commit_sha": history.landed,
            "base": {"repo": {"full_name": REPOSITORY}},
            "user": {"id": 100, "login": "proposer", "type": "User"},
            "body": "Description edited after the original review",
            "updated_at": "2026-10-20T10:00:00Z",
            "html_url": f"https://github.com/{REPOSITORY}/pull/7",
        },
    )
    transport.pages(f"{ROOT}/pulls/7/reviews", [published_review(history)])
    transport.pages(f"{ROOT}/pulls/7/comments", [])
    transport.pages(f"repos/{REPOSITORY}/issues/7/comments", [])
    return transport


def collect(output: Path, history: History, transport: FakeTransport, **kwargs) -> dict:
    return collect_reviews.collect(
        output, 2, api=transport.api(), remote_url=str(history.repository), **kwargs
    )


def case(output: Path, history: History, review_id: int = 11) -> Path:
    return output / "cases" / f"{history.landed}-pr7-review{review_id}"


def baseline(output: Path, history: History, review_id: int = 11, model=None) -> dict:
    path = (
        output
        / "reviews"
        / quote(model or collect_reviews.DEFAULT_MODEL, safe="")
        / f"{case(output, history, review_id).name}.json"
    )
    return json.loads(path.read_text())


def human_record(output: Path, history: History, review_id: int) -> Path:
    return output / "human_reviews" / f"{history.landed}-pr7" / f"review{review_id}"


def test_reconstructs_force_pushed_review_head_not_landed_patch(
    tmp_path: Path,
    history: History,
    transport: FakeTransport,
):
    output = tmp_path / "corpus"
    collect(output, history, transport)

    review_case = case(output, history)
    patch = (review_case / "diff.patch").read_text()
    assert "diff --git a/change.txt b/change.txt" in patch
    assert "+review-only\n" in patch
    assert "final-only" not in patch
    assert "unreviewed.txt" not in patch
    metadata = json.loads((review_case / "metadata.json").read_text())
    assert metadata["head_sha"] == history.reviewed
    assert metadata["base_sha"] == history.root
    assert metadata["landed_commit"] == history.landed
    assert git(
        output / "repository.git", "show", f"{metadata['head_sha']}:change.txt"
    ) == ("base\nreview-only")
    assert (
        git(
            output / "repository.git",
            "rev-parse",
            f"refs/review-heads/{history.reviewed}",
        )
        == history.reviewed
    )
    # The description is deliberately newer than the review, and is not invented.
    assert (review_case / "description.md").read_text() == (
        "Description edited after the original review"
    )
    assert metadata["description_updated_at"] > metadata["review_submitted_at"]


def test_last_n_is_first_parent_commits_including_unreviewed(
    tmp_path: Path,
    history: History,
    transport: FakeTransport,
):
    manifest = collect(tmp_path / "corpus", history, transport)

    assert [entry["landed_commit"] for entry in manifest["commits"]] == [
        history.after,
        history.landed,
    ]
    assert manifest["commits"][0]["status"] == "skipped"
    assert manifest["commits"][0]["skip_reason"] == "no_matching_merged_pull_request"
    assert manifest["commits"][0]["cases"] == []
    assert len(manifest["commits"][1]["cases"]) == 1
    assert not any(
        history.final_head in request.full_url for request in transport.requests
    )
    assert manifest["selection"]["resolved_head"] == history.after


def test_pages_associations_reviews_and_comments_without_pooling_revisions(
    tmp_path: Path,
    history: History,
    transport: FakeTransport,
):
    transport.pages(
        f"{ROOT}/commits/{history.landed}/pulls",
        [{"number": 99}],
        [{"number": 7}],
    )
    transport.object(
        f"{ROOT}/pulls/99",
        {
            "merged": False,
            "merge_commit_sha": history.landed,
            "base": {"repo": {"full_name": REPOSITORY}},
        },
    )
    transport.pages(
        f"{ROOT}/pulls/7/reviews",
        [
            published_review(history, 1, user=HUMAN),
            published_review(history, 2, body="An unrelated bot review"),
            published_review(history, 3, state="PENDING", submitted_at=None),
        ],
        [
            published_review(history),
            published_review(history, 12, commit_id=history.final_head),
        ],
    )
    transport.pages(
        f"{ROOT}/pulls/7/comments",
        [
            inline(history, 90, 999),
            inline(history, 20, line=None),
            inline(history, 30, user=HUMAN),
            inline(history, 40, in_reply_to_id=20),
        ],
        [
            inline(history, 9, line=10, commit_id=history.final_head),
            inline(
                history,
                21,
                12,
                original_commit_id=history.final_head,
                commit_id=history.final_head,
            ),
        ],
    )
    output = tmp_path / "corpus"
    manifest = collect(output, history, transport)

    assert baseline(output, history)["inline_comments"] == {
        "change.txt": {"2": "finding 9\n\n---\n\nfinding 20"},
    }
    assert baseline(output, history, 12)["inline_comments"] == {
        "change.txt": {"2": "finding 21"},
    }
    assert (
        baseline(output, history)["general_comment"]
        == published_review(history)["body"]
    )
    assert len(manifest["commits"][1]["cases"]) == 2
    assert not any(
        "pulls/99/reviews" in request.full_url for request in transport.requests
    )
    assert sum("page=2" in request.full_url for request in transport.requests) == 3


def test_unsupported_findings_are_preserved_verbatim_and_warned(
    tmp_path: Path,
    history: History,
    transport: FakeTransport,
    capsys,
):
    unsupported = [
        inline(history, 1, side="LEFT"),
        inline(history, 2, original_start_line=1),
        inline(history, 3, original_line=None, line=None, position=3),
        inline(history, 4, path="../outside.txt"),
        inline(history, 5, original_commit_id=history.final_head),
    ]
    transport.pages(f"{ROOT}/pulls/7/comments", unsupported + [inline(history, 6)])
    output = tmp_path / "corpus"
    collect(output, history, transport)

    assert baseline(output, history)["inline_comments"] == {
        "change.txt": {"2": "finding 6"}
    }
    metadata = json.loads((case(output, history) / "metadata.json").read_text())
    assert metadata["unsupported_inline_comment_count"] == 5
    assert [
        entry["comment"] for entry in metadata["unsupported_inline_comments"]
    ] == unsupported
    assert all(entry["reason"] for entry in metadata["unsupported_inline_comments"])
    assert "preserving 5 unsupported" in capsys.readouterr().err


def test_no_published_bot_reviews_has_explicit_manifest_entry(
    tmp_path: Path,
    history: History,
    transport: FakeTransport,
):
    transport.pages(f"{ROOT}/pulls/7/reviews", [])
    output = tmp_path / "corpus"
    manifest = collect(output, history, transport)

    entry = manifest["commits"][1]
    assert entry["cases"] == []
    assert entry["status"] == "skipped"
    assert entry["pull_requests"][0]["skip_reason"] == "no_matching_published_reviews"
    assert not (output / "cases").exists()


def test_merged_associated_pr_must_match_actual_landed_commit(
    tmp_path: Path,
    history: History,
    transport: FakeTransport,
):
    transport.object(
        f"{ROOT}/pulls/7",
        {
            "merged": True,
            "merge_commit_sha": history.final_head,
            "base": {"repo": {"full_name": REPOSITORY}},
        },
    )
    manifest = collect(tmp_path / "corpus", history, transport)

    assert manifest["commits"][1]["skip_reason"] == "no_matching_merged_pull_request"
    assert not any("/reviews?" in request.full_url for request in transport.requests)


def test_model_names_are_percent_encoded_without_changing_the_label(
    tmp_path: Path,
    history: History,
    transport: FakeTransport,
):
    model = "provider/model:revision?mode=x&value=../%"
    output = tmp_path / "corpus"
    collect(output, history, transport, original_model=model)

    metadata = json.loads((case(output, history) / "metadata.json").read_text())
    assert metadata["original_model"] == model
    assert baseline(output, history, model=model)["general_comment"]
    assert [path.name for path in (output / "reviews").iterdir()] == [
        quote(model, safe="")
    ]


def test_restart_is_idempotent_and_never_replaces_changed_description(
    tmp_path: Path,
    history: History,
    transport: FakeTransport,
):
    output = tmp_path / "corpus"
    first = collect(output, history, transport)
    assert collect(output, history, transport) == first
    original_description = (case(output, history) / "description.md").read_bytes()
    pr_url = f"{collect_reviews.API_ROOT}/{ROOT}/pulls/7"
    response = transport.responses[pr_url]
    assert isinstance(response, tuple)
    pr, _ = response
    assert isinstance(pr, dict)
    transport.object(
        f"{ROOT}/pulls/7", {**pr, "body": "Changed since corpus was collected"}
    )

    with pytest.raises(
        collect_reviews.CollectionError, match="Existing corpus data differs"
    ):
        collect(output, history, transport)
    assert (
        case(output, history) / "description.md"
    ).read_bytes() == original_description
    assert json.loads((output / "manifest.json").read_text()) == first


def test_restart_uses_cached_head_after_remote_drops_force_pushed_commit(
    tmp_path: Path,
    history: History,
    transport: FakeTransport,
):
    output = tmp_path / "corpus"
    original = collect(output, history, transport)
    # The orphan commit disappears remotely, while the landed branch is intact.
    commit_object = (
        history.repository
        / ".git"
        / "objects"
        / history.reviewed[:2]
        / history.reviewed[2:]
    )
    commit_object.unlink()

    assert collect(output, history, transport) == original
    assert "+review-only" in (case(output, history) / "diff.patch").read_text()


def test_restart_rejects_changed_review_without_overwriting_baseline(
    tmp_path: Path,
    history: History,
    transport: FakeTransport,
):
    output = tmp_path / "corpus"
    collect(output, history, transport)
    original = baseline(output, history)
    transport.pages(
        f"{ROOT}/pulls/7/reviews",
        [
            published_review(
                history,
                body=(collect_reviews.REVIEW_MARKER + "\nEdited published finding"),
            )
        ],
    )

    with pytest.raises(
        collect_reviews.CollectionError, match="Existing corpus data differs"
    ):
        collect(output, history, transport)
    assert baseline(output, history) == original


def test_api_error_does_not_publish_a_successful_manifest(
    tmp_path: Path,
    history: History,
    transport: FakeTransport,
):
    url = f"{collect_reviews.API_ROOT}/{ROOT}/pulls/7/reviews?per_page=100"
    transport.responses[url] = HTTPError(url, 403, "rate limit exceeded", {}, None)
    output = tmp_path / "corpus"

    with pytest.raises(collect_reviews.CollectionError, match="HTTP 403.*rate limit"):
        collect(output, history, transport)
    assert not (output / "manifest.json").exists()


def test_unavailable_historical_head_is_reported_not_replaced_by_final_head(
    tmp_path: Path,
    history: History,
    transport: FakeTransport,
    capsys,
):
    transport.pages(
        f"{ROOT}/pulls/7/reviews", [published_review(history, commit_id="a" * 40)]
    )
    output = tmp_path / "corpus"
    manifest = collect(output, history, transport)

    skipped = manifest["commits"][1]["pull_requests"][0]["skipped_reviews"]
    assert skipped[0]["skip_reason"] == "review_head_unavailable"
    assert skipped[0]["review_id"] == 11
    assert manifest["commits"][1]["cases"] == []
    assert not (output / "cases").exists()
    assert "skipping review 11" in capsys.readouterr().err


def test_api_pagination_authenticates_and_rejects_off_host_next_links():
    transport = FakeTransport()
    transport.pages("items", [{"id": 1}], [{"id": 2}])
    api = collect_reviews.GitHubAPI("secret", opener=transport)
    assert api.pages("items") == [{"id": 1}, {"id": 2}]
    assert all(
        request.get_header("Authorization") == "Bearer secret"
        for request in transport.requests
    )
    transport.responses[f"{collect_reviews.API_ROOT}/items?per_page=100"] = (
        [],
        '<https://attacker.invalid/items>; rel="next"',
    )
    with pytest.raises(collect_reviews.CollectionError, match="Unsafe GitHub API URL"):
        api.pages("items")
    assert len(transport.requests) == 3


def test_cli_help_and_positive_count():
    with pytest.raises(SystemExit) as help_exit:
        collect_reviews.main(["--help"])
    assert help_exit.value.code == 0
    with pytest.raises(SystemExit) as invalid_exit:
        collect_reviews.main(["--count", "0", "--output", "unused"])
    assert invalid_exit.value.code == 2


@pytest.mark.parametrize("model", ["", ".", ".."])
def test_unsafe_model_label_is_rejected_before_creating_output(
    tmp_path: Path, model: str
):
    output = tmp_path / "corpus"
    with pytest.raises(collect_reviews.CollectionError, match="model label"):
        collect_reviews.collect(output, 1, original_model=model)
    assert not output.exists()


def test_human_reviews_are_separate_by_author_and_revision_without_replay_cases(
    tmp_path: Path,
    history: History,
    transport: FakeTransport,
):
    other = {"id": 3, "login": "another-reviewer", "type": "User"}
    transport.pages(
        f"{ROOT}/pulls/7/reviews",
        [
            published_review(history),
            published_review(history, 21, user=HUMAN, body="Check old behavior."),
            published_review(
                history,
                22,
                user=other,
                body="Check final behavior.",
                commit_id=history.final_head,
            ),
            published_review(history, 23, user=HUMAN, body=None, state="APPROVED"),
            published_review(
                history, 24, user=HUMAN, state="PENDING", submitted_at=None
            ),
            published_review(
                history, 25, user=other, body="Earlier concern.", state="DISMISSED"
            ),
            published_review(history, 26, body="Another bot, not our reviewer."),
        ],
    )
    transport.pages(
        f"{ROOT}/pulls/7/comments",
        [
            inline(history, 1, 21, user=HUMAN, line=10),
            inline(
                history,
                2,
                22,
                user=other,
                original_commit_id=history.final_head,
                commit_id=history.final_head,
            ),
            inline(history, 3, 21, user=other),
            inline(history, 4, user=BOT),
            inline(history, 5, 24, user=HUMAN, body="Unsubmitted draft concern."),
        ],
    )
    output = tmp_path / "corpus"
    manifest = collect(output, history, transport)
    entry = manifest["commits"][1]
    assert entry["cases"] == [case(output, history).name]
    assert len(entry["human_reviews"]) == 4
    assert {Path(path).name for path in entry["human_reviews"]} == {
        "review21",
        "review22",
        "review23",
        "review25",
    }
    discussion_path = output / entry["pull_requests"][0]["human_discussion"]
    discussion = json.loads(discussion_path.read_text())
    assert {comment["id"] for comment in discussion["review_comments"]} == {1, 2, 3}
    old = human_record(output, history, 21)
    final = human_record(output, history, 22)
    old_review = json.loads((old / "review.json").read_text())
    final_review = json.loads((final / "review.json").read_text())
    assert old_review["inline_comments"] == {"change.txt": {"2": "finding 1"}}
    assert final_review["inline_comments"] == {"change.txt": {"2": "finding 2"}}
    assert "+review-only" in (old / "diff.patch").read_text()
    assert "+final-only" not in (old / "diff.patch").read_text()
    assert "+final-only" in (final / "diff.patch").read_text()
    assert "+review-only" not in (final / "diff.patch").read_text()
    metadata = json.loads((final / "metadata.json").read_text())
    assert metadata["author"] == other["login"]
    assert metadata["head_sha"] == history.final_head
    assert metadata["is_pr_author"] is False
    approval = human_record(output, history, 23)
    assert json.loads((approval / "review.json").read_text()) == {
        "general_comment": "",
        "inline_comments": {},
    }
    assert json.loads((approval / "metadata.json").read_text())["state"] == "APPROVED"
    assert baseline(output, history)["inline_comments"] == {
        "change.txt": {"2": "finding 4"},
    }


def test_human_only_pr_collects_findings_without_adding_model_inputs(
    tmp_path: Path,
    history: History,
    transport: FakeTransport,
):
    transport.pages(
        f"{ROOT}/pulls/7/reviews",
        [
            published_review(history, 21, user=HUMAN, body="Human concern."),
        ],
    )
    transport.pages(f"{ROOT}/pulls/7/comments", [inline(history, 1, 21, user=HUMAN)])
    output = tmp_path / "corpus"
    manifest = collect(output, history, transport)
    entry = manifest["commits"][1]
    assert entry["status"] == "collected"
    assert entry["cases"] == []
    assert not (output / "cases").exists()
    assert not (output / "reviews").exists()
    review_path = output / entry["human_reviews"][0] / "review.json"
    assert json.loads(review_path.read_text())["inline_comments"] == {
        "change.txt": {"2": "finding 1"},
    }


def test_human_unsupported_coordinates_and_thread_context_are_not_lost(
    tmp_path: Path,
    history: History,
    transport: FakeTransport,
):
    proposer = {"id": 100, "login": "proposer", "type": "User"}
    transport.pages(
        f"{ROOT}/pulls/7/reviews",
        [
            published_review(history),
            published_review(history, 21, user=HUMAN, body="Review concern."),
            published_review(history, 22, user=proposer, body="Author self-review."),
        ],
    )
    comments = [
        inline(history, 1, 21, user=HUMAN, side="LEFT"),
        inline(history, 2, 21, user=HUMAN, original_start_line=1),
        inline(history, 3, 21, user=HUMAN, line=None),
        inline(history, 4, 21, user=proposer, in_reply_to_id=3, body="Author answer."),
        inline(history, 5, user=HUMAN, in_reply_to_id=1000, body="Answer to the bot."),
        inline(history, 6, 21, user=BOT, in_reply_to_id=3),
    ]
    transport.pages(f"{ROOT}/pulls/7/comments", comments)
    transport.pages(
        f"{ROOT}/issues/7/comments",
        [
            {"id": 7, "user": HUMAN, "body": "A concern in the PR conversation."},
            {"id": 8, "user": BOT, "body": "Build passed."},
        ],
    )
    output = tmp_path / "corpus"
    manifest = collect(output, history, transport)
    record = human_record(output, history, 21)
    metadata = json.loads((record / "metadata.json").read_text())
    assert [
        item["comment"]["id"] for item in metadata["unsupported_inline_comments"]
    ] == [1, 2]
    assert json.loads((record / "review.json").read_text())["inline_comments"] == {
        "change.txt": {"2": "finding 3"},
    }
    pr_entry = manifest["commits"][1]["pull_requests"][0]
    discussion = json.loads((output / pr_entry["human_discussion"]).read_text())
    assert [comment["id"] for comment in discussion["review_comments"]] == [
        1,
        2,
        3,
        4,
        5,
    ]
    assert [comment["id"] for comment in discussion["conversation_comments"]] == [7]
    assert discussion["review_comments"][3]["in_reply_to_id"] == 3
    author_record = human_record(output, history, 22)
    assert (
        json.loads((author_record / "metadata.json").read_text())["is_pr_author"]
        is True
    )


def test_human_review_survives_unavailable_historical_head(
    tmp_path: Path,
    history: History,
    transport: FakeTransport,
):
    missing = "f" * 40
    transport.pages(
        f"{ROOT}/pulls/7/reviews",
        [
            published_review(history),
            published_review(
                history,
                21,
                user=HUMAN,
                body="Concern on old commit.",
                commit_id=missing,
            ),
        ],
    )
    output = tmp_path / "corpus"
    manifest = collect(output, history, transport)
    record = human_record(output, history, 21)
    assert json.loads((record / "review.json").read_text())["general_comment"] == (
        "Concern on old commit."
    )
    metadata = json.loads((record / "metadata.json").read_text())
    assert metadata["head_sha"] == missing
    assert metadata["diff_status"] == "unavailable"
    assert metadata["base_sha"] is None
    assert not (record / "diff.patch").exists()
    assert manifest["commits"][1]["human_reviews"] == [
        record.relative_to(output).as_posix()
    ]
    assert manifest["commits"][1]["cases"] == [case(output, history).name]


def test_human_reviews_and_conversation_pagination_preserve_every_reviewer(
    tmp_path: Path,
    history: History,
    transport: FakeTransport,
):
    other = {"id": 3, "login": "another-reviewer", "type": "User"}
    transport.pages(
        f"{ROOT}/pulls/7/reviews",
        [published_review(history), published_review(history, 21, user=HUMAN)],
        [published_review(history, 22, user=other)],
    )
    transport.pages(
        f"{ROOT}/pulls/7/comments",
        [inline(history, 1, 21, user=HUMAN)],
        [inline(history, 2, 22, user=other)],
    )
    transport.pages(
        f"{ROOT}/issues/7/comments",
        [{"id": 3, "user": HUMAN, "body": "First concern."}],
        [{"id": 4, "user": other, "body": "Second concern."}],
    )
    output = tmp_path / "corpus"
    manifest = collect(output, history, transport)
    entry = manifest["commits"][1]
    assert len(entry["human_reviews"]) == 2
    for review_id, comment_id in [(21, 1), (22, 2)]:
        path = human_record(output, history, review_id) / "review.json"
        assert json.loads(path.read_text())["inline_comments"] == {
            "change.txt": {"2": f"finding {comment_id}"},
        }
    path = output / entry["pull_requests"][0]["human_discussion"]
    assert [
        item["id"] for item in json.loads(path.read_text())["conversation_comments"]
    ] == [3, 4]


def test_changed_human_review_is_not_silently_overwritten(
    tmp_path: Path,
    history: History,
    transport: FakeTransport,
):
    human = published_review(history, 21, user=HUMAN, body="Original concern.")
    transport.pages(f"{ROOT}/pulls/7/reviews", [published_review(history), human])
    output = tmp_path / "corpus"
    first = collect(output, history, transport)
    record = human_record(output, history, 21)
    original = (record / "review.json").read_bytes()
    transport.pages(
        f"{ROOT}/pulls/7/reviews",
        [
            published_review(history),
            {**human, "body": "Edited concern."},
        ],
    )
    with pytest.raises(
        collect_reviews.CollectionError, match="Existing corpus data differs"
    ):
        collect(output, history, transport)
    assert (record / "review.json").read_bytes() == original
    assert json.loads((output / "manifest.json").read_text()) == first


def test_old_corpus_schema_requires_fresh_output_without_adding_human_data(
    tmp_path: Path,
    history: History,
    transport: FakeTransport,
):
    output = tmp_path / "corpus"
    output.mkdir()
    old = b'{"schema_version": 1}\n'
    (output / "manifest.json").write_bytes(old)
    with pytest.raises(collect_reviews.CollectionError, match="new output directory"):
        collect(output, history, transport)
    assert (output / "manifest.json").read_bytes() == old
    assert not (output / "human_reviews").exists()
