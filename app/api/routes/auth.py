from fastapi import APIRouter, Depends, status

from app.api.deps import get_auth_service, get_current_user
from app.api.schemas import ErrorResponse, LoginRequest, TokenResponse, UserCreate, UserOut
from app.domain.models import User
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
    responses={409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def register(body: UserCreate, auth: AuthService = Depends(get_auth_service)):
    return auth.register(body.username, body.password)


@router.post(
    "/token",
    response_model=TokenResponse,
    summary="Log in and get a JWT",
    responses={401: {"model": ErrorResponse}},
)
def login(body: LoginRequest, auth: AuthService = Depends(get_auth_service)):
    """Username and password in, JWT out.

    Copy the `access_token` into Swagger's **Authorize** box to sign every
    subsequent /jobs call.
    """
    user = auth.authenticate(body.username, body.password)
    return TokenResponse(access_token=auth.issue_token(user))


@router.get("/me", response_model=UserOut, summary="Who am I")
def me(user: User = Depends(get_current_user)):
    return user
