"""Offline tests: text from attachments. PDF needs pypdf, so run inside the image:
  docker run --rm --network none -v "$PWD/tests:/app/tests:ro" schoolmail:latest python -m unittest discover -s tests
"""
import email
import importlib.util
import io
import os
import unittest
import zipfile

import attachtext
import mailparse

HERE = os.path.dirname(os.path.abspath(__file__))
HAS_PYPDF = importlib.util.find_spec("pypdf") is not None
needs_pypdf = unittest.skipUnless(HAS_PYPDF, "pypdf is installed only in the image")


def raw09():
    with open(os.path.join(HERE, "09_attachments_content.eml"), "rb") as f:
        return f.read()


def letter():
    m = mailparse.parse_message(raw09(), read_attachment=attachtext.read)
    return m, {a["name"]: a for a in m["attachments"]}


def sports_pdf():
    msg = email.message_from_bytes(raw09())
    return next(p.get_payload(decode=True) for p in msg.walk() if p.get_filename() == "sports-day.pdf")


def zipped(files, method=zipfile.ZIP_DEFLATED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", method) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def with_attachments(*parts, truncated=False):
    raw = b'From: a@greenfield.example\nSubject: x\nMIME-Version: 1.0\nContent-Type: multipart/mixed; boundary="B"\n\n'
    raw += b"--B\nContent-Type: text/plain\n\nSee attached.\n"
    for name, ctype, data in parts:
        raw += (f'--B\nContent-Type: {ctype}\nContent-Disposition: attachment; filename="{name}"\n'
                f"Content-Transfer-Encoding: base64\n\n").encode() + email.base64mime.body_encode(data).encode()
    raw += b"--B--\n"
    return mailparse.parse_message(raw, read_attachment=attachtext.read, truncated=truncated)


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def docx(body_xml, extra=None):
    return zipped({"word/document.xml": f'<?xml version="1.0"?><w:document {W}><w:body>{body_xml}</w:body></w:document>',
                   **(extra or {})})


class KindTest(unittest.TestCase):
    def test_kind(self):
        self.assertEqual(attachtext.kind("application/pdf", "x"), "pdf")
        self.assertEqual(attachtext.kind("application/octet-stream", "Timetable.PDF"), "pdf")
        self.assertEqual(attachtext.kind("application/octet-stream", "clubs.xlsx"), "xlsx")
        self.assertEqual(attachtext.kind("text/calendar", None), "ics")
        self.assertEqual(attachtext.kind("image/jpeg", "p.jpg"), "image")
        self.assertIsNone(attachtext.kind("application/zip", "a.zip"))
        self.assertIsNone(attachtext.kind("application/msword", "old.doc"))
        self.assertIsNone(attachtext.kind("application/vnd.ms-word.document.macroEnabled.12", "a.docm"))
        self.assertIsNone(attachtext.kind("application/octet-stream", "budget.xlsm"))


class LetterTest(unittest.TestCase):
    def test_fields_office_calendar_image_and_limit(self):
        m, att = letter()
        for a in m["attachments"]:
            self.assertTrue({"name", "type", "size", "extracted", "truncated", "reason"} <= set(a), a["name"])
            self.assertEqual(a["reason"] is None, a["extracted"], a["name"])
        d = att["consent.docx"]
        self.assertTrue(d["extracted"])
        self.assertIn("Parental consent form: museum trip", d["text"])
        self.assertIn("Child name | Class", d["text"])
        self.assertIn("Return the signed form by 30 October.", d["text"])
        self.assertEqual(att["clubs.xlsx"]["text"], "Day | Club | Fee\nMonday | Chess | 200\nWednesday | Robotics | 350")
        self.assertEqual(att["slides.pptx"]["text"],
                         "[Слайд 1] Sports Day / Bring water and a hat\n[Слайд 2] Parents welcome from 10:00")
        self.assertEqual(att["sports-day.ics"]["events"], [{
            "summary": "Sports Day, Year 5", "start": "2026-11-12T09:00+04:00",
            "end": "2026-11-12T13:00+04:00", "location": "Main field",
            "description": "Children in PE kit.\nParents welcome."}])
        self.assertEqual((att["photo.png"]["extracted"], att["photo.png"]["reason"]), (False, "scan"))
        self.assertEqual(att["extra.pdf"]["reason"], "limit")              # 6th readable file
        for a in m["attachments"]:
            if not a["name"].endswith(".pdf"):
                self.assertNotIn("pages", a)

    @needs_pypdf
    def test_pdf_text_pages_and_links(self):
        m, att = letter()
        pdf = att["sports-day.pdf"]
        self.assertEqual((pdf["extracted"], pdf["truncated"], pdf["reason"], pdf["pages"]), (True, False, None, 2))
        self.assertIn("Thursday 12 November: races start at 09:00", pdf["text"])
        self.assertIn("Register: [ссылка 1]", pdf["text"])
        self.assertIn("medals at 12:30", pdf["text"])
        self.assertEqual(m["links"], ["https://forms.example/sports"])

    def test_without_reader_only_metadata(self):
        m = mailparse.parse_message(raw09())
        self.assertTrue(all(set(a) == {"name", "type", "size"} for a in m["attachments"]))


class PdfTest(unittest.TestCase):
    def test_broken_pdf(self):
        self.assertEqual(attachtext.read(b"%PDF-1.7\n1 0 obj << /Type /Catalog" + b"\x00\xff" * 50, "pdf"),
                         {"error": "broken"})

    @needs_pypdf
    def test_scan_without_text_layer(self):
        from pypdf import PdfWriter
        w, buf = PdfWriter(), io.BytesIO()
        w.add_blank_page(612, 792)
        w.write(buf)
        self.assertEqual(attachtext.read(buf.getvalue(), "pdf"), {"error": "scan"})

    @needs_pypdf
    def test_encrypted_pdf(self):
        from pypdf import PdfReader, PdfWriter

        def encrypted(user_password):
            w, buf = PdfWriter(clone_from=PdfReader(io.BytesIO(sports_pdf()))), io.BytesIO()
            w.encrypt(user_password=user_password, owner_password="owner", algorithm="RC4-128")
            w.write(buf)
            return buf.getvalue()

        self.assertEqual(attachtext.read(encrypted("secret"), "pdf"), {"error": "encrypted"})
        opened = attachtext.read(encrypted(""), "pdf")              # owner password only: readable
        self.assertIn("races start at 09:00", opened.get("text", ""))

    def test_timeout_kills_the_child(self):
        self.assertEqual(attachtext.read(b"%PDF-1.4\n", "pdf", timeout=0.001), {"error": "too_large"})


class ZipTest(unittest.TestCase):
    def test_zip_bomb_by_ratio(self):
        bomb = docx("", {"word/media/fill.bin": b"\0" * (30 * 1024 * 1024)})
        self.assertLess(len(bomb), 200_000)
        self.assertEqual(attachtext.read(bomb, "docx"), {"error": "too_large"})

    def test_zip_bomb_by_member_count(self):
        many = docx("", {f"customXml/item{i}.xml": b"<a/>" for i in range(attachtext.MAX_ZIP_FILES)})
        self.assertEqual(attachtext.read(many, "docx"), {"error": "too_large"})

    def test_zip_bomb_by_total_size(self):
        chunk = os.urandom(64) * (1024 * 1024 // 64)         # 1 MB, compresses below the ratio limit
        big = docx("", {f"word/media/{i}.bin": chunk for i in range(101)})
        self.assertEqual(attachtext.read(big, "docx"), {"error": "too_large"})

    def test_dtd_and_entities_are_refused(self):
        xxe = zipped({"word/document.xml": '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">'
                      '<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
                      f'<w:document {W}><w:body><w:p><w:r><w:t>&x;&b;</w:t></w:r></w:p></w:body></w:document>'})
        self.assertEqual(attachtext.read(xxe, "docx"), {"error": "broken"})

    def test_macros_are_never_read(self):
        doc = docx("<w:p><w:r><w:t>Normal text</w:t></w:r></w:p>",
                   {"word/vbaProject.bin": b"Sub AutoOpen() MACRO_PAYLOAD End Sub"})
        result = attachtext.read(doc, "docx")
        self.assertEqual(result["text"], "Normal text")
        self.assertNotIn("MACRO", str(result))
        m = with_attachments(("budget.xlsm", "application/vnd.ms-excel.sheet.macroEnabled.12", doc))
        self.assertEqual((m["attachments"][0]["extracted"], m["attachments"][0]["reason"]), (False, "unsupported"))

    def test_large_xlsx_is_streamed_and_cut(self):
        S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        rows = "".join(f'<row r="{i}"><c r="A{i}"><v>{i}</v></c><c r="B{i}" t="inlineStr"><is><t>Pupil {i}</t></is></c></row>'
                       for i in range(1, 60001))
        xlsx = zipped({"xl/worksheets/sheet1.xml": f'<?xml version="1.0"?><worksheet {S}><sheetData>{rows}</sheetData></worksheet>'})
        self.assertGreater(len(rows), 5_000_000)
        result = attachtext.read(xlsx, "xlsx")
        self.assertTrue(result["truncated"])
        self.assertTrue(result["text"].startswith("1 | Pupil 1\n2 | Pupil 2\n"))
        self.assertLessEqual(len(result["text"]), attachtext.TEXT_CAP)

    def test_damaged_office_files(self):
        for k in ("docx", "xlsx", "pptx"):
            self.assertEqual(attachtext.read(b"PK\x03\x04 not really a zip" * 10, k), {"error": "broken"}, k)


class LetterLimitsTest(unittest.TestCase):
    def test_cut_letter_means_too_large(self):
        m = with_attachments(("big.pdf", "application/pdf", b"%PDF-1.4\n1 0 obj <<"), truncated=True)
        self.assertEqual(m["attachments"][0]["reason"], "too_large")

    def test_character_budget_per_letter(self):
        text = ("Lorem ipsum dolor sit amet. " * 200).encode()           # 5600 characters each
        m = with_attachments(*[(f"{n}.txt", "text/plain", text) for n in "abc"])
        a, b, c = m["attachments"]
        self.assertLessEqual(len(a["text"]), attachtext.TEXT_CAP)
        self.assertTrue(a["truncated"] and b["truncated"])
        self.assertLessEqual(len(a["text"]) + len(b["text"]), mailparse.ATTACH_TOTAL)
        self.assertEqual((c["extracted"], c["reason"]), (False, "limit"))

    def test_attachment_text_follows_letter_rules(self):
        m = with_attachments(("note.txt", "text/plain", "Привет​‮ мир https://x.example/a\n".encode()),
                             ("e.ics", "text/calendar", b"BEGIN:VEVENT\r\nSUMMARY:Form https://x.example/b\r\n"
                                                        b"DTSTART;VALUE=DATE:20261105\r\nEND:VEVENT\r\n"))
        note, ics = m["attachments"]
        self.assertEqual(note["text"], "Привет мир [ссылка 1]")
        self.assertEqual(ics["events"][0]["summary"], "Form [ссылка 2]")
        self.assertEqual(ics["events"][0]["start"], "2026-11-05")
        self.assertEqual(m["links"], ["https://x.example/a", "https://x.example/b"])


if __name__ == "__main__":
    unittest.main()
