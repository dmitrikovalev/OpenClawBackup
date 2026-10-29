"""HTTP API over a read-only school mailbox (for the OpenClaw agent).

  GET  /health                 service status and whether IMAP login + EXAMINE work (no mail data)
  GET  /stats                  counts only: total, new, acked, skipped_filter
  GET  /check                  counts only: new, total, unserved (polled every few minutes by the
                               scheduler; unserved = new messages /new has not given out this hour)
  GET  /new?limit=10           new accepted messages as plain data, oldest first
  POST /ack {"ids": [...]}     mark messages processed (after the summary was delivered)

Mail on the server is never changed: the mailbox is opened with EXAMINE and fetched with
BODY.PEEK. What was processed is kept locally in SQLite (STATE_DIR, UIDVALIDITY + UID).
Messages returned by /new stay new until they are acked. Settings come from the
environment (env_file .env.mail). Logs carry ids, counts and statuses only: no password,
no addresses, no mail text.

Run: uvicorn server:app --host 0.0.0.0 --port 8000
"""
import contextlib
import datetime as dt
import logging
import os
import threading
import time

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

import mailparse
import reader
import store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("schoolmail")

app = FastAPI(title="School mail (read-only)", version="1.0")


class SkipHealthLog(logging.Filter):
    """Drop uvicorn access-log lines for GET /health and /check (healthcheck and poll noise)."""
    def filter(self, record):
        args = record.args
        return not (isinstance(args, tuple) and len(args) >= 3
                    and str(args[2]).split("?")[0] in ("/health", "/check"))


logging.getLogger("uvicorn.access").addFilter(SkipHealthLog())

CFG = reader.Config.from_env()
STATE_DIR = os.environ.get("STATE_DIR", "/state")
os.makedirs(STATE_DIR, exist_ok=True)
STORE = store.Store(os.path.join(STATE_DIR, "schoolmail.db"))
LOCK = threading.Lock()                 # one IMAP session at a time
HEALTH_TTL = {True: 600, False: 900}    # seconds between IMAP checks made by /health
IMAP = {"ok": None, "error": None, "checked": None, "at": 0.0}
SERVED = {}                             # message id -> time.monotonic() of the last /new with it


def dubai_now():
    return dt.datetime.now(mailparse.DUBAI).isoformat(timespec="seconds")


def safe_error(e):
    text = f"{type(e).__name__}: {e}"
    for secret in (CFG.password, CFG.user):
        if secret:
            text = text.replace(secret, "***")
    return mailparse.clean_line(text, 200)


def record_imap(ok, error=None):
    if ok != IMAP["ok"]:
        log.info("imap: %s", "ok" if ok else f"error: {error}")
    IMAP.update(ok=ok, error=error, checked=dubai_now(), at=time.monotonic())


@contextlib.contextmanager
def mailbox(timeout=30):
    if CFG.error:
        raise HTTPException(503, f"not configured: {CFG.error}")
    try:
        with reader.session(CFG, timeout) as conn:
            yield conn
    except HTTPException:
        raise
    except Exception as e:
        err = safe_error(e)
        record_imap(False, err)
        raise HTTPException(502, f"IMAP error: {err}")
    record_imap(True)


@app.get("/health")
def health():
    if CFG.error:
        return {"status": "error", "imap": "not configured", "detail": CFG.error,
                "time_dubai": dubai_now()}
    fresh = IMAP["ok"] is not None and time.monotonic() - IMAP["at"] < HEALTH_TTL[IMAP["ok"]]
    if not fresh and LOCK.acquire(blocking=False):      # busy: answer from the last result
        try:
            with reader.session(CFG, timeout=8):
                pass
            record_imap(True)
        except Exception as e:
            record_imap(False, safe_error(e))
        finally:
            LOCK.release()
    state = {True: "ok", False: "error", None: "unknown"}[IMAP["ok"]]
    return {"status": "ok" if IMAP["ok"] else "degraded", "imap": state,
            "imap_error": IMAP["error"], "imap_checked": IMAP["checked"], "time_dubai": dubai_now()}


@app.get("/stats")
def stats():
    with LOCK, mailbox() as conn:
        result = reader.stats(conn, STORE, CFG)
    log.info("stats: total=%(total)d new=%(new)d acked=%(acked)d skipped=%(skipped_filter)d", result)
    return result


@app.get("/check")
def check():
    with LOCK, mailbox() as conn:
        result, skipped, ids = reader.check(conn, STORE, CFG)
    result["unserved"] = reader.unserved(ids, SERVED, time.monotonic())
    if result["new"] or skipped:            # quiet polls leave no log lines
        log.info("check: new=%d unserved=%d total=%d newly_skipped=%d",
                 result["new"], result["unserved"], result["total"], skipped)
    return result


@app.get("/new")
def new(limit: int = Query(10, ge=1, le=50)):
    with LOCK, mailbox() as conn:
        result, skipped = reader.new_messages(conn, STORE, CFG, limit)
    now = time.monotonic()
    SERVED.update((m["id"], now) for m in result["messages"])
    log.info("new: ids=%s remaining=%d newly_skipped=%d",
             [m["id"] for m in result["messages"]], result["remaining"], skipped)
    return result


class AckRequest(BaseModel):
    ids: list[str] = Field(..., max_length=500)


@app.post("/ack")
def ack(req: AckRequest):
    groups, invalid, valid = {}, [], set()
    for value in req.ids:
        parsed = reader.parse_id(value)
        if parsed:
            groups.setdefault(parsed[0], set()).add(parsed[1])
            valid.add(f"{parsed[0]}-{parsed[1]}")
        else:
            invalid.append(mailparse.clean_line(str(value), 40))
    acked = sum(STORE.mark(uv, uids, "acked") for uv, uids in groups.items())
    for value in valid:
        SERVED.pop(value, None)
    log.info("ack: ids=%s acked=%d already=%d invalid=%d",
             sorted(valid), acked, len(valid) - acked, len(invalid))
    return {"acked": acked, "already": len(valid) - acked, "invalid": invalid}
