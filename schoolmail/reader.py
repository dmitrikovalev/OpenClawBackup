"""Read-only IMAP access and the /new and /stats logic.

The only IMAP commands sent are LOGIN, EXAMINE (read-only SELECT), STATUS, UID SEARCH,
UID FETCH with BODY.PEEK (does not set \\Seen) and LOGOUT: no STORE, COPY, MOVE, EXPUNGE,
APPEND or CLOSE. Message ids for the agent are "<UIDVALIDITY>-<UID>".
"""
import collections
import contextlib
import dataclasses
import datetime as dt
import imaplib
import logging
import os
import re
import ssl
import time

import attachtext
import mailparse

log = logging.getLogger("schoolmail")

HEADER_ITEMS = "(UID RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM AUTHENTICATION-RESULTS)])"
CHUNK = 200
ATTACH_TIME_PER_CALL = 120      # seconds of attachment reading per /new call


@dataclasses.dataclass(frozen=True)
class Config:
    host: str = ""
    port: int = 993
    user: str = ""
    password: str = dataclasses.field(default="", repr=False)
    mailbox: str = "INBOX"
    allowed_from: frozenset = frozenset()
    school_domains: frozenset = frozenset()
    max_bytes: int = 10 * 1024 * 1024
    error: str = ""             # what is wrong with the settings, if anything

    @classmethod
    def from_env(cls, env=os.environ):
        def items(name):
            return frozenset(x.strip().lower().lstrip("@").strip(".")
                             for x in env.get(name, "").split(",") if x.strip())
        problems = [name for name in ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD") if not env.get(name)]
        if not env.get("MAIL_ALLOWED_FROM") and not env.get("MAIL_SCHOOL_DOMAINS"):
            problems.append("MAIL_ALLOWED_FROM or MAIL_SCHOOL_DOMAINS")
        try:
            port = int(env.get("IMAP_PORT") or 993)
            max_bytes = int(env.get("MAIL_MAX_BYTES") or 10 * 1024 * 1024)
        except ValueError:
            port, max_bytes = 993, 10 * 1024 * 1024
            problems.append("IMAP_PORT/MAIL_MAX_BYTES must be numbers")
        return cls(host=env.get("IMAP_HOST", "").strip(), port=port,
                   user=env.get("IMAP_USER", "").strip(), password=env.get("IMAP_PASSWORD", ""),
                   mailbox=env.get("MAILBOX", "").strip() or "INBOX",
                   allowed_from=items("MAIL_ALLOWED_FROM"), school_domains=items("MAIL_SCHOOL_DOMAINS"),
                   max_bytes=max_bytes, error=("missing or invalid: " + ", ".join(problems)) if problems else "")


def quote_mailbox(name):
    if re.fullmatch(r"[A-Za-z0-9_./-]+", name):
        return name
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


@contextlib.contextmanager
def session(cfg, timeout=30):
    """Logged-in IMAP connection with the mailbox opened by EXAMINE (read-only)."""
    conn = imaplib.IMAP4_SSL(cfg.host, cfg.port, ssl_context=ssl.create_default_context(),
                             timeout=timeout)
    try:
        conn.login(cfg.user, cfg.password)
        typ, _ = conn.select(quote_mailbox(cfg.mailbox), readonly=True)
        if typ != "OK":
            raise imaplib.IMAP4.error(f"EXAMINE {cfg.mailbox} failed")
        yield conn
    finally:
        with contextlib.suppress(Exception):
            conn.logout()


def _ok(typ, what):
    if typ != "OK":
        raise imaplib.IMAP4.error(f"{what} failed: {typ}")


def uidvalidity(conn, mailbox):
    _, data = conn.response("UIDVALIDITY")         # sent by the server with EXAMINE
    value = data[-1] if data else None
    if not value:
        typ, data = conn.status(quote_mailbox(mailbox), "(UIDVALIDITY)")
        _ok(typ, "STATUS")
        m = re.search(rb"UIDVALIDITY\s+(\d+)", b" ".join(d for d in data if isinstance(d, bytes)))
        value = m.group(1) if m else None
    if not value:
        raise imaplib.IMAP4.error("no UIDVALIDITY")
    return int(value)


def all_uids(conn):
    typ, data = conn.uid("SEARCH", None, "ALL")
    _ok(typ, "UID SEARCH")
    return sorted({int(x) for chunk in data if chunk for x in chunk.split() if x.isdigit()})


# ---------------------------------------------------------------- FETCH responses
_NEW_FETCH = re.compile(rb"^\d+ \(")
_META = re.compile(rb'\b(UID|RFC822\.SIZE)\s+(\d+)|\bINTERNALDATE\s+"([^"]+)"', re.I)


def _internaldate(value):
    try:
        return dt.datetime.strptime(value.decode("ascii").strip(), "%d-%b-%Y %H:%M:%S %z")
    except (UnicodeDecodeError, ValueError):
        return None


def _apply_meta(rec, meta):
    for m in _META.finditer(meta):
        if m.group(3):
            rec.setdefault("internaldate", _internaldate(m.group(3)))
        elif m.group(1).upper() == b"UID":
            rec.setdefault("uid", int(m.group(2)))
        else:
            rec.setdefault("size", int(m.group(2)))


def parse_fetch(data):
    """imaplib FETCH data -> [{"uid", "size", "internaldate", "body"}]. The literal is the body;
    UID and sizes may come before or after it; FETCH responses without a literal (e.g.
    unsolicited FLAGS updates) are ignored."""
    records, cur = [], None
    for item in data or ():
        if isinstance(item, tuple) and len(item) >= 2:
            meta, body = item[0], item[1]
            if cur is None or _NEW_FETCH.match(meta):
                cur = {"body": body}
                records.append(cur)
            _apply_meta(cur, meta)
        elif isinstance(item, bytes):
            if _NEW_FETCH.match(item):
                cur = None
            elif cur is not None:
                _apply_meta(cur, item)
    return [r for r in records if "uid" in r and isinstance(r["body"], bytes)]


def fetch_headers(conn, uids):
    """{uid: record} with From and Authentication-Results only."""
    out = {}
    for i in range(0, len(uids), CHUNK):
        chunk = uids[i:i + CHUNK]
        typ, data = conn.uid("FETCH", ",".join(map(str, chunk)), HEADER_ITEMS)
        _ok(typ, "UID FETCH")
        wanted = set(chunk)
        out.update((r["uid"], r) for r in parse_fetch(data) if r["uid"] in wanted)
    return out


def fetch_body(conn, uid, size, max_bytes):
    """Whole message, or only its first max_bytes when it is larger."""
    partial = bool(size and size > max_bytes)
    section = f"BODY.PEEK[]<0.{max_bytes}>" if partial else "BODY.PEEK[]"
    typ, data = conn.uid("FETCH", str(uid), f"(UID RFC822.SIZE INTERNALDATE {section})")
    _ok(typ, "UID FETCH")
    rec = next((r for r in parse_fetch(data) if r["uid"] == uid), None)
    if rec is None:
        return None             # expunged meanwhile
    body = rec["body"]
    return {"raw": body[:max_bytes], "size": rec.get("size", size or len(body)),
            "truncated": partial or len(body) > max_bytes, "internaldate": rec.get("internaldate")}


# ---------------------------------------------------------------- /new and /stats
Scan = collections.namedtuple("Scan", "uidvalidity uids done accepted rejected")


def scan(conn, store, cfg):
    """Unprocessed messages split by the sender filter (headers only, nothing is written)."""
    uv = uidvalidity(conn, cfg.mailbox)
    uids = all_uids(conn)
    done = store.processed(uv)
    pending = [uid for uid in uids if uid not in done]
    heads = fetch_headers(conn, pending)
    accepted, rejected = [], []
    for uid in pending:
        rec = heads.get(uid)
        if rec is None:
            continue
        addr, dmarc_fail = mailparse.sender(rec["body"])
        kind = mailparse.classify(addr, dmarc_fail, cfg.allowed_from, cfg.school_domains)
        if kind:
            accepted.append((uid, kind, rec.get("size")))
        else:
            rejected.append(uid)
    return Scan(uv, uids, done, accepted, rejected)


def _message(conn, cfg, uv, uid, kind, size, deadline=None):
    mid = f"{uv}-{uid}"
    got = fetch_body(conn, uid, size, cfg.max_bytes)
    if got is None:
        return None
    try:
        parsed = mailparse.parse_message(got["raw"], forwarder=kind == "forward", size=got["size"],
                                         truncated=got["truncated"], internaldate=got["internaldate"],
                                         read_attachment=attachtext.read, deadline=deadline)
    except Exception as e:      # never log the exception text: it may quote the message
        log.warning("message %s: parse failed (%s)", mid, type(e).__name__)
        parsed = {"from": None, "original_from": None, "date": None, "subject": None,
                  "forwarded": False, "original_date": None, "original_subject": None,
                  "text": "", "links": [], "attachments": [], "truncated": got["truncated"],
                  "size": got["size"], "error": "parse_failed"}
    return {"id": mid, **parsed}


def new_messages(conn, store, cfg, limit):
    """(payload, skipped): accepted unprocessed messages, oldest first, at most `limit`;
    messages that fail the filter are marked skipped."""
    s = scan(conn, store, cfg)
    skipped = store.mark(s.uidvalidity, s.rejected, "skipped")
    messages = []
    deadline = time.monotonic() + ATTACH_TIME_PER_CALL
    for uid, kind, size in s.accepted[:limit]:
        item = _message(conn, cfg, s.uidvalidity, uid, kind, size, deadline)
        if item:
            messages.append(item)
    payload = {"count": len(messages), "remaining": max(0, len(s.accepted) - limit),
               "messages": messages}
    return payload, skipped


def check(conn, store, cfg):
    """Cheap poll for the scheduler's condition script: marks messages that fail the filter
    as skipped (as /new does). Returns (counts, newly skipped, ids of new messages oldest first)."""
    s = scan(conn, store, cfg)
    skipped = store.mark(s.uidvalidity, s.rejected, "skipped")
    ids = [f"{s.uidvalidity}-{uid}" for uid, _, _ in s.accepted]
    return {"new": len(s.accepted), "total": len(s.uids)}, skipped, ids


def unserved(ids, served, now, batch=10, after=3600):
    """How many of the next `batch` new messages (what /new would return) the agent has not
    received within `after` seconds. If those were served and not acked, a failed run is not
    retried before an hour passes, whatever the scheduler remembers."""
    return sum(1 for i in ids[:batch] if now - served.get(i, float("-inf")) >= after)


def stats(conn, store, cfg):
    s = scan(conn, store, cfg)
    present = set(s.uids)
    acked = sum(1 for uid, st in s.done.items() if st == "acked" and uid in present)
    skipped = sum(1 for uid, st in s.done.items() if st == "skipped" and uid in present)
    return {"total": len(s.uids), "new": len(s.accepted), "acked": acked,
            "skipped_filter": skipped + len(s.rejected)}


_ID = re.compile(r"(\d{1,10})-(\d{1,10})")


def parse_id(value):
    m = _ID.fullmatch(value.strip()) if isinstance(value, str) else None
    return (int(m.group(1)), int(m.group(2))) if m else None
