"""Official OpenAI Workspace Agents API provider for Gumacu AGY MCP.

This adapter provides an opt-in, non-device service integration using OpenAI's
official Workspace Agents API:
- POST https://api.chatgpt.com/v1/workspace_agents/{agent_id}/trigger
- GET https://api.chatgpt.com/v1/workspace_agents/{agent_id}/runs/{run_id}

Important Constraints & Boundaries:
1. Personal ChatGPT Chats: There is NO official external API to read or continue
   arbitrary personal ChatGPT web chats. This module explicitly does NOT scrape,
   harvest session tokens, or simulate browser clicks.
2. Output Availability: The Workspace Agents API returns HTTP 202 Accepted on trigger
   and allows status polling (queued, in_progress, suspended, completed, failed).
   Response text content is NOT returned by this API. Output is viewed in ChatGPT
   via the returned conversation_url.
3. Security: All network traffic is strictly bounded to api.chatgpt.com. Credentials
   are read strictly from environment variables and never logged or exposed in errors.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

PROVIDER_ID = "chatgpt-workspace"
PROVIDER_KIND = "provider"
PROVIDER_NAME = "openai_workspace_agent"

ALLOWED_BASE_HOST = "api.chatgpt.com"
API_BASE_URL = "https://api.chatgpt.com"
BETA_HEADER_KEY = "OpenAI-Beta"
BETA_HEADER_VALUE = "workspace_agent_runs=v1"

ALLOWED_ACTIONS = frozenset({"trigger", "status", "agents", "capabilities"})
ALLOWED_RUN_STATUSES = frozenset({"queued", "in_progress", "suspended", "completed", "failed"})

MAX_INPUT_CHARS = 64_000
MAX_CONVERSATION_KEY_CHARS = 256
MAX_AGENT_ALIAS_CHARS = 64
DEFAULT_HTTP_TIMEOUT_SECONDS = 15.0

AGENT_ID_REGEX = re.compile(r"^agtch_[A-Za-z0-9_-]{8,128}$")
RUN_ID_REGEX = re.compile(r"^apirun_[A-Za-z0-9_-]{8,128}$")
CONVERSATION_KEY_REGEX = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
ALIAS_REGEX = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")


class ChatGPTWorkspaceConfig:
    """Immutable, validated configuration for the ChatGPT Workspace provider."""

    def __init__(
        self,
        token: str,
        agents: dict[str, str],
        base_url: str = API_BASE_URL,
    ) -> None:
        self._token = token.strip()
        self.agents = dict(agents)

        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or parsed.netloc != ALLOWED_BASE_HOST:
            raise ValueError(f"base_url must strictly target https://{ALLOWED_BASE_HOST}")
        self.base_url = base_url.rstrip("/")

    @property
    def is_configured(self) -> bool:
        """Return True only if a token and at least one approved agent ID exist."""
        return bool(self._token and self.agents)

    def get_token(self) -> str:
        """Return bearer token; never expose via repr/str."""
        return self._token

    def __repr__(self) -> str:
        masked_token = "***" if self._token else "<empty>"
        return (
            f"ChatGPTWorkspaceConfig(configured={self.is_configured}, "
            f"token={masked_token}, agents={list(self.agents.keys())})"
        )


def load_config() -> ChatGPTWorkspaceConfig:
    """Load and validate configuration from environment variables."""
    token = os.environ.get("CHATGPT_WORKSPACE_PAT") or os.environ.get("CHATGPT_WORKSPACE_TOKEN") or ""

    agents: dict[str, str] = {}
    raw_agents = os.environ.get("CHATGPT_WORKSPACE_AGENTS", "").strip()
    if raw_agents:
        try:
            parsed = json.loads(raw_agents)
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    if isinstance(k, str) and isinstance(v, str):
                        alias = k.strip()
                        agent_id = v.strip()
                        if ALIAS_REGEX.fullmatch(alias) and AGENT_ID_REGEX.fullmatch(agent_id):
                            agents[alias] = agent_id
                        else:
                            logger.warning(
                                "Skipping invalid agent mapping alias=%r id=%r in CHATGPT_WORKSPACE_AGENTS",
                                alias,
                                agent_id,
                            )
        except json.JSONDecodeError:
            for pair in raw_agents.split(","):
                alias, sep, agent_id = pair.partition(":")
                alias = alias.strip()
                agent_id = agent_id.strip()
                if sep and ALIAS_REGEX.fullmatch(alias) and AGENT_ID_REGEX.fullmatch(agent_id):
                    agents[alias] = agent_id

    single_agent = (os.environ.get("CHATGPT_WORKSPACE_AGENT_ID") or "").strip()
    if single_agent and AGENT_ID_REGEX.fullmatch(single_agent):
        agents.setdefault("default", single_agent)

    return ChatGPTWorkspaceConfig(token=token, agents=agents)


def _mask_id(identifier: str) -> str:
    """Mask identifier for public safe display."""
    if len(identifier) > 12:
        return f"{identifier[:8]}...{identifier[-4:]}"
    return identifier


def resolve_agent_id(
    config: ChatGPTWorkspaceConfig,
    agent_param: str | None = None,
) -> tuple[str, str]:
    """Resolve and validate an approved agent alias or ID from configuration.

    Returns (alias, validated_agtch_id).
    Raises ValueError on unrecognized or unapproved agent targets.
    """
    if not config.agents:
        raise ValueError("no approved workspace agents configured")

    if not agent_param:
        if "default" in config.agents:
            return "default", config.agents["default"]
        if len(config.agents) == 1:
            alias = next(iter(config.agents.keys()))
            return alias, config.agents[alias]
        raise ValueError(
            f"agent parameter required; configured aliases: {sorted(config.agents.keys())}"
        )

    clean = agent_param.strip()
    if clean in config.agents:
        return clean, config.agents[clean]

    for alias, agtch_id in config.agents.items():
        if clean == agtch_id:
            return alias, agtch_id

    raise ValueError(
        f"unapproved agent target: {clean!r}. Must match an approved alias in {sorted(config.agents.keys())}"
    )


def descriptor(config: ChatGPTWorkspaceConfig | None = None) -> dict[str, Any]:
    """Return compact provider catalog descriptor for capability_gateway.catalog()."""
    cfg = config or load_config()
    return {
        "id": PROVIDER_ID,
        "kind": PROVIDER_KIND,
        "provider": PROVIDER_NAME,
        "online": cfg.is_configured,
        "configured": cfg.is_configured,
        "actions": sorted(ALLOWED_ACTIONS),
        "notes": (
            "Official OpenAI Workspace Agents API (trigger & status polling). "
            "Output text retrieval is unavailable via API."
        ),
    }


def get_capabilities(config: ChatGPTWorkspaceConfig | None = None) -> dict[str, Any]:
    """Return detailed provider capabilities and bounds."""
    cfg = config or load_config()
    return {
        "success": True,
        "provider": PROVIDER_ID,
        "kind": PROVIDER_KIND,
        "configured": cfg.is_configured,
        "online": cfg.is_configured,
        "actions": sorted(ALLOWED_ACTIONS),
        "limits": {
            "max_input_chars": MAX_INPUT_CHARS,
            "max_conversation_key_chars": MAX_CONVERSATION_KEY_CHARS,
            "content_retrieval_supported": False,
            "personal_chat_history_supported": False,
        },
        "documentation": {
            "workspace_agents_api": "https://developers.openai.com/workspace-agents/trigger-runs",
            "token_sharing_siwc": "https://developers.openai.com/siwc/token-sharing-open-source",
            "chatgpt_terms_of_use": "https://openai.com/policies/terms-of-use",
        },
        "notes": (
            "Official OpenAI Workspace Agents API provider. "
            "Can trigger workspace agent runs and poll execution status. "
            "Cannot extract or read response text content via API (user views in ChatGPT). "
            "Cannot access or resume arbitrary personal ChatGPT web chat history."
        ),
    }


def list_agents(config: ChatGPTWorkspaceConfig | None = None) -> dict[str, Any]:
    """Return list of configured agent aliases and safely masked IDs."""
    cfg = config or load_config()
    return {
        "success": True,
        "provider": PROVIDER_ID,
        "configured": cfg.is_configured,
        "agents": [
            {"alias": alias, "agent_id": _mask_id(agtch_id)}
            for alias, agtch_id in cfg.agents.items()
        ],
    }


def trigger_run(
    input_text: str,
    *,
    agent: str | None = None,
    conversation_key: str | None = None,
    idempotency_key: str | None = None,
    config: ChatGPTWorkspaceConfig | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Trigger a Workspace Agent run via POST /v1/workspace_agents/{agent_id}/trigger."""
    cfg = config or load_config()
    if not cfg.is_configured:
        return {
            "success": False,
            "error": "ChatGPT Workspace Agent provider is not configured. Set CHATGPT_WORKSPACE_PAT and approved agent IDs.",
            "configured": False,
        }

    if not isinstance(input_text, str) or not input_text.strip():
        return {"success": False, "error": "input text must be a non-empty string"}

    clean_input = input_text.strip()
    if len(clean_input) > MAX_INPUT_CHARS:
        return {
            "success": False,
            "error": f"input length ({len(clean_input)}) exceeds maximum permitted ({MAX_INPUT_CHARS})",
        }

    try:
        alias, agent_id = resolve_agent_id(cfg, agent)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    payload: dict[str, Any] = {"input": clean_input}
    if conversation_key is not None:
        ck = str(conversation_key).strip()
        if not CONVERSATION_KEY_REGEX.fullmatch(ck):
            return {
                "success": False,
                "error": "conversation_key must be 1..256 alphanumeric characters, dashes, or dots",
            }
        payload["conversation_key"] = ck

    headers = {
        "Authorization": f"Bearer {cfg.get_token()}",
        "Content-Type": "application/json",
        BETA_HEADER_KEY: BETA_HEADER_VALUE,
    }
    if idempotency_key:
        headers["Idempotency-Key"] = str(idempotency_key).strip()

    url = f"{cfg.base_url}/v1/workspace_agents/{agent_id}/trigger"

    try:
        if client is not None:
            response = client.post(url, json=payload, headers=headers, timeout=DEFAULT_HTTP_TIMEOUT_SECONDS)
        else:
            with httpx.Client(timeout=DEFAULT_HTTP_TIMEOUT_SECONDS) as default_client:
                response = default_client.post(url, json=payload, headers=headers)

        if response.status_code in {200, 202}:
            try:
                data = response.json()
            except ValueError:
                data = {}
            run_id = str(data.get("agent_trigger_run_id") or data.get("id") or "")
            conversation_url = str(data.get("conversation_url") or "")
            status = str(data.get("status") or "queued")
            return {
                "success": True,
                "provider": PROVIDER_ID,
                "agent_alias": alias,
                "agent_id": agent_id,
                "agent_trigger_run_id": run_id,
                "status": status,
                "conversation_url": conversation_url,
                "output_available": False,
                "output_retrieval": "unavailable",
                "notes": (
                    "Run accepted. Output text retrieval is unavailable via OpenAI Workspace Agents API. "
                    "View run in ChatGPT workspace at conversation_url."
                ),
            }

        try:
            err_data = response.json()
            err_msg = err_data.get("error", {}).get("message") or err_data.get("message") or response.text[:200]
        except Exception:
            err_msg = response.text[:200] or f"HTTP {response.status_code}"

        return {
            "success": False,
            "error": f"Workspace Agent API error (HTTP {response.status_code}): {err_msg}",
            "status_code": response.status_code,
        }
    except httpx.TimeoutException:
        return {"success": False, "error": "Request timed out contacting api.chatgpt.com"}
    except httpx.HTTPError as exc:
        return {"success": False, "error": f"Network error contacting api.chatgpt.com: {type(exc).__name__}"}
    except Exception as exc:
        logger.exception("Unexpected error during Workspace Agent trigger")
        return {"success": False, "error": f"Internal error during Workspace Agent trigger: {type(exc).__name__}"}


def get_run_status(
    run_id: str,
    *,
    agent: str | None = None,
    config: ChatGPTWorkspaceConfig | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Get status of a Workspace Agent run via GET /v1/workspace_agents/{agent_id}/runs/{run_id}."""
    cfg = config or load_config()
    if not cfg.is_configured:
        return {
            "success": False,
            "error": "ChatGPT Workspace Agent provider is not configured. Set CHATGPT_WORKSPACE_PAT and approved agent IDs.",
            "configured": False,
        }

    clean_run_id = str(run_id or "").strip()
    if not RUN_ID_REGEX.fullmatch(clean_run_id):
        return {
            "success": False,
            "error": "run_id must match format 'apirun_[A-Za-z0-9_-]{8,128}'",
        }

    try:
        alias, agent_id = resolve_agent_id(cfg, agent)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    headers = {
        "Authorization": f"Bearer {cfg.get_token()}",
        BETA_HEADER_KEY: BETA_HEADER_VALUE,
    }
    url = f"{cfg.base_url}/v1/workspace_agents/{agent_id}/runs/{clean_run_id}"

    try:
        if client is not None:
            response = client.get(url, headers=headers, timeout=DEFAULT_HTTP_TIMEOUT_SECONDS)
        else:
            with httpx.Client(timeout=DEFAULT_HTTP_TIMEOUT_SECONDS) as default_client:
                response = default_client.get(url, headers=headers)

        if response.status_code == 200:
            try:
                data = response.json()
            except ValueError:
                data = {}
            status = str(data.get("status") or "unknown")
            return {
                "success": True,
                "provider": PROVIDER_ID,
                "agent_alias": alias,
                "agent_id": agent_id,
                "agent_trigger_run_id": clean_run_id,
                "status": status,
                "conversation_url": data.get("conversation_url"),
                "output_available": False,
                "output_retrieval": "unavailable",
                "notes": (
                    "OpenAI Workspace Agents API does not expose run response text/content programmatically. "
                    "View output at conversation_url in ChatGPT."
                ),
            }

        try:
            err_data = response.json()
            err_msg = err_data.get("error", {}).get("message") or err_data.get("message") or response.text[:200]
        except Exception:
            err_msg = response.text[:200] or f"HTTP {response.status_code}"

        return {
            "success": False,
            "error": f"Workspace Agent status error (HTTP {response.status_code}): {err_msg}",
            "status_code": response.status_code,
        }
    except httpx.TimeoutException:
        return {"success": False, "error": "Request timed out contacting api.chatgpt.com"}
    except httpx.HTTPError as exc:
        return {"success": False, "error": f"Network error contacting api.chatgpt.com: {type(exc).__name__}"}
    except Exception as exc:
        logger.exception("Unexpected error during Workspace Agent status query")
        return {"success": False, "error": f"Internal error during Workspace Agent status: {type(exc).__name__}"}


def dispatch_action(
    action: str,
    arguments: dict[str, Any] | None = None,
    config: ChatGPTWorkspaceConfig | None = None,
) -> dict[str, Any]:
    """Execute an allowlisted ChatGPT Workspace action dispatched via agy_dispatch."""
    if action not in ALLOWED_ACTIONS:
        return {
            "success": False,
            "error": f"unsupported action {action!r} on provider {PROVIDER_ID}. Allowed: {sorted(ALLOWED_ACTIONS)}",
        }

    args = arguments or {}
    cfg = config or load_config()

    if action == "capabilities":
        return get_capabilities(cfg)
    if action == "agents":
        return list_agents(cfg)
    if action == "trigger":
        input_text = str(args.get("input") or args.get("prompt") or "")
        agent = args.get("agent")
        conversation_key = args.get("conversation_key")
        idempotency_key = args.get("idempotency_key")
        return trigger_run(
            input_text,
            agent=agent,
            conversation_key=conversation_key,
            idempotency_key=idempotency_key,
            config=cfg,
        )
    if action == "status":
        run_id = str(args.get("run_id") or args.get("agent_trigger_run_id") or "")
        agent = args.get("agent")
        return get_run_status(run_id, agent=agent, config=cfg)

    return {"success": False, "error": f"unhandled action: {action}"}
