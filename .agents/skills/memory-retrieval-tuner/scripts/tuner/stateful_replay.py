"""Within-session production-order replay for Memory Evolution experiments."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


@dataclass
class ReplayStep:
    query_id: str
    turn_index_before_search: int
    visible: list[dict[str, Any]]
    valid_page_ids: list[str]
    turn_index_after_add: int
    search_before_add: bool = True


@dataclass
class StatefulReplayResult:
    steps: list[ReplayStep] = field(default_factory=list)
    status: str = "COMPLETE"

    @property
    def search_before_add(self) -> bool:
        return all(step.search_before_add for step in self.steps)


class WithinSessionStatefulReplay:
    """Replay Search → confirm recall/Heat → Add in real conversation order.

    ``turn_index`` is read from the production adapter/database.  No page
    sequence or synthetic elapsed clock is used for evolution.
    """

    def __init__(
        self,
        *,
        search: Callable[[Any, int], list[dict[str, Any]]],
        add: Callable[[Any], Any],
        current_turn_index: Callable[[], int],
        valid_recall: Callable[[Any, list[dict[str, Any]]], Iterable[str]] | None = None,
        record_recall: Callable[[list[str], int], Any] | None = None,
    ) -> None:
        self.search = search
        self.add = add
        self.current_turn_index = current_turn_index
        self.valid_recall = valid_recall or (lambda _turn, visible: [str(item.get("id")) for item in visible if item.get("id")])
        self.record_recall = record_recall or (lambda _ids, _turn_index: None)

    def replay(self, turns: Iterable[Any]) -> StatefulReplayResult:
        result = StatefulReplayResult()
        for turn in turns:
            before = int(self.current_turn_index())
            # The callback is invoked before Add by construction.  A callback
            # that mutates source state before returning is a producer bug and
            # can be detected by the caller's turn-index implementation.
            visible = list(self.search(turn, before) or [])
            valid_ids = list(dict.fromkeys(str(value) for value in self.valid_recall(turn, visible) if value))
            self.record_recall(valid_ids, before)
            self.add(turn)
            after = int(self.current_turn_index())
            if after <= before:
                raise RuntimeError("production Add did not advance the conversation turn_index")
            result.steps.append(
                ReplayStep(
                    query_id=str(getattr(turn, "query_id", turn)),
                    turn_index_before_search=before,
                    visible=visible,
                    valid_page_ids=valid_ids,
                    turn_index_after_add=after,
                )
            )
        return result


# Short alias used by adapters and downstream experiments.
StatefulReplay = WithinSessionStatefulReplay
