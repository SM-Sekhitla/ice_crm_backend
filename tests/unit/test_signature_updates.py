"""Saved signature link updates preserve formatting and submitted snapshots."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

os.environ["DEFAULT_PLATFORM_ADMIN_EMAIL"] = ""
os.environ["DEFAULT_PLATFORM_ADMIN_PASSWORD"] = ""
os.environ.setdefault("SECRET_KEY", "test-only-secret-key-with-more-than-32-characters")

from app.api import campaigns
from app.api.schemas import SignatureInput
from app.db.models import Signature


class SignatureUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_hosted_image_keeps_owner_styles_and_table_layout(self):
        signature = Signature(
            id="signature",
            owner_id="bdo",
            name="Personal",
            html="<p>Old</p>",
            source_file_name="signature.html",
        )
        session = SimpleNamespace(scalar=AsyncMock(return_value=signature), flush=AsyncMock(), add=Mock())
        body = SignatureInput(
            name="Personal",
            sourceFileName="signature.html",
            html='<div style="font-family:Arial;color:#d32f2f"><table><tr><td><img src="https://cdn.example.com/signature.png" width="600"></td></tr></table></div>',
        )
        with patch.object(campaigns, "signature_record", new_callable=AsyncMock, return_value=signature):
            result = await campaigns.update_signature("signature", body, session, SimpleNamespace(id="admin"))
        self.assertEqual(signature.owner_id, "bdo")
        self.assertIn("https://cdn.example.com/signature.png", signature.html)
        self.assertIn("font-family:Arial", signature.html)
        self.assertIn("color:#d32f2f", signature.html)
        self.assertIn("<table>", signature.html)
        self.assertNotIn("data:image", signature.html)
        self.assertEqual(result["id"], "signature")
        session.flush.assert_awaited_once()

    async def test_unavailable_signature_cannot_be_changed(self):
        session = SimpleNamespace(scalar=AsyncMock(), flush=AsyncMock(), add=Mock())
        with patch.object(
            campaigns,
            "signature_record",
            new_callable=AsyncMock,
            side_effect=HTTPException(404, "Signature not found"),
        ):
            with self.assertRaises(HTTPException):
                await campaigns.update_signature(
                    "private",
                    SignatureInput(name="Name", html="<p>Text</p>"),
                    session,
                    SimpleNamespace(id="other-user"),
                )
        session.flush.assert_not_awaited()
