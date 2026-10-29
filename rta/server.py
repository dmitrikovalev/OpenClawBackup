"""HTTP API for RTA intercity bus search and guest booking (for the OpenClaw agent).

  GET  /health
  GET  /search?from=abu-dhabi|dubai&to=...&date=YYYY-MM-DD&time=HH:MM&until=HH:MM&pax=N
  POST /book  {"from", "to", "date", "trip_id" | "departure_time", "pax"}  -> {"job_id"}
  GET  /book/{job_id}
  POST /book/{job_id}/otp     {"code": "1234"}
  POST /book/{job_id}/cancel

A booking runs in a background thread with Playwright (book.Run) and stops on the
external payment page. Statuses:
  holding -> otp_required -> processing -> payment_ready {payment_url, hold_expires_at}
                                        -> failed {reason} | cancelled
Only one booking at a time. Before payment_ready any failure or cancel releases the
held seats. Buyer data comes from the environment (env_file .env.rta) and never
leaves the service; the payment URL is returned only in GET /book/{job_id}
responses and is never logged.

Run: uvicorn server:app --host 0.0.0.0 --port 8000
"""
import argparse
import datetime as dt
import logging
import os
import queue
import re
import threading
import uuid

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

import book
import search_trips

app = FastAPI(title="RTA intercity bus", version="1.0")


class SkipHealthLog(logging.Filter):
    """Drop uvicorn access-log lines for GET /health (docker healthcheck noise)."""
    def filter(self, record):
        args = record.args
        return not (isinstance(args, tuple) and len(args) >= 3
                    and str(args[2]).split("?")[0] == "/health")


logging.getLogger("uvicorn.access").addFilter(SkipHealthLog())

JOBS_DIR = os.path.join(book.OUT, "jobs")
KEEP_JOBS = 20
RUNNING = {"holding", "otp_required", "processing"}
LOCK = threading.Lock()
JOBS = {}          # job_id -> Job, newest last
ACTIVE = None      # job_id of the running booking


def dubai_now():
    return dt.datetime.now(search_trips.DUBAI_TZ)


def check_route(dep, dest, pax):
    if dep not in search_trips.STATIONS or dest not in search_trips.STATIONS:
        raise HTTPException(422, f"from/to must be one of {list(search_trips.STATIONS)}")
    if dep == dest:
        raise HTTPException(422, "from and to must differ")
    if not 1 <= pax <= 10:
        raise HTTPException(422, "pax must be 1..10")


def parse(value, kind, name):
    try:
        return dt.date.fromisoformat(value) if kind == "date" else dt.time.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(422, f"{name} must be {'YYYY-MM-DD' if kind == 'date' else 'HH:MM'}")


# ---------------------------------------------------------------- search
@app.get("/health")
def health():
    try:
        book.load_buyer()
        buyer = "ok"
    except book.Abort as e:
        buyer = f"invalid: {e}"
    return {"status": "ok", "buyer_env": buyer, "active_job": ACTIVE,
            "time_dubai": dubai_now().isoformat(timespec="seconds")}


@app.get("/search")
def search(dep: str = Query(..., alias="from"), dest: str = Query(..., alias="to"),
           date: str = Query(None, description="YYYY-MM-DD, default today (Dubai)"),
           time: str = Query("00:00"), until: str = Query("23:59"), pax: int = Query(1)):
    check_route(dep, dest, pax)
    now = dubai_now()
    day = parse(date, "date", "date") if date else now.date()
    time_from, time_until = parse(time, "time", "time"), parse(until, "time", "until")
    if day < now.date():
        raise HTTPException(422, "date is in the past")
    if day == now.date() and time_from < now.time():
        time_from = now.time().replace(second=0, microsecond=0)
    try:
        return search_trips.search(dep, dest, day, time_from, time_until, pax)
    except Exception as e:
        raise HTTPException(502, f"RTA API error: {str(e)[:200]}")


# ---------------------------------------------------------------- booking jobs
class BookRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    dep: str = Field(alias="from")
    dest: str = Field(alias="to")
    date: str
    trip_id: int | None = None
    departure_time: str | None = None
    pax: int = 1


class OtpRequest(BaseModel):
    code: str


class Job(book.Hooks):
    otp_hint = "POST /book/{job_id}/otp"

    def __init__(self, req, departure, trip_id):
        self.id = uuid.uuid4().hex[:12]
        self.created = dt.datetime.now(dt.timezone.utc)
        self.request = {"from": req.dep, "to": req.dest, "date": req.date,
                        "departure_time": departure.strftime("%H:%M"), "trip_id": trip_id,
                        "pax": req.pax}
        self.status, self.reason, self.message = "holding", None, "searching the trip and holding seats"
        self.hold_expires_at = self.payment_url = self.trip = None
        self.seats, self.invoice, self.total, self.released = [], None, None, None
        self.otp = queue.Queue()
        self.cancel = threading.Event()
        self.run = None
        self.updated = self.created

    def set(self, status=None, message=None, **fields):
        if status:
            self.status = status
        if message is not None:
            self.message = message
        for k, v in fields.items():
            setattr(self, k, v)
        self.updated = dt.datetime.now(dt.timezone.utc)

    # book.Hooks ------------------------------------------------------
    def on_event(self, name, data):
        if name == "trip_found":
            self.set(trip=data)
        elif name == "seats_held":
            self.set(message="seats held", hold_expires_at=data.get("expires_at"),
                     seats=data.get("seats") or [])
        elif name in ("otp_required", "otp_resent"):
            self.set("otp_required", "SMS with a 4-digit code was sent to the buyer's phone"
                     if name == "otp_required" else
                     "the site rejected the code and sent a NEW SMS: the new code is needed")
        elif name == "otp_retry":
            self.set("otp_required", "the code was rejected, send it again")
        elif name == "otp_bad_format":
            self.set("otp_required", "the code must be exactly 4 digits")
        elif name == "otp_received":
            self.set("processing", "checking the code")
        elif name == "otp_verified":
            self.set("processing", "code accepted, confirming the reservation")
        elif name == "reservation_confirmed":
            self.set("processing", "reservation confirmed, requesting the payment page",
                     invoice=data.get("invoice_id"), total=data.get("total"))
        elif name == "payment_requested":
            # The raw URL is taken from the run, never from logs or events.
            self.set("payment_ready", "pay by the link before the hold expires; nothing has been paid",
                     payment_url=self.run.state.get("redirect_uri"))
        elif name in ("seats_released", "seats_still_reserved"):
            self.set(released=name == "seats_released")

    def otp_requested(self):
        while not self.otp.empty():
            self.otp.get_nowait()

    def get_otp(self, timeout):
        try:
            return self.otp.get(timeout=timeout)
        except queue.Empty:
            return None

    def cancelled(self):
        return self.cancel.is_set()

    # API view ----------------------------------------------------------
    def view(self):
        out = {"job_id": self.id, "status": self.status, "message": self.message,
               "request": self.request, "trip": self.trip, "seats": self.seats,
               "hold_expires_at": self.hold_expires_at,
               "invoice_id": self.invoice, "total": self.total,
               "created": self.created.isoformat(timespec="seconds"),
               "updated": self.updated.isoformat(timespec="seconds")}
        if self.status == "otp_required" and self.run:
            out["otp_deadline"] = self.run.state.get("otp_deadline")
        if self.status == "payment_ready":
            out["payment_url"] = self.payment_url
        if self.status in ("failed", "cancelled"):
            out["reason"] = self.reason
            out["seats_released"] = self.released
        return out


def run_job(job, args):
    global ACTIVE
    job.run = book.Run(args, out=os.path.join(JOBS_DIR, job.id), hooks=job)
    try:
        job.run.run()
        if job.status != "payment_ready":
            job.set("failed", reason="finished without a payment link")
    except BaseException as e:
        if job.status == "payment_ready":
            pass   # the link is out; a later error (screenshot etc.) does not change that
        elif job.cancel.is_set():
            job.set("cancelled", "cancelled, held seats released" if job.released else "cancelled",
                    reason="cancelled by user")
        else:
            job.set("failed", "booking stopped" + (", held seats released" if job.released else ""),
                    reason=book.RED(str(e))[:300])
    finally:
        job.released = job.run.result.get("released", job.released)
        with LOCK:
            if ACTIVE == job.id:
                ACTIVE = None


def get_job(job_id):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job_id")
    return job


@app.post("/book", status_code=202)
def start_booking(req: BookRequest):
    global ACTIVE
    check_route(req.dep, req.dest, req.pax)
    day = parse(req.date, "date", "date")
    if day < dubai_now().date():
        raise HTTPException(422, "date is in the past")
    if (req.trip_id is None) == (req.departure_time is None):
        raise HTTPException(422, "give exactly one of trip_id or departure_time")
    try:
        book.load_buyer()
    except book.Abort as e:
        raise HTTPException(503, f"buyer data is not configured: {e}")
    if req.trip_id is not None:
        try:
            found = search_trips.search(req.dep, req.dest, day, dt.time(0, 0), dt.time(23, 59), req.pax)
        except Exception as e:
            raise HTTPException(502, f"RTA API error: {str(e)[:200]}")
        trip = next((t for t in found["trips"] if t["trip_id"] == req.trip_id), None)
        if not trip:
            raise HTTPException(404, f"trip {req.trip_id} not found on {day} with {req.pax} free seat(s)")
        departure = dt.time.fromisoformat(trip["departure"])
    else:
        departure = parse(req.departure_time, "time", "departure_time")
    with LOCK:
        if ACTIVE:
            raise HTTPException(409, {"error": "another booking is running", "active_job": ACTIVE})
        job = Job(req, departure, req.trip_id)
        JOBS[job.id] = job
        for old in list(JOBS)[:-KEEP_JOBS]:
            if old != ACTIVE:
                JOBS.pop(old)
        ACTIVE = job.id
    args = argparse.Namespace(dep=req.dep, dest=req.dest, date=day, time=departure, pax=req.pax,
                              method="dubaipay", stop_after="payment", save_url=False,
                              clean_check=False)
    threading.Thread(target=run_job, args=(job, args), name=f"book-{job.id}", daemon=True).start()
    return {"job_id": job.id, "status": job.status}


@app.get("/book/{job_id}")
def booking_status(job_id: str):
    return get_job(job_id).view()


@app.post("/book/{job_id}/otp")
def send_otp(job_id: str, body: OtpRequest):
    job = get_job(job_id)
    code = re.sub(r"\s", "", body.code)
    if not re.fullmatch(r"\d{4}", code):
        raise HTTPException(422, "code must be exactly 4 digits")
    if job.status != "otp_required":
        raise HTTPException(409, f"job is {job.status}, not waiting for a code")
    job.otp.put(code)
    job.set("processing", "code received")
    return {"job_id": job.id, "status": job.status}


@app.post("/book/{job_id}/cancel")
def cancel_booking(job_id: str):
    job = get_job(job_id)
    if job.status in ("failed", "cancelled"):
        return job.view()
    if job.status == "payment_ready" or (job.run and job.run.confirmed):
        raise HTTPException(409, "the reservation is already confirmed: seats cannot be released, "
                                 "an unpaid booking expires by itself")
    job.cancel.set()
    job.set(message="cancelling: releasing held seats")
    return {"job_id": job.id, "status": job.status, "message": job.message}
