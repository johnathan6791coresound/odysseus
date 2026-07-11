"""Tests for model_discovery.resolve_cheapest_available_model + cooldown latch."""
import time

import src.model_discovery as md


def _reset():
    md._cheap_model_cache = None
    md.note_local_utility_success()  # clear cooldown


def test_local_model_reachable(monkeypatch):
    _reset()
    monkeypatch.setattr(
        md.ModelDiscovery, "discover_models",
        lambda self: {"hosts": ["h"], "items": [
            {"url": "http://h:11434/v1/chat/completions", "models": ["llama-3.1-8b"]}
        ]},
    )
    monkeypatch.setattr(md, "get_providers", None, raising=False)
    result = md.resolve_cheapest_available_model()
    assert result == ("http://h:11434/v1", "llama-3.1-8b")


def test_no_local_falls_back_to_api(monkeypatch):
    _reset()
    monkeypatch.setattr(
        md.ModelDiscovery, "discover_models",
        lambda self: {"hosts": ["h"], "items": []},
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    result = md.resolve_cheapest_available_model()
    assert result == ("https://api.openai.com/v1", "gpt-4o-mini")


def test_neither_returns_none(monkeypatch):
    _reset()
    monkeypatch.setattr(
        md.ModelDiscovery, "discover_models",
        lambda self: {"hosts": ["h"], "items": []},
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert md.resolve_cheapest_available_model() is None


def test_cooldown_latch_skips_local(monkeypatch):
    _reset()

    def _boom(self):
        raise AssertionError("discover_models should not be called during cooldown")

    monkeypatch.setattr(md.ModelDiscovery, "discover_models", _boom)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    md.note_local_utility_failure()
    # In cooldown → skips local scan entirely, goes straight to cheap API.
    result = md.resolve_cheapest_available_model()
    assert result == ("https://api.openai.com/v1", "gpt-4o-mini")
    assert md._local_utility_in_cooldown() is True

    # After success notification the latch clears.
    md.note_local_utility_success()
    assert md._local_utility_in_cooldown() is False


def test_prefer_local_disabled(monkeypatch):
    _reset()

    def _boom(self):
        raise AssertionError("discover_models should not run when prefer_local disabled")

    monkeypatch.setattr(md.ModelDiscovery, "discover_models", _boom)
    monkeypatch.setattr(
        "src.settings.get_user_setting",
        lambda key, owner="", default=None: False if key == "utility_prefer_local_enabled" else default,
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    result = md.resolve_cheapest_available_model()
    assert result == ("https://api.openai.com/v1", "gpt-4o-mini")
