import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import create_access_token, hash_password, verify_password
from app.domain.errors import DomainError, ValidationError
from app.domain.models import User


class InvalidCredentials(DomainError):
    status_code = 401
    code = "invalid_credentials"

    def __init__(self) -> None:
        super().__init__("Incorrect username or password")


class UsernameTaken(DomainError):
    status_code = 409
    code = "username_taken"


class AuthService:
    def __init__(self, session: Session):
        self.session = session

    def register(self, username: str, password: str) -> User:
        username = (username or "").strip().lower()
        if len(username) < 3:
            raise ValidationError("username must be at least 3 characters")
        if len(password or "") < 8:
            raise ValidationError("password must be at least 8 characters")

        user = User(username=username, password_hash=hash_password(password))
        self.session.add(user)
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            raise UsernameTaken("That username is already registered")
        return user

    def authenticate(self, username: str, password: str) -> User:
        user = self.session.scalar(
            select(User).where(User.username == (username or "").strip().lower())
        )
        if user is None or not verify_password(password, user.password_hash):
            raise InvalidCredentials()
        if not user.is_active:
            raise InvalidCredentials()
        return user

    def issue_token(self, user: User) -> str:
        return create_access_token(user.id, user.username)

    def get(self, user_id: uuid.UUID) -> User | None:
        return self.session.get(User, user_id)
