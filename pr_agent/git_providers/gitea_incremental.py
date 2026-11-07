"""Gitea review checkpoints and immutable net commit-delta patches.

Only complete linear commit chains are incremental; merges trigger a full PR review.
"""
import datetime
import difflib
import io
import json
import re
from types import SimpleNamespace
from urllib.parse import quote

from pr_agent.algo.file_filter import filter_ignored
from pr_agent.algo.git_patch_processing import to_hunk_only_patch
from pr_agent.algo.language_handler import is_valid_file
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.config_loader import get_settings
from pr_agent.mosaico.diff_provider import parse_unified_diff

_CHECKPOINT = "<!-- pr-agent:gitea-review-v1 "
_SHA = re.compile(r"[0-9a-f]{40,64}\Z")


class GiteaIncrementalMixin:
    def get_incremental_commits(self, incremental):
        self.incremental = incremental
        incremental.commits_range = None
        incremental.first_new_commit = None
        incremental.last_seen_commit = None
        self.incremental_fallback_reason = "no trusted completed review"
        self._incremental_diff_files = None
        try:
            if not _SHA.fullmatch(self.sha) or not _SHA.fullmatch(self.base_sha):
                raise ValueError("Missing immutable PR refs")
            identity = self.repo_api.incremental_json("/user")
            user_id = identity.get("id")
            if not isinstance(user_id, int) or isinstance(user_id, bool):
                raise ValueError("Authenticated identity unavailable")
            reviews = self.repo_api.incremental_pages(
                f"{self._incremental_repo_path}/pulls/{self.pr_number}/reviews"
            )
            anchors = []
            for review in reviews:
                if (review.get("user", {}).get("id") != user_id
                        or review.get("state") != "COMMENT" or not review.get("submitted_at")
                        or review.get("dismissed")):
                    continue
                body = review.get("body", "")
                if not isinstance(body, str) or _CHECKPOINT not in body:
                    continue
                try:
                    marker = json.loads(body.rsplit(_CHECKPOINT, 1)[1].split(" -->", 1)[0])
                except (ValueError, IndexError):
                    continue
                if (marker.get("head") != review.get("commit_id")
                        or marker.get("base") != self.base_sha
                        or not isinstance(marker.get("head"), str)
                        or not _SHA.fullmatch(marker["head"])):
                    continue
                anchors.append((review["id"], marker["head"]))
            if not anchors:
                return
            anchor = max(anchors)[1]
            if anchor == self.sha:
                incremental.commits_range = []
                self.unreviewed_files_map = {}
                return
            compare = self.repo_api.incremental_json(
                f"{self._incremental_repo_path}/compare/{anchor}...{self.sha}"
            )
            commits = compare.get("commits")
            if not isinstance(commits, list) or not commits or compare.get("total_commits") != len(commits):
                raise ValueError("Incomplete comparison metadata")
            # Verify ancestry by parent identity, never timestamps or API ordering.
            by_sha = {commit["sha"]: commit for commit in commits}
            chain = []
            cursor = self.sha
            while cursor != anchor:
                commit = by_sha.pop(cursor)
                parents = commit.get("parents")
                if not isinstance(parents, list) or len(parents) != 1:
                    raise ValueError("Nonlinear comparison history")
                chain.append(commit)
                cursor = parents[0]["sha"]
            if by_sha:
                raise ValueError("Comparison includes unrelated commits")
            chain.reverse()
            files = self._build_incremental_files(anchor, chain)
            if get_settings().pr_reviewer.minimal_minutes_for_incremental_review:
                previous = self.repo_api.incremental_json(f"{self._incremental_repo_path}/git/commits/{anchor}")
                date = datetime.datetime.fromisoformat(previous["commit"]["author"]["date"])
                incremental.last_seen_commit = SimpleNamespace(
                    sha=anchor, commit=SimpleNamespace(author=SimpleNamespace(date=date))
                )
            incremental.commits_range = [SimpleNamespace(sha=c["sha"]) for c in chain]
            incremental.first_new_commit = incremental.commits_range[0]
            self._incremental_diff_files = files
            self.unreviewed_files_map = {file.filename: file.patch for file in files}
            self.incremental_fallback_reason = ""
        except Exception:
            # API errors may contain private source or credentials: do not log their bodies.
            self.incremental_fallback_reason = "comparison metadata or file contents unavailable/nonlinear"
            self.logger.warning("Gitea incremental review unavailable; falling back to full PR review")

    def _build_incremental_files(self, anchor, chain):
        paths = {}
        for commit in chain:
            files = commit.get("files")
            if not isinstance(files, list):
                raise ValueError("Missing commit file metadata")
            for file in files:
                path, status = file.get("filename"), file.get("status")
                if not isinstance(path, str) or not path or status not in ("added", "removed", "modified"):
                    raise ValueError("Unsupported commit file metadata")
                initial, _ = paths.get(path, (status != "added", None))
                paths[path] = (initial, status != "removed")
        result = []
        allowed = filter_ignored([{"filename": path} for path in paths], platform="gitea")
        for path in sorted(file["filename"] for file in allowed):
            exists_before, exists_after = paths[path]
            if not is_valid_file(path):
                continue
            before = self._incremental_content(anchor, path) if exists_before else ""
            after = self._incremental_content(self.sha, path) if exists_after else ""
            if before == after and exists_before == exists_after:
                continue
            lines = difflib.unified_diff(
                list(io.StringIO(before, newline="\n")), list(io.StringIO(after, newline="\n")),
                fromfile=f"a/{path}", tofile=f"b/{path}",
            )
            patch = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                            for line in lines)
            edit = EDIT_TYPE.MODIFIED if exists_before and exists_after else (
                EDIT_TYPE.ADDED if exists_after else EDIT_TYPE.DELETED
            )
            result.append(FilePatchInfo(before, after, to_hunk_only_patch(patch), path, edit_type=edit))
        return result

    @property
    def _incremental_repo_path(self):
        return f"/repos/{quote(self.owner, safe='')}/{quote(self.repo, safe='')}"

    def _incremental_content(self, ref, path):
        raw = self.repo_api.incremental_bytes(
            f"{self._incremental_repo_path}/raw/{quote(path, safe='/')}", [("ref", ref)]
        )
        # Refuse lossy decoding: a non-UTF-8 delta uses the normal full-review path.
        return raw.decode("utf-8")

    def publish_review_result(self, body: str, *, complete: bool) -> bool:
        """Atomically publish the review and checkpoint as a submitted native review."""
        current = self.repo_api.get_pull_request(self.owner, self.repo, self.pr_number)
        if current.head.sha != self.sha or current.base.sha != self.base_sha:
            raise RuntimeError("Gitea PR changed during review; retry on the current head")
        # Model output is not checkpoint metadata, especially when a partial review is published.
        body = self._strip_review_checkpoint(body)
        reason = getattr(self, "incremental_fallback_reason", "")
        if reason:
            body += f"\n\n_Incremental review fell back to the full PR: {reason}._"
        marker = ""
        if complete:
            marker = "\n\n" + _CHECKPOINT + json.dumps({"head": self.sha, "base": self.base_sha}) + " -->"
        if len(body) + len(marker) > self.max_comment_chars:
            marker = ""  # A truncated published result is not a complete review checkpoint.
        body = self.limit_output_characters(body, self.max_comment_chars - len(marker)) + marker
        response = self.repo_api.create_inline_comment(
            self.owner, self.repo, self.pr_number, body, self.sha, []
        )
        if not response:
            raise RuntimeError("Gitea review publication failed")
        return True

    @staticmethod
    def _strip_review_checkpoint(body):
        return body.replace(_CHECKPOINT, "<!-- pr-agent:untrusted-review-marker ")

    def _incremental_inline_comments_valid(self, comments):
        """Anchor only new-side lines present in the full PR diff, not old delta coordinates."""
        if not getattr(getattr(self, "incremental", None), "is_incremental", False):
            return True
        current = self.repo_api.get_pull_request(self.owner, self.repo, self.pr_number)
        if current.head.sha != self.sha or current.base.sha != self.base_sha:
            return False
        full_diff = self.repo_api.get_pull_request_diff(self.owner, self.repo, self.pr_number)
        current = self.repo_api.get_pull_request(self.owner, self.repo, self.pr_number)
        if current.head.sha != self.sha or current.base.sha != self.base_sha:
            return False
        positions = {}
        for file in parse_unified_diff(full_diff):
            lines = set()
            new_line = None
            for line in file.patch.splitlines():
                if line.startswith("@@"):
                    match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
                    new_line = int(match[1]) if match else None
                elif new_line is not None and line.startswith(("+", " ")):
                    lines.add(new_line)
                    new_line += 1
            positions[file.filename] = lines
        return all(comment.get("old_position", 0) == 0
                   and comment.get("new_position") in positions.get(comment.get("path"), set())
                   for comment in comments)
