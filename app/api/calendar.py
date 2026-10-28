"""Shared internal diary. Only an event's organiser or a Super Admin can change it."""

from datetime import timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import AwareDatetime, Field, HttpUrl, field_validator, model_validator
from sqlalchemy import select

from app.api.deps import Session, require
from app.api.schemas import Schema
from app.core.permissions import ADMIN
from app.db.models import CalendarEvent, Contact, Product, User
from app.services.common import audit, serialize

router = APIRouter(prefix="/calendar", tags=["Calendar"])
SAST = timezone(timedelta(hours=2))


class EventInput(Schema):
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    kind: str = "Meeting"
    status: str = "Scheduled"
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    all_day: bool = False
    location: str = Field(default="", max_length=250)
    meeting_url: str = Field(default="", max_length=1000)
    contact_id: str | None = Field(default=None, max_length=36)
    product_id: str | None = Field(default=None, max_length=36)
    participant_ids: list[str] = Field(default_factory=list, max_length=100)
    reminder_minutes: int | None = Field(default=15, ge=0, le=10080)

    @field_validator("title")
    @classmethod
    def title_required(cls, value):
        if not value.strip():
            raise ValueError("Enter an event title")
        return value.strip()

    @field_validator("kind")
    @classmethod
    def known_kind(cls, value):
        if value not in {"Meeting", "Call", "Task", "Reminder", "Appointment"}:
            raise ValueError("Choose a supported event type")
        return value

    @field_validator("status")
    @classmethod
    def known_status(cls, value):
        if value not in {"Scheduled", "Completed", "Cancelled"}:
            raise ValueError("Choose a supported event status")
        return value

    @field_validator("meeting_url")
    @classmethod
    def safe_meeting_url(cls, value):
        from pydantic import TypeAdapter

        value = value.strip()
        if value:
            url = TypeAdapter(HttpUrl).validate_python(value)
            if url.scheme != "https" or url.username or url.password or any(c in value for c in "\r\n"):
                raise ValueError("Use an HTTPS meeting link without credentials")
        return value

    @field_validator("participant_ids")
    @classmethod
    def unique_people(cls, value):
        if any(not person or len(person) > 36 for person in value):
            raise ValueError("Invalid attendee")
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def valid_interval(self):
        if self.ends_at <= self.starts_at:
            raise ValueError("The end must be after the start")
        if self.ends_at - self.starts_at > timedelta(days=366):
            raise ValueError("An event cannot exceed 366 days")
        if self.all_day:
            for moment in (self.starts_at, self.ends_at):
                local = moment.astimezone(SAST)
                if any((local.hour, local.minute, local.second, local.microsecond)):
                    raise ValueError("All-day events must start and end at midnight in South African time")
        return self


class EventUpdate(EventInput):
    version: int = Field(ge=1)


async def validate_references(session, body):
    for model, key in ((Contact, body.contact_id), (Product, body.product_id)):
        if key and not await session.get(model, key):
            raise HTTPException(422, "A linked CRM record no longer exists")
    if body.participant_ids:
        existing = set(
            await session.scalars(
                select(User.id).where(
                    User.id.in_(body.participant_ids),
                    User.active.is_(True),
                    User.deleted_at.is_(None),
                    User.password_set.is_(True),
                )
            )
        )
        if existing != set(body.participant_ids):
            raise HTTPException(422, "Choose active team members for attendees")


async def editable_event(session, user, event_id):
    event = await session.scalar(select(CalendarEvent).where(CalendarEvent.id == event_id).with_for_update())
    if not event:
        raise HTTPException(404, "Event not found")
    if user.role != ADMIN and event.created_by != user.id:
        raise HTTPException(403, "Only the organiser or a Super Admin can change this event")
    return event


@router.get("/people")
async def calendar_people(session: Session, user=Depends(require("calendar.view"))):
    records = await session.scalars(
        select(User)
        .where(User.active.is_(True), User.deleted_at.is_(None), User.password_set.is_(True))
        .order_by(User.first_name, User.last_name)
    )
    return [{"id": person.id, "name": f"{person.first_name} {person.last_name}"} for person in records]


@router.get("/events")
async def events(
    session: Session,
    start: AwareDatetime,
    end: AwareDatetime,
    user=Depends(require("calendar.view")),
):
    if end <= start or end - start > timedelta(days=93):
        raise HTTPException(422, "Choose a calendar range of up to 93 days")
    records = list(
        await session.scalars(
            select(CalendarEvent)
            .where(CalendarEvent.starts_at < end, CalendarEvent.ends_at > start)
            .order_by(CalendarEvent.starts_at, CalendarEvent.id)
            .limit(1001)
        )
    )
    return {"items": [serialize(event) for event in records[:1000]], "truncated": len(records) > 1000}


@router.post("/events", status_code=201)
async def add_event(body: EventInput, session: Session, user=Depends(require("calendar.manage"))):
    await validate_references(session, body)
    event = CalendarEvent(**body.model_dump(), created_by=user.id, version=1)
    session.add(event)
    audit(session, user, "Added calendar event", body.contact_id, body.product_id)
    await session.flush()
    await session.refresh(event)
    return serialize(event)


@router.put("/events/{event_id}")
async def update_event(
    event_id: str, body: EventUpdate, session: Session, user=Depends(require("calendar.manage"))
):
    event = await editable_event(session, user, event_id)
    if event.version != body.version:
        raise HTTPException(409, "This event changed. Refresh the calendar and try again.")
    await validate_references(session, body)
    for key, value in body.model_dump(exclude={"version"}).items():
        setattr(event, key, value)
    event.version += 1
    audit(session, user, "Updated calendar event", body.contact_id, body.product_id)
    await session.flush()
    await session.refresh(event)
    return serialize(event)


@router.delete("/events/{event_id}", status_code=204)
async def delete_event(
    event_id: str, session: Session, version: int = Query(ge=1), user=Depends(require("calendar.manage"))
):
    event = await editable_event(session, user, event_id)
    if event.version != version:
        raise HTTPException(409, "This event changed. Refresh the calendar and try again.")
    audit(session, user, "Deleted calendar event", event.contact_id, event.product_id)
    await session.delete(event)
