"""Embeddings: the one way Job Hunt OS turns text into vectors.

    EmbeddingService.for_user(db, user_id)          which model, at which vector size, from the active AI configuration
        .embed_documents(texts)  /  .embed_query(text)
        → LiteLLM POST /v1/embeddings through the instrumented client (app.services.litellm_client)
        → external_api_request_logs (provider LITELLM, service "embedding"): latency, status, the model that answered,
          the cost LiteLLM reported (NULL when it reported none), never the text or the vectors

`LLMClient.generate_prompt` is for generation and is never used for this. The model is configuration, not code: the
EMBEDDER worker (app.services.ai_workers) is assigned an EMBEDDING model of known vector size through the normal
draft → apply flow (app.services.ai_model_config validates both). Embeddings are served through the LiteLLM gateway
only; in any other provider mode the service is unavailable and retrieval runs on full-text search alone.

Every vector is checked against the configured size before anyone can store it: a model answering with another size
raises DIMENSION_MISMATCH, so vectors that cannot be compared never meet.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy.orm import Session

from app.settings import get_settings

WORKER = "EMBEDDER"
# Bump when the text an index embeds per source changes (app.services.rag_index.document_text): a new index version
# is then built beside the old one, never mixed with it.
RAG_DOC_FORMAT_VERSION = "v1"
MAX_INPUT_CHARS = 4000

NOT_CONFIGURED, UNSUPPORTED_PROVIDER, DIMENSION_MISMATCH = "NOT_CONFIGURED", "UNSUPPORTED_PROVIDER", "DIMENSION_MISMATCH"
RATE_LIMITED, UNAVAILABLE, AUTH, REJECTED = "RATE_LIMITED", "UNAVAILABLE", "AUTH", "REJECTED"


class EmbeddingError(RuntimeError):
    """No vectors: `code` says why (one of the constants above). The message never holds the input text."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class EmbeddingUnavailable(EmbeddingError):
    """Embeddings cannot be made for this account at all (nothing configured, unsupported provider)."""


@dataclass(frozen=True)
class EmbeddingModel:
    provider: str
    model: str
    underlying_model: str | None
    dimensions: int
    doc_format_version: str = RAG_DOC_FORMAT_VERSION

    @property
    def identity_hash(self) -> str:
        """Two indexes may share vectors only when this is equal (same model, size and document format)."""
        raw = "|".join([self.provider, self.model, self.underlying_model or "", str(self.dimensions),
                        self.doc_format_version])
        return hashlib.sha256(raw.encode()).hexdigest()


@dataclass
class Embeddings:
    vectors: list[list[float]]
    model: str  # the configured model name
    answered_model: str | None  # what the provider says answered, when it says
    dimensions: int
    input_tokens: int | None = None
    latency_ms: int | None = None
    request_id: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class Backend(Protocol):
    def embed(self, model: EmbeddingModel, texts: list[str], operation: str) -> Embeddings: ...


# ───────────── backends ─────────────

class LiteLLMBackend:
    """POST /v1/embeddings on the account's LiteLLM gateway (its saved base URL and key)."""

    def __init__(self, base_url: str, api_key: str | None, user_id: int | None):
        self.base_url, self.api_key, self.user_id = base_url, api_key, user_id

    def embed(self, model: EmbeddingModel, texts: list[str], operation: str) -> Embeddings:
        from app.services.external_api import RateLimitedCooldown
        from app.services.litellm_client import (
            LiteLLMAPI,
            LiteLLMAuthError,
            LiteLLMError,
            LiteLLMRejected,
            LiteLLMUnsupported,
        )

        timeout = int(get_settings().rag_embed_timeout_seconds or 60)
        try:
            with LiteLLMAPI(self.base_url, self.api_key, user_id=self.user_id, service="embedding",
                            timeout=timeout) as api:
                answer = api.embeddings(model.model, texts, operation=operation, timeout=timeout,
                                        metadata={"dimensions": model.dimensions, "worker_type": WORKER})
        except RateLimitedCooldown as exc:
            raise EmbeddingError(RATE_LIMITED, f"{model.model} is cooling down after a rate limit") from exc
        except LiteLLMAuthError as exc:
            raise EmbeddingError(AUTH, "LiteLLM refused the API key") from exc
        except LiteLLMUnsupported as exc:
            raise EmbeddingUnavailable(UNSUPPORTED_PROVIDER, "This LiteLLM has no /v1/embeddings route") from exc
        except LiteLLMRejected as exc:
            code = RATE_LIMITED if exc.http_status == 429 else REJECTED
            raise EmbeddingError(code, f"LiteLLM rejected the embedding request ({exc.http_status})") from exc
        except LiteLLMError as exc:
            raise EmbeddingError(UNAVAILABLE, f"LiteLLM embeddings failed: {type(exc).__name__}") from exc
        return Embeddings(vectors=answer.vectors, model=model.model,
                          answered_model=answer.litellm.get("model_name") or answer.model,
                          dimensions=model.dimensions, input_tokens=answer.input_tokens,
                          latency_ms=answer.latency_ms, request_id=answer.request_id)


TOKEN = re.compile(r"[a-z0-9][a-z0-9+#.]*")


def hashed_vector(text: str, dimensions: int) -> list[float]:
    """A deterministic bag-of-words vector (feature hashing, L2-normalised). Test/offline use only: it knows no
    meaning, so it never stands in for a real model's quality."""
    vector = [0.0] * dimensions
    for word in TOKEN.findall((text or "").lower()):
        word = word.rstrip(".")
        if len(word) < 2:
            continue
        digest = hashlib.blake2b(word.encode(), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        vector[index] += 1.0 if digest[4] & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector] if norm else vector


class MockBackend:
    """LLM_PROVIDER=mock (tests, offline development): hashed vectors, no network, no telemetry row."""

    def embed(self, model: EmbeddingModel, texts: list[str], operation: str) -> Embeddings:
        return Embeddings(vectors=[hashed_vector(t, model.dimensions) for t in texts], model=model.model,
                          answered_model=model.model, dimensions=model.dimensions)


# ───────────── the service ─────────────

class EmbeddingService:
    def __init__(self, model: EmbeddingModel, backend: Backend, user_id: int | None = None):
        self.model, self.backend, self.user_id = model, backend, user_id

    @property
    def identity(self) -> EmbeddingModel:
        return self.model

    @classmethod
    def for_user(cls, db: Session, user_id: int) -> EmbeddingService:
        """The account's configured embedding model, or EmbeddingUnavailable saying why there is none."""
        from app.services.ai_config import resolve as resolve_ai_config
        from app.services.user_settings import ai_overrides

        provider = resolve_ai_config(get_settings(), ai_overrides(db, user_id))["llm_provider"]
        if provider == "mock":
            size = int(get_settings().rag_mock_dimensions or 64)
            return cls(EmbeddingModel("mock", "mock-embedding", None, size), MockBackend(), user_id)
        if provider != "litellm":
            raise EmbeddingUnavailable(UNSUPPORTED_PROVIDER, f"Embeddings need the LiteLLM gateway; this account uses "
                                                             f"{provider}")
        from app.services import model_resolution
        from app.services.ai_model_config import gateway_settings

        _version, snapshot = model_resolution.active_snapshot(user_id)
        model, _configured, note = model_resolution.resolve_model(snapshot, WORKER)
        if model is None:
            raise EmbeddingUnavailable(NOT_CONFIGURED, f"No embedding model is assigned to the Embedder ({note})")
        entry = (snapshot.get("models") or {}).get(model) or {}
        if entry.get("model_type") != "EMBEDDING":
            raise EmbeddingUnavailable(NOT_CONFIGURED, f"{model} is not configured as an embedding model")
        size = entry.get("embedding_dimensions")
        if not isinstance(size, int) or size <= 0:
            raise EmbeddingUnavailable(NOT_CONFIGURED, f"The vector size of {model} is unknown")
        base_url, key = gateway_settings(db, user_id)
        if not base_url:
            raise EmbeddingUnavailable(NOT_CONFIGURED, "No LiteLLM base URL is configured")
        return cls(EmbeddingModel("litellm", model, entry.get("underlying_model"), size),
                   LiteLLMBackend(base_url, key, user_id), user_id)

    @classmethod
    def for_version(cls, db: Session, user_id: int, version: Any) -> EmbeddingService:
        """A service that embeds queries with the model an existing index version was built with — the only vectors
        that may be compared with it — whatever the configuration says now (a new model may be backfilling)."""
        model = EmbeddingModel(version.provider, version.model, version.underlying_model, version.dimensions,
                               version.doc_format_version)
        if version.provider == "mock":
            return cls(model, MockBackend(), user_id)
        if version.provider != "litellm":
            raise EmbeddingUnavailable(UNSUPPORTED_PROVIDER, f"Unknown embedding provider {version.provider}")
        from app.services.ai_model_config import gateway_settings

        base_url, key = gateway_settings(db, user_id)
        if not base_url:
            raise EmbeddingUnavailable(NOT_CONFIGURED, "No LiteLLM base URL is configured")
        return cls(model, LiteLLMBackend(base_url, key, user_id), user_id)

    def _embed(self, texts: list[str], operation: str) -> Embeddings:
        if not texts:
            return Embeddings([], self.model.model, None, self.model.dimensions)
        clean = [(t or " ")[:MAX_INPUT_CHARS] for t in texts]
        result = self.backend.embed(self.model, clean, operation)
        if len(result.vectors) != len(clean):
            raise EmbeddingError(UNAVAILABLE, f"{len(result.vectors)} vectors for {len(clean)} inputs")
        for vector in result.vectors:
            if len(vector) != self.model.dimensions:
                raise EmbeddingError(DIMENSION_MISMATCH, f"{self.model.model} returned a {len(vector)}-dimension "
                                                         f"vector; the configuration says {self.model.dimensions}")
            if not all(math.isfinite(x) for x in vector):
                raise EmbeddingError(REJECTED, f"{self.model.model} returned a vector with non-finite values")
        return result

    def embed_documents(self, texts: list[str]) -> Embeddings:
        return self._embed(texts, "embed_documents")

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], "embed_query").vectors[0]


def status(db: Session, user_id: int) -> dict[str, Any]:
    """Whether embeddings are available for the account, and the model (metadata only)."""
    try:
        service = EmbeddingService.for_user(db, user_id)
    except EmbeddingError as exc:
        return {"available": False, "code": exc.code, "reason": str(exc)}
    model = service.identity
    return {"available": True, "provider": model.provider, "model": model.model,
            "underlying_model": model.underlying_model, "dimensions": model.dimensions,
            "doc_format_version": model.doc_format_version}
