"""Bound and validate the operator-configured Ollama connection."""

import os
import re
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx2
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from ira_native.errors import IraError


class AgentLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seconds: float = Field(120, gt=0, le=600)
    request_seconds: float = Field(60, gt=0, le=300)
    attempts: int = Field(6, ge=1, le=24)
    executions: int = Field(6, ge=1, le=24)
    requests: int = Field(8, ge=1, le=24)
    tokens: int = Field(16000, ge=512, le=128000)
    context_tokens: int = Field(4096, ge=2048, le=65536)
    response_tokens: int = Field(384, ge=64, le=2048)
    rows: int = Field(12, ge=1, le=48)
    bytes: int = Field(6000, ge=512, le=32000)
    temperature: float = Field(0, ge=0, le=2)
    top_p: float = Field(1, gt=0, le=1)

    @classmethod
    def from_env(cls):
        try:
            return cls.model_validate(
                {
                    key: os.environ[f"IRA_AGENT_{key.upper()}"]
                    for key in cls.model_fields
                    if f"IRA_AGENT_{key.upper()}" in os.environ
                }
            )
        except ValidationError:
            raise IraError("invalid_input") from None


def local_address() -> tuple[str, str]:
    name = os.getenv("IRA_LOCAL_OLLAMA_MODEL", "qwen3.5:4b")
    if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.:/-]{0,127}", name):
        raise IraError("invalid_input")
    addr = os.getenv("IRA_LOCAL_OLLAMA_URL", "http://127.0.0.1:11434")
    try:
        parsed = urlsplit(addr)
        port = parsed.port or 11434
        if (
            "cloud" in name.casefold()
            or parsed.scheme != "http"
            or parsed.hostname not in ("127.0.0.1", "::1")
            or parsed.username is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise IraError("permission_denied")
    except ValueError:
        raise IraError("invalid_input") from None
    host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
    return f"http://{host}:{port}", name if ":" in name else f"{name}:latest"


class LocalModelUnavailable(Exception):
    pass


@asynccontextmanager
async def open_local_model(url: str, name: str, limits: AgentLimits):
    async with httpx2.AsyncClient(
        trust_env=False, follow_redirects=False, timeout=limits.request_seconds
    ) as client:
        try:

            async def probe(path, **kwargs):
                response = await client.request("POST" if kwargs else "GET", url + path, **kwargs)
                response.raise_for_status()
                return response.json()

            version = (await probe("/api/version"))["version"]
            local = next(
                (entry for entry in (await probe("/api/tags"))["models"] if entry["name"] == name),
                None,
            )
            if (
                local is None
                or not local.get("digest")
                or local.get("remote_host")
                or local.get("remote_model")
            ):
                raise LocalModelUnavailable("model_not_installed_locally")
            metadata = await probe("/api/show", json={"model": name})
            if (
                metadata.get("remote_host")
                or metadata.get("remote_model")
                or "tools" not in metadata.get("capabilities", ())
            ):
                raise LocalModelUnavailable("model_tools_unavailable")
        except (httpx2.HTTPError, ValueError, KeyError, TypeError) as error:
            raise LocalModelUnavailable(f"ollama_preflight_{type(error).__name__}") from None

        provider = OpenAIProvider(
            openai_client=AsyncOpenAI(
                base_url=url + "/v1",
                api_key="ollama",
                organization="",
                project="",
                max_retries=0,
                http_client=client,
            )
        )
        yield (
            OpenAIChatModel(name, provider=provider),
            {
                "backend": "ollama_openai_loopback",
                "version": version,
                "model": name,
                "digest": local["digest"],
                "tool_capability": True,
            },
        )
