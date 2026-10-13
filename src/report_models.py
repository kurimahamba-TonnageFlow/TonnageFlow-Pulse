"""Version-one persisted report contract; rejects non-finite metric values."""

from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Metrics(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)
    actual_packs: float | None
    target_packs: float | None
    output_percent: float | None
    running_minutes: float = Field(ge=0)
    missing_hours: int = Field(ge=0)


class Window(BaseModel):
    model_config = ConfigDict(extra="allow")
    start: datetime
    end: datetime
    timezone: str

    @model_validator(mode="after")
    def valid_window(self):
        if (
            self.start.tzinfo is None
            or self.end.tzinfo is None
            or self.end <= self.start
        ):
            raise ValueError("Invalid report window")
        return self


class ReportSnapshot(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)
    id: UUID
    type: Literal["linetech", "engineering", "management"]
    line: str
    window: Window
    generated_at: datetime
    generated_by: dict
    version: int = Field(ge=1)
    template_version: Literal[1]
    metrics: Metrics
    source_refs: list[dict]
    losses: dict
    hourly: dict
    handover_note: str = Field(max_length=500)
