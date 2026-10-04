"""Public historical evidence contracts; no complaint counts are inferred from posts."""

from datetime import date
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")]
Channel = Literal["weibo", "douyin", "xiaohongshu", "blackcat", "news", "wechat", "regulatory", "official_status"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceRecord(Contract):
    record_id: Identifier
    channel: Channel
    source_url: HttpUrl
    publisher: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=1500)
    content_kind: Literal["human_summary", "source_excerpt"] = "human_summary"
    data_mode: Literal["real_historical", "synthetic"]
    published_at: AwareDatetime
    available_at: AwareDatetime | None = None
    collected_at: AwareDatetime
    availability_basis: str = Field(min_length=1, max_length=1000)
    historical_integrity: Literal["archived", "retrospective_unverified", "synthetic"]
    acquisition_method: Literal["manual_web_review", "authorized_export", "synthetic_fixture"]
    role: Literal["input_candidate", "evaluation_only", "context_only"]
    limitations: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def coherent_times_and_mode(self):
        if self.published_at > self.collected_at:
            raise ValueError("published_at cannot be after collected_at")
        if self.available_at and not self.published_at <= self.available_at <= self.collected_at:
            raise ValueError("available_at must be between publication and collection")
        synthetic = self.data_mode == "synthetic"
        if synthetic != (self.historical_integrity == "synthetic"):
            raise ValueError("synthetic data must carry synthetic integrity")
        if synthetic != (self.acquisition_method == "synthetic_fixture"):
            raise ValueError("synthetic acquisition and data_mode must agree")
        return self


class CaseImport(Contract):
    case_id: Identifier
    title: str = Field(min_length=1, max_length=300)
    scope: str = Field(min_length=1, max_length=300)
    cutoff: AwareDatetime
    cutoff_basis: str = Field(min_length=1, max_length=1000)
    data_mode: Literal["real_historical", "synthetic"]
    version: str = Field(min_length=1, max_length=100)
    records: list[SourceRecord] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_and_same_mode(self):
        ids = [record.record_id for record in self.records]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate record_id in case")
        if any(record.data_mode != self.data_mode for record in self.records):
            raise ValueError("a case cannot mix real and synthetic records")
        return self


class DailyComplaint(Contract):
    """Contract only. Day 1 has no real daily complaint dataset or training pipeline."""

    business_date: date
    scope: str = Field(min_length=1)
    count: int | None = Field(default=None, ge=0, strict=True)
    coverage: Literal["complete", "partial", "missing"]
    available_at: AwareDatetime
    source_reference: str = Field(min_length=1)
    definition_version: str = Field(min_length=1)
    data_mode: Literal["real_historical", "synthetic"]

    @model_validator(mode="after")
    def missing_is_not_zero(self):
        if self.coverage == "missing" and self.count is not None:
            raise ValueError("missing coverage requires count=null")
        if self.coverage != "missing" and self.count is None:
            raise ValueError("observed coverage requires an explicit count")
        return self


class AgentDecision(Contract):
    """Future Agent action contract; validating it is not a multi-agent simulation."""

    agent_id: Identifier
    action: Literal["observe", "share", "comment", "seek_clarification", "express_complaint_intent"]
    evidence_ids: list[Identifier] = Field(min_length=1, max_length=20)
    reason: str = Field(min_length=1, max_length=500)
