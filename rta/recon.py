"""Reconnaissance of the RTA intercity bus booking page.

Opens the page, saves a full-page screenshot, the HTML and a list of
form fields. Does NOT fill or submit anything.
"""
import json
import os

from playwright.sync_api import sync_playwright

URL = "https://bustickets.rta.ae/"
OUT = "out"
os.makedirs(OUT, exist_ok=True)

FIELDS_JS = """() => ({
  title: document.title,
  url: location.href,
  selects: [...document.querySelectorAll('select')].map(s => ({
    id: s.id, name: s.name,
    options: [...s.options].map(o => ({value: o.value, text: o.text.trim()}))
  })),
  inputs: [...document.querySelectorAll('input, textarea')].map(i => ({
    id: i.id, name: i.name, type: i.type, placeholder: i.placeholder
  })),
  buttons: [...document.querySelectorAll('button, input[type=submit], a.btn')]
    .map(b => ({id: b.id, text: (b.innerText || b.value || '').trim()}))
    .filter(b => b.text),
  iframes: [...document.querySelectorAll('iframe')].map(f => f.src)
})"""

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        timezone_id="Asia/Dubai",
    )
    try:
        resp = page.goto(URL, wait_until="networkidle", timeout=60000)
        print("HTTP status:", resp.status if resp else "no response")
    except Exception as e:
        print("Navigation problem:", e)

    page.wait_for_timeout(3000)
    page.screenshot(path=f"{OUT}/01_start.png", full_page=True)
    with open(f"{OUT}/01_start.html", "w", encoding="utf-8") as f:
        f.write(page.content())

    info = page.evaluate(FIELDS_JS)
    with open(f"{OUT}/01_fields.json", "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)

    print(json.dumps(info, ensure_ascii=False, indent=2)[:6000])
    browser.close()
