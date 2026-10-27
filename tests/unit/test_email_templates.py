"""Offline email content checks; no provider or database access."""

import unittest
from html.parser import HTMLParser

from app.services.email_templates import invitation_email


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.images = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "a":
            self.links.append(values)
        elif tag == "img":
            self.images.append(values)


class EmailTemplateTests(unittest.TestCase):
    def test_button_and_fallback_preserve_single_use_activation_url(self):
        url = "https://crm.example.com/activate#token=example&test=1"
        body = invitation_email("Alex", url, "https://crm.example.com", 24)
        parsed = Links()
        parsed.feed(body)
        self.assertEqual([link["href"] for link in parsed.links], [url, url])
        self.assertIn("background-color:#b5101d", parsed.links[0]["style"])
        self.assertIn("Set up your account", body)
        self.assertIn("24 hours after it was issued", body)
        self.assertIn("<v:roundrect", body)
        self.assertIn("Hello Alex,", body)

    def test_untrusted_name_is_escaped(self):
        body = invitation_email(
            '<img src=x onerror="alert(1)">',
            "https://crm.example.com/activate",
            "https://crm.example.com",
            48,
        )
        self.assertNotIn("<img src=x", body)
        self.assertIn("&lt;img", body)
        self.assertIn("48 hours", body)

    def test_logo_uses_public_https_frontend_asset_without_embedded_data(self):
        body = invitation_email("Alex", "https://crm.example.com/activate", "https://crm.example.com/", 24)
        parsed = Links()
        parsed.feed(body)
        self.assertEqual(parsed.images[0]["src"], "https://crm.example.com/brand/ice-logo.png")
        self.assertNotIn("data:image", body)

    def test_development_email_does_not_reference_local_logo(self):
        body = invitation_email(
            "", "http://localhost:5173/activate#token=sample", "http://localhost:5173", 24
        )
        parsed = Links()
        parsed.feed(body)
        self.assertFalse(parsed.images)
        self.assertIn("Hello there,", body)
        self.assertIn(">CRM</p>", body)
