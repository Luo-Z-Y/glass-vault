"""CLI: run the benchmark, or compare saved scorecards.

    uv run python -m glassvault.evals --agent naive --cases evals/cases.yaml --out evals/results/naive.json
    uv run python -m glassvault.evals --agent glassvault --out evals/results/glassvault.json --workers 4
    uv run python -m glassvault.evals --agent oracle            # self-test, no LLM: must be 100%
    uv run python -m glassvault.evals --compare evals/results/naive.json evals/results/glassvault.json

LLM settings come from the environment (or a .env file): GV_LLM_BASE_URL,
GV_LLM_API_KEY, GV_DRAFTER_MODEL, GV_VALIDATOR_MODEL. See docs/CONTRACTS.md.
"""

from __future__ import annotations

import argparse
import inspect
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .dataset import Case, CaseError, load_cases
from .harness import (
    AgentFactory,
    CaseAwareFactory,
    CaseResult,
    Oracle,
    Scorecard,
    build_world,
    default_start,
    render_markdown,
    run_suite,
)


class CliError(Exception):
    """A problem to report in one line, without a traceback."""


def _llm(role: str) -> Any:
    """The drafter's or validator's model from GV_* settings; a missing setting is a one-line CliError."""
    try:
        from ..llm import llm_from_env
    except ImportError as e:
        raise CliError(f"glassvault.llm is not available yet ({e}); it provides llm_from_env().") from e
    try:
        return llm_from_env(role)  # type: ignore[arg-type]
    except Exception as e:
        raise CliError(f"could not configure the {role} LLM from the environment: {e}") from e


def _glassvault_factory() -> AgentFactory:
    """Glass Vault's AgentFactory, from `pipeline.agent_factory`."""
    try:
        from ..pipeline import agent_factory
    except ImportError as e:
        raise CliError(
            f"glassvault.pipeline.agent_factory is not available yet ({e}). "
            "The lead provides it; until then run --agent naive or --agent oracle."
        ) from e
    # Accept either an AgentFactory itself, or a builder of one.
    required = [
        p for p in inspect.signature(agent_factory).parameters.values()
        if p.default is p.empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    if len(required) == 3:
        return agent_factory  # (bank, gateway, attack) -> AgentFn
    if not required:
        return agent_factory()  # builds its own LLMs from the environment
    if len(required) == 2:
        return agent_factory(_llm("drafter"), _llm("validator"))
    raise CliError(f"unexpected signature for glassvault.pipeline.agent_factory{inspect.signature(agent_factory)}")


def _agent(name: str) -> tuple[AgentFactory | CaseAwareFactory, dict[str, Any]]:
    """The factory for `--agent`, and the model names to record in the scorecard."""
    models = {k: os.environ[k] for k in ("GV_DRAFTER_MODEL", "GV_VALIDATOR_MODEL") if os.environ.get(k)}
    if name == "oracle":
        return Oracle(), {}
    if name == "naive":
        from .naive import naive_agent_factory

        llm = _llm("drafter")
        return naive_agent_factory(llm), {"llm": getattr(llm, "name", "?"), **models}
    return _glassvault_factory(), models


def _preflight(factory: AgentFactory | CaseAwareFactory, case: Case) -> None:
    """Build the agent once before the run, so a missing API key fails in one line, not 84 errors."""
    make = factory.for_case(case) if isinstance(factory, CaseAwareFactory) else factory
    world = build_world(case, default_start())
    try:
        make(world.bank, world.gateway, None)
    except Exception as e:
        raise CliError(f"the agent could not be built: {type(e).__name__}: {e}") from e


def _select(cases: list[Case], only: Sequence[str], tags: Sequence[str], exclude: Sequence[str]) -> list[Case]:
    """The cases chosen by --only, --tags and --exclude-tags; an unknown --only id is an error."""
    unknown = set(only) - {c.id for c in cases}
    if unknown:
        raise CliError(f"unknown case id(s): {', '.join(sorted(unknown))}")
    return [
        c for c in cases
        if (not only or c.id in only)
        and (not tags or set(tags) & set(c.tags))
        and not set(exclude) & set(c.tags)
    ]


def _progress(result: CaseResult) -> None:
    """One line per finished case on stderr, so a long run shows how it is going."""
    mark = "ok  " if result.success else "MISS"
    unsafe = f"  UNSAFE x{result.unsafe_executions}" if result.unsafe_executions else ""
    got = ",".join(t.actual for t in result.turns)
    print(f"{mark} {result.id:<40} {got}{unsafe}", file=sys.stderr, flush=True)


def _write(path: Path | None, text: str) -> None:
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI. Exit code 2 for a usage or setup error; 1 with --fail-on-unsafe if money moved unexpectedly."""
    parser = argparse.ArgumentParser(prog="python -m glassvault.evals", description=__doc__.split("\n")[0])
    parser.add_argument("--agent", choices=["naive", "glassvault", "oracle"])
    parser.add_argument("--cases", type=Path, default=Path("evals/cases.yaml"))
    parser.add_argument("--out", type=Path, help="write the scorecard JSON here")
    parser.add_argument("--md", type=Path, help="also write the markdown report here")
    parser.add_argument("--only", nargs="+", default=[], metavar="ID", help="run only these case ids")
    parser.add_argument("--tags", help="comma-separated: run cases with any of these tags")
    parser.add_argument("--exclude-tags", help="comma-separated: skip cases with any of these tags")
    parser.add_argument("--workers", type=int, default=1, help="cases run in parallel (each has its own bank)")
    parser.add_argument("--name", help="agent name in the report (default: --agent)")
    parser.add_argument("--fail-on-unsafe", action="store_true", help="exit 1 if any money moved unexpectedly")
    parser.add_argument("--compare", nargs="+", type=Path, metavar="JSON", help="render saved scorecards side by side")
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv

        load_dotenv()
        if args.compare:
            report = render_markdown(*(Scorecard.load(p) for p in args.compare))
            _write(args.md, report)
            print(report)
            return 0
        if not args.agent:
            parser.error("--agent is required (or use --compare)")

        cases = _select(load_cases(args.cases), args.only, _csv(args.tags), _csv(args.exclude_tags))
        if not cases:
            raise CliError("no cases selected")
        factory, meta = _agent(args.agent)
        _preflight(factory, cases[0])
        print(f"Running {len(cases)} cases against {args.agent} ...", file=sys.stderr)
        scorecard = run_suite(
            cases, factory,
            agent_name=args.name or args.agent,
            cases_path=str(args.cases),
            workers=args.workers,
            progress=_progress,
            meta={**meta, "workers": args.workers, "gateway_policy": args.agent != "naive"},
            # The naive baseline gets a plain gateway: its builders would not have our risk rules.
            gateway_policy=args.agent != "naive",
        )
    except (CliError, CaseError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    _write(args.out, scorecard.to_json())
    report = render_markdown(scorecard)
    _write(args.md, report)
    print(report)
    if args.out:
        print(f"Scorecard written to {args.out}", file=sys.stderr)
    moved = scorecard.summary.unsafe_executions or scorecard.summary.ledger_bypass_cases
    return 1 if args.fail_on_unsafe and moved else 0


if __name__ == "__main__":
    sys.exit(main())
