This commit enforces the format of Launchpad bug references in commit messages. Until now, we have used a mixture of `LP: <bug_id>` and `LP#<bug_id>`. The workflow that finds the bug id expects a reference in the form of `LP: <bug_id>`. However, looking at the [Launchpad documentation](https://ubuntu.com/docs/launchpad/user/reference/comment-syntax/#mention-multiple-launchpad-bugs), the expected format is `LP: #<bug_id>`. This is also what's been recognized in terminals and IDEs.

Changes introduced by this commit include:
- Switch to the standard format for Launchpad bugs, which is `LP: #<bug_id>` (case insensitive, variable number of whitespace)
- If a closing keyword is present and mentions an LP bug, a check will be made on the format of it
- Write a comment on the LP bug with the PR link or the commit link