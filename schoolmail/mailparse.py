"""Turn raw e-mail bytes into plain, safe data for the agent. Pure functions, no network.

  sender(raw)   -> (address, dmarc_failed) from the headers (a header-only fetch is enough)
  classify(address, dmarc_failed, allowed_from, school_domains) -> "forward" | "school" | None
  parse_message(raw, forwarder=False, ...) -> dict: from, original_from, date, subject,
      forwarded, original_date, original_subject, text, links, attachments, truncated, size

The text is plain: HTML loses tags, scripts, styles, images (tracking pixels) and hidden
elements; http(s) URLs move to `links` and the text keeps "[ссылка N]". Attachments are
described by name, type and size; with `read_attachment` (attachtext.read) their text is
added too, within per-message limits, links in it numbered like in the letter. Broken MIME
and unknown or wrong charsets give replacement characters or an empty text, never an exception.
"""
import datetime as dt
import time
import email.message
import email.parser
import email.utils
import html
import re
import unicodedata
from email.header import decode_header
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import attachtext

DUBAI = ZoneInfo("Asia/Dubai")
TEXT_LIMIT = 8000
MAX_LINKS = 50
MAX_ATTACHMENTS = 30
ATTACH_FILES = 5            # attachments read per message
ATTACH_TOTAL = 6000         # characters of attachment text per message
ATTACH_TIME = 30            # seconds of attachment reading per message


# ---------------------------------------------------------------- text cleanup
_SURROGATES = re.compile("[\ud800-\udfff]")
# zero-width, bidi controls, invisible fillers and Unicode tag characters (hidden-text tricks)
_INVISIBLE = re.compile("[­͏؜ᅟᅠ឴឵᠋-᠏"
                        "​-‏‪-‮⁠-⁯ㅤ﻿ﾠ"
                        "\U000e0000-\U000e007f]")
_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_SPACES = re.compile(r"[^\S\n]+")


def clean_text(text):
    """Normalize newlines and spaces; drop control, zero-width and bidi characters."""
    text = _SURROGATES.sub("�", text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = unicodedata.normalize("NFC", text)
    text = _CONTROL.sub("", _INVISIBLE.sub("", text))
    lines = (_SPACES.sub(" ", line).strip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def clean_line(text, limit=300):
    return clean_text(text).replace("\n", " ")[:limit]


def decode_bytes(data, charset=None):
    """Bytes -> str with the declared charset, falling back to UTF-8; never raises."""
    charset = (charset or "").strip().strip("\"'").lower()
    if charset in ("", "us-ascii", "ascii", "unknown-8bit", "x-unknown", "default"):
        charset = "utf-8"
    for name in (charset, "utf-8"):
        try:
            return data.decode(name, "replace")
        except Exception:      # unknown charset, or a codec that is not a text encoding
            continue
    return data.decode("latin-1")


def decode_words(value):
    """Raw header value -> text: unfold, decode RFC 2047 words, read stray 8-bit bytes as UTF-8."""
    if value is None:
        return ""
    value = str(value)
    if _SURROGATES.search(value):
        try:
            value = value.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
        except UnicodeError:
            value = _SURROGATES.sub("�", value)
    value = re.sub(r"\r?\n(?=[ \t])", "", value)
    try:
        parts = decode_header(value)
    except Exception:
        return value
    out = []
    for chunk, charset in parts:
        if isinstance(chunk, str):
            out.append(chunk)
        elif charset is None:
            try:
                out.append(chunk.decode("raw-unicode-escape"))
            except Exception:
                out.append(chunk.decode("latin-1"))
        else:
            out.append(decode_bytes(chunk, charset))
    return "".join(out)


# ---------------------------------------------------------------- headers, sender, filter
def _parse(raw, headersonly=False):
    try:
        return email.parser.BytesParser().parsebytes(raw or b"", headersonly=headersonly)
    except Exception:
        return email.message.Message()


def _header(msg, name):
    """First (topmost) raw value of a header, or None."""
    name = name.lower()
    try:
        for key, value in msg.raw_items():
            if key.lower() == name:
                return value
    except Exception:
        pass
    return None


_ADDR = re.compile(r"[^\s<>@,;:\"]+@[^\s<>@,;:\"]+")


def address(value):
    """Raw From value -> ("Name <addr>", "addr" in lower case, or "" if there is none)."""
    text = clean_line(decode_words(value), 300)
    text = re.sub(r"<mailto:([^>]+)>|\[mailto:([^\]]+)\]",
                  lambda m: f"<{m.group(1) or m.group(2)}>", text)
    try:
        name, addr = email.utils.parseaddr(text)
    except Exception:
        name, addr = "", ""
    if not _ADDR.fullmatch(addr or ""):
        # parseaddr gave up (e.g. an unquoted comma in the name): trust only an address in
        # angle brackets or a bare address, never one taken from the display name
        bracketed = re.findall(r"<([^<>\s]+)>", text)
        addr = bracketed[-1] if bracketed else text
        name = text[:text.rfind("<")] if bracketed else ""
        if not _ADDR.fullmatch(addr):
            addr = ""
    addr = addr.lower()
    name = clean_line(name, 150).strip("\"' ")
    if not addr:
        return text, ""
    return (f"{name} <{addr}>" if name and name.lower() != addr else addr), addr


def dmarc_failed(msg):
    """True when the receiving server's (topmost) Authentication-Results says dmarc=fail."""
    value = _header(msg, "Authentication-Results")
    return bool(value) and re.search(r"\bdmarc\s*=\s*fail\b", decode_words(value), re.I) is not None


def sender(raw):
    msg = _parse(raw, headersonly=True)
    return address(_header(msg, "From"))[1], dmarc_failed(msg)


def domain_matches(domain, domains):
    domain = domain.strip(".").lower()
    return any(domain == d or domain.endswith("." + d) for d in domains if d)


def classify(addr, dmarc_fail, allowed_from, school_domains):
    """"forward" for the owner's own forwards, "school" for school domains, None to skip."""
    if not addr or dmarc_fail:
        return None
    if addr in allowed_from:
        return "forward"
    if domain_matches(addr.rpartition("@")[2], school_domains):
        return "school"
    return None


# ---------------------------------------------------------------- dates
def to_dubai(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(DUBAI).isoformat(timespec="minutes")


def header_date(value):
    try:
        parsed = email.utils.parsedate_to_datetime(clean_line(decode_words(value), 200))
        return to_dubai(parsed) if parsed else None
    except Exception:
        return None


_MONTHS = {name: number for number, names in enumerate((
    "jan янв", "feb фев", "mar мар", "apr апр", "may май мая", "jun июн", "jul июл",
    "aug авг", "sep сен", "oct окт", "nov ноя", "dec дек"), start=1) for name in names.split()}
_MON = r"([^\W\d_]{3,})\.?"
_FWD_DATES = (
    (re.compile(_MON + r"\s+(\d{1,2}),?\s+(\d{4})"), "mdy"),            # Oct 26, 2026
    (re.compile(r"(\d{1,2})\.?\s+" + _MON + r",?\s+(\d{4})"), "dmy"),   # 26 Oct 2026, 26 окт. 2026
    (re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})"), "ymd"),                # 2026-10-26
    (re.compile(r"(\d{1,2})[./](\d{1,2})[./](\d{4})"), "num"),          # 26.10.2026, 10/26/2026
)
_CLOCK = re.compile(r"(\d{1,2}):(\d{2})(?::\d{2})?(?:\s*([AaPp])\.?\s*[Mm]\.?(?![^\W\d_]))?")


def _ymd(match, kind):
    g = match.groups()
    if kind == "mdy":
        month, day, year = _MONTHS.get(g[0][:3].lower()), int(g[1]), int(g[2])
    elif kind == "dmy":
        day, month, year = int(g[0]), _MONTHS.get(g[1][:3].lower()), int(g[2])
    elif kind == "ymd":
        year, month, day = map(int, g)
    else:
        a, b, year = map(int, g)
        day, month = (b, a) if "/" in match.group(0) and b > 12 else (a, b)
    return (year, month, day) if month else None


def forward_date(text):
    """Date line of a forwarded message -> Dubai ISO time. A time without a zone is taken as
    Dubai time (the owner's mail client writes it in the owner's zone); text that cannot be
    parsed is returned as is."""
    text = clean_line(text, 100)
    if not text:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(text)
        if parsed.tzinfo:
            return to_dubai(parsed)
    except Exception:
        pass
    for rx, kind in _FWD_DATES:
        for match in rx.finditer(text):
            ymd = _ymd(match, kind)
            if not ymd:
                continue
            clock = _CLOCK.search(text, match.end())
            try:
                if not clock:
                    return dt.date(*ymd).isoformat()
                hour, minute = int(clock.group(1)), int(clock.group(2))
                if clock.group(3):
                    hour = hour % 12 + (12 if clock.group(3).lower() == "p" else 0)
                return dt.datetime(*ymd, hour, minute, tzinfo=DUBAI).isoformat(timespec="minutes")
            except ValueError:
                return text
    return text


# ---------------------------------------------------------------- links
def clean_url(url):
    """http(s) URL without whitespace; Outlook safelinks and Google redirects unwrapped."""
    url = _INVISIBLE.sub("", re.sub(r"[\s\x00-\x1f\x7f]+", "", _SURROGATES.sub("", url or "")))
    if not re.match(r"https?://", url, re.I):
        return None
    try:
        parts = urlsplit(url)
        host, query = (parts.hostname or "").lower(), parse_qs(parts.query)
        target = None
        if host.endswith("safelinks.protection.outlook.com"):
            target = (query.get("url") or [None])[0]
        elif host in ("google.com", "www.google.com") and parts.path == "/url":
            target = (query.get("q") or query.get("url") or [None])[0]
        if target and re.match(r"https?://", target, re.I):
            url = re.sub(r"[\s\x00-\x1f\x7f]+", "", target)
    except ValueError:
        pass
    return url[:2000]


class Links:
    """Numbered, de-duplicated http(s) links; the text keeps only "[ссылка N]"."""

    def __init__(self):
        self.urls, self._index = [], {}

    def ref(self, url):
        url = clean_url(url)
        if not url:
            return None
        if url not in self._index:
            if len(self.urls) >= MAX_LINKS:
                return "[ссылка]"
            self.urls.append(url)
            self._index[url] = len(self.urls)
        return f"[ссылка {self._index[url]}]"


_TEXT_URL = re.compile(r"<(https?://[^\s<>]+)>|\b(https?://[^\s<>\"']+)", re.I)


def replace_urls(text, links):
    def repl(m):
        url, tail = m.group(1), ""
        if url is None:
            url = m.group(2)
            while url and url[-1] in ".,;:!?)]}*" and not (
                    url[-1] == ")" and url.count("(") >= url.count(")")):
                url, tail = url[:-1], url[-1] + tail
        return (links.ref(url) or url) + tail
    return _TEXT_URL.sub(repl, text)


# ---------------------------------------------------------------- HTML -> text
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "keygen", "link", "meta",
         "param", "source", "track", "wbr"}
_SKIP = {"script", "style", "head", "title", "noscript", "template", "svg", "math", "object",
         "iframe", "frame", "frameset", "applet", "canvas", "audio", "video", "select"}
_BLOCK = {"p", "div", "section", "article", "header", "footer", "main", "aside", "nav", "h1",
          "h2", "h3", "h4", "h5", "h6", "ul", "ol", "dl", "table", "thead", "tbody", "tfoot",
          "blockquote", "pre", "address", "center", "form", "fieldset", "figure", "figcaption"}
_ROW = {"tr", "li", "dt", "dd"}         # a new line before, none after: no blank lines between rows
# Only unambiguous hiding: font-size:0 and max-height:0 are layout tricks of normal templates.
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
_URLISH = re.compile(r"\s*(?:https?://|www\.)\S*\s*$", re.I)
_WS = re.compile(r"\s+")


class _HTMLText(HTMLParser):
    """HTML -> plain text with "[ссылка N]" after link texts; drops scripts, styles, images
    and hidden elements."""

    def __init__(self, links):
        super().__init__(convert_charrefs=True)
        self.links = links
        self.out = []
        self.stack = []     # open tags: (tag, hides, active, anchor)
        self.hidden = 0     # open tags that hide their content
        self.pre = 0

    def handle_starttag(self, tag, attrs):
        if tag == "body":
            self._pop_to("head")            # recover from an unclosed <head>
        if tag in _VOID:
            if not self.hidden and tag in ("br", "hr"):
                self.out.append("\n")
            return
        attrs = dict(attrs)
        hides = tag in _SKIP or "hidden" in attrs or bool(_HIDDEN_STYLE.search(attrs.get("style") or ""))
        active = not self.hidden and not hides
        anchor = None
        self.hidden += hides
        if active:
            if tag == "pre":
                self.pre += 1
            if tag in _BLOCK or tag in _ROW:
                self.out.append("\n")
            if tag == "li":
                self.out.append("- ")
            elif tag in ("td", "th"):
                self.out.append(" | ")
            elif tag == "a":
                anchor = (attrs.get("href") or "", len(self.out))
        self.stack.append((tag, hides, active, anchor))

    def handle_endtag(self, tag):
        if tag in _VOID:
            if tag == "br" and not self.hidden:
                self.out.append("\n")
            return
        self._pop_to(tag)

    def handle_data(self, data):
        if not self.hidden:
            self.out.append(data if self.pre else _WS.sub(" ", data))

    def _pop_to(self, tag):
        if not any(entry[0] == tag for entry in self.stack):
            return                          # stray end tag
        while self.stack:
            entry = self.stack.pop()
            self._close(*entry)
            if entry[0] == tag:
                break

    def _close(self, tag, hides, active, anchor):
        self.hidden -= hides
        if not active:
            return
        if tag == "pre":
            self.pre -= 1
        if anchor:
            self._link(*anchor)
        if tag in _BLOCK:
            self.out.append("\n")

    def _link(self, href, start):
        ref = self.links.ref(href)
        if not ref:
            return                          # mailto:, tel:, cid:, javascript: keep the visible text
        text = "".join(self.out[start:]).strip()
        if not text or _URLISH.match(text):
            del self.out[start:]            # the visible URL may differ from the real one
        self.out.append(f" {ref} ")

    def text(self):
        while self.stack:
            self._close(*self.stack.pop())
        lines = []
        for line in "".join(self.out).split("\n"):
            if "|" in line:
                line = re.sub(r"(?:\s*\|\s*)+", " | ", line).strip(" |")
            lines.append(line)
        return "\n".join(lines)


_DROP_BLOCK = re.compile(r"<(script|style|head|title|noscript|template|svg)\b", re.I)


def _strip_tags(source):
    """Linear-time fallback when HTMLParser fails on malformed markup."""
    out, pos = [], 0
    while True:
        m = _DROP_BLOCK.search(source, pos)
        if not m:
            out.append(source[pos:])
            break
        out.append(source[pos:m.start()])
        end = re.compile(r"</%s\s*>" % m.group(1), re.I).search(source, m.end())
        if not end:
            break
        pos = end.end()
    text = re.sub(r"<(?:br|/p|/div|/tr|/li|/h\d)\b[^<>]*>", "\n", "".join(out), flags=re.I)
    return html.unescape(re.sub(r"<[^<>]*>", " ", text))


def html_to_text(source, links):
    parser = _HTMLText(links)
    try:
        parser.feed(source)
        parser.close()
        text = parser.text()
    except Exception:       # HTMLParser raises AssertionError on some malformed declarations
        text = _strip_tags(source)
    text = replace_urls(clean_text(text), links)
    return re.sub(r"(\[ссылка(?: \d+)?\]) (?=[.,;:!?)])", r"\1", text)


# ---------------------------------------------------------------- MIME tree
def _disposition(part):
    try:
        return part.get_content_disposition()
    except Exception:
        return None


def _filename(part):
    try:
        name = part.get_filename()
    except Exception:
        name = None
    if not name:
        return None
    name = clean_line(decode_words(name), 150).replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name or None


def _is_attachment(part):
    if _disposition(part) == "attachment":
        return True
    return bool(_filename(part)) and part.get_content_type() not in ("text/plain", "text/html")


def _is_inline_image(part):
    """Logos and pictures referenced from the HTML by Content-ID: not real attachments."""
    return (part.get_content_maintype() == "image" and part.get("Content-ID") is not None
            and _disposition(part) != "attachment")


def _payload_bytes(part):
    try:
        data = part.get_payload(decode=True)
    except Exception:
        data = None
    return data if isinstance(data, bytes) else b""


def _part_text(part):
    data = _payload_bytes(part)
    try:
        charset = part.get_content_charset()
    except Exception:
        charset = None
    if not charset and part.get_content_type() == "text/html":
        m = re.search(rb"<meta[^>]+charset=[\"']?([\w.:-]+)", data[:4096], re.I)
        charset = m.group(1).decode("ascii", "ignore") if m else None
    return decode_bytes(data, charset)


def _attachment(part):
    return {"name": _filename(part), "type": clean_line(part.get_content_type(), 100),
            "size": len(_payload_bytes(part))}


def _inner(part):
    payload = part.get_payload()
    if isinstance(payload, list) and payload and isinstance(payload[0], email.message.Message):
        return payload[0]
    return None


def _message_attachment(part):
    inner = _inner(part)
    name = _filename(part)
    if not name:
        subject = clean_line(decode_words(_header(inner, "Subject")), 100) if inner else ""
        name = (subject or "message") + ".eml"
    try:
        size = len(inner.as_bytes()) if inner is not None else 0
    except Exception:
        size = 0
    return {"name": name, "type": "message/rfc822", "size": size}


def _contains(part, ctype, depth=0):
    if part.get_content_type() == ctype:
        return True
    payload = part.get_payload()
    return (depth < 10 and part.get_content_maintype() == "multipart" and isinstance(payload, list)
            and any(_contains(child, ctype, depth + 1) for child in payload))


class _AttachmentText:
    """Adds extracted, truncated, reason and the content (text and pages, or calendar events)
    to attachment metadata, within per-message limits on files, characters and time. The
    content gets the same treatment as the letter: cleaned, links to `links` as [ссылка N]."""

    def __init__(self, read, links, incomplete, deadline=None):
        self.read, self.links, self.incomplete = read, links, incomplete
        self.files = self.chars = 0
        self.deadline = min(time.monotonic() + ATTACH_TIME, deadline or float("inf"))

    def describe(self, part, info):
        info.update(extracted=False, truncated=False, reason=None)
        k = attachtext.kind(info["type"], info["name"])
        if k in (None, "image"):
            info["reason"] = "scan" if k else "unsupported"
            return
        left = self.deadline - time.monotonic()
        if self.files >= ATTACH_FILES or self.chars >= ATTACH_TOTAL or left < 1:
            info["reason"] = "limit"
            return
        self.files += 1
        if k == "html":
            result = {"text": html_to_text(_part_text(part), self.links), "truncated": False}
        else:
            result = self.read(_payload_bytes(part), k, min(attachtext.TIMEOUT, left))
            if isinstance(result.get("text"), str):
                result["text"] = replace_urls(clean_text(result["text"]), self.links)
        if isinstance(result.get("events"), list):
            self._events(info, result)
        elif isinstance(result.get("text"), str):
            self._text(info, result)
        else:
            reason = result.get("error")
            if self.incomplete and reason in ("broken", "scan"):
                reason = "too_large"            # the letter was cut at the size limit
            info["reason"] = reason if reason in attachtext.REASONS else "broken"

    def _text(self, info, result):
        text, room = result["text"], min(attachtext.TEXT_CAP, ATTACH_TOTAL - self.chars)
        cut = len(text) > room
        if cut:
            text = re.sub(r"\[[^\]]{0,12}$", "", text[:room]).rstrip() + " …"
        self.chars += len(text)
        info.update(extracted=True, truncated=bool(cut or result.get("truncated")))
        if result.get("pages"):
            info["pages"] = result["pages"]
        info["text"] = text

    def _events(self, info, result):
        def field(value, multiline=False):
            if not isinstance(value, str):
                return None
            value = clean_text(value) if multiline else clean_line(value, 300)
            return replace_urls(value, self.links) or None

        events, cut = [], bool(result.get("truncated"))
        for e in result["events"]:
            if not isinstance(e, dict):
                continue
            event = {"summary": field(e.get("summary")), "start": field(e.get("start")),
                     "end": field(e.get("end")), "location": field(e.get("location")),
                     "description": field(e.get("description"), multiline=True)}
            size = sum(len(v) for v in event.values() if v)
            if self.chars + size > ATTACH_TOTAL:
                cut = True
                break
            self.chars += size
            events.append(event)
        info.update(extracted=True, truncated=cut, events=events)


class _Body:
    """Walks the MIME tree: readable text, attachment metadata, attached messages."""

    def __init__(self, links, reader=None):
        self.links = links
        self.reader = reader    # _AttachmentText or None (metadata only)
        self.attachments = []
        self.messages = []      # message/rfc822 parts

    def text(self, msg):
        out = []
        self._walk(msg, out, 0)
        return "\n\n".join(t for t in out if t)

    def _walk(self, part, out, depth):
        if depth > 30:
            return
        ctype = part.get_content_type()
        payload = part.get_payload()
        if isinstance(payload, list):
            if ctype == "message/rfc822":
                self.messages.append(part)
            elif ctype == "multipart/alternative":
                self._alternative(payload, out, depth)
            else:
                for child in payload:
                    self._walk(child, out, depth + 1)
            return
        if ctype in ("text/plain", "text/html") and not _is_attachment(part):
            text = _part_text(part)
            out.append(html_to_text(text, self.links) if ctype == "text/html"
                       else replace_urls(clean_text(text), self.links))
        elif not _is_inline_image(part):
            info = _attachment(part)
            if self.reader:
                self.reader.describe(part, info)
            self.attachments.append(info)

    def _alternative(self, children, out, depth):
        """HTML first (scripts and hidden text get removed), plain text if HTML gives nothing."""
        body = [c for c in children if not _is_attachment(c)]
        rich = [c for c in reversed(body) if _contains(c, "text/html")]
        for child in rich + [c for c in body if not any(c is r for r in rich)]:
            trial = []
            self._walk(child, trial, depth + 1)
            if any(trial):
                out.extend(trial)
                break
        for child in children:
            if _is_attachment(child):
                self._walk(child, out, depth + 1)


# ---------------------------------------------------------------- forwarded messages
_FWD_SUBJECT = re.compile(r"^\s*(?:fwd?|пересл\.?)\s*:", re.I)
_FWD_MARK = re.compile(
    r"^[-\s]*(?:forwarded message|пересылаемое сообщение|пересланное сообщение"
    r"|переадресованное сообщение)[-\s]*$"
    r"|^\s*(?:begin forwarded message|начало переадресованного сообщения)\s*:?\s*$", re.I)
_SEPARATOR = re.compile(r"^\s*(?:_{8,}|-{3,}\s*(?:original message|исходное сообщение)\s*-{3,})\s*$", re.I)
_FIELD_NAMES = {"from": "from", "от": "from", "отправитель": "from",
                "date": "date", "sent": "date", "дата": "date", "отправлено": "date",
                "subject": "subject", "тема": "subject",
                "to": "to", "кому": "to", "cc": "to", "копия": "to"}
_FIELD = re.compile(r"^[>*\s]{0,4}(%s)\*{0,2}\s*:\s*(.*)$" % "|".join(_FIELD_NAMES), re.I)


def _field(line):
    m = _FIELD.match(line)
    return (_FIELD_NAMES[m.group(1).lower()], m.group(2).strip(" *")) if m else (None, None)


def find_forward(text, subject):
    """Header block of an inline forward (Gmail, Outlook, Apple Mail; English or Russian):
    {"from", "date", "subject"} or None. Reply separators ("____", "Original Message", a bare
    From:/Subject: block) count only when the subject itself says Fwd/FW."""
    lines = text.split("\n")
    fwd_subject = bool(_FWD_SUBJECT.match(subject or ""))
    start = next((i + 1 for i, line in enumerate(lines)
                  if _FWD_MARK.match(line) or (fwd_subject and _SEPARATOR.match(line))), None)
    if start is None and fwd_subject:
        start = next((i for i, line in enumerate(lines) if _field(line)[0] == "from"
                      and any(_field(nxt)[0] == "subject" for nxt in lines[i + 1:i + 8])), None)
    if start is None:
        return None
    fields = {}
    for line in lines[start:start + 15]:
        if not line.strip():
            if fields:
                break
            continue
        name, value = _field(line)
        if name:
            fields.setdefault(name, value)
    return fields if fields.get("from") else None


# ---------------------------------------------------------------- the message
def _limit(text):
    if len(text) <= TEXT_LIMIT:
        return text, False
    cut = text[:TEXT_LIMIT]
    bracket = cut.rfind("[")
    if bracket > TEXT_LIMIT - 12 and "]" not in cut[bracket:]:
        cut = cut[:bracket]                 # do not leave half of "[ссылка N]"
    return cut.rstrip() + " …", True


def parse_message(raw, forwarder=False, size=None, truncated=False, internaldate=None,
                  read_attachment=None, deadline=None):
    """Raw RFC 822 bytes -> plain dict for the agent. `forwarder`: the sender is the owner,
    so an attached message (forward as attachment) is unpacked as the original letter.
    `read_attachment(data, kind, timeout)` (attachtext.read) adds attachment text; `deadline`
    (time.monotonic) caps the reading time shared by several messages."""
    msg = _parse(raw)
    sender_display = address(_header(msg, "From"))[0]
    subject = clean_line(decode_words(_header(msg, "Subject")), 300)
    date = header_date(_header(msg, "Date")) or (to_dubai(internaldate) if internaldate else None)
    links = Links()
    reader = _AttachmentText(read_attachment, links, truncated, deadline) if read_attachment else None
    body = _Body(links, reader)
    text = body.text(msg)
    original = (sender_display, date, subject)
    forwarded = False
    if forwarder and body.messages:
        inner = _inner(body.messages[0])
        if inner is not None:
            body.messages.pop(0)
            inner_body = _Body(links, reader)
            text = "\n\n".join(t for t in (text, inner_body.text(inner)) if t)
            body.attachments += inner_body.attachments
            body.messages += inner_body.messages
            original = (address(_header(inner, "From"))[0], header_date(_header(inner, "Date")),
                        clean_line(decode_words(_header(inner, "Subject")), 300))
            forwarded = True
    if not forwarded:
        fields = find_forward(text, subject)
        if fields:
            original = (address(fields["from"])[0], forward_date(fields.get("date", "")),
                        clean_line(fields.get("subject", ""), 300) or None)
            forwarded = True
    attachments = body.attachments + [_message_attachment(m) for m in body.messages]
    text, cut = _limit(text)
    return {
        "from": sender_display,
        "original_from": original[0],
        "date": date,
        "subject": subject,
        "forwarded": forwarded,
        "original_date": original[1],
        "original_subject": original[2],
        "text": text,
        "links": links.urls,
        "attachments": attachments[:MAX_ATTACHMENTS],
        "truncated": truncated or cut,
        "size": len(raw) if size is None else size,
    }
