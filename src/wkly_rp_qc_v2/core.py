from dataclasses import dataclass
from typing import Any

from .assessment import restore_assessment_quotes, validate_assessment
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


@dataclass(frozen=True)
class ValidatedAssessment:
    assessments: list[JsonDict]
    citation_repairs: list[JsonDict]


class AssessmentValidationError(ValueError):
    def __init__(self, message: str, citation_repairs: list[JsonDict]) -> None:
        super().__init__(message)
        self.citation_repairs = citation_repairs


def validate_assessment_payload(
    payload: JsonDict, commitments: list[JsonDict], sources: JsonDict
) -> ValidatedAssessment:
    citation_repairs: list[JsonDict] = []
    try:
        restored, citation_repairs = restore_assessment_quotes(payload, sources)
        assessments = validate_assessment(restored, commitments, sources)
    except (ValueError, KeyError, TypeError) as error:
        raise AssessmentValidationError(str(error), citation_repairs) from error
    return ValidatedAssessment(assessments, citation_repairs)
