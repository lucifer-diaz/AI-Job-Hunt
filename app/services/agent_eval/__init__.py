"""Phase 7 offline evaluation: versioned LangSmith-compatible datasets, suites over the production code, and review
gates (README §9e). Nothing here calls a hosted service: `--jsonl` writes Examples for a manual LangSmith import, and
the only path to LangSmith stays app.agents.tracing_boundary. `feedback` reads live correction rates per account."""
from app.services.agent_eval.gates import GATES, evaluate
from app.services.agent_eval.suites import SUITES, SuiteResult, dataset_for, run

__all__ = ["GATES", "SUITES", "SuiteResult", "dataset_for", "evaluate", "run"]
