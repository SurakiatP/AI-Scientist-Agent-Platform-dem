"""Private worker-to-broker API; never mounted under the owner/external REST router."""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

from scientist import broker
from scientist.auth import DomainError
from scientist.contracts import OperationRequest, OperationResult
from scientist.db import session

router = APIRouter()


@router.post("/effects", response_model=OperationResult)
def create_effect(
    request: OperationRequest,
    x_worker_capability: str = Header(min_length=1, max_length=4096),
) -> OperationResult:
    with session() as db:
        try:
            result = broker.execute(db, x_worker_capability, request)
        except DomainError as exc:
            raise HTTPException(status_code=exc.status, detail={"code": exc.code}) from exc
        return result
