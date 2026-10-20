from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from modmesh.apps.server.auth import Account, Authentication, require_owner
from modmesh.core.diagnostics import ErrorCode, ModMeshError

router = APIRouter(prefix="/v1")
Email = Annotated[str, Field(min_length=3, max_length=320)]
Password = Annotated[str, Field(min_length=15, max_length=128)]
Token = Annotated[str, Field(min_length=43, max_length=43)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Credentials(Input):
    email: Email
    password: Password


class EmailInput(Input):
    email: Email


class CodeInput(Input):
    code: Token


class ResetInput(CodeInput):
    password: Password


class ChangePassword(Input):
    current_password: Annotated[str, Field(min_length=1, max_length=128)]
    password: Password


class ChangeEmail(Credentials):
    pass


def authentication(request: Request) -> Authentication:
    service = getattr(request.app.state, "authentication", None)
    if not isinstance(service, Authentication):
        raise ModMeshError(ErrorCode.STORAGE, "Configure the server database first")
    return service


def peer(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def account(request: Request) -> Account:
    return authentication(request).authenticate(request.headers.get("authorization"))


@router.post("/auth/register", status_code=202)
def register(body: Credentials, request: Request) -> dict[str, str]:
    authentication(request).register(body.email, body.password, peer(request))
    return {"status": "If eligible, a verification code has been sent"}


@router.post("/auth/verification", status_code=202)
def verification(body: EmailInput, request: Request) -> dict[str, str]:
    authentication(request).request_code(body.email, "verify", peer(request))
    return {"status": "If eligible, a verification code has been sent"}


@router.post("/auth/verify")
def verify(body: CodeInput, request: Request) -> dict[str, str]:
    authentication(request).confirm_email(body.code, peer(request))
    return {"status": "verified"}


@router.post("/auth/login")
def login(body: Credentials, request: Request) -> dict[str, object]:
    return authentication(request).login(body.email, body.password, peer(request))


@router.get("/account")
def me(request: Request) -> dict[str, object]:
    return account(request).document()


@router.get("/accounts/{user_id}")
def owned_account(user_id: str, request: Request) -> dict[str, object]:
    current = account(request)
    require_owner(current, user_id)
    return current.document()


@router.post("/auth/logout")
def logout(request: Request) -> dict[str, str]:
    authentication(request).logout(request.headers.get("authorization", ""))
    return {"status": "logged_out"}


@router.post("/auth/logout-all")
def logout_all(request: Request) -> dict[str, str]:
    authentication(request).logout(request.headers.get("authorization", ""), all_sessions=True)
    return {"status": "all_sessions_revoked"}


@router.post("/auth/recovery", status_code=202)
def recovery(body: EmailInput, request: Request) -> dict[str, str]:
    authentication(request).request_code(body.email, "reset", peer(request))
    return {"status": "If eligible, a recovery code has been sent"}


@router.post("/auth/reset")
def reset(body: ResetInput, request: Request) -> dict[str, str]:
    authentication(request).reset_password(body.code, body.password, peer(request))
    return {"status": "password_reset_and_sessions_revoked"}


@router.post("/auth/password")
def password(body: ChangePassword, request: Request) -> dict[str, str]:
    authentication(request).change_password(
        request.headers.get("authorization", ""),
        body.current_password,
        body.password,
        peer(request),
    )
    return {"status": "password_changed_and_sessions_revoked"}


@router.post("/auth/email", status_code=202)
def email(body: ChangeEmail, request: Request) -> dict[str, str]:
    authentication(request).request_email_change(
        request.headers.get("authorization", ""), body.email, body.password, peer(request)
    )
    return {"status": "If eligible, a confirmation code has been sent to the new email"}


@router.post("/auth/email/confirm")
def confirm_email(body: CodeInput, request: Request) -> dict[str, str]:
    authentication(request).confirm_email_change(body.code, peer(request))
    return {"status": "email_changed_and_sessions_revoked"}
