"""Toggle-matrix tests for cost auto-routing in resolve_endpoint()."""
import src.endpoint_resolver as er


def _settings(**over):
    base = {
        "cost_auto_routing_enabled": True,
        "utility_auto_cheap_enabled": True,
        "utility_endpoint_id": "",
        "utility_model": "",
        "research_endpoint_id": "",
        "research_model": "",
        "task_endpoint_id": "",
        "task_model": "",
        "default_endpoint_id": "",
        "default_model": "",
    }
    base.update(over)
    return base


def _patch(monkeypatch, settings, cheap=("http://local:11434/v1", "llama-3.1-8b")):
    monkeypatch.setattr("src.settings.load_settings", lambda: settings)
    monkeypatch.setattr(
        "src.settings.get_user_setting",
        lambda key, owner="", default=None: settings.get(key, default),
    )
    monkeypatch.setattr(
        "src.model_discovery.resolve_cheapest_available_model",
        lambda owner=None: cheap,
    )


def test_utility_routes_cheap_when_on(monkeypatch):
    _patch(monkeypatch, _settings())
    url, model, _ = er.resolve_endpoint("utility")
    assert model == "llama-3.1-8b"
    assert url == "http://local:11434/v1/chat/completions"


def test_research_and_task_route_cheap_transitively(monkeypatch):
    _patch(monkeypatch, _settings())
    for role in ("research", "task"):
        _, model, _ = er.resolve_endpoint(role)
        assert model == "llama-3.1-8b", role


def test_master_off_disables_all(monkeypatch):
    _patch(monkeypatch, _settings(cost_auto_routing_enabled=False,
                                  default_model="expensive-default"))
    for role in ("utility", "research", "task"):
        # No ModelEndpoint DB row for "" endpoint id → returns fallback (None).
        url, model, _ = er.resolve_endpoint(role)
        assert model != "llama-3.1-8b", role


def test_sub_toggle_off_disables_cheap(monkeypatch):
    _patch(monkeypatch, _settings(utility_auto_cheap_enabled=False,
                                  default_model="expensive-default"))
    _, model, _ = er.resolve_endpoint("utility")
    assert model != "llama-3.1-8b"


def test_explicit_utility_model_wins(monkeypatch):
    # When utility_endpoint_id is set, cheap routing is never consulted.
    _patch(monkeypatch, _settings(utility_endpoint_id="ep-123",
                                  utility_model="configured-model"))
    # ep-123 has no DB row here, so it falls back — but crucially not to cheap.
    _, model, _ = er.resolve_endpoint("utility")
    assert model != "llama-3.1-8b"
