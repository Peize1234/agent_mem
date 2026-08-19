from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from .experiment_branches import BranchRegistry
from .io_utils import stable_hash


RESEARCH_ACTION_SCHEMA = "research_legal_action_v1"


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

    deterministic_names = [branch.spec.name for branch in deterministic_branches]
    roles = dict(coverage.get("coverage_classes") or {})
    remaining = list(coverage.get("remaining_branches") or [])
    non_expensive_remaining = [name for name in remaining if roles.get(name) != "expensive_gated"]
    actions: list[LegalAction] = []
    for name in remaining:
        branch = registry.get(name)
        coverage_class = str(roles.get(name) or "selectable")
        # Expensive Branches stay Python-gated. The unchanged deterministic
        # policy opening the Branch is itself sufficient evidence that the gate
        # has been reached; otherwise cheaper legal work must be consumed first.
        if coverage_class == "expensive_gated" and name not in deterministic_names and non_expensive_remaining:
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
    if (patience_triggered or frontier_converged) and not required_unmet:
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
                    if frontier_converged
                    else "global patience reached and every required Branch is covered"
                ),
                required_now=False,
                stop_reason="frontier_converged",
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
