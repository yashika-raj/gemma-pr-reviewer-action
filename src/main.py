"""GitHub Action entrypoint for gemma-pr-reviewer-action.

Reads the workflow context from environment variables that GitHub provides
(``GITHUB_REPOSITORY``, ``GITHUB_EVENT_PATH``) plus the action inputs, which a
Docker action receives as ``INPUT_<NAME>`` variables, then runs the review.

Exit codes:
    0  review posted, nothing to review, or a non-fatal error (default)
    1  invalid configuration, or any error when ``fail_on_error`` is true
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Mapping, Optional

from github import Auth, Github
from github.GithubException import GithubException

from src import reviewer

logger = logging.getLogger("gemma-pr-reviewer")


class ConfigError(Exception):
    """Raised when the action is misconfigured."""


@dataclass(frozen=True)
class Settings:
    """Validated runtime configuration."""

    repository: str
    event_path: str
    github_token: str
    api_key: str
    model: str
    max_diff_chars: int
    fail_on_error: bool


def _first_value(env: Mapping[str, str], *names: str) -> str:
    for name in names:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return ""


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_settings(env: Mapping[str, str]) -> Settings:
    """Build :class:`Settings` from environment variables.

    Raises:
        ConfigError: if a required value is missing or malformed.
    """
    repository = _first_value(env, "GITHUB_REPOSITORY")
    if not repository:
        raise ConfigError("GITHUB_REPOSITORY is not set. Run this inside GitHub Actions.")

    event_path = _first_value(env, "GITHUB_EVENT_PATH")
    if not event_path:
        raise ConfigError("GITHUB_EVENT_PATH is not set. Run this inside GitHub Actions.")

    token = _first_value(env, "INPUT_GITHUB_TOKEN", "GITHUB_TOKEN")
    if not token:
        raise ConfigError(
            "No GitHub token found. Pass `github_token: ${{ secrets.GITHUB_TOKEN }}`."
        )

    api_key = _first_value(env, "INPUT_GEMINI_API_KEY", "GEMINI_API_KEY")
    if not api_key:
        raise ConfigError(
            "No Gemini API key found. Add a GEMINI_API_KEY repository secret and pass "
            "it as `gemini_api_key: ${{ secrets.GEMINI_API_KEY }}`. Note that secrets "
            "are not available to workflows triggered by pull requests from forks."
        )

    model = _first_value(env, "INPUT_MODEL") or reviewer.DEFAULT_MODEL

    raw_limit = _first_value(env, "INPUT_MAX_DIFF_CHARS")
    try:
        max_diff_chars = int(raw_limit) if raw_limit else reviewer.DEFAULT_MAX_DIFF_CHARS
    except ValueError as exc:
        raise ConfigError(f"`max_diff_chars` must be an integer, got {raw_limit!r}.") from exc
    if max_diff_chars < 1:
        raise ConfigError("`max_diff_chars` must be a positive integer.")

    return Settings(
        repository=repository,
        event_path=event_path,
        github_token=token,
        api_key=api_key,
        model=model,
        max_diff_chars=max_diff_chars,
        fail_on_error=_as_bool(_first_value(env, "INPUT_FAIL_ON_ERROR")),
    )


def load_event(path: str) -> dict:
    """Read and parse the GitHub event payload JSON file."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Could not read the GitHub event payload at {path}: {exc}") from exc


def get_pull_request_number(event: dict) -> int:
    """Extract the pull request number from the event payload."""
    pull_request = event.get("pull_request")
    if isinstance(pull_request, dict) and isinstance(pull_request.get("number"), int):
        return pull_request["number"]
    raise ConfigError(
        "No pull request found in the event payload. This action only supports "
        "`pull_request` and `pull_request_target` events."
    )


def _annotate(level: str, message: str) -> None:
    """Emit a GitHub Actions workflow command (error, warning or notice)."""
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::{level}::{escaped}", flush=True)


def run(env: Optional[Mapping[str, str]] = None, github_client=None) -> int:
    """Run the review and return a process exit code.

    Args:
        env: Environment mapping; defaults to :data:`os.environ`.
        github_client: Optional pre-built ``Github`` client (used by tests).
    """
    env = os.environ if env is None else env

    try:
        settings = load_settings(env)
    except ConfigError as exc:
        _annotate("error", str(exc))
        return 1

    try:
        event = load_event(settings.event_path)
        number = get_pull_request_number(event)

        client = github_client or Github(auth=Auth.Token(settings.github_token))
        pull_request = client.get_repo(settings.repository).get_pull(number)
        logger.info("Reviewing %s#%d with %s", settings.repository, number, settings.model)

        diff = reviewer.fetch_pr_diff(pull_request)
        review = reviewer.generate_review(
            diff,
            settings.api_key,
            pr_title=pull_request.title or "",
            pr_body=pull_request.body or "",
            model=settings.model,
            max_diff_chars=settings.max_diff_chars,
        )
        reviewer.post_review_comment(
            pull_request, reviewer.format_comment(review, settings.model)
        )
    except reviewer.EmptyDiffError as exc:
        _annotate("notice", f"Nothing to review: {exc}")
        return 0
    except ConfigError as exc:
        _annotate("error", str(exc))
        return 1
    except (reviewer.ReviewerError, GithubException) as exc:
        message = str(exc)
        if isinstance(exc, GithubException):
            message = f"GitHub API error (HTTP {exc.status}): {reviewer._github_message(exc)}"
        if settings.fail_on_error:
            _annotate("error", message)
            return 1
        _annotate("warning", f"{message} The pull request was not blocked.")
        return 0

    logger.info("Review complete.")
    return 0


def main() -> None:
    """Console entrypoint."""
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout
    )
    sys.exit(run())


if __name__ == "__main__":
    main()
