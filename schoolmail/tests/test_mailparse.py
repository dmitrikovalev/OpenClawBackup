"""Offline tests: parsing and the sender filter on the sample .eml files (no IMAP, no network).

Run: python -m unittest discover -s tests -v
"""
import json
import os
import unittest

import mailparse

HERE = os.path.dirname(os.path.abspath(__file__))
ALLOWED_FROM = frozenset({"parent@example.com"})
SCHOOL_DOMAINS = frozenset({"greenfield.example"})


def raw(name):
    with open(os.path.join(HERE, name), "rb") as f:
        return f.read()


def kind(name):
    addr, dmarc_fail = mailparse.sender(raw(name))
    return mailparse.classify(addr, dmarc_fail, ALLOWED_FROM, SCHOOL_DOMAINS)


def parse(name):
    return mailparse.parse_message(raw(name), forwarder=kind(name) == "forward")


class FilterTest(unittest.TestCase):
    def test_sample_letters(self):
        expected = {
            "01_school_direct.eml": "school",
            "02_forward_gmail.eml": "forward",
            "03_forward_outlook.eml": "forward",
            "04_html_newsletter.eml": "school",          # subdomain mail.greenfield.example
            "05_attachment.eml": "school",
            "06_stranger.eml": None,                     # school address only in the display name
            "07_spoofed_forward.eml": None,              # From is the owner, but dmarc=fail
            "08_broken_encoding.eml": "school",
            "09_attachments_content.eml": "school",
        }
        self.assertEqual({name: kind(name) for name in expected}, expected)

    def test_domains_match_exactly_or_as_subdomain(self):
        def ok(addr):
            return mailparse.classify(addr, False, ALLOWED_FROM, SCHOOL_DOMAINS)
        self.assertEqual(ok("a@greenfield.example"), "school")
        self.assertEqual(ok("a@x.y.greenfield.example"), "school")
        self.assertIsNone(ok("a@evilgreenfield.example"))
        self.assertIsNone(ok("a@greenfield.example.evil.example"))
        self.assertIsNone(ok("parent@example.com.evil.example"))
        self.assertIsNone(ok(""))

    def test_address_never_comes_from_the_display_name(self):
        for header, addr in [
            ("school.office@greenfield.example <attacker@evil.example>", "attacker@evil.example"),
            ('"office@greenfield.example" <deals@shop.example>', "deals@shop.example"),
            ("=?utf-8?Q?Office,_Greenfield?= <office@greenfield.example>", "office@greenfield.example"),
            ("a@greenfield.example, b@evil.example", ""),
            ("Иван Петров <Ivan@Greenfield.Example>", "ivan@greenfield.example"),
        ]:
            self.assertEqual(mailparse.address(header)[1], addr, header)


class ParseTest(unittest.TestCase):
    def test_direct_letter(self):
        m = parse("01_school_direct.eml")
        self.assertEqual(m["from"], "Ms. Karen Smith <k.smith@greenfield.example>")
        self.assertEqual(m["original_from"], m["from"])
        self.assertFalse(m["forwarded"])
        self.assertEqual(m["subject"], "Year 3 – library books due Thursday")
        self.assertEqual(m["date"], "2026-10-27T09:30+04:00")             # 05:30 UTC -> Dubai
        self.assertEqual(m["links"], ["https://greenfield.example/reading-log",
                                      "https://greenfield.example/library/rules"])
        self.assertIn("available at [ссылка 1].", m["text"])
        self.assertIn("(Late books: see [ссылка 2])", m["text"])
        for ch in "‌‮ ":
            self.assertNotIn(ch, m["text"])

    def test_gmail_forward(self):
        m = parse("02_forward_gmail.eml")
        self.assertTrue(m["forwarded"])
        self.assertEqual(m["from"], "Parent Name <parent@example.com>")
        self.assertEqual(m["original_from"], "Greenfield School Office <office@greenfield.example>")
        self.assertEqual(m["original_date"], "2026-10-26T09:15+04:00")
        self.assertEqual(m["original_subject"], "Parent-teacher meeting on 5 November")
        self.assertEqual(m["subject"], "Fwd: Parent-teacher meeting on 5 November")
        self.assertEqual(m["links"], ["https://forms.example/ptm-booking"])  # Google redirect unwrapped
        self.assertIn("this form [ссылка 1]", m["text"])
        self.assertIn("Пересылаю письмо из школы.", m["text"])
        self.assertIn("14:00–18:00", m["text"])
        self.assertNotIn("track.example", json.dumps(m))                     # tracking pixel

    def test_outlook_forward(self):
        m = parse("03_forward_outlook.eml")
        self.assertTrue(m["forwarded"])
        self.assertEqual(m["original_from"], "Greenfield Transport <transport@greenfield.example>")
        self.assertEqual(m["original_date"], "2026-10-27T07:40+04:00")
        self.assertEqual(m["original_subject"], "Bus route change from 1 November")
        self.assertEqual(m["links"], ["https://greenfield.example/transport/route-12.pdf"])
        self.assertIn("The new route map: route 12 [ссылка 1].", m["text"])
        self.assertNotIn("margin-top", m["text"])

    def test_html_newsletter(self):
        m = parse("04_html_newsletter.eml")
        text = m["text"]
        self.assertEqual(m["links"], ["https://pay.greenfield.example/trip?id=42",   # safelinks unwrapped
                                      "https://greenfield.example/calendar",
                                      "https://evil.example/login"])
        self.assertIn("Sports Day is on Thursday, 12 November.", text)   # inside a font-size:0 cell
        self.assertIn("Pay now [ссылка 1]", text)
        self.assertIn("Full calendar: [ссылка 2]", text)
        self.assertIn("calendar [ссылка 2]", text)
        self.assertIn("Lookalike: [ссылка 3]", text)       # the fake visible URL is not shown
        self.assertNotIn("greenfield.example/login", text)
        self.assertIn("Day | Club\nMonday | Chess\nWednesday | Robotics", text)
        self.assertIn("Questions: office@greenfield.example", text)
        self.assertIn("Click me", text)
        for bad in ("ignore previous", "attacker", "Hidden div", "evil.example/run", "alert",
                    "document.write", ".btn", "Greenfield Mailer", "pixel.gif", "<", ">"):
            self.assertNotIn(bad, text)

    def test_attachments_are_metadata_only(self):
        m = parse("05_attachment.eml")
        self.assertEqual(m["attachments"], [
            {"name": "Расписание ноябрь.pdf", "type": "application/pdf", "size": 1024},
            {"name": "Меню обедов.docx",
             "type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             "size": 2048},
        ])                                                   # the inline logo is not listed
        self.assertIn("please find attached the November timetable", m["text"])
        self.assertNotIn("logo", m["text"].lower())
        self.assertNotIn("JVBER", json.dumps(m))

    def test_broken_encodings_do_not_crash(self):
        m = parse("08_broken_encoding.eml")
        self.assertEqual(m["from"], "Teacher <teacher@greenfield.example>")
        self.assertIn("Объявление", m["subject"])
        self.assertIn("Привет", m["subject"])
        self.assertIsNone(m["date"])
        self.assertIn("Field trip on Friday", m["text"])
        self.assertIn("Bring a hat", m["text"])
        self.assertNotIn("\x00", m["text"])
        self.assertEqual(len(m["attachments"]), 1)
        self.assertEqual(m["attachments"][0]["type"], "application/pdf")

    def test_every_sample_is_json_safe(self):
        for name in sorted(os.listdir(HERE)):
            if name.endswith(".eml"):
                m = parse(name)
                json.dumps(m, ensure_ascii=False).encode("utf-8")      # no lone surrogates
                self.assertLessEqual(len(m["text"]), mailparse.TEXT_LIMIT + 2)

    def test_garbage_input(self):
        for data in (b"", b"\xff\xfe\x00garbage", b"Content-Type: multipart/mixed\n\n--x\n",
                     b"Content-Type: text/html\n\n" + b"<" * 50000 + b"<![bogus[" + b"<div>" * 3000):
            m = mailparse.parse_message(data)
            json.dumps(m, ensure_ascii=False).encode("utf-8")

    def test_long_text_is_cut(self):
        body = ("Line with https://greenfield.example/x\n" * 400).encode()
        m = mailparse.parse_message(b"From: a@greenfield.example\nSubject: long\n\n" + body)
        self.assertTrue(m["truncated"])
        self.assertLessEqual(len(m["text"]), mailparse.TEXT_LIMIT + 2)
        self.assertFalse(m["text"].rstrip(" …").endswith("[ссылка"))
        self.assertEqual(m["links"], ["https://greenfield.example/x"])

    def test_forward_as_attachment(self):
        inner = raw("01_school_direct.eml")
        outer = (b"From: Parent Name <parent@example.com>\nSubject: Fwd: books\n"
                 b"Date: Tue, 27 Oct 2026 10:00:00 +0400\nMIME-Version: 1.0\n"
                 b'Content-Type: multipart/mixed; boundary="B"\n\n--B\n'
                 b"Content-Type: text/plain\n\nSee below\n--B\nContent-Type: message/rfc822\n\n"
                 + inner + b"\n--B--\n")
        m = mailparse.parse_message(outer, forwarder=True)
        self.assertTrue(m["forwarded"])
        self.assertEqual(m["original_from"], "Ms. Karen Smith <k.smith@greenfield.example>")
        self.assertEqual(m["original_date"], "2026-10-27T09:30+04:00")
        self.assertEqual(m["original_subject"], "Year 3 – library books due Thursday")
        self.assertIn("See below", m["text"])
        self.assertIn("return all library books", m["text"])
        self.assertEqual(m["attachments"], [])

    def test_reply_is_not_a_forward(self):
        body = (b"Thanks!\n\n________________________________\nFrom: Parent <parent@example.com>\n"
                b"Sent: Monday, October 26, 2026 9:00 AM\nSubject: question\n\nHello\n")
        m = mailparse.parse_message(b"From: t@greenfield.example\nSubject: RE: question\n\n" + body)
        self.assertFalse(m["forwarded"])
        self.assertEqual(m["original_from"], "t@greenfield.example")

    def test_forward_dates(self):
        cases = {
            "Mon, Oct 26, 2026 at 9:15 AM": "2026-10-26T09:15+04:00",
            "Mon, 26 Oct 2026 at 21:05": "2026-10-26T21:05+04:00",
            "Tuesday, October 27, 2026 12:10 PM": "2026-10-27T12:10+04:00",
            "пн, 26 окт. 2026 г. в 09:15": "2026-10-26T09:15+04:00",
            "понедельник, 26 октября 2026 г. 9:15": "2026-10-26T09:15+04:00",
            "26.10.2026 9:15": "2026-10-26T09:15+04:00",
            "Mon, 26 Oct 2026 05:15:00 +0000": "2026-10-26T09:15+04:00",
            "26 October 2026": "2026-10-26",
            "someday": "someday",
        }
        for text, expected in cases.items():
            self.assertEqual(mailparse.forward_date(text), expected, text)


if __name__ == "__main__":
    unittest.main()
