"""Guest booking on bus.rta.ae up to the external payment page, then STOP.

Buyer data comes only from the environment (docker run --env-file .env.rta):
RTA_TITLE, RTA_NAME (first name + last name), RTA_EMAIL, RTA_COUNTRY_CODE, RTA_PHONE.
Values are never printed: stdout/stderr, logs, HTML/text dumps are redacted and
screenshots are taken with form fields and personal data masked.

Flow: search -> trip card -> Auto Select Seats -> Continue to Payment -> Guest Checkout
-> contact form -> OTP by SMS (code read from out/otp.txt, written by the user)
-> Confirm Reservation -> choose payment method -> "Pay Now" on bus.rta.ae
-> external payment page: screenshot and record, NO clicks and NO card data.
Then opens the redirectUri in a new clean browser context (no cookies/storage).

On any failure before the payment page is reached the held seats are released
(UI first, then ReleaseReservation over the page's own hub connection) and the
script stops.

Run() can also be driven by another program (server.py) through Hooks: status
events, OTP codes from a queue instead of the file, and cancellation.

Usage:
  python -u book.py --from abu-dhabi --to dubai --date 2026-10-28 --time 07:30 --pax 1
  python -u book.py ... --stop-after search    # only find and select the trip card, no hold
  python -u book.py ... --stop-after seats     # dry run: hold seats, release, stop
  python -u book.py ... --stop-after form      # also fill and check the form (not sent), release
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse

from playwright.sync_api import sync_playwright

import search_trips
from recon4 import SSE_HOOK, choose_station, close_tour, try_release

URL = "https://bus.rta.ae/"
OUT = "out"
OTP_FILE = os.path.join(OUT, "otp.txt")
PRIVATE = os.path.join(OUT, "private")
MODAL = "div.fixed.inset-0"
STATION_QUERY = {
    "abu-dhabi": ("Abu Dhabi", re.compile(r"central|abu dhabi bus station", re.I)),
    "dubai": ("Ibn Battuta", re.compile(r"ibn battuta", re.I)),
}
TITLES = ("Mr", "Mrs", "Ms", "Dr")
PII_KEYS = {"email", "phonenumber", "phone", "firstname", "lastname", "code", "token",
            "authorization", "redirecturi", "name", "fullname", "mobile", "nationality",
            "externalid", "maskedeid", "refreshtoken"}
os.makedirs(OUT, exist_ok=True)


class Abort(Exception):
    pass


# ---------------------------------------------------------------- redaction
class Redactor:
    def __init__(self):
        self.values = []
        self.phone_tail = None

    def add(self, *values):
        for v in values:
            v = (v or "").strip()
            if len(v) >= 3 and v not in self.values:
                self.values.append(v)
        self.values.sort(key=len, reverse=True)

    def __call__(self, text):
        if not text:
            return text
        text = str(text)
        for v in self.values:
            text = re.sub(re.escape(v), "***", text, flags=re.I)
        text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "<email>", text)
        if self.phone_tail:
            def phone(m):
                digits = re.sub(r"\D", "", m.group(0))
                return "<phone>" if digits.endswith(self.phone_tail) else m.group(0)
            text = re.sub(r"\+?\d[\d\s\-()]{5,}\d", phone, text)
        return text


RED = Redactor()


class RedactedStream:
    def __init__(self, stream):
        self.stream = stream

    def write(self, s):
        return self.stream.write(RED(s))

    def flush(self):
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


sys.stdout = RedactedStream(sys.stdout)
sys.stderr = RedactedStream(sys.stderr)


def redact_json(obj):
    if isinstance(obj, dict):
        return {k: ("***" if k.lower() in PII_KEYS and obj[k] not in (None, "") else redact_json(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_json(v) for v in obj]
    if isinstance(obj, str):
        return RED(obj)
    return obj


def sanitize_url(u):
    """scheme://host/path with id/token-like path segments and all query values hidden."""
    p = urllib.parse.urlsplit(u)
    segs = ["<id>" if re.search(r"\d{4,}|[A-Za-z0-9_\-]{16,}", s) else s for s in p.path.split("/")]
    query = "&".join(f"{k}=<…>" for k, _ in urllib.parse.parse_qsl(p.query, keep_blank_values=True))
    return urllib.parse.urlunsplit((p.scheme, p.netloc, "/".join(segs), query, ""))


def is_fake_redirect(u):
    """Same test as the site's y4(): such URIs are confirmed as paid without a gateway."""
    t = u.strip()
    if t.upper().startswith("FAKE"):
        return True
    p = urllib.parse.urlsplit(t)
    return "fake" in p.netloc.lower() or any(s.upper().startswith("FAKE") for s in p.path.split("/") if s)


def event(name, **data):
    print(json.dumps({"t": time.strftime("%H:%M:%S"), "event": name, **redact_json(data)},
                     ensure_ascii=False), flush=True)


# ---------------------------------------------------------------- buyer data
def load_buyer():
    env = {k: os.environ.get(k, "").strip() for k in
           ("RTA_TITLE", "RTA_NAME", "RTA_EMAIL", "RTA_COUNTRY_CODE", "RTA_PHONE")}
    missing = [k for k, v in env.items() if not v]
    if missing:
        raise Abort(f"empty env: {missing}")
    title = env["RTA_TITLE"].rstrip(".").capitalize()
    if title not in TITLES:
        raise Abort(f"RTA_TITLE must be one of {TITLES}")
    parts = env["RTA_NAME"].split()
    if len(parts) < 2 or any(len(p) < 2 for p in (parts[0], " ".join(parts[1:]))):
        raise Abort("RTA_NAME must be 'First Last' (each at least 2 letters)")
    code = re.sub(r"\D", "", env["RTA_COUNTRY_CODE"])
    phone = re.sub(r"\D", "", env["RTA_PHONE"])
    if phone.startswith(code) and len(phone) > 9:
        phone = phone[len(code):]
    phone = phone.lstrip("0")
    if not code or len(phone) < 7:
        raise Abort("RTA_COUNTRY_CODE / RTA_PHONE look invalid")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", env["RTA_EMAIL"]):
        raise Abort("RTA_EMAIL looks invalid")
    buyer = {"title": title, "first": parts[0], "last": " ".join(parts[1:]),
             "email": env["RTA_EMAIL"], "code": code, "phone": phone}
    RED.add(env["RTA_NAME"], buyer["first"], buyer["last"], *parts, buyer["email"],
            buyer["phone"], env["RTA_PHONE"])
    RED.phone_tail = phone[-7:]
    return buyer


# ---------------------------------------------------------------- integration
class Hooks:
    """How a caller talks to Run: status events, OTP codes and cancellation."""
    otp_hint = ""

    def on_event(self, name, data):
        """Called for every event, data already redacted."""

    def otp_requested(self):
        """The site has just sent a new OTP: forget codes given before."""

    def get_otp(self, timeout):
        """Return a code given by the user, or None after waiting up to timeout seconds."""
        time.sleep(timeout)
        return None

    def cancelled(self):
        return False


class FileHooks(Hooks):
    """CLI: the user writes the code into out/otp.txt."""
    otp_hint = "write the code into out/otp.txt"

    def otp_requested(self):
        if os.path.exists(OTP_FILE):
            os.remove(OTP_FILE)   # stale code from an earlier attempt

    def get_otp(self, timeout):
        if os.path.exists(OTP_FILE):
            with open(OTP_FILE, encoding="utf-8") as f:
                code = f.read()
            os.remove(OTP_FILE)
            return code
        time.sleep(timeout)
        return None


# ---------------------------------------------------------------- browser helpers
class Run:
    def __init__(self, args, out=OUT, hooks=None):
        self.a = args
        self.out = out
        self.hooks = hooks or FileHooks()
        os.makedirs(out, exist_ok=True)
        self.api = []
        self.hosts = {}
        self.hub_url = None
        self.hub_calls = []
        self.state = {}        # trip_id, hold_id, session_id, seats, expires_at, ...
        self.result = {"started": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
        self.step = 0
        self.confirmed = False
        self.reached_gateway = False

    def emit(self, name, **data):
        event(name, **data)
        try:
            self.hooks.on_event(name, redact_json(data))
        except Exception as e:
            event("hook_failed", error=str(e)[:200])

    def check_cancel(self):
        if self.hooks.cancelled():
            raise Abort("cancelled by user")

    # network capture --------------------------------------------------
    def on_request(self, req):
        host = urllib.parse.urlsplit(req.url).netloc
        self.hosts[host] = self.hosts.get(host, 0) + 1
        if "/hubs/booking?id=" in req.url and req.method == "POST":
            self.hub_url = req.url
            for frame in (req.post_data or "").split("\x1e"):
                try:
                    msg = json.loads(frame)
                except ValueError:
                    continue
                if msg.get("target"):
                    self.hub_calls.append({"target": msg["target"], "arguments": msg.get("arguments")})

    def on_response(self, resp):
        if "bus-api.rta.ae/api" not in resp.url:
            return
        req = resp.request
        entry = {"method": req.method, "url": RED(resp.url.split("?")[0]), "status": resp.status}
        try:
            if req.post_data:
                entry["request"] = redact_json(json.loads(req.post_data))
        except ValueError:
            entry["request"] = "<non-json>"
        try:
            body = resp.json()
            if "request-payment" in resp.url and isinstance(body, dict):
                uri = (body.get("data") or {}).get("redirectUri")
                if uri:
                    self.state["redirect_uri"] = uri
                    self.add_url_secrets(uri)
            if "confirmationcode/verify" in resp.url and isinstance(body, dict):
                RED.add(str((body.get("data") or {}).get("token") or ""))
            entry["response"] = redact_json(body)
        except Exception:
            pass
        self.api.append(entry)

    @staticmethod
    def add_url_secrets(uri):
        p = urllib.parse.urlsplit(uri)
        RED.add(uri, p.query, *[v for _, v in urllib.parse.parse_qsl(p.query) if len(v) >= 6],
                *[s for s in p.path.split("/") if re.search(r"\d{4,}|[A-Za-z0-9_\-]{16,}", s)])

    # page output ---------------------------------------------------------
    def shot(self, page, name):
        """Full-page screenshot with inputs, iframes and any personal data masked.

        Personal data is marked in the DOM, then the screenshot is taken and the page is
        checked again: if the document was replaced or new unmarked personal data appeared
        meanwhile, the screenshot is retaken; after 3 failed tries it is deleted.
        """
        self.step += 1
        path = f"{self.out}/06_{self.step:02d}_{name}.png"
        for _ in range(3):
            try:
                page.wait_for_load_state("load", timeout=15000)
            except Exception:
                pass
            self._pii(page, "mark")
            page.wait_for_timeout(500)
            self._pii(page, "mark")
            masks = [page.locator(s) for s in (
                "input", "textarea", "select", "[contenteditable=true]", ".ant-select-selector",
                "[data-testid=select-country-code]", "iframe", "[data-pii]")]
            try:
                page.screenshot(path=path, full_page=True, mask=masks, mask_color="#555")
            except Exception as e:
                self.emit("screenshot_retry", error=str(e)[:200])
                page.wait_for_timeout(1500)
                continue
            if self._pii(page, "check") == 0:
                break
            page.wait_for_timeout(1500)
        else:
            if os.path.exists(path):
                os.remove(path)
            self.emit("screenshot_skipped", path=path, note="PII mask could not be confirmed")
            return
        try:
            text = page.inner_text("body")
        except Exception:
            text = ""
        with open(path[:-4] + ".txt", "w", encoding="utf-8") as f:
            f.write(sanitize_url(page.url) + "\n\n" + RED(text)[-12000:])
        self.emit("screenshot", path=path)

    def _pii(self, page, mode):
        """mode "mark": tag elements holding personal data with data-pii.
        mode "check": number of personal-data text nodes left untagged (-1 if the page was
        replaced since marking or cannot be inspected)."""
        try:
            return page.evaluate("""([vals, mode]) => {
              if (mode === 'check' && window.__piiMarked !== true) return -1;
              const low = vals.map(v => v.toLowerCase());
              const tail = vals.find(v => /^\\d{7,}$/.test(v));
              const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
              let untagged = 0;
              for (let n; (n = walk.nextNode());) {
                const t = n.textContent.toLowerCase();
                if (!t.trim() || !n.parentElement) continue;
                const digits = t.replace(/\\D/g, '');
                const pii = low.some(v => t.includes(v)) || /@|[•*]{2,}/.test(t) ||
                    (tail && digits.length >= 7 && digits.endsWith(tail.slice(-7)));
                if (!pii) continue;
                if (mode === 'mark') n.parentElement.setAttribute('data-pii', '1');
                else if (!n.parentElement.closest('[data-pii]')) untagged++;
              }
              if (mode === 'mark') window.__piiMarked = true;
              return untagged;
            }""", [[v for v in RED.values + [RED.phone_tail or ""] if len(v) >= 3], mode])
        except Exception:
            return -1

    def buttons(self, page, scope=None):
        loc = (scope or page).locator("button:visible,[role=button]:visible,[role=radio]:visible")
        return [RED(t.strip().replace("\n", " / "))[:80] for t in loc.all_inner_texts() if t.strip()]

    def dump(self):
        self.result["hold"] = {k: v for k, v in self.state.items() if k != "redirect_uri"}
        with open(f"{self.out}/06_api.json", "w", encoding="utf-8") as f:
            json.dump(self.api, f, ensure_ascii=False, indent=2)
        with open(f"{self.out}/06_result.json", "w", encoding="utf-8") as f:
            json.dump(redact_json({**self.result, "hosts": self.hosts,
                                   "hub_calls": [c["target"] for c in self.hub_calls]}),
                      f, ensure_ascii=False, indent=2)

    # ---------------------------------------------------------------- step 1
    def precheck(self):
        a = self.a
        found = search_trips.search(a.dep, a.dest, a.date, a.time, a.time, a.pax)
        trip = next((t for t in found["trips"] if t["departure"] == a.time.strftime("%H:%M")), None)
        if not trip:
            raise Abort(f"no trip {a.date} {a.time:%H:%M} with {a.pax} free seat(s)")
        self.state.update(trip_id=trip["trip_id"], boarding=found["boarding_stop_id"],
                          alighting=found["alighting_stop_id"])
        self.result["trip"] = {**trip, "route": found["title"], "passengers": a.pax}
        self.emit("trip_found", **self.result["trip"])

    def set_dial(self, page, label, target):
        dial = page.locator(f"{MODAL} [role=slider][aria-label='{label}']").first
        dial.focus()
        for _ in range(80):
            now = int(dial.get_attribute("aria-valuenow"))
            if now == target:
                return
            dial.press("ArrowUp" if now < target else "ArrowDown")
            page.wait_for_timeout(100)
        raise Abort(f"could not set {label} to {target}")

    def pick_datetime(self, page):
        a = self.a
        page.get_by_role("button").filter(has_text="Select Date & Time").first.click(timeout=10000)
        modal = page.locator(MODAL).first
        modal.wait_for(timeout=10000)
        month = a.date.strftime("%b %Y")
        for _ in range(12):
            if modal.get_by_text(month, exact=True).count():
                break
            modal.locator("button[aria-label=Next]").first.click()
            page.wait_for_timeout(400)
        else:
            raise Abort(f"month {month} not in calendar")
        days = modal.locator("[aria-hidden=false]").first.locator("button:not([disabled])").filter(
            has_text=re.compile(rf"^\s*{a.date.day}\s*$"))
        if not days.count():
            raise Abort(f"day {a.date} not selectable")
        (days.first if a.date.day < 15 else days.last).click()
        page.wait_for_timeout(800)
        if not page.locator(f"{MODAL} [role=slider]:visible").count():
            modal.locator("button").filter(has_text="Next").last.click()
            page.wait_for_timeout(800)
        # Search a few minutes before the departure so the trip is the first card.
        start = (dt.datetime.combine(a.date, a.time) - dt.timedelta(minutes=5))
        if start.date() != a.date:
            start = dt.datetime.combine(a.date, dt.time(0, 0))
        self.set_dial(page, "Hour dial", start.hour)
        modal.get_by_role("button", name="Select minutes").click()
        page.wait_for_timeout(600)
        self.set_dial(page, "Minute dial", start.minute)
        footer = modal.locator("button").filter(has_text="·").last
        want = f"{a.date.day} {a.date:%b} · {start:%H:%M}"
        if want not in footer.inner_text().replace("0" + str(a.date.day), str(a.date.day)):
            raise Abort(f"date picker shows {footer.inner_text()!r}, expected {want!r}")
        footer.click()
        page.wait_for_timeout(800)

    def pick_pax(self, page):
        page.get_by_role("button").filter(has_text="Passengers").first.click(timeout=10000)
        modal = page.locator(MODAL).first
        modal.wait_for(timeout=10000)
        for _ in range(self.a.pax - 1):
            modal.get_by_role("button", name="Increase Adult", exact=True).click()
            page.wait_for_timeout(250)
        if f"{self.a.pax} passenger" not in modal.inner_text():
            raise Abort("passenger counter did not reach the requested number")
        modal.get_by_role("button", name="Confirm", exact=True).click()
        page.wait_for_timeout(800)

    def search_and_select(self, page):
        a = self.a
        page.goto(URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)
        close_tour(page)
        page.get_by_text("Start your booking as a guest").first.click(timeout=15000)
        page.wait_for_load_state("networkidle", timeout=60000)
        page.wait_for_timeout(2000)
        close_tour(page)
        choose_station(page, 0, *STATION_QUERY[a.dep])
        choose_station(page, 1, *STATION_QUERY[a.dest])
        self.pick_datetime(page)
        self.pick_pax(page)
        with page.expect_response(lambda r: "scheduled-trips?" in r.url, timeout=60000):
            page.get_by_text("Search Trips", exact=True).first.click(timeout=15000)
        page.wait_for_timeout(4000)
        close_tour(page)
        hhmm = a.time.strftime("%H:%M")
        cards = page.locator("button:visible,[role=button]:visible").filter(has_text=hhmm)
        card = next((cards.nth(i) for i in range(cards.count())
                     if cards.nth(i).inner_text().strip().startswith(hhmm)), None)
        if card is None:
            raise Abort(f"trip card {hhmm} not shown")
        card.click(timeout=10000)
        page.wait_for_timeout(2500)
        text = page.inner_text("body")
        if not re.search(rf"DEPARTURE\s*{hhmm}", text, re.I):
            raise Abort("trip details do not show the requested departure")
        m = re.search(r"Tickets\s*(\d+)", text)
        if m and int(m.group(1)) != a.pax:
            raise Abort(f"tickets counter shows {m.group(1)}, expected {a.pax}")
        self.shot(page, "trip_selected")

    # ---------------------------------------------------------------- step 2
    def read_hold(self, page):
        """Hold details from the hub's AutoSelectReservation result (SSE), sessionStorage as fallback."""
        trip_id = self.state["trip_id"]
        try:
            for m in page.evaluate("window.__sse ? window.__sse.splice(0) : []"):
                for frame in str(m.get("data") or "").split("\x1e"):
                    try:
                        msg = json.loads(frame)
                    except ValueError:
                        continue
                    data = ((msg.get("result") or {}) if msg.get("type") == 3 else {}).get("data") or {}
                    if isinstance(data, dict) and data.get("holdId"):
                        self.state["hub_hold"] = data
        except Exception:
            pass
        try:
            store = page.evaluate("Object.fromEntries(Object.keys(sessionStorage)"
                                  ".map(k => [k, sessionStorage.getItem(k)]))")
        except Exception:
            store = {}
        hub = self.state.get("hub_hold") or {}
        self.state["hold_id"] = hub.get("holdId") or store.get("bookingReservationHoldId")
        self.state["session_id"] = hub.get("sessionId") or store.get("bookingReservationSessionId")
        self.state["expires_at"] = store.get("bookingReservationExpiresAt") or hub.get("expiresAt")
        titles = json.loads(store.get(f"selectedSeats_{trip_id}") or "[]")
        seats = search_trips.get_json(f"scheduled-trips/{trip_id}/seats", {
            "BoardingPatternStopId": self.state["boarding"],
            "AlightingPatternStopId": self.state["alighting"]})["data"]["seats"]
        ids = set(hub.get("reservedSeatIds") or [])
        if not ids:   # no hub result: our seat titles from sessionStorage
            ids = {x["id"] for x in seats if x["title"] in map(str, titles) and x["isReserved"]}
        if ids:
            self.state["seats"] = [{"id": x["id"], "title": x["title"]} for x in seats if x["id"] in ids]

    def hold(self, page):
        trip_id = self.state["trip_id"]
        with page.expect_response(lambda r: f"scheduled-trips/{trip_id}/seats" in r.url, timeout=60000):
            page.get_by_role("button", name="Auto Select Seats", exact=True).first.click(timeout=15000)
        page.wait_for_timeout(2500)
        self.read_hold(page)
        seats = self.state.get("seats") or []
        self.emit("seats_held", hold_id=self.state["hold_id"], expires_at=self.state["expires_at"],
              seats=[x["title"] for x in seats], seat_ids=[x["id"] for x in seats],
              from_hub=bool(self.state.get("hub_hold")))
        auto = [c for c in self.hub_calls if c["target"] == "AutoSelectReservation"]
        if not auto:
            raise Abort("AutoSelectReservation was not sent")
        args = auto[-1]["arguments"]
        if args[0] != trip_id or sum(p["count"] for p in args[1]["passengers"]) != self.a.pax:
            raise Abort(f"site held a different trip/party: {args}")
        if len(seats) != self.a.pax or not self.state["session_id"]:
            raise Abort(f"expected {self.a.pax} seat(s) with a session, got {len(seats)}")
        if sorted(self.seats_reserved()) != sorted(x["title"] for x in seats):
            raise Abort("held seats are not shown as reserved by the seats API")
        self.shot(page, "seats_dialog")

    def seats_reserved(self):
        data = search_trips.get_json(f"scheduled-trips/{self.state['trip_id']}/seats", {
            "BoardingPatternStopId": self.state["boarding"],
            "AlightingPatternStopId": self.state["alighting"]})
        ids = {s["id"] for s in self.state.get("seats", [])}
        return [s["title"] for s in data["data"]["seats"] if s["id"] in ids and s["isReserved"]]

    def release(self, page):
        """Release held seats: site UI first, then ReleaseReservation on the hub connection."""
        if not self.state.get("seats") and self.hub_calls:
            try:
                self.read_hold(page)
            except Exception as e:
                self.emit("read_hold_failed", error=str(e)[:300])
        if not self.state.get("seats"):
            self.emit("release_skipped", note="no held seats known")
            return
        try:
            if not self.confirmed:
                try_release(page)
        except Exception as e:
            self.emit("release_ui_failed", error=str(e)[:300])
        page.wait_for_timeout(2000)
        left = self.seats_reserved()
        if left and self.hub_url and self.state.get("session_id"):
            for s in self.state["seats"]:
                frame = json.dumps({"type": 1, "target": "ReleaseReservation", "arguments": [
                    self.state["trip_id"], {"sessionId": self.state["session_id"],
                                            "scheduledTripSeatId": s["id"]}]}) + "\x1e"
                try:
                    page.evaluate("""([u, b]) => fetch(u, {method: 'POST', body: b, credentials: 'include',
                        headers: {'Content-Type': 'text/plain;charset=UTF-8'}}).then(r => r.status)""",
                                  [self.hub_url, frame])
                except Exception as e:
                    self.emit("release_hub_failed", error=str(e)[:300])
            page.wait_for_timeout(3000)
            left = self.seats_reserved()
        self.result["released"] = not left
        self.emit("seats_released" if not left else "seats_still_reserved", seats_still_reserved=left)

    # ---------------------------------------------------------------- step 3
    def click(self, scope, name, timeout=15000):
        btn = scope.get_by_role("button", name=name, exact=True).locator("visible=true").first
        btn.wait_for(timeout=timeout)
        btn.click(timeout=timeout)

    def guest_form(self, page, buyer):
        dlg = page.locator("[role=dialog]:visible,[role=alertdialog]:visible").first
        self.click(dlg, "Continue to Payment")
        page.get_by_test_id("button-guest-checkout").click(timeout=20000)
        page.wait_for_timeout(1000)
        if not page.get_by_test_id("input-first-name").is_visible():
            self.click(page.locator("#step-content-3"), "Verify & Continue")
        page.get_by_test_id("input-first-name").wait_for(timeout=20000)
        title = page.get_by_test_id("select-passenger-title")
        try:
            title.click(timeout=5000)
        except Exception:
            page.get_by_role("combobox", name="Title").click(timeout=5000)
        page.wait_for_timeout(800)
        # Dropdown items: visible elements with exactly this text, outside the field itself.
        root = title.element_handle()
        items = page.get_by_text(buyer["title"], exact=True).locator("visible=true")
        item = next((items.nth(i) for i in range(items.count())
                     if not items.nth(i).evaluate("(e, r) => r.contains(e)", root)), None)
        if item is None:
            raise Abort("title option not found in the dropdown")
        item.click()
        page.wait_for_timeout(500)
        if title.inner_text().strip() != buyer["title"]:
            raise Abort("title was not applied")
        page.get_by_test_id("input-first-name").fill(buyer["first"])
        page.get_by_test_id("input-last-name").fill(buyer["last"])
        page.get_by_test_id("input-email").fill(buyer["email"])
        cc = page.get_by_test_id("select-country-code")
        if re.sub(r"\D", "", cc.inner_text()) != buyer["code"]:
            cc.click()
            box = page.locator("div.absolute.top-full input[type=text]").first
            box.fill(buyer["code"])
            page.wait_for_timeout(800)
            opt = page.locator("div.absolute.top-full").get_by_text(f"+{buyer['code']}", exact=True)
            if not opt.count():
                raise Abort("country code not found in the list")
            opt.first.click()
            if re.sub(r"\D", "", cc.inner_text()) != buyer["code"]:
                raise Abort("country code was not applied")
        page.get_by_test_id("input-phone").fill(buyer["phone"])
        page.wait_for_timeout(800)
        check = {
            "title": title.inner_text().strip() == buyer["title"],
            "first": page.get_by_test_id("input-first-name").input_value() == buyer["first"],
            "last": page.get_by_test_id("input-last-name").input_value() == buyer["last"],
            "email": page.get_by_test_id("input-email").input_value() == buyer["email"],
            "code": re.sub(r"\D", "", cc.inner_text()) == buyer["code"],
            "phone": re.sub(r"\D", "", page.get_by_test_id("input-phone").input_value()) == buyer["phone"],
        }
        self.emit("form_filled", **{f"{k}_ok": v for k, v in check.items()})
        if not all(check.values()):
            raise Abort(f"form values differ: {[k for k, v in check.items() if not v]}")
        self.shot(page, "guest_form")
        errors = [RED(t) for t in page.locator(".ant-form-item-explain-error:visible").all_inner_texts()]
        if errors:
            raise Abort(f"form errors: {errors}")

    def request_otp(self, page):
        self.hooks.otp_requested()
        hold = self.state["hold_id"]
        try:
            with page.expect_response(lambda r: f"reservations/{hold}/confirmationcode/request" in r.url,
                                      timeout=30000) as resp:
                submit = page.locator("#step-content-3").get_by_role(
                    "button", name=re.compile(r"^\s*Verify (and Pay|& Continue)\s*$")).locator("visible=true")
                submit.first.click(timeout=15000)
            r = resp.value
        except Exception as e:
            errors = [RED(t) for t in page.locator(".ant-form-item-explain-error:visible").all_inner_texts()]
            raise Abort(f"OTP was not requested: {errors or str(e)[:200]}")
        if r.status != 200:
            raise Abort(f"confirmationcode/request -> {r.status}: {RED(r.text()[:300])}")
        source = ((r.json() or {}).get("data") or {}).get("source")
        self.result["otp_channel"] = source
        self.emit("otp_required", channel=source, note=self.hooks.otp_hint)
        page.locator("[role=dialog][aria-labelledby=otp-modal-title]").wait_for(timeout=20000)
        self.shot(page, "otp_modal")

    def wait_otp(self):
        exp = self.state.get("expires_at") or ""
        try:
            hold_end = dt.datetime.fromisoformat(exp[:19]).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            hold_end = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=12)
        deadline = min(dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10),
                       hold_end - dt.timedelta(seconds=90))
        self.state["otp_deadline"] = deadline.isoformat(timespec="seconds")
        while dt.datetime.now(dt.timezone.utc) < deadline:
            self.check_cancel()
            code = self.hooks.get_otp(2)
            if code is None:
                continue
            code = re.sub(r"\D", "", code)
            if len(code) == 4:
                RED.add(code)
                self.emit("otp_received")
                return code
            self.emit("otp_bad_format", note="need exactly 4 digits")
        raise Abort("no OTP before the deadline")

    def verify_otp(self, page):
        dlg = page.locator("[role=dialog][aria-labelledby=otp-modal-title]")
        hold = self.state["hold_id"]
        for attempt in (1, 2):
            code = self.wait_otp()
            sent = []
            listener = lambda req: sent.append(1) if "confirmationcode/verify" in req.url else None
            page.on("request", listener)
            try:
                with page.expect_response(lambda r: f"reservations/{hold}/confirmationcode/verify" in r.url,
                                          timeout=30000) as resp:
                    boxes = dlg.locator("[role=group] input")
                    if boxes.count() != 4:
                        raise Abort(f"expected 4 OTP inputs, found {boxes.count()}")
                    for i, d in enumerate(code):
                        boxes.nth(i).click()
                        boxes.nth(i).press_sequentially(d)
                    page.wait_for_timeout(1000)
                    if not sent:
                        dlg.get_by_role("button", name=re.compile(r"^\s*Verify", re.I)).first.click(timeout=10000)
                r = resp.value
            except Abort:
                raise
            except Exception as e:
                raise Abort(f"OTP verify did not happen: {str(e)[:200]}")
            finally:
                page.remove_listener("request", listener)
            if r.status == 200:
                self.emit("otp_verified")
                return
            body = r.text()[:300]
            stale = "not the latest reference" in body.lower()
            self.emit("otp_rejected", status=r.status, body=body, attempt=attempt)
            if attempt == 2:
                break
            for i in range(4):
                dlg.locator("[role=group] input").nth(i).fill("")
            if stale:
                # The server holds a newer OTP reference than this hold's request: ask for a
                # fresh code ("Resend code" appears after the 60 s cooldown, same request endpoint).
                resend = dlg.get_by_role("button", name=re.compile(r"^\s*Resend code\s*$"))
                resend.wait_for(timeout=75000)
                with page.expect_response(lambda x: f"reservations/{hold}/confirmationcode/request" in x.url,
                                          timeout=30000) as again:
                    resend.click()
                if again.value.status != 200:
                    raise Abort(f"resend -> {again.value.status}: {RED(again.value.text()[:300])}")
                self.hooks.otp_requested()
                self.emit("otp_resent", note="a NEW SMS was sent, the new code is needed")
            else:
                self.emit("otp_retry", note="send the code again")
        raise Abort("OTP rejected twice")

    def confirm_reservation(self, page):
        btn = page.get_by_test_id("button-confirm-reservation")
        btn.wait_for(timeout=30000)
        page.wait_for_timeout(1500)
        self.shot(page, "summary")
        if not btn.is_enabled():
            accept = page.get_by_test_id("button-accept-terms")
            if not accept.is_visible():   # terms panel is collapsed behind a ▼ toggle
                page.locator("button:visible").filter(has_text="▼").first.click(timeout=10000)
                page.wait_for_timeout(1000)
            terms = page.locator("div.max-h-96:visible").first
            if terms.count():
                with open(f"{self.out}/06_terms.txt", "w", encoding="utf-8") as f:
                    f.write(terms.inner_text())
            accept.click(timeout=10000)
            page.wait_for_timeout(800)
            self.result["terms_accepted"] = True
            self.emit("terms_accepted")
            self.shot(page, "summary_terms_accepted")
        if not btn.is_enabled():
            raise Abort("Confirm Reservation is disabled")
        with page.expect_response(lambda r: "reservations/confirm" in r.url, timeout=60000) as resp:
            btn.click()
        r = resp.value
        if r.status != 200:
            raise Abort(f"reservations/confirm -> {r.status}: {RED(r.text()[:300])}")
        self.confirmed = True
        data = (r.json() or {}).get("data") or {}
        self.result["invoice"] = {"invoice_id": data.get("invoiceId"), "total": data.get("total"),
                                  "payment_methods": [f"{m.get('id')}: {m.get('name')} ({m.get('code')})"
                                                      for m in data.get("availablePaymentMethod") or []]}
        self.emit("reservation_confirmed", **self.result["invoice"])
        if not data.get("availablePaymentMethod"):
            raise Abort("no payment methods offered")
        return data

    def pay_now(self, page, data):
        group = page.locator("[role=radiogroup]:visible").first
        group.wait_for(timeout=30000)
        page.wait_for_timeout(1000)
        self.result["payment_method_cards"] = self.buttons(page, group)
        self.shot(page, "payment_methods")
        methods = data["availablePaymentMethod"]
        want = self.a.method.lower().replace(" ", "")
        method = next((m for m in methods if want and want in m["name"].lower().replace(" ", "")),
                      None) or next(
            (m for m in methods if "apple" not in m["name"].lower()), methods[0])
        self.result["chosen_method"] = f"{method.get('id')}: {method.get('name')}"
        group.get_by_text(method["name"], exact=True).first.click()
        page.wait_for_timeout(800)
        invoice = data["invoiceId"]

        def guard(route):
            if route.request.method != "POST":
                return route.continue_()
            resp = route.fetch()
            try:
                uri = ((resp.json() or {}).get("data") or {}).get("redirectUri") or ""
            except Exception:
                uri = ""
            if uri and is_fake_redirect(uri):
                self.state["fake_redirect"] = True
                return route.abort()
            if uri:   # read here: the page navigates away before on_response can read the body
                self.state["redirect_uri"] = uri
                self.add_url_secrets(uri)
            route.fulfill(response=resp)

        page.route(re.compile(rf"/invoice/{invoice}/request-payment"), guard)
        # "Pay Now" on bus.rta.ae only creates the transaction and redirects to the
        # external payment page; nothing is paid here.
        try:
            with page.expect_response(lambda r: f"invoice/{invoice}/request-payment" in r.url,
                                      timeout=60000) as resp:
                self.click(page, "Pay Now")
            r = resp.value
        except Exception as e:
            if self.state.get("fake_redirect"):
                raise Abort("redirectUri is a FAKE gateway (site would mark it paid), blocked")
            raise Abort(f"request-payment failed: {str(e)[:200]}")
        if r.status == 200:
            # A payment transaction exists from here on: never release the seats after this point.
            self.reached_gateway = True
        if r.status != 200 or not self.state.get("redirect_uri"):
            raise Abort(f"request-payment -> {r.status}, redirectUri captured: "
                        f"{bool(self.state.get('redirect_uri'))}")
        uri = self.state["redirect_uri"]
        self.result["payment_url"] = sanitize_url(uri)
        if getattr(self.a, "save_url", True):
            os.makedirs(PRIVATE, mode=0o700, exist_ok=True)
            with open(os.path.join(PRIVATE, "redirect_uri.txt"), "w") as f:
                f.write(uri + "\n")
            os.chmod(os.path.join(PRIVATE, "redirect_uri.txt"), 0o600)
        self.emit("payment_requested", payment_url=self.result["payment_url"])
        page.wait_for_url(lambda u: "bus.rta.ae" not in urllib.parse.urlsplit(u).netloc, timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        page.wait_for_timeout(3000)
        self.result["gateway"] = self.describe_gateway(page)
        self.emit("payment_page", **self.result["gateway"])
        self.shot(page, "payment_page")

    def describe_gateway(self, page):
        """What the external page shows, read-only: no clicks, nothing typed."""
        info = page.evaluate("""() => {
          const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
          const txt = e => (e.innerText || e.value || e.getAttribute('aria-label') || e.alt || '').trim().slice(0, 80);
          return {
            title: document.title,
            buttons: [...document.querySelectorAll('button,[role=button],input[type=submit],a.btn,[role=tab],[role=radio]')]
                       .filter(vis).map(txt).filter(Boolean),
            fields: [...document.querySelectorAll('input,select,textarea')].filter(vis)
                       .map(e => e.name || e.id || e.placeholder || e.type),
            labels: [...document.querySelectorAll('label,h1,h2,h3,legend')].filter(vis).map(txt).filter(Boolean),
            images: [...document.querySelectorAll('img[alt]')].filter(vis).map(e => e.alt).filter(Boolean),
            iframes: [...document.querySelectorAll('iframe')].map(f => { try { return new URL(f.src).host } catch { return '' } }),
          };
        }""")
        info = redact_json(info)
        info["url"] = sanitize_url(page.url)
        info["host"] = urllib.parse.urlsplit(page.url).netloc
        return info

    def clean_context_check(self, browser):
        """Open the redirectUri in a brand new context: no cookies, storage or referrer."""
        ctx = browser.new_context(viewport={"width": 1280, "height": 1000}, locale="en-US",
                                  timezone_id="Asia/Dubai")
        page = ctx.new_page()
        chain = []
        page.on("framenavigated", lambda fr: chain.append(sanitize_url(fr.url))
                if fr == page.main_frame else None)
        try:
            resp = page.goto(self.state["redirect_uri"], wait_until="load", timeout=60000)
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            page.wait_for_timeout(3000)
            info = self.describe_gateway(page)
            info.update(status=resp.status if resp else None, chain=chain,
                        same_as_main=info["host"] == self.result["gateway"]["host"]
                        and info["title"] == self.result["gateway"]["title"])
            self.result["clean_context"] = info
            self.emit("clean_context", **info)
            self.shot(page, "payment_page_clean_context")
        finally:
            ctx.close()

    # ---------------------------------------------------------------- main
    def run(self):
        buyer = load_buyer() if self.a.stop_after in ("form", "payment") else None
        self.precheck()
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1280, "height": 1000}, locale="en-US",
                                      timezone_id="Asia/Dubai")
            page = ctx.new_page()
            page.add_init_script(SSE_HOOK)
            page.on("request", self.on_request)
            page.on("response", self.on_response)
            try:
                self.search_and_select(page)
                if self.a.stop_after == "search":
                    return
                self.check_cancel()
                self.hold(page)
                if self.a.stop_after == "seats":
                    self.release(page)
                    return
                self.check_cancel()
                self.guest_form(page, buyer)
                if self.a.stop_after == "form":
                    self.release(page)
                    return
                self.check_cancel()
                self.request_otp(page)
                self.verify_otp(page)
                self.check_cancel()
                data = self.confirm_reservation(page)
                self.pay_now(page, data)
                if getattr(self.a, "clean_check", True):
                    self.clean_context_check(browser)
                self.emit("done", note="stopped on the external payment page, nothing paid")
            except BaseException as e:
                self.result["error"] = RED(f"{type(e).__name__}: {e}")[:1000]
                self.emit("error", error=self.result["error"])
                try:
                    self.shot(page, "error")
                except Exception:
                    pass
                if not self.reached_gateway:
                    if self.confirmed:
                        self.emit("warning", note="reservation already confirmed, releasing seats anyway")
                    try:
                        self.release(page)
                    except Exception as e2:
                        self.emit("release_failed", error=str(e2)[:300])
                raise
            finally:
                self.dump()
                browser.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--from", dest="dep", choices=STATION_QUERY, required=True)
    ap.add_argument("--to", dest="dest", choices=STATION_QUERY, required=True)
    ap.add_argument("--date", type=dt.date.fromisoformat, required=True)
    ap.add_argument("--time", type=dt.time.fromisoformat, required=True, help="departure HH:MM")
    ap.add_argument("--pax", type=int, default=1)
    ap.add_argument("--method", default="", help="payment method name part (default: first non-Apple Pay)")
    ap.add_argument("--stop-after", choices=("search", "seats", "form", "payment"), default="payment")
    a = ap.parse_args()
    if a.dep == a.dest or not 1 <= a.pax <= 10:
        sys.exit("bad direction or --pax")
    try:
        Run(a).run()
    except Abort as e:
        event("aborted", reason=str(e))
        sys.exit(2)


if __name__ == "__main__":
    main()
