"""HTTP contracts and persisted task snapshots; no provider credentials."""

from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    computed_field,
    field_validator,
    model_validator,
)

from scene_pilot.core.confirmation import (
    BriefDraft,
    DepartureEvidence,
    PreparationUsage,
)
from scene_pilot.core.photography import PhotographyBrief
from scene_pilot.core.recommendation import PhotographyRecommendation
from scene_pilot.core.progress import TaskProgress
from scene_pilot.api.errors import error_message

TaskStatus = Literal[
    "extracting",
    "awaiting_confirmation",
    "ready",
    "running",
    "awaiting_origin",
    "completed",
    "incomplete",
    "budget_exhausted",
    "failed",
    "cancelled",
    "interrupted",
]
ACTIVE = frozenset({"extracting", "running", "awaiting_origin"})
TERMINAL = frozenset(
    {
        "completed",
        "incomplete",
        "budget_exhausted",
        "failed",
        "cancelled",
        "interrupted",
    }
)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateTask(Contract):
    request: str = Field(min_length=1, max_length=12000)

    @field_validator("request")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("request_required")
        return value


class Revision(Contract):
    revision: int = Field(ge=1, strict=True)


class UpdateBrief(Revision):
    brief: BriefDraft


class AcceptBrief(Revision):
    accepted: StrictBool

    @field_validator("accepted")
    @classmethod
    def explicit_acceptance(cls, value):
        if value is not True:
            raise ValueError("confirmation_required")
        return value


class SelectOrigin(Revision):
    evidence_id: str | None = Field(default=None, min_length=1, max_length=80)
    skip: StrictBool = False

    @model_validator(mode="after")
    def exactly_one_choice(self):
        if bool(self.evidence_id) == self.skip:
            raise ValueError("choose_id_or_explicit_skip")
        return self


class PlanningResult(Contract):
    status: Literal[
        "completed",
        "incomplete",
        "budget_exhausted",
        "failed",
        "cancelled",
        "interrupted",
    ]
    recommendation: PhotographyRecommendation | None = None
    final_text: str = ""
    metrics: dict[str, int | float] = Field(default_factory=dict)
    error_code: str | None = None

    @computed_field
    @property
    def error_message(self) -> str | None:
        return error_message(self.error_code)


class TaskSummary(Contract):
    id: str
    status: TaskStatus
    revision: int
    created_at: datetime
    updated_at: datetime
    error_code: str | None = None
    progress: TaskProgress = Field(default_factory=TaskProgress)
    settled: bool = True

    @computed_field
    @property
    def error_message(self) -> str | None:
        return error_message(self.error_code)


class TaskDetail(TaskSummary):
    request: str
    draft: BriefDraft | None = None
    confirmed_brief: PhotographyBrief | None = None
    confirmation_id: str | None = None
    confirmed_at: datetime | None = None
    origin_options: list[DepartureEvidence] = Field(default_factory=list)
    selected_origin_id: str | None = None
    origin_skipped: bool = False


class TaskRecord(TaskDetail):
    usage: PreparationUsage = Field(default_factory=PreparationUsage)
    result: PlanningResult | None = None
    event_sequence: int = 0

    def detail(self) -> TaskDetail:
        return TaskDetail.model_validate(
            self.model_dump(
                exclude={"usage", "result", "event_sequence"},
                exclude_computed_fields=True,
            )
        )

    def summary(self) -> TaskSummary:
        return TaskSummary.model_validate(
            {name: getattr(self, name) for name in TaskSummary.model_fields}
        )


class ResultView(Contract):
    task_id: str
    status: TaskStatus
    result: PlanningResult | None
    error_code: str | None
    settled: bool = True

    @computed_field
    @property
    def error_message(self) -> str | None:
        return error_message(self.error_code)


class TaskEvent(Contract):
    task_id: str
    sequence: int
    revision: int
    status: TaskStatus
    progress: TaskProgress
    updated_at: datetime
    error_code: str | None
    result_available: bool
    settled: bool

    @computed_field
    @property
    def error_message(self) -> str | None:
        return error_message(self.error_code)

    @classmethod
    def from_record(cls, task):
        return cls(
            task_id=task.id,
            sequence=task.event_sequence,
            revision=task.revision,
            status=task.status,
            progress=task.progress,
            updated_at=task.updated_at,
            error_code=task.error_code,
            result_available=task.result is not None,
            settled=task.settled,
        )


class TaskError(Exception):
    """Fixed local error codes only; never provider exception text."""

    def __init__(self, code: str, status_code: int = 409):
        self.code = code
        self.status_code = status_code
        super().__init__(code)
