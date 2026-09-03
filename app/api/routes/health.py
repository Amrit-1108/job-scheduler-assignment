from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.schemas import HealthResponse

router = APIRouter(tags=["ops"])


@router.get("/health", response_model=HealthResponse)
def health(response: Response, session: Session = Depends(get_session)):
    try:
        session.execute(text("SELECT 1"))
        return HealthResponse(status="ok", database=True)
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(status="degraded", database=False)
