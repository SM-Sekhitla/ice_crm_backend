from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

STAGES = (
    "Contact",
    "New Lead",
    "Contacted",
    "Interested",
    "Qualified",
    "Proposal Sent",
    "Negotiation",
    "Client",
    "Lost",
    "Not Interested",
    "On Hold",
)


class Schema(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", str_strip_whitespace=True
    )


class Login(Schema):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class Refresh(Schema):
    refresh_token: str = Field(min_length=20, max_length=200)


class PasswordChange(Schema):
    current_password: str = Field(max_length=128)
    password: str = Field(min_length=12, max_length=128)


class UserCreate(Schema):
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    role: Literal["Super Admin", "BDO"] = "BDO"
    product_ids: list[str] = Field(default_factory=list, max_length=100)


class UserUpdate(Schema):
    email: EmailStr | None = None
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    role: Literal["Super Admin", "BDO"]
    active: bool
    product_ids: list[str] = Field(default_factory=list, max_length=100)


class ProductInput(Schema):
    name: str = Field(min_length=1, max_length=150)
    code: str = Field(min_length=1, max_length=30, pattern=r"^[A-Za-z0-9_-]+$")
    type: Literal["Product", "Service", "Project"] = "Product"
    description: str = Field(default="", max_length=10000)
    status: Literal["Active", "Inactive", "Archived"] = "Active"
    color: str = Field(default="#C1121F", pattern=r"^#[0-9A-Fa-f]{6}$")
    owner_id: str


class ContactFields(Schema):
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    phone: str = Field(default="", max_length=50)
    company: str = Field(default="", max_length=200)
    job_title: str = Field(default="", max_length=150)
    source: str = Field(default="", max_length=100)
    notes: str = Field(default="", max_length=20000)
    consent: bool = False
    do_not_email: bool = False
    consent_source: str = Field(default="", max_length=200)
    consent_date: str = ""
    alternative_phone: str = Field(default="", max_length=50)
    industry: str = Field(default="", max_length=150)
    website: str = Field(default="", max_length=500)
    tags: list[str] = Field(default_factory=list, max_length=30)
    address: str = Field(default="", max_length=500)
    city: str = Field(default="", max_length=100)
    province: str = Field(default="", max_length=100)
    country: str = Field(default="", max_length=100)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, tags):
        if any(not tag or len(tag) > 50 for tag in tags):
            raise ValueError("Tags must contain 1–50 characters")
        return list(dict.fromkeys(tags))

    @model_validator(mode="after")
    def validate_consent(self):
        if self.consent and (not self.consent_source or not self.consent_date):
            raise ValueError("Consent requires source and date")
        if self.consent_date and date.fromisoformat(self.consent_date) > date.today():
            raise ValueError("Consent date cannot be in the future")
        return self


class ContactCreate(ContactFields):
    product_ids: list[str] = Field(min_length=1, max_length=100)
    owner_id: str


class AssociationInput(Schema):
    product_id: str
    owner_id: str


class Reassign(Schema):
    owner_id: str


class Transition(Schema):
    stage: str
    expected_value: Decimal | None = Field(
        default=None, ge=0, le=99999999999999, max_digits=16, decimal_places=2
    )
    deal_value: Decimal | None = Field(default=None, ge=0, le=99999999999999, max_digits=16, decimal_places=2)
    priority: Literal["High", "Medium", "Low"] = "Medium"
    reference: str = Field(default="", max_length=100)
    notes: str = Field(default="", max_length=10000)
    follow_up: date | None = None
    conversion_date: date | None = None

    @field_validator("stage")
    @classmethod
    def stage_valid(cls, value):
        if value not in STAGES:
            raise ValueError("Unknown pipeline stage")
        return value


class FollowUpInput(Schema):
    title: str = Field(min_length=1, max_length=200)
    due_date: date
    priority: Literal["High", "Medium", "Low"] = "Medium"
    notes: str = Field(default="", max_length=10000)
    status: Literal["Open", "Completed"] = "Open"


class SignatureInput(Schema):
    name: str = Field(min_length=1, max_length=80)
    html: str = Field(min_length=1, max_length=900000)
    source_file_name: str = Field(default="", max_length=200)


class SenderInput(Schema):
    name: str = Field(min_length=1, max_length=150)
    sender_name: str = Field(min_length=1, max_length=150)
    email: EmailStr
    reply_to: EmailStr
    status: Literal["Verified", "Unverified"] = "Unverified"
    product_ids: list[str] = Field(default_factory=list, max_length=100)
    user_ids: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("name", "sender_name")
    @classmethod
    def no_header_injection(cls, value):
        if "\n" in value or "\r" in value:
            raise ValueError("Header newlines are forbidden")
        return value


class CampaignInput(Schema):
    name: str = Field(min_length=1, max_length=150)
    description: str = Field(default="", max_length=10000)
    product_id: str
    type: Literal[
        "Product Announcement", "Newsletter", "Follow-up", "Promotional", "General Communication"
    ] = "General Communication"
    sender_id: str | None = None
    signature_id: str | None = None
    subject: str = Field(default="", max_length=200)
    body_html: str = Field(default="", max_length=900000)
    recipient_ids: list[str] = Field(default_factory=list, max_length=1000)

    @field_validator("subject")
    @classmethod
    def subject_safe(cls, value):
        if "\r" in value or "\n" in value:
            raise ValueError("Subject must be one line")
        return value

    @field_validator("recipient_ids")
    @classmethod
    def dedupe(cls, value):
        return list(dict.fromkeys(value))


class Launch(Schema):
    mode: Literal["simulation", "email"] = "simulation"

    @field_validator("mode", mode="before")
    @classmethod
    def legacy_live_mode(cls, value):
        return "email" if value == "smtp" else value

    scheduled_at: datetime | None = None
    confirm: Literal[True]

    @field_validator("scheduled_at")
    @classmethod
    def timezone_required(cls, value):
        if value and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Schedule must include timezone offset")
        return value


class SegmentInput(Schema):
    name: str = Field(min_length=1, max_length=100)
    product_id: str
    contact_ids: list[str] = Field(default_factory=list, max_length=1000)


class ContactImport(Schema):
    rows: list[ContactCreate] = Field(min_length=1, max_length=1000)


class BulkTags(Schema):
    contact_ids: list[str] = Field(min_length=1, max_length=1000)
    tags: list[str] = Field(min_length=1, max_length=30)

    @field_validator("tags")
    @classmethod
    def nonempty_tags(cls, value):
        if any(not tag or len(tag) > 50 for tag in value):
            raise ValueError("Tags must contain 1–50 characters")
        return list(dict.fromkeys(value))


class ContactNotes(Schema):
    notes: str = Field(default="", max_length=20000)


class BulkDelete(Schema):
    contact_ids: list[str] = Field(min_length=1, max_length=1000)


class FollowUpStatus(Schema):
    status: Literal["Open", "Completed"]


class InvitationToken(Schema):
    token: str = Field(min_length=32, max_length=200)


class InvitationAccept(InvitationToken):
    password: str = Field(min_length=12, max_length=128)
    confirm_password: str = Field(min_length=12, max_length=128)

    @model_validator(mode="after")
    def passwords_match(self):
        if self.password != self.confirm_password:
            raise ValueError("Passwords do not match")
        return self


class UserDelete(Schema):
    replacement_owner_id: str | None = None
