"""Branded transactional email markup, independent of database and transports."""

from html import escape
from urllib.parse import urlsplit


def invitation_email(first_name: str, activation_url: str, frontend_base_url: str, expiry_hours: int) -> str:
    """Render a single-use invitation for SMTP and Brevo without embedded images."""
    name = escape(first_name.strip() or "there")
    link = escape(activation_url, quote=True)
    base = frontend_base_url.rstrip("/")
    host = urlsplit(base)
    logo = ""
    # Email recipients cannot load images from a local development server.
    if host.scheme == "https" and host.hostname and host.hostname not in {"localhost", "127.0.0.1", "::1"}:
        source = escape(f"{base}/brand/ice-logo.png", quote=True)
        logo = (
            f'<img src="{source}" width="250" alt="Industrial Computing Engineering" '
            'border="0" style="display:block;width:250px;max-width:100%;height:auto;'
            'margin:0 0 24px;color:#ffffff;font-size:14px;">'
        )
    return f"""<!doctype html>
<html lang="en" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:w="urn:schemas-microsoft-com:office:word">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>Your CRM invitation</title>
<style>
@media only screen and (max-width:600px) {{
  .email-outer {{ padding:20px 12px !important; }}
  .email-content {{ padding:28px 24px !important; }}
  .email-title {{ font-size:28px !important;line-height:34px !important; }}
}}
</style>
</head>
<body style="margin:0;padding:0;background-color:#f3f4f6;color:#25252b;font-family:Arial,Helvetica,sans-serif;-webkit-text-size-adjust:100%;">
<div style="display:none;font-size:1px;line-height:1px;color:#f3f4f6;max-height:0;max-width:0;opacity:0;overflow:hidden;mso-hide:all;">
Your workspace is ready. Set your password to join your team in CRM.
</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#f3f4f6">
<tr><td class="email-outer" align="center" style="padding:40px 20px;">
<!--[if mso]><table role="presentation" width="600" cellpadding="0" cellspacing="0"><tr><td><![endif]-->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;border:1px solid #e4e6eb;border-radius:16px;background-color:#ffffff;">
<tr><td class="email-content" bgcolor="#171719" style="padding:32px 40px;border-radius:16px 16px 0 0;">
{logo}
<p style="margin:0 0 6px;font-size:25px;line-height:30px;letter-spacing:2px;font-weight:700;color:#ffffff;">CRM</p>
<p style="margin:0;font-size:10px;line-height:16px;letter-spacing:1.5px;color:#d7d7df;">CUSTOMER RELATIONSHIP MANAGEMENT</p>
</td></tr>
<tr><td class="email-content" style="padding:40px;">
<p style="margin:0 0 16px;font-size:11px;line-height:18px;letter-spacing:2px;font-weight:700;color:#a00f1a;">YOU'RE INVITED</p>
<h1 class="email-title" style="margin:0 0 20px;font-size:32px;line-height:39px;letter-spacing:-0.6px;color:#171719;font-weight:700;">Your next connection<br>starts here.</h1>
<p style="margin:0 0 12px;font-size:16px;line-height:26px;color:#25252b;">Hello {name},</p>
<p style="margin:0 0 28px;font-size:15px;line-height:25px;color:#60616c;">You've been invited to your team's CRM workspace. Bring your contacts, opportunities and campaigns together in one place.</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 28px;background-color:#f7f7f9;border:1px solid #e9e9ee;border-radius:10px;">
<tr><td style="padding:20px 22px;">
<p style="margin:0 0 8px;font-size:14px;line-height:22px;font-weight:700;color:#25252b;">A quick setup, then you're in.</p>
<p style="margin:0;font-size:14px;line-height:23px;color:#60616c;">Choose a password and confirm it. Once signed in, a short welcome tour will help you find your way around.</p>
</td></tr></table>
<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 20px;">
<tr><td align="center">
<!--[if mso]>
<v:roundrect href="{link}" style="height:52px;v-text-anchor:middle;width:248px;" arcsize="15%" stroke="f" fillcolor="#b5101d">
<w:anchorlock/>
<center style="color:#ffffff;font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:bold;">Set up your account</center>
</v:roundrect>
<![endif]-->
<!--[if !mso]><!-->
<a href="{link}" style="display:inline-block;min-width:200px;background-color:#b5101d;border:1px solid #b5101d;border-radius:8px;padding:16px 24px;font-size:15px;line-height:20px;font-weight:700;color:#ffffff;text-align:center;text-decoration:none;mso-hide:all;">Set up your account</a>
<!--<![endif]-->
</td></tr></table>
<p style="margin:0 0 28px;font-size:12px;line-height:20px;color:#737480;">Your personal invitation link can be used once and expires {expiry_hours} hours after it was issued.</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-top:1px solid #e9e9ee;">
<tr><td style="padding-top:24px;">
<p style="margin:0 0 10px;font-size:12px;line-height:20px;color:#737480;">Button not working? Copy and paste this link into your browser:</p>
<p style="margin:0;font-size:11px;line-height:19px;word-break:break-all;overflow-wrap:anywhere;"><a href="{link}" style="color:#60616c;text-decoration:underline;word-break:break-all;">{link}</a></p>
</td></tr></table>
</td></tr>
<tr><td class="email-content" bgcolor="#fafafa" style="padding:24px 40px;border-top:1px solid #e9e9ee;border-radius:0 0 16px 16px;">
<p style="margin:0;font-size:12px;line-height:20px;color:#737480;">Didn't expect this invitation? You can ignore this email. If you need a new invitation, contact your workspace administrator.</p>
</td></tr>
</table>
<!--[if mso]></td></tr></table><![endif]-->
<p style="margin:20px 0 0;font-size:11px;line-height:18px;color:#858690;">Industrial Computing Engineering &nbsp;&middot;&nbsp; CRM</p>
</td></tr></table>
</body></html>"""
