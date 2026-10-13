"""A LangChain-compatible chat model that is only a door into the existing LLM gateway.

    graph node → GatewayChatModel(user_id, prompt, call).invoke(prompt_messages(prompt))
               → LLMClient.for_user(...).generate_prompt(prompt, call)      ← app/services/llm.py, unchanged
               → worker resolution, active AIConfigVersion, failover, escalation, LiteLLM fallbacks,
                 contract validation and correction retry, LLMRequestLog + ExternalAPIRequestLog

It adds nothing to routing and records nothing itself, so a call made through it is the same call, with the same
rows, as one made directly. There is no `model` argument: the worker's model comes from its configuration. The prompt
must be a registered app/prompts prompt, bound when the model is made; the messages a graph passes must be exactly
that prompt's messages, so no prompt text can be written in a graph node. The gateway answers JSON objects only:
tool binding and structured-output helpers are not supported (they would need a gateway that does not exist yet).
"""
from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict

from app.prompts import PROMPT_VERSIONS
from app.prompts.common import Prompt
from app.services.ai_workers import worker_for_prompt
from app.services.llm import LLMClient, LLMConfigError

ROUTING_FIELDS = ("worker_type", "mode", "configured", "requested_model", "model_group", "routing_tier",
                  "fallback_used", "escalated", "escalation_reason", "config_version", "latency_ms")


def prompt_messages(prompt: Prompt) -> list[BaseMessage]:
    """The bound prompt's own messages, as LangChain messages."""
    return [SystemMessage(content=prompt.system), HumanMessage(content=prompt.user)]


class GatewayChatModel(BaseChatModel):
    """LangChain's chat-model interface over `LLMClient.generate_prompt` for one prompt and account."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    user_id: int
    prompt: Any  # a Prompt from app/prompts, kept as given (checked in model_post_init)
    call: Any = None  # an LLMCall (job, attempt, validator), passed to the gateway unchanged
    # A session factory (app.db.SessionLocal by default); the client is built per call, like every other service.
    sessions: Callable[[], Any] | None = None

    def model_post_init(self, context: Any) -> None:
        if not isinstance(self.prompt, Prompt) or PROMPT_VERSIONS.get(self.prompt.name) != self.prompt.version:
            raise LLMConfigError("GatewayChatModel needs a prompt built by app/prompts (registered name and version)")

    @property
    def _llm_type(self) -> str:
        return "jobhunt-gateway"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"prompt_name": self.prompt.name, "prompt_version": self.prompt.version}

    def _check(self, messages: Sequence[BaseMessage]) -> None:
        expected = [(m.type, m.content) for m in prompt_messages(self.prompt)]
        if [(m.type, m.content) for m in messages] != expected:
            raise LLMConfigError("GatewayChatModel only sends its bound app/prompts prompt; "
                                 "build messages with prompt_messages(prompt)")

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                  run_manager: CallbackManagerForLLMRun | None = None, **kwargs: Any) -> ChatResult:
        if stop or kwargs:
            raise LLMConfigError("generation settings come from the worker's configuration, not from the caller")
        self._check(messages)
        factory = self.sessions
        if factory is None:
            from app.db import SessionLocal

            factory = SessionLocal
        with factory() as db:
            db.info["user_id"] = self.user_id
            client = LLMClient.for_user(db, self.user_id)
        result = client.generate_prompt(self.prompt, self.call)
        validated = result.validated
        parsed = validated.model_dump(mode="json") if hasattr(validated, "model_dump") else result.data
        routing = {k: v for k, v in (result.routing or {}).items() if k in ROUTING_FIELDS}
        usage = result.usage
        attribution = {"prompt_name": self.prompt.name, "prompt_version": self.prompt.version,
                       "provider": result.provider, "model": result.model,
                       "worker_type": routing.get("worker_type") or worker_for_prompt(self.prompt.name), **routing,
                       "input_tokens": getattr(usage, "input_tokens", None),
                       "output_tokens": getattr(usage, "output_tokens", None)}
        message = AIMessage(content=json.dumps(result.data), additional_kwargs={"parsed": parsed},
                            response_metadata={"model_name": result.model, "provider": result.provider,
                                               "routing": routing})
        if usage is not None and usage.input_tokens is not None and usage.output_tokens is not None:
            message.usage_metadata = {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
                                      "total_tokens": usage.input_tokens + usage.output_tokens}
        return ChatResult(generations=[ChatGeneration(message=message)], llm_output={"jobhunt": attribution})

    def bind_tools(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("The Job Hunt OS gateway answers JSON objects only; tool calls are not supported")

    def with_structured_output(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("Structured output comes from the bound prompt's contract (additional_kwargs['parsed'])")
