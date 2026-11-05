"""登录接口契约；密码不去除空格。"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[a-zA-Z0-9_.-]{3,64}$")]
    password: Annotated[str, StringConstraints(min_length=1, max_length=128)]


class RegisterRequest(LoginRequest):
    password: Annotated[str, StringConstraints(min_length=12, max_length=128)]
    confirm_password: Annotated[str, StringConstraints(min_length=12, max_length=128)]

    @model_validator(mode="after")
    def passwords_match(self):
        if self.password != self.confirm_password:
            raise ValueError("两次输入的密码不一致。")
        return self


class UserResponse(BaseModel):
    id: str
    username: str


class SessionResponse(BaseModel):
    user: UserResponse
    csrf_token: str
