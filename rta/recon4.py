"""Step 4 reconnaissance: guest booking steps 2 (seats) and 3 (payment page).

Nearest trip Abu Dhabi Central -> Ibn Battuta, 1 passenger, "Auto Select Seats",
then continue to the contact / payment form and STOP there.

Safety:
- never types into any field after the station search (no personal data);
- only clicks buttons whose label is in SAFE_CLICKS, never submit/pay/confirm;
- at the end tries to release held seats (goes back to step 1 / clears the
  search) and closes the browser.

Records every request to bus-api.rta.ae (with POST bodies), SignalR traffic
(WebSocket frames, SSE/long polling via HTTP) and other hosts contacted.

Usage:
  python recon4.py --until seats     # stop after Auto Select Seats
  python recon4.py --until contact   # also continue to step 3 (contact/payment)
  python recon4.py --until guest     # step 3 + choose "Guest Checkout", show the form
"""
import argparse
import json
import os
import re
import time

from playwright.sync_api import sync_playwright

URL = "https://bus.rta.ae/"
OUT = "out"
FROM_QUERY, FROM_HINT = "Abu Dhabi", re.compile(r"central|abu dhabi bus station", re.I)
TO_QUERY, TO_HINT = "Ibn Battuta", re.compile(r"ibn battuta", re.I)
# Buttons that move between steps without submitting personal data or paying.
SAFE_CLICKS = ("Auto Select Seats", "Continue to Payment", "Proceed to Payment",
               "Continue", "Next", "Proceed")
FORBIDDEN = re.compile(r"pay now|confirm|submit|send otp|verify|book now|checkout", re.I)
os.makedirs(OUT, exist_ok=True)

T0 = time.time()
api_calls = []
ws_frames = []
other_hosts = {}
navigations = []


def ts():
    return round(time.time() - T0, 2)


def on_request(req):
    host = req.url.split("/")[2] if "://" in req.url else req.url
    if "bus-api.rta.ae" not in host:
        other_hosts[host] = other_hosts.get(host, 0) + 1


def on_response(resp):
    req = resp.request
    if "bus-api.rta.ae" not in resp.url:
        return
    entry = {"t": ts(), "method": req.method, "url": resp.url, "status": resp.status,
             "type": req.resource_type}
    if req.post_data:
        entry["post_data"] = req.post_data[:5000]
    hdrs = req.headers
    entry["req_headers"] = {k: (v if k.lower() != "authorization" else v[:25] + "...")
                            for k, v in hdrs.items()
                            if k.lower() not in ("user-agent", "accept-encoding", "sec-ch-ua",
                                                 "sec-ch-ua-mobile", "sec-ch-ua-platform")}
    try:
        ct = resp.headers.get("content-type") or ""
        if "json" in ct or "text" in ct:
            entry["body"] = resp.text()[:20000]
    except Exception:
        pass
    api_calls.append(entry)


def on_websocket(ws):
    ws_frames.append({"t": ts(), "event": "open", "url": ws.url})
    ws.on("framesent", lambda p: ws_frames.append({"t": ts(), "dir": "sent", "data": str(p)[:5000]}))
    ws.on("framereceived", lambda p: ws_frames.append({"t": ts(), "dir": "recv", "data": str(p)[:5000]}))
    ws.on("close", lambda _: ws_frames.append({"t": ts(), "event": "close", "url": ws.url}))


# SignalR uses Server-Sent Events here; Playwright can't read the stream body,
# so wrap EventSource in the page and collect messages in window.__sse.
SSE_HOOK = """
(() => {
  const Orig = window.EventSource;
  if (!Orig) return;
  window.__sse = [];
  window.EventSource = function (url, cfg) {
    const es = new Orig(url, cfg);
    window.__sse.push({t: Date.now(), event: 'open', url: String(url)});
    es.addEventListener('message', ev =>
      window.__sse.push({t: Date.now(), dir: 'recv', data: String(ev.data).slice(0, 5000)}));
    return es;
  };
  window.EventSource.prototype = Orig.prototype;
  Object.assign(window.EventSource, {CONNECTING: 0, OPEN: 1, CLOSED: 2});
})();
"""


def collect_sse(page):
    try:
        for m in page.evaluate("window.__sse ? window.__sse.splice(0) : []"):
            m["src"] = "sse"
            ws_frames.append(m)
    except Exception:
        pass


def close_tour(page):
    for label in ("End tour", "Done"):
        try:
            btn = page.get_by_text(label, exact=True).first
            if btn.is_visible():
                btn.click(timeout=2000)
                page.wait_for_timeout(500)
        except Exception:
            pass


def choose_station(page, index, query, hint):
    box = page.locator("input.ant-select-input:not([readonly])").nth(index)
    box.focus()
    box.press_sequentially(query, delay=60)
    page.wait_for_timeout(2500)
    if page.locator(".ant-select-item-option:visible").count() == 0:
        box.click(force=True)
        box.fill("")
        box.press_sequentially(query, delay=60)
        page.wait_for_timeout(2500)
    options = page.locator(".ant-select-item-option:visible")
    texts = [t.strip() for t in options.all_inner_texts()]
    target = next((i for i, t in enumerate(texts) if hint.search(t)), 0 if texts else None)
    if target is None:
        raise RuntimeError(f"No options found for {query}")
    options.nth(target).click()
    page.wait_for_timeout(1500)
    return texts[target]


def describe(page, name):
    """Screenshot + html + visible controls (buttons, inputs, labels, iframes)."""
    collect_sse(page)
    page.screenshot(path=f"{OUT}/04_{name}.png", full_page=True)
    with open(f"{OUT}/04_{name}.html", "w", encoding="utf-8") as f:
        f.write(page.content())
    controls = page.evaluate("""() => {
      const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
      const pick = e => ({tag: e.tagName.toLowerCase(), type: e.type || null,
        name: e.name || null, id: e.id || null, testid: e.dataset ? e.dataset.testid || null : null,
        placeholder: e.placeholder || null, aria: e.getAttribute('aria-label'),
        required: e.required || e.getAttribute('aria-required') === 'true',
        disabled: !!e.disabled, text: (e.innerText || e.value || '').trim().slice(0, 120)});
      return {
        url: location.href,
        buttons: [...document.querySelectorAll('button,a[role=button],[role=button]')].filter(vis).map(pick),
        inputs: [...document.querySelectorAll('input,select,textarea')].filter(vis).map(pick),
        labels: [...document.querySelectorAll('label,.ant-form-item-label')].filter(vis)
                  .map(e => e.innerText.trim()).filter(Boolean),
        iframes: [...document.querySelectorAll('iframe')].map(f => f.src),
        dialogs: [...document.querySelectorAll('[role=dialog],[role=alertdialog]')].filter(vis)
                  .map(e => e.innerText.trim().slice(0, 1000)),
        localStorage: Object.keys(localStorage),
        sessionStorage: Object.fromEntries(Object.keys(sessionStorage)
                  .map(k => [k, sessionStorage.getItem(k).slice(0, 500)])),
      };
    }""")
    controls["text"] = (page.inner_text("main") if page.locator("main").count()
                        else page.inner_text("body"))[-6000:]
    with open(f"{OUT}/04_{name}_controls.json", "w", encoding="utf-8") as f:
        json.dump(controls, f, ensure_ascii=False, indent=2)
    print(f"\n=== [{ts()}s] {name}: {controls['url']}")
    print("buttons:", [b["text"] or b["aria"] or b["testid"] for b in controls["buttons"]][-25:])
    print("inputs:", [(i["type"], i["name"] or i["id"] or i["placeholder"]) for i in controls["inputs"]])
    if controls["dialogs"]:
        print("dialogs:", controls["dialogs"])
    print("sessionStorage:", controls["sessionStorage"])
    if controls["iframes"]:
        print("iframes:", controls["iframes"])
    return controls


def safe_click(page, label):
    assert label in SAFE_CLICKS and not FORBIDDEN.search(label), label
    dlg = page.locator("[role=alertdialog],[role=dialog]").locator("visible=true")
    scope = dlg.first if dlg.count() else page
    btn = scope.get_by_role("button", name=label, exact=True)
    if btn.count() == 0:
        btn = page.get_by_text(label, exact=True)
    btn = btn.locator("visible=true").first
    btn.scroll_into_view_if_needed(timeout=5000)
    btn.click(timeout=15000)
    page.wait_for_load_state("networkidle", timeout=60000)
    page.wait_for_timeout(4000)


def try_release(page):
    """Cancel the seat dialog / go back to step 1 so the site releases held seats."""
    def dialog_button(name):
        dlg = page.locator("[role=alertdialog],[role=dialog]").locator("visible=true")
        if not dlg.count():
            return None
        b = dlg.first.get_by_role("button", name=name, exact=True)
        return b.first if b.count() else None

    # Step 2: "Auto Select Seats" dialog -> Cancel.
    b = dialog_button("Cancel")
    if b:
        b.click(timeout=5000)
        page.wait_for_timeout(4000)
    # Step 3: Back -> "You will lose all progress ... release your seats" -> Continue.
    back = page.locator("#step-content-3").get_by_role("button", name="Back", exact=True)
    if back.count() and back.first.is_visible():
        back.first.click(timeout=5000)
        try:
            page.get_by_text("You will lose all progress").wait_for(timeout=10000)
        except Exception:
            pass
        b = dialog_button("Continue")
        if b:
            b.click(timeout=5000)
            page.wait_for_timeout(4000)
            return
    for testid in ("button-step-header-1", "button-clear-search-collapsed-0"):
        try:
            el = page.get_by_test_id(testid)
            if el.count() and el.first.is_visible():
                el.first.click(timeout=5000)
                page.wait_for_timeout(3000)
                close_tour(page)
                # A "discard selection?" dialog may appear: accept it.
                for ok in ("Yes", "OK", "Confirm", "Discard"):
                    b = page.get_by_role("button", name=ok, exact=True)
                    if b.count() and b.first.is_visible():
                        b.first.click(timeout=3000)
                        page.wait_for_timeout(3000)
                        break
        except Exception as e:
            print("release step failed:", testid, e)


def dump():
    with open(f"{OUT}/04_api.json", "w", encoding="utf-8") as f:
        json.dump(api_calls, f, ensure_ascii=False, indent=2)
    with open(f"{OUT}/04_ws.json", "w", encoding="utf-8") as f:
        json.dump(ws_frames, f, ensure_ascii=False, indent=2)
    with open(f"{OUT}/04_misc.json", "w", encoding="utf-8") as f:
        json.dump({"other_hosts": other_hosts, "navigations": navigations}, f, indent=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--until", choices=("seats", "contact", "guest"), default="seats")
    ap.add_argument("--next", default="Continue to Payment",
                    help="label of the step-2 button that opens step 3")
    ap.add_argument("--observe", type=int, default=0,
                    help="seconds to stay on the last page watching SignalR before release")
    a = ap.parse_args()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1000},
                                locale="en-US", timezone_id="Asia/Dubai")
        page.on("request", on_request)
        page.on("response", on_response)
        page.on("websocket", on_websocket)
        page.add_init_script(SSE_HOOK)
        page.on("framenavigated", lambda fr: navigations.append({"t": ts(), "url": fr.url})
                if fr == page.main_frame else None)
        try:
            page.goto(URL, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(2000)
            close_tour(page)
            page.get_by_text("Start your booking as a guest").first.click(timeout=15000)
            page.wait_for_load_state("networkidle", timeout=60000)
            page.wait_for_timeout(2000)
            close_tour(page)
            choose_station(page, 0, FROM_QUERY, FROM_HINT)
            choose_station(page, 1, TO_QUERY, TO_HINT)
            page.get_by_text("Search Trips", exact=True).first.click(timeout=15000)
            page.wait_for_load_state("networkidle", timeout=60000)
            page.wait_for_timeout(4000)
            close_tour(page)
            describe(page, "1_trip_selected")
            mark = len(api_calls)

            safe_click(page, "Auto Select Seats")
            close_tour(page)
            describe(page, "2_seats")
            print("API after Auto Select Seats:")
            for c in api_calls[mark:]:
                print("  ", c["status"], c["method"], c["url"][:150], (c.get("post_data") or "")[:200])

            if a.until in ("contact", "guest"):
                mark = len(api_calls)
                safe_click(page, a.next)
                close_tour(page)
                describe(page, "3_contact")
                print("API after", a.next)
                for c in api_calls[mark:]:
                    print("  ", c["status"], c["method"], c["url"][:150], (c.get("post_data") or "")[:200])

            if a.until == "guest":
                # Radio-like card, only switches the checkout method (no data sent).
                mark = len(api_calls)
                page.get_by_test_id("button-guest-checkout").click(timeout=10000)
                page.wait_for_timeout(3000)
                describe(page, "3b_guest_form")
                print("API after Guest Checkout:")
                for c in api_calls[mark:]:
                    print("  ", c["status"], c["method"], c["url"][:150], (c.get("post_data") or "")[:200])

            if a.observe:
                page.wait_for_timeout(a.observe * 1000)
                describe(page, "4_after_wait")
        finally:
            mark = len(api_calls)
            try:
                try_release(page)
                describe(page, "9_released")
            except Exception as e:
                print("release failed:", e)
            print("API during release:")
            for c in api_calls[mark:]:
                print("  ", c["status"], c["method"], c["url"][:150], (c.get("post_data") or "")[:200])
            dump()
            browser.close()
    print("\nws frames:", len(ws_frames), "| other hosts:", other_hosts)


if __name__ == "__main__":
    main()
