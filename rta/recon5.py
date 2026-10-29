"""Step 5 reconnaissance: the "Select Date & Time" and "Passengers" controls of step 1.

Read-only: sets values in the pickers and records their markup. Does not
search, does not hold seats and does not type personal data.

Usage: python recon5.py --date 2026-10-28 --time 07:30 --pax 2
"""
import argparse
import datetime as dt
import json
import os

from playwright.sync_api import sync_playwright

from recon4 import close_tour

URL = "https://bus.rta.ae/"
OUT = "out"
os.makedirs(OUT, exist_ok=True)

MODAL = "div.fixed.inset-0"

DUMP = """(sel) => {
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  return [...document.querySelectorAll(sel)].filter(vis).map(m => ({
    text: m.innerText.slice(0, 2000),
    controls: [...m.querySelectorAll('button,input,[role]')].filter(vis).map(e => ({
      tag: e.tagName.toLowerCase(), role: e.getAttribute('role'), aria: e.getAttribute('aria-label'),
      pressed: e.getAttribute('aria-pressed'), now: e.getAttribute('aria-valuenow'),
      min: e.getAttribute('aria-valuemin'), max: e.getAttribute('aria-valuemax'),
      disabled: !!e.disabled, text: (e.innerText || e.value || '').trim().slice(0, 60)}))}));
}"""


def snap(page, name, result):
    result[name] = page.evaluate(DUMP, MODAL)
    page.screenshot(path=f"{OUT}/05_{name}.png")
    print(f"== {name}:", json.dumps(result[name], ensure_ascii=False)[:1500])


def set_dial(page, label, target, log):
    dial = page.locator(f"{MODAL} [role=slider][aria-label='{label}']").first
    dial.focus()
    for _ in range(80):
        now = int(dial.get_attribute("aria-valuenow"))
        log.append((label, now))
        if now == target:
            return True
        dial.press("ArrowUp" if now < target else "ArrowDown")
        page.wait_for_timeout(120)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", type=dt.date.fromisoformat, required=True)
    ap.add_argument("--time", type=dt.time.fromisoformat, required=True)
    ap.add_argument("--pax", type=int, default=2)
    a = ap.parse_args()
    result, log = {}, []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1000}, locale="en-US",
                                timezone_id="Asia/Dubai")
        page.goto(URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)
        close_tour(page)
        page.get_by_text("Start your booking as a guest").first.click(timeout=15000)
        page.wait_for_load_state("networkidle", timeout=60000)
        page.wait_for_timeout(2000)
        close_tour(page)

        page.get_by_role("button").filter(has_text="Select Date & Time").first.click(timeout=10000)
        page.wait_for_timeout(1000)
        modal = page.locator(MODAL).first
        month = a.date.strftime("%b %Y")
        for _ in range(12):
            if modal.get_by_text(month, exact=True).count():
                break
            modal.get_by_role("button", name="Next", exact=True).first.click()
            page.wait_for_timeout(500)
        # Day buttons of the visible month (the panel with aria-hidden=false).
        panel = modal.locator("[aria-hidden=false]").first
        panel.get_by_role("button", name=str(a.date.day), exact=True).locator(
            "visible=true").last.click()
        page.wait_for_timeout(800)
        snap(page, "date_picked", result)
        # Picking a day switches to the time panel by itself.
        if not page.locator(f"{MODAL} [role=slider]").locator("visible=true").count():
            modal.locator("button").filter(has_text="Next").last.click()
            page.wait_for_timeout(1000)
        snap(page, "time_panel", result)
        result["hour_ok"] = set_dial(page, "Hour dial", a.time.hour, log)
        snap(page, "hour_set", result)
        modal.get_by_role("button", name="Select minutes").click()
        page.wait_for_timeout(800)
        snap(page, "minutes_panel", result)
        dial_labels = page.eval_on_selector_all(f"{MODAL} [role=slider]",
                                                "els => els.map(e => e.getAttribute('aria-label'))")
        print("sliders:", dial_labels)
        mlabel = next((l for l in dial_labels if l and "inute" in l), None)
        if mlabel:
            result["minute_ok"] = set_dial(page, mlabel, a.time.minute, log)
        snap(page, "minutes_set", result)
        result["dial_log"] = log
        with open(f"{OUT}/05_time.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        # Footer: confirm the picker (button names recorded above).
        footer = [b.strip() for b in modal.locator("button").all_inner_texts() if b.strip()]
        print("modal buttons:", footer)
        # The footer button shows the picked value ("28 Oct · 07:30") and confirms it.
        modal.locator("button").filter(has_text="·").last.click()
        page.wait_for_timeout(1000)
        result["date_button_after"] = page.get_by_role("button").filter(
            has_text="Select Date & Time").first.inner_text()
        print("date button after:", result["date_button_after"])

        page.get_by_role("button").filter(has_text="Passengers").first.click(timeout=10000)
        page.wait_for_timeout(1000)
        snap(page, "pax", result)
        add = page.locator(f"{MODAL} button").filter(has_text="+")
        result["pax_add_buttons"] = add.count()
        with open(f"{OUT}/05_pax.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        with open(f"{OUT}/05_pickers.json", "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        browser.close()


if __name__ == "__main__":
    main()
