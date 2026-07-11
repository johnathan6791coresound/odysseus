import subprocess
import json
import time
import httpx
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Cache for discovered hosts
_hosts_cache: List[str] = []
_hosts_cache_time: float = 0
_HOSTS_CACHE_TTL = 60  # seconds


def _parse_tailscale_status(raw: str) -> Dict[str, Any]:
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _first_tailscale_ipv4(value: Any) -> Optional[str]:
    if not isinstance(value, list):
        return None
    for ip in value:
        if isinstance(ip, str) and "." in ip:
            return ip
    return None


def discover_tailscale_hosts() -> List[str]:
    """Discover online Tailscale peers, returning their IPv4 addresses."""
    global _hosts_cache, _hosts_cache_time

    now = time.time()
    if _hosts_cache and (now - _hosts_cache_time) < _HOSTS_CACHE_TTL:
        return list(_hosts_cache)

    hosts = []
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return hosts

        data = _parse_tailscale_status(result.stdout)
        if not data:
            return hosts

        # Add self
        self_data = data.get("Self") if isinstance(data.get("Self"), dict) else {}
        self_ip = _first_tailscale_ipv4(self_data.get("TailscaleIPs"))
        if self_ip:
            hosts.append(self_ip)

        # Add online peers (skip funnel-ingress-nodes and android devices)
        peers = data.get("Peer") if isinstance(data.get("Peer"), dict) else {}
        for peer in peers.values():
            if not isinstance(peer, dict):
                continue
            if not peer.get("Online"):
                continue
            hostname = peer.get("HostName", "")
            if hostname == "funnel-ingress-node":
                continue
            os_name = peer.get("OS", "")
            if os_name == "android":
                continue
            peer_ip = _first_tailscale_ipv4(peer.get("TailscaleIPs"))
            if peer_ip:
                hosts.append(peer_ip)

        _hosts_cache = hosts
        _hosts_cache_time = now
        logger.info(f"Tailscale discovery found {len(hosts)} hosts: {hosts}")
    except FileNotFoundError:
        logger.debug("tailscale command not found")
    except Exception as e:
        logger.warning(f"Tailscale discovery failed: {e}")

    return hosts


class ModelDiscovery:
    def __init__(self, default_host: str, openai_api_key: Optional[str] = None):
        self.default_host = default_host
        self.openai_api_key = openai_api_key
        self.openai_compat_path = "/v1/chat/completions"
        # Custom ports from env vars, merged into the scan list by discover_models.
        self._extra_ports: set = set()

    def _get_hosts(self) -> List[str]:
        """Get all hosts to scan, using env override, Tailscale, or default."""
        self._extra_ports = set()

        def _append_host(out: List[str], host: str) -> None:
            host = (host or "").strip()
            if not host or host in out:
                return
            out.append(host)

        def _append_env_hosts(out: List[str]) -> None:
            """Add hosts (and any custom ports) from provider-specific env vars."""
            for env_name in ("OLLAMA_BASE_URL", "OLLAMA_URL", "LM_STUDIO_URL"):
                raw = os.getenv(env_name, "").strip()
                if not raw:
                    continue
                try:
                    parsed = urlparse(raw if "://" in raw else "http://" + raw)
                    _append_host(out, parsed.hostname or "")
                    if parsed.port:
                        self._extra_ports.add(parsed.port)
                except Exception:
                    pass

        # Manual override takes priority
        extra = os.getenv("LLM_HOSTS", "").strip()
        if extra:
            hosts = [h.strip() for h in extra.split(",") if h.strip()]
            # Always include the default host too
            if self.default_host not in hosts:
                hosts.insert(0, self.default_host)
            _append_host(hosts, "host.docker.internal")
            _append_env_hosts(hosts)
            return hosts

        # Try Tailscale discovery
        ts_hosts = discover_tailscale_hosts()
        if ts_hosts:
            # Ensure default_host is included
            if self.default_host not in ts_hosts:
                ts_hosts.insert(0, self.default_host)
            _append_host(ts_hosts, "host.docker.internal")
            _append_env_hosts(ts_hosts)
            return ts_hosts

        hosts = [self.default_host]
        # Docker desktop/Linux compose maps this to the host machine. That is
        # the common "I started Ollama normally on this computer" case.
        _append_host(hosts, "host.docker.internal")
        _append_env_hosts(hosts)
        return hosts

    def _fingerprint_provider(self, host: str, port: int) -> Optional[str]:
        """Identify the server software via its native API, independent of port."""
        try:
            r = httpx.get(f"http://{host}:{port}/api/v1/models", timeout=1.5)
            if r.is_success:
                models = (r.json() or {}).get("models")
                if (
                    isinstance(models, list)
                    and models
                    and isinstance(models[0], dict)
                    and "key" in models[0]
                    and "architecture" in models[0]
                ):
                    return "lmstudio"
        except Exception:
            pass
        # llama.cpp's llama-server exposes a native /props endpoint (no /v1 prefix)
        # describing the loaded model, slots, and chat template — distinct from
        # LM Studio (/api/v1/models) and vLLM (/version, /metrics).
        try:
            r = httpx.get(f"http://{host}:{port}/props", timeout=1.5)
            if r.is_success:
                props = r.json() or {}
                if isinstance(props, dict) and (
                    "default_generation_settings" in props
                    or "total_slots" in props
                    or "chat_template" in props
                ):
                    return "llamacpp"
        except Exception:
            pass
        return None

    def _check_port(self, host: str, port: int) -> Optional[Dict[str, Any]]:
        """Check a single host:port for models."""
        base = f"http://{host}:{port}/v1"
        try:
            r = httpx.get(f"{base}/models", timeout=3)
            if not r.is_success:
                return None
            data = r.json()
            # Some OpenAI-compatible servers return a bare list, not {"data": [...]}.
            items = data if isinstance(data, list) else ((data or {}).get("data") or [])
            ids = [m.get("id") for m in items if isinstance(m, dict) and m.get("id")]
            if ids:
                return {
                    "host": host,
                    "port": port,
                    "url": f"http://{host}:{port}{self.openai_compat_path}",
                    "models": ids,
                    "models_display": [i.lstrip("/") for i in ids],
                    "provider": self._fingerprint_provider(host, port),
                }
        except Exception:
            pass
        return None

    def discover_models(self) -> Dict[str, List[Dict[str, Any]]]:
        """Discover available models from all reachable hosts."""
        hosts = self._get_hosts()
        items = []

        logger.info(f"Scanning {len(hosts)} hosts for models: {hosts}")

        # Well-known ports: 8000-8020 (vLLM, SGLang, Cookbook), 8080 (llama.cpp /
        # llama-server default), 1234 (LM Studio), 11434 (Ollama), 11435 for APFEL
        # as its default port is occupied by Ollama. The env vars can add more
        # ports which will be merged in.
        ports = list(range(8000, 8021)) + [8080, 1234, 11434, 11435]
        ports += [p for p in sorted(self._extra_ports) if p not in ports]
        targets = [(h, p) for h in hosts for p in ports]

        seen_models = (
            set()
        )  # dedupe by (port, model_ids) to avoid same machine via different IPs

        with ThreadPoolExecutor(max_workers=50) as pool:
            futures = {pool.submit(self._check_port, h, p): (h, p) for h, p in targets}
            for future in as_completed(futures):
                result = future.result()
                if result:
                    key = (result["port"], tuple(sorted(result["models"])))
                    if key not in seen_models:
                        seen_models.add(key)
                        items.append(result)

        # Sort by host then port for consistent ordering
        items.sort(key=lambda x: (x["host"], x["port"]))

        logger.info(
            f"Discovered {len(items)} model endpoints across {len(hosts)} hosts"
        )
        return {"hosts": hosts, "items": items}

    def warmup_ping_urls(self, limit: int = 5) -> List[str]:
        """The ``/models`` URLs of up to ``limit`` discovered endpoints.

        Used by the startup warmup / keepalive loop to prime connections. Each
        discovered item already carries a ``/v1/chat/completions`` url; swap the
        suffix for the cheap ``/models`` probe. Failures degrade to an empty list
        so warmup never crashes the caller.
        """
        try:
            items = (self.discover_models() or {}).get("items", [])
        except Exception:
            return []
        urls: List[str] = []
        for ep in items[:limit]:
            url = (ep.get("url") or "").replace("/chat/completions", "/models")
            if url:
                urls.append(url)
        return urls

    def get_providers(self) -> Dict[str, Any]:
        """Get all available providers"""
        discovery = self.discover_models()
        items = discovery["items"]
        providers = [{"provider": "vllm", "hosts": discovery["hosts"], "items": items}]

        if self.openai_api_key:
            openai_models = [
                "gpt-5.2-codex",
                "gpt-4o-mini",
                "gpt-image-1.5",
                "gpt-4o",
                "gpt-5.2",
                "gpt-5.2-pro",
            ]
            providers.append(
                {
                    "provider": "openai",
                    "items": [
                        {
                            "url": "https://api.openai.com/v1/chat/completions",
                            "models": openai_models,
                        }
                    ],
                }
            )

        return {"providers": providers}


# ── Cheapest-available-model resolver (cost auto-routing) ───────────────────
#
# Used by endpoint_resolver.resolve_endpoint() to pick a free local model (or a
# cheap API model) for unset utility/research/task roles instead of silently
# billing the expensive default chat model for background work.

# Full-scan cache: the module-level _hosts_cache above only memoizes the host
# list, not the per-port /v1/models scan, so we add a dedicated short TTL cache
# for the resolved cheap endpoint.
_cheap_model_cache: Optional[tuple] = None  # (expires_at, result)
_CHEAP_MODEL_CACHE_TTL = 60  # seconds

# Process-level failure latch (mirrors embeddings._http_embed_down). When a
# local utility call fails/cancels we skip local for a short window so utility
# calls don't repeatedly stall on a slow/contended local model. Reset on the
# next successful local call.
_local_utility_cooldown_until: float = 0.0
_LOCAL_UTILITY_COOLDOWN = 300  # seconds (5 min)

# Small hardcoded list of cheap hosted models, tried only when no live local
# endpoint is available. Each entry maps a model id to the env var that must be
# present for its provider to be considered configured.
# Each entry is (base_url, model, env_var). base_url is a provider *base* (not a
# full chat/completions URL) so endpoint_resolver.build_chat_url() can derive
# the correct per-provider path.
_KNOWN_CHEAP_API_MODELS = [
    ("https://api.anthropic.com", "claude-haiku-4-5", "ANTHROPIC_API_KEY"),
    ("https://api.openai.com/v1", "gpt-4o-mini", "OPENAI_API_KEY"),
]


def note_local_utility_failure() -> None:
    """Trip the cooldown latch after a failed/cancelled local utility call."""
    global _local_utility_cooldown_until
    _local_utility_cooldown_until = time.time() + _LOCAL_UTILITY_COOLDOWN
    logger.info("Local utility model tripped cooldown latch for %ss", _LOCAL_UTILITY_COOLDOWN)


def note_local_utility_success() -> None:
    """Clear the cooldown latch after a successful local utility call."""
    global _local_utility_cooldown_until
    _local_utility_cooldown_until = 0.0


def _local_utility_in_cooldown() -> bool:
    return time.time() < _local_utility_cooldown_until


def _cheap_api_model() -> Optional[tuple]:
    """First known cheap hosted model whose provider is configured, else None."""
    for base_url, model, env_var in _KNOWN_CHEAP_API_MODELS:
        if os.getenv(env_var):
            return (base_url, model)
    return None


def resolve_cheapest_available_model(owner: Optional[str] = None) -> Optional[tuple]:
    """Resolve the cheapest currently-usable (base_url, model) for utility work.

    Preference order:
      1. A live local model server (free) — unless the user disabled
         ``utility_prefer_local_enabled`` or the cooldown latch is tripped.
      2. A cheap hosted model whose provider key is configured.
      3. None — caller falls through to existing default_model behavior.

    Only endpoints currently answering ``/v1/models`` are returned for the local
    branch, so a merely-installed-but-unloaded model that needs a slow cold load
    is never picked. Wrapped in a 60s TTL cache to avoid re-scanning ports on
    every background call.
    """
    global _cheap_model_cache
    now = time.time()
    if _cheap_model_cache and now < _cheap_model_cache[0]:
        return _cheap_model_cache[1]

    result: Optional[tuple] = None

    prefer_local = True
    try:
        from src.settings import get_user_setting
        prefer_local = bool(get_user_setting(
            "utility_prefer_local_enabled", owner or "", True
        ))
    except Exception:
        pass

    if prefer_local and not _local_utility_in_cooldown():
        try:
            from src.constants import DEFAULT_HOST, OPENAI_API_KEY
            discovery = ModelDiscovery(DEFAULT_HOST, OPENAI_API_KEY).discover_models()
            items = discovery.get("items") or []
            if items:
                first = items[0]
                # discover_models yields a full .../v1/chat/completions url;
                # hand back the base so build_chat_url can re-derive the path.
                full_url = first.get("url") or ""
                base_url = full_url.replace("/chat/completions", "")
                models = first.get("models") or []
                # Prefer a chat model over embeddings/tts in the list.
                model = None
                for m in models:
                    if m and not any(p in str(m).lower() for p in (
                        "embedding", "tts", "whisper", "rerank"
                    )):
                        model = m
                        break
                if not model and models:
                    model = models[0]
                if base_url and model:
                    result = (base_url, model)
        except Exception as e:
            logger.warning("resolve_cheapest_available_model local scan failed: %s", e)

    if result is None:
        result = _cheap_api_model()

    _cheap_model_cache = (now + _CHEAP_MODEL_CACHE_TTL, result)
    return result
