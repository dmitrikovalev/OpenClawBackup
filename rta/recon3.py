"""Step 3 reconnaissance: search trips Abu Dhabi Central -> Ibn Battuta.

Selects stations in the form, clicks "Search Trips" and records the site's
API calls and responses. Stops at the results page: does NOT select seats,
does NOT book and does NOT enter any personal data.
"""
import json
import os
import re

from playwright.sync_api import sync_playwright

URL = "https://bus.rta.ae/"
OUT = "out"
FROM_QUERY, FROM_HINT = "Abu Dhabi", re.compile(r"central|abu dhabi bus station", re.I)
TO_QUERY, TO_HINT = "Ibn Battuta", re.compile(r"ibn battuta", re.I)
os.makedirs(OUT, exist_ok=True)

api_calls = []


def on_response(resp):
    req = resp.request
    if req.resource_type not in ("xhr", "fetch"):
        return
    if "bus-api.rta.ae" not in resp.url:
        return
    entry = {"method": req.method, "url": resp.url, "status": resp.status}
    if req.post_data:
        entry["post_data"] = req.post_data[:2000]
    try:
        if "json" in (resp.headers.get("content-type") or ""):
            entry["body"] = resp.text()[:6000]
    except Exception:
        pass
    api_calls.append(entry)


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
    """Open the From/To ant-select, type the query, pick the option matching hint."""
    # Editable search inputs of the From/To selects (the hidden one is readonly).
    box = page.locator("input.ant-select-input:not([readonly])").nth(index)
    box.focus()
    box.press_sequentially(query, delay=60)
    page.wait_for_timeout(2500)
    if page.locator(".ant-select-item-option:visible").count() == 0:
        # Dropdown did not open on focus: force a click (sticky header may overlap).
        box.click(force=True)
        box.fill("")
        box.press_sequentially(query, delay=60)
        page.wait_for_timeout(2500)
    options = page.locator(".ant-select-item-option:visible")
    texts = [t.strip() for t in options.all_inner_texts()]
    print(f"Options for '{query}':", texts)
    target = next((i for i, t in enumerate(texts) if hint.search(t)), 0 if texts else None)
    if target is None:
        raise RuntimeError(f"No options found for {query}")
    options.nth(target).click()
    page.wait_for_timeout(1500)
    return texts[target]


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(
        viewport={"width": 1280, "height": 1000},
        locale="en-US",
        timezone_id="Asia/Dubai",
    )
    page.on("response", on_response)

    page.goto(URL, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(2000)
    close_tour(page)
    page.get_by_text("Start your booking as a guest").first.click(timeout=15000)
    page.wait_for_load_state("networkidle", timeout=60000)
    page.wait_for_timeout(2000)
    close_tour(page)

    chosen_from = choose_station(page, 0, FROM_QUERY, FROM_HINT)
    chosen_to = choose_station(page, 1, TO_QUERY, TO_HINT)
    print("Chosen:", chosen_from, "->", chosen_to)
    page.screenshot(path=f"{OUT}/03_form.png", full_page=True)

    page.get_by_text("Search Trips", exact=True).first.click(timeout=15000)
    page.wait_for_load_state("networkidle", timeout=60000)
    page.wait_for_timeout(4000)
    page.screenshot(path=f"{OUT}/03_results.png", full_page=True)
    with open(f"{OUT}/03_results.html", "w", encoding="utf-8") as f:
        f.write(page.content())

    with open(f"{OUT}/03_api.json", "w", encoding="utf-8") as f:
        json.dump(api_calls, f, ensure_ascii=False, indent=2)

    print("\n--- page text after search ---")
    print((page.inner_text("body") or "")[:2500])
    print("\n--- API calls ---")
    for c in api_calls:
        print(c["status"], c["method"], c["url"][:160])
        if c.get("post_data"):
            print("   POST:", c["post_data"][:300])
    browser.close()