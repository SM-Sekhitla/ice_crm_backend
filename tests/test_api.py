import asyncio

import pytest
from sqlalchemy import select

from app.core.security import decode_token
from app.db.models import Recipient
from app.db.session import SessionLocal
from app.services.worker import process_one
from tests.conftest import PASSWORD, contact, headers, login

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_auth_rotation_logout_and_disabled_token(client):
    tokens = await login(client)
    auth = {"Authorization": "Bearer " + tokens["accessToken"]}
    assert decode_token(tokens["accessToken"])["sub"] == "admin"
    assert (await client.get("/api/v1/auth/me", headers=auth)).json()["role"] == "Super Admin"
    refreshed = await client.post("/api/v1/auth/refresh", json={"refreshToken": tokens["refreshToken"]})
    assert refreshed.status_code == 200
    assert (
        await client.post("/api/v1/auth/refresh", json={"refreshToken": tokens["refreshToken"]})
    ).status_code == 401
    assert (await client.post("/api/v1/auth/logout", headers=auth)).status_code == 204
    assert (await client.get("/api/v1/auth/me", headers=auth)).status_code == 401
    assert (
        await client.post("/api/v1/auth/refresh", json={"refreshToken": refreshed.json()["refreshToken"]})
    ).status_code == 401


async def test_auth_failure_and_security_headers(client):
    result = await client.post(
        "/api/v1/auth/login", json={"email": "missing@example.com", "password": "incorrect"}
    )
    assert result.status_code == 401
    assert result.headers["x-content-type-options"] == "nosniff"
    assert "x-request-id" in result.headers
    assert (await client.get("/api/v1/contacts")).status_code == 401
    invalid = await client.post("/api/v1/auth/login", json={"email": "bad", "password": "secret-value"})
    assert invalid.status_code == 422
    assert "secret-value" not in invalid.text
    assert (await client.post("/api/v1/auth/login", content="x" * 2000001)).status_code == 413


async def test_password_change_revokes_sessions(client):
    auth = await headers(client)
    assert (
        await client.post(
            "/api/v1/auth/password",
            headers=auth,
            json={"currentPassword": PASSWORD, "password": "NewPassword2026!"},
        )
    ).status_code == 204
    assert (await client.get("/api/v1/auth/me", headers=auth)).status_code == 401
    assert (
        await client.post(
            "/api/v1/auth/login", json={"email": "admin@example.com", "password": "NewPassword2026!"}
        )
    ).status_code == 200


async def test_bdo_shared_crm_reads_and_retained_write_permissions(client):
    admin = await headers(client)
    bdo = await headers(client, "bdo@example.com")
    other = await contact(client, admin, "other-contact@example.com", "p2", "other")
    assert (await client.get("/api/v1/products", headers=bdo)).json()[0]["id"] == "p1"
    assert len((await client.get("/api/v1/products", headers=bdo)).json()) == 2
    assert (await client.get("/api/v1/contacts/" + other["id"], headers=bdo)).status_code == 200
    assert (await client.delete("/api/v1/contacts/" + other["id"], headers=bdo)).status_code == 403
    assert (await client.get("/api/v1/users", headers=bdo)).status_code == 403
    assert (await client.get("/api/v1/contacts", headers=bdo)).json()["total"] == 1
    assert (await client.get("/api/v1/contacts?productId=p2", headers=bdo)).json()["total"] == 1
    assert len((await client.get("/api/v1/associations", headers=bdo)).json()) == 1
    bad = await client.post(
        "/api/v1/contacts",
        headers=bdo,
        json={
            "firstName": "X",
            "lastName": "Y",
            "email": "test@example.com",
            "productIds": ["p2"],
            "ownerId": "bdo",
        },
    )
    assert bad.status_code == 404


async def test_contacts_duplicates_consent_and_archive_guards(client):
    auth = await headers(client)
    record = await contact(client, auth)
    duplicate = await client.post(
        "/api/v1/contacts",
        headers=auth,
        json={
            "firstName": "X",
            "lastName": "Y",
            "email": "CONTACT@example.com",
            "productIds": ["p1"],
            "ownerId": "bdo",
        },
    )
    assert duplicate.status_code == 409
    assert "sql" not in duplicate.text.lower()
    consent = await client.post(
        "/api/v1/contacts",
        headers=auth,
        json={
            "firstName": "X",
            "lastName": "Y",
            "email": "new@example.com",
            "productIds": ["p1"],
            "ownerId": "bdo",
            "consent": True,
        },
    )
    assert consent.status_code == 422
    assert (await client.delete("/api/v1/products/p1", headers=auth)).status_code == 409
    assert (await client.delete("/api/v1/contacts/" + record["id"], headers=auth)).status_code == 204


async def test_lifecycle_followup_conversion_and_history(client):
    auth = await headers(client, "bdo@example.com")
    await contact(client, auth)
    association = (await client.get("/api/v1/associations", headers=auth)).json()[0]
    url = "/api/v1/associations/" + association["id"]
    assert (
        await client.post(url + "/stage", headers=auth, json={"stage": "Qualified", "expectedValue": 1200})
    ).status_code == 200
    followup = await client.post(
        url + "/follow-ups", headers=auth, json={"title": "Call", "dueDate": "2026-12-01"}
    )
    assert followup.status_code == 201, followup.text
    assert (await client.get("/api/v1/leads", headers=auth)).json()[0]["nextFollowUp"] == "2026-12-01"
    edited = await client.put(
        "/api/v1/follow-ups/" + followup.json()["id"],
        headers=auth,
        json={"title": "Call", "dueDate": "2026-12-01", "status": "Completed"},
    )
    assert edited.status_code == 200
    assert (await client.get("/api/v1/leads", headers=auth)).json()[0]["nextFollowUp"] == ""
    assert (
        await client.post(url + "/stage", headers=auth, json={"stage": "Client", "dealValue": 1500})
    ).status_code == 200
    assert len((await client.get("/api/v1/clients", headers=auth)).json()) == 1
    assert (await client.post(url + "/stage", headers=auth, json={"stage": "Contact"})).status_code == 409
    assert (await client.delete(url, headers=auth)).status_code == 409


async def test_admin_user_management_product_assignment_and_session_revocation(client):
    admin = await headers(client)
    bdo = await headers(client, "bdo@example.com")
    edited = await client.put(
        "/api/v1/users/bdo",
        headers=admin,
        json={"firstName": "BDO", "lastName": "Example", "role": "BDO", "active": True, "productIds": ["p2"]},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["productIds"] == ["p2"]
    assert "passwordHash" not in edited.json()
    assert (await client.get("/api/v1/auth/me", headers=bdo)).status_code == 401
    assert (
        await client.put(
            "/api/v1/users/admin",
            headers=admin,
            json={"firstName": "Admin", "lastName": "Example", "role": "BDO", "active": True},
        )
    ).status_code == 409


async def make_campaign(client, auth, recipient_ids):
    sender = await client.post(
        "/api/v1/senders",
        headers=auth,
        json={
            "name": "Example sender",
            "senderName": "ICE Demo",
            "email": "sender@example.com",
            "replyTo": "reply@example.com",
            "status": "Verified",
            "productIds": ["p1"],
            "userIds": ["bdo"],
        },
    )
    assert sender.status_code == 201, sender.text
    campaign = await client.post(
        "/api/v1/campaigns",
        headers=auth,
        json={
            "name": "Example campaign",
            "productId": "p1",
            "senderId": sender.json()["id"],
            "subject": "Hello {{first_name}}",
            "bodyHtml": "<p>Hello {{first_name}}</p>",
            "recipientIds": recipient_ids,
        },
    )
    assert campaign.status_code == 201, campaign.text
    return campaign.json()


async def test_campaign_simulation_and_suppression_recheck(client):
    auth = await headers(client)
    first = await contact(client, auth)
    second = await contact(client, auth, "second@example.com")
    campaign = await make_campaign(client, auth, [first["id"], second["id"]])
    url = "/api/v1/campaigns/" + campaign["id"]
    assert (await client.post(url + "/launch", headers=auth, json={"confirm": True})).status_code == 200
    fields = {k: v for k, v in second.items() if k not in ("id", "createdAt", "updatedAt")}
    fields["doNotEmail"] = True
    assert (
        await client.put("/api/v1/contacts/" + second["id"], headers=auth, json=fields)
    ).status_code == 200
    while await process_one():
        pass
    counts = (await client.get(url + "/analytics", headers=auth)).json()["counts"]
    assert counts == {"Simulated": 1, "Excluded": 1}
    assert (await client.get(url, headers=auth)).json()["status"] == "Sent"
    assert (await client.post(url + "/launch", headers=auth, json={"confirm": True})).status_code == 409
    assert (await client.delete(url, headers=auth)).status_code == 409


async def test_campaign_scheduling_cancel_and_delivery_guard(client):
    auth = await headers(client)
    person = await contact(client, auth)
    campaign = await make_campaign(client, auth, [person["id"]])
    url = "/api/v1/campaigns/" + campaign["id"]
    assert (
        await client.post(url + "/launch", headers=auth, json={"confirm": True, "mode": "smtp"})
    ).status_code == 409
    assert (
        await client.post(
            url + "/launch", headers=auth, json={"confirm": True, "scheduledAt": "2099-01-01T12:00:00"}
        )
    ).status_code == 422
    assert (
        await client.post(
            url + "/launch", headers=auth, json={"confirm": True, "scheduledAt": "2099-01-01T12:00:00+02:00"}
        )
    ).status_code == 200
    assert not await process_one()
    assert (await client.post(url + "/cancel", headers=auth)).status_code == 200
    assert not await process_one()
    assert (await client.get(url + "/recipients", headers=auth)).json()[0]["reason"] == "Campaign cancelled"


async def test_campaign_cannot_reference_suppressed_or_unowned_contacts(client):
    admin = await headers(client)
    bdo = await headers(client, "bdo@example.com")
    person = await contact(client, admin, consent=False)
    response = await client.post(
        "/api/v1/campaigns",
        headers=bdo,
        json={"name": "Bad audience", "productId": "p1", "recipientIds": [person["id"]]},
    )
    assert response.status_code == 422
    other = await contact(client, admin, "otherperson@example.com", "p2", "other")
    response = await client.post(
        "/api/v1/campaigns",
        headers=bdo,
        json={"name": "Hidden contact", "productId": "p1", "recipientIds": [other["id"]]},
    )
    assert response.status_code == 422


async def test_signature_sanitization_and_ownership(client):
    bdo = await headers(client, "bdo@example.com")
    other = await headers(client, "other@example.com")
    result = await client.post(
        "/api/v1/signatures",
        headers=bdo,
        json={
            "name": "Styled",
            "html": '<html><body style="font-family:Arial;color:#d32f2f"><table><tr><td>Example</td></tr></table>'
            '<script>alert(1)</script><p onclick="evil()" style="position:fixed">Hello</p></body></html>',
        },
    )
    assert result.status_code == 201, result.text
    value = result.json()["html"]
    assert "font-family:Arial" in value and "color:#d32f2f" in value and "<table>" in value
    assert "onclick" not in value and "position" not in value and "alert(1)" not in value
    assert (await client.get("/api/v1/signatures", headers=other)).json() == []
    assert (
        await client.delete("/api/v1/signatures/" + result.json()["id"], headers=other)
    ).status_code == 404
    local = await client.post(
        "/api/v1/signatures",
        headers=bdo,
        json={"name": "Local image", "html": '<img src="file:///Users/example/banner.jpg">'},
    )
    assert local.status_code == 422
    assert (await client.delete("/api/v1/signatures/" + result.json()["id"], headers=bdo)).status_code == 204


async def test_concurrent_campaign_launch_is_idempotent(client):
    auth = await headers(client)
    person = await contact(client, auth)
    campaign = await make_campaign(client, auth, [person["id"]])
    results = await asyncio.gather(
        *(
            client.post(
                "/api/v1/campaigns/" + campaign["id"] + "/launch", headers=auth, json={"confirm": True}
            )
            for _ in range(2)
        )
    )
    assert sorted(r.status_code for r in results) == [200, 409]
    async with SessionLocal() as session:
        assert len(list(await session.scalars(select(Recipient)))) == 1


async def test_health_cors_and_openapi(client):
    assert (await client.get("/health/live")).status_code == 200
    assert (await client.get("/health/ready")).status_code == 200
    assert (await client.get("/openapi.json")).status_code == 200
    response = await client.options(
        "/api/v1/contacts",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"},
    )
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert (await client.get("/health/live", headers={"Host": "evil.example"})).status_code == 400


async def test_import_duplicates_and_csv_export_safety(client):
    auth = await headers(client, "bdo@example.com")
    row = {
        "firstName": "=example",
        "lastName": "Contact",
        "email": "import@example.com",
        "productIds": ["p1"],
        "ownerId": "bdo",
    }
    result = await client.post("/api/v1/contacts/import", headers=auth, json={"rows": [row, row]})
    assert result.status_code == 200, result.text
    assert len(result.json()["importedIds"]) == 1
    assert len(result.json()["skipped"]) == 1
    tagged = await client.post(
        "/api/v1/contacts/bulk-tags",
        headers=auth,
        json={"contactIds": result.json()["importedIds"], "tags": ["Follow up"]},
    )
    assert tagged.status_code == 200
    exported = await client.get("/api/v1/contacts/export", headers=auth)
    assert exported.status_code == 200
    assert "'=example" in exported.text


async def test_smtp_worker_acceptance_without_real_email(client, monkeypatch):
    from app.core.config import get_settings
    from app.services import worker

    settings = get_settings()
    monkeypatch.setattr(settings, "email_delivery_enabled", True)
    sent = []
    monkeypatch.setattr(worker, "smtp_send", lambda *args: sent.append(args))
    auth = await headers(client)
    person = await contact(client, auth)
    campaign = await make_campaign(client, auth, [person["id"]])
    url = "/api/v1/campaigns/" + campaign["id"]
    assert (
        await client.post(url + "/launch", headers=auth, json={"mode": "smtp", "confirm": True})
    ).status_code == 200
    while await process_one():
        pass
    assert len(sent) == 1
    assert sent[0][2] == "Hello Example"
    assert (await client.get(url + "/analytics", headers=auth)).json()["counts"] == {"Accepted": 1}


async def test_smtp_uncertain_outcome_is_never_retried(client, monkeypatch):
    from app.core.config import get_settings
    from app.services import worker

    monkeypatch.setattr(get_settings(), "email_delivery_enabled", True)
    attempts = []

    def fail(*args):
        attempts.append(1)
        raise TimeoutError("simulated timeout")

    monkeypatch.setattr(worker, "smtp_send", fail)
    auth = await headers(client)
    person = await contact(client, auth)
    campaign = await make_campaign(client, auth, [person["id"]])
    url = "/api/v1/campaigns/" + campaign["id"]
    assert (
        await client.post(url + "/launch", headers=auth, json={"mode": "smtp", "confirm": True})
    ).status_code == 200
    while await process_one():
        pass
    assert attempts == [1]
    assert (await client.get(url, headers=auth)).json()["status"] == "Failed"
    assert (await client.get(url + "/analytics", headers=auth)).json()["counts"] == {"Unknown": 1}


async def test_workspace_scope_and_empty_database_collections(client):
    admin = await headers(client)
    bdo = await headers(client, "bdo@example.com")
    empty = await client.get("/api/v1/workspace", headers=bdo)
    assert empty.status_code == 200, empty.text
    assert empty.json()["contacts"] == []
    assert empty.json()["campaigns"] == []
    assert empty.json()["senders"] == []
    assert empty.json()["signatures"] == []
    await contact(client, admin, "own-workspace@example.com")
    await contact(client, admin, "hidden-workspace@example.com", "p2", "other")
    scoped = (await client.get("/api/v1/workspace", headers=bdo)).json()
    assert len(scoped["products"]) == 2
    assert {person["id"] for person in scoped["users"]} == {"bdo", "admin", "other"}
    assert all(person["email"] == "" for person in scoped["users"] if person["id"] != "bdo")
    assert {person["email"] for person in scoped["contacts"]} == {
        "own-workspace@example.com",
        "hidden-workspace@example.com",
    }
    assert "passwordHash" not in str(scoped)
    assert "tokenVersion" not in str(scoped)
    complete = (await client.get("/api/v1/workspace", headers=admin)).json()
    assert len(complete["contacts"]) == 2
    assert len(complete["users"]) == 3


async def test_frontend_notes_bulk_delete_and_followup_status_contracts(client):
    auth = await headers(client)
    person = await contact(client, auth)
    assert (
        await client.put(
            "/api/v1/contacts/" + person["id"] + "/notes",
            headers=auth,
            json={"notes": "Updated from the real API"},
        )
    ).status_code == 204
    association = (await client.get("/api/v1/associations", headers=auth)).json()[0]
    url = "/api/v1/associations/" + association["id"]
    assert (
        await client.post(
            url + "/stage",
            headers=auth,
            json={"stage": "New Lead", "expectedValue": 4321, "followUp": "2026-12-01"},
        )
    ).status_code == 200
    assert (await client.post(url + "/stage", headers=auth, json={"stage": "Qualified"})).status_code == 200
    workspace = (await client.get("/api/v1/workspace", headers=auth)).json()
    assert workspace["leads"][0]["expectedValue"] == 4321
    task = workspace["followUps"][0]
    assert (
        await client.put(
            "/api/v1/follow-ups/" + task["id"] + "/status", headers=auth, json={"status": "Completed"}
        )
    ).status_code == 200
    workspace = (await client.get("/api/v1/workspace", headers=auth)).json()
    assert workspace["leads"][0]["nextFollowUp"] == ""
    assert workspace["contacts"][0]["notes"] == "Updated from the real API"
    assert (
        await client.post("/api/v1/contacts/bulk-delete", headers=auth, json={"contactIds": [person["id"]]})
    ).status_code == 204
    workspace = (await client.get("/api/v1/workspace", headers=auth)).json()
    assert (
        workspace["contacts"]
        == workspace["associations"]
        == workspace["leads"]
        == workspace["followUps"]
        == []
    )
