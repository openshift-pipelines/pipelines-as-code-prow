#!/usr/bin/env python3
# Copyright 2025 Red Hat, Inc.
# Author: Chmouel Boudjnah <chmouel@redhat.com>
#
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "requests",
# ]
# ///
import argparse
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

import requests  # type: ignore

from .messages import (  # isort:skip
    APPROVED_TEMPLATE,
    COMMENTS_FETCH_ERROR,
    HELP_TEXT,
    INSUFFICIENT_PERMISSIONS,
    LGTM_BREAKDOWN_TEMPLATE,
    MERGE_FAILED,
    NOT_ENOUGH_LGTM,
    PERMISSION_CHECK_ERROR,
    PERMISSION_DATA_MISSING,
    SELF_APPROVAL_ERROR,
    SUCCESS_MERGED,
    CHERRY_PICK_ERROR,
    CHERRY_PICK_SUCCESS,
    CHERRY_PICK_CONFLICT,
)


class GitHubAPI:
    """
    Wrapper for GitHub API calls to make them mockable.
    """

    timeout: int = 10

    def __init__(self, base_url: str, headers: Dict[str, str]):
        self.base_url = base_url
        self.headers = headers

    def get(self, endpoint: str) -> requests.Response:
        url = f"{self.base_url}/{endpoint}"
        return requests.get(url, headers=self.headers, timeout=self.timeout)

    def post(self, endpoint: str, data: Dict) -> requests.Response:
        url = f"{self.base_url}/{endpoint}"
        return requests.post(url, json=data, headers=self.headers, timeout=self.timeout)

    def put(self, endpoint: str, data: Dict) -> requests.Response:
        url = f"{self.base_url}/{endpoint}"
        return requests.put(url, json=data, headers=self.headers, timeout=self.timeout)

    def delete(self, endpoint: str, data: Optional[Dict] = None) -> requests.Response:
        url = f"{self.base_url}/{endpoint}"
        return requests.delete(
            url, json=data, headers=self.headers, timeout=self.timeout
        )


class PRHandler:  # pylint: disable=too-many-instance-attributes
    """
    Handles PR-related operations.
    """

    def __init__(
        self,
        api: GitHubAPI,
        args: argparse.Namespace,
    ):
        self.api = api
        self.pr_num = args.pr_num
        self.pr_sender = args.pr_sender
        self.comment_sender = args.comment_sender
        self.lgtm_threshold = args.lgtm_threshold
        self.lgtm_permissions = args.lgtm_permissions.split(",")
        self.lgtm_review_event = args.lgtm_review_event
        self.merge_method = args.merge_method

        self._pr_status = None

    def check_response(self, resp: requests.Response) -> bool:
        """
        Checks the response status code and prints an error message if needed.
        """
        if resp.status_code >= 200 and resp.status_code < 300:
            return True
        print(
            f"Error while executing the command: status: {resp.status_code} {resp.text}",
            file=sys.stderr,
        )
        return False

    def _post_comment(self, message: str) -> requests.Response:
        """
        Posts a comment to the pull request.
        """
        endpoint = f"issues/{self.pr_num}/comments"
        return self.api.post(endpoint, {"body": message})

    def _fetch_and_validate_lgtm_votes(self):
        """
        Fetches LGTM votes and validates them.

        Returns the number of valid votes and a dictionary of users with their
        permissions.
        """
        endpoint = f"issues/{self.pr_num}/comments"
        response = self.api.get(endpoint)
        if response.status_code != 200:
            error_message = COMMENTS_FETCH_ERROR.format(
                status_code=response.status_code,
                response_text=response.text,
                pr_num=self.pr_num,
            )
            print(error_message, file=sys.stderr)
            sys.exit(1)

        comments = response.json()
        lgtm_users: Dict[str, Optional[str]] = {}
        for comment in comments:
            body = comment.get("body", "")
            if re.search(r"^/lgtm\b", body, re.IGNORECASE):
                user = comment["user"]["login"]
                if user == self.pr_sender:
                    msg = SELF_APPROVAL_ERROR.format(
                        user=user, comment_url=comment["html_url"]
                    )
                    self._post_comment(msg)
                    print(msg, file=sys.stderr)
                    sys.exit(1)
                lgtm_users[user] = None

        valid_votes = 0
        for user in lgtm_users:
            permission, is_valid = self._check_membership(user)
            lgtm_users[user] = permission
            if is_valid:
                valid_votes += 1

        return valid_votes, lgtm_users

    def _check_membership(self, user: str) -> Tuple[Optional[str], bool]:
        """
        Checks if a user has the required permissions.
        """
        endpoint = f"collaborators/{user}/permission"
        response = self.api.get(endpoint)
        if response.status_code == 404:  # Handle 404 for missing collaborator
            return None, False
        if response.status_code != 200:
            print(
                PERMISSION_CHECK_ERROR.format(
                    user=user, status_code=response.status_code
                ),
                file=sys.stderr,
            )
            return None, False

        permission = response.json().get("permission")
        if not permission:
            print(
                PERMISSION_DATA_MISSING.format(user=user),
                file=sys.stderr,
            )
            return None, False

        return permission, permission in self.lgtm_permissions

    def _get_pr_commits(self, pr_num: int) -> List[Dict]:
        """
        Fetches all commits from a pull request.
        """
        endpoint = f"pulls/{pr_num}/commits"
        response = self.api.get(endpoint)
        if response.status_code != 200:
            return []
        return response.json()

    def _post_lgtm_breakdown(
        self, valid_votes: int, lgtm_users: Dict[str, Optional[str]]
    ) -> None:
        """
        Posts a detailed breakdown of LGTM votes.
        """
        users_table = ""
        for user, permission in lgtm_users.items():
            is_valid = permission in self.lgtm_permissions
            valid_mark = "✅" if is_valid else "❌"
            users_table += f"| @{user} | `{permission or 'none'}` | {valid_mark} |\n"

        message = LGTM_BREAKDOWN_TEMPLATE.format(
            valid_votes=valid_votes,
            threshold=self.lgtm_threshold,
            users_table=users_table,
        )
        self._post_comment(message)

    def _get_branch_sha(self, branch: str) -> Optional[str]:
        """
        Gets the SHA of the latest commit on a branch.
        """
        endpoint = f"git/refs/heads/{branch}"
        response = self.api.get(endpoint)
        if response.status_code != 200:
            return None
        return response.json().get("object", {}).get("sha")

    def _create_branch(self, branch_name: str, base_sha: str) -> bool:
        """
        Creates a new branch from the specified SHA.
        """
        endpoint = "git/refs"
        data = {"ref": f"refs/heads/{branch_name}", "sha": base_sha}
        response = self.api.post(endpoint, data)

        return response.status_code == 201

    def _get_pr_status(self, number: int) -> requests.Response:
        """
        Fetches the status of a pull request.
        """
        if self._pr_status:
            return self._pr_status

        endpoint = f"pulls/{number}"
        self._pr_status = self.api.get(endpoint)
        return self._pr_status

    def check_status(self, num: int, status: str) -> bool:
        pr_status = self._get_pr_status(num)
        if pr_status.status_code != 200:
            print(
                f"⚠️ Unable to fetch PR status for PR #{num}: {pr_status.text}",
                file=sys.stderr,
            )
            sys.exit(1)
        return pr_status.json().get("state") == status

    def assign_unassign(self, command: str, users: List[str]) -> requests.Response:
        """
        Assigns or unassigns users for review.
        """
        endpoint = f"pulls/{self.pr_num}/requested_reviewers"
        users = [user.lstrip("@") for user in users]
        data = {"reviewers": users}
        method = self.api.post if command == "assign" else self.api.delete
        response = method(endpoint, data)
        if response and response.status_code in [200, 201, 204]:
            self._post_comment(
                f"✅ {command.capitalize()}ed <b>{', '.join(users)}</b> for reviews."
            )
        return response

    def label(self, labels: List[str]) -> requests.Response:
        """
        Adds labels to the PR.
        """
        endpoint = f"issues/{self.pr_num}/labels"
        data = {"labels": labels}
        self._post_comment(f"✅ Added labels: <b>{', '.join(labels)}</b>.")
        return self.api.post(endpoint, data)

    def unlabel(self, labels: List[str]) -> requests.Response:
        """
        Removes labels from the PR.
        """
        for label in labels:
            self.api.delete(f"issues/{self.pr_num}/labels/{label}")
        self._post_comment(f"✅ Removed labels: <b>{', '.join(labels)}</b>.")
        return requests.Response()

    def cherry_pick(self, values: List[str]) -> requests.Response:
        """
        Posts a comment indicating the PR will be cherry-picked to the specified branch.
        """
        if len(values) != 1:
            print(
                f"⚠️ Invalid number of arguments for cherry-pick: {values}",
                file=sys.stderr,
            )
            sys.exit(1)

        target_branch = values[0]
        self._post_comment(
            f"✅ We will cherry-pick this PR to the branch `{target_branch}` upon merge."
        )
        return requests.Response()

    def rebase(self) -> requests.Response:
        endpoint = f"pulls/{self.pr_num}/update-branch"
        self._post_comment("✅ Rebased the PR branch on the base branch.")
        return self.api.put(endpoint, {})

    def lgtm(self, send_comment: bool = True) -> int:
        """
        Processes LGTM votes and approves the PR if the threshold is met.
        """
        endpoint = f"issues/{self.pr_num}/comments"
        response = self.api.get(endpoint)
        if response.status_code != 200:
            error_message = COMMENTS_FETCH_ERROR.format(
                status_code=response.status_code,
                response_text=response.text,
                pr_num=self.pr_num,
            )
            print(error_message, file=sys.stderr)
            sys.exit(1)

        comments = response.json()
        lgtm_users: Dict[str, Optional[str]] = {}
        for comment in comments:
            body = comment.get("body", "")
            if re.search(r"^/lgtm\b", body, re.IGNORECASE):
                user = comment["user"]["login"]
                if user == self.pr_sender:
                    msg = SELF_APPROVAL_ERROR.format(
                        user=user, comment_url=comment["html_url"]
                    )
                    self._post_comment(msg)
                    print(msg, file=sys.stderr)
                    sys.exit(1)
                lgtm_users[user] = None

        valid_votes = 0
        for user in lgtm_users:
            permission, is_valid = self._check_membership(user)
            lgtm_users[user] = permission
            if is_valid:
                valid_votes += 1

        if valid_votes >= self.lgtm_threshold:
            users_table = ""
            for user, permission in lgtm_users.items():
                is_valid = permission in self.lgtm_permissions
                valid_mark = "✅" if is_valid else "❌"
                users_table += (
                    f"| @{user} | `{permission or 'none'}` | {valid_mark} |\n"
                )
            endpoint = f"pulls/{self.pr_num}/reviews"
            body = APPROVED_TEMPLATE.format(
                threshold=self.lgtm_threshold,
                valid_votes=valid_votes,
                users_table=users_table,
            )
            data = {"event": self.lgtm_review_event, "body": body}
            print("✅ PR approved with LGTM votes.")
            self.api.post(endpoint, data)
            return valid_votes

        message = NOT_ENOUGH_LGTM.format(
            valid_votes=valid_votes, threshold=self.lgtm_threshold
        )
        print(message)
        if send_comment:
            self._post_lgtm_breakdown(valid_votes, lgtm_users)
        sys.exit(0)

    def merge_pr(self) -> bool:
        """
        Merges the PR if it has enough LGTM approvals and performs cherry-picks.
        """
        # Check if the user has sufficient permissions to merge
        permission, is_valid = self._check_membership(self.comment_sender)
        if not is_valid:
            msg = INSUFFICIENT_PERMISSIONS.format(
                user=self.comment_sender,
                permission=permission,
                required_permissions=", ".join(self.lgtm_permissions),
            )
            self._post_comment(msg)
            print(msg, file=sys.stderr)
            sys.exit(1)

        # Fetch LGTM votes and check if the threshold is met
        valid_votes, lgtm_users = self._fetch_and_validate_lgtm_votes()

        if valid_votes >= self.lgtm_threshold:
            endpoint = f"pulls/{self.pr_num}/merge"
            data = {
                "merge_method": self.merge_method,
                "commit_title": f"Merged PR #{self.pr_num}",
                "commit_message": f"PR #{self.pr_num} merged by {self.pr_sender} with {valid_votes} LGTM votes.",
            }
            response = self.api.put(endpoint, data)
            if response and response.status_code == 200:
                # Fetch all comments to find cherry-pick commands
                comments_response = self.api.get(f"issues/{self.pr_num}/comments")
                if comments_response.status_code != 200:
                    print(
                        f"⚠️ Unable to fetch comments for PR #{self.pr_num}: {comments_response.text}",
                        file=sys.stderr,
                    )
                    sys.exit(1)

                comments = comments_response.json()
                cherry_pick_branches = set()
                for comment in comments:
                    body = comment.get("body", "")
                    match = re.match(r"^/cherry-pick\s+(\S+)", body, re.IGNORECASE)
                    if match:
                        cherry_pick_branches.add(match.group(1))

                # Perform cherry-picks to the specified branches
                for target_branch in cherry_pick_branches:
                    if not self._perform_cherry_pick(target_branch):
                        return False

                # Create the users table for the success message
                users_table = ""
                for user, permission in lgtm_users.items():
                    is_valid = permission in self.lgtm_permissions
                    valid_mark = "✅" if is_valid else "❌"
                    users_table += (
                        f"| @{user} | `{permission or 'unknown'}` | {valid_mark} |\n"
                    )

                success_message = SUCCESS_MERGED.format(
                    merge_method=self.merge_method,
                    comment_sender=self.comment_sender,
                    valid_votes=valid_votes,
                    lgtm_threshold=self.lgtm_threshold,
                    users_table=users_table,
                )
                self._post_comment(success_message)
                return True

            self._post_comment(
                MERGE_FAILED.format(
                    pr_num=self.pr_num,
                    status_code=response.status_code,
                    error_text=response.text,
                ),
            )
            return False

        self._post_comment(
            NOT_ENOUGH_LGTM.format(
                valid_votes=valid_votes, threshold=self.lgtm_threshold
            ),
        )
        return False

    def _perform_cherry_pick(self, target_branch: str) -> bool:
        """
        Performs cherry-pick operation to the specified branch.
        """
        # Get all PR commits in chronological order
        commits = self._get_pr_commits(self.pr_num)
        if not commits:
            self._post_comment(
                CHERRY_PICK_ERROR.format(
                    source_pr=self.pr_num,
                    target_branch=target_branch,
                    status_code="404",
                    error_text="Could not fetch PR commits",
                )
            )
            return False

        # Check if target branch exists
        current_sha = self._get_branch_sha(target_branch)
        if not current_sha:
            # Handle new branch creation
            pr_info = self._get_pr_status(self.pr_num).json()
            base_branch = pr_info["base"]["ref"]
            base_sha = self._get_branch_sha(base_branch)

            if not base_sha:
                self._post_comment(
                    CHERRY_PICK_ERROR.format(
                        source_pr=self.pr_num,
                        target_branch=target_branch,
                        status_code="404",
                        error_text=f"Could not find base branch: {base_branch}",
                    )
                )
                return False

            if not self._create_branch(target_branch, base_sha):
                self._post_comment(
                    CHERRY_PICK_ERROR.format(
                        source_pr=self.pr_num,
                        target_branch=target_branch,
                        status_code="422",
                        error_text=f"Could not create branch: {target_branch}",
                    )
                )
                return False
            current_sha = base_sha

        # Cherry-pick each commit in sequence
        for i, commit in enumerate(commits, 1):
            endpoint = "merges"
            commit_sha = commit["sha"]
            commit_msg = commit.get("commit", {}).get("message", "")

            data = {
                "base": target_branch,
                "head": commit_sha,
                "commit_message": (
                    f"Cherry-pick: ({i}/{len(commits)}) from PR #{self.pr_num} to {target_branch}\n\n"
                    f"Original commit: {commit_msg}\n"
                    f"Cherry-picked by @{self.comment_sender}"
                ),
            }

            response = self.api.post(endpoint, data)

            if response.status_code == 409:
                # Merge conflict - requires manual intervention
                self._handle_merge_conflict(target_branch, commit_sha, i, len(commits))
                return False  # Indicate cherry-pick was not completed

            if response.status_code != 201:
                self._post_comment(
                    CHERRY_PICK_ERROR.format(
                        source_pr=self.pr_num,
                        target_branch=target_branch,
                        status_code=response.status_code,
                        error_text=response.text,
                    )
                )
                return False  # Indicate cherry-pick was not completed

            # Store new SHA but keep target_branch name unchanged
            current_sha = response.json()["sha"]

        # All commits successfully cherry-picked
        self._post_comment(
            CHERRY_PICK_SUCCESS.format(
                source_pr=self.pr_num,
                target_branch=target_branch,
                user=self.comment_sender,
                commit_sha=current_sha,  # Use final SHA in success message
            )
        )
        return True

    def _handle_merge_conflict(
        self,
        target_branch: str,
        commit_sha: str,
        current_commit: int,
        total_commits: int,
    ) -> None:
        """
        Handles merge conflicts during cherry-pick operation.

        Posts detailed information and instructions for manual resolution.
        """
        conflict_message = CHERRY_PICK_CONFLICT.format(
            pr_num=self.pr_num,
            target_branch=target_branch,
            current_commit=current_commit,
            total_commits=total_commits,
            commit_sha=commit_sha,
        )
        self._post_comment(conflict_message)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manage prow-like commands on a GitHub PullRequest.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,  # Show default values in help
    )
    # LGTM threshold argument
    parser.add_argument(
        "--lgtm-threshold",
        default=int(os.getenv("PAC_LGTM_THRESHOLD", "1")),  # Default as string
        type=int,
        help="Minimum number of LGTM approvals required to merge a PR. "
        "Can be overridden via the PAC_LGTM_THRESHOLD environment variable.",
    )
    # LGTM permissions argument
    parser.add_argument(
        "--lgtm-permissions",
        default=os.getenv("PAC_LGTM_PERMISSIONS", "admin,write"),
        help="Comma-separated list of GitHub permissions required to give a valid LGTM. "
        "Can be overridden via the PAC_LGTM_PERMISSIONS environment variable.",
    )
    # LGTM review event argument
    parser.add_argument(
        "--lgtm-review-event",
        default=os.getenv("PAC_LGTM_REVIEW_EVENT", "APPROVE"),
        help="The type of review event to trigger when an LGTM is given. "
        "Can be overridden via the PAC_LGTM_REVIEW_EVENT environment variable.",
    )
    # Merge method argument
    parser.add_argument(
        "--merge-method",
        default=os.getenv("GH_MERGE_METHOD", "rebase"),
        help="The method to use when merging the pull request. "
        "Options: 'merge', 'rebase', or 'squash'. "
        "Can be overridden via the GH_MERGE_METHOD environment variable.",
    )
    # GitHub token argument
    parser.add_argument(
        "--github-token",
        default=os.getenv("GITHUB_TOKEN"),
        help="GitHub API token for authentication. "
        "Required if the GITHUB_TOKEN environment variable is not set.",
    )
    # PR number argument
    parser.add_argument(
        "--pr-num",
        default=os.getenv("GH_PR_NUM"),
        help="The number of the pull request to operate on. "
        "Can be overridden via the GH_PR_NUM environment variable.",
    )
    # PR sender argument
    parser.add_argument(
        "--pr-sender",
        default=os.getenv("GH_PR_SENDER"),
        help="The GitHub username of the user who opened the pull request. "
        "Can be overridden via the GH_PR_SENDER environment variable.",
    )
    # Comment sender argument
    parser.add_argument(
        "--comment-sender",
        default=os.getenv("GH_COMMENT_SENDER"),
        help="The GitHub username of the user who triggered the command. "
        "Can be overridden via the GH_COMMENT_SENDER environment variable.",
    )
    # Repository owner argument
    parser.add_argument(
        "--repo-owner",
        default=os.getenv("GH_REPO_OWNER"),
        help="The owner (organization or user) of the GitHub repository. "
        "Can be overridden via the GH_REPO_OWNER environment variable.",
    )
    # Repository name argument
    parser.add_argument(
        "--repo-name",
        default=os.getenv("GH_REPO_NAME"),
        help="The name of the GitHub repository. "
        "Can be overridden via the GH_REPO_NAME environment variable.",
    )
    # Trigger comment argument
    parser.add_argument(
        "--trigger-comment",
        default=os.getenv("PAC_TRIGGER_COMMENT"),
        help="The comment that triggered this command. "
        "Can be overridden via the PAC_TRIGGER_COMMENT environment variable.",
    )
    parsed = parser.parse_args()
    if not parsed.github_token:
        parser.error(
            "GitHub API token is required. Use --github-token or GITHUB_TOKEN env variable."
        )
    if not parsed.pr_num:
        parser.error("PR number is required. Use --pr-num or GH_PR_NUM env variable.")
    if not parsed.pr_sender:
        parser.error(
            "PR sender is required. Use --pr-sender or GH_PR_SENDER env variable."
        )
    if not parsed.comment_sender:
        parser.error(
            "Comment sender is required. Use --comment-sender or GH_COMMENT_SENDER env variable."
        )
    if not parsed.repo_owner:
        parser.error(
            "Repository owner is required. Use --repo-owner or GH_REPO_OWNER env variable."
        )
    if not parsed.repo_name:
        parser.error(
            "Repository name is required. Use --repo-name or GH_REPO_NAME env variable."
        )
    if not parsed.trigger_comment:
        parser.error(
            "Trigger comment is required. Use --trigger-comment or PAC_TRIGGER_COMMENT env variable."
        )
    return parsed


def main():
    args = parse_args()
    # Initialize GitHub API and PR handler
    api_base = f"https://api.github.com/repos/{args.repo_owner}/{args.repo_name}"
    headers = {
        "Authorization": f"Bearer {args.github_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    api = GitHubAPI(api_base, headers)
    pr_handler = PRHandler(api, args)

    match = re.match(
        r"^/(rebase|cherry-pick|merge|assign|unassign|label|unlabel|lgtm|help)\s*(.*)",
        args.trigger_comment,
    )
    if not match:
        print(
            f"⚠️ No valid command found in comment: {args.trigger_comment}",
            file=sys.stderr,
        )
        sys.exit(1)

    command, values = match.groups()
    values = values.split()

    if not pr_handler.check_status(args.pr_num, "open"):
        print(f"⚠️ PR #{args.pr_num} is not open.", file=sys.stderr)
        sys.exit(1)

    response = None
    if command in ("assign", "unassign"):
        response = pr_handler.assign_unassign(command, values)
    elif command == "label":
        response = pr_handler.label(values)
    elif command == "unlabel":
        response = pr_handler.unlabel(values)
    elif command == "rebase":
        response = pr_handler.rebase()
    elif command == "help":
        response = pr_handler._post_comment(HELP_TEXT.strip())
    elif command == "lgtm":
        pr_handler.lgtm()
    elif command == "merge":
        pr_handler.merge_pr()
    elif command == "cherry-pick":
        pr_handler.cherry_pick(values)

    if response:
        if not pr_handler.check_response(response):
            sys.exit(1)


if __name__ == "__main__":
    main()
