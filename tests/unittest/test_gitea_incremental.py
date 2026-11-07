import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import giteapy
import pytest

from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import IncrementalPR
from pr_agent.git_providers.gitea_incremental import _CHECKPOINT
from pr_agent.git_providers.gitea_provider import GiteaProvider, RepoApi
from pr_agent.mosaico.diff_provider import parse_unified_diff
from pr_agent.tools.pr_reviewer import PRReviewer
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

BASE, ANCHOR, HEAD = "a" * 40, "b" * 40, "c" * 40


def review(head=ANCHOR, **kwargs):
    return dict(id=10, user={"id": 7}, state="COMMENT", submitted_at="2026-01-01T00:00:00Z",
                commit_id=head, body=_CHECKPOINT + json.dumps({"head": head, "base": BASE}) + " -->", **kwargs)


@pytest.fixture
def provider():
    p = GiteaProvider.__new__(GiteaProvider)
    p.owner, p.repo, p.pr_number = "owner", "repo", 1
    p.sha, p.base_sha = HEAD, BASE
    p.logger = MagicMock()
    p.repo_api = MagicMock()
    p.incremental = IncrementalPR(False)
    p.diff_files = None
    p.max_comment_chars = 65000
    p.unreviewed_files_map = {}
    p.repo_api.incremental_pages.return_value = [review()]
    p.repo_api.incremental_json.side_effect = [
        {"id": 7},
        {"total_commits": 1, "commits": [
            {"sha": HEAD, "parents": [{"sha": ANCHOR}],
             "files": [{"filename": "code.py", "status": "modified"}]}
        ]},
    ]
    p.repo_api.incremental_bytes.side_effect = [
        b"old_reviewed_change = True\nvalue = 1\n", b"old_reviewed_change = True\nvalue = 2\n"
    ]
    p.repo_api.get_pull_request.return_value = SimpleNamespace(
        head=SimpleNamespace(sha=HEAD), base=SimpleNamespace(sha=BASE)
    )
    return p


def test_incremental_is_net_delta_not_full_pr(provider):
    incremental = IncrementalPR(True)
    provider.get_incremental_commits(incremental)
    files = provider.get_diff_files()
    assert incremental.first_new_commit_sha == HEAD
    assert files[0].base_file.endswith("value = 1\n")
    assert "-value = 1\n+value = 2" in files[0].patch
    assert "+old_reviewed_change" not in files[0].patch
    assert files[0].head_file_is_complete
    provider.repo_api.get_pull_request_diff.assert_not_called()


@pytest.mark.parametrize("change", ["none", "human", "pending", "dismissed", "no_marker", "mismatch", "base_changed"])
def test_only_completed_authenticated_native_reviews_anchor(provider, change):
    reviews = [review()]
    if change == "none":
        reviews = []
    elif change == "human":
        reviews[0]["user"] = {"id": 8}
    elif change == "pending":
        reviews[0]["state"] = "PENDING"
    elif change == "dismissed":
        reviews[0]["dismissed"] = True
    elif change == "no_marker":
        reviews[0]["body"] = "Failed to review PR"
    elif change == "mismatch":
        reviews[0]["commit_id"] = HEAD
    else:
        provider.base_sha = "d" * 40
    provider.repo_api.incremental_pages.return_value = reviews
    incremental = IncrementalPR(True)
    provider.get_incremental_commits(incremental)
    assert incremental.commits_range is None
    assert provider._incremental_diff_files is None


def test_same_head_does_not_call_compare_or_model(provider):
    provider.sha = ANCHOR
    incremental = IncrementalPR(True)
    provider.get_incremental_commits(incremental)
    assert incremental.commits_range == []
    assert provider.repo_api.incremental_json.call_count == 1
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.incremental, reviewer.git_provider, reviewer.is_auto = incremental, provider, True
    assert not reviewer._can_run_incremental_review()


@pytest.mark.parametrize("change", ["merge", "rewritten", "count", "missing_files", "unavailable", "content"])
def test_uncertain_delta_transparently_falls_back(provider, change):
    comparison = {"total_commits": 1, "commits": [
        {"sha": HEAD, "parents": [{"sha": ANCHOR}], "files": []}
    ]}
    commit = comparison["commits"][0]
    if change == "merge":
        commit["parents"].append({"sha": BASE})
    elif change == "rewritten":
        commit["parents"] = [{"sha": BASE}]
    elif change == "count":
        comparison["total_commits"] = 2
    elif change == "missing_files":
        del commit["files"]
    elif change == "unavailable":
        comparison = ValueError("unavailable")
    else:
        commit["files"] = [{"filename": "code.py", "status": "modified"}]
        provider.repo_api.incremental_bytes.side_effect = OSError("unavailable")
    provider.repo_api.incremental_json.side_effect = [{"id": 7}, comparison]
    incremental = IncrementalPR(True)
    provider.get_incremental_commits(incremental)
    assert incremental.commits_range is None
    assert provider.incremental_fallback_reason


def test_delta_uses_parent_order_and_handles_add_remove_and_revert(provider):
    mid = "d" * 40
    provider.repo_api.incremental_json.side_effect = [{"id": 7}, {
        "total_commits": 2, "commits": [
            {"sha": HEAD, "parents": [{"sha": mid}], "files": [
                {"filename": "temporary.py", "status": "removed"},
                {"filename": "deleted.py", "status": "removed"}]},
            {"sha": mid, "parents": [{"sha": ANCHOR}], "files": [
                {"filename": "temporary.py", "status": "added"},
                {"filename": "new.py", "status": "added"}]},
        ]}]
    provider.repo_api.incremental_bytes.side_effect = [b"old\n", b"new\n"]
    incremental = IncrementalPR(True)
    provider.get_incremental_commits(incremental)
    assert [c.sha for c in incremental.commits_range] == [mid, HEAD]
    assert [f.filename for f in provider.get_diff_files()] == ["deleted.py", "new.py"]


def test_empty_net_delta_has_no_reviewable_files(provider):
    provider.repo_api.incremental_bytes.side_effect = [b"same\n", b"same\n"]
    incremental = IncrementalPR(True)
    provider.get_incremental_commits(incremental)
    assert incremental.first_new_commit_sha == HEAD
    assert provider.get_diff_files() == []
    assert provider.unreviewed_files_map == {}


@pytest.mark.parametrize("complete", [True, False])
def test_atomic_native_review_checkpoint(provider, complete):
    provider.publish_review_result("## Review", complete=complete)
    args = provider.repo_api.create_inline_comment.call_args.args
    assert args[:3] == ("owner", "repo", 1)
    assert (_CHECKPOINT in args[3]) is complete
    assert args[4:] == (HEAD, [])


def test_partial_model_output_cannot_supply_a_checkpoint(provider):
    provider.publish_review_result(review()["body"], complete=False)
    assert _CHECKPOINT not in provider.repo_api.create_inline_comment.call_args.args[3]


def test_truncated_publication_does_not_advance_checkpoint(provider):
    provider.max_comment_chars = 100
    provider.publish_review_result("## Review\n" + "finding " * 50, complete=True)
    assert _CHECKPOINT not in provider.repo_api.create_inline_comment.call_args.args[3]


def test_minimum_minutes_uses_anchor_commit_metadata(provider, monkeypatch):
    monkeypatch.setitem(get_settings().pr_reviewer, "minimal_minutes_for_incremental_review", 10)
    responses = list(provider.repo_api.incremental_json.side_effect)
    responses.append({"commit": {"author": {"date": "2026-01-01T00:00:00Z"}}})
    provider.repo_api.incremental_json.side_effect = responses
    incremental = IncrementalPR(True)
    provider.get_incremental_commits(incremental)
    assert incremental.last_seen_commit.sha == ANCHOR
    assert incremental.last_seen_commit.commit.author.date.year == 2026


@pytest.mark.parametrize("change", ["head", "base", "failed_post"])
def test_publication_failure_or_head_race_never_advances_checkpoint(provider, change):
    if change == "failed_post":
        provider.repo_api.create_inline_comment.return_value = None
    else:
        getattr(provider.repo_api.get_pull_request.return_value, change).sha = "d" * 40
    with pytest.raises(RuntimeError):
        provider.publish_review_result("## Review", complete=True)
    if change != "failed_post":
        provider.repo_api.create_inline_comment.assert_not_called()


def _review_page(batch, total):
    return SimpleNamespace(data=json.dumps(batch).encode(), headers={"X-Total-Count": str(total)})


def test_review_pages_use_one_record_to_verify_server_capped_range():
    api = RepoApi(MagicMock())
    api.incremental_response = MagicMock(side_effect=[_review_page([review()], 2), _review_page([review(HEAD)], 2)])
    assert len(api.incremental_pages("/repos/owner/repo/pulls/1/reviews")) == 2
    assert api.incremental_response.call_args_list[-1].args[1] == [("page", 2), ("limit", 1)]


def test_empty_private_review_page_does_not_hide_later_checkpoint(provider):
    api = RepoApi(MagicMock())
    api.incremental_response = MagicMock(side_effect=[_review_page([], 2), _review_page([review()], 2)])
    provider.repo_api.incremental_pages = api.incremental_pages
    incremental = IncrementalPR(True)
    provider.get_incremental_commits(incremental)
    assert incremental.first_new_commit_sha == HEAD
    assert api.incremental_response.call_count == 2


@pytest.mark.parametrize("failure", ["missing", "changing", "excessive"])
def test_unverifiable_review_pagination_fails_closed(failure):
    api = RepoApi(MagicMock())
    responses = [_review_page([], 2), _review_page([], 3)]
    if failure == "missing":
        responses[0].headers = {}
    elif failure == "excessive":
        responses[0] = _review_page([], 1001)
    api.incremental_response = MagicMock(side_effect=responses)
    with pytest.raises(ValueError):
        api.incremental_pages("/repos/owner/repo/pulls/1/reviews")


def test_review_page_failure_cannot_return_a_partial_anchor_list():
    api = RepoApi(MagicMock())
    api.incremental_response = MagicMock(side_effect=[_review_page([review()], 2), ValueError("unavailable")])
    with pytest.raises(ValueError):
        api.incremental_pages("/repos/owner/repo/pulls/1/reviews")


def test_incremental_http_never_uses_web_url_or_follows_redirects():
    config = giteapy.Configuration()
    config.host = "https://api.example/gitea/api/v1"
    config.api_key["Authorization"] = "token fake-test-token"
    api = RepoApi(giteapy.ApiClient(config))
    request = MagicMock(return_value=SimpleNamespace(status=302, data=b"redirect"))
    with patch.object(api.api_client.rest_client.pool_manager, "request", request):
        with pytest.raises(ValueError):
            api.incremental_json("/user")
    assert request.call_args.args[1] == "https://api.example/gitea/api/v1/user"
    assert request.call_args.kwargs["redirect"] is False


def test_inline_positions_stay_in_full_pr_new_side(provider):
    provider.incremental = IncrementalPR(True)
    provider.repo_api.get_pull_request_diff.return_value = (
        "diff --git a/code.py b/code.py\n--- a/code.py\n+++ b/code.py\n"
        "@@ -10 +10 @@\n-old\n+new\n"
    )
    valid = {"path": "code.py", "new_position": 10, "old_position": 0}
    assert provider._incremental_inline_comments_valid([valid])
    assert not provider._incremental_inline_comments_valid([dict(valid, new_position=1)])
    assert not provider._incremental_inline_comments_valid([dict(valid, old_position=10)])


@pytest.mark.parametrize("separator", ["\u2028", "\f"])
def test_inline_position_index_only_counts_lf_delimited_lines(provider, separator):
    provider.incremental = IncrementalPR(True)
    provider.repo_api.get_pull_request_diff.return_value = (
        "diff --git a/code.py b/code.py\n--- a/code.py\n+++ b/code.py\n"
        f"@@ -10,2 +10,2 @@\n x{separator}+phantom\n-old\n+new\n"
    )
    for line, valid in [(10, True), (11, True), (12, False)]:
        assert provider._incremental_inline_comments_valid([
            {"path": "code.py", "new_position": line, "old_position": 0}
        ]) is valid


def test_inline_position_index_is_reused_without_losing_revision_checks(provider):
    provider.incremental = IncrementalPR(True)
    provider.enabled_pr = True
    provider.repo_api.get_pull_request_diff.return_value = (
        "diff --git a/code.py b/code.py\n--- a/code.py\n+++ b/code.py\n"
        "@@ -10,2 +10,2 @@\n context\n-old\n+new\n"
    )
    suggestions = [{"body": "finding", "relevant_file": "code.py", "relevant_lines_start": line}
                   for line in (10, 11)]
    with patch("pr_agent.git_providers.gitea_incremental.parse_unified_diff", wraps=parse_unified_diff) as parser:
        assert provider.publish_code_suggestions(suggestions)
        provider.repo_api.get_pull_request_diff.assert_called_once()
        parser.assert_called_once()
        assert provider.repo_api.get_pull_request.call_count == 3
        assert provider.repo_api.create_inline_comment.call_count == 2
        provider.repo_api.get_pull_request.return_value.head.sha = ANCHOR
        assert not provider.publish_inline_comments([{"path": "code.py", "new_position": 10}])
        assert provider.repo_api.create_inline_comment.call_count == 2
        assert provider.repo_api.get_pull_request.call_count == 4
        parser.assert_called_once()
        # Starting another incremental command discards the old position index.
        provider.repo_api.get_pull_request.return_value.head.sha = HEAD
        provider.get_incremental_commits(IncrementalPR(True))
        assert provider._incremental_inline_position_cache is None
        assert provider._incremental_inline_comments_valid([{"path": "code.py", "new_position": 10}])
        assert provider.repo_api.get_pull_request_diff.call_count == 2
        assert parser.call_count == 2


def test_inline_publication_uses_captured_head_not_incomplete_commit_listing(provider):
    provider.enabled_pr = True
    provider.last_commit = SimpleNamespace(sha=ANCHOR)
    assert provider.publish_inline_comments([{"path": "code.py", "new_position": 1}], body=review()["body"])
    assert provider.repo_api.create_inline_comment.call_args.kwargs["commit_id"] == HEAD
    assert _CHECKPOINT not in provider.repo_api.create_inline_comment.call_args.kwargs["body"]


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["first", "incremental", "failed_chunk", "omitted", "truncated"])
async def test_reviewer_prompt_pipeline_receives_real_delta_or_first_full_fallback(provider, scenario, monkeypatch):
    """Run the real diff/token pipeline into a stubbed model, without network/model calls."""
    from pr_agent.tools import pr_reviewer as module

    snapshot = snapshot_settings(("config.model", "config.publish_output", "config.is_auto_command",
                                  "config.enable_ai_metadata", "pr_reviewer.enable_large_pr_chunking",
                                  "pr_reviewer.inline_key_issues"))
    try:
        previous = scenario != "first"
        get_settings().set("config.model", "gpt-4o")
        get_settings().set("config.publish_output", True)
        get_settings().set("config.is_auto_command", True)
        get_settings().set("pr_reviewer.enable_large_pr_chunking", False)
        get_settings().set("pr_reviewer.inline_key_issues", True)
        if not previous:
            provider.repo_api.incremental_pages.return_value = []
        provider.get_languages = lambda: {"Python": 100}
        provider.get_files = lambda: ["code.py"]
        provider.get_num_of_files = lambda: 1
        provider.get_pr_branch = lambda: "feature"
        provider.get_pr_description = lambda **kwargs: ("A fake PR", [])
        provider.get_commit_messages = lambda: "change value"
        provider.pr = SimpleNamespace(title="Fake PR", labels=[])
        provider.base_url_html = "https://forge.example"
        provider.get_persistent_comment_bodies = lambda: []
        provider.repo_api.get_pull_request_diff.return_value = (
            "diff --git a/code.py b/code.py\n--- a/code.py\n+++ b/code.py\n"
            "@@ -10 +10 @@\n-old\n+new\n"
        )
        # Full first reviews exercise a backend rejection; incremental reviews reject
        # the finding's line before posting because it is outside the full PR hunk.
        provider.enabled_pr = True
        provider.last_commit = SimpleNamespace(sha=HEAD)
        provider.repo_api.create_inline_comment.side_effect = (
            lambda *args, **kwargs: None if kwargs.get("comments") else object()
        )
        monkeypatch.setattr(module, "get_git_provider_with_context", lambda _: provider)
        monkeypatch.setattr(module, "build_repo_context", lambda _: "")
        monkeypatch.setattr(module, "get_artifact_context", lambda _: "")
        monkeypatch.setattr(module, "get_skills_context", lambda: "")
        monkeypatch.setattr(module, "extract_and_cache_pr_tickets", AsyncMock())
        monkeypatch.setattr(PRReviewer, "_load_previous_findings_context", lambda _: "")
        async def completion(**kwargs):
            if scenario == "failed_chunk":
                reviewer.review_failed_chunk_count = 1
                reviewer.review_chunk_count = 2
            elif scenario == "omitted":
                reviewer.remaining_files_list = ["omitted.py"]
            elif scenario == "truncated":
                provider.max_comment_chars = 100
            return ("review:\n  key_issues_to_review:\n"
                    "    - relevant_file: code.py\n      start_line: 2\n      end_line: 2\n"
                    "      issue_header: Wrong value\n      issue_content: Retained inline finding\n", "stop")

        model = AsyncMock(side_effect=completion)
        reviewer = PRReviewer(
            "https://forge.example/owner/repo/pulls/1", args=["-i"], is_auto=True,
            ai_handler=lambda: SimpleNamespace(chat_completion=model),
        )
        if not previous:
            from pr_agent.algo.types import FilePatchInfo
            provider.diff_files = [FilePatchInfo(
                "", "old_reviewed_change = True\nvalue = 2\n",
                "@@ -0,0 +1,2 @@\n+old_reviewed_change = True\n+value = 2\n", "code.py"
            )]
        reviewer._should_publish_review_no_suggestions = lambda _: True
        with (
            patch.object(provider, "publish_review_result", wraps=provider.publish_review_result) as publisher,
            patch.object(
                provider, "publish_inline_comments", wraps=provider.publish_inline_comments
            ) as inline_publisher,
        ):
            await reviewer.run()
        assert "Retained inline finding" in publisher.call_args.args[0]
        assert publisher.call_args.kwargs["complete"] is (scenario not in ("failed_chunk", "omitted"))
        if scenario != "truncated":
            inline_publisher.assert_called_once()
            if previous:
                provider.repo_api.get_pull_request_diff.assert_called_once()
        model.assert_awaited_once()
        model_input = model.await_args.kwargs["user"]
        assert "+value = 2" in model_input
        assert ("+old_reviewed_change" in model_input) is not previous
        assert reviewer.incremental.is_incremental is previous
        published = provider.repo_api.create_inline_comment.call_args.args[3]
        assert (_CHECKPOINT in published) is (scenario in ("first", "incremental"))
        assert ("fell back to the full PR" in published) is not previous
    finally:
        restore_settings(snapshot)
