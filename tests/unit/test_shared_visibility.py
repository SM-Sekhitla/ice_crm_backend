"""Shared reads and retained write boundaries; no live database or services."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException

os.environ.setdefault("SECRET_KEY", "test-only-secret-key-with-more-than-32-characters")

from app.api import crm, imports, workspace
from app.api.deps import association_scope
from app.db.models import Association, Client, Contact, FollowUp, Lead, Product, User
from app.db.session import get_session


class SharedCrmTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.user = SimpleNamespace(id="bdo", role="BDO")
        self.session = SimpleNamespace(
            scalars=AsyncMock(return_value=[]), scalar=AsyncMock(), commit=AsyncMock(), execute=AsyncMock()
        )

    async def test_all_read_collections_have_no_owner_or_assignment_predicates(self):
        for endpoint in (crm.products, crm.leads, crm.clients, crm.followups):
            await endpoint(self.session, self.user)
            sql = str(self.session.scalars.call_args.args[0])
            self.assertNotIn("WHERE", sql)
        await crm.associations(self.session, self.user, contact_id=None)
        self.assertNotIn("WHERE", str(self.session.scalars.call_args.args[0]))

    async def test_contact_export_matches_shared_read_visibility(self):
        response = await imports.export_contacts(self.session, self.user)
        self.assertEqual(response.media_type, "text/csv")
        self.assertNotIn("WHERE", str(self.session.scalars.call_args.args[0]))

    async def test_unassigned_product_contact_filter_is_readable(self):
        self.session.scalar.return_value = 2
        result = await crm.contacts(
            self.session, self.user, q="", product_id="unassigned", stage=None, offset=0, limit=50
        )
        self.assertEqual(result["total"], 2)
        query = self.session.scalars.call_args.args[0]
        sql = str(query)
        self.assertIn("associations.product_id =", sql)
        self.assertNotIn("associations.owner_id =", sql)
        self.assertNotIn("assignments", sql)

    async def test_contact_detail_read_is_shared_and_write_remains_scoped(self):
        self.session.scalar.return_value = Contact(id="other-contact")
        await crm.contact_record(self.session, self.user, "other-contact", write=False)
        sql = str(self.session.scalar.call_args.args[0])
        self.assertNotIn("associations", sql)
        self.session.scalar.return_value = None
        with self.assertRaises(HTTPException) as denied:
            await crm.contact_record(self.session, self.user, "other-contact")
        self.assertEqual(denied.exception.status_code, 404)
        self.assertIn("associations.owner_id =", str(self.session.scalar.call_args.args[0]))
        self.assertIn("assignments", str(association_scope(self.user)))

    async def test_workspace_includes_shared_records_and_only_public_owner_labels(self):
        owner = User(
            id="other-owner",
            first_name="Other",
            last_name="Owner",
            email="private@example.com",
            role="BDO",
            active=True,
            password_hash="never-return-this",
        )
        records = {
            Contact: [Contact(id="other-contact"), Contact(id="unlinked-contact")],
            Association: [
                Association(
                    id="other-rel", contact_id="other-contact", product_id="other-product", owner_id=owner.id
                )
            ],
            Product: [Product(id="other-product")],
            Lead: [Lead(id="other-lead", association_id="other-rel")],
            Client: [Client(id="other-client", association_id="other-rel")],
            FollowUp: [FollowUp(id="other-task", association_id="other-rel")],
            User: [owner],
        }
        queries = []

        async def scalars(query):
            entity = query.column_descriptions[0].get("entity")
            queries.append((entity, str(query)))
            return records.get(entity, [])

        self.session.scalars.side_effect = scalars
        with patch.object(
            workspace,
            "user_response",
            new_callable=AsyncMock,
            return_value={"id": "bdo", "role": "BDO", "email": "self@example.com", "productIds": []},
        ):
            data = await workspace.workspace(self.session, self.user)
        self.assertEqual([item["id"] for item in data["contacts"]], ["other-contact", "unlinked-contact"])
        self.assertEqual(data["leads"][0]["id"], "other-lead")
        self.assertEqual(data["clients"][0]["id"], "other-client")
        for model, sql in queries:
            if model in {Contact, Association, Lead, Client, Product, FollowUp}:
                self.assertNotIn("WHERE", sql)
        directory = next(person for person in data["users"] if person["id"] == owner.id)
        self.assertEqual(directory["firstName"], "Other")
        self.assertEqual(directory["email"], "")
        self.assertNotIn("private@example.com", str(data))
        self.assertNotIn("never-return-this", str(data))

    async def test_shared_endpoints_still_require_authentication(self):
        app = FastAPI()
        app.include_router(crm.router, prefix="/api/v1")
        app.include_router(workspace.router, prefix="/api/v1")

        async def session():
            yield self.session

        app.dependency_overrides[get_session] = session
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            for path in (
                "products",
                "contacts",
                "contacts/other",
                "associations",
                "leads",
                "clients",
                "workspace",
            ):
                response = await client.get("/api/v1/" + path)
                self.assertEqual(response.status_code, 401)
