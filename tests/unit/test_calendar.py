"""Calendar schemas, authorization and concurrency checks without live services."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

os.environ.setdefault("SECRET_KEY", "test-only-secret-key-with-more-than-32-characters")

from app.api import calendar
from app.api.deps import current_user
from app.core.security import now
from app.db.models import CalendarEvent
from app.db.session import get_session


def body(**changes):
    return {
        "title": "Team demo",
        "startsAt": "2026-10-28T09:00:00+02:00",
        "endsAt": "2026-10-28T10:00:00+02:00",
        **changes,
    }


class CalendarSchemaTests(unittest.TestCase):
    def test_requires_aware_and_increasing_dates_and_real_title(self):
        for values in (
            body(title="   "),
            body(startsAt="2026-10-28T09:00:00"),
            body(endsAt="2026-10-28T08:00:00+02:00"),
            body(kind="Unknown"),
            body(status="Hidden"),
            body(createdBy="other"),
            body(meetingUrl="javascript:alert(1)"),
            body(meetingUrl="https://user:password@example.com"),
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                calendar.EventInput(**values)

    def test_all_day_midnight_is_validated_in_south_african_time(self):
        valid = calendar.EventInput(
            **body(allDay=True, startsAt="2026-10-27T22:00:00Z", endsAt="2026-10-28T22:00:00Z")
        )
        self.assertTrue(valid.all_day)
        with self.assertRaises(ValidationError):
            calendar.EventInput(**body(allDay=True))

    def test_participants_are_deduplicated_and_updates_require_a_version(self):
        self.assertEqual(calendar.EventInput(**body(participantIds=["one", "one"])).participant_ids, ["one"])
        with self.assertRaises(ValidationError):
            calendar.EventUpdate(**body())


class CalendarApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.user = SimpleNamespace(id="owner", role="BDO")
        self.session = SimpleNamespace(
            scalars=AsyncMock(return_value=[]),
            scalar=AsyncMock(),
            get=AsyncMock(),
            add=Mock(),
            flush=AsyncMock(),
            refresh=AsyncMock(),
            delete=AsyncMock(),
        )

    async def test_any_signed_in_user_can_read_the_shared_range(self):
        self.session.scalars.return_value = [
            CalendarEvent(id="other-event", created_by="other", title="Meeting", created_at=now())
        ]
        response = await calendar.events(
            self.session,
            calendar.EventInput(**body()).starts_at,
            calendar.EventInput(**body()).ends_at,
            self.user,
        )
        self.assertEqual(response["items"][0]["id"], "other-event")
        query = str(self.session.scalars.call_args.args[0])
        self.assertNotIn("calendar_events.created_by =", query)
        self.assertIn("calendar_events.starts_at <", query)

    async def test_owner_and_admin_can_edit_but_other_bdos_cannot(self):
        event = CalendarEvent(id="event", created_by="owner", version=1)
        self.session.scalar.return_value = event
        self.assertIs(await calendar.editable_event(self.session, self.user, "event"), event)
        self.assertIs(
            await calendar.editable_event(
                self.session, SimpleNamespace(id="admin", role="Super Admin"), "event"
            ),
            event,
        )
        with self.assertRaises(HTTPException) as denied:
            await calendar.editable_event(self.session, SimpleNamespace(id="other", role="BDO"), "event")
        self.assertEqual(denied.exception.status_code, 403)

    async def test_stale_edits_and_deletes_do_not_overwrite_events(self):
        self.session.scalar.return_value = CalendarEvent(id="event", created_by="owner", version=2)
        with self.assertRaises(HTTPException) as edit:
            await calendar.update_event(
                "event", calendar.EventUpdate(**body(version=1)), self.session, self.user
            )
        self.assertEqual(edit.exception.status_code, 409)
        with self.assertRaises(HTTPException) as delete:
            await calendar.delete_event("event", self.session, 1, self.user)
        self.assertEqual(delete.exception.status_code, 409)
        self.session.delete.assert_not_awaited()

    async def test_creation_uses_the_authenticated_organiser_and_checks_links(self):
        with patch.object(calendar, "audit"):
            result = await calendar.add_event(calendar.EventInput(**body()), self.session, self.user)
        self.assertEqual(result["createdBy"], "owner")
        self.assertEqual(result["version"], 1)
        self.session.get.return_value = None
        with self.assertRaises(HTTPException) as invalid:
            await calendar.validate_references(self.session, calendar.EventInput(**body(contactId="missing")))
        self.assertEqual(invalid.exception.status_code, 422)

    async def test_inactive_attendees_are_rejected(self):
        self.session.scalars.return_value = ["active"]
        with self.assertRaises(HTTPException) as invalid:
            await calendar.validate_references(
                self.session, calendar.EventInput(**body(participantIds=["active", "inactive"]))
            )
        self.assertEqual(invalid.exception.status_code, 422)

    async def test_routes_require_authentication_and_range_is_bounded(self):
        app = FastAPI()
        app.include_router(calendar.router, prefix="/api/v1")

        async def session():
            yield self.session

        app.dependency_overrides[get_session] = session
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/api/v1/calendar/people")
            self.assertEqual(response.status_code, 401)
            app.dependency_overrides[current_user] = lambda: self.user
            response = await client.get(
                "/api/v1/calendar/events",
                params={"start": "2026-01-01T00:00:00Z", "end": "2027-01-01T00:00:00Z"},
            )
            self.assertEqual(response.status_code, 422)
            response = await client.post("/api/v1/calendar/events", json=body())
            self.assertEqual(response.status_code, 201)
