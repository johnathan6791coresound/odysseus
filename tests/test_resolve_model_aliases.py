"""Unit tests for _resolve_model role-alias resolution."""
import pytest

import src.ai_interaction as ai


def test_utility_alias_resolves_via_role(monkeypatch):
    called = {}

    def fake_resolve(prefix, owner=None):
        called["prefix"] = prefix
        return ("http://local/v1/chat/completions", "llama-3.1-8b", {"h": "1"})

    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint", fake_resolve)
    url, model, headers = ai._resolve_model("utility", owner="alice")
    assert called["prefix"] == "utility"
    assert model == "llama-3.1-8b"
    assert headers == {"h": "1"}


def test_cheap_alias_maps_to_utility(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "src.endpoint_resolver.resolve_endpoint",
        lambda prefix, owner=None: (seen.setdefault("p", prefix),
                                    ("http://x/v1/chat/completions", "m", {}))[1],
    )
    ai._resolve_model("cheap")
    assert seen["p"] == "utility"


def test_alias_is_exact_match_only(monkeypatch):
    # "utility-7b" must NOT be treated as the "utility" role — it should fall
    # through to the normal literal-name search path (which raises here because
    # there are no endpoints in the test DB).
    monkeypatch.setattr(
        "src.endpoint_resolver.resolve_endpoint",
        lambda prefix, owner=None: (_ for _ in ()).throw(
            AssertionError("role path should not run for a substring match")),
    )
    with pytest.raises(ValueError):
        ai._resolve_model("utility-7b")


def test_unresolvable_role_raises(monkeypatch):
    monkeypatch.setattr(
        "src.endpoint_resolver.resolve_endpoint",
        lambda prefix, owner=None: (None, None, None),
    )
    with pytest.raises(ValueError):
        ai._resolve_model("task")
