import base64
import html
import re
from html.parser import HTMLParser

import bleach
from bleach.css_sanitizer import CSSSanitizer
from fastapi import HTTPException

TAGS = [
    "div",
    "span",
    "p",
    "br",
    "b",
    "strong",
    "i",
    "em",
    "u",
    "s",
    "a",
    "img",
    "table",
    "thead",
    "tbody",
    "tfoot",
    "tr",
    "td",
    "th",
    "font",
    "h1",
    "h2",
    "h3",
    "h4",
    "ul",
    "ol",
    "li",
    "blockquote",
    "hr",
]
CSS = CSSSanitizer(
    allowed_css_properties=[
        "font-family",
        "font-size",
        "font-weight",
        "font-style",
        "color",
        "background-color",
        "text-align",
        "text-decoration",
        "line-height",
        "letter-spacing",
        "vertical-align",
        "margin",
        "margin-top",
        "margin-right",
        "margin-bottom",
        "margin-left",
        "padding",
        "padding-top",
        "padding-right",
        "padding-bottom",
        "padding-left",
        "border",
        "border-color",
        "border-style",
        "border-width",
        "border-collapse",
        "border-spacing",
        "width",
        "height",
        "max-width",
        "min-width",
    ]
)


class Fragment(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.parts = []
        self.skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "iframe", "object", "head"):
            self.skipping += 1
            return
        if self.skipping:
            return
        if tag == "body":
            tag = "div"
        elif tag == "html":
            return
        self.parts.append(
            "<"
            + tag
            + "".join(f' {key}="{html.escape(value or "", quote=True)}"' for key, value in attrs)
            + ">"
        )

    def handle_endtag(self, tag):
        if tag in ("script", "style", "iframe", "object", "head"):
            self.skipping = max(0, self.skipping - 1)
            return
        if not self.skipping and tag != "html":
            self.parts.append(f"</{'div' if tag == 'body' else tag}>")

    def handle_data(self, value):
        if not self.skipping:
            self.parts.append(html.escape(value))

    def handle_entityref(self, name):
        if not self.skipping:
            self.parts.append(f"&{name};")

    def handle_charref(self, name):
        if not self.skipping:
            self.parts.append(f"&#{name};")


def image_source(value):
    if value.startswith("https://"):
        return True
    match = re.fullmatch(r"data:image/(png|jpeg|gif|webp);base64,([A-Za-z0-9+/=\s]+)", value)
    if not match:
        return False
    try:
        data = base64.b64decode(match[2], validate=True)
        return len(data) <= 250000 and (
            data.startswith(b"\x89PNG\r\n\x1a\n")
            or data.startswith(b"\xff\xd8\xff")
            or data.startswith((b"GIF87a", b"GIF89a"))
            or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
        )
    except ValueError:
        return False


def attr_filter(tag, name, value):
    if name == "src":
        if not image_source(value):
            raise HTTPException(422, "Replace or omit local, relative, CID, or invalid signature images")
        return tag == "img"
    if name == "style":
        return not re.search(r"url\s*\(|expression|javascript|\\|@", value, re.I)
    if name == "href":
        return tag == "a" and value.lower().startswith(("https://", "http://", "mailto:", "tel:"))
    return name in {
        "alt",
        "title",
        "width",
        "height",
        "align",
        "valign",
        "border",
        "cellpadding",
        "cellspacing",
        "colspan",
        "rowspan",
        "color",
        "face",
        "size",
    }


def sanitize_html(value):
    parser = Fragment()
    parser.feed(value)
    return bleach.clean(
        "".join(parser.parts),
        tags=TAGS,
        attributes=attr_filter,
        css_sanitizer=CSS,
        protocols=["http", "https", "mailto", "tel", "data"],
        strip=True,
    )


def personalize(value, tokens):
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: html.escape(str(tokens.get(m[1], "")), quote=True), value)
