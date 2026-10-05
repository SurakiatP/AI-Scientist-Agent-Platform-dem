"""Strict launch-only configuration for one peer GetTask reconciliation."""

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PeerReconciliationTarget(BaseModel):
    """Trusted operation-scoped target for one bounded remote GetTask attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str = Field(min_length=1, max_length=200)
    attempt: int = Field(strict=True, ge=1, le=10)

    @field_validator("operation_id")
    @classmethod
    def nonblank_operation(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("operation_id must not be blank")
        return value
