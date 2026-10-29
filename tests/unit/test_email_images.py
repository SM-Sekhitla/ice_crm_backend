"""Image publishing and SMTP MIME verification without live mail or PostgreSQL."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("SECRET_KEY", "test-only-secret-key-with-more-than-32-characters")

from fastapi import HTTPException
from app.api.email_images import public_image
from app.services.email_images import decode_image, image_base_url, inline_images, publish_images
from app.services import email, worker

PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jP6kAAAAASUVORK5CYII="

class EmailImageTests(unittest.IsolatedAsyncioTestCase):
    def settings(self, **overrides):
        return SimpleNamespace(**(dict(
            email_image_base_url="https://api.example.com", frontend_base_url="http://localhost:5173",
            secret_key="unit-test-image-key", email_provider="brevo", email_delivery_enabled=True,
            brevo_api_key="test-key", brevo_from_name="CRM", smtp_ssl=False, smtp_starttls=False,
            smtp_username="", smtp_host="smtp.example.com", smtp_port=587,
        ) | overrides))

    def test_inline_repeated_images_have_matching_cid(self):
        body, images = inline_images(f'<IMG SRC = \'{PNG}\'><img src="{PNG}">', "recipient")
        self.assertEqual(len(images), 1)
        cid, subtype, data = images[0]
        self.assertEqual(body.count(f"cid:{cid}"), 2)
        self.assertEqual(subtype, "png")
        self.assertTrue(data.startswith(b"\x89PNG"))
        self.assertNotIn("data:image", body)

    def test_invalid_and_oversized_bytes_are_rejected(self):
        import base64
        for value in ["data:image/png;base64,SGVsbG8=", "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 250001).decode()]:
            with self.assertRaises(ValueError):
                decode_image(value)

    def test_public_url_validation_and_frontend_fallback(self):
        self.assertEqual(image_base_url(self.settings(email_image_base_url="", frontend_base_url="https://crm.example.com")), "https://crm.example.com")
        for base in ["http://localhost:5173", "https://localhost", "https://user:pass@example.com", "https://example.com/?x=1"]:
            with self.assertRaises(ValueError):
                image_base_url(self.settings(email_image_base_url=base))

    async def test_publish_deduplicates_keeps_layout_and_leaves_remote_images(self):
        session = SimpleNamespace(execute=AsyncMock())
        body = f'<table><tr><td><img width="150" src="{PNG}"><img src="{PNG}"><img src="https://cdn.example.com/a.png"></td></tr></table>'
        rendered = await publish_images(session, body, self.settings())
        session.execute.assert_awaited_once()
        self.assertEqual(rendered.count("https://api.example.com/api/v1/email-images/"), 2)
        self.assertIn('width="150"', rendered)
        self.assertIn("https://cdn.example.com/a.png", rendered)
        self.assertNotIn("base64", rendered)
        self.assertIn("<table>", rendered)

    async def test_no_embedded_images_need_no_public_configuration_or_database(self):
        session = SimpleNamespace(execute=AsyncMock())
        html = '<img src="https://cdn.example.com/logo.png">'
        self.assertEqual(await publish_images(session, html, self.settings(email_image_base_url="")), html)
        session.execute.assert_not_awaited()

    async def test_public_endpoint_returns_bytes_and_missing_is_404(self):
        data = decode_image(PNG)[1]
        session = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(data=data, content_type="image/png")))
        response = await public_image("a" * 64, session)
        self.assertEqual(response.body, data)
        self.assertEqual(response.media_type, "image/png")
        self.assertIn("immutable", response.headers["cache-control"])
        session.get.return_value = None
        with self.assertRaises(HTTPException) as caught:
            await public_image("b" * 64, session)
        self.assertEqual(caught.exception.status_code, 404)

    def test_smtp_message_is_multipart_related_with_inline_images(self):
        smtp = MagicMock()
        with patch.object(worker, "get_settings", return_value=self.settings()), patch.object(worker.smtplib, "SMTP", return_value=smtp):
            worker.smtp_send(SimpleNamespace(sender_name="CRM", email="sender@example.com", reply_to="sender@example.com"), "recipient@example.com", "Subject", f'<img src="{PNG}">', "recipient")
        message = smtp.__enter__.return_value.send_message.call_args.args[0]
        html = next(p for p in message.walk() if p.get_content_type() == "text/html")
        image = next(p for p in message.walk() if p.get_content_type() == "image/png")
        self.assertIn(f'cid:{image["Content-ID"][1:-1]}', html.get_content())
        self.assertEqual(image.get_content_disposition(), "inline")
        self.assertEqual(image.get_payload(decode=True), decode_image(PNG)[1])

    async def test_brevo_commits_images_before_provider_request(self):
        import httpx
        session = MagicMock()
        session.__aenter__.return_value = session
        published = AsyncMock(return_value='<img src="https://api.example.com/api/v1/email-images/asset">')
        def handler(request):
            import json
            self.assertNotIn("base64", json.loads(request.content)["htmlContent"])
            session.begin.return_value.__aexit__.assert_awaited_once()
            return httpx.Response(201, json={"messageId": "test-message"})
        original = httpx.AsyncClient
        with patch.object(email, "get_settings", return_value=self.settings()), patch.object(email, "SessionLocal", return_value=session), patch.object(email, "publish_images", published), patch.object(email.httpx, "AsyncClient", side_effect=lambda **kw: original(transport=httpx.MockTransport(handler), **kw)):
            result = await email.brevo_send(SimpleNamespace(sender_name="CRM", email="sender@example.com", reply_to="sender@example.com"), "to@example.com", "Subject", f'<img src="{PNG}">', "recipient")
        self.assertEqual(result, "test-message")
        published.assert_awaited_once()
