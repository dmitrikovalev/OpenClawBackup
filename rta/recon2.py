"""Step 2 reconnaissance: open the guest booking flow on bus.rta.ae.

Clicks "Start your booking as a guest", then saves a screenshot, the HTML,
the visible form controls and the site's own API calls (XHR/fetch).
Does NOT fill in or submit anything.
"""
import json
import os

from playwright.sync_api import sync_playwright

URL = "https://bus.rta.ae/"
OUT = "out"
os.makedirs(OUT, exist_ok=True)

api_calls = []


def on_response(resp):
    req = resp.request
    if req.resource_type not in ("xhr", "fetch"):
        return
    entry = {"method": req.method, "url": resp.url, "status": resp.status}
    if req.method == "POST" and req.post_data:
        entry["post_data"] = req.post_data[:500]
    try:
        if "json" in (resp.headers.get("content-type") or ""):
            entry["body"] = resp.text()[:1500]
    except Exception:
        pass
    api_calls.append(entry)


CONTROLS_JS = """() => {
  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const pick = sel => [...document.querySelectorAll(sel)].filter(vis);
  return {
    url: location.href,
    selects: pick('select').map(s => ({
      id: s.id, name: s.name,
      options: [...s.options].map(o => ({value: o.value, text: o.text.trim()}))
    })),
    inputs: pick('input, textarea').map(i => ({
      id: i.id, name: i.name, type: i.type,
      placeholder: i.placeholder, value: i.type === 'password' ? '' : i.value
    })),
    comboboxes: pick('[role=combobox], [role=listbox], .dropdown, .select2, .ng-select')
      .map(c => ({tag: c.tagName, id: c.id, cls: c.className.toString().slice(0, 80),
                  text: (c.innerText || '').trim().slice(0, 200)})),
    labels: pick('label').map(l => (l.innerText || '').trim()).filter(Boolean),
    buttons: pick('button, input[type=submit], a.btn, [role=button]')
      .map(b => (b.innerText || b.value || '').trim()).filter(Boolean),
    text: (document.body.innerText || '').slice(0, 3000)
  };
}"""


def close_tour(page):
    for label in ("End tour", "×", "Done"):
        try:
            btn = page.get_by_text(label, exact=True).first
            if btn.is_visible():
                btn.click(timeout=2000)
                page.wait_for_timeout(500)
        except Exception:
            pass


def dump(page, name):
    page.screenshot(path=f"{OUT}/{name}.png", full_page=True)
    with open(f"{OUT}/{name}.html", "w", encoding="utf-8") as f:
        f.write(page.content())
    info = page.evaluate(CONTROLS_JS)
    with open(f"{OUT}/{name}_controls.json", "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    return info


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        timezone_id="Asia/Dubai",
    )
    page.on("response", on_response)

    page.goto(URL, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(2000)
    close_tour(page)

    page.get_by_text("Start your booking as a guest").first.click(timeout=15000)
    page.wait_for_load_state("networkidle", timeout=60000)
    page.wait_for_timeout(3000)
    close_tour(page)

    info = dump(page, "02_guest")

    with open(f"{OUT}/02_api.json", "w", encoding="utf-8") as f:
        json.dump(api_calls, f, ensure_ascii=False, indent=2)

    summary = {k: v for k, v in info.items() if k != "text"}
    print(json.dumps(summary, ensure_ascii=False, indent=2)[:5000])
    print("\n--- page text (start) ---\n", info["text"][:1500])
    print("\n--- API calls ---")
    for c in api_calls:
        print(c["status"], c["method"], c["url"][:150])
    browser.close()
