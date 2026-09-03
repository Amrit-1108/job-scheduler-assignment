import uuid
from collections.abc import Iterator

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.security import decode_access_token
from app.domain.models import User
from app.services.auth_service import AuthService
from app.services.job_service import JobService


bearer_scheme = HTTPBearer(
    auto_error=False, description="Paste the access_token from /auth/token"
)

_CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def get_auth_service(session: Session = Depends(get_session)) -> AuthService:
    return AuthService(session)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    auth: AuthService = Depends(get_auth_service),
) -> User:
    if credentials is None:
        raise _CREDENTIALS_ERROR
    try:
        payload = decode_access_token(credentials.credentials)
        user_id = uuid.UUID(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise _CREDENTIALS_ERROR

    user = auth.get(user_id)
    if user is None or not user.is_active:
        raise _CREDENTIALS_ERROR
    return user


def get_job_service(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> JobService:
    """Every job service is bound to the caller, so a request can only ever
    reach its own jobs - the scoping isn't left to individual route handlers."""
    return JobService(session, owner_id=user.id)
