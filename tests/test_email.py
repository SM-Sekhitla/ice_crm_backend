from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.db.models import Sender
from app.services import email
from app.services.worker import process_one
from tests.conftest import contact, headers
from tests.test_api import make_campaign

pytestmark = pytest.mark.asyncio(loop_scope="session")


def configure_brevo(monkeypatch):
    settings = get_settings()
    for key, value in {
        "email_provider": "brevo",
        "email_delivery_enabled": True,
        "brevo_api_key": "test-only-brevo-key",
        "brevo_from_email": "sender@example.com",
        "brevo_from_name": "ICE",
    }.items():
        monkeypatch.setattr(settings, key, value)
    return settings


def stub_http(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        email.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )


async def test_provider_configuration_validation():
    config = {"_env_file": None, "secret_key": "test-only-secret-key-with-more-than-32-characters"}
    assert Settings(**config).email_provider == "smtp"
    assert Settings(**config, email_provider=" BREVO ").email_provider == "brevo"
    with pytest.raises(ValidationError):
        Settings(**config, email_provider="unsupported")
    with pytest.raises(ValidationError):
        Settings(**config, email_provider="brevo", email_delivery_enabled=True)
    with pytest.raises(ValidationError):
        Settings(**config, email_provider="smtp", email_delivery_enabled=True)
    ready = Settings(
        **config,
        email_provider="brevo",
        email_delivery_enabled=True,
        brevo_api_key="test-only-key",
        brevo_from_email="sender@example.com",
    )
    assert ready.smtp_host == ""  # Brevo does not require SMTP configuration.


async def test_brevo_request_matches_prism_contract(monkeypatch):
    configure_brevo(monkeypatch)
    captured = []

    def handler(request):
        import json

        assert str(request.url) == email.BREVO_API_URL
        assert request.headers["api-key"] == "test-only-brevo-key"
        captured.append(json.loads(request.content))
        return httpx.Response(201, json={"messageId": "<test-message@example.com>"})

    stub_http(monkeypatch, handler)
    sender = Sender(email="sender@example.com", sender_name="Campaign sender", reply_to="reply@example.com")
    assert (
        await email.brevo_send(sender, "recipient@example.com", "Subject", "<p>Body</p>", "recipient-id")
        == "<test-message@example.com>"
    )
    assert captured == [
        {
            "sender": {"email": "sender@example.com", "name": "Campaign sender"},
            "to": [{"email": "recipient@example.com"}],
            "replyTo": {"email": "reply@example.com"},
            "subject": "Subject",
            "htmlContent": "<p>Body</p>",
        }
    ]


@pytest.mark.parametrize("status", [400, 401, 403, 429])
async def test_brevo_known_rejections_do_not_expose_provider_body(monkeypatch, status):
    configure_brevo(monkeypatch)
    stub_http(
        monkeypatch, lambda request: httpx.Response(status, json={"message": "sensitive-provider-details"})
    )
    sender = Sender(email="sender@example.com", sender_name="ICE", reply_to="reply@example.com")
    with pytest.raises(email.DeliveryRejected) as error:
        await email.brevo_send(sender, "recipient@example.com", "Subject", "<p>Body</p>", "id")
    assert str(status) in str(error.value)
    assert "sensitive-provider-details" not in str(error.value)


@pytest.mark.parametrize("status,body", [(201, {}), (500, {}), (408, {})])
async def test_uncertain_brevo_response_is_not_recorded_as_acceptance(monkeypatch, status, body):
    configure_brevo(monkeypatch)
    stub_http(monkeypatch, lambda request: httpx.Response(status, json=body))
    sender = Sender(email="sender@example.com", sender_name="ICE", reply_to="reply@example.com")
    with pytest.raises(RuntimeError):
        await email.brevo_send(sender, "recipient@example.com", "Subject", "<p>Body</p>", "id")


async def test_embedded_images_are_rejected_before_request(monkeypatch):
    configure_brevo(monkeypatch)
    calls = []
    stub_http(monkeypatch, lambda request: calls.append(request))
    matching = Sender(email="sender@example.com", sender_name="ICE", reply_to="reply@example.com")
    with pytest.raises(email.DeliveryRejected, match="Prepare the embedded images"):
        await email.brevo_send(
            matching, "recipient@example.com", "Subject", '<img src="data:image/png;base64,ABC">', "id"
        )
    assert calls == []


async def test_delivery_switch_and_brevo_worker_acceptance(client, monkeypatch):
    from app.services import worker

    settings = configure_brevo(monkeypatch)
    sent = AsyncMock(return_value="<provider-message-id>")
    monkeypatch.setattr(worker, "brevo_send", sent)

    def forbidden_smtp(*args):
        raise AssertionError("SMTP must not be called for Brevo")

    monkeypatch.setattr(worker, "smtp_send", forbidden_smtp)
    auth = await headers(client)
    person = await contact(client, auth)
    campaign = await make_campaign(client, auth, [person["id"]])
    url = "/api/v1/campaigns/" + campaign["id"]
    result = await client.post(url + "/launch", headers=auth, json={"mode": "email", "confirm": True})
    assert result.status_code == 200, result.text
    while await process_one():
        pass
    sent.assert_awaited_once()
    assert (await client.get(url + "/analytics", headers=auth)).json()["counts"] == {"Accepted": 1}
    log = (await client.get(url + "/recipients", headers=auth)).json()[0]
    assert "Brevo accepted" in log["reason"] and "<provider-message-id>" in log["reason"]
    workspace = (await client.get("/api/v1/workspace", headers=auth)).json()
    assert workspace["emailProvider"] == "brevo"
    assert workspace["senders"][0]["provider"] == "BREVO"
    assert "test-only-brevo-key" not in str(workspace)
    assert workspace["brevoFromEmail"] == settings.brevo_from_email


@pytest.mark.parametrize(
    "outcome,expected",
    [
        (email.DeliveryRejected("Brevo rejected the request (HTTP 401)"), "Failed"),
        (httpx.ReadTimeout("provider timeout"), "Unknown"),
    ],
)
async def test_brevo_worker_never_retries_failed_or_uncertain_sends(client, monkeypatch, outcome, expected):
    from app.services import worker

    configure_brevo(monkeypatch)
    sent = AsyncMock(side_effect=outcome)
    monkeypatch.setattr(worker, "brevo_send", sent)
    auth = await headers(client)
    person = await contact(client, auth)
    campaign = await make_campaign(client, auth, [person["id"]])
    url = "/api/v1/campaigns/" + campaign["id"]
    assert (
        await client.post(url + "/launch", headers=auth, json={"mode": "email", "confirm": True})
    ).status_code == 200
    while await process_one():
        pass
    sent.assert_awaited_once()
    assert (await client.get(url + "/analytics", headers=auth)).json()["counts"] == {expected: 1}


async def test_brevo_launch_uses_authorised_sender_even_when_default_differs(client, monkeypatch):
    settings = configure_brevo(monkeypatch)
    monkeypatch.setattr(settings, "brevo_from_email", "different@example.com")
    auth = await headers(client)
    person = await contact(client, auth)
    campaign = await make_campaign(client, auth, [person["id"]])
    result = await client.post(
        "/api/v1/campaigns/" + campaign["id"] + "/launch",
        headers=auth,
        json={"mode": "email", "confirm": True},
    )
    assert result.status_code == 200
    assert result.json()["status"] == "Sending"
