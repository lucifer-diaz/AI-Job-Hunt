"""Agent graphs: a small LangGraph runtime on top of the existing services (docs/architecture/agentic-roadmap.md).

    API / scheduler  → app.agents.runtime (lease on agent_threads, ownership check)
                     → LangGraph graph (app.agents.pilot, …) with a PostgreSQL checkpointer (app.agents.checkpoint)
                     → existing services; LLM calls only through app.agents.llm → LLMClient.generate_prompt
                     → side effects only through app.agents.effects.once
                     ⇢ optional LangSmith export only through app.agents.tracing_boundary

PostgreSQL business tables stay authoritative: a checkpoint is resumable computation, never a job state, an approval
or an application. Everything here is off unless AGENT_GRAPHS_ENABLED.
"""
