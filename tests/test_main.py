"""End-to-end tests for src/main.py with GitHub and Gemini fully mocked."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from google.genai import errors as genai_errors

from src import main, reviewer

PATCH = "@@ -1 +1,2 @@\n-x = 1\n+x = 2\n+password = 'hunter2'"


@pytest.fixture
def event_file(tmp_path):
    path = tmp_path / "event.json"
    path.write_text(json.dumps({"action": "opened", "pull_request": {"number": 7}}))
    return path


@pytest.fixture
def env(event_file):
    return {
        "GITHUB_REPOSITORY": "octo/demo",
        "GITHUB_EVENT_PATH": str(event_file),
        "INPUT_GITHUB_TOKEN": "gh-token",
        "INPUT_GEMINI_API_KEY": "gemini-key",
    }


def make_github(files=None, existing_comments=None):
    pull_request = MagicMock()
    pull_request.title = "Tighten config"
    pull_request.body = None
    pull_request.get_files.return_value = (
        files
        if files is not None
        else [SimpleNamespace(filename="cfg.py", patch=PATCH, status="modified", previous_filename=None)]
    )
    pull_request.get_issue_comments.return_value = existing_comments or []
    client = MagicMock()
    client.get_repo.return_value.get_pull.return_value = pull_request
    return client, pull_request


@pytest.fixture
def gemini(monkeypatch):
    """Patch genai.Client and return the fake client instance."""
    fake = MagicMock()
    fake.models.generate_content.return_value = SimpleNamespace(
        text="## 🔐 Security\n🔴 High: hardcoded password"
    )
    factory = MagicMock(return_value=fake)
    monkeypatch.setattr(reviewer.genai, "Client", factory)
    fake.factory = factory
    return fake


def test_happy_path_posts_review_comment(env, gemini):
    github, pr = make_github()

    assert main.run(env, github_client=github) == 0

    github.get_repo.assert_called_once_with("octo/demo")
    github.get_repo.return_value.get_pull.assert_called_once_with(7)
    gemini.factory.assert_called_once_with(api_key="gemini-key")
    prompt = gemini.models.generate_content.call_args.kwargs["contents"]
    assert "hunter2" in prompt and "Tighten config" in prompt
    (body,), _ = pr.create_issue_comment.call_args
    assert reviewer.COMMENT_MARKER in body
    assert "hardcoded password" in body


def test_model_and_limit_inputs_are_respected(env, gemini):
    github, _ = make_github()
    env.update({"INPUT_MODEL": "gemini-2.5-pro", "INPUT_MAX_DIFF_CHARS": "50000"})

    assert main.run(env, github_client=github) == 0

    assert gemini.models.generate_content.call_args.kwargs["model"] == "gemini-2.5-pro"


def test_missing_api_key_fails_with_error_annotation(env, capsys):
    env["INPUT_GEMINI_API_KEY"] = ""

    assert main.run(env, github_client=MagicMock()) == 1

    assert "::error::" in capsys.readouterr().out


def test_missing_repository_fails(env, capsys):
    del env["GITHUB_REPOSITORY"]

    assert main.run(env, github_client=MagicMock()) == 1
    assert "GITHUB_REPOSITORY" in capsys.readouterr().out


def test_invalid_max_diff_chars_fails(env, capsys):
    env["INPUT_MAX_DIFF_CHARS"] = "lots"

    assert main.run(env, github_client=MagicMock()) == 1
    assert "max_diff_chars" in capsys.readouterr().out


def test_event_without_pull_request_fails(env, tmp_path, capsys):
    other = tmp_path / "push.json"
    other.write_text(json.dumps({"ref": "refs/heads/main"}))
    env["GITHUB_EVENT_PATH"] = str(other)

    assert main.run(env, github_client=MagicMock()) == 1
    assert "pull_request" in capsys.readouterr().out


def test_unreadable_event_file_fails(env, tmp_path, capsys):
    env["GITHUB_EVENT_PATH"] = str(tmp_path / "missing.json")

    assert main.run(env, github_client=MagicMock()) == 1
    assert "event payload" in capsys.readouterr().out


def test_pr_with_only_binary_files_is_skipped_gracefully(env, gemini, capsys):
    binary = SimpleNamespace(filename="logo.png", patch=None, status="added", previous_filename=None)
    github, pr = make_github(files=[binary])

    assert main.run(env, github_client=github) == 0

    assert "::notice::" in capsys.readouterr().out
    gemini.models.generate_content.assert_not_called()
    pr.create_issue_comment.assert_not_called()


def test_rate_limit_is_a_warning_by_default(env, gemini, monkeypatch, capsys):
    monkeypatch.setattr(reviewer.time, "sleep", lambda _: None)
    gemini.models.generate_content.side_effect = genai_errors.ClientError(
        429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}}
    )
    github, pr = make_github()

    assert main.run(env, github_client=github) == 0

    assert "::warning::" in capsys.readouterr().out
    pr.create_issue_comment.assert_not_called()


def test_rate_limit_fails_when_fail_on_error_is_true(env, gemini, monkeypatch, capsys):
    monkeypatch.setattr(reviewer.time, "sleep", lambda _: None)
    gemini.models.generate_content.side_effect = genai_errors.ClientError(
        429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}}
    )
    env["INPUT_FAIL_ON_ERROR"] = "true"
    github, _ = make_github()

    assert main.run(env, github_client=github) == 1
    assert "::error::" in capsys.readouterr().out


def test_existing_review_comment_is_updated(env, gemini):
    previous = MagicMock()
    previous.body = f"{reviewer.COMMENT_MARKER}\nold"
    github, pr = make_github(existing_comments=[previous])

    assert main.run(env, github_client=github) == 0

    previous.edit.assert_called_once()
    pr.create_issue_comment.assert_not_called()


def test_get_pull_request_number_supports_pull_request_target():
    assert main.get_pull_request_number({"pull_request": {"number": 42}}) == 42
