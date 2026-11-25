from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

UID = Annotated[
    str, StringConstraints(strip_whitespace=True, pattern=r"^[1-9][0-9]{0,19}$")
]
Email = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        max_length=254,
        pattern=r"^[^@\s<>;,]+@[^@\s<>;,]+\.[^@\s<>;,]+$",
    ),
]
Secret = Annotated[str, StringConstraints(min_length=8, max_length=128)]
Token = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{32,80}$")]
Code = Annotated[str, StringConstraints(pattern=r"^[0-9]{6}$")]


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Address(Body):
    name: str = Field(min_length=1, max_length=60)
    phone: str = Field(pattern=r"^1[0-9]{10}$")
    address: str = Field(min_length=3, max_length=300)

    @field_validator("name", "address", mode="before")
    @classmethod
    def strip_text(cls, v):
        return v.strip() if isinstance(v, str) else v


class GiftAddress(Address):
    confirm_refill: bool = False


class UseDefaultAddress(Body):
    use_default: Literal[True]


class AddressPreference(Body):
    auto_fill_address: bool


class EmailRequest(Body):
    email: Email
    purpose: Literal["register", "reset"]


class EmailVerification(Body):
    email: Email
    code: Code


class PasswordReset(EmailVerification):
    password: Secret


class Flow(Body):
    flow: Token


class ConfirmUID(Flow):
    uid: UID


class Registration(ConfirmUID):
    password: Secret
    confirm_password: Secret


class Login(Body):
    identifier: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class ConfirmPassword(Body):
    password: str = Field(min_length=1, max_length=128)


class ChangePassword(ConfirmPassword):
    new_password: Secret


class MailCode(Body):
    code: Code


class Deletion(MailCode):
    confirmed: Literal[True]


class NewEmail(Flow):
    email: Email


class ChangeEmail(NewEmail):
    code: Code


class Decision(Body):
    waive: bool


class AppealInput(Body):
    contact: Email
    uid: UID | None = None
    message: str = Field(min_length=5, max_length=1500)


class AppealReply(Body):
    reply: str = Field(min_length=1, max_length=1000)
