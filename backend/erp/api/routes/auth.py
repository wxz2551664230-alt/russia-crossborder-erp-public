from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from erp.auth_models import ErpUser, UserAuditEvent
from erp.core.access import COOKIE_NAME, SESSION_SECONDS, session_cookie, valid_key
from erp.core.config import get_settings
from erp.core.database import get_db
from erp.core.errors import AppError
from erp.services import accounts

router = APIRouter(prefix="/auth", tags=["auth"])
Role = Literal["admin", "operator", "viewer"]


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(default="", max_length=500, repr=False)
    username: str = Field(default="", max_length=64)
    password: str = Field(default="", max_length=128, repr=False)
    remember: bool = False


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=64)
    display_name: str = Field(default="", max_length=100)
    password: str = Field(min_length=accounts.MIN_PASSWORD_LENGTH, max_length=128, repr=False)
    role: Role = "viewer"


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(default=None, max_length=100)
    password: str | None = Field(default=None, min_length=accounts.MIN_PASSWORD_LENGTH, max_length=128, repr=False)
    role: Role | None = None
    enabled: bool | None = None


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    old_password: str = Field(max_length=128, repr=False)
    new_password: str = Field(min_length=accounts.MIN_PASSWORD_LENGTH, max_length=128, repr=False)


def account_mode() -> None:
    if not get_settings().multiuser_enabled:
        raise AppError("尚未开启多人账号模式", code="multiuser_disabled", status_code=409)


def admin(request: Request) -> dict:
    account_mode()
    user = request.state.user
    if user["role"] != "admin":
        raise AppError("需要管理员权限", code="permission_denied", status_code=403)
    return user


@router.get("/config")
def config() -> dict:
    return {"multiuser": get_settings().multiuser_enabled, "ui_profile": get_settings().web_ui_profile}


@router.get("/client-capabilities")
def client_capabilities() -> dict:
    return {"contract": 1, "operations": ["products.search", "products.read", "products.notes.compare_and_set"],
            "execution": "local-client", "full_erp": False}


@router.post("/login")
def login(payload: Login, request: Request, response: Response) -> dict:
    multiuser = get_settings().multiuser_enabled
    if multiuser:
        if payload.token:
            raise AppError("请使用个人账号和密码登录", code="account_login_required", status_code=401)
        user, cookie = accounts.login_user(payload.username, payload.password, request.client.host if request.client else "unknown", remember=payload.remember)
    else:
        if not valid_key(payload.token):
            raise AppError("访问密钥不正确", code="invalid_access_key", status_code=401)
        user = {"id": "owner", "username": "owner", "display_name": "本地管理员", "role": "admin", "enabled": True}
        cookie = session_cookie()
    response.set_cookie(COOKIE_NAME, cookie, max_age=30 * 86400 if multiuser and payload.remember else SESSION_SECONDS, httponly=True,
                        secure=get_settings().session_cookie_secure or request.url.scheme == "https", samesite="strict", path="/")
    response.headers["Cache-Control"] = "no-store"
    return {"operator": user["id"], "user": user, "multiuser": multiuser}


@router.get("/session")
def session(request: Request) -> dict:
    return {"operator": request.state.operator, "user": request.state.user, "multiuser": get_settings().multiuser_enabled}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    if get_settings().multiuser_enabled:
        accounts.logout_user(request.cookies.get(COOKIE_NAME, ""), request.state.user)
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.post("/password")
def password(payload: PasswordChange, request: Request, response: Response) -> dict:
    account_mode()
    accounts.change_password(request.state.user, payload.old_password, payload.new_password)
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True, "login_required": True}


@router.get("/users")
def users(actor: dict = Depends(admin), db: Session = Depends(get_db)) -> list[dict]:
    return [accounts.user_dict(row) for row in db.scalars(select(ErpUser).order_by(ErpUser.created_at))]


@router.post("/users", status_code=201)
def create_user(payload: UserCreate, actor: dict = Depends(admin), db: Session = Depends(get_db)) -> dict:
    return accounts.user_dict(accounts.create_user(db, **payload.model_dump(), actor=actor))


@router.patch("/users/{user_id}")
def update_user(user_id: str, payload: UserUpdate, actor: dict = Depends(admin), db: Session = Depends(get_db)) -> dict:
    return accounts.user_dict(accounts.update_user(db, user_id, payload.model_dump(exclude_unset=True, exclude_none=True), actor))


@router.get("/audit")
def audit(limit: int = Query(100, ge=1, le=500), actor: dict = Depends(admin), db: Session = Depends(get_db)) -> list[dict]:
    return [{"id": row.id, "actor_name": row.actor_name, "action": row.action, "target": row.target,
             "status_code": row.status_code, "created_at": row.created_at}
            for row in db.scalars(select(UserAuditEvent).order_by(UserAuditEvent.created_at.desc()).limit(limit))]
