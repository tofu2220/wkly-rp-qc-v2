from dataclasses import dataclass
from typing import Any

from .evidence import build_sources, period_context, select_scope

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class PreparedScope:
    scope: JsonDict
    sources: dict[str, JsonDict]
    period_context: JsonDict


def prepare_scope(
    rows: list[JsonDict], *, previous: str, current: str, project: str, role: str, aliases: list[str]
) -> PreparedScope:
    scope = select_scope(rows, previous, current, project, role, aliases)
    return PreparedScope(
        scope=scope,
        sources=build_sources(scope),
        period_context=period_context(scope),
    )
