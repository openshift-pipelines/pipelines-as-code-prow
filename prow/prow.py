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
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

import requests  # type: ignore

LGTM_THRESHOLD = int(os.getenv("PAC_LGTM_THRESHOLD", 1))

# Error and status message templates
PERMISSION_CHECK_ERROR = """
### ⚠️ Permission Check Failed

Unable to verify permissions for user **@{user}**
* API Response Status: `{status_code}`
* This might be due to:
  * User not being a repository collaborator
  * Invalid authentication
  * Rate limiting

Please check user permissions and try again.
"""

PERMISSION_DATA_MISSING = """
### ❌ Permission Data Missing

Failed to retrieve permission level for user **@{user}**
* Received empty permission data from GitHub API
* This might indicate an API response format change
* Please contact repository administrators for assistance
"""

COMMENTS_FETCH_ERROR = """
### 🚫 Failed to Retrieve PR Comments

Unable to process LGTM votes due to API error:
* Status Code: `{status_code}`
* Response: `{response_text}`

**Troubleshooting Steps:**
1. Check your authentication token
2. Verify PR number: `{pr_num}`
3. Ensure the PR hasn't been closed or deleted
"""

SELF_APPROVAL_ERROR = """
### ⚠️ Invalid LGTM Vote

* User **@{user}** attempted to approve their own PR
* Self-approval is not permitted for security reasons
* Please [delete the comment]({comment_url}) before continuing.

Please wait for reviews from other team members.
"""

INSUFFICIENT_PERMISSIONS = """
### 🔒 Insufficient Permissions

* User **@{user}** does not have permission to merge
* Current permission level: `{permission}`
* Required permissions: `{required_permissions}`

Please request assistance from a repository maintainer.
"""

NOT_ENOUGH_LGTM = """
### ❌ Insufficient Approvals

* Current valid LGTM votes: **{valid_votes}**
* Required votes: **{threshold}**

Please obtain additional approvals before merging.
"""

MERGE_FAILED = """
### ❌ Merge Failed

Unable to merge PR #{pr_num}:
* Status Code: `{status_code}`
* Error: `{error_text}`

**Possible causes:**
* Branch protection rules not satisfied
* Merge conflicts present
* Required checks failing

Please resolve any issues and try again.
"""

HELP_TEXT = f"""
### 🤖 Available Commands
| Command                     | Description                                                                     |
| --------------------------- | ------------------------------------------------------------------------------- |
| `/assign user1 user2`       | Assigns users for review to the PR                                              |
| `/unassign user1 user2`     | Removes assigned users                                                          |
| `/label bug feature`        | Adds labels to the PR                                                           |
| `/unlabel bug feature`      | Removes labels from the PR                                                      |
| `/lgtm`                     | Approves the PR if at least {LGTM_THRESHOLD} org members have commented `/lgtm` |
| `/merge`                    | Merges the PR if it has enough `/lgtm` approvals                                |
| `/help`                     | Shows this help message                                                         |
"""

APPROVED_TEMPLATE = """
### ✅ Pull Request Approved

**Approval Status:**
* Required Approvals: {threshold}
* Current Approvals: {valid_votes}

### 👥 Approved By:
| Reviewer | Permission | Status |
|----------|------------|--------|
{users_table}

### 📝 Next Steps
* All required checks must pass
* Branch protection rules apply
* Get a maintainer to use the `/merge` command to merge the PR

Thank you for your contributions! 🎉
"""

LGTM_BREAKDOWN_TEMPLATE = """
### LGTM Vote Breakdown

* **Current valid votes:** {valid_votes}/{threshold}
* **Voting required for approval:** {threshold}

**Votes Summary:**
| Reviewer | Permission | Valid Vote |
|----------|------------|------------|
{users_table}

"""

SUCCESS_MERGED = """
### ✅ PR Successfully Merged

* Merge method: `{merge_method}`
* Merged by: **@{comment_sender}**
* Total approvals: **{valid_votes}/{lgtm_threshold}**

**Approvals Summary:**
| Reviewer | Permission | Status |
|----------|------------|--------|
{users_table}
"""


class GitHubAPI:
    """
    Wrapper for GitHub API calls to make them mockable.
    """

    def __init__(self, base_url: str, headers: Dict[str, str]):
        self.base_url = base_url
        self.headers = headers

    def get(self, endpoint: str) -> requests.Response:
        url = f"{self.base_url}/{endpoint}"
        return requests.get(url, headers=self.headers)

    def post(self, endpoint: str, data: Dict) -> requests.Response:
        url = f"{self.base_url}/{endpoint}"
        return requests.post(url, json=data, headers=self.headers)

    def put(self, endpoint: str, data: Dict) -> requests.Response:
        url = f"{self.base_url}/{endpoint}"
        return requests.put(url, json=data, headers=self.headers)

    def delete(self, endpoint: str, data: Optional[Dict] = None) -> requests.Response:
        url = f"{self.base_url}/{endpoint}"
        return requests.delete(url, json=data, headers=self.headers)


class PRHandler:
    """
    Handles PR-related operations.
    """

    def __init__(
        self,
        api: GitHubAPI,
        pr_num: str,
        pr_sender: str,
        comment_sender: str,
        lgtm_threshold: int,
        lgtm_permissions: str,
        lgtm_review_event: str,
        merge_method: str,
    ):
        self.api = api
        self.pr_num = pr_num
        self.pr_sender = pr_sender
        self.comment_sender = comment_sender
        self.lgtm_threshold = lgtm_threshold
        self.lgtm_permissions = lgtm_permissions.split(",")
        self.lgtm_review_event = lgtm_review_event
        self.merge_method = merge_method

    def post_comment(self, message: str) -> requests.Response:
        """
        Posts a comment to the pull request.
        """
        endpoint = f"issues/{self.pr_num}/comments"
        return self.api.post(endpoint, {"body": message})

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
            self.post_comment(
                f"✅ {command.capitalize()}ed <b>{', '.join(users)}</b> for reviews."
            )
        return response

    def label(self, labels: List[str]) -> requests.Response:
        """
        Adds labels to the PR.
        """
        endpoint = f"issues/{self.pr_num}/labels"
        data = {"labels": labels}
        self.post_comment(f"✅ Added labels: <b>{', '.join(labels)}</b>.")
        return self.api.post(endpoint, data)

    def unlabel(self, labels: List[str]) -> requests.Response:
        """
        Removes labels from the PR.
        """
        for label in labels:
            self.api.delete(f"issues/{self.pr_num}/labels/{label}")
        self.post_comment(f"✅ Removed labels: <b>{', '.join(labels)}</b>.")
        return requests.Response()

    def check_membership(self, user: str) -> Tuple[Optional[str], bool]:
        """
        Checks if a user has the required permissions.
        """
        endpoint = f"collaborators/{user}/permission"
        response = self.api.get(endpoint)
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
                    self.post_comment(msg)
                    print(msg, file=sys.stderr)
                    sys.exit(1)
                lgtm_users[user] = None

        valid_votes = 0
        for user in lgtm_users.keys():
            permission, is_valid = self.check_membership(user)
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
        else:
            message = NOT_ENOUGH_LGTM.format(
                valid_votes=valid_votes, threshold=self.lgtm_threshold
            )
            print(message)
            if send_comment:
                self.post_lgtm_breakdown(valid_votes, lgtm_users)
            sys.exit(0)

        return valid_votes

    def post_lgtm_breakdown(
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
        self.post_comment(message)

    def merge_pr(self) -> bool:
        """
        Merges the PR if it has enough LGTM approvals.
        """
        permission, is_valid = self.check_membership(self.comment_sender)
        if not is_valid:
            msg = INSUFFICIENT_PERMISSIONS.format(
                user=self.comment_sender,
                permission=permission,
                required_permissions=", ".join(self.lgtm_permissions),
            )
            self.post_comment(msg)
            print(msg, file=sys.stderr)
            sys.exit(1)

        valid_votes = self.lgtm(send_comment=False)
        if valid_votes >= self.lgtm_threshold:
            endpoint = f"pulls/{self.pr_num}/merge"
            data = {
                "merge_method": self.merge_method,
                "commit_title": f"Merged PR #{self.pr_num}",
                "commit_message": f"PR #{self.pr_num} merged by {self.pr_sender} with {valid_votes} LGTM votes.",
            }
            response = self.api.put(endpoint, data)
            if response and response.status_code == 200:
                # Get the LGTM breakdown for the success message
                endpoint = f"issues/{self.pr_num}/comments"
                comments = self.api.get(endpoint).json()
                lgtm_users: Dict[str, Optional[str]] = {}

                for comment in comments:
                    body = comment.get("body", "")
                    if re.search(r"^/lgtm\b", body, re.IGNORECASE):
                        user = comment["user"]["login"]
                        if user != self.pr_sender:  # Skip self-approvals
                            lgtm_users[user] = None

                # Get permissions for all LGTM users
                for user in lgtm_users.keys():
                    permission, _ = self.check_membership(user)
                    lgtm_users[user] = permission

                # Create the users table for the message
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
                self.post_comment(success_message)
                return True
            else:
                self.post_comment(
                    MERGE_FAILED.format(
                        pr_num=self.pr_num,
                        status_code=response.status_code,
                        error_text=response.text,
                    ),
                )
                return False
        else:
            self.post_comment(
                NOT_ENOUGH_LGTM.format(
                    valid_votes=valid_votes, threshold=self.lgtm_threshold
                ),
            )
            return False


def main():
    # Load environment variables
    lgtm_threshold = int(LGTM_THRESHOLD)
    lgtm_permissions = os.getenv("PAC_LGTM_PERMISSIONS", "admin,write")
    lgtm_review_event = os.getenv("PAC_LGTM_REVIEW_EVENT", "APPROVE")
    github_token = os.getenv("GITHUB_TOKEN")
    pr_num = os.getenv("GH_PR_NUM")
    pr_sender = os.getenv("GH_PR_SENDER")
    comment_sender = os.getenv("GH_COMMENT_SENDER")
    repo_owner = os.getenv("GH_REPO_OWNER")
    repo_name = os.getenv("GH_REPO_NAME")
    merge_method = os.getenv("GH_MERGE_METHOD", "rebase")
    trigger_comment = os.getenv("PAC_TRIGGER_COMMENT", "")

    # Initialize GitHub API and PR handler
    api_base = f"https://api.github.com/repos/{repo_owner}/{repo_name}"
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    api = GitHubAPI(api_base, headers)
    pr_handler = PRHandler(
        api,
        pr_num,
        pr_sender,
        comment_sender,
        lgtm_threshold,
        lgtm_permissions,
        lgtm_review_event,
        merge_method,
    )

    # Parse and handle the command
    match = re.match(
        r"^/(merge|assign|unassign|label|unlabel|lgtm|help)\s*(.*)", trigger_comment
    )
    if not match:
        print(
            f"⚠️ No valid command found in comment: {trigger_comment}", file=sys.stderr
        )
        sys.exit(1)

    command, values = match.groups()
    values = values.split()

    if command == "assign" or command == "unassign":
        pr_handler.assign_unassign(command, values)
    elif command == "label":
        pr_handler.label(values)
    elif command == "unlabel":
        pr_handler.unlabel(values)
    elif command == "lgtm":
        pr_handler.lgtm()
    elif command == "merge":
        pr_handler.merge_pr()
    elif command == "help":
        pr_handler.post_comment(HELP_TEXT.strip())


if __name__ == "__main__":
    main()
