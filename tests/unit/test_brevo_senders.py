"""Selected Brevo campaign senders: fake HTTP transport, no emails or services."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

os.environ["DEFAULT_PLATFORM_ADMIN_EMAIL"] = ""
os.environ["DEFAULT_PLATFORM_ADMIN_PASSWORD"] = ""
os.environ.setdefault("SECRET_KEY", "test-only-secret-key-with-more-than-32-characters")

from app.services import email, invitations


class BrevoSenderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            email_provider="brevo",
            email_delivery_enabled=True,
            brevo_api_key="test-only-brevo-key",
            brevo_from_email="default@example.com",
            brevo_from_name="Default sender",
        )

    async def test_each_campaign_uses_its_selected_address_and_name(self):
        captured = []

        def handler(request):
            import json

            captured.append(json.loads(request.content))
            return httpx.Response(201, json={"messageId": "<test-message>"})

        original = httpx.AsyncClient
        with (
            patch.object(email, "get_settings", return_value=self.settings),
            patch.object(
                email.httpx,
                "AsyncClient",
                side_effect=lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
            ),
        ):
            for address, name in [
                ("first@example.com", "First sender"),
                ("second@example.com", "Second sender"),
            ]:
                sender = SimpleNamespace(email=address, sender_name=name, reply_to="reply@example.com")
                await email.brevo_send(sender, "recipient@example.com", "Subject", "<p>Body</p>", "id")
        self.assertEqual(
            [body["sender"] for body in captured],
            [
                {"email": "first@example.com", "name": "First sender"},
                {"email": "second@example.com", "name": "Second sender"},
            ],
        )
        self.assertTrue(all(body["replyTo"]["email"] == "reply@example.com" for body in captured))

    async def test_invitation_sender_keeps_configured_default(self):
        with patch.object(invitations, "get_settings", return_value=self.settings):
            sender = await invitations.invitation_sender(None)
        self.assertEqual(sender.email, "default@example.com")
        self.assertEqual(sender.sender_name, "Default sender")

    async def test_name_falls_back_only_when_selected_name_is_missing(self):
        captured = []

        def handler(request):
            import json

            captured.append(json.loads(request.content))
            return httpx.Response(201, json={"messageId": "<test-message>"})

        original = httpx.AsyncClient
        with (
            patch.object(email, "get_settings", return_value=self.settings),
            patch.object(
                email.httpx,
                "AsyncClient",
                side_effect=lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
            ),
        ):
            await email.brevo_send(
                SimpleNamespace(email="selected@example.com", sender_name="", reply_to="reply@example.com"),
                "recipient@example.com",
                "Subject",
                "<p>Body</p>",
                "id",
            )
        self.assertEqual(captured[0]["sender"], {"email": "selected@example.com", "name": "Default sender"})
