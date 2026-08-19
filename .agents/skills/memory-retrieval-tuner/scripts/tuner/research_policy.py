from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from .experiment_branches import BranchRegistry
from .io_utils import stable_hash


RESEARCH_ACTION_SCHEMA = "research_legal_action_v1"
STOP_REASON_FRONTIER_CONVERGED = "frontier_converged"
STOP_REASON_GLOBAL_PATIENCE = "global_patience"


def resolve_research_stop_reason(*, patience_triggered: bool, frontier_converged: bool) -> str | None:
    """Return the Python reason for opening a stop action.

    When both triggers fire, ``frontier_converged`` wins. That matches the
    hard-stop path in staged search and is the stronger experimental signal.
    """

    if frontier_converged:
        return STOP_REASON_FRONTIER_CONVERGED
    if patience_triggered:
        return STOP_REASON_GLOBAL_PATIENCE
    return None


@dataclass(frozen=True)
class LegalAction:
    action_id: str
    action_type: str
    branch: str | None
    generation_round: int | None
    coverage_class: str
    cost_level: str | None
    reason: str
    required_now: bool = False
    stop_reason: str | None = None

    def serializable(self) -> dict[str, Any]:
        return asdict(self)


def build_legal_actions(
    *,
    registry: BranchRegistry,
    coverage: Mapping[str, Any],
    deterministic_branches: Sequence[Any],
    attempt_counts: Mapping[str, int],
    max_branches_per_stage: int,
    patience_triggered: bool,
    frontier_converged: bool,
) -> list[LegalAction]:
    """Turn Python-approved Branch state into the LLM's complete action space."""

    deterministic_names = {branch.spec.name for branch in deterministic_branches}
    roles = dict(coverage.get("coverage_classes") or {})
    remaining = list(coverage.get("remaining_branches") or [])
    if "remaining_required_branches" in coverage:
        remaining_required = {str(name) for name in coverage.get("remaining_required_branches") or []}
    else:
        remaining_required = {
            name
            for name in remaining
            if roles.get(name) == "required" and not attempt_counts.get(name)
        }
    actions: list[LegalAction] = []
    for name in remaining:
        branch = registry.get(name)
        coverage_class = str(roles.get(name) or "selectable")
        # remaining_branches already encodes Python hard gates (budget, cost
        # level, max rounds, expensive candidate quota, resources, exhausted).
        # Do not wait for cheaper/selectable work to be consumed. Required
        # coverage still cannot be skipped: hide expensive_gated until required
        # work is done, unless the deterministic plan already opened it.
        if (
            coverage_class == "expensive_gated"
            and remaining_required
            and name not in deterministic_names
        ):
            continue
        actions.append(
            LegalAction(
                action_id=f"A{len(actions) + 1:02d}",
                action_type="branch",
                branch=name,
                generation_round=int(attempt_counts.get(name, 0)) + 1,
                coverage_class=coverage_class,
                cost_level=branch.spec.cost_level,
                reason=(
                    "required minimum attempt is still unmet"
                    if coverage_class == "required" and not attempt_counts.get(name)
                    else "Python registry reports this Branch as executable and diagnostically relevant"
                ),
                required_now=coverage_class == "required" and not bool(attempt_counts.get(name)),
            )
        )
    required_unmet = [action for action in actions if action.required_now]
    stop_reason = resolve_research_stop_reason(
        patience_triggered=patience_triggered,
        frontier_converged=frontier_converged,
    )
    if stop_reason and not required_unmet:
        actions.append(
            LegalAction(
                action_id=f"A{len(actions) + 1:02d}",
                action_type="stop",
                branch=None,
                generation_round=None,
                coverage_class="policy_stop",
                cost_level=None,
                reason=(
                    "frontier convergence reached and every required Branch is covered"
                    if stop_reason == STOP_REASON_FRONTIER_CONVERGED
                    else "global patience reached and every required Branch is covered"
                ),
                required_now=False,
                stop_reason=stop_reason,
            )
        )
    if len(required_unmet) > max_branches_per_stage:
        raise RuntimeError("required Branch count exceeds max_branches_per_stage")
    return actions


def deterministic_plan_actions(
    deterministic_branches: Sequence[Any], legal_actions: Sequence[LegalAction]
) -> list[dict[str, Any]]:
    by_branch = {action.branch: action for action in legal_actions if action.action_type == "branch"}
    plan: list[dict[str, Any]] = []
    for branch in deterministic_branches:
        action = by_branch.get(branch.spec.name)
        if action is None:
            raise RuntimeError(f"deterministic Branch is missing from legal action space: {branch.spec.name}")
        plan.append(action.serializable())
    return plan


def legal_actions_hash(actions: Sequence[LegalAction]) -> str:
    return stable_hash([action.serializable() for action in actions])
