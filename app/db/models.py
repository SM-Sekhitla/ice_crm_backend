from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Record:
    __mapper_args__ = {"eager_defaults": True}
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Record, Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('Super Admin', 'BDO')"),)
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(30), default="BDO")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    token_version: Mapped[int] = mapped_column(default=0)


class RefreshToken(Record, Base):
    __tablename__ = "refresh_tokens"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class Product(Record, Base):
    __tablename__ = "products"
    __table_args__ = (CheckConstraint("status IN ('Active', 'Inactive', 'Archived')"),)
    name: Mapped[str] = mapped_column(String(150))
    code: Mapped[str] = mapped_column(String(30), unique=True)
    type: Mapped[str] = mapped_column(String(30))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="Active")
    color: Mapped[str] = mapped_column(String(7), default="#C1121F")
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))


class Assignment(Base):
    __tablename__ = "product_assignments"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), primary_key=True)


class Contact(Record, Base):
    __tablename__ = "contacts"
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(254), unique=True)
    phone: Mapped[str] = mapped_column(String(50), default="")
    company: Mapped[str] = mapped_column(String(200), default="")
    job_title: Mapped[str] = mapped_column(String(150), default="")
    source: Mapped[str] = mapped_column(String(100), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    consent: Mapped[bool] = mapped_column(Boolean, default=False)
    do_not_email: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_source: Mapped[str] = mapped_column(String(200), default="")
    consent_date: Mapped[str] = mapped_column(String(10), default="")
    alternative_phone: Mapped[str] = mapped_column(String(50), default="")
    industry: Mapped[str] = mapped_column(String(150), default="")
    website: Mapped[str] = mapped_column(String(500), default="")
    tags: Mapped[list] = mapped_column(JSONB, default=list)
    address: Mapped[str] = mapped_column(String(500), default="")
    city: Mapped[str] = mapped_column(String(100), default="")
    province: Mapped[str] = mapped_column(String(100), default="")
    country: Mapped[str] = mapped_column(String(100), default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Association(Record, Base):
    __tablename__ = "associations"
    __table_args__ = (UniqueConstraint("contact_id", "product_id"),)
    contact_id: Mapped[str] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    stage: Mapped[str] = mapped_column(String(40), default="Contact")


class Lead(Record, Base):
    __tablename__ = "leads"
    __table_args__ = (CheckConstraint("expected_value >= 0"),)
    association_id: Mapped[str] = mapped_column(
        ForeignKey("associations.id", ondelete="CASCADE"), unique=True
    )
    expected_value: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=0)
    priority: Mapped[str] = mapped_column(String(10), default="Medium")
    next_follow_up: Mapped[str] = mapped_column(String(10), default="")
    converted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Client(Record, Base):
    __tablename__ = "clients"
    __table_args__ = (CheckConstraint("deal_value >= 0"),)
    association_id: Mapped[str] = mapped_column(
        ForeignKey("associations.id", ondelete="CASCADE"), unique=True
    )
    client_since: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deal_value: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=0)
    reference: Mapped[str] = mapped_column(String(100), default="")


class FollowUp(Record, Base):
    __tablename__ = "follow_ups"
    association_id: Mapped[str] = mapped_column(ForeignKey("associations.id", ondelete="CASCADE"), index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(200))
    due_date: Mapped[str] = mapped_column(String(10))
    priority: Mapped[str] = mapped_column(String(10), default="Medium")
    notes: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="Open")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Activity(Record, Base):
    __tablename__ = "activities"
    contact_id: Mapped[str | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"), index=True)
    product_id: Mapped[str | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    description: Mapped[str] = mapped_column(Text)


class Sender(Record, Base):
    __tablename__ = "senders"
    name: Mapped[str] = mapped_column(String(150))
    sender_name: Mapped[str] = mapped_column(String(150))
    email: Mapped[str] = mapped_column(String(254))
    reply_to: Mapped[str] = mapped_column(String(254))
    provider: Mapped[str] = mapped_column(String(30), default="SMTP")
    status: Mapped[str] = mapped_column(String(20), default="Unverified")
    product_ids: Mapped[list] = mapped_column(JSONB, default=list)
    user_ids: Mapped[list] = mapped_column(JSONB, default=list)


class Signature(Record, Base):
    __tablename__ = "signatures"
    __table_args__ = (UniqueConstraint("owner_id", "name"),)
    name: Mapped[str] = mapped_column(String(80))
    html: Mapped[str] = mapped_column(Text)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    source_file_name: Mapped[str] = mapped_column(String(200), default="")


class Campaign(Record, Base):
    __tablename__ = "campaigns"
    name: Mapped[str] = mapped_column(String(150))
    description: Mapped[str] = mapped_column(Text, default="")
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    type: Mapped[str] = mapped_column(String(50), default="General Communication")
    status: Mapped[str] = mapped_column(String(30), default="Draft", index=True)
    sender_id: Mapped[str | None] = mapped_column(ForeignKey("senders.id"))
    signature_id: Mapped[str | None] = mapped_column(ForeignKey("signatures.id"))
    subject: Mapped[str] = mapped_column(String(200), default="")
    body_html: Mapped[str] = mapped_column(Text, default="")
    recipient_ids: Mapped[list] = mapped_column(JSONB, default=list)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    mode: Mapped[str] = mapped_column(String(20), default="simulation")
    # Snapshot signature at submission so historical messages cannot change.
    signature_html: Mapped[str] = mapped_column(Text, default="")


class Recipient(Record, Base):
    __tablename__ = "campaign_recipients"
    __table_args__ = (UniqueConstraint("campaign_id", "contact_id"),)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    contact_id: Mapped[str | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    email: Mapped[str] = mapped_column(String(254))
    name: Mapped[str] = mapped_column(String(201))
    status: Mapped[str] = mapped_column(String(20), default="Queued", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    tokens: Mapped[dict] = mapped_column(JSONB, default=dict)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AudienceSegment(Record, Base):
    __tablename__ = "audience_segments"
    name: Mapped[str] = mapped_column(String(100))
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    contact_ids: Mapped[list] = mapped_column(JSONB, default=list)
