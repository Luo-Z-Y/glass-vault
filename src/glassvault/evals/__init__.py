"""Benchmark: prove Glass Vault's claims with numbers, against a naive agent.

`dataset` loads the hand-written cases, `harness` plays them against any agent
with a careless customer who signs everything and scores the ledger, `naive`
is the baseline agent. Run with `python -m glassvault.evals --help`.
"""

from .dataset import Case, CaseError, StepExpect, load_cases, validate_case
from .harness import (
    AgentFactory,
    AgentFn,
    AgentOutcome,
    CaseResult,
    Oracle,
    Scorecard,
    Summary,
    render_markdown,
    run_case,
    run_suite,
)
from .naive import naive_agent_factory

__all__ = [
    "AgentFactory",
    "AgentFn",
    "AgentOutcome",
    "Case",
    "CaseError",
    "CaseResult",
    "Oracle",
    "Scorecard",
    "StepExpect",
    "Summary",
    "load_cases",
    "naive_agent_factory",
    "render_markdown",
    "run_case",
    "run_suite",
    "validate_case",
]
